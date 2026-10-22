"""Application factory shared by all VoltMart FastAPI services.

Every service gets the same baseline: JSON logging with trace correlation,
consistent error bodies, liveness and readiness probes, and OpenTelemetry
instrumentation. A service module only has to supply its routes.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
# Imported so configure_logging can recognise and preserve the OTLP log
# handler that configure_telemetry installs.
from opentelemetry.sdk._logs import LoggingHandler

from .config import server_config
from .errors import install_error_handlers
from .telemetry import (
    configure_telemetry,
    current_trace_id,
    instrument_app,
    shutdown_telemetry,
)


class _JsonFormatter(logging.Formatter):
    """Structured log lines carrying the trace id.

    CloudWatch Logs Insights can filter on these fields directly, and the
    ``trace_id`` is what lets you jump from a log line to the X-Ray trace.
    """

    def __init__(self, service_name: str):
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "service": self.service_name,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # LoggingInstrumentor injects these; fall back to the active span.
        trace_id = getattr(record, "otelTraceID", None) or current_trace_id()
        span_id = getattr(record, "otelSpanID", None)
        if trace_id and trace_id != "0" * 32:
            payload["trace_id"] = trace_id
        if span_id and span_id != "0" * 16:
            payload["span_id"] = span_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(service_name: str, level: str = "info") -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter(service_name))

    root = logging.getLogger()

    # Replace only the stream handlers, so each line is emitted once as JSON.
    #
    # Critically, this must NOT clear the whole handler list. configure_telemetry
    # runs first and attaches an OpenTelemetry LoggingHandler that ships logs
    # over OTLP; `root.handlers = [handler]` removed it again immediately, so
    # log export silently never happened and /aws/voltmart/application stayed
    # empty while traces and metrics flowed normally.
    preserved = [
        h for h in root.handlers
        if not isinstance(h, logging.StreamHandler) or isinstance(h, LoggingHandler)
    ]
    root.handlers = [*preserved, handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # uvicorn installs its own handlers; route them through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True

    # The access log duplicates what the HTTP spans already record.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    return logging.getLogger(service_name)


def create_app(
    service_name: str,
    *,
    title: str,
    description: str = "",
    readiness_check: Callable[[], bool] | None = None,
    on_startup: Callable[[], Awaitable[None]] | None = None,
    on_shutdown: Callable[[], Awaitable[None]] | None = None,
) -> tuple[FastAPI, logging.Logger]:
    """Build a configured FastAPI app plus its logger.

    Telemetry is configured before the app object exists so that startup work
    is already traced.
    """
    cfg = server_config()
    telemetry = configure_telemetry(service_name)
    logger = configure_logging(service_name, cfg.log_level)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        logger.info(
            "starting service=%s version=%s otlp=%s",
            service_name,
            telemetry.service_version,
            telemetry.otlp_endpoint or "disabled",
        )
        if on_startup is not None:
            await on_startup()
        try:
            yield
        finally:
            if on_shutdown is not None:
                await on_shutdown()
            # Flush buffered spans and metrics so the last few seconds before a
            # pod terminates are not lost.
            shutdown_telemetry()
            logger.info("stopped service=%s", service_name)

    app = FastAPI(
        title=title,
        description=description,
        version=telemetry.service_version,
        lifespan=lifespan,
        # The interactive docs are useful in a workshop. They expose only the
        # schema, never data, and the services sit behind the cluster boundary.
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    if cfg.cors_origins:
        # Only set when the frontend is served from a different origin than the
        # API. The shipped topology is same-origin, so this stays off and no
        # wildcard is ever used.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cfg.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["content-type", "x-voltmart-customer-id", "traceparent"],
            max_age=600,
        )

    install_error_handlers(app, logger)

    @app.get("/health", tags=["operations"], summary="Liveness probe")
    async def health() -> dict[str, str]:
        """Is the process alive? Never touches a dependency.

        A liveness probe that checked DynamoDB would restart every pod during a
        dependency blip, turning a partial outage into a full one.
        """
        return {"status": "ok", "service": service_name}

    @app.get("/ready", tags=["operations"], summary="Readiness probe")
    async def ready() -> dict[str, Any]:
        """Can the process serve traffic? Checks its own dependencies."""
        healthy = readiness_check() if readiness_check is not None else True
        return {
            "status": "ready" if healthy else "not_ready",
            "service": service_name,
            "dependencies_ok": healthy,
        }

    @app.get("/info", tags=["operations"], summary="Build and telemetry metadata")
    async def info() -> dict[str, Any]:
        """Non-sensitive metadata, handy for verifying a workshop deployment."""
        return {
            "service": service_name,
            "version": telemetry.service_version,
            "environment": telemetry.environment,
            "pod": os.getenv("K8S_POD_NAME", "local"),
            "node": os.getenv("K8S_NODE_NAME", "local"),
            "region": os.getenv("AWS_REGION", "unset"),
            "telemetry_enabled": telemetry.enabled,
            "storage_backend": os.getenv("STORAGE_BACKEND", "memory"),
        }

    instrument_app(app)
    return app, logger


def run(app_path: str) -> None:
    """Entry point used by each service's ``__main__`` block."""
    import uvicorn

    cfg = server_config()
    uvicorn.run(
        app_path,
        host=cfg.host,
        port=cfg.port,
        log_config=None,  # configure_logging already owns logging
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips="*",  # behind the in-cluster nginx / ALB
    )
