"""VoltMart orders service.

Owns the order record and, more importantly, the rules about how an order is
allowed to change.

Two decisions worth calling out:

*Price capture.* The order prices its own lines from the catalog at creation
time and stores those prices permanently. A cart re-prices on every read; an
order never does. What the customer agreed to pay is what the record says,
forever, regardless of later price changes.

*Guarded transitions.* Status changes go through one table of legal moves, and
the write is conditional on the current status. Two concurrent cancel requests
cannot both succeed, which matters because each one would otherwise trigger its
own refund.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field

from voltmart_common.config import downstream_config, storage_config
from voltmart_common.errors import Conflict, NotFound, ValidationFailed
from voltmart_common.http import ServiceClients
from voltmart_common.ids import is_safe_id, new_order_id
from voltmart_common.metrics import record_order_created, record_order_status_change
from voltmart_common.service import create_app, run
from voltmart_common.storage import Repository
from voltmart_common.telemetry import tracer

SERVICE_NAME = "voltmart-orders"

TAX_RATE = 0.0875
FREE_SHIPPING_THRESHOLD = 99.0
STANDARD_SHIPPING = 9.95

OrderStatus = Literal[
    "pending_payment",
    "confirmed",
    "packed",
    "shipped",
    "delivered",
    "cancelled",
    "payment_failed",
]

# The only legal moves. Anything not listed here is rejected with a 409, which
# is how "cancel an order that already shipped" gets refused.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "pending_payment": {"confirmed", "payment_failed", "cancelled"},
    "confirmed": {"packed", "cancelled"},
    "packed": {"shipped", "cancelled"},
    "shipped": {"delivered"},
    "delivered": set(),
    "cancelled": set(),
    "payment_failed": {"cancelled"},
}

# Statuses from which a customer may still cancel without talking to a human.
CUSTOMER_CANCELLABLE = {"pending_payment", "confirmed", "packed"}

SHIPPING_DAYS = {"standard": 5, "express": 2, "overnight": 1}


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
class OrderLineRequest(BaseModel):
    product_id: str
    quantity: int = Field(..., ge=1, le=20)


class ShippingAddress(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=120)
    line1: str = Field(..., min_length=1, max_length=160)
    line2: str = Field("", max_length=160)
    city: str = Field(..., min_length=1, max_length=80)
    region: str = Field(..., min_length=1, max_length=80)
    postal_code: str = Field(..., min_length=3, max_length=16)
    country: str = Field("US", min_length=2, max_length=2)


class CreateOrderRequest(BaseModel):
    customer_id: str = Field(..., min_length=1, max_length=64)
    email: EmailStr
    lines: list[OrderLineRequest] = Field(..., min_length=1, max_length=40)
    shipping_address: ShippingAddress
    shipping_method: Literal["standard", "express", "overnight"] = "standard"
    cart_id: str = Field("", max_length=64)


class OrderLine(BaseModel):
    product_id: str
    name: str
    brand: str
    category: str
    quantity: int
    unit_price: float
    line_total: float


class OrderTotals(BaseModel):
    subtotal: float
    tax: float
    shipping: float
    total: float
    currency: str = "USD"


class StatusEvent(BaseModel):
    status: str
    at: float
    note: str = ""


class Order(BaseModel):
    order_id: str
    customer_id: str
    email: str
    status: OrderStatus
    lines: list[OrderLine]
    totals: OrderTotals
    shipping_address: ShippingAddress
    shipping_method: str
    payment_id: str = ""
    cart_id: str = ""
    created_at: float
    updated_at: float
    estimated_delivery: str
    tracking_number: str = ""
    cancellable: bool
    history: list[StatusEvent] = Field(default_factory=list)


class ConfirmRequest(BaseModel):
    payment_id: str = Field(..., min_length=1, max_length=64)


class CancelRequest(BaseModel):
    reason: str = Field("customer_request", max_length=200)


class AdvanceRequest(BaseModel):
    status: OrderStatus
    note: str = Field("", max_length=200)


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------
STORAGE = storage_config()
REPO = Repository(STORAGE, key_name="order_id")
CLIENTS = ServiceClients(downstream_config())


async def _shutdown() -> None:
    await CLIENTS.aclose()


app, log = create_app(
    SERVICE_NAME,
    title="VoltMart Orders",
    description="Order creation, status transitions and history.",
    readiness_check=REPO.health,
    on_shutdown=_shutdown,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _round(value: float) -> float:
    return round(value + 1e-9, 2)


def _estimated_delivery(created_at: float, method: str) -> str:
    """Business-day estimate, skipping weekends."""
    days = SHIPPING_DAYS.get(method, 5)
    date = datetime.fromtimestamp(created_at, tz=timezone.utc)
    added = 0
    while added < days:
        date += timedelta(days=1)
        if date.weekday() < 5:
            added += 1
    return date.date().isoformat()


def _to_order(item: dict[str, Any]) -> Order:
    status = item["status"]
    return Order(
        order_id=item["order_id"],
        customer_id=item["customer_id"],
        email=item["email"],
        status=status,
        lines=[OrderLine(**line) for line in item.get("lines", [])],
        totals=OrderTotals(**item["totals"]),
        shipping_address=ShippingAddress(**item["shipping_address"]),
        shipping_method=item.get("shipping_method", "standard"),
        payment_id=item.get("payment_id", ""),
        cart_id=item.get("cart_id", ""),
        created_at=float(item["created_at"]),
        updated_at=float(item["updated_at"]),
        estimated_delivery=item.get("estimated_delivery", ""),
        tracking_number=item.get("tracking_number", ""),
        cancellable=status in CUSTOMER_CANCELLABLE,
        history=[StatusEvent(**event) for event in item.get("history", [])],
    )


def _load(order_id: str) -> dict[str, Any]:
    if not is_safe_id(order_id):
        raise ValidationFailed("order_id is not a valid identifier")
    item = REPO.get(order_id)
    if item is None:
        raise NotFound(f"No order with id {order_id}", order_id=order_id)
    return item


def _transition(item: dict[str, Any], to_status: str, note: str = "") -> dict[str, Any]:
    """Apply a status change if the move is legal and nobody else got there first."""
    from_status = item["status"]

    if to_status == from_status:
        raise Conflict(
            f"Order {item['order_id']} is already {from_status}.",
            order_id=item["order_id"],
            status=from_status,
        )
    if to_status not in ALLOWED_TRANSITIONS.get(from_status, set()):
        raise Conflict(
            f"An order that is {from_status} cannot move to {to_status}.",
            order_id=item["order_id"],
            current_status=from_status,
            requested_status=to_status,
            allowed=sorted(ALLOWED_TRANSITIONS.get(from_status, set())),
        )

    history = list(item.get("history", []))
    history.append({"status": to_status, "at": time.time(), "note": note})

    fields: dict[str, Any] = {
        "status": to_status,
        "updated_at": time.time(),
        "history": history,
    }
    if to_status == "shipped" and not item.get("tracking_number"):
        # Deterministic from the order id so it is stable across reads.
        fields["tracking_number"] = f"1Z{item['order_id'].replace('-', '')}"

    # Conditional on the status we just read: a concurrent writer that already
    # moved the order makes this write fail rather than silently overwrite.
    updated = REPO.update_fields(
        item["order_id"], fields, expected={"status": from_status}
    )
    record_order_status_change(from_status, to_status)
    log.info(
        "order transition order_id=%s from=%s to=%s",
        item["order_id"],
        from_status,
        to_status,
    )
    return updated


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.post("/orders", response_model=Order, status_code=201, tags=["orders"])
async def create_order(request: CreateOrderRequest) -> Order:
    """Create an order in ``pending_payment``, pricing every line now.

    The order is not a sale yet. The checkout service reserves stock before
    calling this, and confirms the order only once payment is authorized.
    """
    with tracer(SERVICE_NAME).start_as_current_span("orders.create") as span:
        product_ids = [line.product_id for line in request.lines]
        products = await CLIENTS.catalog.post(
            "/products/batch", json={"product_ids": product_ids}
        )
        by_id = {product["product_id"]: product for product in (products or [])}

        missing = [pid for pid in product_ids if pid not in by_id]
        if missing:
            # An order referencing a product that does not exist can never be
            # fulfilled, so this is a hard failure rather than a dropped line.
            raise ValidationFailed(
                "Some products in this order are no longer available.",
                product_ids=missing,
            )

        lines: list[dict[str, Any]] = []
        for requested in request.lines:
            product = by_id[requested.product_id]
            unit_price = float(product["price"])
            lines.append(
                {
                    "product_id": requested.product_id,
                    "name": product["name"],
                    "brand": product["brand"],
                    "category": product["category"],
                    "quantity": requested.quantity,
                    "unit_price": unit_price,
                    "line_total": _round(unit_price * requested.quantity),
                }
            )

        subtotal = _round(sum(line["line_total"] for line in lines))
        shipping = (
            0.0
            if subtotal >= FREE_SHIPPING_THRESHOLD and request.shipping_method == "standard"
            else STANDARD_SHIPPING
            if request.shipping_method == "standard"
            else STANDARD_SHIPPING * (2 if request.shipping_method == "express" else 3)
        )
        tax = _round(subtotal * TAX_RATE)
        total = _round(subtotal + tax + shipping)

        now = time.time()
        order_id = new_order_id()
        item = {
            "order_id": order_id,
            "customer_id": request.customer_id,
            "email": str(request.email),
            "status": "pending_payment",
            "lines": lines,
            "totals": {
                "subtotal": subtotal,
                "tax": tax,
                "shipping": shipping,
                "total": total,
                "currency": "USD",
            },
            "shipping_address": request.shipping_address.model_dump(),
            "shipping_method": request.shipping_method,
            "payment_id": "",
            "cart_id": request.cart_id,
            "created_at": now,
            "updated_at": now,
            "estimated_delivery": _estimated_delivery(now, request.shipping_method),
            "tracking_number": "",
            "history": [
                {"status": "pending_payment", "at": now, "note": "Order created"}
            ],
        }
        REPO.put_if_absent(item)

        span.set_attribute("voltmart.order.id", order_id)
        span.set_attribute("voltmart.order.total", total)
        span.set_attribute("voltmart.order.line_count", len(lines))

    record_order_created(total, sum(line["quantity"] for line in lines), "USD")
    log.info("order created order_id=%s total=%.2f", order_id, total)
    return _to_order(item)


@app.get("/orders/{order_id}", response_model=Order, tags=["orders"])
async def get_order(order_id: str) -> Order:
    return _to_order(_load(order_id))


@app.get("/orders", response_model=list[Order], tags=["orders"])
async def list_orders(customer_id: str, limit: int = 25) -> list[Order]:
    """Orders for one customer, newest first.

    ``customer_id`` is required rather than optional. An endpoint that lists
    every order when the filter is omitted is the kind of thing that quietly
    becomes a data leak.
    """
    if not is_safe_id(customer_id):
        raise ValidationFailed("customer_id is not a valid identifier")

    items = REPO.query_index(
        "customer-index", "customer_id", customer_id, limit=max(limit, 1)
    )
    items.sort(key=lambda item: float(item.get("created_at", 0.0)), reverse=True)
    return [_to_order(item) for item in items[:limit]]


@app.post("/orders/{order_id}/confirm", response_model=Order, tags=["orders"])
async def confirm_order(order_id: str, request: ConfirmRequest) -> Order:
    """Mark an order paid. Called by checkout after payment authorization."""
    item = _load(order_id)
    REPO.update_fields(order_id, {"payment_id": request.payment_id})
    item["payment_id"] = request.payment_id
    updated = _transition(item, "confirmed", note="Payment authorized")
    return _to_order(updated)


@app.post("/orders/{order_id}/fail", response_model=Order, tags=["orders"])
async def fail_order(order_id: str, request: CancelRequest) -> Order:
    """Mark an order as failed because payment did not go through."""
    item = _load(order_id)
    updated = _transition(item, "payment_failed", note=request.reason)
    return _to_order(updated)


@app.post("/orders/{order_id}/cancel", response_model=Order, tags=["orders"])
async def cancel_order(order_id: str, request: CancelRequest) -> Order:
    """Cancel an order.

    Refusing a cancellation after dispatch is intentional: the goods are
    physically gone, so the correct path is a return, not a cancellation. The
    error body lists the current status so the support agent can explain why.
    """
    item = _load(order_id)
    status_before = item["status"]

    if status_before not in CUSTOMER_CANCELLABLE:
        raise Conflict(
            f"Order {order_id} is {status_before} and can no longer be cancelled. "
            "Once a parcel has shipped it has to be returned instead.",
            order_id=order_id,
            status=status_before,
            cancellable_statuses=sorted(CUSTOMER_CANCELLABLE),
        )

    updated = _transition(item, "cancelled", note=request.reason)

    # Put the stock back. Which endpoint to call depends on where the order got
    # to, because the units are sitting in a different bucket in each case:
    #
    #   pending_payment  -> checkout reserved but never committed, so the units
    #                       are in `reserved` and need releasing.
    #   confirmed/packed -> checkout committed them, so they are in `sold` and
    #                       need returning.
    #
    # Calling release on a committed order is a silent no-op: `reserved` is
    # already zero, so nothing is given back and the stock stays lost.
    endpoint = (
        "/inventory/release" if status_before == "pending_payment" else "/inventory/return"
    )

    lines = [
        {"product_id": line["product_id"], "quantity": line["quantity"]}
        for line in item.get("lines", [])
    ]
    if lines:
        try:
            await CLIENTS.inventory.post(
                endpoint, json={"lines": lines, "reference": order_id}
            )
            log.info(
                "stock restored order_id=%s via=%s from_status=%s",
                order_id,
                endpoint,
                status_before,
            )
        except Exception as exc:
            # Deliberately not fatal. The customer asked to cancel and the
            # cancellation has happened; stranded stock is recoverable from the
            # low-stock view and the compensation metric, whereas failing here
            # would leave the order in limbo.
            log.error(
                "stock restore failed after cancelling order_id=%s endpoint=%s: %s",
                order_id,
                endpoint,
                exc,
            )

    return _to_order(updated)


@app.post("/orders/{order_id}/advance", response_model=Order, tags=["orders"])
async def advance_order(order_id: str, request: AdvanceRequest) -> Order:
    """Move an order along its fulfilment path.

    Stands in for the warehouse system a real store would have. Workshops use it
    to produce orders in every status so the UI and the agent have something
    realistic to talk about.
    """
    item = _load(order_id)
    updated = _transition(item, request.status, note=request.note or "Manual advance")
    return _to_order(updated)


@app.get("/orders/{order_id}/tracking", tags=["orders"])
async def tracking(order_id: str) -> dict[str, Any]:
    """Shipment view: where the parcel is and when it should arrive."""
    item = _load(order_id)
    status = item["status"]
    return {
        "order_id": order_id,
        "status": status,
        "tracking_number": item.get("tracking_number", ""),
        "carrier": "VoltMart Logistics" if item.get("tracking_number") else "",
        "estimated_delivery": item.get("estimated_delivery", ""),
        "shipping_method": item.get("shipping_method", "standard"),
        "in_transit": status == "shipped",
        "delivered": status == "delivered",
        "history": item.get("history", []),
    }


if __name__ == "__main__":
    run("main:app")
