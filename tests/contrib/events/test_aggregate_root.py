"""Tests for :class:`RootAggregate` (Sprint 2 T2c)."""

from __future__ import annotations

import pytest

from heddle.contrib.events.aggregate import RootAggregate
from heddle.contrib.events.registry import register_aggregate

pytestmark = pytest.mark.usefixtures("registry_isolation")


def _make_cls():
    @register_aggregate("IsoRoot")
    class _Root(RootAggregate):
        pass

    return _Root


def test_register_child_then_children_of() -> None:
    cls = _make_cls()
    agg = cls(aggregate_id="root-1")
    agg.register_child("Operation", "39174-004:laser-cut")
    agg.register_child("Operation", "39174-004:bend")

    assert agg.children_of("Operation") == frozenset(
        {"39174-004:laser-cut", "39174-004:bend"}
    )
    assert agg.children_of("Missing") == frozenset()


def test_all_children_view() -> None:
    cls = _make_cls()
    agg = cls(aggregate_id="root-1")
    agg.register_child("Operation", "op-1")
    agg.register_child("SubAssembly", "sub-1")

    view = agg.all_children()
    assert view == {
        "Operation": frozenset({"op-1"}),
        "SubAssembly": frozenset({"sub-1"}),
    }
    # Mutation of the returned view must not affect the aggregate.
    assert all(isinstance(v, frozenset) for v in view.values())


def test_snapshot_round_trip_preserves_children() -> None:
    cls = _make_cls()
    agg = cls(aggregate_id="root-1")
    agg.register_child("Operation", "op-1")
    agg.register_child("Operation", "op-2")
    agg.register_child("SubAssembly", "sub-1")

    restored = cls.from_snapshot(agg.to_snapshot())
    assert restored.children_of("Operation") == frozenset({"op-1", "op-2"})
    assert restored.children_of("SubAssembly") == frozenset({"sub-1"})
    assert restored.phase == "created"  # inherited default
