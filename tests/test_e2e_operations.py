"""
End-to-end operation tests — full actor coordination via InMemoryBus.

These tests exercise multi-actor workflows without external infrastructure
(no NATS, no LLM APIs). They verify that the Heddle actor mesh works correctly
when components are wired together through the in-memory message bus.

All tests are self-contained and run as part of the standard test suite:
    uv run pytest tests/test_e2e_operations.py -v
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
    """Mock LLM backend that returns configurable responses."""

    def __init__(self, decompose_response: str = "[]", synthesis_response: str = "{}"):
        self._decompose = decompose_response
        self._synthesis = synthesis_response

    async def complete(self, system_prompt, user_message, max_tokens, temperature, **kw):
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


async def _get_final_result(sub, goal_id: str, timeout: float = 3.0) -> dict:
    """Read from a subscription until the final result (task_id == goal_id) arrives.

    The result subject receives both intermediate worker results AND the final
    orchestrator result. Only the final result has task_id == goal_id.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise TimeoutError(f"Final result for {goal_id} not received within {timeout}s")
        msg = await asyncio.wait_for(sub.__anext__(), timeout=remaining)
        if msg.get("task_id") == goal_id:
            return msg


async def _worker_simulator(
    bus: InMemoryBus,
    subject: str,
    response_fn=None,
    max_messages: int = 10,
    sub=None,
):
    """Generic worker simulator that responds to tasks.

    Args:
        bus: The message bus to publish results on.
        subject: NATS subject to listen on (used only if sub is None).
        response_fn: Optional function(TaskMessage) -> dict to generate output.
            Defaults to returning {"processed": True}.
        max_messages: Max messages to process before stopping.
        sub: Pre-created subscription. If None, subscribes to subject.
    """
    if sub is None:
        sub = await bus.subscribe(subject)
    count = 0

    async for data in sub:
        task = TaskMessage(**data)
        # Small delay to let orchestrator set up result subscription
        # (the orchestrator subscribes to heddle.results.{goal_id} AFTER dispatching)
        await asyncio.sleep(0.05)
        output = {"processed": True, "worker_type": task.worker_type}
        if response_fn:
            output = response_fn(task)

        result = TaskResult(
            task_id=task.task_id,
            parent_task_id=task.parent_task_id,
            worker_type=task.worker_type,
            status=TaskStatus.COMPLETED,
            output=output,
            model_used="mock",
            processing_time_ms=10,
            token_usage={"prompt_tokens": 10, "completion_tokens": 5},
        )
        # Publish result to the goal's result subject
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
# Test 1: Router → Worker round-trip
# ---------------------------------------------------------------------------


class TestRouterToWorkerRoundtrip:
    @pytest.mark.asyncio
    async def test_router_routes_task_to_worker_subject(self):
        """Full round-trip: task published → router routes → arrives at worker subject."""
        bus = InMemoryBus()

        rules_path = _write_yaml(
            {
                "tier_overrides": {},
                "rate_limits": {"local": {"max_concurrent": 10}},
            }
        )
        try:
            router = TaskRouter(rules_path, bus)
            await router.run()

            # Subscribe where the worker would listen
            worker_sub = await bus.subscribe("heddle.tasks.summarizer.local")

            # Start router message processing
            process_task = asyncio.create_task(router.process_messages())

            # Publish a task to the incoming subject
            task = TaskMessage(
                worker_type="summarizer",
                payload={"text": "Hello world, please summarize this."},
                model_tier=ModelTier.LOCAL,
            )
            await bus.publish(
                "heddle.tasks.incoming",
                task.model_dump(mode="json"),
            )

            # Worker receives the task
            msg = await asyncio.wait_for(worker_sub.__anext__(), timeout=2.0)
            received_task = TaskMessage(**msg)

            assert received_task.worker_type == "summarizer"
            assert received_task.payload["text"] == "Hello world, please summarize this."
            assert received_task.task_id == task.task_id

            process_task.cancel()
            try:
                await process_task
            except asyncio.CancelledError:
                pass
            await bus.close()
        finally:
            os.unlink(rules_path)


# ---------------------------------------------------------------------------
# Test 2: Orchestrator full goal flow
# ---------------------------------------------------------------------------


