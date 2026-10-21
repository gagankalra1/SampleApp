"""OpenTelemetry wiring for every VoltMart service.

One function, :func:`configure_telemetry`, sets up traces, metrics and logs and
points them at an OTLP endpoint. In the EKS deployment that endpoint is the ADOT
Collector running in the cluster, which fans the data out to AWS X-Ray, Amazon
Managed Service for Prometheus and CloudWatch Logs. Nothing in this module is
AWS-specific beyond the optional X-Ray trace-id format, so the same image works
against any OTLP-compatible backend.

Call order inside a service's ``main``::

    configure_telemetry("voltmart-cart")   # before the app object is built
    app = build_app()
    instrument_app(app)                    # after the routes are registered
"""

from __future__ import annotations

import logging
import os
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from .config import TelemetryConfig, telemetry_config

LOG = logging.getLogger(__name__)

_STATE: dict[str, Any] = {"configured": False, "config": None}


def _build_resource(cfg: TelemetryConfig) -> Resource:
    """Describe *this* process so the backend can group its signals.

    The pod-level attributes come from the Kubernetes downward API (see the
    deployment manifests). They are absent locally, and absent attributes are
    simply left out rather than reported as empty strings.
    """
    attributes: dict[str, Any] = {
        "service.name": cfg.service_name,
        "service.version": cfg.service_version,
        "service.namespace": cfg.namespace,
        "deployment.environment": cfg.environment,
    }

    optional = {
        "k8s.namespace.name": os.getenv("K8S_NAMESPACE"),
        "k8s.pod.name": os.getenv("K8S_POD_NAME"),
        "k8s.node.name": os.getenv("K8S_NODE_NAME"),
        "k8s.cluster.name": os.getenv("K8S_CLUSTER_NAME"),
        "cloud.region": os.getenv("AWS_REGION"),
        "cloud.provider": "aws" if os.getenv("AWS_REGION") else None,
    }
    attributes.update({k: v for k, v in optional.items() if v})

    # Anything set via the standard OTEL_RESOURCE_ATTRIBUTES variable is merged
    # in by Resource.create, so operators can add their own tags without a
    # code change.
    return Resource.create(attributes)


def _id_generator(cfg: TelemetryConfig):
    """Return an X-Ray-compatible id generator when asked for, else ``None``.

    The dependency is optional: if the AWS propagator package is not installed
    the service degrades to standard W3C trace ids instead of failing to boot.
    """
    if not cfg.xray_id_generator:
        return None
    try:
        from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator

        return AwsXRayIdGenerator()
    except ImportError:
        LOG.info("AWS X-Ray id generator unavailable; using W3C random trace ids")
        return None


def _configure_propagators() -> None:
    """Accept both W3C ``traceparent`` and ``X-Amzn-Trace-Id`` headers.

    Requests arriving through an Application Load Balancer carry the X-Ray
    header, while requests between services carry the W3C header. Reading both
    keeps a single trace intact end to end.
    """
    try:
        from opentelemetry.propagate import set_global_textmap
        from opentelemetry.propagators.composite import CompositePropagator
        from opentelemetry.propagators.aws import AwsXRayPropagator
        from opentelemetry.trace.propagation.tracecontext import (
            TraceContextTextMapPropagator,
        )
        from opentelemetry.baggage.propagation import W3CBaggagePropagator

        set_global_textmap(
            CompositePropagator(
                [
                    TraceContextTextMapPropagator(),
                    W3CBaggagePropagator(),
                    AwsXRayPropagator(),
                ]
            )
        )
    except ImportError:
        LOG.info("AWS X-Ray propagator unavailable; using W3C propagation only")


