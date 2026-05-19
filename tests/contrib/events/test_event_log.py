"""Tests for EventLog ABC + InMemoryEventLog (Sprint 2 T4)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from heddle.contrib.events.envelopes import EventEnvelope, EventMetadata
from heddle.contrib.events.errors import ConcurrencyError
from heddle.contrib.events.event_log import InMemoryEventLog


def _ev(
    *,
    aggregate_type: str = "FakeT",
    aggregate_id: str = "a-1",
    version: int,
    payload: dict[str, Any] | None = None,
) -> EventEnvelope:
    now = datetime.now(UTC)
    return EventEnvelope(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        aggregate_version=version,
        event_type="ThingHappened",
        payload=payload or {},
        metadata=EventMetadata(issued_by="user:badge:test"),
        occurred_at=now,
        recorded_at=now,
    )


@pytest.mark.asyncio
async def test_append_then_load_preserves_order() -> None:
    log = InMemoryEventLog()
    for v in (1, 2, 3):
        await log.append(_ev(version=v), expected_version=v - 1)

    loaded = [ev async for ev in log.load("FakeT", "a-1")]
    assert [ev.aggregate_version for ev in loaded] == [1, 2, 3]


@pytest.mark.asyncio
async def test_expected_version_none_accepts_first_append() -> None:
    log = InMemoryEventLog()
    await log.append(_ev(version=1), expected_version=None)
    loaded = [ev async for ev in log.load("FakeT", "a-1")]
    assert len(loaded) == 1


@pytest.mark.asyncio
async def test_expected_version_none_accepts_mid_stream_append() -> None:
    """expected_version=None bypasses the CAS check entirely — used by
    PF observers that trust the source. Monotonicity still enforced."""
    log = InMemoryEventLog()
    await log.append(_ev(version=1), expected_version=None)
    await log.append(_ev(version=2), expected_version=None)
    loaded = [ev async for ev in log.load("FakeT", "a-1")]
    assert [ev.aggregate_version for ev in loaded] == [1, 2]


@pytest.mark.asyncio
async def test_expected_version_matches_succeeds() -> None:
    log = InMemoryEventLog()
    await log.append(_ev(version=1), expected_version=0)
    await log.append(_ev(version=2), expected_version=1)


@pytest.mark.asyncio
async def test_expected_version_mismatch_raises() -> None:
    log = InMemoryEventLog()
    await log.append(_ev(version=1), expected_version=0)
    with pytest.raises(ConcurrencyError, match="expected_version=5"):
        await log.append(_ev(version=2), expected_version=5)


@pytest.mark.asyncio
async def test_envelope_version_skip_raises_even_with_matching_expected() -> None:
    """Monotonicity is independent of expected_version. An envelope
    that skips a version raises even when expected_version matches the
    actual current."""
    log = InMemoryEventLog()
    await log.append(_ev(version=1), expected_version=0)
    with pytest.raises(ConcurrencyError, match="does not follow"):
        await log.append(_ev(version=3), expected_version=1)


@pytest.mark.asyncio
async def test_load_from_version_filters() -> None:
    log = InMemoryEventLog()
    for v in (1, 2, 3, 4):
        await log.append(_ev(version=v), expected_version=v - 1)

    loaded = [ev async for ev in log.load("FakeT", "a-1", from_version=2)]
    assert [ev.aggregate_version for ev in loaded] == [3, 4]


@pytest.mark.asyncio
async def test_load_unknown_aggregate_yields_nothing() -> None:
    log = InMemoryEventLog()
    loaded = [ev async for ev in log.load("FakeT", "nope")]
    assert loaded == []


@pytest.mark.asyncio
async def test_concurrent_appends_one_wins() -> None:
    """Two simultaneous appends at the same version: one wins, the
    other raises ConcurrencyError. The threading.Lock serialises the
    CAS check + write."""
    log = InMemoryEventLog()
    await log.append(_ev(version=1), expected_version=0)

    async def append(v: int) -> Exception | None:
        try:
            await log.append(_ev(version=v), expected_version=1)
            return None
        except ConcurrencyError as exc:
            return exc

    results = await asyncio.gather(append(2), append(2))
    # Exactly one None (winner) and one ConcurrencyError (loser).
    winners = [r for r in results if r is None]
    losers = [r for r in results if isinstance(r, ConcurrencyError)]
    assert len(winners) == 1
    assert len(losers) == 1


@pytest.mark.asyncio
async def test_subscribe_yields_events_appended_after() -> None:
    log = InMemoryEventLog()
    seen: list[int] = []
    started = asyncio.Event()

    async def consumer() -> None:
        async for ev in log.subscribe("FakeT"):
            seen.append(ev.aggregate_version)
            if not started.is_set():
                started.set()
            if len(seen) >= 2:
                return

    task = asyncio.create_task(consumer())
    # Give the subscriber a turn to register before we publish.
    await asyncio.sleep(0)
    await log.append(_ev(version=1), expected_version=0)
    await log.append(_ev(version=2), expected_version=1)
    await asyncio.wait_for(task, timeout=1.0)
    assert seen == [1, 2]


@pytest.mark.asyncio
async def test_subscribe_does_not_yield_prior_events() -> None:
    log = InMemoryEventLog()
    await log.append(_ev(version=1), expected_version=0)

    seen: list[int] = []

    async def consumer() -> None:
        async for ev in log.subscribe("FakeT"):
            seen.append(ev.aggregate_version)
            return

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0)
    await log.append(_ev(version=2), expected_version=1)
    await asyncio.wait_for(task, timeout=1.0)
    assert seen == [2]


@pytest.mark.asyncio
async def test_subscribe_cleanup_on_cancel() -> None:
    log = InMemoryEventLog()

    async def consumer() -> None:
        async for _ev in log.subscribe("FakeT"):
            pass

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0)  # let the subscriber register
    assert len(log._subscribers["FakeT"]) == 1

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        # Expected: cancellation propagates from the subscribe() async
        # generator out through the consumer task. The point of this
        # test is to verify cleanup happens regardless.
        pass

    assert log._subscribers.get("FakeT", []) == []