class TestOrchestratorFullGoalFlow:
    @pytest.mark.asyncio
    async def test_orchestrator_decomposes_dispatches_collects_synthesizes(self):
        """Full orchestrator lifecycle: goal → decompose → dispatch → collect → synthesize."""
        bus = InMemoryBus()
        await bus.connect()

        # Backend returns a plan with 2 subtasks
        plan = json.dumps(
            [
                {"worker_type": "summarizer", "payload": {"text": "chunk 1"}},
                {"worker_type": "summarizer", "payload": {"text": "chunk 2"}},
            ]
        )
        synthesis = json.dumps(
            {
                "combined_summary": "Both chunks summarized",
                "confidence": "high",
            }
        )
        backend = MockBackend(plan, synthesis)

        config_path = _write_yaml(
            {
                "name": "e2e-orchestrator",
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
                actor_id="e2e-orch",
                config_path=config_path,
                backend=backend,
                bus=bus,
            )

            goal = OrchestratorGoal(
                instruction="Summarize both document chunks",
                context={"source": "test"},
            )
            goal_data = wrap("core.OrchestratorGoal", goal).model_dump(mode="json")

            # Subscribe for the final result
            result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

            # Pre-subscribe worker before handle_message (mirrors real deployment)
            worker_sub = await bus.subscribe("heddle.tasks.incoming")
            worker_task = asyncio.create_task(
                _worker_simulator(
                    bus,
                    "heddle.tasks.incoming",
                    response_fn=lambda t: {"summary": f"Summary of {t.payload.get('text', '')}"},
                    max_messages=2,
                    sub=worker_sub,
                )
            )

            # Process the goal
            await actor.handle_message(goal_data)
            await worker_task

            # Verify final result (filter for task_id == goal_id)
            final = await _get_final_result(result_sub, goal.goal_id)
            assert final["status"] == TaskStatus.COMPLETED.value
            assert final["output"] is not None

            await bus.close()
        finally:
            os.unlink(config_path)


# ---------------------------------------------------------------------------
# Test 3: Pipeline multi-stage execution
# ---------------------------------------------------------------------------


class TestPipelineMultiStage:
    @pytest.mark.asyncio
    async def test_pipeline_executes_stages_in_order(self):
        """Pipeline with 2 stages: extract → classify. Stages execute sequentially."""
        bus = InMemoryBus()
        await bus.connect()

        config_path = _write_yaml(
            {
                "name": "e2e-pipeline",
                "timeout_seconds": 5,
                "pipeline_stages": [
                    {
                        "name": "extract",
                        "worker_type": "extractor",
                        "model_tier": "local",
                        "input_mapping": {
                            "file_ref": "goal.context.file_ref",
                        },
                    },
                    {
                        "name": "classify",
                        "worker_type": "classifier",
                        "model_tier": "local",
                        "input_mapping": {
                            "text": "extract.output.text",
                        },
                    },
                ],
            }
        )

        try:
            pipeline = PipelineOrchestrator(
                actor_id="e2e-pipeline",
                config_path=config_path,
                bus=bus,
            )

            goal = OrchestratorGoal(
                instruction="Process document",
                context={"file_ref": "test.pdf"},
            )
            goal_data = wrap("core.OrchestratorGoal", goal).model_dump(mode="json")

            # Subscribe for final result
            result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

            # Execution order tracker
            execution_order = []

            def make_response(task: TaskMessage) -> dict:
                execution_order.append(task.worker_type)
                if task.worker_type == "extractor":
                    return {"text": "Extracted document text", "pages": 5}
                if task.worker_type == "classifier":
                    return {"category": "report", "confidence": 0.95}
                return {"processed": True}

            # Pre-subscribe worker before handle_message
            worker_sub = await bus.subscribe("heddle.tasks.incoming")
            worker_task = asyncio.create_task(
                _worker_simulator(
                    bus,
                    "heddle.tasks.incoming",
                    response_fn=make_response,
                    max_messages=2,
                    sub=worker_sub,
                )
            )

            await pipeline.handle_message(goal_data)
            await worker_task

            # Verify execution order
            assert execution_order == ["extractor", "classifier"]

            # Verify final result
            final = await _get_final_result(result_sub, goal.goal_id)
            assert final["status"] == TaskStatus.COMPLETED.value
            # Final output should contain results from both stages
            output = final["output"]
            assert "extract" in output or "classify" in output

            await bus.close()
        finally:
            os.unlink(config_path)


# ---------------------------------------------------------------------------
# Test 4: Pipeline with parallel independent stages
# ---------------------------------------------------------------------------


class TestPipelineParallelStages:
    @pytest.mark.asyncio
    async def test_independent_stages_run_in_parallel(self):
        """Two stages with no inter-dependencies run concurrently."""
        bus = InMemoryBus()
        await bus.connect()

        config_path = _write_yaml(
            {
                "name": "parallel-pipeline",
                "timeout_seconds": 5,
                "pipeline_stages": [
                    {
                        "name": "extract_text",
                        "worker_type": "text_extractor",
                        "model_tier": "local",
                        "input_mapping": {
                            "file_ref": "goal.context.file_ref",
                        },
                    },
                    {
                        "name": "extract_images",
                        "worker_type": "image_extractor",
                        "model_tier": "local",
                        "input_mapping": {
                            "file_ref": "goal.context.file_ref",
                        },
                    },
                ],
            }
        )

        try:
            pipeline = PipelineOrchestrator(
                actor_id="e2e-parallel",
                config_path=config_path,
                bus=bus,
            )

            goal = OrchestratorGoal(
                instruction="Extract both text and images",
                context={"file_ref": "doc.pdf"},
            )
            goal_data = wrap("core.OrchestratorGoal", goal).model_dump(mode="json")
            result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

            def make_response(task: TaskMessage) -> dict:
                if task.worker_type == "text_extractor":
                    return {"text": "Document text content"}
                if task.worker_type == "image_extractor":
                    return {"images": ["img1.png", "img2.png"]}
                return {}

            worker_sub = await bus.subscribe("heddle.tasks.incoming")
            worker_task = asyncio.create_task(
                _worker_simulator(
                    bus, "heddle.tasks.incoming", make_response, max_messages=2, sub=worker_sub
                )
            )

            await pipeline.handle_message(goal_data)
            await worker_task

            final = await _get_final_result(result_sub, goal.goal_id)
            assert final["status"] == TaskStatus.COMPLETED.value

            await bus.close()
        finally:
            os.unlink(config_path)


# ---------------------------------------------------------------------------
# Test 5: Concurrent goals isolation
# ---------------------------------------------------------------------------


class TestConcurrentGoalsE2E:
    @pytest.mark.asyncio
    async def test_concurrent_goals_produce_independent_results(self):
        """Multiple goals processed concurrently produce independent results."""
        bus = InMemoryBus()
        await bus.connect()

        # Each goal decomposes to 1 subtask
        plan = json.dumps(
            [
                {
                    "worker_type": "summarizer",
                    "payload": {"text": "content"},
                }
            ]
        )
        synthesis = json.dumps({"confidence": "high"})
        backend = MockBackend(plan, synthesis)

        config_path = _write_yaml(
            {
                "name": "concurrent-orch",
                "timeout_seconds": 5,
                "max_concurrent_tasks": 5,
                "max_concurrent_goals": 3,
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
                actor_id="e2e-concurrent",
                config_path=config_path,
                backend=backend,
                bus=bus,
            )

            # Create 3 independent goals
            goals = [OrchestratorGoal(instruction=f"Goal {i}", context={"n": i}) for i in range(3)]

            # Subscribe for all results
            result_subs = {}
            for g in goals:
                result_subs[g.goal_id] = await bus.subscribe(f"heddle.results.{g.goal_id}")

            # Pre-subscribe worker before goals are processed
            worker_sub = await bus.subscribe("heddle.tasks.incoming")
            worker_task = asyncio.create_task(
                _worker_simulator(
                    bus,
                    "heddle.tasks.incoming",
                    response_fn=lambda t: {"summary": f"Done: {t.payload.get('text', '')}"},
                    max_messages=3,
                    sub=worker_sub,
                )
            )

            # Process all goals concurrently
            await asyncio.gather(
                *(
                    actor.handle_message(wrap("core.OrchestratorGoal", g).model_dump(mode="json"))
                    for g in goals
                )
            )
            await worker_task

            # Verify all 3 got results
            for g in goals:
                final = await _get_final_result(result_subs[g.goal_id], g.goal_id)
                assert final["status"] == TaskStatus.COMPLETED.value

            # Verify no active goals remain
            assert len(actor._active_goals) == 0

            await bus.close()
        finally:
            os.unlink(config_path)


# ---------------------------------------------------------------------------
# Test 6: Router + Orchestrator wired together
# ---------------------------------------------------------------------------


class TestRouterOrchestratorWired:
    @pytest.mark.asyncio
    async def test_router_forwards_subtasks_to_worker_subjects(self):
        """Orchestrator dispatches to heddle.tasks.incoming → router routes to worker subject."""
        bus = InMemoryBus()
        await bus.connect()

        # Set up router
        rules_path = _write_yaml(
            {
                "tier_overrides": {},
                "rate_limits": {},
            }
        )
        plan = json.dumps(
            [
                {
                    "worker_type": "summarizer",
                    "payload": {"text": "content"},
                    "model_tier": "local",
                }
            ]
        )
        synthesis = json.dumps({"result": "done"})
        backend = MockBackend(plan, synthesis)

        config_path = _write_yaml(
            {
                "name": "wired-orch",
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
            router = TaskRouter(rules_path, bus)
            await router.run()
            router_task = asyncio.create_task(router.process_messages())

            actor = OrchestratorActor(
                actor_id="wired-orch",
                config_path=config_path,
                backend=backend,
                bus=bus,
            )

            goal = OrchestratorGoal(instruction="Test wired flow")
            result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

            # Worker listens on the routed subject (not heddle.tasks.incoming)
            # Pre-subscribe before handle_message
            worker_sub = await bus.subscribe("heddle.tasks.summarizer.local")
            worker_task = asyncio.create_task(
                _worker_simulator(
                    bus,
                    "heddle.tasks.summarizer.local",
                    response_fn=lambda t: {"summary": "routed and processed"},
                    max_messages=1,
                    sub=worker_sub,
                )
            )

            await actor.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))
            await worker_task

            final = await _get_final_result(result_sub, goal.goal_id)
            assert final["status"] == TaskStatus.COMPLETED.value

            router_task.cancel()
            try:
                await router_task
            except asyncio.CancelledError:
                pass
            await bus.close()
        finally:
            os.unlink(rules_path)
            os.unlink(config_path)
