"""Optimistic finalization lease for P2/P3 coordination.

P2 (:class:`CascadeProjector`) and P3 (:class:`FinalizationHorizonProjector`)
both reach for the same outcome: publishing ``InternalFinalize`` for an
aggregate at the right moment. Without a lease, two well-meaning
projectors can race — both succeed (the second one's duplicate
``CommandRejected`` is swallowed, but the redundant work hits the
event log and the audit trail).

The lease is a single ``SET NX EX`` against Valkey keyed on
``heddle:events:horizon:{aggregate_type}:{aggregate_id}``. Whichever
projector claims it first publishes the command; the loser logs +
skips. The lease is **not** released in the normal flow — it expires
after :data:`FINALIZATION_LEASE_TTL_SECONDS` (default 30s). Releasing
on success would invite re-claim races with horizon retries.

Crash recovery: an in-flight finalization aborted mid-publish leaves
the lease held for at most the TTL. The next P3 cycle re-claims and
publishes. No manual cleanup.

The lease is for **audit clarity**, not state correctness — the
receiving aggregate's ``apply_internal_finalized`` already no-ops on
an already-finalized aggregate, and the dedup buffer (with snapshot)
catches duplicate command_ids. The lease keeps the event log from
carrying redundant "framework:cascade then framework:horizon both
finalized me" pairs.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from heddle.contrib.events.sli import get_recorder, time_observation

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from heddle.core.kvstore import KeyValueStore

_log = logging.getLogger(__name__)


FINALIZATION_LEASE_TTL_SECONDS: int = 30
"""Seconds the lease is held before automatic expiry. Long enough for
a projector's ``handle()`` round-trip to commit the InternalFinalized
event; short enough that a crashed projector's slot recovers within
one P3 horizon cycle."""


def lease_key(aggregate_type: str, aggregate_id: str) -> str:
    """The Valkey key used for one aggregate's finalization lease."""
    return f"heddle:events:horizon:{aggregate_type}:{aggregate_id}"


@asynccontextmanager
async def finalization_lease(
    kv: "KeyValueStore",
    aggregate_type: str,
    aggregate_id: str,
    projector_name: str,
    ttl_seconds: int = FINALIZATION_LEASE_TTL_SECONDS,
) -> "AsyncGenerator[bool, None]":
    """Try to claim the finalization lease for one aggregate.

    Yields ``True`` when the lease was claimed (caller MUST proceed to
    publish ``InternalFinalize``), ``False`` when preempted (caller
    MUST log + skip).

    Example::

        async with finalization_lease(
            kv, "Job", "39174-004", "framework:cascade"
        ) as claimed:
            if not claimed:
                return  # preempted by another projector
            await command_handler.handle(InternalFinalize_cmd)

    The context manager does not release the lease on exit by design —
    see module docstring.
    """
    key = lease_key(aggregate_type, aggregate_id)
    value = f"{projector_name}:{uuid.uuid4()}"
    with time_observation() as elapsed:
        claimed = await kv.set_if_not_exists(key, value, ttl_seconds=ttl_seconds)
    get_recorder().observe_lease_acquisition(
        aggregate_type=aggregate_type,
        projector_name=projector_name,
        outcome="claimed" if claimed else "preempted",
        duration_seconds=elapsed(),
    )
    if not claimed:
        holder = await kv.get(key)
        _log.info(
            "finalization lease for %s:%s preempted; holder=%s",
            aggregate_type, aggregate_id, holder,
        )
    yield claimed
