"""Business metrics emitted over OTLP alongside the automatic HTTP metrics.

Auto-instrumentation already reports request rate, latency and error rate. The
instruments here describe things a storefront operator actually asks about:
how many orders were placed, what they were worth, why payments failed, and
which tools the support agent reached for.

Instrument creation is lazy so that importing this module before
``configure_telemetry`` runs does not bind a no-op meter permanently.
"""

from __future__ import annotations

from typing import Any

from .telemetry import meter

_CACHE: dict[str, Any] = {}


def _counter(name: str, unit: str, description: str):
    key = f"counter:{name}"
    if key not in _CACHE:
        _CACHE[key] = meter("voltmart.business").create_counter(
            name, unit=unit, description=description
        )
    return _CACHE[key]


def _histogram(name: str, unit: str, description: str):
    key = f"histogram:{name}"
    if key not in _CACHE:
        _CACHE[key] = meter("voltmart.business").create_histogram(
            name, unit=unit, description=description
        )
    return _CACHE[key]


# --------------------------------------------------------------------------
# Cart
# --------------------------------------------------------------------------
def record_cart_item_added(product_id: str, category: str, quantity: int) -> None:
    _counter(
        "voltmart.cart.items_added", "{item}", "Items added to a shopping cart"
    ).add(quantity, {"product_id": product_id, "category": category})


def record_cart_item_removed(product_id: str, quantity: int) -> None:
    _counter(
        "voltmart.cart.items_removed", "{item}", "Items removed from a shopping cart"
    ).add(quantity, {"product_id": product_id})


def record_cart_abandoned_value(value_usd: float) -> None:
    _histogram(
        "voltmart.cart.cleared_value", "USD", "Value of carts cleared without checkout"
    ).record(value_usd)


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------
def record_inventory_reservation(product_id: str, quantity: int, outcome: str) -> None:
    _counter(
        "voltmart.inventory.reservations",
        "{reservation}",
        "Inventory reservation attempts by outcome",
    ).add(1, {"product_id": product_id, "outcome": outcome})
    if outcome == "reserved":
        _histogram(
            "voltmart.inventory.reserved_units", "{item}", "Units reserved per request"
        ).record(quantity, {"product_id": product_id})


def record_stock_level(product_id: str, available: int) -> None:
    _histogram(
        "voltmart.inventory.available_units",
        "{item}",
        "Observed available units at read time",
    ).record(available, {"product_id": product_id})


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------
def record_order_created(order_total_usd: float, item_count: int, currency: str) -> None:
    _counter("voltmart.orders.created", "{order}", "Orders created").add(
        1, {"currency": currency}
    )
    _histogram("voltmart.orders.value", "USD", "Order grand total").record(
        order_total_usd, {"currency": currency}
    )
    _histogram("voltmart.orders.item_count", "{item}", "Items per order").record(
        item_count
    )


def record_order_status_change(from_status: str, to_status: str) -> None:
    _counter(
        "voltmart.orders.status_transitions",
        "{transition}",
        "Order status transitions",
    ).add(1, {"from": from_status, "to": to_status})


# --------------------------------------------------------------------------
# Payments
# --------------------------------------------------------------------------
def record_payment(outcome: str, amount_usd: float, method: str, decline_reason: str = "") -> None:
    attributes = {"outcome": outcome, "method": method}
    if decline_reason:
        attributes["decline_reason"] = decline_reason
    _counter(
        "voltmart.payments.attempts", "{payment}", "Payment attempts by outcome"
    ).add(1, attributes)
    if outcome == "authorized":
        _histogram("voltmart.payments.authorized_amount", "USD", "Authorized amount").record(
            amount_usd, {"method": method}
        )


def record_refund(amount_usd: float, reason: str) -> None:
    _counter("voltmart.payments.refunds", "{refund}", "Refunds issued").add(
        1, {"reason": reason}
    )
    _histogram("voltmart.payments.refunded_amount", "USD", "Refunded amount").record(
        amount_usd
    )


# --------------------------------------------------------------------------
# Checkout saga
# --------------------------------------------------------------------------
def record_checkout(outcome: str, failed_step: str = "") -> None:
    attributes = {"outcome": outcome}
    if failed_step:
        attributes["failed_step"] = failed_step
    _counter(
        "voltmart.checkout.attempts", "{checkout}", "Checkout attempts by outcome"
    ).add(1, attributes)


def record_checkout_duration(seconds: float, outcome: str) -> None:
    _histogram(
        "voltmart.checkout.duration", "s", "End-to-end checkout saga duration"
    ).record(seconds, {"outcome": outcome})


def record_compensation(step: str, succeeded: bool) -> None:
    """A compensation that itself fails leaves reserved stock behind.

    Operators alert on this: it is the signal that the saga left the system in
    an inconsistent state and needs manual repair.
    """
    _counter(
        "voltmart.checkout.compensations",
        "{compensation}",
        "Saga compensation actions run after a failed checkout",
    ).add(1, {"step": step, "succeeded": str(succeeded).lower()})


# --------------------------------------------------------------------------
# Support agent
# --------------------------------------------------------------------------
def record_agent_conversation(outcome: str) -> None:
    _counter(
        "voltmart.agent.conversations", "{conversation}", "Support agent turns by outcome"
    ).add(1, {"outcome": outcome})


def record_agent_tool_call(tool_name: str, outcome: str, duration_seconds: float) -> None:
    _counter(
        "voltmart.agent.tool_calls", "{call}", "Support agent tool invocations"
    ).add(1, {"tool": tool_name, "outcome": outcome})
    _histogram(
        "voltmart.agent.tool_duration", "s", "Support agent tool invocation duration"
    ).record(duration_seconds, {"tool": tool_name})


def record_agent_tokens(input_tokens: int, output_tokens: int, model_id: str) -> None:
    counter = _counter(
        "voltmart.agent.tokens", "{token}", "Model tokens consumed by the support agent"
    )
    counter.add(input_tokens, {"direction": "input", "model_id": model_id})
    counter.add(output_tokens, {"direction": "output", "model_id": model_id})


def record_agent_escalation(reason: str) -> None:
    _counter(
        "voltmart.agent.escalations", "{escalation}", "Hand-offs to a human agent"
    ).add(1, {"reason": reason})
