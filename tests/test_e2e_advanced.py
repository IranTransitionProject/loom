"""Advanced end-to-end tests — failure paths, retries, and operational scenarios.

These tests exercise error handling and operational resilience patterns
via InMemoryBus. No external infrastructure needed.

Complements test_e2e_operations.py (happy-path multi-actor workflows).
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

import pytest
import yaml

from heddle.bus.memory import InMemoryBus
from heddle.core.envelope import wrap
from heddle.core.messages import (
    ModelTier,
    OrchestratorGoal,
    TaskMessage,
    TaskResult,
    TaskStatus,
)
from heddle.orchestrator.pipeline import PipelineOrchestrator
from heddle.orchestrator.runner import OrchestratorActor
from heddle.router.router import TaskRouter
from heddle.worker.backends import LLMBackend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(data: dict) -> str:
    """Write a YAML dict to a temp file and return the path."""
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as f:
        yaml.dump(data, f)
    return path


class MockBackend(LLMBackend):
    """Mock LLM backend with configurable per-call responses."""

    def __init__(
        self,
        decompose_response: str = "[]",
        synthesis_response: str = "{}",
    ) -> None:
        self._decompose = decompose_response
        self._synthesis = synthesis_response

    async def complete(self, system_prompt, user_message, max_tokens, temperature=0.0, **kw):
        if "task decomposition" in system_prompt.lower():
            content = self._decompose
        else:
            content = self._synthesis
        return {
            "content": content,
            "model": "mock",
            "prompt_tokens": 50,
            "completion_tokens": 25,
        }


async def _get_result(sub, goal_id: str, timeout: float = 3.0) -> dict:
    """Wait for the final result matching goal_id."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise TimeoutError(f"Result for {goal_id} not received in {timeout}s")
        msg = await asyncio.wait_for(sub.__anext__(), timeout=remaining)
        if msg.get("task_id") == goal_id:
            return msg


async def _failing_worker(
    bus: InMemoryBus,
    subject: str,
    fail_worker_types: set[str] | None = None,
    error_msg: str = "Worker crashed",
    max_messages: int = 10,
    sub=None,
):
    """Worker simulator that fails for specified worker types."""
    if sub is None:
        sub = await bus.subscribe(subject)
    count = 0

    async for data in sub:
        task = TaskMessage(**data)
        await asyncio.sleep(0.05)

        should_fail = fail_worker_types and task.worker_type in fail_worker_types
        status = TaskStatus.FAILED if should_fail else TaskStatus.COMPLETED
        output = None if should_fail else {"result": "ok"}
        error = error_msg if should_fail else None

        result = TaskResult(
            task_id=task.task_id,
            parent_task_id=task.parent_task_id,
            worker_type=task.worker_type,
            status=status,
            output=output,
            error=error,
            model_used="mock",
            processing_time_ms=10,
            token_usage={"prompt_tokens": 10, "completion_tokens": 5},
        )
        if task.parent_task_id:
            await bus.publish(
                f"heddle.results.{task.parent_task_id}",
                result.model_dump(mode="json"),
            )

        count += 1
        if count >= max_messages:
            break

    await sub.unsubscribe()


async def _delayed_worker(
    bus: InMemoryBus,
    subject: str,
    delay_seconds: float = 0.5,
    max_messages: int = 10,
    sub=None,
):
    """Worker simulator with configurable response delay."""
    if sub is None:
        sub = await bus.subscribe(subject)
    count = 0

    async for data in sub:
        task = TaskMessage(**data)
        await asyncio.sleep(delay_seconds)

        result = TaskResult(
            task_id=task.task_id,
            parent_task_id=task.parent_task_id,
            worker_type=task.worker_type,
            status=TaskStatus.COMPLETED,
            output={"delayed": True, "worker_type": task.worker_type},
            model_used="mock",
            processing_time_ms=int(delay_seconds * 1000),
            token_usage={"prompt_tokens": 10, "completion_tokens": 5},
        )
        if task.parent_task_id:
            await bus.publish(
                f"heddle.results.{task.parent_task_id}",
                result.model_dump(mode="json"),
            )

        count += 1
        if count >= max_messages:
            break

    await sub.unsubscribe()


