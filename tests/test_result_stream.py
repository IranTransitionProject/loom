"""
Unit tests for ResultStream (orchestrator/stream.py).

Tests cover:
- Basic collection: all results arrive → collect_all returns them
- Timeout: partial results returned when deadline expires
- Duplicate filtering: same task_id arriving twice is skipped
- Unknown task_ids: results not in expected set are ignored
- Early exit via on_result callback returning True
- Callback errors are non-fatal
- Single-use enforcement: cannot iterate twice
- Async iteration (streaming mode)
- Properties: collected, timed_out, early_exited, pending_ids

All tests use InMemoryBus -- no NATS or external infrastructure required.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from heddle.bus.memory import InMemoryBus
from heddle.core.messages import TaskMessage, TaskResult, TaskStatus
from heddle.orchestrator.stream import ResultStream

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(worker_type: str = "summarizer") -> TaskMessage:
    """Create a TaskMessage with a unique task_id."""
    return TaskMessage(worker_type=worker_type, payload={"text": "test"})


def _make_result(
    task_id: str,
    worker_type: str = "summarizer",
    status: TaskStatus = TaskStatus.COMPLETED,
    output: dict | None = None,
) -> dict[str, Any]:
    """Create a TaskResult dict suitable for bus publishing."""
    result = TaskResult(
        task_id=task_id,
        worker_type=worker_type,
        status=status,
        output=output or {"summary": "done"},
        model_used="mock",
        processing_time_ms=50,
        token_usage={"prompt_tokens": 10, "completion_tokens": 5},
    )
    return result.model_dump(mode="json")


async def _bg_publish(bus: InMemoryBus, subject: str, payloads: list[dict]) -> None:
    """Publish a list of payloads with a small delay (background task helper)."""
    await asyncio.sleep(0.01)
    for payload in payloads:
        await bus.publish(subject, payload)


# ---------------------------------------------------------------------------
# Basic collection tests
# ---------------------------------------------------------------------------


class TestCollectAll:
    @pytest.mark.asyncio
    async def test_collects_all_expected_results(self):
        """All expected results arrive → collect_all returns them."""
        bus = InMemoryBus()
        await bus.connect()

        tasks = [_make_task() for _ in range(3)]
        expected_ids = {t.task_id for t in tasks}
        subject = "heddle.results.test-goal"

        _bg = asyncio.create_task(
            _bg_publish(bus, subject, [_make_result(t.task_id) for t in tasks]),
        )

        stream = ResultStream(
            bus=bus,
            subject=subject,
            expected_task_ids=expected_ids,
            timeout=5.0,
        )

        async with stream:
            results = await stream.collect_all()
        assert len(results) == 3
        assert stream.all_collected
        assert not stream.timed_out
        assert not stream.early_exited
        assert stream.pending_ids == frozenset()
        await _bg

    @pytest.mark.asyncio
    async def test_single_result(self):
        """Single expected result arrives correctly."""
        bus = InMemoryBus()
        await bus.connect()

        task = _make_task()
        subject = "heddle.results.single"

        _bg = asyncio.create_task(
            _bg_publish(bus, subject, [_make_result(task.task_id)]),
        )

        stream = ResultStream(
            bus=bus,
            subject=subject,
            expected_task_ids={task.task_id},
            timeout=5.0,
        )

        async with stream:
            results = await stream.collect_all()
        assert len(results) == 1
        assert results[0].task_id == task.task_id
        assert results[0].status == TaskStatus.COMPLETED
        await _bg

    @pytest.mark.asyncio
    async def test_empty_expected_set_returns_immediately(self):
        """No expected task IDs → collect_all returns empty immediately."""
        bus = InMemoryBus()
        await bus.connect()

        stream = ResultStream(
            bus=bus,
            subject="heddle.results.empty",
            expected_task_ids=set(),
            timeout=5.0,
        )

        async with stream:
            results = await stream.collect_all()
        assert results == []
        assert stream.all_collected


# ---------------------------------------------------------------------------
# Timeout tests
# ---------------------------------------------------------------------------


class TestTimeout:
    @pytest.mark.asyncio
    async def test_timeout_returns_partial_results(self):
        """When timeout fires before all results, returns what's collected."""
        bus = InMemoryBus()
        await bus.connect()

        tasks = [_make_task() for _ in range(3)]
        expected_ids = {t.task_id for t in tasks}
        subject = "heddle.results.timeout"

        # Only publish 1 of 3 results.
        _bg = asyncio.create_task(
            _bg_publish(bus, subject, [_make_result(tasks[0].task_id)]),
        )

        stream = ResultStream(
            bus=bus,
            subject=subject,
            expected_task_ids=expected_ids,
            timeout=0.3,
        )

        async with stream:
            results = await stream.collect_all()
        assert len(results) == 1
        assert stream.timed_out
        assert not stream.all_collected
        assert len(stream.pending_ids) == 2
        await _bg

    @pytest.mark.asyncio
    async def test_zero_timeout_returns_nothing(self):
        """Zero timeout returns immediately with nothing collected."""
        bus = InMemoryBus()
        await bus.connect()

        task = _make_task()
        stream = ResultStream(
            bus=bus,
            subject="heddle.results.zero",
            expected_task_ids={task.task_id},
            timeout=0.0,
        )

        async with stream:
            results = await stream.collect_all()
        assert results == []
        assert stream.timed_out


