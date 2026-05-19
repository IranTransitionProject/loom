"""Integration tests for :class:`JetStreamEventLog` (Sprint 3 T8).

These tests require a running NATS server with JetStream enabled.
Marked ``@pytest.mark.integration`` so the unit suite skips them
by default. Run locally with::

    NATS_URL=nats://localhost:4222 uv run pytest -q -m integration

If ``NATS_URL`` is unset, the fixtures skip the whole module.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import pytest

from heddle.contrib.events.envelopes import EventEnvelope, EventMetadata
from heddle.contrib.events.errors import ConcurrencyError

NATS_URL = os.environ.get("NATS_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(NATS_URL is None, reason="NATS_URL not set"),
]


def _ev(*, version: int, agg_id: str = "a-1", event_type: str = "T1") -> EventEnvelope:
    now = datetime.now(UTC)
    return EventEnvelope(
        aggregate_type="JsEvT",
        aggregate_id=agg_id,
        aggregate_version=version,
        event_type=event_type,
        payload={"v": version},
        metadata=EventMetadata(issued_by="user:badge:test"),
        occurred_at=now,
        recorded_at=now,
    )


@pytest.fixture
async def js_connection() -> Any:
    from heddle.contrib.events.jetstream import connect_jetstream

    async with connect_jetstream(NATS_URL or "nats://localhost:4222") as conn:
        yield conn


@pytest.fixture
async def event_log(js_connection: Any) -> Any:
    from heddle.contrib.events.jetstream import (
        JetStreamEventLog,
        ensure_event_stream,
    )

    await ensure_event_stream(js_connection.js, "JsEvT")
    return JetStreamEventLog(js_connection.js)


@pytest.mark.asyncio
async def test_append_then_load_roundtrip(event_log: Any) -> None:
    await event_log.append(_ev(version=1), expected_version=0)
    await event_log.append(_ev(version=2), expected_version=1)
    loaded = [ev async for ev in event_log.load("JsEvT", "a-1")]
    assert [ev.aggregate_version for ev in loaded] == [1, 2]


@pytest.mark.asyncio
async def test_expected_version_mismatch_raises_concurrency_error(
    event_log: Any,
) -> None:
    await event_log.append(_ev(version=1), expected_version=0)
    with pytest.raises(ConcurrencyError):
        # Stale CAS: we claim the last version is 0 but it's 1.
        await event_log.append(_ev(version=2), expected_version=0)


@pytest.mark.asyncio
async def test_expected_version_none_skips_cas(event_log: Any) -> None:
    await event_log.append(_ev(version=1, agg_id="no-cas"), expected_version=None)
    loaded = [ev async for ev in event_log.load("JsEvT", "no-cas")]
    assert len(loaded) == 1


@pytest.mark.asyncio
async def test_subscribe_delivers_events_published_after(event_log: Any) -> None:
    import asyncio

    iterator = await event_log.subscribe("JsEvT")

    async def consumer() -> EventEnvelope:
        async for ev in iterator:
            return ev
        raise AssertionError("subscribe yielded nothing")

    task = asyncio.create_task(consumer())
    await event_log.append(_ev(version=1, agg_id="sub-1"), expected_version=0)
    received = await asyncio.wait_for(task, timeout=2.0)
    assert received.aggregate_id == "sub-1"


@pytest.mark.asyncio
async def test_load_filters_from_version(event_log: Any) -> None:
    for v in (1, 2, 3):
        await event_log.append(_ev(version=v, agg_id="filt"), expected_version=v - 1)
    loaded = [ev async for ev in event_log.load("JsEvT", "filt", from_version=1)]
    assert [ev.aggregate_version for ev in loaded] == [2, 3]