# ---------------------------------------------------------------------------
# Test 1: Pipeline stage failure propagation
# ---------------------------------------------------------------------------


class TestPipelineFailurePropagation:
    """Verify that a failed pipeline stage produces a FAILED final result."""

    @pytest.mark.asyncio
    async def test_second_stage_failure_produces_failed_result(self):
        """When the second stage fails, the pipeline reports FAILED."""
        bus = InMemoryBus()
        await bus.connect()

        config_path = _write_yaml(
            {
                "name": "fail-pipeline",
                "timeout_seconds": 5,
                "pipeline_stages": [
                    {
                        "name": "extract",
                        "worker_type": "extractor",
                        "model_tier": "local",
                        "input_mapping": {"file_ref": "goal.context.file_ref"},
                    },
                    {
                        "name": "classify",
                        "worker_type": "classifier",
                        "model_tier": "local",
                        "input_mapping": {"text": "extract.output.text"},
                    },
                ],
            }
        )

        try:
            pipeline = PipelineOrchestrator(
                actor_id="fail-pipeline",
                config_path=config_path,
                bus=bus,
            )

            goal = OrchestratorGoal(
                instruction="Process document",
                context={"file_ref": "test.pdf"},
            )
            result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

            # Extractor succeeds, classifier fails
            worker_sub = await bus.subscribe("heddle.tasks.incoming")

            async def _worker():
                count = 0
                async for data in worker_sub:
                    task = TaskMessage(**data)
                    await asyncio.sleep(0.05)
                    if task.worker_type == "extractor":
                        result = TaskResult(
                            task_id=task.task_id,
                            parent_task_id=task.parent_task_id,
                            worker_type=task.worker_type,
                            status=TaskStatus.COMPLETED,
                            output={"text": "Doc text"},
                            model_used="mock",
                            processing_time_ms=10,
                        )
                    else:
                        result = TaskResult(
                            task_id=task.task_id,
                            parent_task_id=task.parent_task_id,
                            worker_type=task.worker_type,
                            status=TaskStatus.FAILED,
                            error="Classification model unavailable",
                            model_used="mock",
                            processing_time_ms=10,
                        )
                    await bus.publish(
                        f"heddle.results.{task.parent_task_id}",
                        result.model_dump(mode="json"),
                    )
                    count += 1
                    if count >= 2:
                        break
                await worker_sub.unsubscribe()

            worker_task = asyncio.create_task(_worker())
            await pipeline.handle_message(
                wrap("core.OrchestratorGoal", goal).model_dump(mode="json")
            )
            await worker_task

            final = await _get_result(result_sub, goal.goal_id)
            assert final["status"] == TaskStatus.FAILED.value
            assert "classifier" in (final.get("error") or "").lower() or (
                "classify" in (final.get("error") or "").lower()
            )

            await bus.close()
        finally:
            os.unlink(config_path)


# ---------------------------------------------------------------------------
# Test 2: Pipeline stage timeout
# ---------------------------------------------------------------------------


