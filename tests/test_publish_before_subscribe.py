"""Subscribe-before-publish ordering tests.

NATS is at-most-once.  An orchestrator that publishes a task BEFORE it
subscribes to the result subject can lose results to fast workers — the
worker's reply lands on the bus while no subscription exists, and the
message is dropped.  The orchestrator then waits out its timeout for a
result that already happened.

Three sites in the codebase publish a task and wait for a single
result; all three must subscribe first:

- :meth:`PipelineOrchestrator._dispatch_and_wait_for_result`
- :meth:`OrchestratorActor` via :class:`ResultStream` ``async with``
- :meth:`heddle.contrib.council.orchestrator.CouncilOrchestrator
  ._dispatch_and_wait_for_result`

This module pins the ordering with two complementary techniques:

1. **Order-of-operations tests** — a ``BusRecorder`` captures every
   ``subscribe`` and ``publish`` call.  After running each helper, we
   assert the result-subject subscribe appears in the log before the
   first task publish.  This catches a regression statically without
   depending on timing.
2. **Race-regression tests** — a ``WorkerSimBus`` simulates a worker
   that replies *instantaneously* (publishing the result inline,
   inside the orchestrator's own ``await publish(...)``).  With the
   fix in place the result is delivered; with the old lazy-subscribe
   shape it would be dropped and the helper would time out.  This is
   the test the original bug would have caught — running it against
   the pre-fix code reproduces the bug.
"""

from __future__ import annotations

from typing import Any

import pytest

from heddle.bus.memory import InMemoryBus, InMemorySubscription
from heddle.core.envelope import wrap
from heddle.core.messages import (
    OrchestratorGoal,
    TaskMessage,
    TaskResult,
    TaskStatus,
)

# ---------------------------------------------------------------------------
# Test buses
# ---------------------------------------------------------------------------


