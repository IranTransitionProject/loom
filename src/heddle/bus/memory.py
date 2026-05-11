"""
In-memory message bus for testing and local development.

Provides the same MessageBus interface as NATSBus but routes messages
through in-process async queues. No external infrastructure required.

Limitations compared to NATSBus:
- Single process only (no inter-process communication)
- No persistence (messages lost on close)
- Simplified queue group semantics (round-robin among group members)
- No wildcard subject matching

This is intentionally simple. Use NATSBus for production.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Coroutine

from heddle.bus.base import MessageBus, Subscription


class InMemorySubscription(Subscription):
    """Subscription backed by an asyncio.Queue."""

    def __init__(self, subject: str) -> None:
        self.subject = subject
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._active = True

    async def unsubscribe(self) -> None:
        """Unsubscribe and unblock any waiting consumer."""
        self._active = False
        # Push a sentinel so any waiting __anext__ unblocks.
        await self._queue.put(None)

    def __aiter__(self) -> InMemorySubscription:
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._active:
            raise StopAsyncIteration
        data = await self._queue.get()
        if data is None:
            raise StopAsyncIteration
        return data

    async def _deliver(self, data: dict[str, Any]) -> None:
        """Deliver a message to this subscription's queue."""
        if self._active:
            await self._queue.put(data)


class InMemoryBus(MessageBus):
    """In-memory message bus for testing.

    Messages published to a subject are delivered to all active subscribers
    on that subject. Queue groups are supported: within a group, messages
    are delivered to one member via round-robin.
    """

    def __init__(self) -> None:
        # subject -> list of (queue_group | None, subscription)
        self._subscribers: dict[str, list[tuple[str | None, InMemorySubscription]]] = defaultdict(
            list
        )
        # subject -> queue_group -> round-robin counter
        self._group_counters: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._connected = False

    async def connect(self) -> None:
        """Mark the bus as connected."""
        self._connected = True

    async def close(self) -> None:
        """Close the bus and unsubscribe all active subscriptions."""
        self._connected = False
        for subs in self._subscribers.values():
            for _, sub in subs:
                await sub.unsubscribe()
        self._subscribers.clear()

    async def publish(self, subject: str, data: dict[str, Any]) -> None:
        """Publish a message to all subscribers on the given subject.

        Delivery is parallel across subscribers via ``asyncio.gather``
        (with ``return_exceptions=True``).  Today's subscriptions
        back onto unbounded ``asyncio.Queue`` instances so serial
        delivery never blocked head-of-line; the gather form keeps
        delivery O(1) in wall-clock if a future ``maxsize`` bound or
        slow-consumer detection ever lands on the queue.
        """
        subs = self._subscribers.get(subject, [])
        if not subs:
            return

        # Partition into ungrouped and grouped subscribers.
        ungrouped = [(g, s) for g, s in subs if g is None and s._active]
        grouped: dict[str, list[InMemorySubscription]] = defaultdict(list)
        for group, sub in subs:
            if group is not None and sub._active:
                grouped[group].append(sub)

        # Build the list of coroutines to drive concurrently.
        deliveries: list[Coroutine[Any, Any, None]] = [sub._deliver(data) for _, sub in ungrouped]

        # One delivery per queue group (round-robin selection).
        for group, members in grouped.items():
            if not members:
                continue
            idx = self._group_counters[subject][group] % len(members)
            deliveries.append(members[idx]._deliver(data))
            self._group_counters[subject][group] += 1

        if deliveries:
            # ``return_exceptions=True`` so one stuck subscriber
            # doesn't block delivery to the others.  We don't
            # surface per-subscriber failures here — the test
            # harness keeps delivery in a known-good state.
            await asyncio.gather(*deliveries, return_exceptions=True)

    async def subscribe(
        self,
        subject: str,
        queue_group: str | None = None,
    ) -> InMemorySubscription:
        """Subscribe to a subject, optionally with a queue group."""
        sub = InMemorySubscription(subject)
        self._subscribers[subject].append((queue_group, sub))
        return sub
