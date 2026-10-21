"""Environment-driven configuration shared by all VoltMart services.

Nothing here reads a secret from a file or from source control. Values arrive as
environment variables, which in EKS come from a ConfigMap (plain settings) or a
Secret mounted by the platform (credentials). The service never logs them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on", "enabled"}


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class TelemetryConfig:
    """How this process talks to the OpenTelemetry Collector."""

    service_name: str
    service_version: str = field(default_factory=lambda: _env("SERVICE_VERSION", "1.0.0"))
    namespace: str = field(default_factory=lambda: _env("OTEL_SERVICE_NAMESPACE", "voltmart"))
    environment: str = field(default_factory=lambda: _env("DEPLOYMENT_ENVIRONMENT", "workshop"))

    # Empty endpoint means "no collector reachable": the service still runs and
    # still records spans in-process, it just does not export them. That keeps
    # local development and unit tests free of a collector dependency.
    otlp_endpoint: str = field(
        default_factory=lambda: _env("OTEL_EXPORTER_OTLP_ENDPOINT")
    )
    otlp_insecure: bool = field(
        default_factory=lambda: _env_bool("OTEL_EXPORTER_OTLP_INSECURE", True)
    )
    console_traces: bool = field(
        default_factory=lambda: _env_bool("OTEL_TRACES_CONSOLE_EXPORTER", False)
    )
    metric_export_interval_ms: int = field(
        default_factory=lambda: _env_int("OTEL_METRIC_EXPORT_INTERVAL", 30_000)
    )
    capture_logs: bool = field(
        default_factory=lambda: _env_bool("OTEL_PYTHON_LOG_CORRELATION", True)
    )
    # X-Ray-compatible 1:1 trace ids keep the AWS console and the OTLP pipeline
    # pointing at the same trace. Turn off for pure OTLP backends.
    xray_id_generator: bool = field(
        default_factory=lambda: _env_bool("OTEL_AWS_XRAY_ID_GENERATOR", True)
    )

    @property
    def enabled(self) -> bool:
        return bool(self.otlp_endpoint) or self.console_traces


@dataclass(frozen=True)
class StorageConfig:
    """Where a service keeps its state.

    ``memory`` is for local development and tests. ``dynamodb`` is what the
    CloudFormation stack provisions and what the EKS deployment uses.
    """

    backend: str = field(default_factory=lambda: _env("STORAGE_BACKEND", "memory").lower())
    table_name: str = field(default_factory=lambda: _env("TABLE_NAME"))
    region: str = field(
        default_factory=lambda: _env("AWS_REGION") or _env("AWS_DEFAULT_REGION", "us-west-2")
    )
    endpoint_url: str = field(default_factory=lambda: _env("DYNAMODB_ENDPOINT_URL"))

    def __post_init__(self) -> None:
        if self.backend not in {"memory", "dynamodb"}:
            raise ValueError(
                f"STORAGE_BACKEND must be 'memory' or 'dynamodb', got {self.backend!r}"
            )
        if self.backend == "dynamodb" and not self.table_name:
            raise ValueError("STORAGE_BACKEND=dynamodb requires TABLE_NAME to be set")


@dataclass(frozen=True)
class DownstreamConfig:
    """Base URLs of the sibling services.

    Every value is a cluster-internal URL. These services are ClusterIP-only;
    nothing here should ever be a public hostname.
    """

    catalog: str = field(default_factory=lambda: _env("CATALOG_URL", "http://localhost:8081"))
    inventory: str = field(default_factory=lambda: _env("INVENTORY_URL", "http://localhost:8082"))
    cart: str = field(default_factory=lambda: _env("CART_URL", "http://localhost:8083"))
    orders: str = field(default_factory=lambda: _env("ORDERS_URL", "http://localhost:8084"))
    payment: str = field(default_factory=lambda: _env("PAYMENT_URL", "http://localhost:8085"))
    checkout: str = field(default_factory=lambda: _env("CHECKOUT_URL", "http://localhost:8086"))
    timeout_seconds: float = field(
        default_factory=lambda: _env_float("DOWNSTREAM_TIMEOUT_SECONDS", 5.0)
    )


@dataclass(frozen=True)
class ServerConfig:
    # Default bind is loopback so that running a service by hand never
    # accidentally publishes it. Containers set BIND_HOST=0.0.0.0 explicitly,
    # where the pod network — not the process — is the isolation boundary.
    host: str = field(default_factory=lambda: _env("BIND_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("PORT", 8080))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "info").lower())
    # Browser origins allowed to call the API. The frontend is served from the
    # same origin as the API in the shipped topology, so the default is empty.
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            origin.strip()
            for origin in _env("CORS_ALLOWED_ORIGINS").split(",")
            if origin.strip()
        )
    )


def telemetry_config(service_name: str) -> TelemetryConfig:
    return TelemetryConfig(service_name=_env("OTEL_SERVICE_NAME", service_name))


def storage_config() -> StorageConfig:
    return StorageConfig()


def downstream_config() -> DownstreamConfig:
    return DownstreamConfig()


def server_config() -> ServerConfig:
    return ServerConfig()
