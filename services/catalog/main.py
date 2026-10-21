"""VoltMart catalog service.

Owns product information: what VoltMart sells, what it costs and what it does.
Read-only, and the data is a JSON file baked into the image, because a product
catalog in a workshop sample should not need a database to be interesting.

Stock levels deliberately live in the inventory service instead. Price and
description change on a release cadence; stock changes on every order, and
mixing the two in one service is the usual way these designs go wrong.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from fastapi import Query
from pydantic import BaseModel, Field

from voltmart_common.errors import NotFound, ValidationFailed
from voltmart_common.ids import is_safe_id
from voltmart_common.service import create_app, run
from voltmart_common.telemetry import tracer

SERVICE_NAME = "voltmart-catalog"
PRODUCTS_FILE = Path(__file__).parent / "products.json"


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
class Product(BaseModel):
    product_id: str
    name: str
    brand: str
    category: str
    price: float
    currency: str = "USD"
    rating: float
    review_count: int
    short_description: str
    description: str
    specs: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    image_accent: str = "#6366f1"
    featured: bool = False


class ProductSummary(BaseModel):
    """The subset of fields a listing page or a cart line needs."""

    product_id: str
    name: str
    brand: str
    category: str
    price: float
    currency: str
    rating: float
    review_count: int
    short_description: str
    tags: list[str]
    image_accent: str
    featured: bool


class ProductPage(BaseModel):
    items: list[ProductSummary]
    total: int
    limit: int
    offset: int


class CategoryInfo(BaseModel):
    category: str
    label: str
    product_count: int
    min_price: float
    max_price: float


class BatchRequest(BaseModel):
    product_ids: list[str] = Field(..., min_length=1, max_length=100)


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------
CATEGORY_LABELS = {
    "laptops": "Laptops",
    "smartphones": "Smartphones",
    "audio": "Audio",
    "wearables": "Wearables",
    "televisions": "TVs & Displays",
    "cameras": "Cameras",
    "gaming": "Gaming",
    "smart-home": "Smart Home",
    "accessories": "Accessories",
}


def _load_products() -> dict[str, Product]:
    with PRODUCTS_FILE.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    products = {}
    for entry in raw:
        product = Product(**entry)
        products[product.product_id] = product
    return products


PRODUCTS: dict[str, Product] = _load_products()


def _summary(product: Product) -> ProductSummary:
    return ProductSummary(**product.model_dump(include=set(ProductSummary.model_fields)))


def _matches_search(product: Product, needle: str) -> bool:
    """Substring match across the fields a shopper would expect to search.

    Not a real search engine. It is deterministic, which matters more here: a
    workshop attendee asking the agent for "noise cancelling headphones" should
    get the same answer every time.
    """
    haystack = " ".join(
        [
            product.name,
            product.brand,
            product.category,
            product.short_description,
            product.description,
            " ".join(product.tags),
            " ".join(product.specs.values()),
        ]
    ).lower()
    return all(token in haystack for token in needle.lower().split())


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------
app, log = create_app(
    SERVICE_NAME,
    title="VoltMart Catalog",
    description="Product listings, search and detail for the VoltMart storefront.",
)


@app.get("/products", response_model=ProductPage, tags=["catalog"])
async def list_products(
    category: str | None = Query(None, description="Filter to one category slug"),
    q: str | None = Query(None, max_length=120, description="Free-text search"),
    brand: str | None = Query(None, max_length=60),
    min_price: float | None = Query(None, ge=0),
    max_price: float | None = Query(None, ge=0),
    featured: bool | None = Query(None),
    sort: Literal["relevance", "price_asc", "price_desc", "rating"] = "relevance",
    limit: int = Query(24, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> ProductPage:
    """Browse and search the catalog."""
    if min_price is not None and max_price is not None and min_price > max_price:
        raise ValidationFailed("min_price cannot be greater than max_price")

    with tracer(SERVICE_NAME).start_as_current_span("catalog.filter") as span:
        results = list(PRODUCTS.values())

        if category:
            results = [p for p in results if p.category == category]
        if brand:
            results = [p for p in results if p.brand.lower() == brand.lower()]
        if min_price is not None:
            results = [p for p in results if p.price >= min_price]
        if max_price is not None:
            results = [p for p in results if p.price <= max_price]
        if featured is not None:
            results = [p for p in results if p.featured == featured]
        if q:
            results = [p for p in results if _matches_search(p, q)]

        if sort == "price_asc":
            results.sort(key=lambda p: p.price)
        elif sort == "price_desc":
            results.sort(key=lambda p: -p.price)
        elif sort == "rating":
            results.sort(key=lambda p: (-p.rating, -p.review_count))
        else:
            # "Relevance" with no query means featured first, then best rated.
            results.sort(key=lambda p: (not p.featured, -p.rating))

        total = len(results)
        page = results[offset : offset + limit]

        span.set_attribute("voltmart.catalog.result_count", total)
        span.set_attribute("voltmart.catalog.returned", len(page))
        if q:
            span.set_attribute("voltmart.catalog.query", q)
        if category:
            span.set_attribute("voltmart.catalog.category", category)

    return ProductPage(
        items=[_summary(p) for p in page], total=total, limit=limit, offset=offset
    )


@app.get("/products/{product_id}", response_model=Product, tags=["catalog"])
async def get_product(product_id: str) -> Product:
    """Full detail for one product."""
    if not is_safe_id(product_id):
        raise ValidationFailed("product_id is not a valid identifier")
    product = PRODUCTS.get(product_id)
    if product is None:
        raise NotFound(f"No product with id {product_id}", product_id=product_id)
    return product


@app.post("/products/batch", response_model=list[ProductSummary], tags=["catalog"])
async def get_products_batch(request: BatchRequest) -> list[ProductSummary]:
    """Resolve many ids at once.

    The cart and orders services call this to put names and prices next to the
    ids they store. Unknown ids are skipped rather than failing the whole batch,
    so one delisted product does not make a saved cart unreadable.
    """
    found = [
        _summary(PRODUCTS[pid])
        for pid in request.product_ids
        if pid in PRODUCTS
    ]
    missing = len(request.product_ids) - len(found)
    if missing:
        log.info("batch lookup skipped %d unknown product ids", missing)
    return found


@app.get("/categories", response_model=list[CategoryInfo], tags=["catalog"])
async def list_categories() -> list[CategoryInfo]:
    """Categories with counts and price ranges, for building nav and filters."""
    grouped: dict[str, list[Product]] = {}
    for product in PRODUCTS.values():
        grouped.setdefault(product.category, []).append(product)

    return [
        CategoryInfo(
            category=category,
            label=CATEGORY_LABELS.get(category, category.replace("-", " ").title()),
            product_count=len(items),
            min_price=min(p.price for p in items),
            max_price=max(p.price for p in items),
        )
        for category, items in sorted(grouped.items())
    ]


@app.get("/brands", response_model=list[str], tags=["catalog"])
async def list_brands() -> list[str]:
    return sorted({product.brand for product in PRODUCTS.values()})


@app.get("/stats", tags=["catalog"])
async def catalog_stats() -> dict[str, Any]:
    """Summary numbers, used by the support agent to answer scope questions."""
    prices = [p.price for p in PRODUCTS.values()]
    return {
        "product_count": len(PRODUCTS),
        "category_count": len({p.category for p in PRODUCTS.values()}),
        "brand_count": len({p.brand for p in PRODUCTS.values()}),
        "price_range": {"min": min(prices), "max": max(prices)},
        "featured_count": sum(1 for p in PRODUCTS.values() if p.featured),
    }


log.info("catalog loaded products=%d", len(PRODUCTS))

if __name__ == "__main__":
    run("main:app")
