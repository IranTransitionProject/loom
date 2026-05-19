"""Tests for the hybrid mark_processed mechanism (Sprint 3 T4 / R7).

The Nats{Dedup}Publisher / Nats{Dedup}Subscriber pair speaks the
narrow NATS-core surface ``publish(subject, bytes)`` /
``subscribe(subject, cb=...) -> Subscription`` /
``Subscription.unsubscribe()``. We exercise them against an in-memory
double of that surface rather than spinning up a real NATS server —
the JetStream integration tests (T17) cover the real-bus path.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from heddle.contrib.events.aggregate import IntervalAggregate
from heddle.contrib.events.cache import AggregateCache
from heddle.contrib.events.command_handler import CommandHandler
from heddle.contrib.events.dedup_publisher import (
    NatsDedupPublisher,
    NullDedupPublisher,
    dedup_subject,
)
from heddle.contrib.events.dedup_subscriber import (
    NatsDedupSubscriber,
    NullDedupSubscriber,
)
from heddle.contrib.events.envelopes import CommandMessage, CommandMetadata
from heddle.contrib.events.event_log import InMemoryEventLog
from heddle.contrib.events.registry import register_aggregate
from heddle.contrib.events.rejection_log import InMemoryRejectionLog
from heddle.contrib.events.snapshot_store import SnapshotStore
from heddle.core.kvstore import InMemoryKeyValueStore

pytestmark = pytest.mark.usefixtures("registry_isolation")


@dataclass
class _FakeMsg:
    data: bytes


class _FakeSub:
    def __init__(self, bus: InMemoryNatsBus, subject: str) -> None:
        self._bus = bus
        self._subject = subject
        self.active = True

    async def unsubscribe(self) -> None:
        self.active = False
        self._bus._remove(self._subject, self)


class InMemoryNatsBus:
    """Minimal NATS-core stand-in: publish/subscribe with async callbacks."""

    def __init__(self) -> None:
        self._subs: dict[str, list[tuple[_FakeSub, Any]]] = {}

    async def publish(self, subject: str, data: bytes) -> None:
        for sub, cb in list(self._subs.get(subject, [])):
            if sub.active:
                await cb(_FakeMsg(data=data))

    async def subscribe(self, subject: str, cb: Any) -> _FakeSub:
        sub = _FakeSub(self, subject)
        self._subs.setdefault(subject, []).append((sub, cb))
        return sub

    def _remove(self, subject: str, sub: _FakeSub) -> None:
        self._subs[subject] = [(s, c) for (s, c) in self._subs.get(subject, []) if s is not sub]

    def sub_count(self, subject: str) -> int:
        return len([s for (s, _c) in self._subs.get(subject, []) if s.active])


def _make_class():
    @register_aggregate("HybridT")
    class _HybridT(IntervalAggregate):
        def handle_do_thing(
            self, payload: dict[str, Any], _meta: CommandMetadata
        ) -> tuple[str, dict[str, Any]]:
            return "ThingHappened", dict(payload)

        def apply_thing_happened(self, payload: dict[str, Any], _meta: Any) -> None:
            pass

    return _HybridT


def _cmd(*, command_id: str, expected_aggregate_version: int | None = None) -> CommandMessage:
    return CommandMessage(
        aggregate_type="HybridT",
        aggregate_id="a-1",
        command_type="DoThing",
        payload={},
        metadata=CommandMetadata(issued_by="user:badge:1"),
        issued_at=datetime.now(UTC),
        expected_aggregate_version=expected_aggregate_version,
        command_id=command_id,
    )


@pytest.mark.asyncio
async def test_dedup_subject_format() -> None:
    assert dedup_subject("Foo", "bar") == "heddle.dedup.Foo.bar"


@pytest.mark.asyncio
async def test_null_implementations_noop() -> None:
    pub = NullDedupPublisher()
    sub = NullDedupSubscriber()
    cache: AggregateCache = AggregateCache()
    await pub.publish("X", "y", "cmd")
    await sub.subscribe("X", "y", cache)
    await sub.unsubscribe("X", "y")


@pytest.mark.asyncio
async def test_cross_process_dedup_via_bus() -> None:
    """Two CommandHandler instances share the bus + snapshot store.
    Handler1 processes ``cmd-X``; the published mark_processed event
    propagates into Handler2's cached aggregate so Handler2's
    subsequent ``cmd-X`` returns the original event (cross-process
    dedup observable)."""
    _make_class()
    bus = InMemoryNatsBus()
    el = InMemoryEventLog()
    rl = InMemoryRejectionLog()
    kv = InMemoryKeyValueStore()
    snapshots = SnapshotStore(kv)

    h1 = CommandHandler(
        el, rl,
        snapshot_store=snapshots,
        dedup_publisher=NatsDedupPublisher(bus),  # type: ignore[arg-type]
        dedup_subscriber=NatsDedupSubscriber(bus),  # type: ignore[arg-type]
    )
    h2 = CommandHandler(
        el, rl,
        snapshot_store=snapshots,
        dedup_publisher=NatsDedupPublisher(bus),  # type: ignore[arg-type]
        dedup_subscriber=NatsDedupSubscriber(bus),  # type: ignore[arg-type]
    )

    # Prime h2's cache so it has the aggregate and a live subscription.
    from heddle.contrib.events.registry import get_aggregate_class as _gac
    await h2._load_or_create(_gac("HybridT"), "a-1")

    # Sanity: h2 subscribed on cache.put → bus has the subscription.
    assert bus.sub_count(dedup_subject("HybridT", "a-1")) == 1

    # Handler1 processes cmd-X. Publish propagates to h2's cache.
    env1 = await h1.handle(_cmd(command_id="cmd-X"))

    # Yield so the awaitable in the subscriber callback completes.
    await asyncio.sleep(0)

    h2_cached = h2._cache.get(("HybridT", "a-1"))
    assert h2_cached is not None
    assert h2_cached.has_processed("cmd-X")

    # Handler2 re-runs cmd-X. Snapshot path returns the original event.
    # (Handler1's snapshot-on-write fired because snapshot_every_n=100 default;
    # we need to assert h2 finds it via the cache, which already has it.)
    env2 = await h2.handle(_cmd(command_id="cmd-X", expected_aggregate_version=1))
    assert env1.event_id == env2.event_id


@pytest.mark.asyncio
async def test_subscriber_unsubscribes_on_cache_evict() -> None:
    """When the cache evicts an aggregate, its dedup subscription is
    torn down — no stale callbacks accumulate."""
    _make_class()
    bus = InMemoryNatsBus()
    el = InMemoryEventLog()
    rl = InMemoryRejectionLog()
    subscriber = NatsDedupSubscriber(bus)  # type: ignore[arg-type]
    cache: AggregateCache = AggregateCache(max_size=1)
    h = CommandHandler(
        el, rl,
        cache=cache,
        dedup_publisher=NullDedupPublisher(),
        dedup_subscriber=subscriber,
    )
    # Load aggregate "a-1" → subscription created.
    from heddle.contrib.events.registry import get_aggregate_class
    cls = get_aggregate_class("HybridT")
    await h._load_or_create(cls, "a-1")
    assert bus.sub_count(dedup_subject("HybridT", "a-1")) == 1
    # Load aggregate "a-2" → evicts "a-1" → subscription torn down.
    await h._load_or_create(cls, "a-2")
    assert bus.sub_count(dedup_subject("HybridT", "a-1")) == 0
    assert bus.sub_count(dedup_subject("HybridT", "a-2")) == 1
