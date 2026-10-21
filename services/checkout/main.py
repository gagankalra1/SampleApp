"""VoltMart checkout service — the saga that turns a cart into an order.

This is where the distributed-systems content of the workshop lives. A single
``POST /checkout`` fans out to five services in sequence:

    cart  ->  inventory.reserve  ->  orders.create
              ->  payment.authorize  ->  orders.confirm
              ->  inventory.commit   ->  cart.clear

There is no distributed transaction. Each step is a separate HTTP call that can
succeed or fail on its own, so the saga keeps a stack of compensating actions
and unwinds it in reverse when a step fails. The result is a trace with a
visible shape: a happy path is one straight line of spans, and a failure is a
line that stops and then grows a compensation branch.

Two things the compensation logic takes care to get right:

*Compensations never raise.* If releasing a reservation fails while we are
already handling a payment decline, raising would replace a precise 402 with a
useless 500. The failure is recorded on the span and counted in a metric that
operators alert on, because it means stock is stranded and needs repair.

*Order of unwinding matters.* Stock is released before the order is marked
failed, so an operator reading the order history sees the terminal state last.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Literal

from opentelemetry import trace
from pydantic import BaseModel, EmailStr, Field

from voltmart_common.config import downstream_config
from voltmart_common.errors import (
    Conflict,
    OutOfStock,
    PaymentDeclined,
    ValidationFailed,
    VoltMartError,
)
from voltmart_common.http import ServiceClients
from voltmart_common.ids import is_safe_id
from voltmart_common.metrics import (
    record_checkout,
    record_checkout_duration,
    record_compensation,
)
from voltmart_common.service import create_app, run
from voltmart_common.telemetry import current_trace_id, tracer

SERVICE_NAME = "voltmart-checkout"


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
class ShippingAddress(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=120)
    line1: str = Field(..., min_length=1, max_length=160)
    line2: str = Field("", max_length=160)
    city: str = Field(..., min_length=1, max_length=80)
    region: str = Field(..., min_length=1, max_length=80)
    postal_code: str = Field(..., min_length=3, max_length=16)
    country: str = Field("US", min_length=2, max_length=2)


class CardDetails(BaseModel):
    model_config = {"extra": "forbid"}

    number: str = Field(..., min_length=12, max_length=19, repr=False)
    exp_month: int = Field(..., ge=1, le=12)
    exp_year: int = Field(..., ge=2024, le=2099)
    cvc: str = Field(..., min_length=3, max_length=4, repr=False)
    holder_name: str = Field(..., min_length=1, max_length=120)


class CheckoutRequest(BaseModel):
    cart_id: str = Field(..., min_length=1, max_length=64)
    customer_id: str = Field(..., min_length=1, max_length=64)
    email: EmailStr
    shipping_address: ShippingAddress
    shipping_method: Literal["standard", "express", "overnight"] = "standard"
    payment_method: Literal["card", "wallet", "gift_card"] = "card"
    card: CardDetails | None = None


class CheckoutResult(BaseModel):
    order_id: str
    payment_id: str
    status: str
    total: float
    currency: str
    estimated_delivery: str
    email: str
    trace_id: str | None = None
    steps_completed: list[str]
    duration_ms: int


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------
CLIENTS = ServiceClients(downstream_config())


async def _shutdown() -> None:
    await CLIENTS.aclose()


app, log = create_app(
    SERVICE_NAME,
    title="VoltMart Checkout",
    description="Orchestrates cart, inventory, orders and payment into one purchase.",
    on_shutdown=_shutdown,
)


# --------------------------------------------------------------------------
# Saga machinery
# --------------------------------------------------------------------------
class Saga:
    """Tracks completed steps and the compensation for each one."""

    def __init__(self, name: str):
        self.name = name
        self.completed: list[str] = []
        self._compensations: list[tuple[str, Callable[[], Awaitable[None]]]] = []

    def record(self, step: str, compensation: Callable[[], Awaitable[None]] | None = None) -> None:
        self.completed.append(step)
        if compensation is not None:
            self._compensations.append((step, compensation))

    async def unwind(self, failed_step: str) -> None:
        """Run compensations newest-first. Never raises."""
        span = trace.get_current_span()
        span.set_attribute("voltmart.saga.failed_step", failed_step)
        span.set_attribute("voltmart.saga.compensations", len(self._compensations))

        for step, compensate in reversed(self._compensations):
            with tracer(SERVICE_NAME).start_as_current_span(
                f"checkout.compensate.{step}"
            ) as compensation_span:
                try:
                    await compensate()
                    record_compensation(step, succeeded=True)
                    log.info("compensated step=%s saga=%s", step, self.name)
                except Exception as exc:
                    # The system is now inconsistent and a human has to fix it.
                    # This counter is the alarm for that.
                    compensation_span.record_exception(exc)
                    compensation_span.set_status(
                        trace.Status(trace.StatusCode.ERROR, "compensation failed")
                    )
                    record_compensation(step, succeeded=False)
                    log.error(
                        "COMPENSATION FAILED step=%s saga=%s error=%s "
                        "(manual reconciliation required)",
                        step,
                        self.name,
                        exc,
                    )


# --------------------------------------------------------------------------
# Route
# --------------------------------------------------------------------------
@app.post("/checkout", response_model=CheckoutResult, status_code=201, tags=["checkout"])
async def checkout(request: CheckoutRequest) -> CheckoutResult:
    """Buy everything in a cart.

    Returns 201 with the order on success. On failure the status code says what
    went wrong and who can fix it: 409 for an empty or stale cart, 402 for a
    declined card, 503 when a dependency is down.
    """
    if not is_safe_id(request.cart_id) or not is_safe_id(request.customer_id):
        raise ValidationFailed("cart_id and customer_id must be valid identifiers")
    if request.payment_method == "card" and request.card is None:
        raise ValidationFailed("Card details are required for a card payment.")

    started = time.perf_counter()
    saga = Saga(f"checkout:{request.cart_id}")
    reserved_lines: list[dict[str, Any]] = []
    order_id = ""

    root = tracer(SERVICE_NAME).start_as_current_span("checkout.saga")
    with root as span:
        span.set_attribute("voltmart.checkout.cart_id", request.cart_id)
        span.set_attribute("voltmart.checkout.customer_id", request.customer_id)
        span.set_attribute("voltmart.checkout.shipping_method", request.shipping_method)
        span.set_attribute("voltmart.checkout.payment_method", request.payment_method)

        try:
            # ---- 1. Read the cart ------------------------------------------
            with tracer(SERVICE_NAME).start_as_current_span("checkout.load_cart"):
                cart = await CLIENTS.cart.get(f"/carts/{request.cart_id}")

            lines = cart.get("lines", [])
            if not lines:
                raise Conflict(
                    "This cart is empty. Add something before checking out.",
                    cart_id=request.cart_id,
                )
            saga.record("load_cart")
            span.set_attribute("voltmart.checkout.line_count", len(lines))

            reserved_lines = [
                {"product_id": line["product_id"], "quantity": line["quantity"]}
                for line in lines
            ]

            # ---- 2. Reserve stock ------------------------------------------
            # Before taking money. The alternative — charge first, then discover
            # the item is gone — is a refund and an apology every time.
            with tracer(SERVICE_NAME).start_as_current_span("checkout.reserve_stock"):
                reservation = await CLIENTS.inventory.post(
                    "/inventory/reserve",
                    json={"lines": reserved_lines, "reference": request.cart_id},
                )
            reservation_id = reservation["reservation_id"]
            span.set_attribute("voltmart.checkout.reservation_id", reservation_id)

            async def _release_stock() -> None:
                await CLIENTS.inventory.post(
                    "/inventory/release",
                    json={"lines": reserved_lines, "reference": order_id or request.cart_id},
                )

            saga.record("reserve_stock", _release_stock)

            # ---- 3. Create the order ---------------------------------------
            with tracer(SERVICE_NAME).start_as_current_span("checkout.create_order"):
                order = await CLIENTS.orders.post(
                    "/orders",
                    json={
                        "customer_id": request.customer_id,
                        "email": str(request.email),
                        "lines": reserved_lines,
                        "shipping_address": request.shipping_address.model_dump(),
                        "shipping_method": request.shipping_method,
                        "cart_id": request.cart_id,
                    },
                )
            order_id = order["order_id"]
            order_total = float(order["totals"]["total"])
            span.set_attribute("voltmart.order.id", order_id)
            span.set_attribute("voltmart.order.total", order_total)

            async def _unwind_order() -> None:
                # Which terminal state the order can reach depends on how far
                # the saga got. `pending_payment` can move to `payment_failed`;
                # once `confirm_order` has run the order is `confirmed`, and the
                # orders service only allows `cancelled` from there. Trying
                # `fail` on a confirmed order is a 409, so prefer `cancel` when
                # we know we got past confirmation.
                if "confirm_order" in saga.completed:
                    await CLIENTS.orders.post(
                        f"/orders/{order_id}/cancel",
                        json={"reason": "Checkout could not be completed"},
                    )
                else:
                    await CLIENTS.orders.post(
                        f"/orders/{order_id}/fail",
                        json={"reason": "Checkout could not be completed"},
                    )

            saga.record("create_order", _unwind_order)

            # ---- 4. Take payment -------------------------------------------
            payment_payload: dict[str, Any] = {
                "order_id": order_id,
                "amount": order_total,
                "currency": order["totals"].get("currency", "USD"),
                "method": request.payment_method,
            }
            if request.card is not None:
                # Card data is forwarded once, to the service whose job it is,
                # and is not stored or logged anywhere along the way.
                payment_payload["card"] = request.card.model_dump()

            with tracer(SERVICE_NAME).start_as_current_span("checkout.authorize_payment"):
                payment = await CLIENTS.payment.post("/payments", json=payment_payload)
            payment_id = payment["payment_id"]
            span.set_attribute("voltmart.payment.id", payment_id)

            async def _refund_payment() -> None:
                await CLIENTS.payment.post(
                    f"/payments/{payment_id}/refund",
                    json={"reason": "checkout_failed_after_authorization"},
                )

            saga.record("authorize_payment", _refund_payment)

            # ---- 5. Confirm the order --------------------------------------
            with tracer(SERVICE_NAME).start_as_current_span("checkout.confirm_order"):
                confirmed = await CLIENTS.orders.post(
                    f"/orders/{order_id}/confirm", json={"payment_id": payment_id}
                )
            saga.record("confirm_order")

            # ---- 6. Commit the reservation ---------------------------------
            # Past this point the sale is real: reserved units become sold.
            with tracer(SERVICE_NAME).start_as_current_span("checkout.commit_stock"):
                await CLIENTS.inventory.post(
                    "/inventory/commit",
                    json={"lines": reserved_lines, "reference": order_id},
                )
            saga.record("commit_stock")

            # ---- 7. Empty the cart -----------------------------------------
            # Best effort on purpose. The order exists and is paid for; a cart
            # that failed to clear is a cosmetic problem, and failing the
            # checkout here would be far worse than a stale cart.
            try:
                with tracer(SERVICE_NAME).start_as_current_span("checkout.clear_cart"):
                    await CLIENTS.cart.delete(f"/carts/{request.cart_id}")
                saga.record("clear_cart")
            except Exception as exc:
                span.set_attribute("voltmart.checkout.cart_clear_failed", True)
                log.warning(
                    "cart clear failed after successful order order_id=%s: %s",
                    order_id,
                    exc,
                )

            duration = time.perf_counter() - started
            span.set_attribute("voltmart.checkout.outcome", "success")
            record_checkout("success")
            record_checkout_duration(duration, "success")

            log.info(
                "checkout complete order_id=%s payment_id=%s total=%.2f duration_ms=%d",
                order_id,
                payment_id,
                order_total,
                int(duration * 1000),
            )

            return CheckoutResult(
                order_id=order_id,
                payment_id=payment_id,
                status=confirmed["status"],
                total=order_total,
                currency=order["totals"].get("currency", "USD"),
                estimated_delivery=confirmed.get("estimated_delivery", ""),
                email=str(request.email),
                trace_id=current_trace_id(),
                steps_completed=saga.completed,
                duration_ms=int(duration * 1000),
            )

        except OutOfStock as exc:
            # Nothing was reserved, so there is nothing to unwind.
            await saga.unwind("reserve_stock")
            _record_failure(span, started, "out_of_stock", "reserve_stock")
            raise

        except PaymentDeclined as exc:
            # Release the stock and mark the order failed. The customer keeps
            # their cart so they can retry with another card.
            await saga.unwind("authorize_payment")
            _record_failure(span, started, "payment_declined", "authorize_payment")
            log.info(
                "checkout declined order_id=%s reason=%s",
                order_id or "-",
                exc.details.get("decline_reason", "unknown"),
            )
            raise

        except VoltMartError as exc:
            failed_step = saga.completed[-1] if saga.completed else "load_cart"
            await saga.unwind(failed_step)
            _record_failure(span, started, exc.code, failed_step)
            raise

        except Exception as exc:
            failed_step = saga.completed[-1] if saga.completed else "load_cart"
            await saga.unwind(failed_step)
            _record_failure(span, started, "internal_error", failed_step)
            span.record_exception(exc)
            raise


def _record_failure(span: trace.Span, started: float, outcome: str, step: str) -> None:
    duration = time.perf_counter() - started
    span.set_attribute("voltmart.checkout.outcome", outcome)
    span.set_attribute("voltmart.checkout.failed_step", step)
    record_checkout(outcome, step)
    record_checkout_duration(duration, outcome)


@app.get("/checkout/preview/{cart_id}", tags=["checkout"])
async def preview(cart_id: str) -> dict[str, Any]:
    """What this cart would cost, and whether it can actually be bought.

    Called by the checkout page before the shopper commits, and by the support
    agent when someone asks why their checkout keeps failing. Reserves nothing.
    """
    if not is_safe_id(cart_id):
        raise ValidationFailed("cart_id is not a valid identifier")

    cart = await CLIENTS.cart.get(f"/carts/{cart_id}")
    lines = cart.get("lines", [])

    blockers: list[dict[str, Any]] = []
    for line in lines:
        if line.get("exceeds_available"):
            blockers.append(
                {
                    "product_id": line["product_id"],
                    "name": line["name"],
                    "requested": line["quantity"],
                    "available": line.get("available"),
                    "issue": "insufficient_stock",
                }
            )

    return {
        "cart_id": cart_id,
        "line_count": len(lines),
        "totals": cart.get("totals", {}),
        "can_checkout": bool(lines) and not blockers,
        "blockers": blockers,
        "reason": (
            "Cart is empty"
            if not lines
            else "Some items are no longer available in the requested quantity"
            if blockers
            else ""
        ),
    }


if __name__ == "__main__":
    run("main:app")