class TestPipelineStageTimeout:
    """Verify that a slow stage causes a timeout and FAILED result."""

    @pytest.mark.asyncio
    async def test_slow_stage_times_out(self):
        """A stage that exceeds its timeout produces a FAILED result."""
        bus = InMemoryBus()
        await bus.connect()

        config_path = _write_yaml(
            {
                "name": "timeout-pipeline",
                "timeout_seconds": 0.3,  # Very short timeout
                "pipeline_stages": [
                    {
                        "name": "slow_stage",
                        "worker_type": "slow_worker",
                        "model_tier": "local",
                        "input_mapping": {"data": "goal.context.data"},
                    },
                ],
            }
        )

        try:
            pipeline = PipelineOrchestrator(
                actor_id="timeout-pipeline",
                config_path=config_path,
                bus=bus,
            )

            goal = OrchestratorGoal(
                instruction="Process slowly",
                context={"data": "test"},
            )
            result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

            # Worker that never responds (simulating timeout)
            worker_sub = await bus.subscribe("heddle.tasks.incoming")

            await pipeline.handle_message(
                wrap("core.OrchestratorGoal", goal).model_dump(mode="json")
            )

            final = await _get_result(result_sub, goal.goal_id)
            assert final["status"] == TaskStatus.FAILED.value
            assert "timed out" in (final.get("error") or "").lower()

            await worker_sub.unsubscribe()
            await bus.close()
        finally:
            os.unlink(config_path)


# ---------------------------------------------------------------------------
# Test 3: Router dead-letter on malformed messages
# ---------------------------------------------------------------------------


class TestRouterDeadLetter:
    """Verify that malformed or unroutable tasks go to dead-letter."""

    @pytest.mark.asyncio
    async def test_malformed_message_dead_lettered(self):
        """A message that fails TaskMessage validation goes to dead letter."""
        bus = InMemoryBus()

        rules_path = _write_yaml({"tier_overrides": {}, "rate_limits": {}})
        try:
            router = TaskRouter(rules_path, bus)
            await router.run()

            dead_sub = await bus.subscribe("heddle.tasks.dead_letter")

            # Publish a malformed message (missing required fields)
            await bus.publish("heddle.tasks.incoming", {"bad": "data"})

            process_task = asyncio.create_task(router.process_messages())

            msg = await asyncio.wait_for(dead_sub.__anext__(), timeout=2.0)
            assert "reason" in msg
            assert "invalid_task_message" in msg["reason"]

            process_task.cancel()
            try:
                await process_task
            except asyncio.CancelledError:
                pass
            await bus.close()
        finally:
            os.unlink(rules_path)

    @pytest.mark.asyncio
    async def test_rate_limited_task_dead_lettered(self):
        """When a tier's rate limit is exhausted, tasks go to dead letter."""
        bus = InMemoryBus()

        rules_path = _write_yaml(
            {
                "tier_overrides": {},
                "rate_limits": {
                    # max_concurrent=1 means only 1 token available
                    "local": {"max_concurrent": 1},
                },
            }
        )
        try:
            router = TaskRouter(rules_path, bus)
            await router.run()

            dead_sub = await bus.subscribe("heddle.tasks.dead_letter")
            await bus.subscribe("heddle.tasks.summarizer.local")

            process_task = asyncio.create_task(router.process_messages())

            # Publish many tasks rapidly to exhaust the rate limit
            for i in range(5):
                task = TaskMessage(
                    worker_type="summarizer",
                    payload={"text": f"task {i}"},
                    model_tier=ModelTier.LOCAL,
                )
                await bus.publish(
                    "heddle.tasks.incoming",
                    task.model_dump(mode="json"),
                )

            # Give router time to process
            await asyncio.sleep(0.2)

            # At least one should have been rate-limited
            dead_count = 0
            while True:
                try:
                    msg = await asyncio.wait_for(dead_sub.__anext__(), timeout=0.1)
                    if "rate_limited" in msg.get("reason", ""):
                        dead_count += 1
                except TimeoutError:
                    break

            assert dead_count >= 1, "Expected at least one rate-limited task"

            process_task.cancel()
            try:
                await process_task
            except asyncio.CancelledError:
                pass
            await bus.close()
        finally:
            os.unlink(rules_path)


# ---------------------------------------------------------------------------
# Test 4: Pipeline with conditional stage skipping
# ---------------------------------------------------------------------------


