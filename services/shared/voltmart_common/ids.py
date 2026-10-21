"""Identifier helpers.

Ids are opaque, URL-safe and unguessable. Cart ids in particular are bearer
tokens in this design: anyone holding a cart id can read that cart, so they are
generated from ``secrets`` rather than from a counter or a timestamp.
"""

from __future__ import annotations

import re
import secrets
import time
import uuid

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def new_cart_id() -> str:
    return f"cart_{secrets.token_urlsafe(16)}"


def new_order_id() -> str:
    """Order ids are readable for support conversations but not sequential.

    Format: ``VM-<yymmdd>-<8 random chars>``. The date prefix helps a human
    scanning a list; the random suffix prevents enumeration of other customers'
    orders.
    """
    stamp = time.strftime("%y%m%d", time.gmtime())
    return f"VM-{stamp}-{secrets.token_hex(4).upper()}"


def new_payment_id() -> str:
    return f"pay_{secrets.token_urlsafe(16)}"


def new_reservation_id() -> str:
    return f"rsv_{uuid.uuid4().hex}"


def new_conversation_id() -> str:
    return f"conv_{secrets.token_urlsafe(12)}"


def new_customer_id() -> str:
    return f"cust_{secrets.token_urlsafe(10)}"


def is_safe_id(value: str) -> bool:
    """Reject ids that could escape a URL path or a DynamoDB key expression."""
    return bool(value and _SAFE_ID.match(value))
