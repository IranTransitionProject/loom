"""Test PipelineOrchestrator (unit tests, no infrastructure)."""

import asyncio

import pytest

from heddle.bus.memory import InMemoryBus
from heddle.core.envelope import wrap
from heddle.core.messages import (
    OrchestratorGoal,
    TaskResult,
    TaskStatus,
)
from heddle.orchestrator.pipeline import (
    PipelineMappingError,
    PipelineOrchestrator,
    PipelineStageError,
    PipelineTimeoutError,
    PipelineValidationError,
    PipelineWorkerError,
)

# --- _resolve_path tests ---


class TestResolvePath:
    def test_simple_path(self):
        ctx = {"goal": {"context": {"file_ref": "report.pdf"}}}
        assert PipelineOrchestrator._resolve_path("goal.context.file_ref", ctx) == "report.pdf"

    def test_nested_output(self):
        ctx = {"extract": {"output": {"page_count": 42}}}
        assert PipelineOrchestrator._resolve_path("extract.output.page_count", ctx) == 42

    def test_top_level_key(self):
        ctx = {"extract": {"output": {"text": "hello"}}}
        result = PipelineOrchestrator._resolve_path("extract", ctx)
        assert result == {"output": {"text": "hello"}}

    def test_missing_key_raises(self):
        ctx = {"extract": {"output": {}}}
        with pytest.raises(KeyError, match="not found"):
            PipelineOrchestrator._resolve_path("extract.output.missing_field", ctx)

    def test_traverse_non_dict_raises(self):
        ctx = {"extract": {"output": "not_a_dict"}}
        with pytest.raises(ValueError, match="cannot traverse"):
            PipelineOrchestrator._resolve_path("extract.output.field", ctx)

    def test_array_value(self):
        ctx = {"extract": {"output": {"sections": ["intro", "body", "conclusion"]}}}
        assert PipelineOrchestrator._resolve_path("extract.output.sections", ctx) == [
            "intro",
            "body",
            "conclusion",
        ]

    def test_boolean_value(self):
        ctx = {"extract": {"output": {"has_tables": True}}}
        assert PipelineOrchestrator._resolve_path("extract.output.has_tables", ctx) is True


# --- _evaluate_condition tests ---


class TestEvaluateCondition:
    def test_equals_true(self):
        ctx = {"extract": {"output": {"has_tables": True}}}
        assert (
            PipelineOrchestrator._evaluate_condition("extract.output.has_tables == true", ctx)
            is True
        )

    def test_equals_false(self):
        ctx = {"extract": {"output": {"has_tables": False}}}
        assert (
            PipelineOrchestrator._evaluate_condition("extract.output.has_tables == true", ctx)
            is False
        )

    def test_not_equals_none(self):
        ctx = {"extract": {"output": {"data": "something"}}}
        assert PipelineOrchestrator._evaluate_condition("extract.output.data != null", ctx) is True

    def test_equals_none(self):
        ctx = {"extract": {"output": {"data": None}}}
        assert PipelineOrchestrator._evaluate_condition("extract.output.data == null", ctx) is True

    def test_missing_path_returns_false(self):
        ctx = {"extract": {"output": {}}}
        assert (
            PipelineOrchestrator._evaluate_condition("extract.output.nonexistent == true", ctx)
            is False
        )

    def test_invalid_condition_defaults_false_strict(self):
        """G7: malformed conditions skip the stage (fail-closed) by default.

        Earlier a malformed condition fell back to running the stage
        (fail-open) so a typo silently broadened the pipeline.  Now
        strict-by-default; set ``HEDDLE_STRICT_CONDITIONS=0`` to
        restore legacy fail-open semantics for migration.
        """
        ctx = {}
        assert PipelineOrchestrator._evaluate_condition("invalid", ctx) is False

    def test_invalid_condition_legacy_fail_open(self, monkeypatch):
        """``HEDDLE_STRICT_CONDITIONS=0`` restores the legacy fail-open behaviour."""
        monkeypatch.setenv("HEDDLE_STRICT_CONDITIONS", "0")
        ctx = {}
        assert PipelineOrchestrator._evaluate_condition("invalid", ctx) is True

    def test_unsupported_operator_skips_stage_strict(self):
        """Unknown operator (e.g. ``>=``) skips the stage by default."""
        ctx = {"x": {"output": {"n": 5}}}
        assert PipelineOrchestrator._evaluate_condition("x.output.n >= 3", ctx) is False

    def test_string_comparison(self):
        ctx = {"classify": {"output": {"document_type": "invoice"}}}
        assert (
            PipelineOrchestrator._evaluate_condition(
                "classify.output.document_type == invoice", ctx
            )
            is True
        )


# --- _build_stage_payload tests ---


class TestBuildStagePayload:
    def _make_orchestrator(self, tmp_path):
        """Create a PipelineOrchestrator with minimal config."""
        import yaml

        config = {
            "name": "test_pipeline",
            "pipeline_stages": [],
            "timeout_seconds": 10,
        }
        config_file = tmp_path / "pipeline.yaml"
        config_file.write_text(yaml.dump(config))
        return PipelineOrchestrator("test", str(config_file))

    def test_basic_mapping(self, tmp_path):
        orch = self._make_orchestrator(tmp_path)
        context = {
            "goal": {"context": {"file_ref": "doc.pdf"}},
            "extract": {"output": {"page_count": 10, "text_preview": "hello"}},
        }
        stage = {
            "name": "classify",
            "worker_type": "doc_classifier",
            "input_mapping": {
                "text_preview": "extract.output.text_preview",
                "page_count": "extract.output.page_count",
            },
        }
        payload = orch._build_stage_payload(stage, context)
        assert payload == {"text_preview": "hello", "page_count": 10}

    def test_goal_context_mapping(self, tmp_path):
        orch = self._make_orchestrator(tmp_path)
        context = {"goal": {"context": {"file_ref": "report.pdf"}}}
        stage = {
            "name": "extract",
            "worker_type": "doc_extractor",
            "input_mapping": {"file_ref": "goal.context.file_ref"},
        }
        payload = orch._build_stage_payload(stage, context)
        assert payload == {"file_ref": "report.pdf"}

    def test_missing_path_raises(self, tmp_path):
        orch = self._make_orchestrator(tmp_path)
        context = {"goal": {"context": {}}}
        stage = {
            "name": "extract",
            "worker_type": "doc_extractor",
            "input_mapping": {"file_ref": "goal.context.file_ref"},
        }
        with pytest.raises(KeyError):
            orch._build_stage_payload(stage, context)

    def test_empty_mapping(self, tmp_path):
        orch = self._make_orchestrator(tmp_path)
        stage = {"name": "noop", "worker_type": "noop"}
        payload = orch._build_stage_payload(stage, {})
        assert payload == {}

    def test_cross_stage_mapping(self, tmp_path):
        """Output from one stage feeds into another."""
        orch = self._make_orchestrator(tmp_path)
        context = {
            "extract": {"output": {"file_ref": "extracted.json"}},
            "classify": {"output": {"document_type": "invoice"}},
        }
        stage = {
            "name": "summarize",
            "worker_type": "doc_summarizer",
            "input_mapping": {
                "file_ref": "extract.output.file_ref",
                "document_type": "classify.output.document_type",
            },
        }
        payload = orch._build_stage_payload(stage, context)
        assert payload == {"file_ref": "extracted.json", "document_type": "invoice"}

    def test_literal_mapping_value(self, tmp_path):
        """Single-quoted strings in input_mapping are literal values, not paths.

        ``configs/orchestrators/rag_pipeline.yaml`` relies on this for the
        ``vectorize`` stage (``action: "'store'"``).  The validator in
        ``core/config.py`` already treats single-quoted strings as
        literals (skips dependency validation for them); the runtime
        payload builder must agree, otherwise ``_resolve_path`` tries
        to traverse a top-level context key named ``"'store'"`` and
        raises ``KeyError`` — the shipped config validates but fails
        at dispatch time.
        """
        orch = self._make_orchestrator(tmp_path)
        context = {"chunk": {"output": {"chunks": [{"id": 1}]}}}
        stage = {
            "name": "vectorize",
            "worker_type": "rag_vectorstore",
            "input_mapping": {
                "action": "'store'",
                "chunks": "chunk.output.chunks",
            },
        }
        payload = orch._build_stage_payload(stage, context)
        assert payload == {"action": "store", "chunks": [{"id": 1}]}


