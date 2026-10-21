"""VoltMart inventory service.

Owns one number per product: how many units are actually available to promise.

The interesting part of this service is that it holds the only correctness
constraint in the whole application — stock must never go negative, even when
several checkouts race for the last unit. That is enforced by a conditional
atomic update in DynamoDB rather than by a read-modify-write in Python, so the
database arbitrates and the application does not have to lock.

Reservation model
-----------------
``available``  units free to promise to a new order
``reserved``   units held by an in-flight checkout, not yet shipped
``sold``       units committed to a completed order

A checkout reserves, then either commits (reserved -> sold) or releases
(reserved -> available) if a later step fails. Nothing is deducted permanently
until payment succeeds.
"""

from __future__ import annotations

import os
import time
from typing import Any

from pydantic import BaseModel, Field

from voltmart_common.config import storage_config
from voltmart_common.errors import Conflict, NotFound, OutOfStock, ValidationFailed
from voltmart_common.ids import is_safe_id, new_reservation_id
from voltmart_common.metrics import record_inventory_reservation, record_stock_level
from voltmart_common.service import create_app, run
from voltmart_common.storage import Repository, seed_if_empty
from voltmart_common.telemetry import tracer

SERVICE_NAME = "voltmart-inventory"

# Starting stock for the workshop. Two products are seeded deliberately low so
# that attendees can trigger and then trace an out-of-stock failure.
INITIAL_STOCK: dict[str, int] = {
    "vm-lap-001": 24,
    "vm-lap-002": 60,
    "vm-lap-003": 9,
    "vm-phn-001": 85,
    "vm-phn-002": 120,
    "vm-phn-003": 14,
    "vm-aud-001": 150,
    "vm-aud-002": 210,
    "vm-aud-003": 18,
    "vm-aud-004": 95,
    "vm-wear-001": 110,
    "vm-wear-002": 2,
    "vm-tv-001": 16,
    "vm-tv-002": 31,
    "vm-cam-001": 7,
    "vm-cam-002": 12,
    "vm-cam-003": 64,
    "vm-gam-001": 40,
    "vm-gam-002": 1,
    "vm-gam-003": 140,
    "vm-hom-001": 72,
    "vm-hom-002": 88,
    "vm-acc-001": 300,
    "vm-acc-002": 180,
    "vm-acc-003": 45,
    "vm-acc-004": 130,
}

LOW_STOCK_THRESHOLD = int(os.getenv("LOW_STOCK_THRESHOLD", "10"))


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
class StockRecord(BaseModel):
    product_id: str
    available: int
    reserved: int
    sold: int
    updated_at: float
    status: str
    low_stock_threshold: int


class ReserveLine(BaseModel):
    product_id: str
    quantity: int = Field(..., ge=1, le=50)


class ReserveRequest(BaseModel):
    lines: list[ReserveLine] = Field(..., min_length=1, max_length=50)
    reference: str = Field("", max_length=80, description="Order or checkout id, for audit")


class ReservationResult(BaseModel):
    reservation_id: str
    reserved: list[ReserveLine]
    reference: str


class RestockRequest(BaseModel):
    product_id: str
    quantity: int = Field(..., ge=1, le=10_000)


# --------------------------------------------------------------------------
# Repository
# --------------------------------------------------------------------------
STORAGE = storage_config()
REPO = Repository(STORAGE, key_name="product_id")


def _seed() -> None:
    now = time.time()
    seed_if_empty(
        REPO,
        [
            {
                "product_id": product_id,
                "available": quantity,
                "reserved": 0,
                "sold": 0,
                "updated_at": now,
            }
            for product_id, quantity in INITIAL_STOCK.items()
        ],
    )


def _status(available: int) -> str:
    if available <= 0:
        return "out_of_stock"
    if available <= LOW_STOCK_THRESHOLD:
        return "low_stock"
    return "in_stock"


def _to_record(item: dict[str, Any]) -> StockRecord:
    available = int(item.get("available", 0))
    return StockRecord(
        product_id=item["product_id"],
        available=available,
        reserved=int(item.get("reserved", 0)),
        sold=int(item.get("sold", 0)),
        updated_at=float(item.get("updated_at", 0.0)),
        status=_status(available),
        low_stock_threshold=LOW_STOCK_THRESHOLD,
    )


async def _startup() -> None:
    _seed()


