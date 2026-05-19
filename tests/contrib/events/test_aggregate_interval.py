"""Tests for :class:`IntervalAggregate` (Sprint 2 T2b)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from heddle.contrib.events.aggregate import IntervalAggregate
from heddle.contrib.events.envelopes import EventEnvelope, EventMetadata
from heddle.contrib.events.registry import register_aggregate

pytestmark = pytest.mark.usefixtures("registry_isolation")


def _internal_finalized_envelope(version: int) -> EventEnvelope:
    now = datetime.now(UTC)
    return EventEnvelope(
        aggregate_type="IsoInterval",
        aggregate_id="i-1",
        aggregate_version=version,
        event_type="InternalFinalized",
        payload={},
        metadata=EventMetadata(issued_by="framework:cascade"),
        occurred_at=now,
        recorded_at=now,
    )


def _make_cls():
    @register_aggregate("IsoInterval")
    class _Fake(IntervalAggregate):
        pass

    return _Fake


def test_default_phase_is_created() -> None:
    cls = _make_cls()
    assert cls(aggregate_id="i-1").phase == "created"


def test_internal_finalized_transitions_to_finalized() -> None:
    cls = _make_cls()
    agg = cls(aggregate_id="i-1")
    agg.apply(_internal_finalized_envelope(1))
    assert agg.phase == "finalized"
    assert agg.aggregate_version == 1


def test_repeat_internal_finalized_is_noop_state_wise() -> None:
    """Per v7 §4.11: ring-buffer overflow can leak a duplicate cascade
    command. Re-applying InternalFinalized to a finalized aggregate
    must be harmless — phase stays finalized, version still
    advances (each event still consumes a version slot)."""
    cls = _make_cls()
    agg = cls(aggregate_id="i-1")
    agg.apply(_internal_finalized_envelope(1))
    agg.apply(_internal_finalized_envelope(2))
    assert agg.phase == "finalized"
    assert agg.aggregate_version == 2


def test_phase_survives_snapshot_round_trip() -> None:
    cls = _make_cls()
    agg = cls(aggregate_id="i-1")
    agg.apply(_internal_finalized_envelope(1))

    restored = cls.from_snapshot(agg.to_snapshot())
    assert restored.phase == "finalized"
    assert restored.aggregate_version == 1
