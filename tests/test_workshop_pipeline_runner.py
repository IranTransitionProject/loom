"""Tests for PipelineTestRunner (workshop/pipeline_runner.py).

Each test wires a minimal pipeline + worker configs through
``worker_config_overrides`` so nothing touches the on-disk configs/
tree. Mock backends keyed by worker name produce deterministic JSON
outputs; the runner exercises the real PipelineOrchestrator code
path (input mappings, conditional stages, dependency inference,
parallelism, retry, output schema validation).
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

import pytest
import yaml

from heddle.worker.backends import LLMBackend
from heddle.workshop.pipeline_runner import (
    PipelineTestResult,
    PipelineTestRunner,
)

# ---------------------------------------------------------------------------
# Mock backends and helpers
# ---------------------------------------------------------------------------


class RoutingBackend(LLMBackend):
    """Mock backend that picks a JSON response by matching the worker name in the system prompt.

    Each worker's system_prompt contains a marker like ``[worker=extract]``;
    the backend reads that, looks up the configured response, and returns
    it. Sentinel ``__raise__`` lets a worker simulate a backend failure
    (causes the worker to publish a FAILED TaskResult through the normal
    handle_message exception path).
    """

    def __init__(self, responses: dict[str, Any]):
        self.responses = responses

    async def complete(self, system_prompt, user_message, max_tokens=2000, temperature=0.0, **kw):
        worker = _extract_marker(system_prompt)
        resp = self.responses.get(worker)
        if resp == "__raise__":
            raise RuntimeError(f"Backend failure for {worker}")
        if resp is None:
            resp = {}
        return {
            "content": json.dumps(resp),
            "model": "mock-model",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "tool_calls": None,
            "stop_reason": "end_turn",
        }


class CountingBackend(LLMBackend):
    """Fail the first N calls per worker, then succeed."""

    def __init__(self, fail_per_worker: dict[str, int], success_responses: dict[str, Any]):
        self.remaining = dict(fail_per_worker)
        self.success = success_responses
        self.calls: dict[str, int] = {}

    async def complete(self, system_prompt, user_message, max_tokens=2000, temperature=0.0, **kw):
        worker = _extract_marker(system_prompt)
        self.calls[worker] = self.calls.get(worker, 0) + 1
        if self.remaining.get(worker, 0) > 0:
            self.remaining[worker] -= 1
            raise RuntimeError(f"Transient failure for {worker}")
        return {
            "content": json.dumps(self.success.get(worker, {})),
            "model": "mock-model",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "tool_calls": None,
            "stop_reason": "end_turn",
        }


def _extract_marker(system_prompt: str) -> str:
    """Return the worker name marker embedded in a test system prompt."""
    import re

    m = re.search(r"\[worker=([^\]]+)\]", system_prompt or "")
    return m.group(1) if m else ""


def _worker_cfg(
    name: str,
    *,
    input_schema: dict | None = None,
    output_schema: dict | None = None,
    cls: str | None = None,
) -> dict[str, Any]:
    """Build a minimal valid worker config dict."""
    cfg = {
        "name": name,
        "description": f"Test worker {name}",
        "system_prompt": f"[worker={name}] Return JSON.",
        "default_model_tier": "local",
        "input_schema": input_schema if input_schema is not None else {"type": "object"},
        "output_schema": output_schema if output_schema is not None else {"type": "object"},
    }
    if cls is not None:
        cfg["class"] = cls
    return cfg


def _pipeline_cfg(name: str, stages: list[dict[str, Any]], **kw) -> dict[str, Any]:
    """Build a minimal valid pipeline config dict."""
    return {"name": name, "timeout_seconds": 5, "pipeline_stages": stages, **kw}


def _write_yaml_tempfile(data: dict) -> str:
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as f:
        yaml.safe_dump(data, f)
    return path


@pytest.fixture
def workers_dir(tmp_path):
    return str(tmp_path / "workers-empty")  # Intentionally non-existent; overrides used.


# ---------------------------------------------------------------------------
# 1. Linear pipeline (A → B → C)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_linear_pipeline_all_stages_succeed(workers_dir):
    """A → B → C: stage_results ordered, final_output carries merged context."""
    pipeline_cfg = _pipeline_cfg(
        "linear",
        [
            {"name": "A", "worker_type": "a_worker", "input_mapping": {"q": "goal.context.q"}},
            {"name": "B", "worker_type": "b_worker", "input_mapping": {"v": "A.output.v"}},
            {"name": "C", "worker_type": "c_worker", "input_mapping": {"v": "B.output.v"}},
        ],
    )
    pipeline_path = _write_yaml_tempfile(pipeline_cfg)

    overrides = {
        "a_worker": _worker_cfg("a_worker"),
        "b_worker": _worker_cfg("b_worker"),
        "c_worker": _worker_cfg("c_worker"),
    }
    backend = RoutingBackend(
        {
            "a_worker": {"v": 1},
            "b_worker": {"v": 2},
            "c_worker": {"v": 3, "final": True},
        }
    )

    runner = PipelineTestRunner(
        backends={"local": backend},
        workers_dir=workers_dir,
        worker_config_overrides=overrides,
    )
    try:
        result = await runner.run(pipeline_path, context={"q": "hi"})
    finally:
        await runner.aclose()
        os.unlink(pipeline_path)

    assert isinstance(result, PipelineTestResult)
    assert result.success is True
    assert result.error is None
    names = [s.stage_name for s in result.stage_results]
    assert names == ["A", "B", "C"]
    assert all(s.status == "completed" for s in result.stage_results)
    assert result.final_output is not None
    assert result.final_output["C"] == {"v": 3, "final": True}


# ---------------------------------------------------------------------------
# 2. Diamond pipeline (A → {B, C} → D): parallel level + join
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diamond_pipeline_parallel_middle_joined_by_d(workers_dir):
    pipeline_cfg = _pipeline_cfg(
        "diamond",
        [
            {"name": "A", "worker_type": "a_worker", "input_mapping": {"q": "goal.context.q"}},
            {"name": "B", "worker_type": "b_worker", "input_mapping": {"v": "A.output.v"}},
            {"name": "C", "worker_type": "c_worker", "input_mapping": {"v": "A.output.v"}},
            {
                "name": "D",
                "worker_type": "d_worker",
                "input_mapping": {"b": "B.output.v", "c": "C.output.v"},
            },
        ],
    )
    pipeline_path = _write_yaml_tempfile(pipeline_cfg)

    overrides = {
        "a_worker": _worker_cfg("a_worker"),
        "b_worker": _worker_cfg("b_worker"),
        "c_worker": _worker_cfg("c_worker"),
        "d_worker": _worker_cfg("d_worker"),
    }
    backend = RoutingBackend(
        {
            "a_worker": {"v": 10},
            "b_worker": {"v": 20},
            "c_worker": {"v": 30},
            "d_worker": {"sum": 50},
        }
    )

    runner = PipelineTestRunner(
        backends={"local": backend},
        workers_dir=workers_dir,
        worker_config_overrides=overrides,
    )
    try:
        result = await runner.run(pipeline_path, context={"q": "hi"})
    finally:
        await runner.aclose()
        os.unlink(pipeline_path)

    assert result.success is True
    # All four stages completed.
    statuses = {s.stage_name: s.status for s in result.stage_results}
    assert statuses == {"A": "completed", "B": "completed", "C": "completed", "D": "completed"}
    assert result.final_output["D"] == {"sum": 50}


# ---------------------------------------------------------------------------
# 3. Conditional stage (condition false → skipped)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conditional_stage_skipped_when_condition_false(workers_dir):
    """Stage B's condition is false → skipped; C still runs (depends only on A)."""
    pipeline_cfg = _pipeline_cfg(
        "conditional",
        [
            {"name": "A", "worker_type": "a_worker", "input_mapping": {"q": "goal.context.q"}},
            {
                "name": "B",
                "worker_type": "b_worker",
                "input_mapping": {"v": "A.output.v"},
                "condition": "A.output.run_b == true",
            },
            {"name": "C", "worker_type": "c_worker", "input_mapping": {"v": "A.output.v"}},
        ],
    )
    pipeline_path = _write_yaml_tempfile(pipeline_cfg)

    overrides = {
        "a_worker": _worker_cfg("a_worker"),
        "b_worker": _worker_cfg("b_worker"),
        "c_worker": _worker_cfg("c_worker"),
    }
    backend = RoutingBackend(
        {
            "a_worker": {"v": 1, "run_b": False},
            "b_worker": {"v": 99},  # Should never be called.
            "c_worker": {"v": 3},
        }
    )

    runner = PipelineTestRunner(
        backends={"local": backend},
        workers_dir=workers_dir,
        worker_config_overrides=overrides,
    )
    try:
        result = await runner.run(pipeline_path, context={"q": "hi"})
    finally:
        await runner.aclose()
        os.unlink(pipeline_path)

    assert result.success is True
    by_name = {s.stage_name: s for s in result.stage_results}
    assert by_name["A"].status == "completed"
    assert by_name["B"].status == "skipped"
    assert by_name["C"].status == "completed"


