"""Provenance check for framework-only event types (v7 §4.5).

A forged ``InternalFinalized`` — one whose ``issued_by`` does not start
with ``framework:`` — MUST raise :class:`AggregateInvariantError`. A
legitimate ``framework:`` issuer MUST be accepted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from heddle.contrib.events.aggregate import Aggregate
from heddle.contrib.events.envelopes import EventEnvelope, EventMetadata
from heddle.contrib.events.errors import AggregateInvariantError
from heddle.contrib.events.registry import register_aggregate

pytestmark = pytest.mark.usefixtures("registry_isolation")


def _make_fake_class() -> type[Aggregate]:
    @register_aggregate("Fake")
    class _Fake(Aggregate):
        def apply_internal_finalized(
            self, payload: dict[str, Any], metadata: EventMetadata
        ) -> None:
            pass

    return _Fake


def _internal_finalized(*, issued_by: str) -> EventEnvelope:
    now = datetime.now(UTC)
    return EventEnvelope(
        aggregate_type="Fake",
        aggregate_id="agg-1",
        aggregate_version=1,
        event_type="InternalFinalized",
        payload={},
        metadata=EventMetadata(issued_by=issued_by),
        occurred_at=now,
        recorded_at=now,
    )


def test_forged_issuer_raises_aggregate_invariant_error() -> None:
    agg = _make_fake_class()(aggregate_id="agg-1")
    with pytest.raises(AggregateInvariantError, match="likely forged"):
        agg.apply(_internal_finalized(issued_by="user:badge:206"))


def test_framework_issuer_accepted() -> None:
    agg = _make_fake_class()(aggregate_id="agg-1")
    agg.apply(_internal_finalized(issued_by="framework:cascade"))
    assert agg.aggregate_version == 1
