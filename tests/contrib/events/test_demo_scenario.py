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
            events = [ev async for ev in event_log.load("FakeInterval", child_id)]
            finalized = [ev for ev in events if ev.event_type == "InternalFinalized"]
            assert len(finalized) == 1, (
                f"child {child_id} missing InternalFinalized "
                f"(events={[e.event_type for e in events]})"
            )
            assert finalized[0].metadata.issued_by == CASCADE_ISSUED_BY

        # 5. Idempotence: re-deliver the root's InternalFinalized
        # via a direct project() call. Deterministic command IDs +
        # already-finalized rejection mean no double-finalization.
        root_events = [ev async for ev in event_log.load("FakeRoot", "root-1")]
        finalized_root_event = next(
            ev for ev in root_events if ev.event_type == "InternalFinalized"
        )
        await cascade.project(finalized_root_event)

        for child_id in ("child-1", "child-2"):
            events = [ev async for ev in event_log.load("FakeInterval", child_id)]
            internal_finalized = [ev for ev in events if ev.event_type == "InternalFinalized"]
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

        rejections = [r async for r in rejection_log.load("FakeRoot", "root-1")]
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

    child_events = [ev async for ev in in_memory_event_log.load("FakeInterval", "c-fixture")]
    assert any(
        ev.event_type == "InternalFinalized" and ev.metadata.issued_by == CASCADE_ISSUED_BY
        for ev in child_events
    )


# ---------------------------------------------------------------------------
# Sprint 3 demo-scenario extensions (T15).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_process_dedup_via_published_events() -> None:
    """Sprint 3 R7 end-to-end: two CommandHandler instances share a
    snapshot store and an in-memory NATS-core bus. Handler1 processes
    a command; Handler2 (with the aggregate already cached) sees the
    published mark_processed event and treats a repeat as a dup."""
    from heddle.contrib.events.dedup_publisher import NatsDedupPublisher
    from heddle.contrib.events.dedup_subscriber import NatsDedupSubscriber
    from heddle.contrib.events.registry import get_aggregate_class
    from heddle.contrib.events.snapshot_store import SnapshotStore
    from heddle.core.kvstore import InMemoryKeyValueStore
    from tests.contrib.events.test_hybrid_dedup import InMemoryNatsBus

    event_log = InMemoryEventLog()
    rejection_log = InMemoryRejectionLog()
    bus = InMemoryNatsBus()
    kv = InMemoryKeyValueStore()
    snapshots = SnapshotStore(kv)

    h1 = CommandHandler(
        event_log, rejection_log,
        snapshot_store=snapshots,
        dedup_publisher=NatsDedupPublisher(bus),  # type: ignore[arg-type]
        dedup_subscriber=NatsDedupSubscriber(bus),  # type: ignore[arg-type]
    )
    h2 = CommandHandler(
        event_log, rejection_log,
        snapshot_store=snapshots,
        dedup_publisher=NatsDedupPublisher(bus),  # type: ignore[arg-type]
        dedup_subscriber=NatsDedupSubscriber(bus),  # type: ignore[arg-type]
    )

    # Prime h2's cache so it has a live mark_processed subscription.
    await h2._load_or_create(get_aggregate_class("FakeRoot"), "root-shared")

    first = await h1.handle(
        make_command(
            aggregate_type="FakeRoot",
            aggregate_id="root-shared",
            command_type="AddChild",
            payload={"child_id": "x"},
        )
    )
    await asyncio.sleep(0)  # let the subscriber callback complete

    # h2's cached aggregate now knows about the command_id from h1.
    cached = h2._cache.get(("FakeRoot", "root-shared"))
    assert cached is not None
    assert cached.has_processed(first.metadata.command_id or "")

    # Re-running the same command on h2 returns the original event.
    second = await h2.handle(
        make_command(
            aggregate_type="FakeRoot",
            aggregate_id="root-shared",
            command_type="AddChild",
            payload={"child_id": "x"},
            command_id=first.metadata.command_id,
            expected_aggregate_version=1,
        )
    )
    assert second.event_id == first.event_id


@pytest.mark.asyncio
async def test_lease_prevents_double_finalization() -> None:
    """Sprint 3 R5 end-to-end: with both P2 and P3 wired up against a
    shared in-memory KV, a horizon fire and a cascade attempt cannot
    both publish — the lease serialises them."""
    from heddle.contrib.events.lease import finalization_lease, lease_key
    from heddle.core.kvstore import InMemoryKeyValueStore

    event_log = InMemoryEventLog()
    rejection_log = InMemoryRejectionLog()
    handler = CommandHandler(event_log, rejection_log)
    kv = InMemoryKeyValueStore()
    membership = ScopeMembershipProjector()
    cascade = CascadeProjector(membership, handler, kv=kv)

    dispatcher = EventDispatcher(event_log)
    dispatcher.register(membership)
    dispatcher.register(cascade)
    await dispatcher.start("FakeRoot")
    await dispatcher.start("FakeInterval")

    try:
        # Bring the root + a child into existence.
        await handler.handle(
            make_command(
                aggregate_type="FakeRoot",
                aggregate_id="root-lease",
                command_type="AddChild",
                payload={"child_id": "c-lease"},
            )
        )
        await asyncio.sleep(DISPATCH_DRAIN)

        # Simulate P3 winning the race by pre-claiming the child's lease.
        async with finalization_lease(
            kv, "FakeInterval", "c-lease", "framework:horizon"
        ) as claimed:
            assert claimed is True
            # While the lease is held, finalise the root; cascade fires
            # but the lease is occupied → cascade publish is skipped.
            await handler.handle(
                make_command(
                    aggregate_type="FakeRoot",
                    aggregate_id="root-lease",
                    command_type="InternalFinalize",
                    payload={},
                    issued_by="framework:horizon",
                    expected_aggregate_version=1,
                )
            )
            await asyncio.sleep(DISPATCH_DRAIN)

        # Verify: child has NOT been finalised by cascade (lease blocked it).
        child_events = [
            ev async for ev in event_log.load("FakeInterval", "c-lease")
        ]
        finalised = [ev for ev in child_events if ev.event_type == "InternalFinalized"]
        assert finalised == [], (
            "cascade should not have published while lease was held"
        )
        # Lease key remains for audit.
        held = await kv.get(lease_key("FakeInterval", "c-lease"))
        assert held is not None and held.startswith("framework:horizon:")
    finally:
        await dispatcher.stop()