# ---------------------------------------------------------------------------
# 4. Mid-pipeline failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_midpipeline_failure_marks_downstream_upstream_failure(workers_dir):
    """Stage B fails → A completed, B failed, C upstream-failure."""
    pipeline_cfg = _pipeline_cfg(
        "fails-mid",
        [
            {"name": "A", "worker_type": "a_worker", "input_mapping": {"q": "goal.context.q"}},
            {"name": "B", "worker_type": "b_worker", "input_mapping": {"v": "A.output.v"}},
            {"name": "C", "worker_type": "c_worker", "input_mapping": {"v": "B.output.v"}},
        ],
    )
    pipeline_path = _write_yaml_tempfile(pipeline_cfg)

    overrides = {
        "a_worker": _worker_cfg("a_worker"),
        "b_worker": _worker_cfg("b_worker"),
        "c_worker": _worker_cfg("c_worker"),
    }
    backend = RoutingBackend(
        {
            "a_worker": {"v": 1},
            "b_worker": "__raise__",
            "c_worker": {"v": 3},
        }
    )

    runner = PipelineTestRunner(
        backends={"local": backend},
        workers_dir=workers_dir,
        worker_config_overrides=overrides,
    )
    try:
        result = await runner.run(pipeline_path, context={"q": "hi"})
    finally:
        await runner.aclose()
        os.unlink(pipeline_path)

    assert result.success is False
    by_name = {s.stage_name: s for s in result.stage_results}
    assert by_name["A"].status == "completed"
    assert by_name["B"].status == "failed"
    assert by_name["B"].error is not None
    assert by_name["C"].status == "upstream-failure"