# ---------------------------------------------------------------------------
# Filtering tests
# ---------------------------------------------------------------------------


class TestFiltering:
    @pytest.mark.asyncio
    async def test_ignores_unexpected_task_ids(self):
        """Results for task_ids not in expected set are silently dropped."""
        bus = InMemoryBus()
        await bus.connect()

        expected_task = _make_task()
        unexpected_task = _make_task()
        subject = "heddle.results.filter"

        # Publish unexpected first, then expected.
        _bg = asyncio.create_task(
            _bg_publish(
                bus,
                subject,
                [
                    _make_result(unexpected_task.task_id),
                    _make_result(expected_task.task_id),
                ],
            ),
        )

        stream = ResultStream(
            bus=bus,
            subject=subject,
            expected_task_ids={expected_task.task_id},
            timeout=5.0,
        )

        async with stream:
            results = await stream.collect_all()
        assert len(results) == 1
        assert results[0].task_id == expected_task.task_id
        await _bg

    @pytest.mark.asyncio
    async def test_skips_duplicate_results(self):
        """Same task_id arriving twice is collected only once."""
        bus = InMemoryBus()
        await bus.connect()

        task = _make_task()
        subject = "heddle.results.dedup"

        # Publish the same result twice.
        _bg = asyncio.create_task(
            _bg_publish(
                bus,
                subject,
                [
                    _make_result(task.task_id),
                    _make_result(task.task_id),
                ],
            ),
        )

        stream = ResultStream(
            bus=bus,
            subject=subject,
            expected_task_ids={task.task_id},
            timeout=5.0,
        )

        async with stream:
            results = await stream.collect_all()
        assert len(results) == 1
        await _bg

    @pytest.mark.asyncio
    async def test_skips_malformed_messages(self):
        """Messages that fail TaskResult parsing are skipped."""
        bus = InMemoryBus()
        await bus.connect()

        task = _make_task()
        subject = "heddle.results.malformed"

        async def publish():
            await asyncio.sleep(0.01)
            # Publish malformed data (missing required fields).
            await bus.publish(subject, {"task_id": task.task_id, "garbage": True})
            # Then publish a valid result.
            await bus.publish(subject, _make_result(task.task_id))

        _bg = asyncio.create_task(publish())

        stream = ResultStream(
            bus=bus,
            subject=subject,
            expected_task_ids={task.task_id},
            timeout=5.0,
        )

        async with stream:
            results = await stream.collect_all()
        assert len(results) == 1
        assert results[0].task_id == task.task_id
        await _bg


# ---------------------------------------------------------------------------
# Callback tests
# ---------------------------------------------------------------------------