class TestPipelineConditionalStages:
    """Verify that stages with false conditions are skipped."""

    @pytest.mark.asyncio
    async def test_condition_false_skips_stage(self):
        """A stage whose condition evaluates to false is skipped."""
        bus = InMemoryBus()
        await bus.connect()

        config_path = _write_yaml(
            {
                "name": "conditional-pipeline",
                "timeout_seconds": 5,
                "pipeline_stages": [
                    {
                        "name": "extract",
                        "worker_type": "extractor",
                        "model_tier": "local",
                        "input_mapping": {"file_ref": "goal.context.file_ref"},
                    },
                    {
                        "name": "ocr",
                        "worker_type": "ocr_worker",
                        "model_tier": "local",
                        "input_mapping": {"file_ref": "goal.context.file_ref"},
                        # This condition will be false because extract.output
                        # won't have needs_ocr == true
                        "condition": "extract.output.needs_ocr == true",
                    },
                ],
            }
        )

        try:
            pipeline = PipelineOrchestrator(
                actor_id="conditional-pipeline",
                config_path=config_path,
                bus=bus,
            )

            goal = OrchestratorGoal(
                instruction="Process doc with conditional OCR",
                context={"file_ref": "test.pdf"},
            )
            result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

            worker_sub = await bus.subscribe("heddle.tasks.incoming")

            async def _worker():
                async for data in worker_sub:
                    task = TaskMessage(**data)
                    await asyncio.sleep(0.05)
                    result = TaskResult(
                        task_id=task.task_id,
                        parent_task_id=task.parent_task_id,
                        worker_type=task.worker_type,
                        status=TaskStatus.COMPLETED,
                        output={"text": "Extracted text", "needs_ocr": False},
                        model_used="mock",
                        processing_time_ms=10,
                    )
                    await bus.publish(
                        f"heddle.results.{task.parent_task_id}",
                        result.model_dump(mode="json"),
                    )
                    break
                await worker_sub.unsubscribe()

            worker_task = asyncio.create_task(_worker())
            await pipeline.handle_message(
                wrap("core.OrchestratorGoal", goal).model_dump(mode="json")
            )
            await worker_task

            final = await _get_result(result_sub, goal.goal_id)
            assert final["status"] == TaskStatus.COMPLETED.value
            # OCR stage should have been skipped — only extract in output
            output = final["output"]
            assert "extract" in output
            assert "ocr" not in output

            await bus.close()
        finally:
            os.unlink(config_path)


# ---------------------------------------------------------------------------
# Test 5: Router tier override
# ---------------------------------------------------------------------------


class TestRouterTierOverride:
    """Verify tier_overrides in router_rules.yaml are applied."""

    @pytest.mark.asyncio
    async def test_tier_override_routes_to_overridden_subject(self):
        """A task for 'classifier' with tier override to 'frontier' goes to the frontier subject."""
        bus = InMemoryBus()

        rules_path = _write_yaml(
            {
                "tier_overrides": {
                    "classifier": "frontier",  # Force classifier to frontier
                },
                "rate_limits": {},
            }
        )
        try:
            router = TaskRouter(rules_path, bus)
            await router.run()

            # Subscribe to the overridden subject
            frontier_sub = await bus.subscribe("heddle.tasks.classifier.frontier")

            process_task = asyncio.create_task(router.process_messages())

            # Publish with LOCAL tier — should be overridden to FRONTIER
            task = TaskMessage(
                worker_type="classifier",
                payload={"text": "test"},
                model_tier=ModelTier.LOCAL,
            )
            await bus.publish("heddle.tasks.incoming", task.model_dump(mode="json"))

            msg = await asyncio.wait_for(frontier_sub.__anext__(), timeout=2.0)
            received = TaskMessage(**msg)
            assert received.worker_type == "classifier"
            assert received.task_id == task.task_id

            process_task.cancel()
            try:
                await process_task
            except asyncio.CancelledError:
                pass
            await bus.close()
        finally:
            os.unlink(rules_path)


# ---------------------------------------------------------------------------
# Test 6: Orchestrator with empty decomposition
# ---------------------------------------------------------------------------