# ---------------------------------------------------------------------------
# 5. Per-stage retry honored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_stage_retry_succeeds_after_transient_failures(workers_dir):
    """Backend fails twice then succeeds: stage marks completed."""
    pipeline_cfg = _pipeline_cfg(
        "retry",
        [
            {
                "name": "A",
                "worker_type": "a_worker",
                "input_mapping": {"q": "goal.context.q"},
                "max_retries": 2,
            }
        ],
    )
    pipeline_path = _write_yaml_tempfile(pipeline_cfg)
    overrides = {"a_worker": _worker_cfg("a_worker")}
    backend = CountingBackend(
        fail_per_worker={"a_worker": 2},
        success_responses={"a_worker": {"v": 42}},
    )

    runner = PipelineTestRunner(
        backends={"local": backend},
        workers_dir=workers_dir,
        worker_config_overrides=overrides,
    )
    try:
        result = await runner.run(pipeline_path, context={"q": "hi"})
    finally:
        await runner.aclose()
        os.unlink(pipeline_path)

    assert result.success is True
    assert len(result.stage_results) == 1
    assert result.stage_results[0].status == "completed"
    assert result.stage_results[0].output == {"v": 42}
    assert backend.calls["a_worker"] == 3  # 2 failures + 1 success


# ---------------------------------------------------------------------------
# 6. Input mapping resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_input_mapping_propagates_field_from_upstream_stage(workers_dir):
    """A → B: B's input_mapping renames A.output.text to B.input.phrase."""
    pipeline_cfg = _pipeline_cfg(
        "mapping",
        [
            {"name": "A", "worker_type": "a_worker", "input_mapping": {"q": "goal.context.q"}},
            {
                "name": "B",
                "worker_type": "b_worker",
                "input_schema": {
                    "type": "object",
                    "required": ["phrase"],
                    "properties": {"phrase": {"type": "string"}},
                },
                "input_mapping": {"phrase": "A.output.text"},
            },
        ],
    )
    pipeline_path = _write_yaml_tempfile(pipeline_cfg)
    overrides = {
        "a_worker": _worker_cfg("a_worker"),
        "b_worker": _worker_cfg("b_worker"),
    }
    backend = RoutingBackend(
        {
            "a_worker": {"text": "hi"},
            # B echoes back what it received so we can assert.
            "b_worker": {"echo": "ok"},
        }
    )

    runner = PipelineTestRunner(
        backends={"local": backend},
        workers_dir=workers_dir,
        worker_config_overrides=overrides,
    )
    try:
        result = await runner.run(pipeline_path, context={"q": "ignored"})
    finally:
        await runner.aclose()
        os.unlink(pipeline_path)

    # If the mapping failed, B's input would not have validated against
    # required=['phrase'] and B would have failed.  Success here is the
    # assertion.
    assert result.success is True, result.error
    by_name = {s.stage_name: s for s in result.stage_results}
    assert by_name["B"].status == "completed"


