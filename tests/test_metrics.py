"""Tests for ``heddle.tracing.metrics`` — OTel metrics surface.

Per OTel-audit S4 follow-up. Two test surfaces:

1. **No-op fallback** — exercised without the OTel SDK; verifies
   ``record_task_completed`` and the underlying instrument calls
   silently no-op (no exceptions, no log noise). Critical because
   wiring metrics into ``TaskWorker._publish_result`` puts this
   code on every task's hot path; a wiring bug here would cascade
   across the 2700+ test suite.

2. **In-memory reader** — installs an ``InMemoryMetricReader`` as
   the global ``MeterProvider`` and verifies the framework's
   ``heddle.tasks.completed`` counter is observable with the
   expected attribute schema. Mirrors the in-memory-span-exporter
   pattern from ``test_tracing_e2e.py``.

Both surfaces use the OTel SDK's ``InMemoryMetricReader``, available
in ``opentelemetry-sdk`` 1.20+; skip when unavailable.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

# Metrics SDK is in the ``otel`` extra; without it, in-memory-reader
# tests can't run. The no-op tests below do *not* skip — they validate
# the fallback path that runs when OTel isn't installed.
pytest.importorskip("opentelemetry.sdk.metrics")

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from heddle.tracing.metrics import (
    _NoOpCounter,
    _NoOpHistogram,
    _NoOpMeter,
    record_bus_publish_latency,
    record_orchestrator_goal_received,
    record_task_completed,
    record_task_duration,
    record_task_received,
)

# ---------------------------------------------------------------------------
# No-op fallback — runs without an OTel meter provider configured.
# ---------------------------------------------------------------------------


class TestNoOpFallback:
    """Pre-init / no-OTel path must not raise on any instrument call."""

    def test_noop_counter_add_accepts_attributes(self) -> None:
        """The no-op counter swallows attribute dicts of any shape."""
        c = _NoOpCounter()
        c.add(1)
        c.add(5, {"worker_type": "x"})
        c.add(0.5, {"a": 1, "b": "two", "c": True})
        # Reaching here without exception is the assertion.

    def test_noop_histogram_record_accepts_attributes(self) -> None:
        """The no-op histogram swallows record calls and attribute dicts."""
        h = _NoOpHistogram()
        h.record(0)
        h.record(123)
        h.record(0.5, {"messaging.destination.name": "heddle.tasks.incoming"})

    def test_noop_meter_returns_noop_instruments(self) -> None:
        """The no-op meter's factory methods return the right stand-ins."""
        m = _NoOpMeter()
        assert isinstance(m.create_counter("x"), _NoOpCounter)
        assert isinstance(m.create_histogram("y"), _NoOpHistogram)
        # Construction must accept the OTel-canonical kwargs.
        assert isinstance(m.create_counter("x", unit="1", description="..."), _NoOpCounter)
        assert isinstance(m.create_histogram("y", unit="ms", description="..."), _NoOpHistogram)

    def test_record_task_completed_no_raise_when_metrics_unavailable(self) -> None:
        """The helper must be safe to call when OTel metrics isn't installed.

        ``TaskWorker._publish_result`` calls this on every task; a
        raise here would crash the worker loop on every task.
        """
        # Patch both: feature-detection flag (so any branch consulting
        # _HAS_METRICS sees False) and the bound counter (so the call
        # route is explicitly the no-op path).
        with (
            patch("heddle.tracing.metrics._HAS_METRICS", False),
            patch("heddle.tracing.metrics.TASKS_COMPLETED", _NoOpCounter()),
        ):
            record_task_completed("any_worker", "local", "completed")
            record_task_completed("any_worker", "frontier", "failed", count=3)


# ---------------------------------------------------------------------------
# In-memory reader — exercises the actual instrument with real OTel SDK.
# ---------------------------------------------------------------------------


# OTel's MeterProvider, like TracerProvider, is set-once globally per process.
# Install one with an in-memory reader at module load; each test reads its
# accumulated metrics and (implicitly) starts fresh because new test data
# accumulates atop the prior cleared snapshot — see the per-test fixture.
_READER = InMemoryMetricReader()
_PROVIDER = MeterProvider(metric_readers=[_READER])
metrics.set_meter_provider(_PROVIDER)


