"""
Heddle distributed tracing — optional OpenTelemetry integration.

Provides span creation, trace context propagation through NATS messages,
and graceful no-op behavior when OTel SDK is not installed.

Install the optional dependency::

    uv sync --extra otel

Usage::

    from heddle.tracing import get_tracer, inject_trace_context, extract_trace_context

    tracer = get_tracer("heddle.pipeline")

    # Creating spans
    with tracer.start_as_current_span("pipeline.execute") as span:
        span.set_attribute("pipeline.name", name)
        ...

    # Propagating context through NATS messages
    msg = task.model_dump(mode="json")
    inject_trace_context(msg)       # adds _trace_context key
    await bus.publish(subject, msg)

    # Extracting context on the receiving side
    ctx = extract_trace_context(data)
    with tracer.start_as_current_span("worker.process", context=ctx):
        ...
"""

from heddle.tracing.metrics import (
    get_meter,
    init_metrics,
    record_task_completed,
)
from heddle.tracing.otel import (
    extract_trace_context,
    get_tracer,
    init_tracing,
    inject_trace_context,
)

__all__ = [
    "extract_trace_context",
    "get_meter",
    "get_tracer",
    "init_metrics",
    "init_tracing",
    "inject_trace_context",
    "record_task_completed",
]
