"""Tests for the real FinalizationHorizonProjector (Sprint 3 T6).

Replaces the Sprint 2 stub-only test. Short horizons let the timer
fire within test time without slowing the suite.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from heddle.contrib.events.aggregate import IntervalAggregate
from heddle.contrib.events.command_handler import CommandHandler
from heddle.contrib.events.envelopes import EventEnvelope, EventMetadata
from heddle.contrib.events.event_log import InMemoryEventLog
from heddle.contrib.events.lease import lease_key
from heddle.contrib.events.projectors import FinalizationHorizonProjector
from heddle.contrib.events.projectors.finalization_horizon import HORIZON_ISSUED_BY
from heddle.contrib.events.registry import register_aggregate
from heddle.contrib.events.rejection_log import InMemoryRejectionLog
from heddle.core.kvstore import InMemoryKeyValueStore

pytestmark = pytest.mark.usefixtures("registry_isolation")


def _make_fast_horizon_class():
    @register_aggregate("FastHorizonT")
    class _FastHorizon(IntervalAggregate):
        HORIZON_TIMEOUT_SECONDS = 0.1

        def handle_seed(self, _payload: dict[str, Any], _meta: Any) -> tuple[str, dict[str, Any]]:
            return "Seeded", {}

        def apply_seeded(self, _payload: dict[str, Any], _meta: EventMetadata) -> None:
            pass

        def handle_internal_finalize(
            self, _payload: dict[str, Any], _meta: Any
        ) -> tuple[str, dict[str, Any]]:
            from heddle.contrib.events.errors import CommandRejected

            if self.phase == "finalized":
                raise CommandRejected("ALREADY_FINALIZED", "")
            return "InternalFinalized", {}

    return _FastHorizon


def _seed_event(
    *,
    aggregate_type: str = "FastHorizonT",
    aggregate_id: str = "a-1",
    version: int = 1,
    event_type: str = "Seeded",
    issued_by: str = "user:badge:1",
) -> EventEnvelope:
    now = datetime.now(UTC)
    return EventEnvelope(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        aggregate_version=version,
        event_type=event_type,
        payload={},
        metadata=EventMetadata(issued_by=issued_by),
        occurred_at=now,
        recorded_at=now,
    )


@pytest.mark.asyncio
async def test_horizon_fires_internal_finalize() -> None:
    """After the short horizon elapses, P3 publishes InternalFinalize
    with issued_by='framework:horizon' and the aggregate finalises."""
    cls = _make_fast_horizon_class()
    el = InMemoryEventLog()
    rl = InMemoryRejectionLog()
    kv = InMemoryKeyValueStore()
    handler = CommandHandler(el, rl)
    p3 = FinalizationHorizonProjector(kv, handler)

    # Seed the aggregate by appending an event and projecting it.
    seed = _seed_event(version=1)
    await el.append(seed, expected_version=0)
    await p3.project(seed)

    # Wait past the horizon plus a small slack for the publish round-trip.
    await asyncio.sleep(0.35)
    await p3.shutdown()

    events = [ev async for ev in el.load("FastHorizonT", "a-1")]
    finalized = [ev for ev in events if ev.event_type == "InternalFinalized"]
    assert len(finalized) == 1
    assert finalized[0].metadata.issued_by == HORIZON_ISSUED_BY
    _ = cls  # silence unused — registry side-effect is the point


@pytest.mark.asyncio
async def test_organic_finalize_cancels_horizon_timer() -> None:
    """If InternalFinalized is observed before the horizon, P3 cancels
    the timer — no second 'horizon' finalize event is produced."""
    _make_fast_horizon_class()
    el = InMemoryEventLog()
    rl = InMemoryRejectionLog()
    kv = InMemoryKeyValueStore()
    handler = CommandHandler(el, rl)
    p3 = FinalizationHorizonProjector(kv, handler)

    seed = _seed_event(version=1)
    await el.append(seed, expected_version=0)
    await p3.project(seed)

    # Synthesise the "organic finalize" observation.
    finalized_ev = _seed_event(
        version=2, event_type="InternalFinalized", issued_by="framework:cascade"
    )
    await p3.project(finalized_ev)

    await asyncio.sleep(0.25)
    await p3.shutdown()

    # No fresh InternalFinalized published by the horizon (the only
    # one in the log is the synthesised cascade observation, which we
    # never appended — verify by listing what's actually in the log).
    events = [ev async for ev in el.load("FastHorizonT", "a-1")]
    assert all(ev.event_type != "InternalFinalized" for ev in events)


@pytest.mark.asyncio
async def test_lease_contention_horizon_preempted() -> None:
    """Pre-claim the lease as cascade; P3's horizon fire sees the
    lease held and skips publishing."""
    _make_fast_horizon_class()
    el = InMemoryEventLog()
    rl = InMemoryRejectionLog()
    kv = InMemoryKeyValueStore()
    handler = CommandHandler(el, rl)
    p3 = FinalizationHorizonProjector(kv, handler)

    # Pre-claim the lease.
    await kv.set_if_not_exists(
        lease_key("FastHorizonT", "a-1"), "framework:cascade:abc", ttl_seconds=30
    )

    seed = _seed_event(version=1)
    await el.append(seed, expected_version=0)
    await p3.project(seed)
    await asyncio.sleep(0.35)
    await p3.shutdown()

    events = [ev async for ev in el.load("FastHorizonT", "a-1")]
    assert all(ev.event_type != "InternalFinalized" for ev in events)


@pytest.mark.asyncio
async def test_multiple_aggregates_have_independent_timers() -> None:
    _make_fast_horizon_class()
    el = InMemoryEventLog()
    rl = InMemoryRejectionLog()
    kv = InMemoryKeyValueStore()
    handler = CommandHandler(el, rl)
    p3 = FinalizationHorizonProjector(kv, handler)

    seed_a = _seed_event(aggregate_id="a", version=1)
    seed_b = _seed_event(aggregate_id="b", version=1)
    await el.append(seed_a, expected_version=0)
    await el.append(seed_b, expected_version=0)
    await p3.project(seed_a)
    await p3.project(seed_b)

    await asyncio.sleep(0.35)
    await p3.shutdown()

    a_events = [ev async for ev in el.load("FastHorizonT", "a")]
    b_events = [ev async for ev in el.load("FastHorizonT", "b")]
    assert any(ev.event_type == "InternalFinalized" for ev in a_events)
    assert any(ev.event_type == "InternalFinalized" for ev in b_events)


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_timers() -> None:
    _make_fast_horizon_class()
    el = InMemoryEventLog()
    rl = InMemoryRejectionLog()
    kv = InMemoryKeyValueStore()
    handler = CommandHandler(el, rl)
    p3 = FinalizationHorizonProjector(kv, handler)

    seed = _seed_event(version=1)
    await el.append(seed, expected_version=0)
    await p3.project(seed)
    # Shut down before the horizon fires.
    await p3.shutdown()
    await asyncio.sleep(0.25)

    events = [ev async for ev in el.load("FastHorizonT", "a-1")]
    assert all(ev.event_type != "InternalFinalized" for ev in events)


@pytest.mark.asyncio
async def test_non_interval_aggregate_does_not_start_timer() -> None:
    """Only IntervalAggregate subclasses get horizon timers."""
    el = InMemoryEventLog()
    rl = InMemoryRejectionLog()
    kv = InMemoryKeyValueStore()
    handler = CommandHandler(el, rl)
    p3 = FinalizationHorizonProjector(kv, handler)

    # Unknown aggregate_type — should not raise and should not arm.
    seed = _seed_event(aggregate_type="UnregisteredT", version=1)
    await p3.project(seed)
    assert p3._timers == {}