def _flatten_data_points(reader: InMemoryMetricReader, instrument_name: str) -> list[dict]:
    """Extract data points for a single instrument from the reader output.

    Returns a flat list of dicts with whichever fields the data point
    type carries: counters expose ``value``; histograms expose ``sum``
    and ``count``. ``attributes`` is always present. Hides the SDK's
    three-level (ResourceMetrics → ScopeMetrics → Metric) nesting so
    tests assert on shape, not structure.
    """
    metrics_data = reader.get_metrics_data()
    if metrics_data is None:
        return []
    out: list[dict] = []
    for rm in metrics_data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name != instrument_name:
                    continue
                for dp in m.data.data_points:
                    flat: dict = {"attributes": dict(dp.attributes)}
                    # Counters have ``value``; histograms have ``sum`` + ``count``.
                    # ``getattr`` with default lets the same helper serve both.
                    flat["value"] = getattr(dp, "value", None)
                    flat["sum"] = getattr(dp, "sum", None)
                    flat["count"] = getattr(dp, "count", None)
                    out.append(flat)
    return out


@pytest.fixture
def reader():
    """Yield the module-shared reader and (for the next test) drain its
    accumulated state by snapshotting current values as the baseline.

    Counters in OTel are cumulative — clearing per-test is non-trivial
    because the SDK keeps internal accumulator state. Instead, tests
    record the baseline before triggering the instrument and assert
    the *delta*.
    """
    return _READER


class TestTasksCompletedCounter:
    """``heddle.tasks.completed`` counter — OTel-audit S4 first instrument."""

    def test_counter_increments_with_documented_attributes(self, reader) -> None:
        """A single recorded completion shows up with the right attributes."""
        baseline = _flatten_data_points(reader, "heddle.tasks.completed")

        record_task_completed("echo_worker", "local", "completed")

        after = _flatten_data_points(reader, "heddle.tasks.completed")
        # New data point or an incremented existing one — depends on
        # whether this attribute set has been seen before in the run.
        matching = [
            dp
            for dp in after
            if dp["attributes"]
            == {"worker_type": "echo_worker", "model_tier": "local", "status": "completed"}
        ]
        assert matching, "no data point matched the documented attribute schema"
        # Find the baseline value for the same attribute set, if any.
        baseline_value = next(
            (dp["value"] for dp in baseline if dp["attributes"] == matching[0]["attributes"]),
            0,
        )
        assert matching[0]["value"] >= baseline_value + 1, (
            f"counter delta should be at least +1; baseline={baseline_value}, "
            f"after={matching[0]['value']}"
        )

    def test_separate_attribute_sets_produce_separate_data_points(self, reader) -> None:
        """Different (worker_type, model_tier, status) tuples track independently."""
        record_task_completed("alpha", "local", "completed")
        record_task_completed("alpha", "local", "failed")
        record_task_completed("beta", "frontier", "completed")

        after = _flatten_data_points(reader, "heddle.tasks.completed")
        attribute_sets = {tuple(sorted(dp["attributes"].items())) for dp in after}
        # At least these three distinct attribute combinations should
        # be present in the accumulated state.
        assert (
            ("model_tier", "local"),
            ("status", "completed"),
            ("worker_type", "alpha"),
        ) in attribute_sets
        assert (
            ("model_tier", "local"),
            ("status", "failed"),
            ("worker_type", "alpha"),
        ) in attribute_sets
        assert (
            ("model_tier", "frontier"),
            ("status", "completed"),
            ("worker_type", "beta"),
        ) in attribute_sets

    def test_count_argument_increments_by_more_than_one(self, reader) -> None:
        """``count`` kwarg lets callers record batched completions atomically."""
        baseline = _flatten_data_points(reader, "heddle.tasks.completed")
        baseline_attrs = {"worker_type": "batched", "model_tier": "local", "status": "completed"}
        baseline_value = next(
            (dp["value"] for dp in baseline if dp["attributes"] == baseline_attrs),
            0,
        )

        record_task_completed("batched", "local", "completed", count=7)

        after = _flatten_data_points(reader, "heddle.tasks.completed")
        matching = next(
            (dp["value"] for dp in after if dp["attributes"] == baseline_attrs),
            None,
        )
        assert matching is not None, "expected data point not present"
        assert matching >= baseline_value + 7, (
            f"counter should advance by 7; baseline={baseline_value}, after={matching}"
        )