# ---------------------------------------------------------------------------
# 7. Unknown worker_type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_worker_type_fails_fast(tmp_path):
    """Pipeline references a worker absent from workers_dir and overrides → clear error."""
    pipeline_cfg = _pipeline_cfg(
        "unknown",
        [
            {
                "name": "A",
                "worker_type": "missing_worker",
                "input_mapping": {"q": "goal.context.q"},
            }
        ],
    )
    pipeline_path = _write_yaml_tempfile(pipeline_cfg)

    runner = PipelineTestRunner(
        backends={"local": RoutingBackend({})},
        workers_dir=str(tmp_path),  # empty dir
    )
    try:
        result = await runner.run(pipeline_path, context={"q": "hi"})
    finally:
        await runner.aclose()
        os.unlink(pipeline_path)

    assert result.success is False
    assert result.error is not None
    assert "missing_worker" in result.error
    # No partial stage results — we bailed before spawning anything.
    assert result.stage_results == []


# ---------------------------------------------------------------------------
# 8. Inter-stage schema validation failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interstage_schema_validation_failure_marks_stage_failed(workers_dir):
    """A produces output that violates B's declared input schema → B failed."""
    pipeline_cfg = _pipeline_cfg(
        "schema-mismatch",
        [
            {"name": "A", "worker_type": "a_worker", "input_mapping": {"q": "goal.context.q"}},
            {
                "name": "B",
                "worker_type": "b_worker",
                # B requires an int but A emits a string.
                "input_schema": {
                    "type": "object",
                    "required": ["n"],
                    "properties": {"n": {"type": "integer"}},
                },
                "input_mapping": {"n": "A.output.n"},
            },
        ],
    )
    pipeline_path = _write_yaml_tempfile(pipeline_cfg)

    overrides = {
        "a_worker": _worker_cfg("a_worker"),
        "b_worker": _worker_cfg("b_worker"),
    }
    backend = RoutingBackend(
        {
            "a_worker": {"n": "not-an-int"},
            "b_worker": {"ok": True},
        }
    )

    runner = PipelineTestRunner(
        backends={"local": backend},
        workers_dir=workers_dir,
        worker_config_overrides=overrides,
    )
    try:
        result = await runner.run(pipeline_path, context={"q": "hi"})
    finally:
        await runner.aclose()
        os.unlink(pipeline_path)

    assert result.success is False
    by_name = {s.stage_name: s for s in result.stage_results}
    assert by_name["A"].status == "completed"
    assert by_name["B"].status == "failed"
    assert by_name["B"].error is not None
    assert "B" in by_name["B"].error or "validation" in by_name["B"].error.lower()


# ---------------------------------------------------------------------------
# 9. Mixed TaskWorker + TypedTaskWorker pipeline
# ---------------------------------------------------------------------------

# A concrete TypedTaskWorker for the test. Defined at module scope so
# the runner's importlib lookup ('module:Class') can find it.
from pydantic import BaseModel  # noqa: E402

from heddle.worker.typed import TypedTaskWorker  # noqa: E402


class _DoublePayload(BaseModel):
    n: int


class _DoubleOutput(BaseModel):
    doubled: int


