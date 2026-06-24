"""Lightweight observability: structlog JSON by default, OpenTelemetry optional.

The corporate host is small (2 vCPU / 8 GB) and already runs Postgres, Redis,
RabbitMQ, Ollama and a couple of agents, so tracing is OFF by default and
structured JSON logs are the baseline. Turn OpenTelemetry on per-agent only
where the trace value justifies the RAM, pointing at a shared collector.
"""

from __future__ import annotations

import logging
import sys
from functools import lru_cache
from typing import TYPE_CHECKING, Any, cast

import structlog

if TYPE_CHECKING:
    from agentkit.config import BaseAgentSettings


def setup_observability(settings: BaseAgentSettings) -> None:
    """Configure structlog once at process start. Idempotent."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(agent=settings.agent_name, env=settings.env)

    if settings.effective_otel_enabled:
        _init_otel(settings)
    elif settings.otel_exporter_otlp_endpoint and settings.private_mode:
        get_logger().warning(
            "observability.otel_disabled_by_private_mode",
            agent=settings.agent_name,
            detail="PRIVATE_MODE=true overrides OTEL_EXPORTER_OTLP_ENDPOINT",
        )


@lru_cache
def get_logger(name: str = "agentkit") -> structlog.stdlib.BoundLogger:
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name))


def _init_otel(settings: BaseAgentSettings) -> None:
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:  # pragma: no cover
        get_logger().warning("observability.otel_missing_dep")
        return
    provider = TracerProvider(resource=Resource.create({"service.name": settings.agent_name}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint))
    )
    trace.set_tracer_provider(provider)
    # NOTE: this only registers the provider/exporter. Actual spans come from the
    # auto-instrumentation wired in agentkit.server.create_app (FastAPIInstrumentor
    # + HTTPXClientInstrumentor), gated on the same OTLP endpoint — so enabling the
    # endpoint produces real traces rather than an empty collector (F9).
    get_logger().info("observability.otel_enabled", endpoint=settings.otel_exporter_otlp_endpoint)
