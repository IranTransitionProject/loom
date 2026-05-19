"""Process-local aggregate cache for :class:`CommandHandler`.

Caches reconstructed aggregates so consecutive commands against the
same ``(aggregate_type, aggregate_id)`` don't re-replay events from
the event log. LRU eviction; configurable max size.

The cache is also what makes Sprint 3's cross-process dedup observable:
:class:`DedupSubscriber` watches mark_processed events for cached
aggregates and updates their dedup buffer on receive. Cache eviction
triggers an ``on_evict`` callback so subscribers can unsubscribe.

See ``heddle-contrib-events-m2-architecture-v7.md`` §4.5 and the
Sprint 2 → Sprint 3 boundary table in ``docs/CONCEPTS.md``.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from heddle.contrib.events.aggregate import Aggregate


CacheKey = tuple[str, str]
"""(aggregate_type, aggregate_id)."""


DEFAULT_CACHE_SIZE: int = 1024
"""Default per-CommandHandler aggregate cache size. ~3× headroom over
the largest realistic working set (a shift's worth of active
Operations on a busy machine)."""


class AggregateCache:
    """LRU cache of reconstructed aggregates.

    Thread-safe access to the underlying OrderedDict; the optional
    ``on_evict`` callback fires *after* the lock is released, so
    callbacks may do async I/O without risk of deadlock.

    ``max_size=0`` disables caching entirely — every ``put`` is an
    immediate eviction, useful for tests of the no-cache path.
    """

    def __init__(
        self,
        max_size: int = DEFAULT_CACHE_SIZE,
        on_evict: Callable[[CacheKey, "Aggregate"], Awaitable[None]] | None = None,
    ) -> None:
        self._max_size = max_size
        self._entries: OrderedDict[CacheKey, Aggregate] = OrderedDict()
        self._lock = threading.Lock()
        self.on_evict = on_evict

    def get(self, key: CacheKey) -> "Aggregate | None":
        """Look up an aggregate. Updates LRU order on hit."""
        with self._lock:
            agg = self._entries.get(key)
            if agg is not None:
                self._entries.move_to_end(key)
            return agg

    async def put(self, key: CacheKey, aggregate: "Aggregate") -> None:
        """Insert (or replace) an aggregate. Evicts oldest entries past max_size.

        With ``max_size=0`` the put is an immediate evict — the
        aggregate is announced to ``on_evict`` and not retained.
        """
        evicted: list[tuple[CacheKey, Aggregate]] = []
        with self._lock:
            if self._max_size == 0:
                evicted.append((key, aggregate))
            else:
                if key in self._entries:
                    self._entries.move_to_end(key)
                    self._entries[key] = aggregate
                else:
                    self._entries[key] = aggregate
                    while len(self._entries) > self._max_size:
                        ev_key, ev_agg = self._entries.popitem(last=False)
                        evicted.append((ev_key, ev_agg))
        if self.on_evict:
            for ev_key, ev_agg in evicted:
                await self.on_evict(ev_key, ev_agg)

    async def invalidate(self, key: CacheKey) -> None:
        """Remove an entry; fires ``on_evict`` if it was present."""
        evicted: list[tuple[CacheKey, Aggregate]] = []
        with self._lock:
            if key in self._entries:
                evicted.append((key, self._entries.pop(key)))
        if self.on_evict:
            for ev_key, ev_agg in evicted:
                await self.on_evict(ev_key, ev_agg)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._entries