class DoubleTypedWorker(TypedTaskWorker[_DoublePayload, _DoubleOutput]):
    payload_model = _DoublePayload
    output_model = _DoubleOutput

    async def handle(self, payload, metadata):
        return _DoubleOutput(doubled=payload.n * 2)


@pytest.mark.asyncio
async def test_mixed_taskworker_and_typedtaskworker_pipeline(workers_dir):
    """Pipeline with one LLM (TaskWorker) and one TypedTaskWorker; both produce expected output."""
    pipeline_cfg = _pipeline_cfg(
        "mixed",
        [
            {
                "name": "Extract",
                "worker_type": "a_worker",
                "input_mapping": {"q": "goal.context.q"},
            },
            {
                "name": "Double",
                "worker_type": "doubler",
                "input_mapping": {"n": "Extract.output.n"},
            },
        ],
    )
    pipeline_path = _write_yaml_tempfile(pipeline_cfg)

    overrides = {
        "a_worker": _worker_cfg("a_worker"),
        # Typed worker config with the class reference.
        "doubler": _worker_cfg(
            "doubler",
            input_schema={
                "type": "object",
                "required": ["n"],
                "properties": {"n": {"type": "integer"}},
            },
            output_schema={
                "type": "object",
                "required": ["doubled"],
                "properties": {"doubled": {"type": "integer"}},
            },
            cls=f"{__name__}:DoubleTypedWorker",
        ),
    }
    backend = RoutingBackend({"a_worker": {"n": 21}})

    runner = PipelineTestRunner(
        backends={"local": backend},
        workers_dir=workers_dir,
        worker_config_overrides=overrides,
    )
    try:
        result = await runner.run(pipeline_path, context={"q": "go"})
    finally:
        await runner.aclose()
        os.unlink(pipeline_path)

    assert result.success is True, result.error
    by_name = {s.stage_name: s for s in result.stage_results}
    assert by_name["Extract"].status == "completed"
    assert by_name["Double"].status == "completed"
    assert by_name["Double"].output == {"doubled": 42}


# ---------------------------------------------------------------------------
# 10. aclose() cleanup
# ---------------------------------------------------------------------------


class _ClosableBackend(LLMBackend):
    """Records aclose() calls."""

    def __init__(self):
        self.closed = False

    async def complete(self, *a, **kw):
        return {"content": "{}", "model": "x", "prompt_tokens": 0, "completion_tokens": 0}

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_aclose_closes_bus_and_backends(workers_dir):
    """After aclose(), backends are closed and a subsequent run() raises."""
    backend = _ClosableBackend()
    runner = PipelineTestRunner(
        backends={"local": backend},
        workers_dir=workers_dir,
        worker_config_overrides={"a_worker": _worker_cfg("a_worker")},
    )

    # Touch the bus so aclose has something to close.
    await runner._ensure_bus()
    assert runner._bus is not None

    await runner.aclose()

    assert backend.closed is True
    assert runner._bus is None

    # A second aclose is a no-op (idempotent).
    await runner.aclose()

    # Subsequent run() rejects.
    pipeline_cfg = _pipeline_cfg(
        "post-close",
        [{"name": "A", "worker_type": "a_worker", "input_mapping": {"q": "goal.context.q"}}],
    )
    pipeline_path = _write_yaml_tempfile(pipeline_cfg)
    try:
        with pytest.raises(RuntimeError, match="closed"):
            await runner.run(pipeline_path, context={"q": "hi"})
    finally:
        os.unlink(pipeline_path)


# ---------------------------------------------------------------------------
# Edge cases — error paths and helpers
# ---------------------------------------------------------------------------


def test_pipeline_test_result_succeeded_stages_property():
    """The ``succeeded_stages`` property counts only ``completed`` entries."""
    from heddle.workshop.pipeline_runner import StageResult

    result = PipelineTestResult(goal_id="g", success=False)
    result.stage_results = [
        StageResult(stage_name="A", worker_type="w", status="completed"),
        StageResult(stage_name="B", worker_type="w", status="failed"),
        StageResult(stage_name="C", worker_type="w", status="completed"),
        StageResult(stage_name="D", worker_type="w", status="skipped"),
    ]
    assert result.succeeded_stages == 2


def test_import_worker_class_rejects_bare_name():
    """A class reference without a module path raises ValueError."""
    from heddle.workshop.pipeline_runner import _import_worker_class

    with pytest.raises(ValueError, match="dotted path"):
        _import_worker_class("BareClass")


