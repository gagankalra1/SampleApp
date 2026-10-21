"""Shared building blocks for every VoltMart microservice.

Importing this package does not start any telemetry. Each service calls
:func:`voltmart_common.telemetry.configure_telemetry` once, as early as possible
in its startup path, then :func:`voltmart_common.telemetry.instrument_app`.
"""

__all__ = [
    "config",
    "errors",
    "http",
    "ids",
    "metrics",
    "service",
    "storage",
    "telemetry",
]

__version__ = "1.0.0"