class TestCallbacks:
    @pytest.mark.asyncio
    async def test_on_result_called_for_each_result(self):
        """on_result callback is invoked with correct counts."""
        bus = InMemoryBus()
        await bus.connect()

        tasks = [_make_task() for _ in range(3)]
        subject = "heddle.results.callback"
        callback_log: list[tuple[str, int, int]] = []

        async def on_result(result, collected, expected):
            callback_log.append((result.task_id, collected, expected))

        _bg = asyncio.create_task(
            _bg_publish(bus, subject, [_make_result(t.task_id) for t in tasks]),
        )

        stream = ResultStream(
            bus=bus,
            subject=subject,
            expected_task_ids={t.task_id for t in tasks},
            timeout=5.0,
            on_result=on_result,
        )

        async with stream:
            await stream.collect_all()
        assert len(callback_log) == 3
        # Counts should be 1/3, 2/3, 3/3
        assert callback_log[0][1:] == (1, 3)
        assert callback_log[1][1:] == (2, 3)
        assert callback_log[2][1:] == (3, 3)
        await _bg

    @pytest.mark.asyncio
    async def test_early_exit_via_callback(self):
        """Returning True from on_result stops collection."""
        bus = InMemoryBus()
        await bus.connect()

        tasks = [_make_task() for _ in range(5)]
        subject = "heddle.results.early"

        async def stop_after_two(result, collected, expected):
            return collected >= 2

        _bg = asyncio.create_task(
            _bg_publish(bus, subject, [_make_result(t.task_id) for t in tasks]),
        )

        stream = ResultStream(
            bus=bus,
            subject=subject,
            expected_task_ids={t.task_id for t in tasks},
            timeout=5.0,
            on_result=stop_after_two,
        )

        async with stream:
            results = await stream.collect_all()
        assert len(results) == 2
        assert stream.early_exited
        assert not stream.timed_out
        assert not stream.all_collected
        await _bg

    @pytest.mark.asyncio
    async def test_sync_callback_supported(self):
        """Non-async callbacks also work (duck typing)."""
        bus = InMemoryBus()
        await bus.connect()

        task = _make_task()
        subject = "heddle.results.sync-cb"
        called = []

        def sync_callback(result, collected, expected):
            called.append(result.task_id)

        _bg = asyncio.create_task(
            _bg_publish(bus, subject, [_make_result(task.task_id)]),
        )

        stream = ResultStream(
            bus=bus,
            subject=subject,
            expected_task_ids={task.task_id},
            timeout=5.0,
            on_result=sync_callback,
        )

        async with stream:
            await stream.collect_all()
        assert called == [task.task_id]
        await _bg

    @pytest.mark.asyncio
    async def test_callback_error_is_non_fatal(self):
        """If on_result raises, collection continues."""
        bus = InMemoryBus()
        await bus.connect()

        tasks = [_make_task() for _ in range(2)]
        subject = "heddle.results.cb-error"

        async def failing_callback(result, collected, expected):
            if collected == 1:
                raise ValueError("Callback boom!")

        _bg = asyncio.create_task(
            _bg_publish(bus, subject, [_make_result(t.task_id) for t in tasks]),
        )

        stream = ResultStream(
            bus=bus,
            subject=subject,
            expected_task_ids={t.task_id for t in tasks},
            timeout=5.0,
            on_result=failing_callback,
        )

        # Should not raise — callback error is swallowed.
        async with stream:
            results = await stream.collect_all()
        assert len(results) == 2
        await _bg


# ---------------------------------------------------------------------------
# Streaming iteration tests
# ---------------------------------------------------------------------------