def configure_telemetry(service_name: str) -> TelemetryConfig:
    """Install global tracer, meter and logger providers. Idempotent.

    Safe to call when no collector exists: without ``OTEL_EXPORTER_OTLP_ENDPOINT``
    the providers are still installed (so instrumentation and custom metrics
    work) but no exporter is attached.
    """
    cfg = telemetry_config(service_name)

    if _STATE["configured"]:
        return _STATE["config"]  # type: ignore[return-value]

    resource = _build_resource(cfg)
    _configure_propagators()

    # ---- Traces -----------------------------------------------------------
    provider_kwargs: dict[str, Any] = {"resource": resource}
    generator = _id_generator(cfg)
    if generator is not None:
        provider_kwargs["id_generator"] = generator

    tracer_provider = TracerProvider(**provider_kwargs)
    if cfg.otlp_endpoint:
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=cfg.otlp_endpoint, insecure=cfg.otlp_insecure)
            )
        )
    if cfg.console_traces:
        tracer_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    # ---- Metrics ----------------------------------------------------------
    readers = []
    if cfg.otlp_endpoint:
        readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(
                    endpoint=cfg.otlp_endpoint, insecure=cfg.otlp_insecure
                ),
                export_interval_millis=cfg.metric_export_interval_ms,
            )
        )
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers))

    # ---- Logs -------------------------------------------------------------
    if cfg.capture_logs and cfg.otlp_endpoint:
        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(endpoint=cfg.otlp_endpoint, insecure=cfg.otlp_insecure)
            )
        )
        handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
        logging.getLogger().addHandler(handler)
        _STATE["logger_provider"] = logger_provider

    _STATE["configured"] = True
    _STATE["config"] = cfg

    LOG.info(
        "telemetry configured service=%s endpoint=%s",
        cfg.service_name,
        cfg.otlp_endpoint or "<none: spans recorded but not exported>",
    )
    return cfg


def instrument_app(app: Any, *, excluded_urls: str = "/health,/ready,/metrics") -> None:
    """Attach auto-instrumentation to a FastAPI app and its AWS/HTTP clients.

    Health and readiness probes are excluded so that kubelet traffic does not
    drown the trace data in noise.
    """
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app, excluded_urls=excluded_urls)

    # Outbound HTTP between services, so a checkout trace reaches cart,
    # inventory, orders and payment as child spans.
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception as exc:  # pragma: no cover - instrumentation is best effort
        LOG.warning("httpx instrumentation unavailable: %s", exc)

    # DynamoDB calls appear as their own spans with table and operation names.
    try:
        from opentelemetry.instrumentation.botocore import BotocoreInstrumentor

        BotocoreInstrumentor().instrument()
    except Exception as exc:  # pragma: no cover
        LOG.warning("botocore instrumentation unavailable: %s", exc)

    # Stamps trace_id/span_id into every log line for log-to-trace correlation.
    try:
        from opentelemetry.instrumentation.logging import LoggingInstrumentor

        LoggingInstrumentor().instrument(set_logging_format=False)
    except Exception as exc:  # pragma: no cover
        LOG.warning("logging instrumentation unavailable: %s", exc)


def tracer(name: str = "voltmart") -> trace.Tracer:
    return trace.get_tracer(name)


def meter(name: str = "voltmart") -> metrics.Meter:
    return metrics.get_meter(name)


def current_trace_id() -> str | None:
    """Hex trace id of the active span, for echoing back in error responses.

    Handing the trace id to the caller is what lets a workshop attendee paste it
    straight into the X-Ray console after seeing a failure in the UI.
    """
    span = trace.get_current_span()
    context = span.get_span_context()
    if not context.is_valid:
        return None
    return format(context.trace_id, "032x")


def shutdown_telemetry() -> None:
    """Flush pending spans, metrics and logs during graceful shutdown."""
    provider = trace.get_tracer_provider()
    if hasattr(provider, "shutdown"):
        try:
            provider.shutdown()
        except Exception:  # pragma: no cover
            LOG.debug("tracer provider shutdown raised", exc_info=True)

    meter_provider = metrics.get_meter_provider()
    if hasattr(meter_provider, "shutdown"):
        try:
            meter_provider.shutdown()
        except Exception:  # pragma: no cover
            LOG.debug("meter provider shutdown raised", exc_info=True)

    logger_provider = _STATE.get("logger_provider")
    if logger_provider is not None:
        try:
            logger_provider.shutdown()
        except Exception:  # pragma: no cover
            LOG.debug("logger provider shutdown raised", exc_info=True)