class TestOrchestratorEmptyDecomposition:
    """When the LLM returns an empty task list, the orchestrator treats it as
    a failure (no work to do = goal cannot be fulfilled)."""

    @pytest.mark.asyncio
    async def test_empty_plan_produces_failed_result(self):
        """An empty decomposition plan results in a FAILED status."""
        bus = InMemoryBus()
        await bus.connect()

        backend = MockBackend(
            decompose_response="[]",
            synthesis_response=json.dumps({"note": "No tasks needed"}),
        )

        config_path = _write_yaml(
            {
                "name": "empty-plan-orch",
                "timeout_seconds": 5,
                "max_concurrent_tasks": 5,
                "available_workers": [
                    {
                        "name": "summarizer",
                        "description": "Summarizes text",
                        "input_schema": {"type": "object", "required": ["text"]},
                        "default_model_tier": "local",
                    },
                ],
            }
        )

        try:
            actor = OrchestratorActor(
                actor_id="empty-plan",
                config_path=config_path,
                backend=backend,
                bus=bus,
            )

            goal = OrchestratorGoal(instruction="Nothing to do")
            result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

            await actor.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))

            final = await _get_result(result_sub, goal.goal_id)
            # Empty plan = no subtasks = orchestrator reports failure
            assert final["status"] == TaskStatus.FAILED.value

            await bus.close()
        finally:
            os.unlink(config_path)


# ---------------------------------------------------------------------------
# Test 7: Pipeline with 3-level dependency chain
# ---------------------------------------------------------------------------


class TestPipelineThreeLevelChain:
    """Three-stage pipeline: A -> B -> C, verifying data flows through all levels."""

    @pytest.mark.asyncio
    async def test_three_stage_data_propagation(self):
        """Data from stage A flows through B and reaches C correctly."""
        bus = InMemoryBus()
        await bus.connect()

        config_path = _write_yaml(
            {
                "name": "three-level-pipeline",
                "timeout_seconds": 5,
                "pipeline_stages": [
                    {
                        "name": "ingest",
                        "worker_type": "ingestor",
                        "model_tier": "local",
                        "input_mapping": {"source": "goal.context.source"},
                    },
                    {
                        "name": "transform",
                        "worker_type": "transformer",
                        "model_tier": "local",
                        "input_mapping": {"data": "ingest.output.raw_data"},
                    },
                    {
                        "name": "analyze",
                        "worker_type": "analyzer",
                        "model_tier": "local",
                        "input_mapping": {
                            "transformed": "transform.output.clean_data",
                        },
                    },
                ],
            }
        )

        try:
            pipeline = PipelineOrchestrator(
                actor_id="three-level",
                config_path=config_path,
                bus=bus,
            )

            goal = OrchestratorGoal(
                instruction="Full pipeline",
                context={"source": "data.csv"},
            )
            result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")
            execution_order = []

            worker_sub = await bus.subscribe("heddle.tasks.incoming")

            async def _worker():
                count = 0
                async for data in worker_sub:
                    task = TaskMessage(**data)
                    await asyncio.sleep(0.05)
                    execution_order.append(task.worker_type)

                    output_map = {
                        "ingestor": {"raw_data": "raw content from source"},
                        "transformer": {"clean_data": "cleaned and normalized"},
                        "analyzer": {"insights": ["trend_1", "trend_2"]},
                    }
                    output = output_map.get(task.worker_type, {"ok": True})

                    result = TaskResult(
                        task_id=task.task_id,
                        parent_task_id=task.parent_task_id,
                        worker_type=task.worker_type,
                        status=TaskStatus.COMPLETED,
                        output=output,
                        model_used="mock",
                        processing_time_ms=10,
                    )
                    await bus.publish(
                        f"heddle.results.{task.parent_task_id}",
                        result.model_dump(mode="json"),
                    )
                    count += 1
                    if count >= 3:
                        break
                await worker_sub.unsubscribe()

            worker_task = asyncio.create_task(_worker())
            await pipeline.handle_message(
                wrap("core.OrchestratorGoal", goal).model_dump(mode="json")
            )
            await worker_task

            # Verify sequential execution order
            assert execution_order == ["ingestor", "transformer", "analyzer"]

            final = await _get_result(result_sub, goal.goal_id)
            assert final["status"] == TaskStatus.COMPLETED.value
            output = final["output"]
            assert "ingest" in output
            assert "transform" in output
            assert "analyze" in output

            await bus.close()
        finally:
            os.unlink(config_path)


