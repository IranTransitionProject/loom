"""Tests for ScopeMembershipProjector (Sprint 2 T8a)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from heddle.contrib.events.aggregate import IntervalAggregate, RootAggregate
from heddle.contrib.events.envelopes import EventEnvelope, EventMetadata
from heddle.contrib.events.projectors import (
    CHILD_MEMBERSHIP_KEY,
    ScopeMembershipProjector,
)
from heddle.contrib.events.registry import register_aggregate

pytestmark = pytest.mark.usefixtures("registry_isolation")


def _ev(*, agg_type: str, agg_id: str = "r-1", payload: dict | None = None) -> EventEnvelope:
    now = datetime.now(UTC)
    return EventEnvelope(
        aggregate_type=agg_type,
        aggregate_id=agg_id,
        aggregate_version=1,
        event_type="ChildAdded",
        payload=payload or {},
        metadata=EventMetadata(issued_by="user:badge:test"),
        occurred_at=now,
        recorded_at=now,
    )


def _make_root() -> type[RootAggregate]:
    @register_aggregate("FRoot")
    class _R(RootAggregate):
        pass

    return _R


def _make_interval() -> type[IntervalAggregate]:
    @register_aggregate("FInterval")
    class _I(IntervalAggregate):
        pass

    return _I


@pytest.mark.asyncio
async def test_add_registers_child() -> None:
    _make_root()
    p = ScopeMembershipProjector()
    await p.project(
        _ev(
            agg_type="FRoot",
            payload={CHILD_MEMBERSHIP_KEY: {"add": [{"type": "Op", "id": "X"}]}},
        )
    )
    assert p.children_of("FRoot", "r-1", "Op") == frozenset({"X"})


@pytest.mark.asyncio
async def test_no_membership_key_is_noop() -> None:
    _make_root()
    p = ScopeMembershipProjector()
    await p.project(_ev(agg_type="FRoot", payload={"unrelated": True}))
    assert p.all_children_of("FRoot", "r-1") == {}


@pytest.mark.asyncio
async def test_non_root_aggregate_ignored() -> None:
    _make_interval()
    p = ScopeMembershipProjector()
    await p.project(
        _ev(
            agg_type="FInterval",
            payload={CHILD_MEMBERSHIP_KEY: {"add": [{"type": "Op", "id": "X"}]}},
        )
    )
    assert p.all_children_of("FInterval", "r-1") == {}


@pytest.mark.asyncio
async def test_remove_discards_child() -> None:
    _make_root()
    p = ScopeMembershipProjector()
    await p.project(
        _ev(
            agg_type="FRoot",
            payload={
                CHILD_MEMBERSHIP_KEY: {
                    "add": [{"type": "Op", "id": "X"}, {"type": "Op", "id": "Y"}]
                }
            },
        )
    )
    await p.project(
        _ev(
            agg_type="FRoot",
            payload={CHILD_MEMBERSHIP_KEY: {"remove": [{"type": "Op", "id": "X"}]}},
        )
    )
    assert p.children_of("FRoot", "r-1", "Op") == frozenset({"Y"})


@pytest.mark.asyncio
async def test_multiple_adds_accumulate() -> None:
    _make_root()
    p = ScopeMembershipProjector()
    for i in range(5):
        await p.project(
            _ev(
                agg_type="FRoot",
                payload={CHILD_MEMBERSHIP_KEY: {"add": [{"type": "Op", "id": f"op-{i}"}]}},
            )
        )
    assert p.children_of("FRoot", "r-1", "Op") == frozenset({f"op-{i}" for i in range(5)})


@pytest.mark.asyncio
async def test_all_children_of_groups_by_type() -> None:
    _make_root()
    p = ScopeMembershipProjector()
    await p.project(
        _ev(
            agg_type="FRoot",
            payload={
                CHILD_MEMBERSHIP_KEY: {
                    "add": [
                        {"type": "Op", "id": "op-1"},
                        {"type": "Sub", "id": "sub-1"},
                    ]
                }
            },
        )
    )
    view = p.all_children_of("FRoot", "r-1")
    assert view == {
        "Op": frozenset({"op-1"}),
        "Sub": frozenset({"sub-1"}),
    }
