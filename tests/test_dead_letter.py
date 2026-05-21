"""
Unit tests for DeadLetterConsumer (router/dead_letter.py).

Tests cover:
- Message storage from bus subscription
- list_entries with limit/offset
- count() and clear()
- Bounded list eviction when max_size is exceeded
- replay() re-publishes to heddle.tasks.incoming
- Entry structure and metadata extraction
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from heddle.bus.memory import InMemoryBus
from heddle.core.messages import ModelTier, TaskMessage
from heddle.router.dead_letter import (
    INCOMING_SUBJECT,
    DeadLetterConsumer,
    DeadLetterEntry,
    ReplayRecord,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dead_letter_msg(
    reason: str = "rate_limited",
    worker_type: str = "summarizer",
    task_id: str | None = None,
) -> dict[str, Any]:
    """Create a dead-letter envelope as the router would publish it."""
    from heddle.core.envelope import wrap

    task = TaskMessage(
        worker_type=worker_type,
        input={"text": "hello"},
        model_tier=ModelTier.LOCAL,
    )
    data = wrap("core.TaskMessage", task).model_dump(mode="json")
    if task_id is None:
        task_id = data["payload"]["task_id"]
    return {
        "reason": reason,
        "original_task": data,
        "task_id": task_id,
        "worker_type": worker_type,
    }


# ---------------------------------------------------------------------------
# DeadLetterEntry tests
# ---------------------------------------------------------------------------


class TestDeadLetterEntry:
    def test_entry_has_required_fields(self):
        entry = DeadLetterEntry(
            original_task={"foo": "bar"},
            reason="test_reason",
            task_id="t-1",
            worker_type="summarizer",
        )
        assert entry.id is not None
        assert entry.timestamp is not None
        assert entry.reason == "test_reason"
        assert entry.task_id == "t-1"
        assert entry.worker_type == "summarizer"
        assert entry.original_task == {"foo": "bar"}

    def test_to_dict(self):
        entry = DeadLetterEntry(
            original_task={"x": 1},
            reason="bad",
            task_id="t-2",
            worker_type="classifier",
        )
        d = entry.to_dict()
        assert d["id"] == entry.id
        assert d["timestamp"] == entry.timestamp
        assert d["reason"] == "bad"
        assert d["task_id"] == "t-2"
        assert d["worker_type"] == "classifier"
        assert d["original_task"] == {"x": 1}

    def test_defaults_for_optional_fields(self):
        entry = DeadLetterEntry(original_task={}, reason="unknown")
        assert entry.task_id is None
        assert entry.worker_type is None


# ---------------------------------------------------------------------------
# DeadLetterConsumer.store tests
# ---------------------------------------------------------------------------


class TestStore:
    def test_store_adds_entry(self):
        bus = InMemoryBus()
        consumer = DeadLetterConsumer(bus=bus)
        consumer.store({"task": "data"}, "test_reason", task_id="t-1", worker_type="w")
        assert consumer.count() == 1

    def test_store_most_recent_first(self):
        bus = InMemoryBus()
        consumer = DeadLetterConsumer(bus=bus)
        consumer.store({"n": 1}, "r1", task_id="t-1")
        consumer.store({"n": 2}, "r2", task_id="t-2")
        entries = consumer.list_entries()
        assert entries[0]["task_id"] == "t-2"
        assert entries[1]["task_id"] == "t-1"

    def test_store_returns_entry(self):
        bus = InMemoryBus()
        consumer = DeadLetterConsumer(bus=bus)
        entry = consumer.store({"x": 1}, "reason")
        assert entry.reason == "reason"
        assert entry.original_task == {"x": 1}


# ---------------------------------------------------------------------------
# DeadLetterConsumer.list_entries tests
# ---------------------------------------------------------------------------


class TestListEntries:
    def test_default_limit(self):
        bus = InMemoryBus()
        consumer = DeadLetterConsumer(bus=bus)
        for i in range(60):
            consumer.store({"n": i}, f"r{i}", task_id=f"t-{i}")
        entries = consumer.list_entries()
        assert len(entries) == 50  # default limit

    def test_custom_limit(self):
        bus = InMemoryBus()
        consumer = DeadLetterConsumer(bus=bus)
        for i in range(10):
            consumer.store({"n": i}, f"r{i}")
        entries = consumer.list_entries(limit=3)
        assert len(entries) == 3

    def test_offset(self):
        bus = InMemoryBus()
        consumer = DeadLetterConsumer(bus=bus)
        for i in range(5):
            consumer.store({"n": i}, f"r{i}", task_id=f"t-{i}")
        # Most recent first: t-4, t-3, t-2, t-1, t-0
        entries = consumer.list_entries(limit=2, offset=2)
        assert len(entries) == 2
        assert entries[0]["task_id"] == "t-2"
        assert entries[1]["task_id"] == "t-1"

    def test_offset_beyond_entries(self):
        bus = InMemoryBus()
        consumer = DeadLetterConsumer(bus=bus)
        consumer.store({"n": 1}, "r1")
        entries = consumer.list_entries(offset=10)
        assert entries == []

    def test_empty_list(self):
        bus = InMemoryBus()
        consumer = DeadLetterConsumer(bus=bus)
        assert consumer.list_entries() == []


# ---------------------------------------------------------------------------
# DeadLetterConsumer.count and clear tests
# ---------------------------------------------------------------------------


class TestCountAndClear:
    def test_count_empty(self):
        bus = InMemoryBus()
        consumer = DeadLetterConsumer(bus=bus)
        assert consumer.count() == 0

    def test_count_after_stores(self):
        bus = InMemoryBus()
        consumer = DeadLetterConsumer(bus=bus)
        consumer.store({}, "r1")
        consumer.store({}, "r2")
        assert consumer.count() == 2

    def test_clear(self):
        bus = InMemoryBus()
        consumer = DeadLetterConsumer(bus=bus)
        consumer.store({}, "r1")
        consumer.store({}, "r2")
        consumer.clear()
        assert consumer.count() == 0
        assert consumer.list_entries() == []


# ---------------------------------------------------------------------------
# Bounded list eviction tests
# ---------------------------------------------------------------------------


class TestBoundedList:
    def test_max_size_eviction(self):
        bus = InMemoryBus()
        consumer = DeadLetterConsumer(bus=bus, max_size=3)
        for i in range(5):
            consumer.store({"n": i}, f"r{i}", task_id=f"t-{i}")
        assert consumer.count() == 3
        # Should have the 3 most recent
        entries = consumer.list_entries()
        task_ids = [e["task_id"] for e in entries]
        assert task_ids == ["t-4", "t-3", "t-2"]

    def test_max_size_of_one(self):
        bus = InMemoryBus()
        consumer = DeadLetterConsumer(bus=bus, max_size=1)
        consumer.store({"n": 1}, "r1", task_id="t-1")
        consumer.store({"n": 2}, "r2", task_id="t-2")
        assert consumer.count() == 1
        entries = consumer.list_entries()
        assert entries[0]["task_id"] == "t-2"


# ---------------------------------------------------------------------------
# DeadLetterConsumer.handle_message tests (bus subscription)
# ---------------------------------------------------------------------------


class TestHandleMessage:
    @pytest.mark.asyncio
    async def test_message_stored_from_bus(self):
        """Messages published to dead_letter subject are stored."""
        bus = InMemoryBus()
        consumer = DeadLetterConsumer(bus=bus)
        await bus.connect()

        msg = _make_dead_letter_msg(reason="rate_limited", worker_type="summarizer")
        await consumer.handle_message(msg)

        assert consumer.count() == 1
        entries = consumer.list_entries()
        assert entries[0]["reason"] == "rate_limited"
        assert entries[0]["worker_type"] == "summarizer"

    @pytest.mark.asyncio
    async def test_message_extracts_original_task(self):
        """The original_task is extracted from the dead-letter envelope."""
        bus = InMemoryBus()
        consumer = DeadLetterConsumer(bus=bus)

        msg = _make_dead_letter_msg()
        await consumer.handle_message(msg)

        entries = consumer.list_entries()
        assert "payload" in entries[0]["original_task"]
        assert entries[0]["original_task"]["payload"]["worker_type"] == "summarizer"

    @pytest.mark.asyncio
    async def test_message_without_reason_defaults_to_unknown(self):
        """Messages missing 'reason' default to 'unknown'."""
        bus = InMemoryBus()
        consumer = DeadLetterConsumer(bus=bus)

        await consumer.handle_message({"original_task": {"x": 1}})

        entries = consumer.list_entries()
        assert entries[0]["reason"] == "unknown"

    @pytest.mark.asyncio
    async def test_message_without_original_task_uses_data(self):
        """If no 'original_task' key, the entire data dict is stored."""
        bus = InMemoryBus()
        consumer = DeadLetterConsumer(bus=bus)

        raw = {"reason": "test", "some_field": "value"}
        await consumer.handle_message(raw)

        entries = consumer.list_entries()
        assert entries[0]["original_task"] == raw


# ---------------------------------------------------------------------------
# DeadLetterConsumer.replay tests
# ---------------------------------------------------------------------------


class TestReplay:
    @pytest.mark.asyncio
    async def test_replay_publishes_to_incoming(self):
        """Replay re-publishes the original task to heddle.tasks.incoming."""
        bus = InMemoryBus()
        await bus.connect()
        consumer = DeadLetterConsumer(bus=bus)

        original_task = {"worker_type": "summarizer", "payload": {"text": "hi"}}
        entry = consumer.store(original_task, "rate_limited", task_id="t-1")

        # Subscribe to incoming to verify the replayed message
        incoming_sub = await bus.subscribe(INCOMING_SUBJECT)

        result = await consumer.replay(entry.id, bus)
        assert result is True

        msg = await asyncio.wait_for(incoming_sub.__anext__(), timeout=2.0)
        assert msg == original_task

    @pytest.mark.asyncio
    async def test_replay_removes_entry(self):
        """After replay, the entry is removed from the consumer."""
        bus = InMemoryBus()
        await bus.connect()
        consumer = DeadLetterConsumer(bus=bus)

        entry = consumer.store({"x": 1}, "r1", task_id="t-1")
        assert consumer.count() == 1

        await consumer.replay(entry.id, bus)
        assert consumer.count() == 0

    @pytest.mark.asyncio
    async def test_replay_unknown_entry_returns_false(self):
        """Replaying a nonexistent entry ID returns False."""
        bus = InMemoryBus()
        await bus.connect()
        consumer = DeadLetterConsumer(bus=bus)

        result = await consumer.replay("nonexistent-id", bus)
        assert result is False

    @pytest.mark.asyncio
    async def test_replay_correct_entry_among_multiple(self):
        """Replay targets the correct entry when multiple exist."""
        bus = InMemoryBus()
        await bus.connect()
        consumer = DeadLetterConsumer(bus=bus)

        consumer.store({"n": 1}, "r1", task_id="t-1")
        e2 = consumer.store({"n": 2}, "r2", task_id="t-2")
        consumer.store({"n": 3}, "r3", task_id="t-3")

        incoming_sub = await bus.subscribe(INCOMING_SUBJECT)

        await consumer.replay(e2.id, bus)
        assert consumer.count() == 2

        msg = await asyncio.wait_for(incoming_sub.__anext__(), timeout=2.0)
        assert msg == {"n": 2}

        # Remaining entries should be e3 and e1 (most recent first)
        entries = consumer.list_entries()
        task_ids = [e["task_id"] for e in entries]
        assert "t-2" not in task_ids
        assert "t-3" in task_ids
        assert "t-1" in task_ids


# ---------------------------------------------------------------------------
# Integration: full flow via bus
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ReplayRecord tests
# ---------------------------------------------------------------------------


class TestReplayRecord:
    def test_record_has_required_fields(self):
        record = ReplayRecord(
            entry_id="e-1",
            task_id="t-1",
            worker_type="summarizer",
            original_reason="rate_limited",
        )
        assert record.entry_id == "e-1"
        assert record.task_id == "t-1"
        assert record.worker_type == "summarizer"
        assert record.original_reason == "rate_limited"
        assert record.replayed_at is not None

    def test_to_dict(self):
        record = ReplayRecord(entry_id="e-1", task_id="t-1")
        d = record.to_dict()
        assert d["entry_id"] == "e-1"
        assert d["task_id"] == "t-1"
        assert "replayed_at" in d

    def test_defaults(self):
        record = ReplayRecord(entry_id="e-1")
        assert record.task_id is None
        assert record.worker_type is None
        assert record.original_reason == ""


# ---------------------------------------------------------------------------
# Replay audit log tests
# ---------------------------------------------------------------------------


class TestReplayLog:
    @pytest.mark.asyncio
    async def test_replay_records_in_log(self):
        bus = InMemoryBus()
        await bus.connect()
        consumer = DeadLetterConsumer(bus=bus)

        entry = consumer.store({"x": 1}, "rate_limited", task_id="t-1", worker_type="w1")
        await consumer.replay(entry.id, bus)

        assert consumer.replay_count() == 1
        log = consumer.replay_log()
        assert len(log) == 1
        assert log[0]["entry_id"] == entry.id
        assert log[0]["task_id"] == "t-1"
        assert log[0]["worker_type"] == "w1"
        assert log[0]["original_reason"] == "rate_limited"

    @pytest.mark.asyncio
    async def test_replay_log_most_recent_first(self):
        bus = InMemoryBus()
        await bus.connect()
        consumer = DeadLetterConsumer(bus=bus)

        e1 = consumer.store({"n": 1}, "r1", task_id="t-1")
        e2 = consumer.store({"n": 2}, "r2", task_id="t-2")
        await consumer.replay(e1.id, bus)
        await consumer.replay(e2.id, bus)

        log = consumer.replay_log()
        assert len(log) == 2
        # Most recent first
        assert log[0]["task_id"] == "t-2"
        assert log[1]["task_id"] == "t-1"

    @pytest.mark.asyncio
    async def test_replay_log_limit(self):
        bus = InMemoryBus()
        await bus.connect()
        consumer = DeadLetterConsumer(bus=bus)

        for i in range(5):
            entry = consumer.store({"n": i}, f"r{i}", task_id=f"t-{i}")
            await consumer.replay(entry.id, bus)

        log = consumer.replay_log(limit=3)
        assert len(log) == 3

    @pytest.mark.asyncio
    async def test_replay_log_empty_initially(self):
        bus = InMemoryBus()
        consumer = DeadLetterConsumer(bus=bus)
        assert consumer.replay_log() == []
        assert consumer.replay_count() == 0

    @pytest.mark.asyncio
    async def test_failed_replay_not_in_log(self):
        bus = InMemoryBus()
        await bus.connect()
        consumer = DeadLetterConsumer(bus=bus)

        await consumer.replay("nonexistent-id", bus)
        assert consumer.replay_count() == 0

    @pytest.mark.asyncio
    async def test_replay_log_bounded_fifo(self):
        """Pin Invariant 12: ``_replay_log`` is a bounded FIFO.

        Without the cap the audit log grows unboundedly and a long-running
        router leaks memory.  Drives more than ``max_size`` replays through
        the public path and asserts the cap is enforced AND that the OLDEST
        records are evicted (not the newest).
        """
        bus = InMemoryBus()
        await bus.connect()
        consumer = DeadLetterConsumer(bus=bus, max_size=5)

        # Store + replay max_size + 10 = 15 cycles.  Each replay appends one
        # record to ``_replay_log`` and pops the entry from ``_entries``.
        for i in range(15):
            entry = consumer.store(
                {"n": i},
                f"reason-{i}",
                task_id=f"t-{i}",
                worker_type="w",
            )
            ok = await consumer.replay(entry.id, bus)
            assert ok

        # Cap enforced.
        assert len(consumer._replay_log) == 5
        assert consumer.replay_count() == 5

        # Oldest evicted: only entries 10..14 remain (most recent 5).
        # ``replay_log()`` returns most-recent-first, so the first item is i=14.
        log = consumer.replay_log(limit=100)
        task_ids = [r["task_id"] for r in log]
        assert task_ids == ["t-14", "t-13", "t-12", "t-11", "t-10"]


# ---------------------------------------------------------------------------
# Integration: full flow via bus
# ---------------------------------------------------------------------------


class TestFullFlow:
    @pytest.mark.asyncio
    async def test_end_to_end_store_and_replay(self):
        """Verify the full flow: receive -> store -> list -> replay."""
        bus = InMemoryBus()
        await bus.connect()
        consumer = DeadLetterConsumer(bus=bus, max_size=100)

        # Simulate 3 dead-letter messages arriving
        for i in range(3):
            msg = _make_dead_letter_msg(
                reason=f"reason_{i}",
                worker_type=f"worker_{i}",
                task_id=f"task-{i}",
            )
            await consumer.handle_message(msg)

        assert consumer.count() == 3

        # List should be most recent first
        entries = consumer.list_entries()
        assert entries[0]["task_id"] == "task-2"
        assert entries[2]["task_id"] == "task-0"

        # Replay the middle entry
        incoming_sub = await bus.subscribe(INCOMING_SUBJECT)
        entry_id = entries[1]["id"]
        result = await consumer.replay(entry_id, bus)
        assert result is True
        assert consumer.count() == 2

        replayed = await asyncio.wait_for(incoming_sub.__anext__(), timeout=2.0)
        assert replayed["payload"]["worker_type"] == "worker_1"

        # Verify replay was logged
        assert consumer.replay_count() == 1
        log = consumer.replay_log()
        assert log[0]["entry_id"] == entry_id
        assert log[0]["original_reason"] == "reason_1"

        # Clear remaining
        consumer.clear()
        assert consumer.count() == 0