def test_import_worker_class_supports_dotted_form():
    """Both ``module:Class`` and ``module.Class`` are accepted."""
    from heddle.workshop.pipeline_runner import _import_worker_class

    cls = _import_worker_class(f"{__name__}.DoubleTypedWorker")
    assert cls is DoubleTypedWorker


def test_pick_representative_returns_none_for_empty():
    """Helper short-circuits cleanly when a stage produced no results."""
    assert PipelineTestRunner._pick_representative([]) is None


def test_extract_stage_from_error_helper():
    """The PipelineStageError message format is parsed for the failing stage name."""
    assert (
        PipelineTestRunner._extract_stage_from_error(
            "Stage 'classify' input validation failed: [...]"
        )
        == "classify"
    )
    assert PipelineTestRunner._extract_stage_from_error("no stage name here") is None
    assert PipelineTestRunner._extract_stage_from_error("") is None


@pytest.mark.asyncio
async def test_pipeline_config_not_found_returns_clean_error(workers_dir, tmp_path):
    """A missing pipeline config path produces a clear error, no partial run."""
    missing = tmp_path / "does-not-exist.yaml"
    runner = PipelineTestRunner(
        backends={"local": RoutingBackend({})},
        workers_dir=workers_dir,
    )
    try:
        result = await runner.run(str(missing), context={})
    finally:
        await runner.aclose()

    assert result.success is False
    assert result.error is not None
    assert "not found" in result.error.lower()
    assert result.stage_results == []


@pytest.mark.asyncio
async def test_invalid_pipeline_config_returns_validation_error(workers_dir):
    """A pipeline config missing required keys surfaces the validator's error list."""
    # 'pipeline_stages' is required; omit it.
    pipeline_path = _write_yaml_tempfile({"name": "broken"})
    runner = PipelineTestRunner(
        backends={"local": RoutingBackend({})},
        workers_dir=workers_dir,
    )
    try:
        result = await runner.run(pipeline_path, context={})
    finally:
        await runner.aclose()
        os.unlink(pipeline_path)

    assert result.success is False
    assert result.error is not None
    assert "Invalid pipeline config" in result.error
    assert result.stage_results == []


@pytest.mark.asyncio
async def test_duplicate_worker_type_resolves_once(workers_dir):
    """Two stages with the same worker_type share a single worker actor.

    Covers the worker_configs dedup branch (worker_type already loaded).
    """
    pipeline_cfg = _pipeline_cfg(
        "dedup",
        [
            {"name": "S1", "worker_type": "shared", "input_mapping": {"q": "goal.context.q"}},
            {"name": "S2", "worker_type": "shared", "input_mapping": {"q": "S1.output.v"}},
        ],
    )
    pipeline_path = _write_yaml_tempfile(pipeline_cfg)

    overrides = {"shared": _worker_cfg("shared")}
    backend = RoutingBackend({"shared": {"v": "ok"}})
    runner = PipelineTestRunner(
        backends={"local": backend},
        workers_dir=workers_dir,
        worker_config_overrides=overrides,
    )
    try:
        result = await runner.run(pipeline_path, context={"q": "x"})
    finally:
        await runner.aclose()
        os.unlink(pipeline_path)

    assert result.success is True
    assert {s.stage_name for s in result.stage_results} == {"S1", "S2"}


class _RaisingCloseBackend(LLMBackend):
    """Raises on aclose; exercises the best-effort path in PipelineTestRunner.aclose."""

    async def complete(self, *a, **kw):
        return {"content": "{}", "model": "x", "prompt_tokens": 0, "completion_tokens": 0}

    async def aclose(self):
        raise RuntimeError("backend refused to close")


@pytest.mark.asyncio
async def test_aclose_swallows_backend_close_errors(workers_dir):
    """An exception from one backend.aclose must not stop the rest of teardown."""
    good = _ClosableBackend()
    bad = _RaisingCloseBackend()
    runner = PipelineTestRunner(
        backends={"local": bad, "standard": good},
        workers_dir=workers_dir,
    )
    await runner._ensure_bus()  # so the bus-close branch runs too
    await runner.aclose()  # must not raise
    assert good.closed is True