# ---------------------------------------------------------------------------
# Test 8: Pipeline with diamond dependency pattern
# ---------------------------------------------------------------------------


class TestPipelineDiamondDependency:
    """Diamond pattern: A -> (B, C) -> D. B and C run in parallel."""

    @pytest.mark.asyncio
    async def test_diamond_dependency_executes_correctly(self):
        """Diamond: ingest -> (text_analysis, meta_analysis) -> merge."""
        bus = InMemoryBus()
        await bus.connect()

        config_path = _write_yaml(
            {
                "name": "diamond-pipeline",
                "timeout_seconds": 5,
                "pipeline_stages": [
                    {
                        "name": "ingest",
                        "worker_type": "ingestor",
                        "model_tier": "local",
                        "input_mapping": {"source": "goal.context.source"},
                    },
                    {
                        "name": "text_analysis",
                        "worker_type": "text_analyzer",
                        "model_tier": "local",
                        "input_mapping": {
                            "text": "ingest.output.text",
                        },
                    },
                    {
                        "name": "meta_analysis",
                        "worker_type": "meta_analyzer",
                        "model_tier": "local",
                        "input_mapping": {
                            "metadata": "ingest.output.metadata",
                        },
                    },
                    {
                        "name": "merge",
                        "worker_type": "merger",
                        "model_tier": "local",
                        "input_mapping": {
                            "text_result": "text_analysis.output.result",
                            "meta_result": "meta_analysis.output.result",
                        },
                    },
                ],
            }
        )

        try:
            pipeline = PipelineOrchestrator(
                actor_id="diamond",
                config_path=config_path,
                bus=bus,
            )

            goal = OrchestratorGoal(
                instruction="Diamond analysis",
                context={"source": "doc.pdf"},
            )
            result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

            worker_sub = await bus.subscribe("heddle.tasks.incoming")

            async def _worker():
                count = 0
                async for data in worker_sub:
                    task = TaskMessage(**data)
                    await asyncio.sleep(0.05)

                    output_map = {
                        "ingestor": {"text": "doc text", "metadata": {"pages": 5}},
                        "text_analyzer": {"result": "text insights"},
                        "meta_analyzer": {"result": "meta insights"},
                        "merger": {"combined": "full analysis"},
                    }
                    output = output_map.get(task.worker_type, {"ok": True})

                    result = TaskResult(
                        task_id=task.task_id,
                        parent_task_id=task.parent_task_id,
                        worker_type=task.worker_type,
                        status=TaskStatus.COMPLETED,
                        output=output,
                        model_used="mock",
                        processing_time_ms=10,
                    )
                    await bus.publish(
                        f"heddle.results.{task.parent_task_id}",
                        result.model_dump(mode="json"),
                    )
                    count += 1
                    if count >= 4:
                        break
                await worker_sub.unsubscribe()

            worker_task = asyncio.create_task(_worker())
            await pipeline.handle_message(
                wrap("core.OrchestratorGoal", goal).model_dump(mode="json")
            )
            await worker_task

            final = await _get_result(result_sub, goal.goal_id)
            assert final["status"] == TaskStatus.COMPLETED.value
            output = final["output"]
            # All 4 stages should be in the output
            assert "ingest" in output
            assert "text_analysis" in output
            assert "meta_analysis" in output
            assert "merge" in output

            await bus.close()
        finally:
            os.unlink(config_path)
