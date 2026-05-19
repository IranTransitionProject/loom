"""Tests for SLI instrumentation hooks (Sprint 3 T12)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from heddle.contrib.events.aggregate import IntervalAggregate
from heddle.contrib.events.command_handler import CommandHandler
from heddle.contrib.events.envelopes import (
    CommandMessage,
    CommandMetadata,
    EventMetadata,
)
from heddle.contrib.events.errors import CommandRejected
from heddle.contrib.events.event_log import InMemoryEventLog
from heddle.contrib.events.lease import finalization_lease
from heddle.contrib.events.registry import register_aggregate
from heddle.contrib.events.rejection_log import InMemoryRejectionLog
from heddle.contrib.events.sli import (
    _NullRecorder,
    get_recorder,
    install_recorder,
    time_observation,
)
from heddle.core.kvstore import InMemoryKeyValueStore

pytestmark = pytest.mark.usefixtures("registry_isolation")


@dataclass
class _CapturingRecorder:
    command_handles: list[dict[str, Any]] = field(default_factory=list)
    fan_outs: list[dict[str, Any]] = field(default_factory=list)
    lease_acquisitions: list[dict[str, Any]] = field(default_factory=list)

    def observe_command_handle(self, **kwargs: Any) -> None:
        self.command_handles.append(kwargs)

    def observe_dispatcher_fan_out(self, **kwargs: Any) -> None:
        self.fan_outs.append(kwargs)

    def observe_lease_acquisition(self, **kwargs: Any) -> None:
        self.lease_acquisitions.append(kwargs)


@pytest.fixture
def recorder():
    rec = _CapturingRecorder()
    install_recorder(rec)
    try:
        yield rec
    finally:
        install_recorder(_NullRecorder())


def _make_fake_class():
    @register_aggregate("SliT")
    class _Sli(IntervalAggregate):
        def handle_do_thing(
            self, payload: dict[str, Any], _meta: CommandMetadata
        ) -> tuple[str, dict[str, Any]]:
            if payload.get("forbidden"):
                raise CommandRejected("FORBIDDEN", "test")
            return "ThingHappened", dict(payload)

        def apply_thing_happened(self, _payload: dict[str, Any], _meta: EventMetadata) -> None:
            pass

    return _Sli


def _cmd(*, command_id: str | None = None, forbidden: bool = False) -> CommandMessage:
    kwargs: dict[str, Any] = {
        "aggregate_type": "SliT",
        "aggregate_id": "a-1",
        "command_type": "DoThing",
        "payload": {"forbidden": True} if forbidden else {},
        "metadata": CommandMetadata(issued_by="user:badge:t"),
        "issued_at": datetime.now(UTC),
        "expected_aggregate_version": None,
    }
    if command_id is not None:
        kwargs["command_id"] = command_id
    return CommandMessage(**kwargs)


def test_default_recorder_is_null() -> None:
    install_recorder(_NullRecorder())
    rec = get_recorder()
    # Should accept all three observation methods without raising.
    rec.observe_command_handle(
        aggregate_type="X", command_type="Y", outcome="success", duration_seconds=0.0
    )
    rec.observe_dispatcher_fan_out(aggregate_type="X", duration_seconds=0.0)
    rec.observe_lease_acquisition(
        aggregate_type="X", projector_name="p", outcome="claimed", duration_seconds=0.0
    )


def test_time_observation_returns_elapsed() -> None:
    with time_observation() as elapsed:
        pass
    assert elapsed() >= 0.0


@pytest.mark.asyncio
async def test_command_handle_success_observed(recorder: _CapturingRecorder) -> None:
    _make_fake_class()
    h = CommandHandler(InMemoryEventLog(), InMemoryRejectionLog())
    await h.handle(_cmd())
    assert len(recorder.command_handles) == 1
    obs = recorder.command_handles[0]
    assert obs["aggregate_type"] == "SliT"
    assert obs["command_type"] == "DoThing"
    assert obs["outcome"] == "success"
    assert obs["duration_seconds"] >= 0.0


@pytest.mark.asyncio
async def test_command_handle_rejected_observed(recorder: _CapturingRecorder) -> None:
    _make_fake_class()
    h = CommandHandler(InMemoryEventLog(), InMemoryRejectionLog())
    with pytest.raises(CommandRejected):
        await h.handle(_cmd(forbidden=True))
    assert len(recorder.command_handles) == 1
    assert recorder.command_handles[0]["outcome"] == "rejected"


@pytest.mark.asyncio
async def test_lease_claimed_and_preempted_observed(recorder: _CapturingRecorder) -> None:
    kv = InMemoryKeyValueStore()
    async with finalization_lease(kv, "T", "a", "framework:cascade"):
        pass
    async with finalization_lease(kv, "T", "a", "framework:horizon"):
        pass
    outcomes = [obs["outcome"] for obs in recorder.lease_acquisitions]
    assert outcomes == ["claimed", "preempted"]
    assert recorder.lease_acquisitions[0]["projector_name"] == "framework:cascade"
    assert recorder.lease_acquisitions[1]["projector_name"] == "framework:horizon"


@pytest.mark.asyncio
async def test_dispatcher_fan_out_observed(recorder: _CapturingRecorder) -> None:
    """End-to-end through the dispatcher; the fan-out histogram fires
    once per event dispatched."""
    from heddle.contrib.events.dispatcher import EventDispatcher, Projector
    from heddle.contrib.events.envelopes import EventEnvelope

    class _Noop(Projector):
        async def project(self, _ev: EventEnvelope) -> None:
            return

    _make_fake_class()
    log = InMemoryEventLog()
    rl = InMemoryRejectionLog()
    h = CommandHandler(log, rl)
    d = EventDispatcher(log)
    d.register(_Noop())
    await d.start("SliT")
    try:
        await h.handle(_cmd())
        # Let the dispatcher drain.
        for _ in range(50):
            if recorder.fan_outs:
                break
            import asyncio as _asyncio

            await _asyncio.sleep(0)
        assert any(obs["aggregate_type"] == "SliT" for obs in recorder.fan_outs)
    finally:
        await d.stop()
