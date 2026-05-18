"""Tests for EventEnvelope / CommandMessage wire envelopes (Sprint 1)."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from heddle.core.messages import (
    CommandMessage,
    CommandMetadata,
    EventEnvelope,
    EventMetadata,
)


def _event_kwargs(**overrides):
    base = {
        "aggregate_type": "Job",
        "aggregate_id": "39174-004",
        "aggregate_version": 1,
        "event_type": "JobClockedIn",
        "metadata": EventMetadata(issued_by="user:badge:206"),
        "occurred_at": datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC),
        "recorded_at": datetime(2026, 5, 16, 12, 0, 1, tzinfo=UTC),
    }
    base.update(overrides)
    return base


def _command_kwargs(**overrides):
    base = {
        "aggregate_type": "Job",
        "aggregate_id": "39174-004",
        "command_type": "JobClockIn",
        "metadata": CommandMetadata(issued_by="user:badge:206"),
        "issued_at": datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return base


def test_event_envelope_round_trip():
    env = EventEnvelope(**_event_kwargs(payload={"badge": "206"}))
    restored = EventEnvelope.model_validate_json(env.model_dump_json())
    assert restored == env


def test_event_envelope_requires_issued_by():
    with pytest.raises(ValidationError):
        EventMetadata()  # type: ignore[call-arg]


def test_event_envelope_event_id_defaults_to_uuid7():
    e1 = EventEnvelope(**_event_kwargs())
    e2 = EventEnvelope(**_event_kwargs())
    assert e1.event_id != e2.event_id
    # UUIDv7 is time-ordered — second id should sort >= first lexically.
    assert e2.event_id >= e1.event_id


def test_event_envelope_aggregate_version_minimum():
    with pytest.raises(ValidationError):
        EventEnvelope(**_event_kwargs(aggregate_version=0))


def test_command_message_round_trip():
    cmd = CommandMessage(**_command_kwargs(payload={"badge": "206"}))
    restored = CommandMessage.model_validate_json(cmd.model_dump_json())
    assert restored == cmd


def test_command_message_expected_aggregate_version_optional():
    cmd = CommandMessage(**_command_kwargs(expected_aggregate_version=None))
    assert cmd.expected_aggregate_version is None
    cmd_with_cas = CommandMessage(**_command_kwargs(expected_aggregate_version=7))
    assert cmd_with_cas.expected_aggregate_version == 7


def test_command_metadata_issued_by_legacy_reserved():
    meta = CommandMetadata(issued_by="user:badge:206", issued_by_legacy="anything")
    assert meta.issued_by_legacy == "anything"
    # Default is None — reserved slot, no business logic.
    bare = CommandMetadata(issued_by="user:badge:206")
    assert bare.issued_by_legacy is None
