"""P2: CascadeProjector — root finalization fan-out to children.

Per v7 §5.1. When a root aggregate's ``InternalFinalized`` event is
observed, look up children via :class:`ScopeMembershipProjector` (P1)
and emit an ``InternalFinalize`` command to each child via the
:class:`CommandHandler`.

Cascade commands carry::

    issued_by = "framework:cascade"
    expected_aggregate_version = None      (opportunistic)
    command_id = deterministic(root_id, child_id, root_event_id)

Determinism on the ``command_id`` is what makes the cascade idempotent
under retry — the same root finalization event always produces the
same set of cascade command IDs, so any retry path that re-runs P2
on the same root event collides on the dedup buffer (Sprint 3 with
snapshot) or the JetStream ``Nats-Msg-Id`` dedup (Sprint 3 wire).
Sprint 2 in-memory: idempotence is guaranteed by the receiving
aggregate's ``apply_internal_finalized`` being a no-op on an
already-finalized aggregate.

Child commands that fail with :class:`CommandRejected` (child
already finalized via a different path) or :class:`ConcurrencyError`
(child concurrently mutated) are swallowed silently — cascade is
opportunistic. P3 (Sprint 3) catches children that never finalize
via the horizon timeout.
"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import UUID

from heddle.contrib.events.dispatcher import Projector
from heddle.contrib.events.envelopes import CommandMessage, CommandMetadata
from heddle.contrib.events.errors import CommandRejected, ConcurrencyError
from heddle.contrib.events.lease import finalization_lease
from heddle.contrib.events.registry import is_root_type

if TYPE_CHECKING:
    from heddle.contrib.events.command_handler import CommandHandler
    from heddle.contrib.events.envelopes import EventEnvelope
    from heddle.contrib.events.projectors.scope_membership import (
        ScopeMembershipProjector,
    )
    from heddle.core.kvstore import KeyValueStore

CASCADE_ISSUED_BY = "framework:cascade"


def deterministic_cascade_id(root_id: str, child_id: str, root_event_id: str) -> str:
    """Generate a deterministic UUIDv4-shaped id for a cascade command.

    Same ``(root_id, child_id, root_event_id)`` always yields the same
    string. The shape is a valid UUID but the bits are SHA-256-derived
    rather than UUIDv7-timestamped — this is fine because the cascade
    command's identity is the (root, child, root_event) triple, not
    wall-clock ordering.
    """
    digest = sha256(f"cascade:{root_id}:{child_id}:{root_event_id}".encode()).digest()
    return str(UUID(bytes=digest[:16]))


class CascadeProjector(Projector):
    """Cascade finalization from a root aggregate to its children."""

    def __init__(
        self,
        membership: ScopeMembershipProjector,
        command_handler: CommandHandler,
        kv: KeyValueStore | None = None,
    ) -> None:
        self._membership = membership
        self._handler = command_handler
        self._kv = kv

    async def project(self, envelope: EventEnvelope) -> None:
        """Emit cascade commands when a root aggregate finalizes."""
        if envelope.event_type != "InternalFinalized":
            return
        if not is_root_type(envelope.aggregate_type):
            return

        children = self._membership.all_children_of(envelope.aggregate_type, envelope.aggregate_id)
        for child_type, child_ids in children.items():
            for child_id in child_ids:
                await self._cascade_one(envelope, child_type, child_id)

    async def _cascade_one(
        self,
        envelope: EventEnvelope,
        child_type: str,
        child_id: str,
    ) -> None:
        cmd = CommandMessage(
            command_id=deterministic_cascade_id(envelope.aggregate_id, child_id, envelope.event_id),
            aggregate_type=child_type,
            aggregate_id=child_id,
            command_type="InternalFinalize",
            payload={},
            metadata=CommandMetadata(
                correlation_id=envelope.metadata.correlation_id or envelope.event_id,
                issued_by=CASCADE_ISSUED_BY,
            ),
            issued_at=datetime.now(UTC),
            expected_aggregate_version=None,
        )

        if self._kv is None:
            # No lease — Sprint 2 behaviour for in-memory tests.
            await self._try_handle(cmd)
            return

        async with finalization_lease(self._kv, child_type, child_id, CASCADE_ISSUED_BY) as claimed:
            if not claimed:
                return  # P3 (or another P2 instance) already claimed.
            await self._try_handle(cmd)

    async def _try_handle(self, cmd: CommandMessage) -> None:
        try:
            await self._handler.handle(cmd)
        except CommandRejected:
            # Child already finalized via another path, or a domain
            # rule. P3 catches children that never finalize via the
            # horizon timeout.
            pass
        except ConcurrencyError:
            # Child concurrent mutation. Cascade is opportunistic; the
            # next round of replay will re-try if needed.
            pass
