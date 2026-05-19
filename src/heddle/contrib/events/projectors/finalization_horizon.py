"""P3: FinalizationHorizonProjector.

Per v7 §5.1. Emits ``InternalFinalize`` commands for aggregates that
pass their horizon timer without finalising organically.

Horizon timers are per-aggregate, started when P3 observes the
aggregate's first event (a proxy for "the aggregate is active") and
cancelled when ``InternalFinalized`` is observed.

Coordinates with P2 via the optimistic lease
(:func:`heddle.contrib.events.lease.finalization_lease`). Either P2
(cascade) or P3 (horizon) may be the one that actually finalises;
whichever claims the lease first wins, the loser logs and skips.

Per-aggregate horizon configuration: each
:class:`heddle.contrib.events.aggregate.IntervalAggregate` subclass
may override :data:`IntervalAggregate.HORIZON_TIMEOUT_SECONDS` as a
ClassVar. Sprint 3 ships a 24-hour default; Sprint 4a domain
aggregates (e.g. ``OperatorJobSession``) set their own much shorter
values.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from heddle.contrib.events.dispatcher import Projector
from heddle.contrib.events.envelopes import CommandMessage, CommandMetadata
from heddle.contrib.events.errors import CommandRejected, ConcurrencyError
from heddle.contrib.events.lease import finalization_lease
from heddle.contrib.events.registry import get_aggregate_class

if TYPE_CHECKING:
    from heddle.contrib.events.command_handler import CommandHandler
    from heddle.contrib.events.envelopes import EventEnvelope
    from heddle.core.kvstore import KeyValueStore


_log = logging.getLogger(__name__)


HORIZON_ISSUED_BY = "framework:horizon"

DEFAULT_HORIZON_TIMEOUT_SECONDS: float = 24 * 60 * 60.0
"""24 hours. Concrete IntervalAggregate subclasses override via the
``HORIZON_TIMEOUT_SECONDS`` ClassVar."""


def _is_interval_aggregate_class(cls: type) -> bool:
    from heddle.contrib.events.aggregate import IntervalAggregate

    return issubclass(cls, IntervalAggregate)


def _horizon_timeout_for(cls: type) -> float:
    return float(getattr(cls, "HORIZON_TIMEOUT_SECONDS", DEFAULT_HORIZON_TIMEOUT_SECONDS))


class FinalizationHorizonProjector(Projector):
    """Emit ``InternalFinalize`` for aggregates past their horizon.

    Stateless across restarts — timers live in process memory. After
    a restart the next event on each aggregate re-arms the timer.
    """

    def __init__(
        self,
        kv: "KeyValueStore",
        command_handler: "CommandHandler",
    ) -> None:
        self._kv = kv
        self._handler = command_handler
        self._timers: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    async def project(self, envelope: "EventEnvelope") -> None:
        """Start/cancel horizon timers based on observed events.

        - ``InternalFinalized``: cancel the timer (the aggregate
          finalised organically before horizon).
        - Any other event on an :class:`IntervalAggregate`-typed
          aggregate: arm the timer if not already running.
        """
        key = (envelope.aggregate_type, envelope.aggregate_id)

        if envelope.event_type == "InternalFinalized":
            await self._cancel_timer(key)
            return

        try:
            cls = get_aggregate_class(envelope.aggregate_type)
        except KeyError:
            return
        if not _is_interval_aggregate_class(cls):
            return

        async with self._lock:
            existing = self._timers.get(key)
            if existing is not None and not existing.done():
                return
            timeout = _horizon_timeout_for(cls)
            self._timers[key] = asyncio.create_task(self._fire_horizon(key, timeout))

    async def _cancel_timer(self, key: tuple[str, str]) -> None:
        async with self._lock:
            task = self._timers.pop(key, None)
        if task is not None and not task.done():
            task.cancel()

    async def _fire_horizon(self, key: tuple[str, str], timeout: float) -> None:
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            return
        aggregate_type, aggregate_id = key

        async with finalization_lease(
            self._kv, aggregate_type, aggregate_id, HORIZON_ISSUED_BY
        ) as claimed:
            if not claimed:
                _log.info(
                    "horizon fire preempted by lease holder for %s:%s",
                    aggregate_type, aggregate_id,
                )
                return

            cmd = CommandMessage(
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                command_type="InternalFinalize",
                payload={},
                metadata=CommandMetadata(
                    correlation_id=None,
                    issued_by=HORIZON_ISSUED_BY,
                ),
                issued_at=datetime.now(UTC),
                expected_aggregate_version=None,
            )
            try:
                await self._handler.handle(cmd)
            except (CommandRejected, ConcurrencyError) as exc:
                _log.info(
                    "horizon InternalFinalize rejected for %s:%s — %s",
                    aggregate_type, aggregate_id, exc,
                )

    async def shutdown(self) -> None:
        """Cancel all running horizon timers. Call on dispatcher stop."""
        async with self._lock:
            tasks = list(self._timers.values())
            self._timers.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
