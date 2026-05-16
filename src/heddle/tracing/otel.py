"""
OpenTelemetry integration for Heddle distributed tracing.

All public functions in this module are safe to call without
``opentelemetry`` installed — they degrade to no-ops. This lets
production code instrument unconditionally while making OTel an
optional dependency.

Trace context propagation uses W3C ``traceparent`` format, injected into
NATS message dicts under the ``_trace_context`` key.

GenAI semantic conventions
~~~~~~~~~~~~~~~~~~~~~~~~~~

LLM call spans (``llm.call``) in ``worker/runner.py`` follow the emerging
OTel GenAI semantic conventions for attribute naming:

- ``gen_ai.system`` — provider identifier (``anthropic``, ``ollama``, ``openai``)
- ``gen_ai.request.model`` / ``gen_ai.response.model`` — model names
- ``gen_ai.usage.input_tokens`` / ``gen_ai.usage.output_tokens`` — token counts
- ``gen_ai.request.temperature`` / ``gen_ai.request.max_tokens`` — request params

When ``HEDDLE_TRACE_CONTENT=1``, prompt and completion text are recorded as
span events (``gen_ai.content.prompt``, ``gen_ai.content.completion``).

See: https://opentelemetry.io/docs/specs/semconv/gen-ai/

Legacy ``llm.*`` attributes are preserved for backward compatibility.

Setup::

    from heddle.tracing import init_tracing
    init_tracing("heddle-pipeline", endpoint="http://localhost:4317")
"""

from __future__ import annotations

import contextlib
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Feature detection — is OTel SDK available?
# ---------------------------------------------------------------------------

_HAS_OTEL = False
_trace_mod: Any = None
_context_mod: Any = None
_propagate_mod: Any = None

with contextlib.suppress(ImportError):
    from opentelemetry import propagate as _propagate_mod  # type: ignore[no-redef]
    from opentelemetry import trace as _trace_mod  # type: ignore[no-redef]

    _HAS_OTEL = True


# ---------------------------------------------------------------------------
# No-op fallbacks (used when OTel is not installed)
# ---------------------------------------------------------------------------


class _NoOpSpan:
    """Minimal span stand-in that accepts any method call silently."""

    def set_attribute(self, key: str, value: Any) -> None:
        """No-op."""

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        """No-op."""

    def record_exception(self, exception: BaseException) -> None:
        """No-op."""

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        """No-op."""

    def end(self) -> None:
        """No-op."""

    def __enter__(self) -> _NoOpSpan:
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class _NoOpTracer:
    """Minimal tracer that returns ``_NoOpSpan`` for every call."""

    @contextmanager
    def start_as_current_span(
        self,
        name: str,
        context: Any = None,
        attributes: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Iterator[_NoOpSpan]:
        """Yield a no-op span."""
        yield _NoOpSpan()

    def start_span(
        self,
        name: str,
        context: Any = None,
        attributes: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> _NoOpSpan:
        """Return a no-op span."""
        return _NoOpSpan()


_NOOP_TRACER = _NoOpTracer()

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


_TRACING_INITIALIZED = False


def init_tracing(
    service_name: str = "heddle",
    *,
    endpoint: str | None = None,
) -> bool:
    """Initialize OTel tracing with OTLP exporter.

    Idempotent: a second call is a no-op that returns ``True``.  Without
    this guard, calling ``init_tracing`` twice triggered the OTel SDK's
    "Overriding of current TracerProvider is not allowed" warning, which
    surfaced in tests and in CLI commands that re-imported the tracing
    module.

    Args:
        service_name: Service name reported to the collector.
        endpoint: OTLP gRPC endpoint (e.g. ``http://localhost:4317``).
            Defaults to the ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var.

    Returns:
        ``True`` if OTel was initialized (or was already initialized),
        ``False`` if not installed.
    """
    global _TRACING_INITIALIZED  # noqa: PLW0603 — module-level singleton flag is the simplest idempotency guard

    if not _HAS_OTEL:
        logger.info("tracing.otel_not_available", hint="install with: uv sync --extra otel")
        return False

    if _TRACING_INITIALIZED:
        logger.debug("tracing.already_initialized", service_name=service_name)
        return True

    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # type: ignore[import-untyped]
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource  # type: ignore[import-untyped]
        from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import-untyped]
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,  # type: ignore[import-untyped]
        )
    except ImportError:
        logger.warning("tracing.sdk_import_failed", hint="install opentelemetry-sdk and exporter")
        return False

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    exporter_kwargs: dict[str, Any] = {}
    if endpoint:
        exporter_kwargs["endpoint"] = endpoint

    exporter = OTLPSpanExporter(**exporter_kwargs)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    _trace_mod.set_tracer_provider(provider)
    _TRACING_INITIALIZED = True

    logger.info("tracing.initialized", service_name=service_name, endpoint=endpoint)
    return True


def get_tracer(name: str = "heddle") -> Any:
    """Get a tracer instance (real or no-op depending on OTel availability).

    Args:
        name: Instrumentation scope name (e.g. ``heddle.pipeline``).

    Returns:
        An OTel ``Tracer`` if SDK is available, otherwise a ``_NoOpTracer``.
    """
    if _HAS_OTEL:
        return _trace_mod.get_tracer(name)
    return _NOOP_TRACER


def inject_trace_context(carrier: dict[str, Any]) -> None:
    """Inject current trace context into a message dict.

    Adds a ``_trace_context`` key containing W3C propagation headers.
    Safe to call without OTel installed (no-op).

    Args:
        carrier: Message dict (modified in-place).
    """
    if not _HAS_OTEL:
        return
    headers: dict[str, str] = {}
    _propagate_mod.inject(headers)
    if headers:
        carrier["_trace_context"] = headers


def extract_trace_context(carrier: dict[str, Any]) -> Any:
    """Extract trace context from a message dict.

    Reads the ``_trace_context`` key and returns an OTel ``Context``
    that can be passed to ``tracer.start_as_current_span(context=...)``.

    Args:
        carrier: Message dict with optional ``_trace_context`` key.

    Returns:
        An OTel ``Context`` if available, otherwise ``None``.
    """
    if not _HAS_OTEL:
        return None
    headers = carrier.get("_trace_context")
    if not headers or not isinstance(headers, dict):
        return None
    return _propagate_mod.extract(headers)


def trace_correlation_processor(
    logger: Any,  # noqa: ARG001 — structlog processor signature requires (logger, method, event)
    method_name: str,  # noqa: ARG001 — structlog processor signature requires (logger, method, event)
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Structlog processor that tags log records with the active trace context.

    When called inside a span, adds ``trace_id`` (32-char hex) and
    ``span_id`` (16-char hex) to the event_dict so downstream renderers
    and shippers can correlate logs with their span in any OTel backend.
    No-op when OTel is unavailable or when no span is active.

    Wire into a ``structlog.configure(...)`` call before the renderer,
    e.g.::

        structlog.configure(processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            trace_correlation_processor,
            structlog.dev.ConsoleRenderer(),
        ])

    The hex encoding matches the W3C traceparent convention used by
    most OTel backends and the heddle ``_trace_context`` wire field.
    """
    if not _HAS_OTEL:
        return event_dict
    span = _trace_mod.get_current_span()
    span_context = span.get_span_context()
    if not span_context.is_valid:
        return event_dict
    event_dict["trace_id"] = format(span_context.trace_id, "032x")
    event_dict["span_id"] = format(span_context.span_id, "016x")
    return event_dict