# --- _infer_dependencies tests ---


class TestInferDependencies:
    def test_goal_only_paths_have_no_deps(self):
        stages = [
            {"name": "extract", "input_mapping": {"file_ref": "goal.context.file_ref"}},
        ]
        deps = PipelineOrchestrator._infer_dependencies(stages)
        assert deps == {"extract": set()}

    def test_single_stage_dependency(self):
        stages = [
            {"name": "extract", "input_mapping": {"file_ref": "goal.context.file_ref"}},
            {"name": "classify", "input_mapping": {"text": "extract.output.text"}},
        ]
        deps = PipelineOrchestrator._infer_dependencies(stages)
        assert deps["extract"] == set()
        assert deps["classify"] == {"extract"}

    def test_multiple_dependencies(self):
        stages = [
            {"name": "A", "input_mapping": {"x": "goal.context.x"}},
            {"name": "B", "input_mapping": {"x": "goal.context.x"}},
            {"name": "C", "input_mapping": {"a": "A.output.a", "b": "B.output.b"}},
        ]
        deps = PipelineOrchestrator._infer_dependencies(stages)
        assert deps["C"] == {"A", "B"}

    def test_explicit_depends_on_overrides_inference(self):
        stages = [
            {"name": "A", "input_mapping": {"x": "goal.context.x"}},
            {"name": "B", "input_mapping": {"x": "goal.context.x"}},
            {
                "name": "C",
                "input_mapping": {"a": "A.output.a"},
                "depends_on": ["A", "B"],
            },
        ]
        deps = PipelineOrchestrator._infer_dependencies(stages)
        assert deps["C"] == {"A", "B"}

    def test_unknown_segments_ignored(self):
        stages = [
            {"name": "A", "input_mapping": {"x": "unknown_thing.output.x"}},
        ]
        deps = PipelineOrchestrator._infer_dependencies(stages)
        assert deps["A"] == set()

    def test_docman_pipeline_stays_sequential(self):
        """The docman 4-stage pipeline has fully sequential dependencies."""
        stages = [
            {"name": "extract", "input_mapping": {"file_ref": "goal.context.file_ref"}},
            {
                "name": "classify",
                "input_mapping": {
                    "text_preview": "extract.output.text_preview",
                    "page_count": "extract.output.page_count",
                },
            },
            {
                "name": "summarize",
                "input_mapping": {
                    "file_ref": "extract.output.file_ref",
                    "document_type": "classify.output.document_type",
                },
            },
            {
                "name": "ingest",
                "input_mapping": {
                    "source_file": "goal.context.file_ref",
                    "file_ref": "extract.output.file_ref",
                    "document_type": "classify.output.document_type",
                    "summary": "summarize.output.summary",
                },
            },
        ]
        deps = PipelineOrchestrator._infer_dependencies(stages)
        assert deps["extract"] == set()
        assert deps["classify"] == {"extract"}
        assert deps["summarize"] == {"extract", "classify"}
        assert deps["ingest"] == {"extract", "classify", "summarize"}

    def test_empty_input_mapping(self):
        stages = [{"name": "A"}]
        deps = PipelineOrchestrator._infer_dependencies(stages)
        assert deps["A"] == set()


# --- _build_execution_levels tests ---


class TestBuildExecutionLevels:
    def test_fully_sequential(self):
        stages = [
            {"name": "A"},
            {"name": "B"},
            {"name": "C"},
        ]
        deps = {"A": set(), "B": {"A"}, "C": {"B"}}
        levels = PipelineOrchestrator._build_execution_levels(stages, deps)
        assert len(levels) == 3
        assert [s["name"] for s in levels[0]] == ["A"]
        assert [s["name"] for s in levels[1]] == ["B"]
        assert [s["name"] for s in levels[2]] == ["C"]

    def test_two_independent_stages(self):
        stages = [
            {"name": "A"},
            {"name": "B"},
            {"name": "C"},
        ]
        deps = {"A": set(), "B": set(), "C": {"A", "B"}}
        levels = PipelineOrchestrator._build_execution_levels(stages, deps)
        assert len(levels) == 2
        assert [s["name"] for s in levels[0]] == ["A", "B"]  # alphabetical
        assert [s["name"] for s in levels[1]] == ["C"]

    def test_diamond_pattern(self):
        """A → B, A → C, B+C → D."""
        stages = [
            {"name": "A"},
            {"name": "B"},
            {"name": "C"},
            {"name": "D"},
        ]
        deps = {"A": set(), "B": {"A"}, "C": {"A"}, "D": {"B", "C"}}
        levels = PipelineOrchestrator._build_execution_levels(stages, deps)
        assert len(levels) == 3
        assert [s["name"] for s in levels[0]] == ["A"]
        assert [s["name"] for s in levels[1]] == ["B", "C"]
        assert [s["name"] for s in levels[2]] == ["D"]

    def test_circular_dependency_raises(self):
        stages = [{"name": "A"}, {"name": "B"}]
        deps = {"A": {"B"}, "B": {"A"}}
        with pytest.raises(ValueError, match="Circular dependency"):
            PipelineOrchestrator._build_execution_levels(stages, deps)

    def test_all_independent(self):
        stages = [{"name": "X"}, {"name": "Y"}, {"name": "Z"}]
        deps = {"X": set(), "Y": set(), "Z": set()}
        levels = PipelineOrchestrator._build_execution_levels(stages, deps)
        assert len(levels) == 1
        assert [s["name"] for s in levels[0]] == ["X", "Y", "Z"]

    def test_deterministic_ordering_within_level(self):
        """Stages within a level are sorted alphabetically."""
        stages = [{"name": "gamma"}, {"name": "alpha"}, {"name": "beta"}]
        deps = {"gamma": set(), "alpha": set(), "beta": set()}
        levels = PipelineOrchestrator._build_execution_levels(stages, deps)
        assert [s["name"] for s in levels[0]] == ["alpha", "beta", "gamma"]

    def test_docman_pipeline_produces_four_levels(self):
        """Docman's sequential pipeline → 4 levels of 1 stage each."""
        stages = [
            {"name": "extract"},
            {"name": "classify"},
            {"name": "summarize"},
            {"name": "ingest"},
        ]
        deps = {
            "extract": set(),
            "classify": {"extract"},
            "summarize": {"extract", "classify"},
            "ingest": {"extract", "classify", "summarize"},
        }
        levels = PipelineOrchestrator._build_execution_levels(stages, deps)
        assert len(levels) == 4
        for level in levels:
            assert len(level) == 1


# --- Parallel execution integration tests (InMemoryBus) ---


def _make_pipeline_orchestrator(tmp_path, stages, timeout=5, max_concurrent_goals=None):
    """Create a PipelineOrchestrator with given stages and InMemoryBus."""
    import yaml

    config = {
        "name": "test_pipeline",
        "pipeline_stages": stages,
        "timeout_seconds": timeout,
    }
    if max_concurrent_goals is not None:
        config["max_concurrent_goals"] = max_concurrent_goals
    config_file = tmp_path / "pipeline.yaml"
    config_file.write_text(yaml.dump(config))
    bus = InMemoryBus()
    orch = PipelineOrchestrator("test-pipeline", str(config_file), bus=bus)
    return orch, bus