class TestStreamingIteration:
    @pytest.mark.asyncio
    async def test_async_for_yields_incrementally(self):
        """async for yields results one at a time."""
        bus = InMemoryBus()
        await bus.connect()

        tasks = [_make_task() for _ in range(3)]
        subject = "heddle.results.stream"

        _bg = asyncio.create_task(
            _bg_publish(bus, subject, [_make_result(t.task_id) for t in tasks]),
        )

        stream = ResultStream(
            bus=bus,
            subject=subject,
            expected_task_ids={t.task_id for t in tasks},
            timeout=5.0,
        )

        async with stream:
            yielded = [result.task_id async for result in stream]
        assert len(yielded) == 3
        await _bg

    @pytest.mark.asyncio
    async def test_cannot_iterate_twice(self):
        """ResultStream raises RuntimeError on second iteration within a started context."""
        bus = InMemoryBus()
        await bus.connect()

        stream = ResultStream(
            bus=bus,
            subject="heddle.results.once",
            expected_task_ids=set(),
            timeout=1.0,
        )

        async with stream:
            await stream.collect_all()

            with pytest.raises(RuntimeError, match="already been consumed"):
                await stream.collect_all()

    @pytest.mark.asyncio
    async def test_iteration_outside_async_with_raises(self):
        """Iterating without an active subscription is a ``RuntimeError``.

        This is the publish-before-subscribe race tripwire: callers MUST
        enter the ``async with`` block (or call ``start()``) before
        iterating, otherwise fast workers' results would be lost
        silently.
        """
        bus = InMemoryBus()
        await bus.connect()

        stream = ResultStream(
            bus=bus,
            subject="heddle.results.no-with",
            expected_task_ids={"task-x"},
            timeout=0.1,
        )

        with pytest.raises(RuntimeError, match="must be started"):
            await stream.collect_all()


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


class TestProperties:
    @pytest.mark.asyncio
    async def test_initial_state(self):
        """Properties have correct initial values before iteration."""
        bus = InMemoryBus()
        await bus.connect()

        stream = ResultStream(
            bus=bus,
            subject="heddle.results.props",
            expected_task_ids={"a", "b", "c"},
            timeout=5.0,
        )

        assert stream.expected_count == 3
        assert stream.collected_count == 0
        assert not stream.all_collected
        assert not stream.timed_out
        assert not stream.early_exited
        assert stream.pending_ids == frozenset({"a", "b", "c"})

    @pytest.mark.asyncio
    async def test_pending_ids_shrinks_as_results_arrive(self):
        """pending_ids reflects uncollected task IDs."""
        bus = InMemoryBus()
        await bus.connect()

        tasks = [_make_task() for _ in range(3)]
        subject = "heddle.results.pending"

        # Only publish 2 of 3.
        _bg = asyncio.create_task(
            _bg_publish(
                bus,
                subject,
                [
                    _make_result(tasks[0].task_id),
                    _make_result(tasks[1].task_id),
                ],
            ),
        )

        stream = ResultStream(
            bus=bus,
            subject=subject,
            expected_task_ids={t.task_id for t in tasks},
            timeout=0.3,
        )

        async with stream:
            await stream.collect_all()
        assert stream.pending_ids == frozenset({tasks[2].task_id})
        await _bg


# ---------------------------------------------------------------------------
# Integration with failed results
# ---------------------------------------------------------------------------


class TestFailedResults:
    @pytest.mark.asyncio
    async def test_failed_results_are_collected(self):
        """FAILED status results are collected (not filtered out)."""
        bus = InMemoryBus()
        await bus.connect()

        task = _make_task()
        subject = "heddle.results.failed"

        async def publish():
            await asyncio.sleep(0.01)
            result = TaskResult(
                task_id=task.task_id,
                worker_type="summarizer",
                status=TaskStatus.FAILED,
                error="Worker crashed",
                processing_time_ms=0,
            )
            await bus.publish(subject, result.model_dump(mode="json"))

        _bg = asyncio.create_task(publish())

        stream = ResultStream(
            bus=bus,
            subject=subject,
            expected_task_ids={task.task_id},
            timeout=5.0,
        )

        async with stream:
            results = await stream.collect_all()
        assert len(results) == 1
        assert results[0].status == TaskStatus.FAILED
        assert results[0].error == "Worker crashed"
        await _bg
