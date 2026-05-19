"""Tests for the FinalizationHorizonProjector stub (Sprint 2 T8c)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from heddle.contrib.events.envelopes import EventEnvelope, EventMetadata
from heddle.contrib.events.projectors import FinalizationHorizonProjector, finalization_horizon


@pytest.mark.asyncio
async def test_project_is_noop() -> None:
    p = FinalizationHorizonProjector()
    now = datetime.now(UTC)
    envelope = EventEnvelope(
        aggregate_type="Any",
        aggregate_id="any-1",
        aggregate_version=1,
        event_type="Whatever",
        payload={},
        metadata=EventMetadata(issued_by="user:badge:test"),
        occurred_at=now,
        recorded_at=now,
    )
    # MUST NOT raise. Returns None.
    assert await p.project(envelope) is None


def test_docstring_marks_stub_for_sprint_3() -> None:
    """Sanity gate so the stub can't graduate to a real implementation
    without updating its module docstring — a small forcing function
    that anyone replacing this file actually thinks about Sprint 3."""
    doc = finalization_horizon.__doc__ or ""
    assert "STUB" in doc
    assert "Sprint 3" in doc
