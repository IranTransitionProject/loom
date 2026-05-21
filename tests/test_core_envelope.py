"""Unit tests for the base ``WireEnvelope`` and its payload-type registry.

S1 ships the generic frame + the body-resolution mechanism only — no real
message bodies are reshaped yet. These tests exercise the mechanism with a
*dummy* body and assert the bootstrap contract (loud failure when a body type
was never registered / its module never imported).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel

from heddle.core import envelope
from heddle.core.envelope import (
    WireEnvelope,
    get_payload_model,
    parse,
    register_payload_type,
    unwrap,
    wrap,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def payload_registry_isolation() -> Iterator[None]:
    """Snapshot and restore ``_PAYLOAD_REGISTRY`` around a test.

    Mirrors ``registry_isolation`` for ``AGGREGATE_REGISTRY``: only test-local
    registrations are rolled back; pre-existing entries survive.
    """
    snapshot = dict(envelope._PAYLOAD_REGISTRY)
    yield
    envelope._PAYLOAD_REGISTRY.clear()
    envelope._PAYLOAD_REGISTRY.update(snapshot)


class _DummyBody(BaseModel):
    """A stand-in body — includes a datetime to prove JSON round-trip."""

    name: str
    when: datetime


class _OtherBody(BaseModel):
    count: int


def test_defaults_are_uuid7_and_utc() -> None:
    env = WireEnvelope(payload_type="test.Dummy", payload={})

    parsed_id = uuid.UUID(env.message_id)
    assert parsed_id.version == 7, "message_id is a UUIDv7 (time-ordered)"
    assert env.occurred_at.tzinfo is not None, "occurred_at is timezone-aware"
    assert env.recorded_at.tzinfo is not None, "recorded_at is timezone-aware"
    # occurred_at's factory runs before recorded_at's — documents current
    # default behavior (S2 may collapse them to one instant via a validator).
    assert env.occurred_at <= env.recorded_at
    assert env.origin is None
    assert env.correlation_id is None


def test_wrap_unwrap_round_trip(payload_registry_isolation: None) -> None:
    register_payload_type("test.Dummy", _DummyBody)
    body = _DummyBody(name="alpha", when=datetime(2026, 5, 20, 12, 0, tzinfo=UTC))

    env = wrap("test.Dummy", body, origin="user:test")

    assert env.payload_type == "test.Dummy"
    assert env.origin == "user:test"
    assert isinstance(env.payload, dict)
    assert env.payload["name"] == "alpha"

    recovered = unwrap(env)
    assert isinstance(recovered, _DummyBody)
    assert recovered == body, "body survives wrap → JSON dump → validate round-trip"


def test_parse_dispatches_on_payload_type(payload_registry_isolation: None) -> None:
    register_payload_type("test.Dummy", _DummyBody)
    register_payload_type("test.Other", _OtherBody)

    data = {"payload_type": "test.Other", "payload": {"count": 7}}
    env, body = parse(data)

    assert isinstance(env, WireEnvelope)
    assert isinstance(body, _OtherBody), "parse resolves the right model for the discriminator"
    assert body.count == 7


def test_get_payload_model_unregistered_raises_keyerror(
    payload_registry_isolation: None,
) -> None:
    with pytest.raises(KeyError, match="no payload model registered"):
        get_payload_model("test.NeverRegistered")


def test_cold_process_unimported_body_fails_loud(
    payload_registry_isolation: None,
) -> None:
    """The bootstrap gotcha: a body whose module was never imported (so never
    registered) fails loudly at parse time, not silently."""
    data = {"payload_type": "core.NotImportedInThisProcess", "payload": {}}
    with pytest.raises(KeyError, match="Did the module defining it get imported"):
        parse(data)


def test_register_payload_type_rejects_shadowing(
    payload_registry_isolation: None,
) -> None:
    register_payload_type("test.Dummy", _DummyBody)
    # Same model again is a no-op (reload-tolerant).
    register_payload_type("test.Dummy", _DummyBody)
    # A different model under the same name is rejected.
    with pytest.raises(ValueError, match="already registered"):
        register_payload_type("test.Dummy", _OtherBody)


def test_middleware_lane_underscore_keys_preserved() -> None:
    """WireEnvelope is the single Middleware-Lane carrier (#22): top-level
    underscore-prefixed keys ride through validation and round-trip."""
    data = {
        "payload_type": "test.Dummy",
        "payload": {},
        "_trace_context": {"traceparent": "00-abc-def-01"},
    }
    env = WireEnvelope.model_validate(data)
    dumped = env.model_dump()
    assert dumped["_trace_context"] == {"traceparent": "00-abc-def-01"}, (
        "underscore middleware keys preserved unchanged"
    )
    # The wire path is JSON, not dict — confirm the key survives a full
    # JSON round-trip, which is what Invariant #22 actually requires.
    revived = WireEnvelope.model_validate_json(env.model_dump_json())
    assert revived.model_dump()["_trace_context"] == {"traceparent": "00-abc-def-01"}


def test_misspelled_top_level_field_is_rejected() -> None:
    """A non-underscore unknown key is a typo, not a Middleware-Lane key."""
    with pytest.raises(ValueError, match="unknown top-level key"):
        WireEnvelope.model_validate(
            {"payload_type": "test.Dummy", "payload": {}, "correlaton_id": "oops"}
        )
