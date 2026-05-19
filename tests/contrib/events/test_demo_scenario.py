"""End-to-end Sprint 2 demo scenario (T11).

This is the regression sentinel — if anything in Sprint 2 breaks
structurally, this test fails first. Exercises:

- AGGREGATE_REGISTRY (FakeRoot + FakeInterval from
  ``heddle.contrib.events.testing``)
- :class:`CommandHandler` orchestration
- :class:`InMemoryEventLog` (CAS + monotonicity)
- :class:`InMemoryRejectionLog`
- :class:`EventDispatcher` serial fan-out
- :class:`ScopeMembershipProjector` (P1)
- :class:`CascadeProjector` (P2)
- :class:`IntervalAggregate.apply_internal_finalized` discipline
- Cascade idempotence via the receiving aggregate's
  already-finalized rejection
- Rejection path (RejectionEnvelope written)
"""

from __future__ import annotations

import asyncio

import pytest

from heddle.contrib.events.command_handler import CommandHandler
from heddle.contrib.events.dispatcher import EventDispatcher
from heddle.contrib.events.event_log import InMemoryEventLog
from heddle.contrib.events.projectors import (
    CASCADE_ISSUED_BY,
    CascadeProjector,
    ScopeMembershipProjector,
)
from heddle.contrib.events.rejection_log import InMemoryRejectionLog
from heddle.contrib.events.testing import make_command

# Empirically: ~10 ms is plenty for the dispatcher loop + projector
# fan-out + cascade command-handler round-trip on a quiet machine.
# Bumped to 100 ms for CI variance.
DISPATCH_DRAIN = 0.10


async def _wait_for_subscriber(
    event_log: InMemoryEventLog, aggregate_type: str, timeout: float = 1.0
) -> None:
    """Spin until the dispatcher's _run task has registered its queue
    with the in-memory event log. Cheaper and more deterministic than
    sleeping a magic constant."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if event_log._subscribers.get(aggregate_type):
            return
        await asyncio.sleep(0)
    raise AssertionError(
        f"dispatcher did not subscribe to {aggregate_type!r} within {timeout}s"
    )


@pytest.mark.asyncio
async def test_root_finalization_cascades_to_children() -> None:
    event_log = InMemoryEventLog()
    rejection_log = InMemoryRejectionLog()
    handler = CommandHandler(event_log, rejection_log)
    membership = ScopeMembershipProjector()
    cascade = CascadeProjector(membership, handler)

    dispatcher = EventDispatcher(event_log)
    dispatcher.register(membership)
    dispatcher.register(cascade)
    await dispatcher.start("FakeRoot")
    await dispatcher.start("FakeInterval")
    await _wait_for_subscriber(event_log, "FakeRoot")
    await _wait_for_subscriber(event_log, "FakeInterval")

    try:
        # 1. Bring root + 2 children into existence.
        await handler.handle(
            make_command(
                aggregate_type="FakeRoot",
                aggregate_id="root-1",
                command_type="AddChild",
                payload={"child_id": "child-1"},
            )
        )
        await handler.handle(
            make_command(
                aggregate_type="FakeRoot",
                aggregate_id="root-1",
                command_type="AddChild",
                payload={"child_id": "child-2"},
                expected_aggregate_version=1,
            )
        )

        await asyncio.sleep(DISPATCH_DRAIN)

        # 2. P1 has registered both children.
        children = membership.children_of("FakeRoot", "root-1", "FakeInterval")
        assert children == frozenset({"child-1", "child-2"})

        # 3. Finalize the root.
        await handler.handle(
            make_command(
                aggregate_type="FakeRoot",
                aggregate_id="root-1",
                command_type="InternalFinalize",
                payload={},
                issued_by="framework:horizon",
                expected_aggregate_version=2,
            )
        )

        await asyncio.sleep(DISPATCH_DRAIN)

        # 4. P2 cascade-finalized both children with
        # issued_by='framework:cascade'.
        for child_id in ("child-1", "child-2"):
            events = [
                ev async for ev in event_log.load("FakeInterval", child_id)
            ]
            finalized = [
                ev for ev in events if ev.event_type == "InternalFinalized"
            ]
            assert len(finalized) == 1, (
                f"child {child_id} missing InternalFinalized "
                f"(events={[e.event_type for e in events]})"
            )
            assert finalized[0].metadata.issued_by == CASCADE_ISSUED_BY

        # 5. Idempotence: re-deliver the root's InternalFinalized
        # via a direct project() call. Deterministic command IDs +
        # already-finalized rejection mean no double-finalization.
        root_events = [
            ev async for ev in event_log.load("FakeRoot", "root-1")
        ]
        finalized_root_event = next(
            ev for ev in root_events if ev.event_type == "InternalFinalized"
        )
        await cascade.project(finalized_root_event)

        for child_id in ("child-1", "child-2"):
            events = [
                ev async for ev in event_log.load("FakeInterval", child_id)
            ]
            internal_finalized = [
                ev for ev in events if ev.event_type == "InternalFinalized"
            ]
            assert len(internal_finalized) == 1, (
                f"child {child_id} double-finalized; cascade not idempotent"
            )

        # 6. Rejection path: re-finalize the root.
        from heddle.contrib.events.errors import CommandRejected

        with pytest.raises(CommandRejected) as exc:
            await handler.handle(
                make_command(
                    aggregate_type="FakeRoot",
                    aggregate_id="root-1",
                    command_type="InternalFinalize",
                    payload={},
                    expected_aggregate_version=3,
                )
            )
        assert exc.value.reason == "ALREADY_FINALIZED"

        rejections = [
            r async for r in rejection_log.load("FakeRoot", "root-1")
        ]
        assert len(rejections) == 1
        assert rejections[0].reason == "ALREADY_FINALIZED"
        assert rejections[0].command.command_type == "InternalFinalize"

    finally:
        await dispatcher.stop()


@pytest.mark.asyncio
async def test_demo_uses_wired_dispatcher_fixture(
    wired_dispatcher, in_memory_event_log, command_handler
) -> None:
    """Smoke test that the fixtures in tests/fixtures.py actually wire
    P1 + P2 onto the dispatcher correctly."""
    await wired_dispatcher.start("FakeRoot")
    await wired_dispatcher.start("FakeInterval")
    await _wait_for_subscriber(in_memory_event_log, "FakeRoot")
    await _wait_for_subscriber(in_memory_event_log, "FakeInterval")

    await command_handler.handle(
        make_command(
            aggregate_type="FakeRoot",
            aggregate_id="root-fixture",
            command_type="AddChild",
            payload={"child_id": "c-fixture"},
        )
    )
    await asyncio.sleep(DISPATCH_DRAIN)
    await command_handler.handle(
        make_command(
            aggregate_type="FakeRoot",
            aggregate_id="root-fixture",
            command_type="InternalFinalize",
            payload={},
            issued_by="framework:horizon",
            expected_aggregate_version=1,
        )
    )
    await asyncio.sleep(DISPATCH_DRAIN)

    child_events = [
        ev async for ev in in_memory_event_log.load(
            "FakeInterval", "c-fixture"
        )
    ]
    assert any(
        ev.event_type == "InternalFinalized"
        and ev.metadata.issued_by == CASCADE_ISSUED_BY
        for ev in child_events
    )
