"""Tests for heddle.contrib.events.testing (Sprint 2 T9).

The fakes here register at module import; the registry_isolation
fixture does NOT cover them (it snapshots after import). These
tests verify their public registration is stable and the factories
produce valid envelopes.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from heddle.contrib.events.envelopes import CommandMessage, EventEnvelope
from heddle.contrib.events.errors import CommandRejected
from heddle.contrib.events.registry import get_aggregate_class
from heddle.contrib.events.testing import (
    FakeIntervalAggregate,
    FakeRootAggregate,
    make_command,
    make_event,
)


def test_make_event_defaults() -> None:
    env = make_event()
    assert isinstance(env, EventEnvelope)
    assert env.aggregate_type == "FakeInterval"
    assert env.aggregate_id == "test-id"
    assert env.event_type == "ThingHappened"
    assert env.aggregate_version == 1
    assert env.metadata.issued_by == "user:badge:test"


def test_make_event_overrides() -> None:
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    env = make_event(
        aggregate_type="Job",
        aggregate_id="39174-004",
        event_type="JobShipped",
        aggregate_version=7,
        payload={"a": 1},
        issued_by="framework:cascade",
        occurred_at=when,
        recorded_at=when,
    )
    assert env.aggregate_type == "Job"
    assert env.aggregate_id == "39174-004"
    assert env.event_type == "JobShipped"
    assert env.aggregate_version == 7
    assert env.payload == {"a": 1}
    assert env.metadata.issued_by == "framework:cascade"
    assert env.occurred_at == when
    assert env.recorded_at == when


def test_make_command_defaults() -> None:
    cmd = make_command()
    assert isinstance(cmd, CommandMessage)
    assert cmd.aggregate_type == "FakeInterval"
    assert cmd.command_type == "DoThing"
    assert cmd.expected_aggregate_version is None
    assert cmd.metadata.issued_by == "user:badge:test"


def test_make_command_overrides() -> None:
    cmd = make_command(
        aggregate_type="Job",
        aggregate_id="39174-004",
        command_type="ClockIn",
        payload={"badge": "206"},
        correlation_id="corr-1",
        expected_aggregate_version=3,
    )
    assert cmd.command_type == "ClockIn"
    assert cmd.payload == {"badge": "206"}
    assert cmd.metadata.correlation_id == "corr-1"
    assert cmd.expected_aggregate_version == 3


def test_fake_interval_registered() -> None:
    assert get_aggregate_class("FakeInterval") is FakeIntervalAggregate


def test_fake_root_registered() -> None:
    assert get_aggregate_class("FakeRoot") is FakeRootAggregate


def test_fake_interval_handler_emits_event() -> None:
    agg = FakeIntervalAggregate(aggregate_id="x")
    event_type, payload = agg.handle_do_thing(
        {"a": 1}, make_command().metadata
    )
    assert event_type == "ThingHappened"
    assert payload == {"a": 1}


def test_fake_interval_handler_rejects_forbidden() -> None:
    agg = FakeIntervalAggregate(aggregate_id="x")
    with pytest.raises(CommandRejected) as exc:
        agg.handle_do_thing({"forbidden": True}, make_command().metadata)
    assert exc.value.reason == "FORBIDDEN"


def test_fake_root_handler_emits_child_membership() -> None:
    agg = FakeRootAggregate(aggregate_id="r-1")
    event_type, payload = agg.handle_add_child(
        {"child_id": "c-1"}, make_command().metadata
    )
    assert event_type == "ChildAdded"
    assert payload["_child_membership"]["add"][0]["id"] == "c-1"
    assert payload["_child_membership"]["add"][0]["type"] == "FakeInterval"
