"""VoltMart payment service — a simulator, not a payment processor.

No money moves and no real card network is contacted. Outcomes are decided by
the test card number, which makes workshop failures reproducible: the same card
always declines the same way, so an attendee can trigger a decline, find it in
the trace, and then fix it.

What it does take seriously is card data handling, because the wrong habits are
the ones that get copied out of sample apps:

* The card number is never logged, never put on a span, and never stored. Only
  the last four digits and the brand survive the request.
* The CVC is used for the simulated check and then dropped. It is not persisted
  in any form, which is the actual PCI DSS rule.
* The stored record holds no data that could be used to charge the card again.

A real integration would not accept raw card data here at all — it would take a
single-use token from the processor's client-side SDK, so the number never
touches your servers. That is noted in the docs rather than implemented,
because tokenisation needs a processor account the workshop does not have.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from voltmart_common.config import storage_config
from voltmart_common.errors import Conflict, NotFound, PaymentDeclined, ValidationFailed
from voltmart_common.ids import is_safe_id, new_payment_id
from voltmart_common.metrics import record_payment, record_refund
from voltmart_common.service import create_app, run
from voltmart_common.storage import Repository
from voltmart_common.telemetry import tracer

SERVICE_NAME = "voltmart-payment"

# Deliberate latency so the payment span is visibly the slow one in a trace,
# the way a real external processor call would be.
MIN_LATENCY_MS = int(os.getenv("PAYMENT_MIN_LATENCY_MS", "120"))
MAX_LATENCY_MS = int(os.getenv("PAYMENT_MAX_LATENCY_MS", "450"))

# Random failure injection, off by default. Workshops turn this up to generate
# an error rate worth alerting on.
CHAOS_FAILURE_RATE = float(os.getenv("PAYMENT_CHAOS_FAILURE_RATE", "0"))

# Above this amount the simulator asks for manual review, which gives the
# support agent a realistic "why is my order stuck" case to explain.
REVIEW_THRESHOLD = float(os.getenv("PAYMENT_REVIEW_THRESHOLD", "8000"))

# Test cards. Keyed by full number; the value is the decline reason, or None for
# a successful authorization.
TEST_CARDS: dict[str, str | None] = {
    "4242424242424242": None,
    "5555555555554444": None,
    "378282246310005": None,
    "4000000000000002": "insufficient_funds",
    "4000000000000069": "expired_card",
    "4000000000000127": "incorrect_cvc",
    "4000000000000119": "processing_error",
    "4000000000009995": "insufficient_funds",
    "4100000000000019": "suspected_fraud",
}

PaymentStatus = Literal["authorized", "declined", "refunded", "under_review", "failed"]


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
class Card(BaseModel):
    """Card details, accepted and then immediately forgotten.

    ``model_config`` sets ``extra="forbid"`` so a caller cannot smuggle extra
    fields through into anything downstream.
    """

    model_config = {"extra": "forbid"}

    number: str = Field(..., min_length=12, max_length=19, repr=False)
    exp_month: int = Field(..., ge=1, le=12)
    exp_year: int = Field(..., ge=2024, le=2099)
    cvc: str = Field(..., min_length=3, max_length=4, repr=False)
    holder_name: str = Field(..., min_length=1, max_length=120)

    @field_validator("number")
    @classmethod
    def _digits_only(cls, value: str) -> str:
        cleaned = value.replace(" ", "").replace("-", "")
        if not cleaned.isdigit():
            raise ValueError("card number must contain digits only")
        return cleaned

    @field_validator("cvc")
    @classmethod
    def _cvc_digits(cls, value: str) -> str:
        if not value.isdigit():
            raise ValueError("cvc must contain digits only")
        return value

    def __str__(self) -> str:  # pragma: no cover - defensive
        return f"<Card ****{self.number[-4:]}>"

    __repr__ = __str__


class PaymentRequest(BaseModel):
    order_id: str = Field(..., min_length=1, max_length=64)
    amount: float = Field(..., gt=0, le=1_000_000)
    currency: str = Field("USD", min_length=3, max_length=3)
    method: Literal["card", "wallet", "gift_card"] = "card"
    card: Card | None = None
    idempotency_key: str = Field("", max_length=80)


class Payment(BaseModel):
    payment_id: str
    order_id: str
    status: PaymentStatus
    amount: float
    currency: str
    method: str
    card_brand: str = ""
    card_last4: str = ""
    decline_reason: str = ""
    authorized_at: float = 0.0
    refunded_amount: float = 0.0
    refunded_at: float = 0.0
    refund_reason: str = ""
    created_at: float
    processor_reference: str = ""


class RefundRequest(BaseModel):
    amount: float | None = Field(None, gt=0, description="Omit to refund in full")
    reason: str = Field("customer_request", max_length=200)


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------
STORAGE = storage_config()
REPO = Repository(STORAGE, key_name="payment_id")

app, log = create_app(
    SERVICE_NAME,
    title="VoltMart Payment",
    description="Simulated card authorization and refunds.",
    readiness_check=REPO.health,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _card_brand(number: str) -> str:
    if number.startswith("4"):
        return "Visa"
    if number[:2] in {"51", "52", "53", "54", "55"}:
        return "Mastercard"
    if number[:2] in {"34", "37"}:
        return "American Express"
    if number.startswith("6011") or number.startswith("65"):
        return "Discover"
    return "Unknown"


def _luhn_valid(number: str) -> bool:
    """Standard Luhn checksum, the same first-pass check a real gateway runs."""
    total = 0
    for index, digit in enumerate(reversed(number)):
        value = int(digit)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _is_expired(exp_month: int, exp_year: int) -> bool:
    now = time.gmtime()
    return (exp_year, exp_month) < (now.tm_year, now.tm_mon)


def _to_payment(item: dict[str, Any]) -> Payment:
    return Payment(**item)


def _decide(request: PaymentRequest, card: Card | None) -> tuple[str, str]:
    """Return ``(status, decline_reason)`` for this attempt.

    Order of checks mirrors a real gateway: structural validation first, then
    issuer-style decisions, then risk rules.
    """
    if CHAOS_FAILURE_RATE > 0 and random.random() < CHAOS_FAILURE_RATE:
        return "failed", "processor_timeout"

    if request.method in {"wallet", "gift_card"}:
        # Non-card methods always succeed in the simulator; they exist so the
        # agent has more than one payment shape to reason about.
        return "authorized", ""

    if card is None:
        raise ValidationFailed("Card details are required when method is 'card'.")

    if not _luhn_valid(card.number):
        return "declined", "invalid_card_number"

    if _is_expired(card.exp_month, card.exp_year):
        return "declined", "expired_card"

    scripted = TEST_CARDS.get(card.number, "unknown_card")
    if scripted == "unknown_card":
        # Not one of the documented test cards. Rejected on purpose so nobody
        # gets the impression a real card would work here.
        return "declined", "card_not_recognised"
    if scripted is not None:
        return "declined", scripted

    if request.amount > REVIEW_THRESHOLD:
        return "under_review", "amount_exceeds_review_threshold"

    return "authorized", ""


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.post("/payments", response_model=Payment, status_code=201, tags=["payment"])
async def create_payment(request: PaymentRequest) -> Payment:
    """Authorize a payment for an order.

    A declined card is a 402 with a machine-readable ``decline_reason``, not a
    500. The checkout saga needs to tell "the customer's card was refused" from
    "our payment service is broken", because the first is a message to the
    shopper and the second is a page for an engineer.
    """
    if not is_safe_id(request.order_id):
        raise ValidationFailed("order_id is not a valid identifier")

    with tracer(SERVICE_NAME).start_as_current_span("payment.authorize") as span:
        span.set_attribute("voltmart.payment.order_id", request.order_id)
        span.set_attribute("voltmart.payment.amount", request.amount)
        span.set_attribute("voltmart.payment.method", request.method)
        # Note what is absent: no card number, no CVC, no holder name. Spans
        # are exported to a telemetry backend, which is not a vault.

        # Simulated processor round trip.
        latency = random.randint(MIN_LATENCY_MS, max(MIN_LATENCY_MS, MAX_LATENCY_MS))
        await asyncio.sleep(latency / 1000.0)
        span.set_attribute("voltmart.payment.processor_latency_ms", latency)

        status, reason = _decide(request, request.card)
        span.set_attribute("voltmart.payment.status", status)
        if reason:
            span.set_attribute("voltmart.payment.decline_reason", reason)

        payment_id = new_payment_id()
        now = time.time()
        item: dict[str, Any] = {
            "payment_id": payment_id,
            "order_id": request.order_id,
            "status": status,
            "amount": request.amount,
            "currency": request.currency.upper(),
            "method": request.method,
            "card_brand": _card_brand(request.card.number) if request.card else "",
            "card_last4": request.card.number[-4:] if request.card else "",
            "decline_reason": reason,
            "authorized_at": now if status == "authorized" else 0.0,
            "refunded_amount": 0.0,
            "refunded_at": 0.0,
            "refund_reason": "",
            "created_at": now,
            "processor_reference": f"sim_{payment_id[-12:]}",
        }
        REPO.put(item)

    record_payment(status, request.amount, request.method, reason)

    # Logged with the last four only, which is what a support conversation
    # needs and all a log file should ever hold.
    log.info(
        "payment %s payment_id=%s order_id=%s amount=%.2f last4=%s reason=%s",
        status,
        payment_id,
        request.order_id,
        request.amount,
        item["card_last4"] or "-",
        reason or "-",
    )

    if status == "declined":
        raise PaymentDeclined(
            _decline_message(reason),
            payment_id=payment_id,
            order_id=request.order_id,
            decline_reason=reason,
        )
    if status == "failed":
        # A processor fault is our problem, not the customer's: 503 so the
        # caller can retry rather than telling the shopper their card failed.
        from voltmart_common.errors import DownstreamUnavailable

        raise DownstreamUnavailable(
            "The payment processor did not respond. No charge was made; please retry.",
            payment_id=payment_id,
            order_id=request.order_id,
        )

    return _to_payment(item)


def _decline_message(reason: str) -> str:
    """Customer-facing text for a decline.

    Vague on fraud by design: telling someone exactly which rule flagged them
    is a gift to whoever is testing stolen cards.
    """
    return {
        "insufficient_funds": "The card was declined for insufficient funds.",
        "expired_card": "That card has expired. Please use a different card.",
        "incorrect_cvc": "The security code did not match. Please check and retry.",
        "invalid_card_number": "That card number is not valid.",
        "card_not_recognised": "That card was not accepted. See the documented test cards.",
        "suspected_fraud": "The card was declined. Please contact your card issuer.",
        "processing_error": "The card could not be processed. Please try again.",
    }.get(reason, "The card was declined.")


@app.get("/payments/{payment_id}", response_model=Payment, tags=["payment"])
async def get_payment(payment_id: str) -> Payment:
    if not is_safe_id(payment_id):
        raise ValidationFailed("payment_id is not a valid identifier")
    item = REPO.get(payment_id)
    if item is None:
        raise NotFound(f"No payment with id {payment_id}", payment_id=payment_id)
    return _to_payment(item)


@app.get("/payments", response_model=list[Payment], tags=["payment"])
async def list_payments_for_order(order_id: str) -> list[Payment]:
    """All attempts against one order, including the declined ones.

    Useful in support: "it says my card failed three times" is answerable.
    """
    if not is_safe_id(order_id):
        raise ValidationFailed("order_id is not a valid identifier")
    items = REPO.query_index("order-index", "order_id", order_id, limit=50)
    items.sort(key=lambda item: float(item.get("created_at", 0.0)), reverse=True)
    return [_to_payment(item) for item in items]


@app.post("/payments/{payment_id}/refund", response_model=Payment, tags=["payment"])
async def refund_payment(payment_id: str, request: RefundRequest) -> Payment:
    """Refund an authorized payment, in full or in part."""
    if not is_safe_id(payment_id):
        raise ValidationFailed("payment_id is not a valid identifier")

    item = REPO.get(payment_id)
    if item is None:
        raise NotFound(f"No payment with id {payment_id}", payment_id=payment_id)

    if item["status"] not in {"authorized", "refunded"}:
        raise Conflict(
            f"A payment that is {item['status']} cannot be refunded.",
            payment_id=payment_id,
            status=item["status"],
        )

    already_refunded = float(item.get("refunded_amount", 0.0))
    remaining = round(float(item["amount"]) - already_refunded, 2)
    if remaining <= 0:
        raise Conflict(
            "This payment has already been refunded in full.",
            payment_id=payment_id,
            amount=item["amount"],
        )

    amount = round(request.amount if request.amount is not None else remaining, 2)
    if amount > remaining:
        raise ValidationFailed(
            f"Refund of {amount:.2f} exceeds the {remaining:.2f} still refundable.",
            payment_id=payment_id,
            refundable=remaining,
        )

    with tracer(SERVICE_NAME).start_as_current_span("payment.refund") as span:
        span.set_attribute("voltmart.payment.id", payment_id)
        span.set_attribute("voltmart.refund.amount", amount)
        await asyncio.sleep(random.randint(60, 180) / 1000.0)

        total_refunded = round(already_refunded + amount, 2)
        # Conditional on the amount we read, so two concurrent refunds cannot
        # both pass the "exceeds refundable" check and both be written.
        updated = REPO.update_fields(
            payment_id,
            {
                "status": "refunded",
                "refunded_amount": total_refunded,
                "refunded_at": time.time(),
                "refund_reason": request.reason,
            },
            expected={"refunded_amount": already_refunded},
        )

    record_refund(amount, request.reason)
    log.info(
        "refund issued payment_id=%s amount=%.2f total_refunded=%.2f reason=%s",
        payment_id,
        amount,
        total_refunded,
        request.reason,
    )
    return _to_payment(updated)


@app.get("/test-cards", tags=["payment"])
async def test_cards() -> dict[str, Any]:
    """The card numbers this simulator recognises, and what each one does.

    Published on purpose: these are fake numbers, and a workshop attendee needs
    to know which one produces which failure.
    """
    return {
        "note": "Simulated numbers only. No real card is ever accepted or charged.",
        "cards": [
            {"number": "4242 4242 4242 4242", "brand": "Visa", "result": "authorized"},
            {"number": "5555 5555 5555 4444", "brand": "Mastercard", "result": "authorized"},
            {"number": "3782 822463 10005", "brand": "Amex", "result": "authorized"},
            {"number": "4000 0000 0000 0002", "result": "declined: insufficient_funds"},
            {"number": "4000 0000 0000 0069", "result": "declined: expired_card"},
            {"number": "4000 0000 0000 0127", "result": "declined: incorrect_cvc"},
            {"number": "4000 0000 0000 0119", "result": "declined: processing_error"},
            {"number": "4100 0000 0000 0019", "result": "declined: suspected_fraud"},
        ],
        "rules": [
            f"Any authorized amount above ${REVIEW_THRESHOLD:,.0f} goes to under_review.",
            "Any card number not listed here is declined as card_not_recognised.",
            "A past expiry date is declined as expired_card regardless of the number.",
            f"Random processor failures are injected at a rate of {CHAOS_FAILURE_RATE}.",
        ],
    }


if __name__ == "__main__":
    run("main:app")
