"""Tests for the CommandHandler nine-step orchestration (Sprint 2 T6)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from heddle.contrib.events.aggregate import IntervalAggregate
from heddle.contrib.events.cache import AggregateCache
from heddle.contrib.events.command_handler import CommandHandler
from heddle.contrib.events.envelopes import (
    CommandMessage,
    CommandMetadata,
    EventMetadata,
)
from heddle.contrib.events.errors import (
    CommandRejected,
    ConcurrencyError,
)
from heddle.contrib.events.event_log import InMemoryEventLog
from heddle.contrib.events.registry import register_aggregate
from heddle.contrib.events.rejection_log import InMemoryRejectionLog
from heddle.contrib.events.snapshot_store import SnapshotStore
from heddle.core.kvstore import InMemoryKeyValueStore

pytestmark = pytest.mark.usefixtures("registry_isolation")


def _cmd(
    *,
    aggregate_type: str = "FakeI",
    aggregate_id: str = "a-1",
    command_type: str = "DoThing",
    payload: dict[str, Any] | None = None,
    issued_by: str = "user:badge:206",
    correlation_id: str | None = "corr-1",
    expected_aggregate_version: int | None = None,
    command_id: str | None = None,
) -> CommandMessage:
    kwargs: dict[str, Any] = {
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "command_type": command_type,
        "payload": payload or {},
        "metadata": CommandMetadata(issued_by=issued_by, correlation_id=correlation_id),
        "issued_at": datetime.now(UTC),
        "expected_aggregate_version": expected_aggregate_version,
    }
    if command_id is not None:
        kwargs["command_id"] = command_id
    return CommandMessage(**kwargs)


def _make_fake_class():
    @register_aggregate("FakeI")
    class _Fake(IntervalAggregate):
        def __init__(self, aggregate_id: str) -> None:
            super().__init__(aggregate_id)
            self.things: list[dict[str, Any]] = []

        def handle_do_thing(
            self, payload: dict[str, Any], metadata: CommandMetadata
        ) -> tuple[str, dict[str, Any]]:
            if payload.get("forbidden"):
                raise CommandRejected("FORBIDDEN", "payload had forbidden flag")
            return "ThingHappened", dict(payload)

        def apply_thing_happened(self, payload: dict[str, Any], metadata: EventMetadata) -> None:
            self.things.append(payload)

    return _Fake


@pytest.fixture
def handler() -> tuple[CommandHandler, InMemoryEventLog, InMemoryRejectionLog]:
    el = InMemoryEventLog()
    rl = InMemoryRejectionLog()
    return CommandHandler(el, rl), el, rl


@pytest.mark.asyncio
async def test_happy_path(handler) -> None:
    _make_fake_class()
    h, el, _rl = handler

    env = await h.handle(_cmd(payload={"a": 1}))

    assert env.event_type == "ThingHappened"
    assert env.aggregate_version == 1
    assert env.payload == {"a": 1}
    # Metadata propagated from command.
    assert env.metadata.correlation_id == "corr-1"
    assert env.metadata.issued_by == "user:badge:206"
    # Event landed in the log.
    events = [ev async for ev in el.load("FakeI", "a-1")]
    assert len(events) == 1
    assert events[0].event_id == env.event_id


@pytest.mark.asyncio
async def test_expected_version_match_succeeds(handler) -> None:
    _make_fake_class()
    h, _el, _rl = handler
    await h.handle(_cmd(payload={"first": True}))
    env = await h.handle(_cmd(payload={"second": True}, expected_aggregate_version=1))
    assert env.aggregate_version == 2


@pytest.mark.asyncio
async def test_expected_version_mismatch_raises(handler) -> None:
    _make_fake_class()
    h, _el, _rl = handler
    await h.handle(_cmd())

    with pytest.raises(ConcurrencyError, match="expected_aggregate_version=99"):
        await h.handle(_cmd(expected_aggregate_version=99))


@pytest.mark.asyncio
async def test_unregistered_aggregate_type_raises(handler) -> None:
    h, _el, _rl = handler
    with pytest.raises(KeyError, match="no aggregate registered"):
        await h.handle(_cmd(aggregate_type="Unknown"))


@pytest.mark.asyncio
async def test_missing_handler_raises_attribute_error(handler) -> None:
    _make_fake_class()
    h, _el, _rl = handler
    with pytest.raises(AttributeError, match="handle_no_such_thing"):
        await h.handle(_cmd(command_type="NoSuchThing"))


@pytest.mark.asyncio
async def test_command_rejected_logged_and_reraised(handler) -> None:
    _make_fake_class()
    h, el, rl = handler

    with pytest.raises(CommandRejected) as excinfo:
        await h.handle(_cmd(payload={"forbidden": True}))
    assert excinfo.value.reason == "FORBIDDEN"

    rejections = [r async for r in rl.load("FakeI")]
    assert len(rejections) == 1
    assert rejections[0].reason == "FORBIDDEN"
    assert rejections[0].detail == "payload had forbidden flag"
    assert rejections[0].command.command_type == "DoThing"

    # The rejected command produced NO event.
    events = [ev async for ev in el.load("FakeI", "a-1")]
    assert events == []


@pytest.mark.asyncio
async def test_dedup_was_per_call_in_sprint_2() -> None:
    """Sprint 2 pinned-behaviour regression guard.

    With BOTH the aggregate cache disabled (``max_size=0``) AND no
    snapshot store wired in, the handler matches Sprint 2's reload-
    every-call semantics: the dedup buffer is empty at the start of
    each call (v7 §4.5: never reconstructed from event replay), so a
    duplicate command_id produces a SECOND event with a fresh
    event_id. Harmless per v7 §4.11.

    Sprint 3 cache (T2) and snapshot store (T3) each independently
    make cross-call dedup work; the
    :func:`test_dedup_works_cross_call_with_snapshot` test below
    documents the positive case.
    """
    _make_fake_class()
    el = InMemoryEventLog()
    rl = InMemoryRejectionLog()
    h = CommandHandler(el, rl, cache=AggregateCache(max_size=0))

    first = await h.handle(_cmd(command_id="cmd-X", payload={"v": 1}))
    second = await h.handle(
        _cmd(
            command_id="cmd-X",
            payload={"v": 1},
            expected_aggregate_version=1,
        )
    )

    assert first.event_id != second.event_id
    assert first.metadata.command_id == "cmd-X"
    assert second.metadata.command_id == "cmd-X"

    events = [ev async for ev in el.load("FakeI", "a-1")]
    assert len(events) == 2


@pytest.mark.asyncio
async def test_dedup_works_within_call_via_cache() -> None:
    """Sprint 3 T2 positive: with the default cache enabled but NO
    snapshot store, the second call hits the cached aggregate (whose
    buffer remembers ``cmd-X``) and the original event is returned.
    Cross-process is still untested here; that's the snapshot test.
    """
    _make_fake_class()
    el = InMemoryEventLog()
    rl = InMemoryRejectionLog()
    h = CommandHandler(el, rl)

    first = await h.handle(_cmd(command_id="cmd-X", payload={"v": 1}))
    second = await h.handle(
        _cmd(
            command_id="cmd-X",
            payload={"v": 1},
            expected_aggregate_version=1,
        )
    )

    assert first.event_id == second.event_id
    events = [ev async for ev in el.load("FakeI", "a-1")]
    assert len(events) == 1


@pytest.mark.asyncio
async def test_dedup_works_cross_call_with_snapshot() -> None:
    """Sprint 3 T3 / R1 positive: snapshot persistence makes dedup
    survive across CommandHandler instances. Process Handler1 with a
    shared snapshot store; discard it; build Handler2 against the same
    KV; replay of ``cmd-X`` returns the original event.
    """
    _make_fake_class()
    el = InMemoryEventLog()
    rl = InMemoryRejectionLog()
    kv = InMemoryKeyValueStore()
    snapshots = SnapshotStore(kv)

    h1 = CommandHandler(el, rl, snapshot_store=snapshots, snapshot_every_n=1)
    first = await h1.handle(_cmd(command_id="cmd-X", payload={"v": 1}))

    h2 = CommandHandler(el, rl, snapshot_store=snapshots, snapshot_every_n=1)
    second = await h2.handle(
        _cmd(
            command_id="cmd-X",
            payload={"v": 1},
            expected_aggregate_version=1,
        )
    )

    assert first.event_id == second.event_id
    events = [ev async for ev in el.load("FakeI", "a-1")]
    assert len(events) == 1


@pytest.mark.asyncio
async def test_dedup_edge_case_buffer_restore_without_events() -> None:
    """v7 §4.11 edge case: dedup buffer carries a command_id but no
    event in the log corresponds to it (e.g. snapshot persisted the
    buffer after the in-memory mark but before the event reached the
    log, then process crashed). Re-execution is allowed; the resulting
    duplicate event carries a fresh event_id and is harmless.

    Sprint 3 reaches this branch via the snapshot path: stage a
    snapshot whose dedup buffer contains a phantom command_id, then
    submit a command with that command_id and verify a new event is
    produced rather than a NoneType returned to the caller.
    """
    cls = _make_fake_class()
    el = InMemoryEventLog()
    rl = InMemoryRejectionLog()
    kv = InMemoryKeyValueStore()
    snapshots = SnapshotStore(kv)

    # Hand-craft a snapshot at version 0 carrying a phantom command_id.
    phantom = cls(aggregate_id="a-1")
    phantom.mark_processed("phantom-cmd")
    await snapshots.save(phantom)

    h = CommandHandler(el, rl, snapshot_store=snapshots)
    env = await h.handle(_cmd(command_id="phantom-cmd", payload={"v": 1}))
    # Buffer claimed processed; log had nothing — re-execute path.
    assert env.aggregate_version == 1
    events = [ev async for ev in el.load("FakeI", "a-1")]
    assert len(events) == 1


@pytest.mark.asyncio
async def test_metadata_propagates_command_id_and_correlation(handler) -> None:
    _make_fake_class()
    h, _el, _rl = handler

    cmd = _cmd(correlation_id="corr-42")
    env = await h.handle(cmd)

    assert env.metadata.command_id == cmd.command_id
    assert env.metadata.correlation_id == "corr-42"
    assert env.metadata.issued_by == cmd.metadata.issued_by


@pytest.mark.asyncio
async def test_replay_reaches_current_version(handler) -> None:
    """A second command on an aggregate whose state is rebuilt purely
    from the log lands at version=2. Proves load+replay+apply works
    end-to-end."""
    _make_fake_class()
    h, _el, _rl = handler

    await h.handle(_cmd(payload={"n": 1}))
    env2 = await h.handle(_cmd(payload={"n": 2}))
    assert env2.aggregate_version == 2
