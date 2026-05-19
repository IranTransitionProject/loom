"""Tests for Projector ABC + EventDispatcher (Sprint 2 T7)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from heddle.contrib.events.dispatcher import EventDispatcher, Projector
from heddle.contrib.events.envelopes import EventEnvelope, EventMetadata
from heddle.contrib.events.event_log import InMemoryEventLog


def _ev(*, agg_type: str = "FakeT", agg_id: str = "a-1", version: int = 1) -> EventEnvelope:
    now = datetime.now(timezone.utc)
    return EventEnvelope(
        aggregate_type=agg_type,
        aggregate_id=agg_id,
        aggregate_version=version,
        event_type="ThingHappened",
        payload={},
        metadata=EventMetadata(issued_by="user:badge:test"),
        occurred_at=now,
        recorded_at=now,
    )


class _RecorderProjector(Projector):
    def __init__(self, name: str) -> None:
        self.name = name
        self.seen: list[tuple[str, str, int]] = []

    async def project(self, envelope: EventEnvelope) -> None:
        self.seen.append(
            (envelope.aggregate_type, envelope.aggregate_id, envelope.aggregate_version)
        )


class _BoomProjector(Projector):
    async def project(self, envelope: EventEnvelope) -> None:
        raise RuntimeError("intentional projector failure")


@pytest.mark.asyncio
async def test_register_then_project() -> None:
    log = InMemoryEventLog()
    p = _RecorderProjector("p1")
    d = EventDispatcher(log)
    d.register(p)
    await d.start("FakeT")
    try:
        await asyncio.sleep(0)
        await log.append(_ev(version=1), expected_version=0)
        await asyncio.sleep(0.05)
        assert p.seen == [("FakeT", "a-1", 1)]
    finally:
        await d.stop()


@pytest.mark.asyncio
async def test_multiple_projectors_in_registration_order() -> None:
    log = InMemoryEventLog()
    p1 = _RecorderProjector("p1")
    p2 = _RecorderProjector("p2")
    order: list[str] = []

    class _Tracer(Projector):
        def __init__(self, name: str) -> None:
            self.name = name

        async def project(self, envelope: EventEnvelope) -> None:
            order.append(self.name)

    t1 = _Tracer("t1")
    t2 = _Tracer("t2")

    d = EventDispatcher(log)
    d.register(t1)
    d.register(t2)
    await d.start("FakeT")
    try:
        await asyncio.sleep(0)
        await log.append(_ev(version=1), expected_version=0)
        await asyncio.sleep(0.05)
        assert order == ["t1", "t2"]
    finally:
        await d.stop()


@pytest.mark.asyncio
async def test_projector_exception_does_not_stop_subsequent_projectors() -> None:
    log = InMemoryEventLog()
    boom = _BoomProjector()
    recorder = _RecorderProjector("after-boom")

    d = EventDispatcher(log)
    d.register(boom)
    d.register(recorder)
    await d.start("FakeT")
    try:
        await asyncio.sleep(0)
        await log.append(_ev(version=1), expected_version=0)
        await asyncio.sleep(0.05)
        assert recorder.seen == [("FakeT", "a-1", 1)]
    finally:
        await d.stop()


@pytest.mark.asyncio
async def test_start_is_idempotent_per_type() -> None:
    log = InMemoryEventLog()
    d = EventDispatcher(log)
    await d.start("FakeT")
    await d.start("FakeT")
    try:
        assert len(d._running) == 1
    finally:
        await d.stop()


@pytest.mark.asyncio
async def test_stop_cancels_all_tasks() -> None:
    log = InMemoryEventLog()
    d = EventDispatcher(log)
    await d.start("FakeT")
    await d.start("OtherT")
    assert len(d._running) == 2
    await d.stop()
    assert d._running == {}


@pytest.mark.asyncio
async def test_two_aggregate_types_run_independently() -> None:
    log = InMemoryEventLog()
    p = _RecorderProjector("p")
    d = EventDispatcher(log)
    d.register(p)
    await d.start("TypeA")
    await d.start("TypeB")
    try:
        await asyncio.sleep(0)
        await log.append(_ev(agg_type="TypeA", version=1), expected_version=0)
        await log.append(_ev(agg_type="TypeB", version=1), expected_version=0)
        await asyncio.sleep(0.05)
        seen_types = {e[0] for e in p.seen}
        assert seen_types == {"TypeA", "TypeB"}
    finally:
        await d.stop()


@pytest.mark.asyncio
async def test_re_register_same_projector_instance_is_noop() -> None:
    log = InMemoryEventLog()
    p = _RecorderProjector("p")
    d = EventDispatcher(log)
    d.register(p)
    d.register(p)
    assert len(d._projectors) == 1
