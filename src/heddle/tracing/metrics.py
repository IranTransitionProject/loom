"""
OpenTelemetry metrics for Heddle.

Companion to :mod:`heddle.tracing.otel` (spans). The two are separate
modules but share the same no-op-when-absent pattern: every public
function in this module is safe to call without ``opentelemetry``
installed; instruments degrade to no-ops.

Initialize alongside tracing via :func:`heddle.tracing.init_tracing`
(which calls :func:`init_metrics` internally) or directly via
:func:`init_metrics` if metrics-only.

Reserved framework instruments
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Heddle emits a small set of framework-level instruments, added
incrementally as audit work lands. Names are stable; attribute
schemas follow OTel semantic conventions where applicable
(``messaging.*`` for bus) and heddle-native names where not
(``worker_type``, ``model_tier``, ``status``, ``orchestrator_name``).

Operators querying these instruments in their backend (Prometheus,
Grafana, etc.) depend on stable names and attribute keys — changes
here are wire-breaks for their dashboards.

Name (instrument)                       Type       Unit  Attributes
--------------------------------------  ---------  ----  ----------------------------------
``heddle.tasks.received``               counter    1     worker_type, model_tier
``heddle.tasks.completed``              counter    1     worker_type, model_tier, status
``heddle.task.duration``                histogram  ms    worker_type, model_tier, status
``heddle.bus.publish.latency``          histogram  ms    messaging.destination.name,
                                                          messaging.system
``heddle.orchestrator.goals.received``  counter    1     orchestrator_name

Operationally: ``received - completed`` ≈ in-flight task count per
``(worker_type, model_tier)``. ``task.duration`` is recorded for both
success and failure paths so operators see worst-case latency under
load, not just the happy-path mean.

Why module-level instruments are safe to define before init
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

OTel-Python uses ``ProxyMeterProvider`` semantics. ``get_meter(...)``
returns a meter that delegates to whichever ``MeterProvider`` is
current at call time, not at meter-creation time. So instruments
created at this module's import — before ``init_metrics`` has run —
correctly route to the configured exporter once init completes.
This also lets tests install an in-memory ``MeterProvider`` *after*
this module is imported and still observe the framework instruments.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any

import structlog

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Feature detection
# ---------------------------------------------------------------------------

_HAS_METRICS = False
_metrics_mod: Any = None

with contextlib.suppress(ImportError):
    from opentelemetry import metrics as _metrics_mod  # type: ignore[no-redef]

    _HAS_METRICS = True


# ---------------------------------------------------------------------------
# No-op fallbacks (when OTel API is not installed)
# ---------------------------------------------------------------------------


class _NoOpCounter:
    """Counter stand-in that silently accepts ``add()`` calls."""

    def add(
        self,
        amount: int | float = 1,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """No-op."""


class _NoOpHistogram:
    """Histogram stand-in that silently accepts ``record()`` calls."""

    def record(
        self,
        amount: int | float,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """No-op."""


class _NoOpMeter:
    """Meter stand-in that returns no-op instruments for every call."""

    def create_counter(
        self,
        name: str,
        unit: str = "",
        description: str = "",
    ) -> _NoOpCounter:
        """Return a no-op counter."""
        return _NoOpCounter()

    def create_histogram(
        self,
        name: str,
        unit: str = "",
        description: str = "",
    ) -> _NoOpHistogram:
        """Return a no-op histogram."""
        return _NoOpHistogram()


_NOOP_METER = _NoOpMeter()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


_METRICS_INITIALIZED = False


def init_metrics(
    service_name: str = "heddle",
    *,
    endpoint: str | None = None,
) -> bool:
    """Initialize OTel metrics with an OTLP gRPC exporter.

    Idempotent: a second call is a no-op that returns ``True``.

    Args:
        service_name: Service name reported to the collector.
        endpoint: OTLP gRPC endpoint (e.g. ``http://localhost:4317``).
            Defaults to the ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var.

    Returns:
        ``True`` if metrics were initialized (or were already
        initialized), ``False`` if the OTel SDK is not installed.
    """
    global _METRICS_INITIALIZED  # noqa: PLW0603 — module-level singleton flag

    if not _HAS_METRICS:
        logger.info("metrics.otel_not_available", hint="install with: uv sync --extra otel")
        return False

    if _METRICS_INITIALIZED:
        logger.debug("metrics.already_initialized", service_name=service_name)
        return True

    try:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (  # type: ignore[import-untyped]
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider  # type: ignore[import-untyped]
        from opentelemetry.sdk.metrics.export import (
            PeriodicExportingMetricReader,  # type: ignore[import-untyped]
        )
        from opentelemetry.sdk.resources import Resource  # type: ignore[import-untyped]
    except ImportError:
        logger.warning(
            "metrics.sdk_import_failed",
            hint="install opentelemetry-sdk and opentelemetry-exporter-otlp",
        )
        return False

    resource = Resource.create({"service.name": service_name})
    exporter_kwargs: dict[str, Any] = {}
    if endpoint:
        exporter_kwargs["endpoint"] = endpoint
    exporter = OTLPMetricExporter(**exporter_kwargs)
    reader = PeriodicExportingMetricReader(exporter)
    provider = MeterProvider(resource=resource, metric_readers=[reader])
    _metrics_mod.set_meter_provider(provider)
    _METRICS_INITIALIZED = True

    effective_endpoint = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    logger.info("metrics.initialized", service_name=service_name, endpoint=effective_endpoint)
    return True


def get_meter(name: str = "heddle") -> Any:
    """Return a Meter instance (real or no-op based on OTel availability).

    Args:
        name: Instrumentation scope (e.g. ``heddle.framework``).

    Returns:
        An OTel ``Meter`` if the API is importable, otherwise a
        ``_NoOpMeter``.
    """
    if _HAS_METRICS:
        return _metrics_mod.get_meter(name)
    return _NOOP_METER


# ---------------------------------------------------------------------------
# Framework instruments
# ---------------------------------------------------------------------------

_framework_meter = get_meter("heddle.framework")

# Counter: tasks received by a worker (immediately after envelope parse,
# before any processing). Wired into ``TaskWorker.handle_message``.
TASKS_RECEIVED = _framework_meter.create_counter(
    "heddle.tasks.received",
    description=(
        "Count of tasks a worker has received (parsed off the bus, "
        "about to process). Combined with ``heddle.tasks.completed`` "
        "yields per-worker in-flight depth: received - completed."
    ),
    unit="1",
)

# Counter: tasks reaching a terminal status (completed or failed).
# Wired into ``heddle.worker.base.TaskWorker._publish_result``.
TASKS_COMPLETED = _framework_meter.create_counter(
    "heddle.tasks.completed",
    description=(
        "Count of tasks reaching a terminal status (completed or failed). "
        "Use the `status` attribute to split successes from failures."
    ),
    unit="1",
)

# Histogram: end-to-end task processing time, recorded at terminal status
# (success and failure paths both contribute). Wired into ``_publish_result``.
TASK_DURATION = _framework_meter.create_histogram(
    "heddle.task.duration",
    description=(
        "End-to-end task processing time in milliseconds, recorded for "
        "both successful and failed terminal statuses. Provides p50/p95/"
        "p99 latency by worker type without requiring trace sampling."
    ),
    unit="ms",
)

# Histogram: bus publish latency, in milliseconds. Wired into
# ``BaseActor.publish`` so every actor-originated publish contributes.
BUS_PUBLISH_LATENCY = _framework_meter.create_histogram(
    "heddle.bus.publish.latency",
    description=(
        "Time spent in the bus publish call, in milliseconds. Captures "
        "transport-layer latency separately from worker processing time."
    ),
    unit="ms",
)

# Counter: orchestrator goals received. Wired into both
# ``PipelineOrchestrator`` and ``OrchestratorActor`` message handlers.
ORCHESTRATOR_GOALS_RECEIVED = _framework_meter.create_counter(
    "heddle.orchestrator.goals.received",
    description=(
        "Count of OrchestratorGoals an orchestrator has accepted for "
        "processing. Use the ``orchestrator_name`` attribute to split "
        "per-orchestrator volume."
    ),
    unit="1",
)


def record_task_received(worker_type: str, model_tier: str, *, count: int = 1) -> None:
    """Record one (or more) task receptions at the worker.

    Called at the top of :meth:`heddle.worker.base.TaskWorker.handle_message`
    after the envelope is parsed but before any processing. Combined
    with :func:`record_task_completed`, yields per-worker in-flight
    depth (received minus completed).
    """
    TASKS_RECEIVED.add(
        count,
        {"worker_type": worker_type, "model_tier": model_tier},
    )


def record_task_completed(
    worker_type: str, model_tier: str, status: str, *, count: int = 1
) -> None:
    """Record one (or more) task completions.

    Centralised so call sites don't have to assemble the attribute
    dict and so future attribute-schema changes touch one place.

    Args:
        worker_type: The ``worker_type`` of the completed task.
        model_tier: The ``model_tier`` (``local``/``standard``/``frontier``).
        status: Terminal status — typically ``"completed"`` or ``"failed"``.
        count: Number of completions to record (default 1).
    """
    TASKS_COMPLETED.add(
        count,
        {
            "worker_type": worker_type,
            "model_tier": model_tier,
            "status": status,
        },
    )


def record_task_duration(
    worker_type: str, model_tier: str, status: str, elapsed_ms: int | float
) -> None:
    """Record one task's elapsed processing time.

    Recorded on both success and failure paths in
    :meth:`heddle.worker.base.TaskWorker._publish_result` so the
    histogram captures worst-case latency, not just happy-path latency.
    """
    TASK_DURATION.record(
        elapsed_ms,
        {
            "worker_type": worker_type,
            "model_tier": model_tier,
            "status": status,
        },
    )


def record_bus_publish_latency(subject: str, system: str, elapsed_ms: int | float) -> None:
    """Record one bus publish's elapsed time.

    Wired into :meth:`heddle.core.actor.BaseActor.publish`. The
    ``system`` attribute identifies the underlying bus transport (e.g.
    ``"nats"``, ``"in_memory"``); the ``subject`` follows OTel
    ``messaging.destination.name`` semantic convention.
    """
    BUS_PUBLISH_LATENCY.record(
        elapsed_ms,
        {
            "messaging.destination.name": subject,
            "messaging.system": system,
        },
    )


def record_orchestrator_goal_received(orchestrator_name: str, *, count: int = 1) -> None:
    """Record one (or more) goals received by an orchestrator.

    Wired into both :class:`heddle.orchestrator.pipeline.PipelineOrchestrator`
    and :class:`heddle.orchestrator.runner.OrchestratorActor` message
    handlers. ``orchestrator_name`` is typically the actor_id so
    operators can filter per-orchestrator volume.
    """
    ORCHESTRATOR_GOALS_RECEIVED.add(count, {"orchestrator_name": orchestrator_name})
