"""Tests for AggregateCache (Sprint 3 T2)."""

from __future__ import annotations

from typing import Any

import pytest

from heddle.contrib.events.aggregate import Aggregate, IntervalAggregate
from heddle.contrib.events.cache import CacheKey, AggregateCache
from heddle.contrib.events.registry import register_aggregate

pytestmark = pytest.mark.usefixtures("registry_isolation")


def _make_class():
    @register_aggregate("CacheT")
    class _CacheT(IntervalAggregate):
        pass

    return _CacheT


def _agg(cls: type[Aggregate], aggregate_id: str) -> Aggregate:
    return cls(aggregate_id=aggregate_id)


def test_get_on_empty_returns_none() -> None:
    cache: AggregateCache = AggregateCache()
    assert cache.get(("CacheT", "x")) is None


@pytest.mark.asyncio
async def test_put_then_get_returns_same_instance() -> None:
    cls = _make_class()
    cache: AggregateCache = AggregateCache()
    inst = _agg(cls, "x")
    await cache.put(("CacheT", "x"), inst)
    assert cache.get(("CacheT", "x")) is inst


@pytest.mark.asyncio
async def test_eviction_lru_order() -> None:
    cls = _make_class()
    cache: AggregateCache = AggregateCache(max_size=2)
    a, b, c = _agg(cls, "a"), _agg(cls, "b"), _agg(cls, "c")
    await cache.put(("CacheT", "a"), a)
    await cache.put(("CacheT", "b"), b)
    # Access "a" to refresh LRU.
    assert cache.get(("CacheT", "a")) is a
    await cache.put(("CacheT", "c"), c)
    # "b" was least-recently-used — evicted.
    assert cache.get(("CacheT", "b")) is None
    assert cache.get(("CacheT", "a")) is a
    assert cache.get(("CacheT", "c")) is c


@pytest.mark.asyncio
async def test_on_evict_callback_fires() -> None:
    cls = _make_class()
    evicted: list[tuple[CacheKey, Aggregate]] = []

    async def on_evict(key: CacheKey, agg: Aggregate) -> None:
        evicted.append((key, agg))

    cache: AggregateCache = AggregateCache(max_size=1, on_evict=on_evict)
    a, b = _agg(cls, "a"), _agg(cls, "b")
    await cache.put(("CacheT", "a"), a)
    await cache.put(("CacheT", "b"), b)
    assert evicted == [(("CacheT", "a"), a)]


@pytest.mark.asyncio
async def test_invalidate_removes_and_fires_evict() -> None:
    cls = _make_class()
    evicted: list[CacheKey] = []

    async def on_evict(key: CacheKey, _agg: Aggregate) -> None:
        evicted.append(key)

    cache: AggregateCache = AggregateCache(on_evict=on_evict)
    a = _agg(cls, "a")
    await cache.put(("CacheT", "a"), a)
    await cache.invalidate(("CacheT", "a"))
    assert cache.get(("CacheT", "a")) is None
    assert evicted == [("CacheT", "a")]


@pytest.mark.asyncio
async def test_max_size_zero_is_immediate_evict() -> None:
    cls = _make_class()
    evicted: list[CacheKey] = []

    async def on_evict(key: CacheKey, _agg: Aggregate) -> None:
        evicted.append(key)

    cache: AggregateCache = AggregateCache(max_size=0, on_evict=on_evict)
    a = _agg(cls, "a")
    await cache.put(("CacheT", "a"), a)
    assert cache.get(("CacheT", "a")) is None
    assert evicted == [("CacheT", "a")]
    assert len(cache) == 0


@pytest.mark.asyncio
async def test_put_replaces_existing() -> None:
    cls = _make_class()
    cache: AggregateCache = AggregateCache()
    a1 = _agg(cls, "a")
    a2 = _agg(cls, "a")
    await cache.put(("CacheT", "a"), a1)
    await cache.put(("CacheT", "a"), a2)
    assert cache.get(("CacheT", "a")) is a2
    assert len(cache) == 1