class BusRecorder(InMemoryBus):
    """InMemoryBus that records every subscribe/publish call in order."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str]] = []

    async def publish(self, subject: str, data: dict[str, Any]) -> None:
        self.calls.append(("publish", subject))
        await super().publish(subject, data)

    async def subscribe(
        self,
        subject: str,
        queue_group: str | None = None,
    ) -> InMemorySubscription:
        self.calls.append(("subscribe", subject))
        return await super().subscribe(subject, queue_group)


class WorkerSimBus(BusRecorder):
    """Bus that simulates a worker replying inline, with no scheduling delay.

    On every publish to ``heddle.tasks.incoming`` we immediately publish
    a synthetic :class:`TaskResult` onto ``heddle.results.{goal_id}``.
    The synthetic publish runs INSIDE the orchestrator's
    ``await publish(...)`` call — i.e., before the next ``await`` in
    the orchestrator can run.  In the old (lazy-subscribe) shape the
    orchestrator had not yet subscribed, so the result was dropped on
    the floor.  With the fix, the orchestrator subscribed first and
    the synthetic result is delivered.
    """

    def __init__(self, goal_id: str, output: dict[str, Any]) -> None:
        super().__init__()
        self._goal_id = goal_id
        self._output = output

    async def publish(self, subject: str, data: dict[str, Any]) -> None:
        await super().publish(subject, data)
        if subject == "heddle.tasks.incoming":
            # Inline reply.  This recursion is bounded — the synthetic
            # publish targets ``heddle.results.*`` which doesn't match
            # ``heddle.tasks.incoming`` so it does not recurse again.
            payload = data.get("payload", data)
            result = TaskResult(
                task_id=payload["task_id"],
                parent_task_id=payload.get("parent_task_id"),
                worker_type=payload["worker_type"],
                status=TaskStatus.COMPLETED,
                output=self._output,
                processing_time_ms=1,
                token_usage={"prompt_tokens": 0, "completion_tokens": 0},
            )
            await self.publish(
                f"heddle.results.{self._goal_id}",
                wrap("core.TaskResult", result).model_dump(mode="json"),
            )


class MalformedThenValidWorkerSimBus(BusRecorder):
    """Replies with a malformed result followed by a valid one.

    Pins parse-error resilience in
    :func:`heddle.orchestrator.dispatch.dispatch_and_wait_for_result`:
    a malformed result that happens to carry the matching ``task_id``
    must NOT take down the consumer task.  Without resilience the
    Pydantic ``ValidationError`` propagates out of ``_consume``,
    ``result_future`` is never set, and the orchestrator times out
    even though a valid result was right behind the malformed one.
    """

    def __init__(self, goal_id: str, output: dict[str, Any]) -> None:
        super().__init__()
        self._goal_id = goal_id
        self._output = output

    async def publish(self, subject: str, data: dict[str, Any]) -> None:
        await super().publish(subject, data)
        if subject == "heddle.tasks.incoming":
            payload = data.get("payload", data)
            # First: malformed result with matching task_id but
            # missing required fields and an invalid status enum.
            await self.publish(
                f"heddle.results.{self._goal_id}",
                {"task_id": payload["task_id"], "status": "bogus"},
            )
            # Second: valid result with the same task_id.  Inline so
            # the timeout window doesn't matter — both publishes complete
            # before dispatch_and_wait_for_result gets to ``await
            # wait_for(result_future, ...)``.
            valid = TaskResult(
                task_id=payload["task_id"],
                parent_task_id=payload.get("parent_task_id"),
                worker_type=payload["worker_type"],
                status=TaskStatus.COMPLETED,
                output=self._output,
                processing_time_ms=1,
                token_usage={"prompt_tokens": 0, "completion_tokens": 0},
            )
            await self.publish(
                f"heddle.results.{self._goal_id}",
                wrap("core.TaskResult", valid).model_dump(mode="json"),
            )


def _assert_subscribe_before_publish(
    calls: list[tuple[str, str]],
    *,
    result_subject: str,
    task_subject: str = "heddle.tasks.incoming",
) -> None:
    """Assert the result subscribe appears in the call log before any task publish."""
    sub_idx = next(
        (i for i, (op, subj) in enumerate(calls) if op == "subscribe" and subj == result_subject),
        None,
    )
    pub_idx = next(
        (i for i, (op, subj) in enumerate(calls) if op == "publish" and subj == task_subject),
        None,
    )
    assert sub_idx is not None, f"Expected a subscribe to {result_subject!r}; calls were {calls}"
    assert pub_idx is not None, f"Expected a publish to {task_subject!r}; calls were {calls}"
    assert sub_idx < pub_idx, (
        f"Subscribe to {result_subject!r} (index {sub_idx}) must precede "
        f"publish to {task_subject!r} (index {pub_idx}). Calls: {calls}"
    )


# ---------------------------------------------------------------------------
# 1. PipelineOrchestrator
# ---------------------------------------------------------------------------


class TestPipelineOrchestratorOrdering:
    """Pin the ordering on the shared dispatch helper.

    Originally tested
    ``PipelineOrchestrator._dispatch_and_wait_for_result``; the body
    moved to :func:`heddle.orchestrator.dispatch.dispatch_and_wait_for_result`
    so both pipeline and council orchestrators share it.  The pipeline
    class is kept in the test name because it's the original caller —
    if the shared helper subtly diverges from what the pipeline needs,
    these tests will catch it.
    """

    @pytest.mark.asyncio
    async def test_subscribe_precedes_publish(self):
        """``dispatch_and_wait_for_result`` subscribes BEFORE publishing the task."""
        from heddle.orchestrator.dispatch import dispatch_and_wait_for_result

        bus = BusRecorder()
        await bus.connect()

        task = TaskMessage(worker_type="summarizer", input={"text": "hi"})
        # Time out fast — we don't actually wait for a real reply.
        await dispatch_and_wait_for_result(
            bus=bus,
            task=task,
            task_data=wrap("core.TaskMessage", task).model_dump(mode="json"),
            goal_id="goal-pipe",
            timeout=0.05,
        )

        _assert_subscribe_before_publish(bus.calls, result_subject="heddle.results.goal-pipe")

    @pytest.mark.asyncio
    async def test_inline_worker_reply_is_delivered(self):
        """A worker that replies inline (zero-delay) must still be heard.

        Race-regression test: with the pre-fix lazy-subscribe shape this
        deadlocked or timed out because the synthetic result was
        published before the subscription existed.
        """
        from heddle.orchestrator.dispatch import dispatch_and_wait_for_result

        bus = WorkerSimBus(goal_id="goal-pipe-race", output={"summary": "fast"})
        await bus.connect()

        task = TaskMessage(worker_type="summarizer", input={"text": "hi"})
        result = await dispatch_and_wait_for_result(
            bus=bus,
            task=task,
            task_data=wrap("core.TaskMessage", task).model_dump(mode="json"),
            goal_id="goal-pipe-race",
            timeout=2.0,
        )

        assert result is not None, "fast worker reply was lost — race not fixed"
        assert result.status == TaskStatus.COMPLETED
        assert result.output == {"summary": "fast"}

    @pytest.mark.asyncio
    async def test_malformed_matching_result_is_skipped(self):
        """A malformed matching result must not kill the consumer task.

        ``ResultStream`` (orchestrator/stream.py) already logs and
        skips Pydantic-rejected payloads.  ``dispatch_and_wait_for_result``
        used to construct ``TaskResult(**data)`` unguarded — the
        ValidationError propagated out of the consumer task, the
        future was never set, and the helper timed out even when a
        valid result followed the malformed one.
        """
        from heddle.orchestrator.dispatch import dispatch_and_wait_for_result

        bus = MalformedThenValidWorkerSimBus(
            goal_id="goal-pipe-malformed", output={"summary": "valid"}
        )
        await bus.connect()

        task = TaskMessage(worker_type="summarizer", input={"text": "hi"})
        result = await dispatch_and_wait_for_result(
            bus=bus,
            task=task,
            task_data=wrap("core.TaskMessage", task).model_dump(mode="json"),
            goal_id="goal-pipe-malformed",
            timeout=2.0,
        )

        assert result is not None, (
            "valid result after malformed one was lost — parse-error resilience not in place"
        )
        assert result.status == TaskStatus.COMPLETED
        assert result.output == {"summary": "valid"}


# ---------------------------------------------------------------------------
# 2. OrchestratorActor (via ResultStream context)
# ---------------------------------------------------------------------------


def _write_orch_config(timeout_seconds: int = 1) -> str:
    """Write a minimal orchestrator config to a temp file."""
    import os
    import tempfile

    import yaml

    config = {
        "name": "test-orchestrator",
        "timeout_seconds": timeout_seconds,
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
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as f:
        yaml.dump(config, f)
    return path


class _OneSubtaskBackend:
    """Backend that decomposes into exactly one summarizer subtask."""

    async def complete(self, system_prompt, user_message, max_tokens=2000, temperature=0.0, **kw):
        import json as _json

        if "task decomposition" in system_prompt.lower():
            content = _json.dumps([{"worker_type": "summarizer", "payload": {"text": "hi"}}])
        else:
            content = "{}"
        return {
            "content": content,
            "model": "mock",
            "prompt_tokens": 1,
            "completion_tokens": 1,
        }


class TestOrchestratorActorOrdering:
    @pytest.mark.asyncio
    async def test_handle_message_subscribes_before_dispatch(self):
        """End-to-end: handle_message subscribes BEFORE dispatch publishes.

        Stronger than asserting on ResultStream alone — this drives the
        runner's actual code path.  If anyone refactors
        ``_handle_message`` and accidentally moves ``_dispatch_subtasks``
        outside the ``async with stream:`` block, this test fails.
        """
        import os

        from heddle.orchestrator.runner import OrchestratorActor

        config_path = _write_orch_config(timeout_seconds=1)
        try:
            bus = BusRecorder()
            await bus.connect()

            actor = OrchestratorActor(
                actor_id="test-orch-ordering",
                config_path=config_path,
                backend=_OneSubtaskBackend(),
            )
            actor._bus = bus

            goal = OrchestratorGoal(instruction="Summarise this")
            await actor.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))

            _assert_subscribe_before_publish(
                bus.calls, result_subject=f"heddle.results.{goal.goal_id}"
            )
        finally:
            os.unlink(config_path)

    @pytest.mark.asyncio
    async def test_handle_message_collects_inline_worker_reply(self):
        """End-to-end: a worker that replies inline is collected (race-regression).

        Drives ``OrchestratorActor.handle_message`` with a ``WorkerSimBus``
        that publishes a synthetic result the moment the orchestrator
        publishes the subtask.  With the fix in place the result is
        collected and ``_synthesize_results`` sees it; pre-fix the
        result is dropped and the synthesizer sees an empty list.

        The assertion checks ``goal_state.collected_results`` directly
        via a monkey-patched ``_synthesize_results`` because the final
        published TaskResult does NOT distinguish "collected one result"
        from "timed out with zero results" — both end with status
        COMPLETED.  Reading what the synthesizer actually received is
        the unambiguous signal.
        """
        import os

        from heddle.orchestrator.runner import OrchestratorActor

        config_path = _write_orch_config(timeout_seconds=2)
        try:
            # Goal id is generated, so we can't pre-bind it on the bus.
            # Build a bus that handles whichever goal arrives by parsing
            # the parent_task_id from each task.
            class AnyGoalWorkerBus(BusRecorder):
                async def publish(self, subject, data):
                    await BusRecorder.publish(self, subject, data)
                    if subject == "heddle.tasks.incoming":
                        payload = data.get("payload", data)
                        result = TaskResult(
                            task_id=payload["task_id"],
                            parent_task_id=payload.get("parent_task_id"),
                            worker_type=payload["worker_type"],
                            status=TaskStatus.COMPLETED,
                            output={"summary": "ok"},
                            processing_time_ms=1,
                        )
                        # parent_task_id IS goal_id for orchestrator-dispatched
                        # subtasks.
                        await self.publish(
                            f"heddle.results.{payload['parent_task_id']}",
                            wrap("core.TaskResult", result).model_dump(mode="json"),
                        )

            bus = AnyGoalWorkerBus()
            await bus.connect()

            actor = OrchestratorActor(
                actor_id="test-orch-race",
                config_path=config_path,
                backend=_OneSubtaskBackend(),
            )
            actor._bus = bus

            # Capture what the synthesizer actually receives.  This is
            # the load-bearing signal: with the fix the list has one
            # COMPLETED entry, without the fix it is empty.
            seen_results: list[TaskResult] = []
            original_synth = actor._synthesize_results

            async def capture_synth(goal, results, log):
                seen_results.extend(results)
                return await original_synth(goal, results, log)

            actor._synthesize_results = capture_synth  # type: ignore[method-assign]

            goal = OrchestratorGoal(instruction="Summarise this")
            await actor.handle_message(wrap("core.OrchestratorGoal", goal).model_dump(mode="json"))

            assert len(seen_results) == 1, (
                f"synthesizer received {len(seen_results)} results — "
                "the inline worker reply was dropped (race not fixed)"
            )
            assert seen_results[0].status == TaskStatus.COMPLETED
            assert seen_results[0].output == {"summary": "ok"}
        finally:
            os.unlink(config_path)

    @pytest.mark.asyncio
    async def test_inline_worker_reply_is_collected(self):
        """ResultStream subscribed in __aenter__ catches an inline-reply worker."""
        from heddle.orchestrator.stream import ResultStream

        goal_id = "goal-orch-race"
        bus = WorkerSimBus(goal_id=goal_id, output={"summary": "fast"})
        await bus.connect()

        task = TaskMessage(
            worker_type="summarizer",
            input={"text": "hi"},
            parent_task_id=goal_id,
        )

        stream = ResultStream(
            bus=bus,
            subject=f"heddle.results.{goal_id}",
            expected_task_ids={task.task_id},
            timeout=2.0,
        )

        async with stream:
            await bus.publish(
                "heddle.tasks.incoming", wrap("core.TaskMessage", task).model_dump(mode="json")
            )
            results = await stream.collect_all()

        assert len(results) == 1, "fast worker reply was lost — race not fixed"
        assert results[0].status == TaskStatus.COMPLETED
        assert results[0].output == {"summary": "fast"}


# ---------------------------------------------------------------------------
# 3. CouncilOrchestrator (NATS mode)
# ---------------------------------------------------------------------------


class TestCouncilOrchestratorOrdering:
    """Council mirror of the pipeline ordering test class.

    Both orchestrators now share
    :func:`heddle.orchestrator.dispatch.dispatch_and_wait_for_result`.
    Keeping a separate class for the council path documents the
    intent — "the council uses the same helper, and we expect it to
    keep doing so."
    """

    @pytest.mark.asyncio
    async def test_subscribe_precedes_publish(self):
        from heddle.orchestrator.dispatch import dispatch_and_wait_for_result

        bus = BusRecorder()
        await bus.connect()

        task = TaskMessage(worker_type="reviewer", input={"prompt": "hi"})
        await dispatch_and_wait_for_result(
            bus=bus,
            task=task,
            task_data=wrap("core.TaskMessage", task).model_dump(mode="json"),
            goal_id="goal-council",
            timeout=0.05,
        )

        _assert_subscribe_before_publish(bus.calls, result_subject="heddle.results.goal-council")

    @pytest.mark.asyncio
    async def test_inline_worker_reply_is_delivered(self):
        from heddle.orchestrator.dispatch import dispatch_and_wait_for_result

        bus = WorkerSimBus(goal_id="goal-council-race", output={"content": "ok"})
        await bus.connect()

        task = TaskMessage(worker_type="reviewer", input={"prompt": "hi"})
        result = await dispatch_and_wait_for_result(
            bus=bus,
            task=task,
            task_data=wrap("core.TaskMessage", task).model_dump(mode="json"),
            goal_id="goal-council-race",
            timeout=2.0,
        )

        assert result is not None, "fast worker reply was lost — race not fixed"
        assert result.status == TaskStatus.COMPLETED


# ---------------------------------------------------------------------------
# 4. ResultStream contract — iteration without start() raises (already covered
#    in test_result_stream.py).  Cross-reference here so future readers can
#    find the full picture in one place.
# ---------------------------------------------------------------------------


def test_resultstream_contract_is_in_test_result_stream_py():
    """Pointer for the `must call start()` enforcement test.

    See ``tests/test_result_stream.py::TestStreamingIteration::
    test_iteration_outside_async_with_raises`` for the assertion that
    iterating a ResultStream without entering its ``async with`` block
    is a ``RuntimeError`` — the structural enforcement of the
    subscribe-before-publish contract.
    """
