"""VoltMart cart service.

Holds what a shopper has picked but not yet bought.

Pricing policy: the cart stores product ids and quantities, and re-prices from
the catalog on every read. It deliberately does *not* trust a stored price. If
a price changes while an item sits in a cart, the shopper sees the current
price, and the price that gets charged is the one the order service captures at
checkout. Storing the price in the cart and billing from it is how sample apps
end up selling a $2,399 laptop for whatever it cost last month.

Availability is annotated from the inventory service on read, but never
enforced here. A cart is allowed to contain more units than exist; the attempt
is refused at checkout, where a reservation either succeeds or does not.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

from voltmart_common.config import downstream_config, storage_config
from voltmart_common.errors import NotFound, ValidationFailed
from voltmart_common.http import ServiceClients
from voltmart_common.ids import is_safe_id, new_cart_id
from voltmart_common.metrics import (
    record_cart_abandoned_value,
    record_cart_item_added,
    record_cart_item_removed,
)
from voltmart_common.service import create_app, run
from voltmart_common.storage import Repository
from voltmart_common.telemetry import tracer

SERVICE_NAME = "voltmart-cart"

TAX_RATE = 0.0875
FREE_SHIPPING_THRESHOLD = 99.0
STANDARD_SHIPPING = 9.95
MAX_LINES_PER_CART = 40
MAX_QUANTITY_PER_LINE = 20
CART_TTL_SECONDS = 60 * 60 * 24 * 30  # carts expire after 30 days


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
class AddItemRequest(BaseModel):
    product_id: str
    quantity: int = Field(1, ge=1, le=MAX_QUANTITY_PER_LINE)


class UpdateItemRequest(BaseModel):
    quantity: int = Field(..., ge=0, le=MAX_QUANTITY_PER_LINE)


class CreateCartRequest(BaseModel):
    customer_id: str = Field("", max_length=64)


class CartLine(BaseModel):
    product_id: str
    name: str
    brand: str
    category: str
    quantity: int
    unit_price: float
    line_total: float
    currency: str
    image_accent: str
    available: int | None = None
    stock_status: str | None = None
    # True when the cart holds more units than inventory can currently supply.
    # The UI shows a warning; checkout is what actually refuses.
    exceeds_available: bool = False


class CartTotals(BaseModel):
    subtotal: float
    tax: float
    shipping: float
    total: float
    currency: str = "USD"
    item_count: int
    free_shipping_applied: bool
    amount_to_free_shipping: float


class Cart(BaseModel):
    cart_id: str
    customer_id: str
    lines: list[CartLine]
    totals: CartTotals
    created_at: float
    updated_at: float
    has_stock_warnings: bool


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------
STORAGE = storage_config()
REPO = Repository(STORAGE, key_name="cart_id")
CLIENTS = ServiceClients(downstream_config())


async def _shutdown() -> None:
    await CLIENTS.aclose()


app, log = create_app(
    SERVICE_NAME,
    title="VoltMart Cart",
    description="Shopping carts, line items and running totals.",
    readiness_check=REPO.health,
    on_shutdown=_shutdown,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _round(value: float) -> float:
    return round(value + 1e-9, 2)


def _compute_totals(lines: list[CartLine]) -> CartTotals:
    subtotal = _round(sum(line.line_total for line in lines))
    item_count = sum(line.quantity for line in lines)

    free_shipping = subtotal >= FREE_SHIPPING_THRESHOLD or item_count == 0
    shipping = 0.0 if free_shipping else STANDARD_SHIPPING
    tax = _round(subtotal * TAX_RATE)
    total = _round(subtotal + tax + shipping)

    return CartTotals(
        subtotal=subtotal,
        tax=tax,
        shipping=shipping,
        total=total,
        item_count=item_count,
        free_shipping_applied=free_shipping and item_count > 0,
        amount_to_free_shipping=_round(max(0.0, FREE_SHIPPING_THRESHOLD - subtotal)),
    )


async def _enrich(stored_lines: list[dict[str, Any]]) -> list[CartLine]:
    """Turn stored (product_id, quantity) pairs into priced, annotated lines.

    One batch call to the catalog and one inventory call per product. The
    inventory lookups are tolerated failures: if inventory is down the cart
    still renders with prices, just without stock badges. A shopper should be
    able to see their cart during a partial outage.
    """
    if not stored_lines:
        return []

    product_ids = [line["product_id"] for line in stored_lines]
    quantities = {line["product_id"]: int(line["quantity"]) for line in stored_lines}

    with tracer(SERVICE_NAME).start_as_current_span("cart.enrich") as span:
        span.set_attribute("voltmart.cart.line_count", len(product_ids))

        products = await CLIENTS.catalog.post(
            "/products/batch", json={"product_ids": product_ids}
        )
        by_id = {product["product_id"]: product for product in (products or [])}

        lines: list[CartLine] = []
        for product_id in product_ids:
            product = by_id.get(product_id)
            if product is None:
                # Delisted since it was added. Drop it from the view rather
                # than failing the whole cart read.
                log.info("dropping delisted product from cart view id=%s", product_id)
                continue

            quantity = quantities[product_id]
            unit_price = float(product["price"])

            available: int | None = None
            stock_status: str | None = None
            try:
                stock = await CLIENTS.inventory.get(f"/inventory/{product_id}")
                available = int(stock["available"])
                stock_status = str(stock["status"])
            except Exception as exc:  # inventory is optional for a cart read
                span.set_attribute("voltmart.cart.inventory_degraded", True)
                log.warning("inventory lookup failed product_id=%s: %s", product_id, exc)

            lines.append(
                CartLine(
                    product_id=product_id,
                    name=product["name"],
                    brand=product["brand"],
                    category=product["category"],
                    quantity=quantity,
                    unit_price=unit_price,
                    line_total=_round(unit_price * quantity),
                    currency=product.get("currency", "USD"),
                    image_accent=product.get("image_accent", "#6366f1"),
                    available=available,
                    stock_status=stock_status,
                    exceeds_available=available is not None and quantity > available,
                )
            )

    return lines


async def _render(item: dict[str, Any]) -> Cart:
    lines = await _enrich(item.get("lines", []))
    return Cart(
        cart_id=item["cart_id"],
        customer_id=item.get("customer_id", ""),
        lines=lines,
        totals=_compute_totals(lines),
        created_at=float(item.get("created_at", 0.0)),
        updated_at=float(item.get("updated_at", 0.0)),
        has_stock_warnings=any(line.exceeds_available for line in lines),
    )


def _load(cart_id: str) -> dict[str, Any]:
    if not is_safe_id(cart_id):
        raise ValidationFailed("cart_id is not a valid identifier")
    item = REPO.get(cart_id)
    if item is None:
        raise NotFound(f"No cart with id {cart_id}", cart_id=cart_id)
    return item


def _persist(item: dict[str, Any]) -> dict[str, Any]:
    item["updated_at"] = time.time()
    item["expires_at"] = int(time.time() + CART_TTL_SECONDS)
    REPO.put(item)
    return item


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.post("/carts", response_model=Cart, status_code=201, tags=["cart"])
async def create_cart(request: CreateCartRequest) -> Cart:
    now = time.time()
    item = {
        "cart_id": new_cart_id(),
        "customer_id": request.customer_id,
        "lines": [],
        "created_at": now,
        "updated_at": now,
        "expires_at": int(now + CART_TTL_SECONDS),
    }
    REPO.put(item)
    log.info("cart created cart_id=%s", item["cart_id"])
    return await _render(item)


@app.get("/carts/{cart_id}", response_model=Cart, tags=["cart"])
async def get_cart(cart_id: str) -> Cart:
    return await _render(_load(cart_id))


@app.get("/carts/by-customer/{customer_id}", response_model=Cart, tags=["cart"])
async def get_cart_by_customer(customer_id: str) -> Cart:
    """Most recently updated cart for a customer.

    The support agent uses this: it knows who it is talking to, but not which
    cart id that person's browser is holding.
    """
    if not is_safe_id(customer_id):
        raise ValidationFailed("customer_id is not a valid identifier")

    matches = REPO.query_index("customer-index", "customer_id", customer_id, limit=25)
    if not matches:
        raise NotFound(
            f"No cart found for customer {customer_id}", customer_id=customer_id
        )
    newest = max(matches, key=lambda item: float(item.get("updated_at", 0.0)))
    return await _render(newest)


@app.post("/carts/{cart_id}/items", response_model=Cart, tags=["cart"])
async def add_item(cart_id: str, request: AddItemRequest) -> Cart:
    """Add units of a product, merging into an existing line if present."""
    item = _load(cart_id)

    if not is_safe_id(request.product_id):
        raise ValidationFailed("product_id is not a valid identifier")

    # Verify the product exists before storing it. A 404 from the catalog
    # propagates as a 404 here, which is the honest answer.
    product = await CLIENTS.catalog.get(f"/products/{request.product_id}")

    lines: list[dict[str, Any]] = item.get("lines", [])
    existing = next(
        (line for line in lines if line["product_id"] == request.product_id), None
    )

    if existing is not None:
        merged = int(existing["quantity"]) + request.quantity
        if merged > MAX_QUANTITY_PER_LINE:
            raise ValidationFailed(
                f"A cart line is limited to {MAX_QUANTITY_PER_LINE} units; "
                f"this would make {merged}.",
                product_id=request.product_id,
                limit=MAX_QUANTITY_PER_LINE,
            )
        existing["quantity"] = merged
    else:
        if len(lines) >= MAX_LINES_PER_CART:
            raise ValidationFailed(
                f"A cart is limited to {MAX_LINES_PER_CART} distinct products.",
                limit=MAX_LINES_PER_CART,
            )
        lines.append({"product_id": request.product_id, "quantity": request.quantity})

    item["lines"] = lines
    _persist(item)

    record_cart_item_added(request.product_id, product["category"], request.quantity)
    log.info(
        "item added cart_id=%s product_id=%s quantity=%d",
        cart_id,
        request.product_id,
        request.quantity,
    )
    return await _render(item)


@app.put("/carts/{cart_id}/items/{product_id}", response_model=Cart, tags=["cart"])
async def update_item(cart_id: str, product_id: str, request: UpdateItemRequest) -> Cart:
    """Set an exact quantity. Zero removes the line."""
    item = _load(cart_id)
    lines: list[dict[str, Any]] = item.get("lines", [])

    existing = next((line for line in lines if line["product_id"] == product_id), None)
    if existing is None:
        raise NotFound(
            f"Product {product_id} is not in cart {cart_id}",
            cart_id=cart_id,
            product_id=product_id,
        )

    if request.quantity == 0:
        removed = int(existing["quantity"])
        item["lines"] = [line for line in lines if line["product_id"] != product_id]
        record_cart_item_removed(product_id, removed)
    else:
        existing["quantity"] = request.quantity
        item["lines"] = lines

    _persist(item)
    return await _render(item)


@app.delete("/carts/{cart_id}/items/{product_id}", response_model=Cart, tags=["cart"])
async def remove_item(cart_id: str, product_id: str) -> Cart:
    item = _load(cart_id)
    lines: list[dict[str, Any]] = item.get("lines", [])

    existing = next((line for line in lines if line["product_id"] == product_id), None)
    if existing is None:
        raise NotFound(
            f"Product {product_id} is not in cart {cart_id}",
            cart_id=cart_id,
            product_id=product_id,
        )

    record_cart_item_removed(product_id, int(existing["quantity"]))
    item["lines"] = [line for line in lines if line["product_id"] != product_id]
    _persist(item)
    log.info("item removed cart_id=%s product_id=%s", cart_id, product_id)
    return await _render(item)


@app.delete("/carts/{cart_id}", response_model=Cart, tags=["cart"])
async def clear_cart(cart_id: str) -> Cart:
    """Empty a cart without deleting it.

    Called by the checkout service after a successful order, and by the shopper
    from the cart page. The record survives so the same cart id keeps working.
    """
    item = _load(cart_id)
    rendered = await _render(item)
    if rendered.totals.subtotal > 0:
        record_cart_abandoned_value(rendered.totals.subtotal)

    item["lines"] = []
    _persist(item)
    log.info("cart cleared cart_id=%s", cart_id)
    return await _render(item)


if __name__ == "__main__":
    run("main:app")