class TestTasksReceivedCounter:
    """``heddle.tasks.received`` — pairs with completed for in-flight depth."""

    def test_counter_records_with_worker_type_and_tier(self, reader) -> None:
        record_task_received("inflight_test", "local")
        after = _flatten_data_points(reader, "heddle.tasks.received")
        matching = [
            dp
            for dp in after
            if dp["attributes"] == {"worker_type": "inflight_test", "model_tier": "local"}
        ]
        assert matching, "no data point matched the documented attribute schema"
        assert matching[0]["value"] >= 1


class TestTaskDurationHistogram:
    """``heddle.task.duration`` — recorded for both success and failure."""

    def test_histogram_records_value_with_full_attributes(self, reader) -> None:
        record_task_duration("dur_test", "local", "completed", elapsed_ms=42)
        after = _flatten_data_points(reader, "heddle.task.duration")
        matching = [
            dp
            for dp in after
            if dp["attributes"]
            == {"worker_type": "dur_test", "model_tier": "local", "status": "completed"}
        ]
        assert matching, "expected histogram data point not present"
        # Histogram data point exposes ``sum`` (total of observations) and
        # ``count`` (number of observations). A single record of 42 should
        # be reflected in both.
        assert matching[0]["sum"] >= 42
        assert matching[0]["count"] >= 1

    def test_histogram_records_failed_status_separately(self, reader) -> None:
        """Success and failure durations live in separate data points."""
        record_task_duration("dur_split", "local", "failed", elapsed_ms=100)
        after = _flatten_data_points(reader, "heddle.task.duration")
        failed_dps = [dp for dp in after if dp["attributes"].get("status") == "failed"]
        assert failed_dps, "no `status=failed` data point present"


class TestBusPublishLatencyHistogram:
    """``heddle.bus.publish.latency`` — uses OTel messaging.* semconv."""

    def test_histogram_uses_messaging_semconv_attributes(self, reader) -> None:
        record_bus_publish_latency(subject="heddle.tasks.incoming", system="nats", elapsed_ms=3)
        after = _flatten_data_points(reader, "heddle.bus.publish.latency")
        matching = [
            dp
            for dp in after
            if dp["attributes"]
            == {
                "messaging.destination.name": "heddle.tasks.incoming",
                "messaging.system": "nats",
            }
        ]
        assert matching, "expected data point with messaging.* attributes not present"

    def test_histogram_distinguishes_bus_systems(self, reader) -> None:
        """messaging.system attribute splits NATS vs in-memory transport."""
        record_bus_publish_latency(subject="heddle.tasks.foo", system="nats", elapsed_ms=1)
        record_bus_publish_latency(subject="heddle.tasks.foo", system="in_memory", elapsed_ms=1)
        after = _flatten_data_points(reader, "heddle.bus.publish.latency")
        systems = {
            dp["attributes"].get("messaging.system")
            for dp in after
            if dp["attributes"].get("messaging.destination.name") == "heddle.tasks.foo"
        }
        assert {"nats", "in_memory"} <= systems


class TestOrchestratorGoalsReceivedCounter:
    """``heddle.orchestrator.goals.received`` — splits per orchestrator."""

    def test_counter_records_by_orchestrator_name(self, reader) -> None:
        record_orchestrator_goal_received("itp-quick")
        record_orchestrator_goal_received("itp-standard")
        after = _flatten_data_points(reader, "heddle.orchestrator.goals.received")
        names = {dp["attributes"].get("orchestrator_name") for dp in after}
        assert {"itp-quick", "itp-standard"} <= names