app, log = create_app(
    SERVICE_NAME,
    title="VoltMart Inventory",
    description="Stock levels, reservations and restocking.",
    readiness_check=REPO.health,
    on_startup=_startup,
)


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------
@app.get("/inventory/{product_id}", response_model=StockRecord, tags=["inventory"])
async def get_stock(product_id: str) -> StockRecord:
    if not is_safe_id(product_id):
        raise ValidationFailed("product_id is not a valid identifier")
    item = REPO.get(product_id)
    if item is None:
        raise NotFound(f"No inventory record for {product_id}", product_id=product_id)
    record = _to_record(item)
    record_stock_level(product_id, record.available)
    return record


@app.get("/inventory", response_model=list[StockRecord], tags=["inventory"])
async def list_stock(limit: int = 200) -> list[StockRecord]:
    """Every stock record. The table has one row per product, so this is cheap."""
    return [_to_record(item) for item in REPO.scan(limit=limit)]


@app.get("/inventory/alerts/low-stock", response_model=list[StockRecord], tags=["inventory"])
async def low_stock() -> list[StockRecord]:
    """Products at or below the low-stock threshold."""
    records = [_to_record(item) for item in REPO.scan(limit=500)]
    return sorted(
        (r for r in records if r.available <= LOW_STOCK_THRESHOLD),
        key=lambda r: r.available,
    )


# --------------------------------------------------------------------------
# Reservation lifecycle
# --------------------------------------------------------------------------
@app.post("/inventory/reserve", response_model=ReservationResult, tags=["inventory"])
async def reserve(request: ReserveRequest) -> ReservationResult:
    """Hold stock for an in-flight checkout.

    All-or-nothing: if any line cannot be satisfied, the lines already reserved
    in this call are released before the error is returned. Without that, a
    multi-item checkout that fails on its last line would leave the earlier
    items stranded until a human noticed.
    """
    reservation_id = new_reservation_id()
    applied: list[ReserveLine] = []

    with tracer(SERVICE_NAME).start_as_current_span("inventory.reserve") as span:
        span.set_attribute("voltmart.reservation.id", reservation_id)
        span.set_attribute("voltmart.reservation.line_count", len(request.lines))
        if request.reference:
            span.set_attribute("voltmart.reservation.reference", request.reference)

        for line in request.lines:
            try:
                # Atomic and conditional: the write only lands while available
                # would stay at or above zero.
                REPO.add_to_number(
                    line.product_id, "available", -line.quantity, floor=0
                )
            except NotFound:
                await _release_lines(applied)
                record_inventory_reservation(line.product_id, line.quantity, "unknown_product")
                raise NotFound(
                    f"No inventory record for {line.product_id}",
                    product_id=line.product_id,
                )
            except Conflict:
                await _release_lines(applied)
                current = REPO.get(line.product_id) or {}
                available = int(current.get("available", 0))
                record_inventory_reservation(line.product_id, line.quantity, "insufficient")
                span.set_attribute("voltmart.reservation.outcome", "insufficient_stock")
                raise OutOfStock(
                    f"Only {available} unit(s) of {line.product_id} are available, "
                    f"{line.quantity} requested.",
                    product_id=line.product_id,
                    requested=line.quantity,
                    available=available,
                )

            REPO.add_to_number(line.product_id, "reserved", line.quantity)
            REPO.update_fields(line.product_id, {"updated_at": time.time()})
            applied.append(line)
            record_inventory_reservation(line.product_id, line.quantity, "reserved")

        span.set_attribute("voltmart.reservation.outcome", "reserved")

    log.info(
        "reserved lines=%d reservation_id=%s reference=%s",
        len(applied),
        reservation_id,
        request.reference or "-",
    )
    return ReservationResult(
        reservation_id=reservation_id, reserved=applied, reference=request.reference
    )


async def _release_lines(lines: list[ReserveLine]) -> None:
    """Undo reservations made earlier in a failed reserve call."""
    for line in lines:
        try:
            REPO.add_to_number(line.product_id, "available", line.quantity)
            REPO.add_to_number(line.product_id, "reserved", -line.quantity)
        except (NotFound, Conflict):
            # Logged rather than raised: the caller is already handling a
            # failure and this leak is recoverable by the reconciliation view.
            log.error(
                "failed to roll back reservation product_id=%s quantity=%d",
                line.product_id,
                line.quantity,
            )


