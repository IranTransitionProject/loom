"""End-to-end OTel propagation tests.

Verifies that the trace tree forms correctly across the
orchestrator → worker → result hop without requiring NATS or any
running infrastructure. Uses real OTel SDK components
(``TracerProvider`` + ``InMemorySpanExporter``) plus heddle's
``inject_trace_context`` / ``extract_trace_context`` against
``InMemoryBus``.

What this guards against:

- A refactor that breaks ``extract_trace_context`` at the worker
  entry (``actor.py:222``) and silently fragments traces.
- A refactor that drops the ``inject_trace_context`` call from
  ``TaskWorker._publish_result`` (``worker/base.py``) — the
  return-path symmetry added in 2026-05-15's OTel-audit S1 fix.

The previous tracing tests (``test_tracing.py``) exercise
``inject``/``extract`` against mocked carriers; they don't catch
either of the regressions above. This test does.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import yaml

# OTel SDK is in the ``otel`` extra; without it, none of this applies.
pytest.importorskip("opentelemetry.sdk")

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from heddle.core.envelope import wrap
from heddle.core.messages import ModelTier, TaskMessage
from heddle.tracing.otel import extract_trace_context, inject_trace_context
from heddle.worker.base import TaskWorker

# OTel's TracerProvider can only be set once per process — subsequent
# ``set_tracer_provider`` calls log a warning and are silently denied.
# Set it once at module load and clear the exporter between tests.
_EXPORTER = InMemorySpanExporter()
_PROVIDER = TracerProvider()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))
trace.set_tracer_provider(_PROVIDER)


@pytest.fixture
def span_exporter() -> InMemorySpanExporter:
    """Yield the module-shared in-memory exporter with spans cleared.

    Spans collected here form the assertion surface. Per-test ``clear()``
    keeps cross-test leakage from hiding a regression.
    """
    _EXPORTER.clear()
    return _EXPORTER


def _spans_by_name(exporter: InMemorySpanExporter) -> dict[str, object]:
    """Index the exported spans by name for readable assertions."""
    return {span.name: span for span in exporter.get_finished_spans()}


class TestRoundTripPropagation:
    """The load-bearing case: orchestrator span → worker span chains."""

    def test_worker_span_parents_under_orchestrator_span(
        self, span_exporter: InMemorySpanExporter
    ) -> None:
        """Outbound chain: extract at worker entry sets worker.parent = orchestrator."""
        tracer = trace.get_tracer("test")

        task_dict: dict = {"task_id": "t1", "payload": {}}

        # Orchestrator side: start dispatch span, inject context.
        with tracer.start_as_current_span("orchestrator.dispatch") as orch_span:
            inject_trace_context(task_dict)
            orchestrator_trace_id = orch_span.get_span_context().trace_id
            orchestrator_span_id = orch_span.get_span_context().span_id

        # Wire hop: task_dict crosses the bus carrying _trace_context.
        assert "_trace_context" in task_dict, "outbound inject must populate _trace_context"

        # Worker side: extract context, start worker span with it as parent.
        ctx = extract_trace_context(task_dict)
        with tracer.start_as_current_span("worker.process", context=ctx) as worker_span:
            worker_trace_id = worker_span.get_span_context().trace_id
            worker_parent_span_id = worker_span.parent.span_id if worker_span.parent else None

        # Same trace, worker parented under orchestrator.
        assert worker_trace_id == orchestrator_trace_id, (
            "worker span must share trace_id with orchestrator"
        )
        assert worker_parent_span_id == orchestrator_span_id, (
            "worker span's parent_span_id must equal orchestrator span's span_id"
        )

    def test_return_path_inject_carries_worker_context(
        self, span_exporter: InMemorySpanExporter
    ) -> None:
        """OTel-audit S1: result publish injects worker context so a future
        consumer-side span could parent under the worker.

        Guards against the S1 regression: if ``_publish_result`` ever drops
        the ``inject_trace_context`` call, this test fails.
        """
        tracer = trace.get_tracer("test")

        task_dict: dict = {"task_id": "t2", "payload": {}}
        result_dict: dict = {"task_id": "t2", "status": "completed"}

        with tracer.start_as_current_span("orchestrator.dispatch"):
            inject_trace_context(task_dict)

        ctx = extract_trace_context(task_dict)
        with tracer.start_as_current_span("worker.process", context=ctx) as worker_span:
            worker_trace_id = worker_span.get_span_context().trace_id
            worker_span_id = worker_span.get_span_context().span_id
            # Mirrors TaskWorker._publish_result's behaviour after the
            # 2026-05-15 OTel S1 fix.
            inject_trace_context(result_dict)

        # The result now carries the worker's context.
        assert "_trace_context" in result_dict, "return-path inject must populate _trace_context"

        # A hypothetical consumer-side span on the result should parent under the worker.
        consumer_ctx = extract_trace_context(result_dict)
        with tracer.start_as_current_span("result.received", context=consumer_ctx) as consumer_span:
            consumer_trace_id = consumer_span.get_span_context().trace_id
            consumer_parent_span_id = consumer_span.parent.span_id if consumer_span.parent else None

        assert consumer_trace_id == worker_trace_id, (
            "consumer-side span must share trace_id with worker span"
        )
        assert consumer_parent_span_id == worker_span_id, (
            "consumer-side span's parent_span_id must equal worker span's span_id "
            "(this is what the return-path inject enables)"
        )

    def test_full_tree_exports_three_linked_spans(
        self, span_exporter: InMemorySpanExporter
    ) -> None:
        """Sanity check: the exporter sees orchestrator + worker + consumer
        spans, all sharing a single trace_id and forming a parent-child chain.
        """
        tracer = trace.get_tracer("test")
        task_dict: dict = {"task_id": "t3", "payload": {}}
        result_dict: dict = {"task_id": "t3", "status": "completed"}

        with tracer.start_as_current_span("orchestrator.dispatch"):
            inject_trace_context(task_dict)

        ctx = extract_trace_context(task_dict)
        with tracer.start_as_current_span("worker.process", context=ctx):
            inject_trace_context(result_dict)

        consumer_ctx = extract_trace_context(result_dict)
        with tracer.start_as_current_span("result.received", context=consumer_ctx):
            pass

        spans = _spans_by_name(span_exporter)
        assert {"orchestrator.dispatch", "worker.process", "result.received"} <= spans.keys()

        trace_ids = {span.get_span_context().trace_id for span in spans.values()}
        assert len(trace_ids) == 1, f"expected one trace_id across all spans, got {trace_ids}"


# Minimal echo-worker config exercising input/output schemas.
_ECHO_CONFIG = {
    "name": "echo_worker",
    "input_schema": {
        "type": "object",
        "required": ["text"],
        "properties": {"text": {"type": "string"}},
    },
    "output_schema": {
        "type": "object",
        "required": ["echo"],
        "properties": {"echo": {"type": "string"}},
    },
}


class _EchoWorker(TaskWorker):
    """Concrete TaskWorker used to verify _publish_result wiring."""

    async def process(self, payload, metadata):
        return {
            "output": {"echo": payload.get("text", "")},
            "model_used": "echo-v1",
            "token_usage": {},
        }


class TestPublishResultRealWorker:
    """Guards the actual S1 fix in ``worker/base.py:_publish_result``.

    The ``TestRoundTripPropagation`` cases above test the inject/extract
    API contract. This class exercises the *real* ``TaskWorker``
    method so a refactor that drops the inject from
    ``_publish_result`` is caught here even if the inject/extract
    contract is unchanged.
    """

    @pytest.mark.asyncio
    async def test_publish_result_injects_trace_context_when_span_active(
        self, span_exporter: InMemorySpanExporter, tmp_path
    ) -> None:
        config_file = tmp_path / "echo.yaml"
        config_file.write_text(yaml.dump(_ECHO_CONFIG))

        worker = _EchoWorker("test-worker", str(config_file))
        worker.publish = AsyncMock()

        tracer = trace.get_tracer("test")
        task_dict = wrap(
            "core.TaskMessage",
            TaskMessage(
                worker_type="echo_worker",
                input={"text": "hello"},
                model_tier=ModelTier.LOCAL,
                parent_task_id="goal-trace",
            ),
        ).model_dump(mode="json")

        # Active span at the time handle_message runs is what
        # _publish_result's inject_trace_context call captures.
        with tracer.start_as_current_span("orchestrator.dispatch"):
            await worker.handle_message(task_dict)

        worker.publish.assert_called_once()
        result_dict = worker.publish.call_args[0][1]
        assert "_trace_context" in result_dict, (
            "S1 regression: TaskWorker._publish_result must call inject_trace_context "
            "on the result before publishing — see worker/base.py and OTEL_AUDIT_2026-05-15.md S1"
        )
        # And the injected headers should be a non-empty dict carrying
        # a traceparent — bare presence isn't enough; sanity-check shape.
        trace_ctx = result_dict["_trace_context"]
        assert isinstance(trace_ctx, dict), "_trace_context must be a dict"
        assert trace_ctx, "_trace_context must be a non-empty dict of W3C propagation headers"
