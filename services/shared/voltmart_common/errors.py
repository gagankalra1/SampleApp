"""A single error shape for every VoltMart service.

Two goals. First, a caller always gets the same JSON body, so the frontend and
the support agent parse one format. Second, the response carries the trace id,
which turns "it broke" into "it broke, here is the trace" without the user
having to dig through logs.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace

from .telemetry import current_trace_id


class VoltMartError(Exception):
    """Base class for errors that map to a deliberate HTTP status.

    Anything that is *not* a ``VoltMartError`` is a bug, gets logged with a
    stack trace, is recorded on the span as an exception, and comes back to the
    caller as a generic 500 with no internal detail.
    """

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFound(VoltMartError):
    status_code = 404
    code = "not_found"


class ValidationFailed(VoltMartError):
    status_code = 422
    code = "validation_failed"


class Conflict(VoltMartError):
    """The request is well-formed but the current state forbids it.

    Used for "that order is already shipped" and "not enough stock".
    """

    status_code = 409
    code = "conflict"


class OutOfStock(Conflict):
    code = "out_of_stock"


class PaymentDeclined(VoltMartError):
    status_code = 402
    code = "payment_declined"


class Unauthorized(VoltMartError):
    status_code = 401
    code = "unauthorized"


class Forbidden(VoltMartError):
    status_code = 403
    code = "forbidden"


class DownstreamUnavailable(VoltMartError):
    status_code = 503
    code = "downstream_unavailable"


def _body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        payload["error"]["details"] = details
    trace_id = current_trace_id()
    if trace_id:
        payload["error"]["trace_id"] = trace_id
    return payload


def install_error_handlers(app: FastAPI, logger: Any) -> None:
    @app.exception_handler(VoltMartError)
    async def _handle_known(request: Request, exc: VoltMartError) -> JSONResponse:
        span = trace.get_current_span()
        span.set_attribute("voltmart.error.code", exc.code)
        # A 4xx is the caller's problem and a normal part of operation, so it
        # does not mark the span as an error and does not page anyone.
        if exc.status_code >= 500:
            span.set_status(trace.Status(trace.StatusCode.ERROR, exc.message))
            logger.error(
                "request failed path=%s code=%s message=%s",
                request.url.path,
                exc.code,
                exc.message,
            )
        else:
            logger.info(
                "request rejected path=%s code=%s message=%s",
                request.url.path,
                exc.code,
                exc.message,
            )
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        span = trace.get_current_span()
        span.record_exception(exc)
        span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
        logger.exception("unhandled error path=%s", request.url.path)
        # Deliberately opaque: internal messages can leak table names, ARNs and
        # stack details to whoever is calling.
        return JSONResponse(
            status_code=500,
            content=_body("internal_error", "An unexpected error occurred."),
        )
