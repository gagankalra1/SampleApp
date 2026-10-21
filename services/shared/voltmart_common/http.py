"""HTTP client used for service-to-service calls.

Three things matter here for a workshop about observability:

1. One shared ``httpx.AsyncClient`` per service, so connection reuse is visible
   in the traces instead of a TCP handshake per call.
2. Trace context propagates automatically — ``HTTPXClientInstrumentor`` injects
   ``traceparent`` — which is what stitches a checkout into one trace spanning
   five services.
3. A downstream failure becomes a typed error, not a raw ``httpx`` exception, so
   the saga in the checkout service can tell "declined" from "unreachable" and
   compensate accordingly.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from opentelemetry import trace

from .config import DownstreamConfig
from .errors import (
    Conflict,
    DownstreamUnavailable,
    Forbidden,
    NotFound,
    OutOfStock,
    PaymentDeclined,
    Unauthorized,
    ValidationFailed,
    VoltMartError,
)

LOG = logging.getLogger(__name__)

# Maps the error code a service reports back onto the matching exception class,
# so a 409 "out_of_stock" from inventory arrives at the caller as OutOfStock.
_CODE_TO_ERROR: dict[str, type[VoltMartError]] = {
    "not_found": NotFound,
    "validation_failed": ValidationFailed,
    "conflict": Conflict,
    "out_of_stock": OutOfStock,
    "payment_declined": PaymentDeclined,
    "unauthorized": Unauthorized,
    "forbidden": Forbidden,
}

_STATUS_TO_ERROR: dict[int, type[VoltMartError]] = {
    400: ValidationFailed,
    401: Unauthorized,
    402: PaymentDeclined,
    403: Forbidden,
    404: NotFound,
    409: Conflict,
    422: ValidationFailed,
}


class ServiceClient:
    """Thin async JSON client for one downstream service."""

    def __init__(self, name: str, base_url: str, timeout_seconds: float = 5.0):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_seconds),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            headers={"user-agent": f"voltmart-service-client/{name}"},
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        span = trace.get_current_span()
        span.set_attribute("voltmart.downstream.service", self.name)
        try:
            response = await self._client.request(
                method, path, json=json, params=params, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise DownstreamUnavailable(
                f"The {self.name} service did not respond in time.", service=self.name
            ) from exc
        except httpx.HTTPError as exc:
            raise DownstreamUnavailable(
                f"The {self.name} service is unreachable.", service=self.name
            ) from exc

        if response.status_code >= 400:
            raise self._translate(response)

        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _translate(self, response: httpx.Response) -> VoltMartError:
        """Turn a downstream error response back into a typed exception."""
        code = ""
        message = f"The {self.name} service returned HTTP {response.status_code}."
        details: dict[str, Any] = {}
        try:
            payload = response.json()
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            code = str(error.get("code", ""))
            message = str(error.get("message", message))
            details = error.get("details", {}) or {}
        except ValueError:
            LOG.warning(
                "non-JSON error from %s status=%s", self.name, response.status_code
            )

        if response.status_code >= 500:
            # Do not relay a downstream 500's message to our own caller; it may
            # contain internals from the other service.
            return DownstreamUnavailable(
                f"The {self.name} service failed to handle the request.",
                service=self.name,
            )

        error_class = _CODE_TO_ERROR.get(code) or _STATUS_TO_ERROR.get(
            response.status_code, VoltMartError
        )
        return error_class(message, service=self.name, **details)

    # -- convenience wrappers ---------------------------------------------
    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> Any:
        return await self.request("POST", path, **kwargs)

    async def put(self, path: str, **kwargs: Any) -> Any:
        return await self.request("PUT", path, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> Any:
        return await self.request("DELETE", path, **kwargs)


class ServiceClients:
    """Lazily-built clients for the sibling services a process actually calls."""

    def __init__(self, config: DownstreamConfig | None = None):
        self.config = config or DownstreamConfig()
        self._clients: dict[str, ServiceClient] = {}

    def _client(self, name: str, base_url: str) -> ServiceClient:
        if name not in self._clients:
            self._clients[name] = ServiceClient(
                name, base_url, self.config.timeout_seconds
            )
        return self._clients[name]

    @property
    def catalog(self) -> ServiceClient:
        return self._client("catalog", self.config.catalog)

    @property
    def inventory(self) -> ServiceClient:
        return self._client("inventory", self.config.inventory)

    @property
    def cart(self) -> ServiceClient:
        return self._client("cart", self.config.cart)

    @property
    def orders(self) -> ServiceClient:
        return self._client("orders", self.config.orders)

    @property
    def payment(self) -> ServiceClient:
        return self._client("payment", self.config.payment)

    @property
    def checkout(self) -> ServiceClient:
        return self._client("checkout", self.config.checkout)

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
