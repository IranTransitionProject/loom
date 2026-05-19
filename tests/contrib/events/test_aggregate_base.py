"""Tests for the :class:`Aggregate` abstract base (Sprint 2 T2a).

Covers apply() discipline, version monotonicity, provenance check on
framework-only event types, dedup buffer, and snapshot round-trip.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from heddle.contrib.events.aggregate import (
    PROCESSED_COMMAND_RING_SIZE,
    Aggregate,
    snake_case,
)
from heddle.contrib.events.envelopes import EventEnvelope, EventMetadata
from heddle.contrib.events.errors import (
    AggregateInvariantError,
    CorruptAggregateAlert,
    UnknownEventVersionError,
)
from heddle.contrib.events.registry import register_aggregate

pytestmark = pytest.mark.usefixtures("registry_isolation")


# ---------------------------------------------------------------------------
# Snake-case helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("camel", "expected"),
    [
        ("JobClockedIn", "job_clocked_in"),
        ("InternalFinalized", "internal_finalized"),
        ("JobShippedFromPF", "job_shipped_from_pf"),
        ("OperationLaborRecorded", "operation_labor_recorded"),
        ("DoThing", "do_thing"),
        ("A", "a"),
    ],
)
def test_snake_case_helper(camel: str, expected: str) -> None:
    assert snake_case(camel) == expected


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _envelope(
    *,
    event_type: str = "ThingHappened",
    aggregate_version: int = 1,
    issued_by: str = "user:badge:test",
    aggregate_type: str = "Fake",
    aggregate_id: str = "agg-1",
    payload: dict[str, Any] | None = None,
) -> EventEnvelope:
    now = datetime.now(UTC)
    return EventEnvelope(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        aggregate_version=aggregate_version,
        event_type=event_type,
        payload=payload or {},
        metadata=EventMetadata(issued_by=issued_by),
        occurred_at=now,
        recorded_at=now,
    )


def _make_fake_class():
    """Build and register a fresh _Fake subclass under registry_isolation."""

    @register_aggregate("Fake")
    class _Fake(Aggregate):
        def __init__(self, aggregate_id: str) -> None:
            super().__init__(aggregate_id)
            self.things: list[dict[str, Any]] = []

        def apply_thing_happened(
            self, payload: dict[str, Any], metadata: EventMetadata
        ) -> None:
            self.things.append(payload)

        def apply_internal_finalized(
            self, payload: dict[str, Any], metadata: EventMetadata
        ) -> None:
            self.things.append({"_finalized": True})

    return _Fake


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_unregistered_concrete_subclass_raises() -> None:
    class _Bare(Aggregate):
        pass

    with pytest.raises(RuntimeError, match="not registered"):
        _Bare(aggregate_id="x")


# ---------------------------------------------------------------------------
# apply() dispatch + version monotonicity
# ---------------------------------------------------------------------------


def test_apply_dispatches_to_method_by_event_type() -> None:
    cls = _make_fake_class()
    agg = cls(aggregate_id="agg-1")

    agg.apply(_envelope(event_type="ThingHappened", payload={"a": 1}))

    assert agg.things == [{"a": 1}]
    assert agg.aggregate_version == 1


def test_apply_increments_version_monotonically() -> None:
    cls = _make_fake_class()
    agg = cls(aggregate_id="agg-1")

    agg.apply(_envelope(aggregate_version=1))
    agg.apply(_envelope(aggregate_version=2))
    agg.apply(_envelope(aggregate_version=3))

    assert agg.aggregate_version == 3


def test_apply_rejects_version_skip() -> None:
    cls = _make_fake_class()
    agg = cls(aggregate_id="agg-1")
    agg.apply(_envelope(aggregate_version=1))

    with pytest.raises(AggregateInvariantError, match="does not follow"):
        agg.apply(_envelope(aggregate_version=3))

    # State must not have been mutated.
    assert agg.aggregate_version == 1
    assert agg.things == [{}]


def test_apply_unknown_event_type_raises() -> None:
    cls = _make_fake_class()
    agg = cls(aggregate_id="agg-1")

    with pytest.raises(UnknownEventVersionError, match="apply_mystery"):
        agg.apply(_envelope(event_type="Mystery"))


def test_apply_wraps_handler_exceptions() -> None:
    @register_aggregate("Boom")
    class _Boom(Aggregate):
        def apply_kaboom(self, payload, metadata) -> None:
            raise ValueError("nope")

    agg = _Boom(aggregate_id="b-1")
    with pytest.raises(AggregateInvariantError, match="kaboom raised: nope"):
        agg.apply(_envelope(aggregate_type="Boom", event_type="Kaboom"))


# ---------------------------------------------------------------------------
# Provenance check
# ---------------------------------------------------------------------------


def test_internal_finalized_with_user_issuer_is_forgery() -> None:
    cls = _make_fake_class()
    agg = cls(aggregate_id="agg-1")

    with pytest.raises(CorruptAggregateAlert, match="likely forged"):
        agg.apply(
            _envelope(
                event_type="InternalFinalized",
                issued_by="user:badge:206",
            )
        )


def test_internal_finalized_with_framework_issuer_accepted() -> None:
    cls = _make_fake_class()
    agg = cls(aggregate_id="agg-1")

    agg.apply(
        _envelope(
            event_type="InternalFinalized",
            issued_by="framework:cascade",
        )
    )
    assert agg.aggregate_version == 1


# ---------------------------------------------------------------------------
# Dedup buffer
# ---------------------------------------------------------------------------


def test_dedup_buffer_basic() -> None:
    cls = _make_fake_class()
    agg = cls(aggregate_id="agg-1")

    assert agg.has_processed("cmd-1") is False
    agg.mark_processed("cmd-1")
    assert agg.has_processed("cmd-1") is True


def test_dedup_buffer_evicts_oldest() -> None:
    cls = _make_fake_class()
    agg = cls(aggregate_id="agg-1")
    for i in range(PROCESSED_COMMAND_RING_SIZE + 10):
        agg.mark_processed(f"cmd-{i}")

    # First 10 evicted; last PROCESSED_COMMAND_RING_SIZE present.
    assert not agg.has_processed("cmd-0")
    assert not agg.has_processed("cmd-9")
    assert agg.has_processed("cmd-10")
    assert agg.has_processed(f"cmd-{PROCESSED_COMMAND_RING_SIZE + 9}")


# ---------------------------------------------------------------------------
# Snapshot round-trip
# ---------------------------------------------------------------------------


def test_snapshot_round_trip_preserves_base_state() -> None:
    cls = _make_fake_class()
    agg = cls(aggregate_id="agg-1")
    agg.apply(_envelope(aggregate_version=1))
    agg.apply(_envelope(aggregate_version=2))
    agg.mark_processed("cmd-a")
    agg.mark_processed("cmd-b")

    data = agg.to_snapshot()
    restored = cls.from_snapshot(data)

    assert restored.aggregate_id == "agg-1"
    assert restored.aggregate_version == 2
    assert restored.has_processed("cmd-a")
    assert restored.has_processed("cmd-b")
