"""Subscriber half of the hybrid mark_processed mechanism.

A :class:`DedupSubscriber` maintains one subscription per cached
aggregate. On receive, it calls ``mark_processed(command_id)`` on the
cached aggregate (looked up via the shared :class:`AggregateCache`).

The CommandHandler wires the subscriber's lifecycle to the cache:

- After a successful ``cache.put(key, agg)``, the handler calls
  ``subscribe(type, id, cache)``.
- The cache's ``on_evict`` hook calls ``unsubscribe(type, id)`` so a
  stale subscription doesn't outlive its aggregate.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from heddle.contrib.events.dedup_publisher import dedup_subject

if TYPE_CHECKING:
    from nats.aio.client import Client as NATSClient

    from heddle.contrib.events.cache import AggregateCache


class DedupSubscriber(ABC):
    """Maintains per-aggregate ``mark_processed`` subscriptions."""

    @abstractmethod
    async def subscribe(
        self,
        aggregate_type: str,
        aggregate_id: str,
        cache: "AggregateCache",
    ) -> None:
        """Start watching mark_processed events for one cached aggregate."""

    @abstractmethod
    async def unsubscribe(self, aggregate_type: str, aggregate_id: str) -> None:
        """Stop watching; called on cache eviction."""


class NullDedupSubscriber(DedupSubscriber):
    """No-op. For tests and single-process deployments."""

    async def subscribe(
        self,
        aggregate_type: str,
        aggregate_id: str,
        cache: "AggregateCache",
    ) -> None:
        return

    async def unsubscribe(self, aggregate_type: str, aggregate_id: str) -> None:
        return


class NatsDedupSubscriber(DedupSubscriber):
    """Subscribes to NATS-core ``mark_processed`` events."""

    def __init__(self, nc: "NATSClient") -> None:
        self._nc = nc
        self._subs: dict[tuple[str, str], Any] = {}

    async def subscribe(
        self,
        aggregate_type: str,
        aggregate_id: str,
        cache: "AggregateCache",
    ) -> None:
        key = (aggregate_type, aggregate_id)
        if key in self._subs:
            return

        async def cb(msg: Any) -> None:
            payload = json.loads(msg.data.decode())
            agg = cache.get(key)
            if agg is None:
                # Evicted between publish and receive; harmless.
                return
            agg.mark_processed(payload["command_id"])

        sub = await self._nc.subscribe(  # type: ignore[reportUnknownMemberType]
            dedup_subject(aggregate_type, aggregate_id), cb=cb
        )
        self._subs[key] = sub

    async def unsubscribe(self, aggregate_type: str, aggregate_id: str) -> None:
        sub = self._subs.pop((aggregate_type, aggregate_id), None)
        if sub is not None:
            await sub.unsubscribe()
