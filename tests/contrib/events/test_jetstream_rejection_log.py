"""Integration tests for :class:`JetStreamRejectionLog` (Sprint 3 T9).

Gated on ``NATS_URL`` — see ``test_jetstream_event_log`` module
docstring for the rationale and how to run.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import pytest

from heddle.contrib.events.envelopes import CommandMessage, CommandMetadata
from heddle.contrib.events.rejection_log import RejectionEnvelope

NATS_URL = os.environ.get("NATS_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(NATS_URL is None, reason="NATS_URL not set"),
]


def _rejection(*, aggregate_id: str = "a-1", reason: str = "FORBIDDEN") -> RejectionEnvelope:
    return RejectionEnvelope(
        command=CommandMessage(
            aggregate_type="JsRjT",
            aggregate_id=aggregate_id,
            command_type="DoThing",
            payload={},
            metadata=CommandMetadata(issued_by="user:badge:test"),
            issued_at=datetime.now(UTC),
            expected_aggregate_version=None,
        ),
        reason=reason,
        detail="integration test",
        rejected_at=datetime.now(UTC),
    )


@pytest.fixture
async def js_connection() -> Any:
    from heddle.contrib.events.jetstream import connect_jetstream

    async with connect_jetstream(NATS_URL or "nats://localhost:4222") as conn:
        yield conn


@pytest.fixture
async def rejection_log(js_connection: Any) -> Any:
    from heddle.contrib.events.jetstream import (
        JetStreamRejectionLog,
        ensure_rejection_stream,
    )

    await ensure_rejection_stream(js_connection.js, "JsRjT")
    return JetStreamRejectionLog(js_connection.js)


@pytest.mark.asyncio
async def test_append_then_load_roundtrip(rejection_log: Any) -> None:
    await rejection_log.append(_rejection())
    loaded = [r async for r in rejection_log.load("JsRjT")]
    assert any(r.reason == "FORBIDDEN" for r in loaded)


@pytest.mark.asyncio
async def test_filter_by_aggregate_id(rejection_log: Any) -> None:
    await rejection_log.append(_rejection(aggregate_id="a-1"))
    await rejection_log.append(_rejection(aggregate_id="a-2"))
    loaded = [r async for r in rejection_log.load("JsRjT", "a-1")]
    assert all(r.command.aggregate_id == "a-1" for r in loaded)
