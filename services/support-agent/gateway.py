"""Synchronous HTTP gateway to the VoltMart microservices.

The rest of VoltMart uses async clients. The agent's tools are plain
synchronous functions, because that is what Strands calls them as, and mixing
an event loop into a tool body is a reliable source of subtle bugs. So this
module keeps one shared ``httpx.Client`` instead.

``HTTPXClientInstrumentor`` instruments the synchronous client too, so every
call a tool makes still shows up as a child span of the agent's trace. That is
the point of the whole exercise: you can see the agent decide, call three
services, and answer.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from voltmart_common.config import DownstreamConfig

LOG = logging.getLogger(__name__)


class ServiceError(Exception):
    """A downstream call failed in a way the agent should explain, not crash on.

    Carries the HTTP status and the structured error code so a tool can turn
    "409 conflict / order already shipped" into a sentence a customer
    understands.
    """

    def __init__(self, message: str, *, status: int = 0, code: str = "", details: Any = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.details = details or {}


class ServiceGateway:
    """One client per downstream service, sharing a connection pool."""

    def __init__(self, config: DownstreamConfig | None = None):
        self.config = config or DownstreamConfig()
        self._client = httpx.Client(
            timeout=httpx.Timeout(self.config.timeout_seconds),
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=15),
            headers={"user-agent": "voltmart-support-agent/1.0"},
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    # -- plumbing ----------------------------------------------------------
    def _call(
        self,
        base_url: str,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{base_url.rstrip('/')}{path}"
        try:
            response = self._client.request(method, url, json=json, params=params)
        except httpx.TimeoutException as exc:
            raise ServiceError(
                "The service did not respond in time.", status=504, code="timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise ServiceError(
                "The service is unreachable.", status=503, code="unreachable"
            ) from exc

        if response.status_code >= 400:
            message = f"HTTP {response.status_code}"
            code = ""
            details: Any = {}
            try:
                payload = response.json()
                error = payload.get("error", {}) if isinstance(payload, dict) else {}
                message = error.get("message", message)
                code = error.get("code", "")
                details = error.get("details", {})
            except ValueError:
                pass
            raise ServiceError(
                message, status=response.status_code, code=code, details=details
            )

        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # -- catalog -----------------------------------------------------------
    def search_products(self, **params: Any) -> dict[str, Any]:
        return self._call(self.config.catalog, "GET", "/products", params=params)

    def get_product(self, product_id: str) -> dict[str, Any]:
        return self._call(self.config.catalog, "GET", f"/products/{product_id}")

    def list_categories(self) -> list[dict[str, Any]]:
        return self._call(self.config.catalog, "GET", "/categories")

    def catalog_stats(self) -> dict[str, Any]:
        return self._call(self.config.catalog, "GET", "/stats")

    # -- inventory ---------------------------------------------------------
    def get_stock(self, product_id: str) -> dict[str, Any]:
        return self._call(self.config.inventory, "GET", f"/inventory/{product_id}")

    # -- cart --------------------------------------------------------------
    def get_cart(self, cart_id: str) -> dict[str, Any]:
        return self._call(self.config.cart, "GET", f"/carts/{cart_id}")

    def get_cart_for_customer(self, customer_id: str) -> dict[str, Any]:
        return self._call(
            self.config.cart, "GET", f"/carts/by-customer/{customer_id}"
        )

    def create_cart(self, customer_id: str) -> dict[str, Any]:
        return self._call(
            self.config.cart, "POST", "/carts", json={"customer_id": customer_id}
        )

    def add_cart_item(self, cart_id: str, product_id: str, quantity: int) -> dict[str, Any]:
        return self._call(
            self.config.cart,
            "POST",
            f"/carts/{cart_id}/items",
            json={"product_id": product_id, "quantity": quantity},
        )

    def set_cart_item_quantity(
        self, cart_id: str, product_id: str, quantity: int
    ) -> dict[str, Any]:
        return self._call(
            self.config.cart,
            "PUT",
            f"/carts/{cart_id}/items/{product_id}",
            json={"quantity": quantity},
        )

    def remove_cart_item(self, cart_id: str, product_id: str) -> dict[str, Any]:
        return self._call(
            self.config.cart, "DELETE", f"/carts/{cart_id}/items/{product_id}"
        )

    # -- orders ------------------------------------------------------------
    def list_orders(self, customer_id: str, limit: int = 10) -> list[dict[str, Any]]:
        return self._call(
            self.config.orders,
            "GET",
            "/orders",
            params={"customer_id": customer_id, "limit": limit},
        )

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._call(self.config.orders, "GET", f"/orders/{order_id}")

    def get_tracking(self, order_id: str) -> dict[str, Any]:
        return self._call(self.config.orders, "GET", f"/orders/{order_id}/tracking")

    def cancel_order(self, order_id: str, reason: str) -> dict[str, Any]:
        return self._call(
            self.config.orders,
            "POST",
            f"/orders/{order_id}/cancel",
            json={"reason": reason},
        )

    # -- payment -----------------------------------------------------------
    def list_payments(self, order_id: str) -> list[dict[str, Any]]:
        return self._call(
            self.config.payment, "GET", "/payments", params={"order_id": order_id}
        )

    def refund_payment(self, payment_id: str, reason: str) -> dict[str, Any]:
        return self._call(
            self.config.payment,
            "POST",
            f"/payments/{payment_id}/refund",
            json={"reason": reason},
        )

    # -- checkout ----------------------------------------------------------
    def checkout_preview(self, cart_id: str) -> dict[str, Any]:
        return self._call(
            self.config.checkout, "GET", f"/checkout/preview/{cart_id}"
        )
