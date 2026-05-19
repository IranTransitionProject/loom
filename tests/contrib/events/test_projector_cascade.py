"""Tests for CascadeProjector (Sprint 2 T8b)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from heddle.contrib.events.aggregate import IntervalAggregate, RootAggregate
from heddle.contrib.events.command_handler import CommandHandler
from heddle.contrib.events.envelopes import (
    CommandMetadata,
    EventEnvelope,
    EventMetadata,
)
from heddle.contrib.events.event_log import InMemoryEventLog
from heddle.contrib.events.projectors import (
    CASCADE_ISSUED_BY,
    CHILD_MEMBERSHIP_KEY,
    CascadeProjector,
    ScopeMembershipProjector,
    deterministic_cascade_id,
)
from heddle.contrib.events.registry import register_aggregate
from heddle.contrib.events.rejection_log import InMemoryRejectionLog

pytestmark = pytest.mark.usefixtures("registry_isolation")


def _make_classes():
    @register_aggregate("CRoot")
    class _Root(RootAggregate):
        def handle_internal_finalize(
            self, payload: dict[str, Any], metadata: CommandMetadata
        ) -> tuple[str, dict[str, Any]]:
            from heddle.contrib.events.errors import CommandRejected

            if self.phase == "finalized":
                raise CommandRejected("ALREADY_FINALIZED", "root finalized")
            return "InternalFinalized", {}

    @register_aggregate("CChild")
    class _Child(IntervalAggregate):
        def handle_internal_finalize(
            self, payload: dict[str, Any], metadata: CommandMetadata
        ) -> tuple[str, dict[str, Any]]:
            from heddle.contrib.events.errors import CommandRejected

            if self.phase == "finalized":
                raise CommandRejected("ALREADY_FINALIZED", "child finalized")
            return "InternalFinalized", {}

    return _Root, _Child


def _root_finalized_envelope(
    *, root_id: str = "root-1", event_id: str | None = None
) -> EventEnvelope:
    now = datetime.now(UTC)
    kwargs: dict[str, Any] = {
        "aggregate_type": "CRoot",
        "aggregate_id": root_id,
        "aggregate_version": 1,
        "event_type": "InternalFinalized",
        "payload": {},
        "metadata": EventMetadata(issued_by="framework:horizon"),
        "occurred_at": now,
        "recorded_at": now,
    }
    if event_id is not None:
        kwargs["event_id"] = event_id
    return EventEnvelope(**kwargs)


@pytest.fixture
def wiring():
    _make_classes()
    el = InMemoryEventLog()
    rl = InMemoryRejectionLog()
    h = CommandHandler(el, rl)
    m = ScopeMembershipProjector()
    c = CascadeProjector(m, h)
    return el, rl, h, m, c


@pytest.mark.asyncio
async def test_cascade_finalizes_registered_children(wiring) -> None:
    el, _rl, _h, m, c = wiring

    # Membership pre-populated for the root.
    for child_id in ("c-1", "c-2"):
        await m.project(
            EventEnvelope(
                aggregate_type="CRoot",
                aggregate_id="root-1",
                aggregate_version=1,
                event_type="ChildAdded",
                payload={
                    CHILD_MEMBERSHIP_KEY: {
                        "add": [{"type": "CChild", "id": child_id}]
                    }
                },
                metadata=EventMetadata(issued_by="user:badge:test"),
                occurred_at=datetime.now(UTC),
                recorded_at=datetime.now(UTC),
            )
        )

    await c.project(_root_finalized_envelope())

    for child_id in ("c-1", "c-2"):
        events = [ev async for ev in el.load("CChild", child_id)]
        finalized = [ev for ev in events if ev.event_type == "InternalFinalized"]
        assert len(finalized) == 1, f"child {child_id} missing InternalFinalized"
        assert finalized[0].metadata.issued_by == CASCADE_ISSUED_BY


@pytest.mark.asyncio
async def test_deterministic_command_id() -> None:
    a = deterministic_cascade_id("root-1", "c-1", "event-X")
    b = deterministic_cascade_id("root-1", "c-1", "event-X")
    c = deterministic_cascade_id("root-1", "c-2", "event-X")
    assert a == b
    assert a != c
    # Shape: a parseable UUID.
    from uuid import UUID

    UUID(a)


@pytest.mark.asyncio
async def test_cascade_is_idempotent(wiring) -> None:
    """Re-running the projector on the same root event must not
    double-finalize the children. Sprint 2 idempotence comes from
    the receiving aggregate's apply_internal_finalized being a no-op
    on an already-finalized aggregate AND the receiving aggregate
    rejecting an InternalFinalize command when already finalized
    (CommandRejected, swallowed)."""
    el, _rl, _h, m, c = wiring

    await m.project(
        EventEnvelope(
            aggregate_type="CRoot",
            aggregate_id="root-1",
            aggregate_version=1,
            event_type="ChildAdded",
            payload={
                CHILD_MEMBERSHIP_KEY: {
                    "add": [{"type": "CChild", "id": "c-1"}]
                }
            },
            metadata=EventMetadata(issued_by="user:badge:test"),
            occurred_at=datetime.now(UTC),
            recorded_at=datetime.now(UTC),
        )
    )

    root_ev = _root_finalized_envelope(event_id="fixed-event-id-1")
    await c.project(root_ev)
    await c.project(root_ev)

    events = [ev async for ev in el.load("CChild", "c-1")]
    finalized = [ev for ev in events if ev.event_type == "InternalFinalized"]
    assert len(finalized) == 1


@pytest.mark.asyncio
async def test_command_rejected_swallowed(wiring) -> None:
    """A child that rejects InternalFinalize (already finalized) must
    not raise out of cascade."""
    el, _rl, _h, m, c = wiring

    # Register a child and finalize it directly via the handler first.
    await m.project(
        EventEnvelope(
            aggregate_type="CRoot",
            aggregate_id="root-1",
            aggregate_version=1,
            event_type="ChildAdded",
            payload={
                CHILD_MEMBERSHIP_KEY: {
                    "add": [{"type": "CChild", "id": "c-1"}]
                }
            },
            metadata=EventMetadata(issued_by="user:badge:test"),
            occurred_at=datetime.now(UTC),
            recorded_at=datetime.now(UTC),
        )
    )
    # Pre-finalize the child via direct cascade-shaped command.
    from heddle.contrib.events.envelopes import CommandMessage

    await c._handler.handle(
        CommandMessage(
            aggregate_type="CChild",
            aggregate_id="c-1",
            command_type="InternalFinalize",
            payload={},
            metadata=CommandMetadata(issued_by="framework:cascade"),
            issued_at=datetime.now(UTC),
        )
    )

    # Now cascade on the root — the child rejects; we expect no raise.
    await c.project(_root_finalized_envelope())

    events = [ev async for ev in el.load("CChild", "c-1")]
    finalized = [ev for ev in events if ev.event_type == "InternalFinalized"]
    # Only one InternalFinalized (from the pre-finalize call), not a
    # second from the swallowed cascade attempt.
    assert len(finalized) == 1


@pytest.mark.asyncio
async def test_non_internal_finalized_event_ignored(wiring) -> None:
    el, _rl, _h, m, c = wiring
    await m.project(
        EventEnvelope(
            aggregate_type="CRoot",
            aggregate_id="root-1",
            aggregate_version=1,
            event_type="ChildAdded",
            payload={
                CHILD_MEMBERSHIP_KEY: {
                    "add": [{"type": "CChild", "id": "c-1"}]
                }
            },
            metadata=EventMetadata(issued_by="user:badge:test"),
            occurred_at=datetime.now(UTC),
            recorded_at=datetime.now(UTC),
        )
    )

    # An unrelated event on the root must NOT trigger cascade.
    await c.project(
        EventEnvelope(
            aggregate_type="CRoot",
            aggregate_id="root-1",
            aggregate_version=2,
            event_type="ChildAdded",
            payload={},
            metadata=EventMetadata(issued_by="user:badge:test"),
            occurred_at=datetime.now(UTC),
            recorded_at=datetime.now(UTC),
        )
    )

    events = [ev async for ev in el.load("CChild", "c-1")]
    assert events == []


@pytest.mark.asyncio
async def test_non_root_internal_finalized_ignored(wiring) -> None:
    el, _rl, _h, _m, c = wiring
    # CChild is an IntervalAggregate, not a Root. Even if it finalizes,
    # cascade must NOT fire for it.
    await c.project(
        EventEnvelope(
            aggregate_type="CChild",
            aggregate_id="c-1",
            aggregate_version=1,
            event_type="InternalFinalized",
            payload={},
            metadata=EventMetadata(issued_by="framework:horizon"),
            occurred_at=datetime.now(UTC),
            recorded_at=datetime.now(UTC),
        )
    )
    # Nothing should have been emitted.
    assert el._events == {}