@app.post("/inventory/release", response_model=list[StockRecord], tags=["inventory"])
async def release(request: ReserveRequest) -> list[StockRecord]:
    """Return reserved units to available stock after a failed checkout."""
    results = []
    with tracer(SERVICE_NAME).start_as_current_span("inventory.release") as span:
        span.set_attribute("voltmart.reservation.reference", request.reference or "-")
        for line in request.lines:
            item = REPO.get(line.product_id)
            if item is None:
                raise NotFound(
                    f"No inventory record for {line.product_id}",
                    product_id=line.product_id,
                )
            # Clamp to what is actually reserved. A duplicate release must not
            # invent stock that was never held.
            releasable = min(line.quantity, int(item.get("reserved", 0)))
            if releasable:
                REPO.add_to_number(line.product_id, "available", releasable)
                REPO.add_to_number(line.product_id, "reserved", -releasable)
            updated = REPO.update_fields(line.product_id, {"updated_at": time.time()})
            results.append(_to_record(updated))
            record_inventory_reservation(line.product_id, releasable, "released")
    return results


@app.post("/inventory/commit", response_model=list[StockRecord], tags=["inventory"])
async def commit(request: ReserveRequest) -> list[StockRecord]:
    """Convert a reservation into a sale once payment has been authorized."""
    results = []
    with tracer(SERVICE_NAME).start_as_current_span("inventory.commit") as span:
        span.set_attribute("voltmart.reservation.reference", request.reference or "-")
        for line in request.lines:
            item = REPO.get(line.product_id)
            if item is None:
                raise NotFound(
                    f"No inventory record for {line.product_id}",
                    product_id=line.product_id,
                )
            committable = min(line.quantity, int(item.get("reserved", 0)))
            if committable:
                REPO.add_to_number(line.product_id, "reserved", -committable)
                REPO.add_to_number(line.product_id, "sold", committable)
            updated = REPO.update_fields(line.product_id, {"updated_at": time.time()})
            results.append(_to_record(updated))
            record_inventory_reservation(line.product_id, committable, "committed")
    return results


@app.post("/inventory/return", response_model=list[StockRecord], tags=["inventory"])
async def return_units(request: ReserveRequest) -> list[StockRecord]:
    """Put sold units back on sale.

    This is the counterpart to ``/inventory/commit``, and it exists because
    ``/inventory/release`` cannot do the job. Release moves ``reserved ->
    available``; once a checkout has committed, the units are in ``sold`` and
    ``reserved`` is back to zero, so a release finds nothing to give back and
    silently restores nothing.

    Cancelling a confirmed order, or accepting a return, lands here instead:
    ``sold -> available``.
    """
    results = []
    with tracer(SERVICE_NAME).start_as_current_span("inventory.return") as span:
        span.set_attribute("voltmart.reservation.reference", request.reference or "-")
        for line in request.lines:
            item = REPO.get(line.product_id)
            if item is None:
                raise NotFound(
                    f"No inventory record for {line.product_id}",
                    product_id=line.product_id,
                )
            # Clamped to what was actually sold, so a duplicate return cannot
            # manufacture stock that never existed.
            returnable = min(line.quantity, int(item.get("sold", 0)))
            if returnable:
                REPO.add_to_number(line.product_id, "sold", -returnable)
                REPO.add_to_number(line.product_id, "available", returnable)
            updated = REPO.update_fields(line.product_id, {"updated_at": time.time()})
            results.append(_to_record(updated))
            record_inventory_reservation(line.product_id, returnable, "returned")

            if returnable < line.quantity:
                log.warning(
                    "return clamped product_id=%s requested=%d returned=%d reference=%s",
                    line.product_id,
                    line.quantity,
                    returnable,
                    request.reference or "-",
                )
    return results


@app.post("/inventory/restock", response_model=StockRecord, tags=["inventory"])
async def restock(request: RestockRequest) -> StockRecord:
    """Add units to available stock.

    A store operator action. In a real deployment this would sit behind an
    authenticated admin route; here it is in-cluster only and exists so a
    workshop can refill the deliberately-scarce products.
    """
    if not is_safe_id(request.product_id):
        raise ValidationFailed("product_id is not a valid identifier")
    item = REPO.get(request.product_id)
    if item is None:
        # Unknown products get a fresh record rather than an error, so a
        # restock can introduce a product the inventory seed missed.
        REPO.put(
            {
                "product_id": request.product_id,
                "available": request.quantity,
                "reserved": 0,
                "sold": 0,
                "updated_at": time.time(),
            }
        )
    else:
        REPO.add_to_number(request.product_id, "available", request.quantity)
        REPO.update_fields(request.product_id, {"updated_at": time.time()})

    updated = REPO.require(request.product_id)
    log.info("restocked product_id=%s quantity=%d", request.product_id, request.quantity)
    return _to_record(updated)


if __name__ == "__main__":
    run("main:app")
