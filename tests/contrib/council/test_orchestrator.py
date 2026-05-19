"""Tests for CouncilOrchestrator with InMemoryBus.

Exercises the full NATS-connected council flow using an in-memory bus
so no infrastructure is needed. Follows the pattern of
tests/test_e2e_operations.py.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import yaml

from heddle.bus.memory import InMemoryBus
from heddle.contrib.council.orchestrator import CouncilOrchestrator
from heddle.core.messages import (
    OrchestratorGoal,
    TaskResult,
    TaskStatus,
)
from heddle.worker.backends import LLMBackend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(data: dict) -> str:
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as f:
        yaml.dump(data, f)
    return path


def _council_config(max_rounds=2, convergence_method="none"):
    # ``timeout_seconds`` and ``synthesis_timeout_seconds`` are sized
    # to pass the per-turn-floor validation (K3 / commit msg):
    # (120 - 20) / (max_rounds * 2 agents) >= 5s for max_rounds <= 10.
    # The wall-clock values don't matter under mocked workers, but
    # the implied per-turn budget must clear the 5s floor.
    return {
        "name": "test_council",
        "protocol": "round_robin",
        "max_rounds": max_rounds,
        "timeout_seconds": 120,
        "synthesis_timeout_seconds": 20,
        "convergence": {"method": convergence_method, "threshold": 0.9},
        "agents": [
            {
                "name": "analyst",
                "worker_type": "test_worker",
                "tier": "standard",
                "role": "Analyst",
            },
            {"name": "critic", "worker_type": "test_worker", "tier": "standard", "role": "Critic"},
        ],
        "facilitator": {
            "tier": "standard",
            "synthesis_prompt": "Synthesize.",
        },
    }


class MockFacilitatorBackend(LLMBackend):
    """Mock backend used by the facilitator for synthesis and convergence."""

    async def complete(self, system_prompt, user_message, max_tokens=2000, temperature=0.0, **kw):
        if "score" in system_prompt.lower() or "agreement" in system_prompt.lower():
            return {
                "content": '{"score": 0.95, "reason": "Everyone agrees"}',
                "model": "mock",
                "prompt_tokens": 50,
                "completion_tokens": 20,
            }
        return {
            "content": "The team reached consensus on the approach.",
            "model": "mock",
            "prompt_tokens": 100,
            "completion_tokens": 50,
        }


async def _simulate_worker(
    bus: InMemoryBus,
    respond_to_n: int = 10,
    ready: asyncio.Event | None = None,
) -> None:
    """Subscribe to heddle.tasks.incoming and respond with mock results.

    Simulates the router+worker path: reads TaskMessage from incoming,
    publishes TaskResult to the appropriate result subject.

    If ``ready`` is provided, it is ``set()`` once the subscription is
    live so the caller can wait for subscribe-before-publish ordering
    (Design Invariant 17).  Without this, a fast orchestrator
    dispatches before the worker subscribes, the result is dropped,
    and the test waits the per-turn timeout for nothing.  K3's 5s
    per-turn floor amplified this latent race into a 25s-per-turn
    test slowdown.
    """
    sub = await bus.subscribe("heddle.tasks.incoming")
    if ready is not None:
        ready.set()
    count = 0
    async for data in sub:
        parent_id = data.get("parent_task_id", "default")
        task_id = data.get("task_id")
        worker_type = data.get("worker_type", "unknown")
        agent = data.get("metadata", {}).get("agent", "unknown")

        result = TaskResult(
            task_id=task_id,
            parent_task_id=parent_id,
            worker_type=worker_type,
            status=TaskStatus.COMPLETED,
            output={"content": f"Position from {agent}: I think we should proceed."},
            model_used="mock-worker",
            token_usage={"prompt_tokens": 30, "completion_tokens": 20},
            processing_time_ms=10,
        )
        await bus.publish(
            f"heddle.results.{parent_id}",
            result.model_dump(mode="json"),
        )

        count += 1
        if count >= respond_to_n:
            break


async def _get_final_result(sub, goal_id: str, timeout: float = 5.0) -> dict:
    """Wait for the final council result on the result subject."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            msg = f"Final result for {goal_id} not received within {timeout}s"
            raise TimeoutError(msg)
        data = await asyncio.wait_for(sub.__anext__(), timeout=remaining)
        if data.get("task_id") == goal_id:
            return data


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCouncilOrchestrator:
    async def test_basic_two_round_discussion(self):
        """Full 2-round council with mock workers and facilitator."""
        bus = InMemoryBus()
        await bus.connect()

        config_path = _write_yaml(_council_config(max_rounds=2))
        backend = MockFacilitatorBackend()

        orch = CouncilOrchestrator(
            actor_id="test-council-orch",
            config_path=config_path,
            backend=backend,
            bus=bus,
        )

        goal = OrchestratorGoal(instruction="Should we adopt microservices?")
        goal_data = goal.model_dump(mode="json")

        # Subscribe to result subject before starting.
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        # 2 agents * 2 rounds = 4 worker responses needed.
        # Subscribe-before-publish (Invariant 17): wait for the worker
        # task to confirm its subscription is live before the
        # orchestrator dispatches.
        ready = asyncio.Event()
        worker_task = asyncio.create_task(_simulate_worker(bus, respond_to_n=4, ready=ready))
        await ready.wait()

        # Run the orchestrator's message handler directly.
        await orch.handle_message(goal_data)

        # Get the final result.
        final = await _get_final_result(result_sub, goal.goal_id)

        assert final["status"] == "completed"
        assert final["output"]["rounds_completed"] == 2
        assert final["output"]["converged"] is False  # method=none
        assert "consensus" in final["output"]["synthesis"].lower()
        assert "analyst" in final["output"]["agent_summaries"]
        assert "critic" in final["output"]["agent_summaries"]

        worker_task.cancel()
        await bus.close()

    async def test_convergence_stops_early(self):
        """Council with llm_judge convergence that stops on round 1."""
        bus = InMemoryBus()
        await bus.connect()

        config = _council_config(max_rounds=5, convergence_method="llm_judge")
        config["convergence"]["threshold"] = 0.5  # Low threshold
        config_path = _write_yaml(config)
        backend = MockFacilitatorBackend()

        orch = CouncilOrchestrator(
            actor_id="test-council-conv",
            config_path=config_path,
            backend=backend,
            bus=bus,
        )

        goal = OrchestratorGoal(instruction="Test convergence")
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        # Provide enough responses for up to 5 rounds (but expect early stop).
        ready = asyncio.Event()
        worker_task = asyncio.create_task(_simulate_worker(bus, respond_to_n=10, ready=ready))
        await ready.wait()

        await orch.handle_message(goal.model_dump(mode="json"))

        final = await _get_final_result(result_sub, goal.goal_id)

        assert final["status"] == "completed"
        # Should converge after round 1 since mock returns score 0.95 > 0.5.
        assert final["output"]["rounds_completed"] == 1
        assert final["output"]["converged"] is True

        worker_task.cancel()
        await bus.close()

    async def test_worker_timeout_produces_error_entry(self, monkeypatch):
        """When a worker doesn't respond, the transcript notes the timeout.

        Originally configured a sub-second ``timeout_seconds`` so the
        per-turn wait would fire fast.  K3 introduced a 5s floor on
        per-turn budget, which would make this test wait 5s * 2 agents
        before the final result fires — a 10x slowdown for a behaviour
        test that doesn't need real timing.

        Patches ``dispatch_and_wait_for_result`` to return ``None``
        immediately (the signature for "no worker responded") so the
        transcript exercises the timeout-noted-in-entry path without
        depending on wall-clock.
        """
        bus = InMemoryBus()
        await bus.connect()

        # Patch the dispatch helper to simulate worker non-response
        # without paying the per-turn wait.  Module-level patch covers
        # both ``orchestrator.dispatch_and_wait_for_result`` imports.
        async def _fake_dispatch(*args, **kwargs):
            return None

        monkeypatch.setattr(
            "heddle.contrib.council.orchestrator.dispatch_and_wait_for_result",
            _fake_dispatch,
        )

        config_path = _write_yaml(_council_config(max_rounds=1))
        backend = MockFacilitatorBackend()

        orch = CouncilOrchestrator(
            actor_id="test-council-timeout",
            config_path=config_path,
            backend=backend,
            bus=bus,
        )

        goal = OrchestratorGoal(instruction="Test timeout")
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        await orch.handle_message(goal.model_dump(mode="json"))

        final = await _get_final_result(result_sub, goal.goal_id)

        assert final["status"] == "completed"
        # Timeouts are not fatal — the council still produces a result.
        assert final["output"]["rounds_completed"] == 1

        await bus.close()

    async def test_per_turn_timeout_produces_timeout_entry(self, monkeypatch):
        """Per-turn timeout invariant — orchestrator side (J4).

        Mirror of ``TestCouncilRunnerTimeouts.test_per_turn_timeout_
        produces_timeout_entry`` in ``test_runner.py``. Pins the
        cross-path invariant: when an agent turn exceeds the per-turn
        budget, *both* execution paths produce ``[Timeout: ...]`` as
        the transcript content for that agent, not a raised exception
        that escapes to the caller.

        The runner enforces the budget via ``call_with_budget`` and
        catches ``CouncilTimeoutError`` to write the transcript entry;
        the orchestrator enforces it via the dispatch-helper's
        ``timeout=`` argument, treating ``None`` as the no-response
        signal.  Different mechanisms, identical observable shape —
        and that's the invariant J4 pins.
        """
        bus = InMemoryBus()
        await bus.connect()

        async def _fake_dispatch(*args, **kwargs):
            return None

        monkeypatch.setattr(
            "heddle.contrib.council.orchestrator.dispatch_and_wait_for_result",
            _fake_dispatch,
        )

        config_path = _write_yaml(_council_config(max_rounds=1))
        backend = MockFacilitatorBackend()

        orch = CouncilOrchestrator(
            actor_id="test-council-per-turn-timeout",
            config_path=config_path,
            backend=backend,
            bus=bus,
        )

        goal = OrchestratorGoal(instruction="Test per-turn timeout")
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        await orch.handle_message(goal.model_dump(mode="json"))

        final = await _get_final_result(result_sub, goal.goal_id)

        assert final["status"] == "completed"
        summaries = final["output"]["agent_summaries"]
        # Every agent timed out; their latest position is the
        # ``[Timeout: ...]`` entry.  Same invariant the runner test
        # asserts on ``result.transcript[0].entries``.
        assert summaries, "expected at least one agent summary"
        assert all("[Timeout" in content for content in summaries.values())

        await bus.close()

    async def test_worker_failure_noted_in_transcript(self):
        """When a worker returns FAILED, the transcript notes the error."""
        bus = InMemoryBus()
        await bus.connect()

        config_path = _write_yaml(_council_config(max_rounds=1))
        backend = MockFacilitatorBackend()

        orch = CouncilOrchestrator(
            actor_id="test-council-fail",
            config_path=config_path,
            backend=backend,
            bus=bus,
        )

        goal = OrchestratorGoal(instruction="Test failure")
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")

        # Simulate a worker that returns FAILED.  Signals readiness
        # before consuming so the test waits for subscribe-before-publish
        # (Invariant 17) rather than relying on per-turn timeout slack.
        worker_ready = asyncio.Event()

        async def _failing_worker():
            sub = await bus.subscribe("heddle.tasks.incoming")
            worker_ready.set()
            count = 0
            async for data in sub:
                parent_id = data.get("parent_task_id", "default")
                task_id = data.get("task_id")
                result = TaskResult(
                    task_id=task_id,
                    parent_task_id=parent_id,
                    worker_type=data.get("worker_type", "unknown"),
                    status=TaskStatus.FAILED,
                    output=None,
                    error="Worker crashed",
                    token_usage={},
                    processing_time_ms=5,
                )
                await bus.publish(
                    f"heddle.results.{parent_id}",
                    result.model_dump(mode="json"),
                )
                count += 1
                if count >= 2:
                    break

        worker_task = asyncio.create_task(_failing_worker())
        await worker_ready.wait()
        await orch.handle_message(goal.model_dump(mode="json"))

        final = await _get_final_result(result_sub, goal.goal_id)
        assert final["status"] == "completed"

        worker_task.cancel()
        await bus.close()

    async def test_final_result_on_correct_subject(self):
        """The final result is published to heddle.results.{goal_id}."""
        bus = InMemoryBus()
        await bus.connect()

        config_path = _write_yaml(_council_config(max_rounds=1))
        backend = MockFacilitatorBackend()

        orch = CouncilOrchestrator(
            actor_id="test-council-subject",
            config_path=config_path,
            backend=backend,
            bus=bus,
        )

        goal = OrchestratorGoal(instruction="Test subject")
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")
        ready = asyncio.Event()
        worker_task = asyncio.create_task(_simulate_worker(bus, respond_to_n=2, ready=ready))
        await ready.wait()

        await orch.handle_message(goal.model_dump(mode="json"))

        final = await _get_final_result(result_sub, goal.goal_id)
        assert final["task_id"] == goal.goal_id
        assert final["worker_type"] == "council:test_council"

        worker_task.cancel()
        await bus.close()