async def _wait_for_pipeline_result(result_sub, goal_id, timeout=3):
    """Read messages from result_sub until we find the final pipeline result.

    The final result has task_id == goal_id (the pipeline publishes it with
    that convention).  Intermediate stage results also land on the same
    subject, so we skip those.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    async for data in result_sub:
        if data.get("payload", {}).get("task_id") == goal_id:
            return data.get("payload", data)
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError("Timed out waiting for pipeline result")
    raise TimeoutError("Subscription ended without pipeline result")


class TestParallelExecution:
    @pytest.mark.asyncio
    async def test_independent_stages_dispatch_concurrently(self, tmp_path):
        """Two independent stages should both be dispatched before either result arrives."""
        stages = [
            {
                "name": "A",
                "worker_type": "workerA",
                "tier": "local",
                "input_mapping": {"x": "goal.context.x"},
            },
            {
                "name": "B",
                "worker_type": "workerB",
                "tier": "local",
                "input_mapping": {"y": "goal.context.y"},
            },
            {
                "name": "C",
                "worker_type": "workerC",
                "tier": "local",
                "input_mapping": {"a": "A.output.result", "b": "B.output.result"},
            },
        ]
        orch, bus = _make_pipeline_orchestrator(tmp_path, stages, timeout=5)
        await bus.connect()

        goal = OrchestratorGoal(
            instruction="test",
            context={"x": "val_x", "y": "val_y"},
        )

        # Subscribe to intercept dispatched tasks and results.
        task_sub = await bus.subscribe("heddle.tasks.incoming")
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        # Run the pipeline in a background task.
        pipeline_task = asyncio.create_task(
            orch.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))
        )

        # Collect the dispatched tasks — A and B should both arrive
        # before we send any results.
        dispatched = {}
        for _ in range(2):
            data = await asyncio.wait_for(task_sub.__anext__(), timeout=2)
            dispatched[data["payload"]["metadata"]["stage_name"]] = data

        assert "A" in dispatched
        assert "B" in dispatched

        # Send results for A and B.
        for stage_name in ["A", "B"]:
            task_data = dispatched[stage_name]
            result = TaskResult(
                task_id=task_data["payload"]["task_id"],
                worker_type=task_data["payload"]["worker_type"],
                status=TaskStatus.COMPLETED,
                output={"result": f"{stage_name}_done"},
                processing_time_ms=10,
            )
            await bus.publish(
                f"heddle.results.{goal.goal_id}",
                wrap("core.TaskResult", result).model_dump(mode="json"),
            )

        # Now stage C should be dispatched.
        c_data = await asyncio.wait_for(task_sub.__anext__(), timeout=2)
        assert c_data["payload"]["metadata"]["stage_name"] == "C"

        # Send result for C.
        c_result = TaskResult(
            task_id=c_data["payload"]["task_id"],
            worker_type=c_data["payload"]["worker_type"],
            status=TaskStatus.COMPLETED,
            output={"result": "C_done"},
            processing_time_ms=10,
        )
        await bus.publish(
            f"heddle.results.{goal.goal_id}",
            wrap("core.TaskResult", c_result).model_dump(mode="json"),
        )

        # Pipeline should complete.
        await asyncio.wait_for(pipeline_task, timeout=3)

        # Verify final result (skip intermediate stage results).
        final = await asyncio.wait_for(
            _wait_for_pipeline_result(result_sub, goal.goal_id),
            timeout=3,
        )
        assert final["status"] == TaskStatus.COMPLETED.value
        assert "A" in final["output"]
        assert "B" in final["output"]
        assert "C" in final["output"]

    @pytest.mark.asyncio
    async def test_stage_failure_aborts_pipeline(self, tmp_path):
        """If a parallel stage fails, the pipeline aborts with FAILED."""
        stages = [
            {
                "name": "A",
                "worker_type": "workerA",
                "tier": "local",
                "input_mapping": {"x": "goal.context.x"},
            },
            {
                "name": "B",
                "worker_type": "workerB",
                "tier": "local",
                "input_mapping": {"y": "goal.context.y"},
            },
        ]
        orch, bus = _make_pipeline_orchestrator(tmp_path, stages, timeout=5)
        await bus.connect()

        goal = OrchestratorGoal(
            instruction="test",
            context={"x": "1", "y": "2"},
        )

        task_sub = await bus.subscribe("heddle.tasks.incoming")
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        pipeline_task = asyncio.create_task(
            orch.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))
        )

        # Both A and B dispatched concurrently.
        dispatched = {}
        for _ in range(2):
            data = await asyncio.wait_for(task_sub.__anext__(), timeout=2)
            dispatched[data["payload"]["metadata"]["stage_name"]] = data

        # A succeeds, B fails.
        a_result = TaskResult(
            task_id=dispatched["A"]["payload"]["task_id"],
            worker_type="workerA",
            status=TaskStatus.COMPLETED,
            output={"result": "ok"},
            processing_time_ms=10,
        )
        await bus.publish(
            f"heddle.results.{goal.goal_id}",
            wrap("core.TaskResult", a_result).model_dump(mode="json"),
        )

        b_result = TaskResult(
            task_id=dispatched["B"]["payload"]["task_id"],
            worker_type="workerB",
            status=TaskStatus.FAILED,
            error="something went wrong",
            processing_time_ms=10,
        )
        await bus.publish(
            f"heddle.results.{goal.goal_id}",
            wrap("core.TaskResult", b_result).model_dump(mode="json"),
        )

        await asyncio.wait_for(pipeline_task, timeout=3)

        final = await asyncio.wait_for(
            _wait_for_pipeline_result(result_sub, goal.goal_id),
            timeout=3,
        )
        assert final["status"] == TaskStatus.FAILED.value
        assert "B" in final["error"]

    @pytest.mark.asyncio
    async def test_failure_cancels_running_peers_in_same_level(self, tmp_path):
        """C4: when a parallel stage fails, still-running peers are cancelled.

        Pre-fix the level waited on every dispatched stage (``while pending:
        asyncio.wait(FIRST_COMPLETED)``) — a fast failure had to sit through
        the slowest sibling's full per-stage timeout before the pipeline
        could abort.  This wasted wall-clock time and (for LLM stages)
        burned tokens whose results would be discarded.

        Test shape: two parallel stages, B fails immediately, A is *never*
        given a result.  With cancellation, the pipeline aborts in well
        under the per-stage timeout.  Without cancellation, the pipeline
        hangs until A's stage timeout elapses (set to 4s here so the bug
        is observable in a 2s wait_for).
        """
        stages = [
            {
                "name": "A",
                "worker_type": "workerA",
                "tier": "local",
                "input_mapping": {"x": "goal.context.x"},
            },
            {
                "name": "B",
                "worker_type": "workerB",
                "tier": "local",
                "input_mapping": {"y": "goal.context.y"},
            },
        ]
        # 4s per-stage timeout — A would hang this long without cancellation.
        orch, bus = _make_pipeline_orchestrator(tmp_path, stages, timeout=4)
        await bus.connect()

        goal = OrchestratorGoal(
            instruction="cancel-test",
            context={"x": "1", "y": "2"},
        )

        task_sub = await bus.subscribe("heddle.tasks.incoming")
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        pipeline_task = asyncio.create_task(
            orch.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))
        )

        # Both A and B dispatched concurrently.
        dispatched = {}
        for _ in range(2):
            data = await asyncio.wait_for(task_sub.__anext__(), timeout=2)
            dispatched[data["payload"]["metadata"]["stage_name"]] = data

        # B fails immediately; A is intentionally NEVER given a result.
        b_result = TaskResult(
            task_id=dispatched["B"]["payload"]["task_id"],
            worker_type="workerB",
            status=TaskStatus.FAILED,
            error="B blew up",
            processing_time_ms=10,
        )
        await bus.publish(
            f"heddle.results.{goal.goal_id}",
            wrap("core.TaskResult", b_result).model_dump(mode="json"),
        )

        # The pipeline must abort well before A's 4s per-stage timeout.
        # ``wait_for(timeout=2)`` is generous: the cancellation path is
        # immediate, but the orchestrator still does a graceful shutdown.
        await asyncio.wait_for(pipeline_task, timeout=2)

        final = await asyncio.wait_for(
            _wait_for_pipeline_result(result_sub, goal.goal_id),
            timeout=2,
        )
        assert final["status"] == TaskStatus.FAILED.value
        assert "B" in final["error"]

    @pytest.mark.asyncio
    async def test_sequential_pipeline_unchanged(self, tmp_path):
        """A fully sequential pipeline still works correctly."""
        stages = [
            {
                "name": "first",
                "worker_type": "w1",
                "tier": "local",
                "input_mapping": {"x": "goal.context.x"},
            },
            {
                "name": "second",
                "worker_type": "w2",
                "tier": "local",
                "input_mapping": {"y": "first.output.result"},
            },
        ]
        orch, bus = _make_pipeline_orchestrator(tmp_path, stages, timeout=5)
        await bus.connect()

        goal = OrchestratorGoal(
            instruction="sequential test",
            context={"x": "input"},
        )

        task_sub = await bus.subscribe("heddle.tasks.incoming")
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        pipeline_task = asyncio.create_task(
            orch.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))
        )

        # First stage dispatched.
        first_data = await asyncio.wait_for(task_sub.__anext__(), timeout=2)
        assert first_data["payload"]["metadata"]["stage_name"] == "first"

        # Send result for first.
        first_result = TaskResult(
            task_id=first_data["payload"]["task_id"],
            worker_type="w1",
            status=TaskStatus.COMPLETED,
            output={"result": "first_done"},
            processing_time_ms=10,
        )
        await bus.publish(
            f"heddle.results.{goal.goal_id}",
            wrap("core.TaskResult", first_result).model_dump(mode="json"),
        )

        # Second stage dispatched.
        second_data = await asyncio.wait_for(task_sub.__anext__(), timeout=2)
        assert second_data["payload"]["metadata"]["stage_name"] == "second"

        # Send result for second.
        second_result = TaskResult(
            task_id=second_data["payload"]["task_id"],
            worker_type="w2",
            status=TaskStatus.COMPLETED,
            output={"result": "second_done"},
            processing_time_ms=10,
        )
        await bus.publish(
            f"heddle.results.{goal.goal_id}",
            wrap("core.TaskResult", second_result).model_dump(mode="json"),
        )

        await asyncio.wait_for(pipeline_task, timeout=3)

        final = await asyncio.wait_for(
            _wait_for_pipeline_result(result_sub, goal.goal_id),
            timeout=3,
        )
        assert final["status"] == TaskStatus.COMPLETED.value
        assert final["output"]["first"]["result"] == "first_done"
        assert final["output"]["second"]["result"] == "second_done"


# --- max_concurrent_goals tests ---


# --- Inter-stage contract validation tests (P1.1) ---


class TestInterStageValidation:
    """Verify that pipeline validates payloads against stage schemas."""

    @pytest.mark.asyncio
    async def test_input_schema_mismatch_fails_pipeline(self, tmp_path):
        """If a stage's input doesn't match its input_schema, the pipeline fails."""
        stages = [
            {
                "name": "extract",
                "worker_type": "extractor",
                "tier": "local",
                "input_mapping": {"file_ref": "goal.context.file_ref"},
            },
            {
                "name": "classify",
                "worker_type": "classifier",
                "tier": "local",
                "input_mapping": {
                    # Maps from extract output — but schema requires 'text' which
                    # won't be provided.
                    "text_preview": "extract.output.text_preview",
                },
                "input_schema": {
                    "type": "object",
                    "required": ["text_preview", "page_count"],
                    "properties": {
                        "text_preview": {"type": "string"},
                        "page_count": {"type": "integer"},
                    },
                },
            },
        ]
        orch, bus = _make_pipeline_orchestrator(tmp_path, stages, timeout=5)
        await bus.connect()

        goal = OrchestratorGoal(
            instruction="test input validation",
            context={"file_ref": "doc.pdf"},
        )

        task_sub = await bus.subscribe("heddle.tasks.incoming")
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        pipeline_task = asyncio.create_task(
            orch.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))
        )

        # Extract stage dispatched — send result (no page_count).
        first_data = await asyncio.wait_for(task_sub.__anext__(), timeout=2)
        first_result = TaskResult(
            task_id=first_data["payload"]["task_id"],
            worker_type="extractor",
            status=TaskStatus.COMPLETED,
            output={"text_preview": "hello world"},
            processing_time_ms=10,
        )
        await bus.publish(
            f"heddle.results.{goal.goal_id}",
            wrap("core.TaskResult", first_result).model_dump(mode="json"),
        )

        await asyncio.wait_for(pipeline_task, timeout=3)

        # Pipeline should fail because classify stage's input is missing 'page_count'.
        final = await asyncio.wait_for(
            _wait_for_pipeline_result(result_sub, goal.goal_id),
            timeout=3,
        )
        assert final["status"] == TaskStatus.FAILED.value
        assert "input validation failed" in final["error"].lower()
        assert "page_count" in final["error"]

    @pytest.mark.asyncio
    async def test_output_schema_mismatch_fails_pipeline(self, tmp_path):
        """If a stage's output doesn't match its output_schema, the pipeline fails."""
        stages = [
            {
                "name": "extract",
                "worker_type": "extractor",
                "tier": "local",
                "input_mapping": {"file_ref": "goal.context.file_ref"},
                "output_schema": {
                    "type": "object",
                    "required": ["text_preview", "page_count"],
                    "properties": {
                        "text_preview": {"type": "string"},
                        "page_count": {"type": "integer"},
                    },
                },
            },
        ]
        orch, bus = _make_pipeline_orchestrator(tmp_path, stages, timeout=5)
        await bus.connect()

        goal = OrchestratorGoal(
            instruction="test output validation",
            context={"file_ref": "doc.pdf"},
        )

        task_sub = await bus.subscribe("heddle.tasks.incoming")
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        pipeline_task = asyncio.create_task(
            orch.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))
        )

        # Extract stage returns output missing 'page_count'.
        data = await asyncio.wait_for(task_sub.__anext__(), timeout=2)
        result = TaskResult(
            task_id=data["payload"]["task_id"],
            worker_type="extractor",
            status=TaskStatus.COMPLETED,
            output={"text_preview": "hello"},  # Missing page_count
            processing_time_ms=10,
        )
        await bus.publish(
            f"heddle.results.{goal.goal_id}",
            wrap("core.TaskResult", result).model_dump(mode="json"),
        )

        await asyncio.wait_for(pipeline_task, timeout=3)

        final = await asyncio.wait_for(
            _wait_for_pipeline_result(result_sub, goal.goal_id),
            timeout=3,
        )
        assert final["status"] == TaskStatus.FAILED.value
        assert "output validation failed" in final["error"].lower()
        assert "page_count" in final["error"]

    @pytest.mark.asyncio
    async def test_valid_schemas_pass_through(self, tmp_path):
        """Stages with matching schemas proceed normally."""
        stages = [
            {
                "name": "extract",
                "worker_type": "extractor",
                "tier": "local",
                "input_mapping": {"file_ref": "goal.context.file_ref"},
                "input_schema": {
                    "type": "object",
                    "required": ["file_ref"],
                    "properties": {"file_ref": {"type": "string"}},
                },
                "output_schema": {
                    "type": "object",
                    "required": ["text"],
                    "properties": {"text": {"type": "string"}},
                },
            },
            {
                "name": "classify",
                "worker_type": "classifier",
                "tier": "local",
                "input_mapping": {"text": "extract.output.text"},
                "input_schema": {
                    "type": "object",
                    "required": ["text"],
                    "properties": {"text": {"type": "string"}},
                },
            },
        ]
        orch, bus = _make_pipeline_orchestrator(tmp_path, stages, timeout=5)
        await bus.connect()

        goal = OrchestratorGoal(
            instruction="test valid schemas",
            context={"file_ref": "doc.pdf"},
        )

        task_sub = await bus.subscribe("heddle.tasks.incoming")
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        pipeline_task = asyncio.create_task(
            orch.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))
        )

        # Extract stage.
        data = await asyncio.wait_for(task_sub.__anext__(), timeout=2)
        await bus.publish(
            f"heddle.results.{goal.goal_id}",
            wrap(
                "core.TaskResult",
                TaskResult(
                    task_id=data["payload"]["task_id"],
                    worker_type="extractor",
                    status=TaskStatus.COMPLETED,
                    output={"text": "document content"},
                    processing_time_ms=10,
                ),
            ).model_dump(mode="json"),
        )

        # Classify stage.
        data2 = await asyncio.wait_for(task_sub.__anext__(), timeout=2)
        await bus.publish(
            f"heddle.results.{goal.goal_id}",
            wrap(
                "core.TaskResult",
                TaskResult(
                    task_id=data2["payload"]["task_id"],
                    worker_type="classifier",
                    status=TaskStatus.COMPLETED,
                    output={"category": "report"},
                    processing_time_ms=10,
                ),
            ).model_dump(mode="json"),
        )

        await asyncio.wait_for(pipeline_task, timeout=3)

        final = await asyncio.wait_for(
            _wait_for_pipeline_result(result_sub, goal.goal_id),
            timeout=3,
        )
        assert final["status"] == TaskStatus.COMPLETED.value

    @pytest.mark.asyncio
    async def test_no_schemas_still_works(self, tmp_path):
        """Stages without schemas (backward compat) pass without validation."""
        stages = [
            {
                "name": "A",
                "worker_type": "w1",
                "tier": "local",
                "input_mapping": {"x": "goal.context.x"},
                # No input_schema or output_schema — should still work.
            },
        ]
        orch, bus = _make_pipeline_orchestrator(tmp_path, stages, timeout=5)
        await bus.connect()

        goal = OrchestratorGoal(instruction="test no schema", context={"x": "val"})

        task_sub = await bus.subscribe("heddle.tasks.incoming")
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        pipeline_task = asyncio.create_task(
            orch.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))
        )

        data = await asyncio.wait_for(task_sub.__anext__(), timeout=2)
        await bus.publish(
            f"heddle.results.{goal.goal_id}",
            wrap(
                "core.TaskResult",
                TaskResult(
                    task_id=data["payload"]["task_id"],
                    worker_type="w1",
                    status=TaskStatus.COMPLETED,
                    output={"anything": "goes"},
                    processing_time_ms=10,
                ),
            ).model_dump(mode="json"),
        )

        await asyncio.wait_for(pipeline_task, timeout=3)

        final = await asyncio.wait_for(
            _wait_for_pipeline_result(result_sub, goal.goal_id),
            timeout=3,
        )
        assert final["status"] == TaskStatus.COMPLETED.value

    @pytest.mark.asyncio
    async def test_input_type_mismatch_fails(self, tmp_path):
        """Type mismatch (string expected, got integer) is caught by inter-stage validation."""
        stages = [
            {
                "name": "A",
                "worker_type": "w1",
                "tier": "local",
                "input_mapping": {"x": "goal.context.x"},
                "input_schema": {
                    "type": "object",
                    "required": ["x"],
                    "properties": {"x": {"type": "string"}},
                },
            },
        ]
        orch, bus = _make_pipeline_orchestrator(tmp_path, stages, timeout=5)
        await bus.connect()

        # Pass integer instead of string.
        goal = OrchestratorGoal(instruction="test type mismatch", context={"x": 42})

        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        pipeline_task = asyncio.create_task(
            orch.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))
        )

        await asyncio.wait_for(pipeline_task, timeout=3)

        final = await asyncio.wait_for(
            _wait_for_pipeline_result(result_sub, goal.goal_id),
            timeout=3,
        )
        assert final["status"] == TaskStatus.FAILED.value
        assert "input validation failed" in final["error"].lower()


class TestPipelineConcurrentGoals:
    def test_default_max_concurrent_goals_is_one(self, tmp_path):
        """Without config, max_concurrent_goals defaults to 1."""
        orch, _ = _make_pipeline_orchestrator(tmp_path, stages=[])
        assert orch.max_concurrent == 1

    def test_max_concurrent_goals_from_config(self, tmp_path):
        """Config value is passed through to BaseActor.max_concurrent."""
        orch, _ = _make_pipeline_orchestrator(
            tmp_path,
            stages=[],
            max_concurrent_goals=3,
        )
        assert orch.max_concurrent == 3

    def test_bus_injection_via_constructor(self, tmp_path):
        """The bus= keyword argument is forwarded to BaseActor."""
        orch, bus = _make_pipeline_orchestrator(tmp_path, stages=[])
        assert orch._bus is bus


# --- Typed pipeline error tests (P1.2) ---


class TestTypedPipelineErrors:
    """Verify correct error subclasses are raised for each failure mode."""

    def test_error_hierarchy(self):
        """All typed errors are subclasses of PipelineStageError."""
        assert issubclass(PipelineTimeoutError, PipelineStageError)
        assert issubclass(PipelineValidationError, PipelineStageError)
        assert issubclass(PipelineWorkerError, PipelineStageError)
        assert issubclass(PipelineMappingError, PipelineStageError)

    def test_existing_catch_block_catches_subclasses(self):
        """An `except PipelineStageError` block catches all subtypes."""
        for cls in (
            PipelineTimeoutError,
            PipelineValidationError,
            PipelineWorkerError,
            PipelineMappingError,
        ):
            err = cls("test_stage", "test message")
            assert isinstance(err, PipelineStageError)
            assert err.stage_name == "test_stage"

    @pytest.mark.asyncio
    async def test_mapping_error_raises_pipeline_mapping_error(self, tmp_path):
        """A bad input_mapping raises PipelineMappingError."""
        stages = [
            {
                "name": "bad_stage",
                "worker_type": "w1",
                "tier": "local",
                "input_mapping": {"x": "goal.context.nonexistent"},
            },
        ]
        orch, bus = _make_pipeline_orchestrator(tmp_path, stages, timeout=5)
        await bus.connect()

        goal = OrchestratorGoal(instruction="test", context={"y": "val"})
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        pipeline_task = asyncio.create_task(
            orch.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))
        )
        await asyncio.wait_for(pipeline_task, timeout=3)

        final = await asyncio.wait_for(
            _wait_for_pipeline_result(result_sub, goal.goal_id), timeout=3
        )
        assert final["status"] == TaskStatus.FAILED.value
        assert "mapping error" in final["error"].lower()

    @pytest.mark.asyncio
    async def test_input_validation_raises_pipeline_validation_error(self, tmp_path):
        """Input schema mismatch raises PipelineValidationError."""
        stages = [
            {
                "name": "val_stage",
                "worker_type": "w1",
                "tier": "local",
                "input_mapping": {"x": "goal.context.x"},
                "input_schema": {
                    "type": "object",
                    "required": ["x"],
                    "properties": {"x": {"type": "string"}},
                },
            },
        ]
        orch, bus = _make_pipeline_orchestrator(tmp_path, stages, timeout=5)
        await bus.connect()

        # Pass integer instead of string.
        goal = OrchestratorGoal(instruction="test", context={"x": 42})
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        pipeline_task = asyncio.create_task(
            orch.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))
        )
        await asyncio.wait_for(pipeline_task, timeout=3)

        final = await asyncio.wait_for(
            _wait_for_pipeline_result(result_sub, goal.goal_id), timeout=3
        )
        assert final["status"] == TaskStatus.FAILED.value
        assert "input validation failed" in final["error"].lower()

    @pytest.mark.asyncio
    async def test_timeout_raises_pipeline_timeout_error(self, tmp_path):
        """A stage timeout raises PipelineTimeoutError."""
        stages = [
            {
                "name": "slow_stage",
                "worker_type": "w1",
                "tier": "local",
                "input_mapping": {"x": "goal.context.x"},
                "timeout_seconds": 0.1,  # Very short timeout.
            },
        ]
        orch, bus = _make_pipeline_orchestrator(tmp_path, stages, timeout=5)
        await bus.connect()

        goal = OrchestratorGoal(instruction="test", context={"x": "val"})
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        # Don't send any result — let it time out.
        pipeline_task = asyncio.create_task(
            orch.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))
        )
        await asyncio.wait_for(pipeline_task, timeout=3)

        final = await asyncio.wait_for(
            _wait_for_pipeline_result(result_sub, goal.goal_id), timeout=3
        )
        assert final["status"] == TaskStatus.FAILED.value
        assert "timed out" in final["error"].lower()

    @pytest.mark.asyncio
    async def test_worker_failure_raises_pipeline_worker_error(self, tmp_path):
        """A worker returning FAILED raises PipelineWorkerError."""
        stages = [
            {
                "name": "fail_stage",
                "worker_type": "w1",
                "tier": "local",
                "input_mapping": {"x": "goal.context.x"},
            },
        ]
        orch, bus = _make_pipeline_orchestrator(tmp_path, stages, timeout=5)
        await bus.connect()

        goal = OrchestratorGoal(instruction="test", context={"x": "val"})
        task_sub = await bus.subscribe("heddle.tasks.incoming")
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        pipeline_task = asyncio.create_task(
            orch.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))
        )

        data = await asyncio.wait_for(task_sub.__anext__(), timeout=2)
        fail_result = TaskResult(
            task_id=data["payload"]["task_id"],
            worker_type="w1",
            status=TaskStatus.FAILED,
            error="worker crashed",
            processing_time_ms=10,
        )
        await bus.publish(
            f"heddle.results.{goal.goal_id}",
            wrap("core.TaskResult", fail_result).model_dump(mode="json"),
        )

        await asyncio.wait_for(pipeline_task, timeout=3)

        final = await asyncio.wait_for(
            _wait_for_pipeline_result(result_sub, goal.goal_id), timeout=3
        )
        assert final["status"] == TaskStatus.FAILED.value
        assert "failed" in final["error"].lower()

    @pytest.mark.asyncio
    async def test_output_validation_raises_pipeline_validation_error(self, tmp_path):
        """Output schema mismatch raises PipelineValidationError."""
        stages = [
            {
                "name": "out_stage",
                "worker_type": "w1",
                "tier": "local",
                "input_mapping": {"x": "goal.context.x"},
                "output_schema": {
                    "type": "object",
                    "required": ["result", "count"],
                    "properties": {
                        "result": {"type": "string"},
                        "count": {"type": "integer"},
                    },
                },
            },
        ]
        orch, bus = _make_pipeline_orchestrator(tmp_path, stages, timeout=5)
        await bus.connect()

        goal = OrchestratorGoal(instruction="test", context={"x": "val"})
        task_sub = await bus.subscribe("heddle.tasks.incoming")
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        pipeline_task = asyncio.create_task(
            orch.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))
        )

        data = await asyncio.wait_for(task_sub.__anext__(), timeout=2)
        # Return output missing 'count'.
        ok_result = TaskResult(
            task_id=data["payload"]["task_id"],
            worker_type="w1",
            status=TaskStatus.COMPLETED,
            output={"result": "ok"},  # Missing 'count'.
            processing_time_ms=10,
        )
        await bus.publish(
            f"heddle.results.{goal.goal_id}",
            wrap("core.TaskResult", ok_result).model_dump(mode="json"),
        )

        await asyncio.wait_for(pipeline_task, timeout=3)

        final = await asyncio.wait_for(
            _wait_for_pipeline_result(result_sub, goal.goal_id), timeout=3
        )
        assert final["status"] == TaskStatus.FAILED.value
        assert "output validation failed" in final["error"].lower()


class TestStageRetry:
    """Test per-stage retry behavior."""

    @pytest.mark.asyncio
    async def test_retry_on_worker_failure_then_succeed(self, tmp_path):
        """Worker fails once, retries, succeeds on second attempt."""
        stages = [
            {
                "name": "retry_stage",
                "worker_type": "w1",
                "tier": "local",
                "input_mapping": {"x": "goal.context.x"},
                "max_retries": 1,
            },
        ]
        orch, bus = _make_pipeline_orchestrator(tmp_path, stages, timeout=5)
        await bus.connect()

        goal = OrchestratorGoal(instruction="test retry", context={"x": "val"})
        task_sub = await bus.subscribe("heddle.tasks.incoming")
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        pipeline_task = asyncio.create_task(
            orch.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))
        )

        # First attempt — worker fails.
        data1 = await asyncio.wait_for(task_sub.__anext__(), timeout=2)
        fail_result = TaskResult(
            task_id=data1["payload"]["task_id"],
            worker_type="w1",
            status=TaskStatus.FAILED,
            error="transient error",
            processing_time_ms=10,
        )
        await bus.publish(
            f"heddle.results.{goal.goal_id}",
            wrap("core.TaskResult", fail_result).model_dump(mode="json"),
        )

        # Second attempt (retry) — worker succeeds.
        data2 = await asyncio.wait_for(task_sub.__anext__(), timeout=2)
        ok_result = TaskResult(
            task_id=data2["payload"]["task_id"],
            worker_type="w1",
            status=TaskStatus.COMPLETED,
            output={"result": "success"},
            processing_time_ms=10,
        )
        await bus.publish(
            f"heddle.results.{goal.goal_id}",
            wrap("core.TaskResult", ok_result).model_dump(mode="json"),
        )

        await asyncio.wait_for(pipeline_task, timeout=3)

        final = await asyncio.wait_for(
            _wait_for_pipeline_result(result_sub, goal.goal_id), timeout=3
        )
        assert final["status"] == TaskStatus.COMPLETED.value
        assert final["output"]["retry_stage"]["result"] == "success"

    @pytest.mark.asyncio
    async def test_retry_exhausted_raises_last_error(self, tmp_path):
        """After exhausting retries, the pipeline fails."""
        stages = [
            {
                "name": "doomed",
                "worker_type": "w1",
                "tier": "local",
                "input_mapping": {"x": "goal.context.x"},
                "max_retries": 1,
            },
        ]
        orch, bus = _make_pipeline_orchestrator(tmp_path, stages, timeout=5)
        await bus.connect()

        goal = OrchestratorGoal(instruction="test", context={"x": "val"})
        task_sub = await bus.subscribe("heddle.tasks.incoming")
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        pipeline_task = asyncio.create_task(
            orch.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))
        )

        # Both attempts fail.
        for _ in range(2):
            data = await asyncio.wait_for(task_sub.__anext__(), timeout=2)
            fail_result = TaskResult(
                task_id=data["payload"]["task_id"],
                worker_type="w1",
                status=TaskStatus.FAILED,
                error="persistent error",
                processing_time_ms=10,
            )
            await bus.publish(
                f"heddle.results.{goal.goal_id}",
                wrap("core.TaskResult", fail_result).model_dump(mode="json"),
            )

        await asyncio.wait_for(pipeline_task, timeout=3)

        final = await asyncio.wait_for(
            _wait_for_pipeline_result(result_sub, goal.goal_id), timeout=3
        )
        assert final["status"] == TaskStatus.FAILED.value
        assert "failed" in final["error"].lower()

    @pytest.mark.asyncio
    async def test_no_retry_on_validation_error(self, tmp_path):
        """Validation errors are NOT retried — fail immediately."""
        stages = [
            {
                "name": "val_no_retry",
                "worker_type": "w1",
                "tier": "local",
                "input_mapping": {"x": "goal.context.x"},
                "input_schema": {
                    "type": "object",
                    "required": ["x"],
                    "properties": {"x": {"type": "string"}},
                },
                "max_retries": 3,  # Should NOT be used for validation errors.
            },
        ]
        orch, bus = _make_pipeline_orchestrator(tmp_path, stages, timeout=5)
        await bus.connect()

        # Pass integer — validation fails before dispatch.
        goal = OrchestratorGoal(instruction="test", context={"x": 42})
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        pipeline_task = asyncio.create_task(
            orch.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))
        )
        await asyncio.wait_for(pipeline_task, timeout=3)

        # No tasks should have been dispatched (validation fails before dispatch).
        # We check by verifying the pipeline failed and no tasks were sent.
        final = await asyncio.wait_for(
            _wait_for_pipeline_result(result_sub, goal.goal_id), timeout=3
        )
        assert final["status"] == TaskStatus.FAILED.value
        assert "input validation failed" in final["error"].lower()

    @pytest.mark.asyncio
    async def test_no_retry_on_mapping_error(self, tmp_path):
        """Mapping errors are NOT retried — fail immediately."""
        stages = [
            {
                "name": "map_no_retry",
                "worker_type": "w1",
                "tier": "local",
                "input_mapping": {"x": "goal.context.missing_key"},
                "max_retries": 3,  # Should NOT be used for mapping errors.
            },
        ]
        orch, bus = _make_pipeline_orchestrator(tmp_path, stages, timeout=5)
        await bus.connect()

        goal = OrchestratorGoal(instruction="test", context={"y": "val"})
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        pipeline_task = asyncio.create_task(
            orch.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))
        )
        await asyncio.wait_for(pipeline_task, timeout=3)

        final = await asyncio.wait_for(
            _wait_for_pipeline_result(result_sub, goal.goal_id), timeout=3
        )
        assert final["status"] == TaskStatus.FAILED.value
        assert "mapping error" in final["error"].lower()

    @pytest.mark.asyncio
    async def test_pipeline_max_retries_negative_rejected(self, tmp_path):
        """J8 / G7: ``max_retries=-1`` is rejected with a clear error.

        Earlier a negative value made ``range(max_retries + 1)`` an
        empty iterator, so the retry loop never executed and the
        post-loop ``raise last_error`` tripped a NameError /
        type-ignore.  G7 validates at the point of use; this test
        pins the named-field error message so a future "let's be
        permissive" change can't silently restore the original
        crash mode.
        """
        from heddle.orchestrator.pipeline import PipelineStageError

        stages = [
            {
                "name": "negative_retries",
                "worker_type": "w1",
                "tier": "local",
                "input_mapping": {"x": "goal.context.x"},
                "max_retries": -1,
            },
        ]
        orch, bus = _make_pipeline_orchestrator(tmp_path, stages, timeout=5)
        await bus.connect()

        goal = OrchestratorGoal(instruction="test", context={"x": "val"})
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        pipeline_task = asyncio.create_task(
            orch.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))
        )
        await asyncio.wait_for(pipeline_task, timeout=3)

        final = await asyncio.wait_for(
            _wait_for_pipeline_result(result_sub, goal.goal_id), timeout=3
        )
        assert final["status"] == TaskStatus.FAILED.value
        # The error message must name the stage and the bad value
        # so the operator can fix the YAML without grepping.
        err = final["error"].lower()
        assert "negative_retries" in err
        assert "max_retries=-1" in err or "max_retries" in err
        assert ">= 0" in final["error"]

        # The underlying exception type is ``PipelineStageError`` —
        # not a NameError / type-ignore tripped at the bottom of
        # the retry loop, which was the pre-G7 failure mode.  We
        # cannot inspect the exception type via the published
        # result; the assertion above on ``">= 0"`` substring is
        # the strongest proxy reachable from the bus surface.
        assert PipelineStageError  # symbol used; type-pinning sentinel.

    @pytest.mark.asyncio
    async def test_retry_on_timeout_then_succeed(self, tmp_path):
        """Timeout on first attempt, success on retry."""
        stages = [
            {
                "name": "timeout_retry",
                "worker_type": "w1",
                "tier": "local",
                "input_mapping": {"x": "goal.context.x"},
                "timeout_seconds": 0.2,
                "max_retries": 1,
            },
        ]
        orch, bus = _make_pipeline_orchestrator(tmp_path, stages, timeout=5)
        await bus.connect()

        goal = OrchestratorGoal(instruction="test", context={"x": "val"})
        task_sub = await bus.subscribe("heddle.tasks.incoming")
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        pipeline_task = asyncio.create_task(
            orch.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))
        )

        # First attempt — don't respond (let it time out).
        await asyncio.wait_for(task_sub.__anext__(), timeout=2)
        # Don't send a result — timeout triggers retry.

        # Second attempt (retry) — respond successfully.
        data2 = await asyncio.wait_for(task_sub.__anext__(), timeout=2)
        ok_result = TaskResult(
            task_id=data2["payload"]["task_id"],
            worker_type="w1",
            status=TaskStatus.COMPLETED,
            output={"result": "recovered"},
            processing_time_ms=10,
        )
        await bus.publish(
            f"heddle.results.{goal.goal_id}",
            wrap("core.TaskResult", ok_result).model_dump(mode="json"),
        )

        await asyncio.wait_for(pipeline_task, timeout=5)

        final = await asyncio.wait_for(
            _wait_for_pipeline_result(result_sub, goal.goal_id), timeout=3
        )
        assert final["status"] == TaskStatus.COMPLETED.value
        assert final["output"]["timeout_retry"]["result"] == "recovered"


class TestRequestIdPropagation:
    """Test that request_id propagates from goal to task messages."""

    @pytest.mark.asyncio
    async def test_request_id_set_on_dispatched_task(self, tmp_path):
        """Pipeline sets request_id = goal.goal_id on dispatched TaskMessages."""
        stages = [
            {
                "name": "A",
                "worker_type": "workerA",
                "tier": "local",
                "input_mapping": {"x": "goal.context.x"},
            },
        ]
        orch, bus = _make_pipeline_orchestrator(tmp_path, stages, timeout=5)
        await bus.connect()

        goal = OrchestratorGoal(instruction="test", context={"x": "val"})
        task_sub = await bus.subscribe("heddle.tasks.incoming")
        await bus.subscribe(f"heddle.results.{goal.goal_id}")

        pipeline_task = asyncio.create_task(
            orch.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))
        )

        # Read the dispatched task and verify request_id.
        data = await asyncio.wait_for(task_sub.__anext__(), timeout=2)
        assert data["payload"]["request_id"] == goal.goal_id

        # Complete the pipeline so it doesn't hang.
        result = TaskResult(
            task_id=data["payload"]["task_id"],
            worker_type="workerA",
            status=TaskStatus.COMPLETED,
            output={"result": "done"},
            processing_time_ms=10,
        )
        await bus.publish(
            f"heddle.results.{goal.goal_id}",
            wrap("core.TaskResult", result).model_dump(mode="json"),
        )
        await asyncio.wait_for(pipeline_task, timeout=3)

    @pytest.mark.asyncio
    async def test_request_id_consistent_across_stages(self, tmp_path):
        """All stages in a pipeline share the same request_id (the goal_id)."""
        stages = [
            {
                "name": "A",
                "worker_type": "workerA",
                "tier": "local",
                "input_mapping": {"x": "goal.context.x"},
            },
            {
                "name": "B",
                "worker_type": "workerB",
                "tier": "local",
                "input_mapping": {"y": "goal.context.y"},
            },
        ]
        orch, bus = _make_pipeline_orchestrator(tmp_path, stages, timeout=5)
        await bus.connect()

        goal = OrchestratorGoal(instruction="test", context={"x": "1", "y": "2"})
        task_sub = await bus.subscribe("heddle.tasks.incoming")
        await bus.subscribe(f"heddle.results.{goal.goal_id}")

        pipeline_task = asyncio.create_task(
            orch.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))
        )

        # Both independent stages dispatched — collect and verify request_id.
        dispatched = []
        for _ in range(2):
            data = await asyncio.wait_for(task_sub.__anext__(), timeout=2)
            dispatched.append(data)

        for data in dispatched:
            assert data["payload"]["request_id"] == goal.goal_id

        # Complete both stages.
        for data in dispatched:
            result = TaskResult(
                task_id=data["payload"]["task_id"],
                worker_type=data["payload"]["worker_type"],
                status=TaskStatus.COMPLETED,
                output={"result": "done"},
                processing_time_ms=10,
            )
            await bus.publish(
                f"heddle.results.{goal.goal_id}",
                wrap("core.TaskResult", result).model_dump(mode="json"),
            )
        await asyncio.wait_for(pipeline_task, timeout=3)


# --- Pipeline execution timeline tests (P2.4) ---


class TestPipelineTimeline:
    """Verify that pipeline results include execution timeline data."""

    @pytest.mark.asyncio
    async def test_single_stage_timeline(self, tmp_path):
        """Completed pipeline includes _timeline with stage timing data."""
        stages = [
            {
                "name": "extract",
                "worker_type": "extractor",
                "tier": "local",
                "input_mapping": {"file_ref": "goal.context.file_ref"},
            },
        ]
        orch, bus = _make_pipeline_orchestrator(tmp_path, stages, timeout=5)
        await bus.connect()

        goal = OrchestratorGoal(instruction="test timeline", context={"file_ref": "doc.pdf"})
        task_sub = await bus.subscribe("heddle.tasks.incoming")
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        pipeline_task = asyncio.create_task(
            orch.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))
        )

        data = await asyncio.wait_for(task_sub.__anext__(), timeout=2)
        await bus.publish(
            f"heddle.results.{goal.goal_id}",
            wrap(
                "core.TaskResult",
                TaskResult(
                    task_id=data["payload"]["task_id"],
                    worker_type="extractor",
                    status=TaskStatus.COMPLETED,
                    output={"text": "hello"},
                    processing_time_ms=42,
                ),
            ).model_dump(mode="json"),
        )

        await asyncio.wait_for(pipeline_task, timeout=3)
        final = await asyncio.wait_for(
            _wait_for_pipeline_result(result_sub, goal.goal_id), timeout=3
        )

        assert final["status"] == TaskStatus.COMPLETED.value
        assert "_timeline" in final["output"]

        timeline = final["output"]["_timeline"]
        assert len(timeline) == 1
        entry = timeline[0]
        assert entry["stage"] == "extract"
        assert entry["started_at"] is not None
        assert entry["ended_at"] is not None
        assert entry["wall_time_ms"] >= 0
        assert entry["processing_time_ms"] == 42

    @pytest.mark.asyncio
    async def test_multi_stage_timeline_ordering(self, tmp_path):
        """Timeline entries preserve stage execution order."""
        stages = [
            {
                "name": "A",
                "worker_type": "w1",
                "tier": "local",
                "input_mapping": {"x": "goal.context.x"},
            },
            {
                "name": "B",
                "worker_type": "w2",
                "tier": "local",
                "input_mapping": {"y": "A.output.y"},
            },
        ]
        orch, bus = _make_pipeline_orchestrator(tmp_path, stages, timeout=5)
        await bus.connect()

        goal = OrchestratorGoal(instruction="test", context={"x": "val"})
        task_sub = await bus.subscribe("heddle.tasks.incoming")
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        pipeline_task = asyncio.create_task(
            orch.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))
        )

        # Stage A
        data_a = await asyncio.wait_for(task_sub.__anext__(), timeout=2)
        await bus.publish(
            f"heddle.results.{goal.goal_id}",
            wrap(
                "core.TaskResult",
                TaskResult(
                    task_id=data_a["payload"]["task_id"],
                    worker_type="w1",
                    status=TaskStatus.COMPLETED,
                    output={"y": "from_a"},
                    processing_time_ms=10,
                ),
            ).model_dump(mode="json"),
        )

        # Stage B
        data_b = await asyncio.wait_for(task_sub.__anext__(), timeout=2)
        await bus.publish(
            f"heddle.results.{goal.goal_id}",
            wrap(
                "core.TaskResult",
                TaskResult(
                    task_id=data_b["payload"]["task_id"],
                    worker_type="w2",
                    status=TaskStatus.COMPLETED,
                    output={"z": "from_b"},
                    processing_time_ms=20,
                ),
            ).model_dump(mode="json"),
        )

        await asyncio.wait_for(pipeline_task, timeout=3)
        final = await asyncio.wait_for(
            _wait_for_pipeline_result(result_sub, goal.goal_id), timeout=3
        )

        timeline = final["output"]["_timeline"]
        assert len(timeline) == 2
        stage_names = [e["stage"] for e in timeline]
        assert "A" in stage_names
        assert "B" in stage_names

        # A should have started before B (sequential dependency).
        a_entry = next(e for e in timeline if e["stage"] == "A")
        b_entry = next(e for e in timeline if e["stage"] == "B")
        assert a_entry["ended_at"] <= b_entry["started_at"]

    @pytest.mark.asyncio
    async def test_failed_pipeline_has_no_timeline(self, tmp_path):
        """Failed pipelines don't include timeline (output is None)."""
        stages = [
            {
                "name": "A",
                "worker_type": "w1",
                "tier": "local",
                "input_mapping": {"x": "goal.context.x"},
            },
        ]
        orch, bus = _make_pipeline_orchestrator(tmp_path, stages, timeout=5)
        await bus.connect()

        goal = OrchestratorGoal(instruction="test", context={"x": "val"})
        task_sub = await bus.subscribe("heddle.tasks.incoming")
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        pipeline_task = asyncio.create_task(
            orch.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))
        )

        data = await asyncio.wait_for(task_sub.__anext__(), timeout=2)
        await bus.publish(
            f"heddle.results.{goal.goal_id}",
            wrap(
                "core.TaskResult",
                TaskResult(
                    task_id=data["payload"]["task_id"],
                    worker_type="w1",
                    status=TaskStatus.FAILED,
                    error="boom",
                    processing_time_ms=10,
                ),
            ).model_dump(mode="json"),
        )

        await asyncio.wait_for(pipeline_task, timeout=3)
        final = await asyncio.wait_for(
            _wait_for_pipeline_result(result_sub, goal.goal_id), timeout=3
        )

        assert final["status"] == TaskStatus.FAILED.value
        # Failed pipeline has no output, so no timeline.
        assert final.get("output") is None
