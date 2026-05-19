"""Tests for SnapshotStore (Sprint 3 T3)."""

from __future__ import annotations

import pytest

from heddle.contrib.events.aggregate import IntervalAggregate
from heddle.contrib.events.registry import register_aggregate
from heddle.contrib.events.snapshot_store import SnapshotStore
from heddle.core.kvstore import InMemoryKeyValueStore

pytestmark = pytest.mark.usefixtures("registry_isolation")


def _make_class():
    @register_aggregate("SnapT")
    class _SnapT(IntervalAggregate):
        pass

    return _SnapT


@pytest.mark.asyncio
async def test_save_load_roundtrip_preserves_state() -> None:
    cls = _make_class()
    kv = InMemoryKeyValueStore()
    store = SnapshotStore(kv)

    inst = cls(aggregate_id="a-1")
    inst.aggregate_version = 7
    inst.phase = "active"
    inst.mark_processed("cmd-1")
    inst.mark_processed("cmd-2")
    await store.save(inst)

    restored = await store.load(cls, "a-1")
    assert restored is not None
    assert restored.aggregate_id == "a-1"
    assert restored.aggregate_version == 7
    assert restored.phase == "active"  # type: ignore[attr-defined]
    assert restored.has_processed("cmd-1")
    assert restored.has_processed("cmd-2")


@pytest.mark.asyncio
async def test_load_returns_none_when_no_snapshot() -> None:
    cls = _make_class()
    store = SnapshotStore(InMemoryKeyValueStore())
    assert await store.load(cls, "missing") is None


@pytest.mark.asyncio
async def test_version_zero_before_first_save() -> None:
    _make_class()
    store = SnapshotStore(InMemoryKeyValueStore())
    assert await store.version("SnapT", "x") == 0


@pytest.mark.asyncio
async def test_version_reports_aggregate_version_at_save() -> None:
    cls = _make_class()
    store = SnapshotStore(InMemoryKeyValueStore())
    inst = cls(aggregate_id="a-1")
    inst.aggregate_version = 42
    await store.save(inst)
    assert await store.version("SnapT", "a-1") == 42
