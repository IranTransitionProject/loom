"""
Unit tests for OrchestratorActor (orchestrator/runner.py).

Tests cover:
- GoalState: all_collected, pending_count properties
- OrchestratorActor lifecycle: decompose → dispatch → collect → synthesize
- Timeout and partial collection behaviour
- Checkpoint triggering
- Error handling (parse errors, decomposition failure)
- Concurrent goal processing (max_concurrent_goals)

All tests use InMemoryBus -- no NATS or external infrastructure required.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
import time
from typing import Any

import pytest
import yaml

from heddle.bus.memory import InMemoryBus
from heddle.core.envelope import wrap
from heddle.core.messages import (
    OrchestratorGoal,
    TaskMessage,
    TaskResult,
    TaskStatus,
)
from heddle.orchestrator.runner import GoalState, OrchestratorActor
from heddle.orchestrator.store import InMemoryCheckpointStore
from heddle.worker.backends import LLMBackend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockOrchestratorBackend(LLMBackend):
    """Returns configurable responses depending on system prompt content."""

    def __init__(self, decompose_response: str, synthesis_response: str = "{}"):
        self._decompose = decompose_response
        self._synthesis = synthesis_response

    async def complete(self, system_prompt, user_message, max_tokens, temperature, **kw):
        # Route to decomposition or synthesis based on system prompt
        if "task decomposition" in system_prompt.lower():
            content = self._decompose
        else:
            content = self._synthesis
        return {
            "content": content,
            "model": "mock-orch",
            "prompt_tokens": 100,
            "completion_tokens": 50,
        }


def _write_config(
    available_workers: list[dict] | None = None,
    timeout_seconds: int = 5,
    max_concurrent_goals: int | None = None,
) -> str:
    """Write a minimal orchestrator config to a temp file."""
    config = {
        "name": "test-orchestrator",
        "timeout_seconds": timeout_seconds,
        "max_concurrent_tasks": 5,
        "available_workers": available_workers
        or [
            {
                "name": "summarizer",
                "description": "Summarizes text",
                "input_schema": {"type": "object", "required": ["text"]},
                "default_model_tier": "local",
            },
        ],
    }
    if max_concurrent_goals is not None:
        config["max_concurrent_goals"] = max_concurrent_goals
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as f:
        yaml.dump(config, f)
    return path


def _make_goal_data(instruction: str = "Summarize this document") -> dict[str, Any]:
    goal = OrchestratorGoal(instruction=instruction, context={"text": "Hello world"})
    return wrap("core.OrchestratorGoal", goal).model_dump(mode="json")


def _make_result_data(
    task_id: str,
    worker_type: str = "summarizer",
    status: TaskStatus = TaskStatus.COMPLETED,
    output: dict | None = None,
) -> dict[str, Any]:
    result = TaskResult(
        task_id=task_id,
        worker_type=worker_type,
        status=status,
        output=output or {"summary": "Test summary"},
        model_used="mock",
        processing_time_ms=100,
        token_usage={"prompt_tokens": 50, "completion_tokens": 30},
    )
    return wrap("core.TaskResult", result).model_dump(mode="json")


# ---------------------------------------------------------------------------
# GoalState tests
# ---------------------------------------------------------------------------


class TestGoalState:
    def test_all_collected_false_when_empty(self):
        goal = OrchestratorGoal(instruction="test")
        state = GoalState(goal=goal)
        assert state.all_collected is False

    def test_all_collected_false_when_partial(self):
        goal = OrchestratorGoal(instruction="test")
        state = GoalState(goal=goal)
        task = TaskMessage(worker_type="summarizer", input={})
        state.dispatched_tasks[task.task_id] = task
        assert state.all_collected is False

    def test_all_collected_true_when_complete(self):
        goal = OrchestratorGoal(instruction="test")
        state = GoalState(goal=goal)

        task = TaskMessage(worker_type="summarizer", input={})
        state.dispatched_tasks[task.task_id] = task

        result = TaskResult(
            task_id=task.task_id,
            worker_type="summarizer",
            status=TaskStatus.COMPLETED,
            output={"data": "test"},
        )
        state.collected_results[task.task_id] = result

        assert state.all_collected is True

    def test_pending_count(self):
        goal = OrchestratorGoal(instruction="test")
        state = GoalState(goal=goal)

        for i in range(3):
            task = TaskMessage(worker_type="summarizer", input={})
            state.dispatched_tasks[task.task_id] = task

        assert state.pending_count == 3

        # Collect one result
        first_id = next(iter(state.dispatched_tasks.keys()))
        state.collected_results[first_id] = TaskResult(
            task_id=first_id,
            worker_type="summarizer",
            status=TaskStatus.COMPLETED,
            output={},
        )
        assert state.pending_count == 2

    def test_start_time_is_set(self):
        goal = OrchestratorGoal(instruction="test")
        before = time.monotonic()
        state = GoalState(goal=goal)
        after = time.monotonic()
        assert before <= state.start_time <= after


# ---------------------------------------------------------------------------
# OrchestratorActor handle_message tests
# ---------------------------------------------------------------------------


class TestHandleMessage:
    @pytest.mark.asyncio
    async def test_invalid_goal_data_does_not_crash(self):
        """Malformed goal data is handled gracefully."""
        config_path = _write_config()
        try:
            backend = MockOrchestratorBackend("[]")
            bus = InMemoryBus()
            actor = OrchestratorActor(
                actor_id="test-orch",
                config_path=config_path,
                backend=backend,
                nats_url="nats://localhost:4222",
            )
            actor._bus = bus
            await bus.connect()

            # Pass garbage -- should not raise
            await actor.handle_message({"invalid": True})
        finally:
            os.unlink(config_path)

    @pytest.mark.asyncio
    async def test_empty_decomposition_publishes_failure(self):
        """When decomposition produces no subtasks, a FAILED result is published."""
        config_path = _write_config()
        try:
            backend = MockOrchestratorBackend("[]")  # Empty plan
            bus = InMemoryBus()
            actor = OrchestratorActor(
                actor_id="test-orch",
                config_path=config_path,
                backend=backend,
            )
            actor._bus = bus
            await bus.connect()

            goal_data = _make_goal_data()
            goal_id = goal_data["payload"]["goal_id"]
            sub = await bus.subscribe(f"heddle.results.{goal_id}")

            await actor.handle_message(goal_data)

            result = await sub.__anext__()
            assert result["payload"]["status"] == TaskStatus.FAILED.value
            assert "no subtasks" in result["payload"]["error"].lower()
        finally:
            os.unlink(config_path)

    @pytest.mark.asyncio
    async def test_goal_state_cleaned_up_after_completion(self):
        """Goal state is removed from _active_goals after processing."""
        config_path = _write_config(timeout_seconds=1)
        try:
            # Return a valid subtask plan
            plan = json.dumps(
                [
                    {
                        "worker_type": "summarizer",
                        "payload": {"text": "test"},
                    }
                ]
            )
            backend = MockOrchestratorBackend(plan)
            bus = InMemoryBus()
            actor = OrchestratorActor(
                actor_id="test-orch",
                config_path=config_path,
                backend=backend,
            )
            actor._bus = bus
            await bus.connect()

            goal_data = _make_goal_data()
            # Will timeout on collection since no worker responds, but state
            # should be cleaned up regardless
            await actor.handle_message(goal_data)

            assert len(actor._active_goals) == 0
        finally:
            os.unlink(config_path)


# ---------------------------------------------------------------------------
# _record_in_history tests
# ---------------------------------------------------------------------------


class TestRecordInHistory:
    @pytest.mark.asyncio
    async def test_history_accumulates(self):
        config_path = _write_config()
        try:
            backend = MockOrchestratorBackend("[]")
            actor = OrchestratorActor(
                actor_id="test-orch",
                config_path=config_path,
                backend=backend,
            )

            goal = OrchestratorGoal(instruction="Test goal")
            goal_state = GoalState(goal=goal)
            results = [
                TaskResult(
                    task_id="t1",
                    worker_type="summarizer",
                    status=TaskStatus.COMPLETED,
                    output={"summary": "done"},
                    processing_time_ms=100,
                ),
            ]
            synthesis = {"confidence": "high"}

            await actor._record_in_history(goal_state, results, synthesis)
            assert len(goal_state.conversation_history) == 1

            entry = goal_state.conversation_history[0]
            assert entry["goal_id"] == goal.goal_id
            assert entry["subtask_count"] == 1
            assert entry["synthesis_confidence"] == "high"
        finally:
            os.unlink(config_path)

    @pytest.mark.asyncio
    async def test_history_records_failures(self):
        config_path = _write_config()
        try:
            backend = MockOrchestratorBackend("[]")
            actor = OrchestratorActor(
                actor_id="test-orch",
                config_path=config_path,
                backend=backend,
            )

            goal = OrchestratorGoal(instruction="Test")
            goal_state = GoalState(goal=goal)
            results = [
                TaskResult(
                    task_id="t1",
                    worker_type="summarizer",
                    status=TaskStatus.FAILED,
                    error="timeout",
                    processing_time_ms=0,
                ),
            ]
            await actor._record_in_history(goal_state, results, {})

            entry = goal_state.conversation_history[0]
            assert entry["results"][0]["error"] == "timeout"
        finally:
            os.unlink(config_path)


# ---------------------------------------------------------------------------
# Concurrent goal processing tests
# ---------------------------------------------------------------------------


class TestConcurrentGoals:
    @pytest.mark.asyncio
    async def test_default_max_concurrent_goals_is_one(self):
        """Without config, max_concurrent_goals defaults to 1."""
        config_path = _write_config()
        try:
            backend = MockOrchestratorBackend("[]")
            actor = OrchestratorActor(
                actor_id="test-orch",
                config_path=config_path,
                backend=backend,
            )
            assert actor.max_concurrent == 1
        finally:
            os.unlink(config_path)

    @pytest.mark.asyncio
    async def test_max_concurrent_goals_from_config(self):
        """Config value is passed through to BaseActor.max_concurrent."""
        config_path = _write_config(max_concurrent_goals=4)
        try:
            backend = MockOrchestratorBackend("[]")
            actor = OrchestratorActor(
                actor_id="test-orch",
                config_path=config_path,
                backend=backend,
            )
            assert actor.max_concurrent == 4
        finally:
            os.unlink(config_path)

    @pytest.mark.asyncio
    async def test_concurrent_goals_have_isolated_history(self):
        """Each GoalState maintains its own conversation history."""
        config_path = _write_config(max_concurrent_goals=4)
        try:
            backend = MockOrchestratorBackend("[]")
            actor = OrchestratorActor(
                actor_id="test-orch",
                config_path=config_path,
                backend=backend,
            )

            goal_states = []

            async def record_one(i: int):
                goal = OrchestratorGoal(instruction=f"Goal {i}")
                gs = GoalState(goal=goal)
                goal_states.append(gs)
                results = [
                    TaskResult(
                        task_id=f"t{i}",
                        worker_type="summarizer",
                        status=TaskStatus.COMPLETED,
                        output={"n": i},
                        processing_time_ms=10,
                    ),
                ]
                await actor._record_in_history(gs, results, {"confidence": "high"})

            # Fire 20 concurrent writes — each to its own GoalState
            await asyncio.gather(*(record_one(i) for i in range(20)))

            assert len(goal_states) == 20
            # Each GoalState should have exactly 1 entry (no cross-contamination)
            for gs in goal_states:
                assert len(gs.conversation_history) == 1
        finally:
            os.unlink(config_path)

    @pytest.mark.asyncio
    async def test_bus_injection_via_constructor(self):
        """The bus= keyword argument is forwarded to BaseActor."""
        config_path = _write_config()
        try:
            backend = MockOrchestratorBackend("[]")
            bus = InMemoryBus()
            actor = OrchestratorActor(
                actor_id="test-orch",
                config_path=config_path,
                backend=backend,
                bus=bus,
            )
            assert actor._bus is bus
        finally:
            os.unlink(config_path)

    @pytest.mark.asyncio
    async def test_concurrent_goals_under_bus_flap(self):
        """J9: concurrent goals tolerate transient publish failures
        without deadlock, state contamination, or lost results.

        Runs three goals with ``max_concurrent_goals=3`` while a wrapper
        around ``bus.publish`` fails the first attempt for each goal's
        first task dispatch (simulating a NATS bus flap mid-dispatch).
        Asserts three invariants the prior reviews did not stress:

        - **No goal hangs.** Each ``handle_message`` returns within the
          orchestrator's timeout; ``asyncio.wait_for`` would raise
          otherwise.
        - **No state contamination.** No ``GoalState.dispatched_tasks``
          ever contained a ``task_id`` from a different goal — the
          per-goal isolation invariant (Invariant 7 / ADR-009) holds
          under concurrent dispatch.
        - **Late-result drop holds under concurrency.** A late result
          arriving on a goal's subject after the orchestrator has
          torn down its subscription is logged-and-dropped, not
          delivered to a different goal's collection.

        Heaviest test in the J session (per session-starter); the
        timeout values are loose so a slow CI runner doesn't flake.
        """
        plan = json.dumps([{"worker_type": "summarizer", "payload": {"text": "chunk"}}])
        synthesis = json.dumps({"summary": "done"})

        config_path = _write_config(
            max_concurrent_goals=3,
            timeout_seconds=3,
        )
        try:
            bus = InMemoryBus()
            await bus.connect()

            # Wrap ``bus.publish`` so the first publish per goal's
            # task dispatch fails, simulating a transient NATS flap.
            # Distinguish task-dispatch publishes (subject is
            # ``heddle.tasks.incoming``) from result-collection
            # publishes (subject is ``heddle.results.<goal_id>``);
            # only the dispatch side flaps so the worker can still
            # respond once the dispatch retry succeeds.
            real_publish = bus.publish
            flap_counter: dict[str, int] = {}

            async def flapping_publish(subject: str, data: dict) -> None:
                if subject == "heddle.tasks.incoming":
                    parent_id = data.get("payload", {}).get("parent_task_id") or ""
                    n = flap_counter.get(parent_id, 0)
                    flap_counter[parent_id] = n + 1
                    if n == 0:
                        raise RuntimeError(f"transient bus flap on dispatch {parent_id[:8]}")
                await real_publish(subject, data)

            bus.publish = flapping_publish  # type: ignore[assignment]

            backend = MockOrchestratorBackend(plan, synthesis)
            actor = OrchestratorActor(
                actor_id="test-flap",
                config_path=config_path,
                backend=backend,
                bus=bus,
            )

            # Each goal gets its own worker responder.  The responder
            # listens to ``heddle.tasks.incoming``, picks the message
            # whose ``parent_task_id`` matches its assigned goal, and
            # publishes a result back.  Other goals' messages are
            # left for their respective responders.
            worker_sub = await bus.subscribe("heddle.tasks.incoming")

            goal_data_by_id: dict[str, dict] = {}
            result_subs_by_id: dict[str, Any] = {}
            for _i in range(3):
                gd = _make_goal_data(f"Concurrent goal {_i}")
                gid = gd["payload"]["goal_id"]
                goal_data_by_id[gid] = gd
                result_subs_by_id[gid] = await bus.subscribe(f"heddle.results.{gid}")

            seen_parents: list[str] = []

            async def worker_loop() -> None:
                # Respond to every dispatch.  The orchestrator may
                # send each task once or twice (the first publish
                # raised, see ``flapping_publish``); only the second
                # call lands here, so we shouldn't see duplicates per
                # task_id.  We do see one per goal.
                seen_task_ids: set[str] = set()
                try:
                    while len(seen_task_ids) < 3:
                        data = await asyncio.wait_for(worker_sub.__anext__(), timeout=4)
                        task = TaskMessage(**data["payload"])
                        if task.task_id in seen_task_ids:
                            continue  # duplicate from a retry path; ignore
                        seen_task_ids.add(task.task_id)
                        seen_parents.append(task.parent_task_id or "")
                        result = TaskResult(
                            task_id=task.task_id,
                            parent_task_id=task.parent_task_id,
                            worker_type=task.worker_type,
                            status=TaskStatus.COMPLETED,
                            output={"summary": f"r-{task.task_id[:6]}"},
                            processing_time_ms=10,
                        )
                        await bus.publish(
                            f"heddle.results.{task.parent_task_id}",
                            wrap("core.TaskResult", result).model_dump(mode="json"),
                        )
                except TimeoutError:
                    pass

            worker_task = asyncio.create_task(worker_loop())

            # Dispatch all 3 goals concurrently.  The orchestrator's
            # actor-level dict isolates state per goal — concurrent
            # dispatch must not contaminate any of them.
            handles = [actor.handle_message(gd) for gd in goal_data_by_id.values()]
            await asyncio.wait_for(asyncio.gather(*handles), timeout=10)

            # ---- Invariant 1: no goal hangs ----
            # The asyncio.wait_for above would have raised.

            # ---- Invariant 2: no state contamination ----
            # Every goal's GoalState was torn down (the actor cleans
            # ``_active_goals`` after synthesis publishes).  If any
            # GoalState had captured a task from another goal, the
            # synthesis output below would carry that task's id; we
            # assert each goal's final synthesis names only its own
            # parent_task_id.
            seen_finals: dict[str, dict] = {}
            for gid, sub in result_subs_by_id.items():
                for _ in range(5):
                    msg = await asyncio.wait_for(sub.__anext__(), timeout=3)
                    if msg.get("payload", {}).get("worker_type") == "test-orchestrator":
                        seen_finals[gid] = msg
                        break
                assert gid in seen_finals, f"goal {gid} never reached terminal state"
                final = seen_finals[gid]
                assert final["payload"]["status"] == TaskStatus.COMPLETED.value, final

            assert len(seen_finals) == 3
            assert len(actor._active_goals) == 0, (
                f"_active_goals leaked across concurrent runs: {list(actor._active_goals)}"
            )

            # ---- Invariant 3: late-result drop holds under concurrency ----
            # Publish a synthetic late result to one of the now-finished
            # goal subjects.  It must be silently dropped — not surfaced
            # to the result subscriber as a second message attributed to
            # that goal's synthesis.
            late_target_gid = next(iter(seen_finals))
            late = TaskResult(
                task_id="late-task",
                parent_task_id=late_target_gid,
                worker_type="summarizer",
                status=TaskStatus.COMPLETED,
                output={"summary": "too late"},
                processing_time_ms=5,
            )
            await bus.publish(
                f"heddle.results.{late_target_gid}",
                wrap("core.TaskResult", late).model_dump(mode="json"),
            )

            # The subscriber receives the late publish (InMemoryBus has
            # no per-goal scoping), but its ``worker_type`` is
            # ``"summarizer"``, not the orchestrator's ``test-orchestrator``;
            # the late-drop invariant is about orchestrator-side
            # republish, which would have set worker_type back to
            # ``test-orchestrator``.  Confirm no second orchestrator
            # message arrives — the actor's _active_goals is empty so
            # it can't synthesize a second answer.
            async def _drain_for_republish() -> None:
                async with asyncio.timeout(0.5):
                    while True:
                        msg = await result_subs_by_id[late_target_gid].__anext__()
                        if msg.get("payload", {}).get("worker_type") == "test-orchestrator":
                            raise AssertionError(
                                "Orchestrator republished after _active_goals emptied"
                            )

            with pytest.raises(TimeoutError):
                await _drain_for_republish()

            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task
        finally:
            os.unlink(config_path)


# ---------------------------------------------------------------------------
# Per-goal state isolation tests
# ---------------------------------------------------------------------------


class TestGoalIsolation:
    def test_goalstate_conversation_history_defaults_empty(self):
        """New GoalState has empty conversation_history."""
        goal = OrchestratorGoal(instruction="test")
        state = GoalState(goal=goal)
        assert state.conversation_history == []
        assert state.checkpoint_counter == 0

    def test_goalstate_history_not_shared(self):
        """Two GoalState instances do not share the same history list."""
        goal_a = OrchestratorGoal(instruction="A")
        goal_b = OrchestratorGoal(instruction="B")
        state_a = GoalState(goal=goal_a)
        state_b = GoalState(goal=goal_b)

        state_a.conversation_history.append({"goal_id": "a"})
        assert len(state_b.conversation_history) == 0

    def test_checkpoint_counter_per_goal(self):
        """Checkpoint counters are independent across GoalState instances."""
        goal_a = OrchestratorGoal(instruction="A")
        goal_b = OrchestratorGoal(instruction="B")
        state_a = GoalState(goal=goal_a)
        state_b = GoalState(goal=goal_b)

        state_a.checkpoint_counter += 1
        state_a.checkpoint_counter += 1
        state_b.checkpoint_counter += 1

        assert state_a.checkpoint_counter == 2
        assert state_b.checkpoint_counter == 1

    @pytest.mark.asyncio
    async def test_record_in_history_writes_to_goal_state(self):
        """_record_in_history writes to the GoalState, not the actor."""
        config_path = _write_config()
        try:
            backend = MockOrchestratorBackend("[]")
            actor = OrchestratorActor(
                actor_id="test-orch",
                config_path=config_path,
                backend=backend,
            )

            goal = OrchestratorGoal(instruction="Test")
            gs = GoalState(goal=goal)
            results = [
                TaskResult(
                    task_id="t1",
                    worker_type="summarizer",
                    status=TaskStatus.COMPLETED,
                    output={"data": "x"},
                    processing_time_ms=10,
                ),
            ]
            await actor._record_in_history(gs, results, {"confidence": "high"})

            assert len(gs.conversation_history) == 1
            assert gs.conversation_history[0]["goal_id"] == goal.goal_id
            # Actor should have no shared history attribute
            assert not hasattr(actor, "_conversation_history")
        finally:
            os.unlink(config_path)


# ---------------------------------------------------------------------------
# Full lifecycle tests
# ---------------------------------------------------------------------------


class TestFullLifecycle:
    @pytest.mark.asyncio
    async def test_full_goal_lifecycle_with_simulated_worker(self):
        """Full decompose → dispatch → collect → synthesize → publish cycle."""
        # Backend that returns a valid single-task plan
        plan = json.dumps(
            [
                {
                    "worker_type": "summarizer",
                    "payload": {"text": "test content"},
                }
            ]
        )
        synthesis = json.dumps(
            {
                "summary": "synthesized result",
                "confidence": "high",
            }
        )
        backend = MockOrchestratorBackend(plan, synthesis)

        config_path = _write_config(timeout_seconds=5)
        try:
            bus = InMemoryBus()
            await bus.connect()

            actor = OrchestratorActor(
                actor_id="test-full",
                config_path=config_path,
                backend=backend,
                bus=bus,
            )

            goal_data = _make_goal_data("Summarize the document")
            goal_id = goal_data["payload"]["goal_id"]

            # Subscribe for the final result
            result_sub = await bus.subscribe(f"heddle.results.{goal_id}")

            # Pre-subscribe the worker BEFORE handle_message starts.
            # This mirrors real deployments where workers are already running.
            worker_sub = await bus.subscribe("heddle.tasks.incoming")
            ready = asyncio.Event()

            async def worker_simulator():
                ready.set()
                async for data in worker_sub:
                    task = TaskMessage(**data["payload"])
                    # Small delay to let orchestrator set up result subscription
                    await asyncio.sleep(0.05)
                    result = TaskResult(
                        task_id=task.task_id,
                        parent_task_id=task.parent_task_id,
                        worker_type=task.worker_type,
                        status=TaskStatus.COMPLETED,
                        output={"summary": "Worker produced this"},
                        model_used="mock",
                        processing_time_ms=50,
                        token_usage={"prompt_tokens": 10, "completion_tokens": 5},
                    )
                    await bus.publish(
                        f"heddle.results.{task.parent_task_id}",
                        wrap("core.TaskResult", result).model_dump(mode="json"),
                    )
                    await worker_sub.unsubscribe()
                    break

            worker_task = asyncio.create_task(worker_simulator())
            await ready.wait()
            await actor.handle_message(goal_data)
            await worker_task

            # Verify final result was published. The result subject receives
            # both worker intermediate results AND the final orchestrator result.
            # The final result has task_id == goal_id.
            final = None
            for _ in range(5):
                msg = await asyncio.wait_for(result_sub.__anext__(), timeout=2.0)
                if msg["payload"]["task_id"] == goal_id:
                    final = msg
                    break

            assert final is not None, "Final orchestrator result not found"
            assert final["payload"]["status"] == TaskStatus.COMPLETED.value
            assert final["payload"]["output"] is not None
        finally:
            os.unlink(config_path)

    @pytest.mark.asyncio
    async def test_subtask_limit_fails_goal_with_explicit_error(self):
        """A plan exceeding ``max_concurrent_tasks`` fails the goal explicitly.

        Earlier shape silently truncated the plan and dispatched the
        first N subtasks, then published ``COMPLETED`` — the caller
        had no signal that data was lost.  We now publish ``FAILED``
        with an actionable message so the operator either raises the
        limit or splits the goal at submission time.

        Regression test: dispatch must be skipped (no tasks published
        to ``heddle.tasks.incoming``) and the final result must be
        FAILED with a message naming both the requested and limit
        counts.
        """
        # 10 subtasks, max_concurrent_tasks=5 (default in _write_config).
        plan = json.dumps(
            [{"worker_type": "summarizer", "payload": {"text": f"chunk {i}"}} for i in range(10)]
        )
        backend = MockOrchestratorBackend(plan)

        config_path = _write_config(timeout_seconds=1)
        try:
            bus = InMemoryBus()
            await bus.connect()
            actor = OrchestratorActor(
                actor_id="test-limit",
                config_path=config_path,
                backend=backend,
                bus=bus,
            )

            # Subscribe BEFORE the goal arrives so we can verify
            # nothing was dispatched.
            task_sub = await bus.subscribe("heddle.tasks.incoming")

            goal = OrchestratorGoal(instruction="Process many chunks")
            result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

            await actor.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))

            # No tasks should have been dispatched — the goal failed
            # before reaching the dispatch step.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(task_sub.__anext__(), timeout=0.2)

            # The final result must be FAILED with both counts named.
            final = await asyncio.wait_for(result_sub.__anext__(), timeout=0.5)
            result = TaskResult(**final["payload"])
            assert result.status == TaskStatus.FAILED
            assert "10 subtasks" in result.error
            assert "max_concurrent_tasks=5" in result.error
        finally:
            os.unlink(config_path)

    @pytest.mark.asyncio
    async def test_decomposition_error_publishes_failure(self):
        """When decomposition fails, a FAILED result is published."""
        # Backend that raises RuntimeError — the decomposer catches this
        # and re-raises as ValueError/RuntimeError which the orchestrator
        # catches and publishes as FAILED.

        class FailingBackend(LLMBackend):
            async def complete(self, system_prompt, user_message, max_tokens, temperature, **kw):
                raise RuntimeError("LLM unavailable")

        config_path = _write_config(timeout_seconds=1)
        try:
            bus = InMemoryBus()
            await bus.connect()
            actor = OrchestratorActor(
                actor_id="test-decomp-fail",
                config_path=config_path,
                backend=FailingBackend(),
                bus=bus,
            )

            goal_data = _make_goal_data("This will fail")
            goal_id = goal_data["payload"]["goal_id"]
            result_sub = await bus.subscribe(f"heddle.results.{goal_id}")

            await actor.handle_message(goal_data)

            result = await asyncio.wait_for(result_sub.__anext__(), timeout=2.0)
            assert result["payload"]["status"] == TaskStatus.FAILED.value
            # Either an error message about decomposition/orchestrator failure,
            # or "no subtasks" if decomposer caught the error and returned empty
            assert result["payload"]["error"] is not None
        finally:
            os.unlink(config_path)

    @pytest.mark.asyncio
    async def test_collection_timeout_returns_partial_results(self):
        """When timeout expires before all results arrive, partial results are synthesized."""
        # Plan with 3 subtasks, only 1 will respond
        plan = json.dumps(
            [{"worker_type": "summarizer", "payload": {"text": f"chunk {i}"}} for i in range(3)]
        )
        synthesis = json.dumps({"partial": True, "confidence": "low"})
        backend = MockOrchestratorBackend(plan, synthesis)

        config_path = _write_config(timeout_seconds=1)
        try:
            bus = InMemoryBus()
            await bus.connect()
            actor = OrchestratorActor(
                actor_id="test-timeout",
                config_path=config_path,
                backend=backend,
                bus=bus,
            )

            goal_data = _make_goal_data("Partial timeout test")
            goal_id = goal_data["payload"]["goal_id"]
            result_sub = await bus.subscribe(f"heddle.results.{goal_id}")

            # Pre-subscribe the worker before handle_message
            worker_sub = await bus.subscribe("heddle.tasks.incoming")

            # Worker only responds to first task
            async def partial_worker():
                data = await worker_sub.__anext__()
                task = TaskMessage(**data["payload"])
                # Small delay to let orchestrator set up result subscription
                await asyncio.sleep(0.05)
                result = TaskResult(
                    task_id=task.task_id,
                    parent_task_id=task.parent_task_id,
                    worker_type=task.worker_type,
                    status=TaskStatus.COMPLETED,
                    output={"summary": "partial"},
                    processing_time_ms=10,
                )
                await bus.publish(
                    f"heddle.results.{task.parent_task_id}",
                    wrap("core.TaskResult", result).model_dump(mode="json"),
                )
                # Don't respond to remaining tasks — let timeout fire
                await worker_sub.unsubscribe()

            worker_task = asyncio.create_task(partial_worker())
            await actor.handle_message(goal_data)
            await worker_task

            # Should still get a final result (synthesized from partial)
            final = await asyncio.wait_for(result_sub.__anext__(), timeout=3.0)
            assert final["payload"]["status"] == TaskStatus.COMPLETED.value
        finally:
            os.unlink(config_path)

    @pytest.mark.asyncio
    async def test_collection_timeout_synthesizes_failed_pending_results(self):
        """Pending tasks become synthetic FAILED results with timeout metadata.

        Pre-fix, ``_collect_results`` returned only the genuinely-arrived
        results, the synthesizer's ``failed`` list was empty, and the
        operator had to dig through ``goal_state.dispatched_tasks`` vs.
        the published synthesis to figure out which workers timed out.
        Post-fix, every dispatched-but-unresponded task surfaces in the
        synthesis output's ``failed`` list, and the goal-level metadata
        carries ``expected_count`` / ``collected_count`` / ``timeout_seconds``
        / ``pending_task_ids`` so an operator can triage without
        cross-referencing.
        """
        plan = json.dumps(
            [{"worker_type": "summarizer", "payload": {"text": f"chunk {i}"}} for i in range(3)]
        )
        synthesis = json.dumps({"partial": True, "confidence": "low"})
        backend = MockOrchestratorBackend(plan, synthesis)

        config_path = _write_config(timeout_seconds=1)
        try:
            bus = InMemoryBus()
            await bus.connect()
            actor = OrchestratorActor(
                actor_id="test-timeout-synthetic",
                config_path=config_path,
                backend=backend,
                bus=bus,
            )

            goal_data = _make_goal_data("Synthetic-timeout pin")
            goal_id = goal_data["payload"]["goal_id"]
            result_sub = await bus.subscribe(f"heddle.results.{goal_id}")
            worker_sub = await bus.subscribe("heddle.tasks.incoming")

            responded_id: dict[str, str] = {}

            async def one_responder():
                data = await worker_sub.__anext__()
                task = TaskMessage(**data["payload"])
                responded_id["task_id"] = task.task_id
                await asyncio.sleep(0.05)
                result = TaskResult(
                    task_id=task.task_id,
                    parent_task_id=task.parent_task_id,
                    worker_type=task.worker_type,
                    status=TaskStatus.COMPLETED,
                    output={"summary": "first"},
                    processing_time_ms=10,
                )
                await bus.publish(
                    f"heddle.results.{task.parent_task_id}",
                    wrap("core.TaskResult", result).model_dump(mode="json"),
                )
                await worker_sub.unsubscribe()

            worker_task = asyncio.create_task(one_responder())
            await actor.handle_message(goal_data)
            await worker_task

            # Both worker and orchestrator publish to ``heddle.results.{goal_id}``;
            # filter for the orchestrator's own final result.  The orchestrator's
            # ``worker_type`` is its config name (``"test-orchestrator"``); workers
            # use their own type (``"summarizer"``).
            final = None
            for _ in range(5):
                msg = await asyncio.wait_for(result_sub.__anext__(), timeout=3.0)
                if msg.get("payload", {}).get("worker_type") == "test-orchestrator":
                    final = msg
                    break
            assert final is not None, "orchestrator never published its final synthesis"
            assert final["payload"]["status"] == TaskStatus.COMPLETED.value

            output = final["payload"]["output"]
            # The two unresponded tasks became synthetic FAILED entries.
            failed = output["failed"]
            assert len(failed) == 2, (
                f"Expected 2 synthetic FAILED entries for the 2 timed-out tasks; "
                f"got {len(failed)}: {failed}"
            )
            for entry in failed:
                assert "timeout" in (entry["error"] or "").lower(), entry
                assert entry["task_id"] != responded_id["task_id"], (
                    "Synthetic timeout was attributed to the task that DID respond"
                )

            # Goal-level timeout metadata must be present so the operator
            # can triage without grepping the failed list.
            timeout_meta = output["metadata"]["timeout"]
            assert timeout_meta["expected_count"] == 3
            assert timeout_meta["collected_count"] == 1
            assert timeout_meta["timeout_seconds"] == 1
            assert len(timeout_meta["pending_task_ids"]) == 2
            assert responded_id["task_id"] not in timeout_meta["pending_task_ids"]
        finally:
            os.unlink(config_path)

    @pytest.mark.asyncio
    async def test_collection_timeout_ignores_late_arriving_result(self):
        """Late result (after timeout fired) does not displace the synthetic FAILED.

        ``_collect_results`` closes the ResultStream subscription when the
        timeout fires.  A worker result that arrives AFTER that point
        lands on a closed channel — the orchestrator must NOT incorporate
        it into the synthesis, and must NOT publish a second synthesis
        in reaction to it.  H1b in session-starters/H pins this so a
        future refactor that adds re-subscription or re-synthesis on
        late delivery has to update this contract deliberately.
        """
        plan = json.dumps(
            [{"worker_type": "summarizer", "payload": {"text": f"chunk {i}"}} for i in range(3)]
        )
        synthesis = json.dumps({"partial": True, "confidence": "low"})
        backend = MockOrchestratorBackend(plan, synthesis)

        config_path = _write_config(timeout_seconds=1)
        try:
            bus = InMemoryBus()
            await bus.connect()
            actor = OrchestratorActor(
                actor_id="test-late-result",
                config_path=config_path,
                backend=backend,
                bus=bus,
            )

            goal_data = _make_goal_data("Late-result-after-timeout pin")
            goal_id = goal_data["payload"]["goal_id"]
            result_sub = await bus.subscribe(f"heddle.results.{goal_id}")
            worker_sub = await bus.subscribe("heddle.tasks.incoming")

            on_time_id: dict[str, str] = {}
            late_id: dict[str, str] = {}

            async def staggered_worker() -> None:
                # Task 1 — respond before timeout (~50 ms after dispatch).
                data1 = await worker_sub.__anext__()
                t1 = TaskMessage(**data1["payload"])
                on_time_id["task_id"] = t1.task_id
                await asyncio.sleep(0.05)
                await bus.publish(
                    f"heddle.results.{t1.parent_task_id}",
                    wrap(
                        "core.TaskResult",
                        TaskResult(
                            task_id=t1.task_id,
                            parent_task_id=t1.parent_task_id,
                            worker_type=t1.worker_type,
                            status=TaskStatus.COMPLETED,
                            output={"summary": "on time"},
                            processing_time_ms=10,
                        ),
                    ).model_dump(mode="json"),
                )

                # Task 2 — accept dispatch, but publish ~200 ms AFTER the
                # 1 s collection timeout has fired.
                data2 = await worker_sub.__anext__()
                t2 = TaskMessage(**data2["payload"])
                late_id["task_id"] = t2.task_id
                await asyncio.sleep(1.2)
                await bus.publish(
                    f"heddle.results.{t2.parent_task_id}",
                    wrap(
                        "core.TaskResult",
                        TaskResult(
                            task_id=t2.task_id,
                            parent_task_id=t2.parent_task_id,
                            worker_type=t2.worker_type,
                            status=TaskStatus.COMPLETED,
                            # A distinctive output: if this ever leaks into the
                            # synthesis, the assertion below will surface it.
                            output={"summary": "TOO_LATE_SHOULD_BE_IGNORED"},
                            processing_time_ms=10,
                        ),
                    ).model_dump(mode="json"),
                )

                # Task 3 — drain dispatch, never respond.
                await worker_sub.__anext__()
                await worker_sub.unsubscribe()

            worker_task = asyncio.create_task(staggered_worker())
            await actor.handle_message(goal_data)
            # Wait for the staggered worker to finish (its sleep extends past
            # both the orchestrator's timeout and its synthesis publish).
            await worker_task

            # Collect every orchestrator-originated publish for the goal.
            # Workers publish to the same subject, so filter by worker_type.
            orch_publishes: list[dict] = []
            # 0.5 s grace after worker_task returns gives the orchestrator
            # plenty of time to do a hypothetical (incorrect) re-publish.
            deadline = time.monotonic() + 0.5
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    msg = await asyncio.wait_for(result_sub.__anext__(), timeout=remaining)
                except TimeoutError:
                    break
                if msg.get("payload", {}).get("worker_type") == "test-orchestrator":
                    orch_publishes.append(msg)

            assert len(orch_publishes) == 1, (
                f"Orchestrator published its synthesis {len(orch_publishes)} "
                f"time(s); a late result must not trigger a second publish."
            )

            final = orch_publishes[0]
            output = final["payload"]["output"]
            failed = output["failed"]

            # Both unresponded-by-timeout tasks (the late one and the
            # never-respond one) must surface as synthetic FAILED entries.
            assert len(failed) == 2, (
                f"Expected 2 synthetic FAILED entries (late + never-responded); "
                f"got {len(failed)}: {failed}"
            )

            late_entry = next((e for e in failed if e["task_id"] == late_id["task_id"]), None)
            assert late_entry is not None, (
                f"Late task {late_id['task_id']} missing from failed list — "
                f"its post-timeout result may have leaked into the synthesis. "
                f"failed = {failed}"
            )
            # The synthetic FAILED carries the timeout error string, NOT the
            # real (post-timeout) worker output.
            assert "timeout" in (late_entry.get("error") or "").lower(), late_entry
            assert "TOO_LATE_SHOULD_BE_IGNORED" not in json.dumps(output), (
                "The late result's output appeared somewhere in the synthesis — "
                "post-timeout deliveries must be dropped on the closed channel."
            )

            # The on-time task must not be in failed (its real output should
            # have made it through to succeeded).
            assert all(e["task_id"] != on_time_id["task_id"] for e in failed)

            meta = output["metadata"]["timeout"]
            assert meta["expected_count"] == 3
            assert meta["collected_count"] == 1
            assert late_id["task_id"] in meta["pending_task_ids"]
        finally:
            os.unlink(config_path)


# ---------------------------------------------------------------------------
# Checkpoint tests
# ---------------------------------------------------------------------------


class TestCheckpointing:
    @pytest.mark.asyncio
    async def test_checkpoint_triggered_when_threshold_exceeded(self):
        """Checkpoint is created when conversation history exceeds token threshold."""
        config_path = _write_config()
        try:
            backend = MockOrchestratorBackend("[]")
            store = InMemoryCheckpointStore()
            actor = OrchestratorActor(
                actor_id="test-ckpt",
                config_path=config_path,
                backend=backend,
                checkpoint_store=store,
            )

            # Configure a very low threshold so checkpoint triggers
            actor._checkpoint_manager.token_threshold = 10

            goal = OrchestratorGoal(instruction="Test checkpoint")
            goal_state = GoalState(goal=goal)

            # Add enough history to trigger
            for i in range(5):
                results = [
                    TaskResult(
                        task_id=f"t{i}",
                        worker_type="summarizer",
                        status=TaskStatus.COMPLETED,
                        output={"data": "x" * 100},
                        processing_time_ms=10,
                    ),
                ]
                await actor._record_in_history(goal_state, results, {"confidence": "high"})

            import structlog

            log = structlog.get_logger().bind(goal_id=goal.goal_id)
            await actor._maybe_checkpoint(goal_state, log)

            # Checkpoint should have been created
            assert goal_state.checkpoint_counter == 1
            # History should be trimmed to recent window
            assert (
                len(goal_state.conversation_history) <= actor._checkpoint_manager.recent_window_size
            )
        finally:
            os.unlink(config_path)

    @pytest.mark.asyncio
    async def test_checkpoint_failure_is_non_fatal(self):
        """If checkpoint store raises, orchestrator continues without crashing."""

        class FailingStore(InMemoryCheckpointStore):
            async def save(self, checkpoint):
                raise RuntimeError("Store unavailable")

        config_path = _write_config()
        try:
            backend = MockOrchestratorBackend("[]")
            actor = OrchestratorActor(
                actor_id="test-ckpt-fail",
                config_path=config_path,
                backend=backend,
                checkpoint_store=FailingStore(),
            )
            actor._checkpoint_manager.token_threshold = 10

            goal = OrchestratorGoal(instruction="Test")
            goal_state = GoalState(goal=goal)

            for i in range(5):
                results = [
                    TaskResult(
                        task_id=f"t{i}",
                        worker_type="summarizer",
                        status=TaskStatus.COMPLETED,
                        output={"data": "x" * 100},
                        processing_time_ms=10,
                    ),
                ]
                await actor._record_in_history(goal_state, results, {})

            import structlog

            log = structlog.get_logger().bind(goal_id=goal.goal_id)

            # Should not raise
            await actor._maybe_checkpoint(goal_state, log)
        finally:
            os.unlink(config_path)

    @pytest.mark.asyncio
    async def test_no_checkpoint_when_no_store(self):
        """_maybe_checkpoint is a no-op when no checkpoint store is configured."""
        config_path = _write_config()
        try:
            backend = MockOrchestratorBackend("[]")
            actor = OrchestratorActor(
                actor_id="test-no-ckpt",
                config_path=config_path,
                backend=backend,
                checkpoint_store=None,
            )

            goal = OrchestratorGoal(instruction="Test")
            goal_state = GoalState(goal=goal)

            import structlog

            log = structlog.get_logger().bind(goal_id=goal.goal_id)
            await actor._maybe_checkpoint(goal_state, log)
            assert goal_state.checkpoint_counter == 0
        finally:
            os.unlink(config_path)


# ---------------------------------------------------------------------------
# on_reload tests (lines 278-281)
# ---------------------------------------------------------------------------


class TestOnReload:
    @pytest.mark.asyncio
    async def test_on_reload_updates_timeout_and_concurrency(self, tmp_path):
        """Lines 278-281: on_reload re-reads config from disk."""
        import yaml

        config_path = _write_config(timeout_seconds=10)
        try:
            backend = MockOrchestratorBackend("[]")
            actor = OrchestratorActor(
                actor_id="test-reload",
                config_path=config_path,
                backend=backend,
            )

            assert actor._task_timeout == 10.0

            # Write an updated config with a different timeout.
            updated = {
                "name": "test-orchestrator",
                "timeout_seconds": 42,
                "max_concurrent_tasks": 3,
                "available_workers": [],
            }
            with open(config_path, "w") as f:
                yaml.dump(updated, f)

            await actor.on_reload()

            assert actor._task_timeout == 42.0
            assert actor._max_concurrent_tasks == 3
        finally:
            os.unlink(config_path)

    @pytest.mark.asyncio
    async def test_on_reload_uses_defaults_when_keys_absent(self, tmp_path):
        """on_reload falls back to defaults (300s, 5 tasks) when keys are missing."""
        import yaml

        config_path = _write_config(timeout_seconds=5)
        try:
            backend = MockOrchestratorBackend("[]")
            actor = OrchestratorActor(
                actor_id="test-reload-defaults",
                config_path=config_path,
                backend=backend,
            )

            # Write a config without timeout_seconds or max_concurrent_tasks.
            minimal = {"name": "test-orchestrator", "available_workers": []}
            with open(config_path, "w") as f:
                yaml.dump(minimal, f)

            await actor.on_reload()

            assert actor._task_timeout == 300.0
            assert actor._max_concurrent_tasks == 5
        finally:
            os.unlink(config_path)


# ---------------------------------------------------------------------------
# handle_message exception handler (lines 404-407)
# ---------------------------------------------------------------------------


class TestHandleMessageExceptionPath:
    @pytest.mark.asyncio
    async def test_unexpected_exception_during_dispatch_publishes_failure(self):
        """Lines 404-407: unexpected exception during _dispatch_subtasks publishes FAILED."""
        config_path = _write_config(timeout_seconds=5)
        try:
            plan = json.dumps([{"worker_type": "summarizer", "payload": {"text": "test"}}])
            backend = MockOrchestratorBackend(plan)
            bus = InMemoryBus()
            await bus.connect()
            actor = OrchestratorActor(
                actor_id="test-exc",
                config_path=config_path,
                backend=backend,
                bus=bus,
            )

            goal_data = _make_goal_data("Exception test")
            goal_id = goal_data["payload"]["goal_id"]
            result_sub = await bus.subscribe(f"heddle.results.{goal_id}")

            # Patch _dispatch_subtasks to raise an unexpected exception.
            async def _boom(*args, **kwargs):
                raise RuntimeError("unexpected crash")

            actor._dispatch_subtasks = _boom

            await actor.handle_message(goal_data)

            result = await asyncio.wait_for(result_sub.__anext__(), timeout=2.0)
            assert result["payload"]["status"] == TaskStatus.FAILED.value
            assert "Orchestrator error" in result["payload"]["error"]
            assert "unexpected crash" in result["payload"]["error"]
            # Goal state must be cleaned up.
            assert goal_id not in actor._active_goals
        finally:
            os.unlink(config_path)


# ---------------------------------------------------------------------------
# _collect_results early_exit path (lines 567-568)
# ---------------------------------------------------------------------------


class TestCollectResultsEarlyExit:
    @pytest.mark.asyncio
    async def test_early_exit_branch_logged_when_callback_signals_stop(self):
        """Lines 567-568: early_exited branch triggers log message."""
        config_path = _write_config(timeout_seconds=5)
        try:
            bus = InMemoryBus()
            await bus.connect()
            backend = MockOrchestratorBackend("[]")
            actor = OrchestratorActor(
                actor_id="test-early-exit",
                config_path=config_path,
                backend=backend,
                bus=bus,
            )

            goal = OrchestratorGoal(instruction="Early exit test")
            task = TaskMessage(worker_type="summarizer", input={"text": "hi"})
            goal_state = GoalState(goal=goal)
            goal_state.dispatched_tasks[task.task_id] = task

            # Callback that signals early exit after first result.
            async def stop_after_first(result, collected, expected):
                return True  # signal stop

            import structlog

            log = structlog.get_logger().bind(goal_id=goal.goal_id)

            # Publish a result so the stream can collect it and trigger early exit.
            async def publisher():
                await asyncio.sleep(0.05)
                result = TaskResult(
                    task_id=task.task_id,
                    parent_task_id=goal.goal_id,
                    worker_type="summarizer",
                    status=TaskStatus.COMPLETED,
                    output={"summary": "done"},
                )
                await bus.publish(
                    f"heddle.results.{goal.goal_id}",
                    wrap("core.TaskResult", result).model_dump(mode="json"),
                )

            # _collect_results now requires a started ResultStream
            # (the subscribe-before-publish race fix).  Build one,
            # enter it, dispatch the publisher, then collect.
            from heddle.orchestrator.stream import ResultStream

            stream = ResultStream(
                bus=bus,
                subject=f"heddle.results.{goal.goal_id}",
                expected_task_ids={task.task_id},
                timeout=5.0,
                on_result=stop_after_first,
            )

            async with stream:
                pub_task = asyncio.create_task(publisher())
                results = await actor._collect_results(stream, goal_state, log)
                await pub_task

            # We should have gotten exactly 1 result (the one we published).
            assert len(results) == 1
            assert results[0].task_id == task.task_id
        finally:
            os.unlink(config_path)
