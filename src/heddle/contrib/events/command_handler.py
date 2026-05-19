"""CommandHandler — orchestrates the command -> event flow.

Per v7 §4.6, every command goes through nine steps:

1. Look up aggregate class via the registry.
2. Load events from EventLog and replay through ``apply()``
   (Sprint 3 adds a KeyValueStore snapshot fast path).
3. Check ``has_processed(command_id)`` — dedup buffer.
4. Validate ``expected_aggregate_version`` if not None.
5. Look up ``handle_<command_type_snake>`` on the aggregate.
6. Invoke handler -> ``(event_type, event_payload)``.
7. On :class:`CommandRejected`: append a
   :class:`RejectionEnvelope` to RejectionLog and re-raise.
8. Construct :class:`EventEnvelope` (new event_id, version=current+1,
   propagated correlation_id + command_id).
9. Append with CAS, then ``apply()`` to the in-memory aggregate and
   ``mark_processed(command_id)``. Return the envelope.

Sprint 2 ships the base class. Sprint 3 adds a JetStream-backed
subclass plus the snapshot fast path. Sprint 4a's PFObservers use
``CommandHandler`` directly to ingest PF-sourced commands.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from heddle.contrib.events.aggregate import Aggregate, snake_case
from heddle.contrib.events.envelopes import (
    EventEnvelope,
    EventMetadata,
)
from heddle.contrib.events.errors import (
    CommandRejected,
    ConcurrencyError,
)
from heddle.contrib.events.registry import get_aggregate_class
from heddle.contrib.events.rejection_log import RejectionEnvelope

if TYPE_CHECKING:
    from heddle.contrib.events.envelopes import CommandMessage
    from heddle.contrib.events.event_log import EventLog
    from heddle.contrib.events.rejection_log import RejectionLog


class CommandHandler:
    """Orchestrate command processing through the aggregate model."""

    def __init__(
        self, event_log: EventLog, rejection_log: RejectionLog
    ) -> None:
        self._event_log = event_log
        self._rejection_log = rejection_log

    async def handle(self, cmd: CommandMessage) -> EventEnvelope:
        """Process a command and produce the resulting event.

        Raises:
            KeyError: ``aggregate_type`` not registered.
            ConcurrencyError: ``expected_aggregate_version`` mismatch.
            CommandRejected: aggregate handler rejected the command.
            AttributeError: aggregate has no ``handle_<command_type>``.
        """
        cls = get_aggregate_class(cmd.aggregate_type)
        aggregate = await self._load_or_create(cls, cmd.aggregate_id)

        # ---- 3. Dedup check — idempotent retry path. -----------------------
        if aggregate.has_processed(cmd.command_id):
            replay = await self._find_event_by_command_id(
                cmd.aggregate_type, cmd.aggregate_id, cmd.command_id
            )
            if replay is not None:
                return replay
            # Edge case (v7 §4.11): dedup buffer says yes but no event
            # carries this command_id — buffer-restore-without-events.
            # Fall through and re-execute; the resulting duplicate
            # event with a new event_id is harmless.

        # ---- 4. Optimistic concurrency check at command level. -------------
        if (
            cmd.expected_aggregate_version is not None
            and cmd.expected_aggregate_version != aggregate.aggregate_version
        ):
            raise ConcurrencyError(
                f"command expected_aggregate_version="
                f"{cmd.expected_aggregate_version} but aggregate "
                f"version={aggregate.aggregate_version}"
            )

        # ---- 5. Dispatch to aggregate.handle_<command_type>(). -------------
        handler_name = f"handle_{snake_case(cmd.command_type)}"
        handler = getattr(aggregate, handler_name, None)
        if handler is None:
            raise AttributeError(
                f"{type(aggregate).__name__} has no {handler_name}"
            )

        # ---- 6+7. Invoke handler; rejection -> append + re-raise. ----------
        try:
            event_type, event_payload = handler(cmd.payload, cmd.metadata)
        except CommandRejected as rej:
            await self._rejection_log.append(
                RejectionEnvelope(
                    command=cmd,
                    reason=rej.reason,
                    detail=rej.detail,
                    rejected_at=datetime.now(UTC),
                )
            )
            raise

        # ---- 8. Build envelope. --------------------------------------------
        current_version = aggregate.aggregate_version
        now = datetime.now(UTC)
        envelope = EventEnvelope(
            aggregate_type=cmd.aggregate_type,
            aggregate_id=cmd.aggregate_id,
            aggregate_version=current_version + 1,
            event_type=event_type,
            event_version=1,
            payload=event_payload,
            metadata=EventMetadata(
                command_id=cmd.command_id,
                correlation_id=cmd.metadata.correlation_id,
                issued_by=cmd.metadata.issued_by,
            ),
            occurred_at=now,
            recorded_at=now,
        )

        # ---- 9. Append with CAS, then apply + mark_processed. --------------
        await self._event_log.append(envelope, expected_version=current_version)
        aggregate.apply(envelope)
        aggregate.mark_processed(cmd.command_id)

        return envelope

    async def _load_or_create(
        self, cls: type[Aggregate], aggregate_id: str
    ) -> Aggregate:
        """Rebuild aggregate from event-log replay (no snapshot in Sprint 2)."""
        aggregate = cls(aggregate_id=aggregate_id)
        async for envelope in self._event_log.load(
            cls.aggregate_type, aggregate_id
        ):
            aggregate.apply(envelope)
        return aggregate

    async def _find_event_by_command_id(
        self, aggregate_type: str, aggregate_id: str, command_id: str
    ) -> EventEnvelope | None:
        """Locate a previously-produced event by command_id.

        Sprint 2 implementation: linear scan over the aggregate's
        event log. Sprint 3 swaps this for a KeyValueStore secondary
        index so dedup-replay stays O(1).
        """
        async for ev in self._event_log.load(aggregate_type, aggregate_id):
            if ev.metadata.command_id == command_id:
                return ev
        return None
