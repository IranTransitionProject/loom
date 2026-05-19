"""EventDispatcher — fans events out to projectors.

Per v7 §4.9:

- :meth:`EventLog.subscribe` yields an async stream of events for an
  aggregate type.
- The dispatcher iterates the stream; for each event, it invokes
  every registered projector's :meth:`Projector.project` in
  registration order, serially.

Sprint 2 ships serial fan-out as the contract. Sprint 3 may add
parallel-fan-out for projectors that don't share state, but serial
is the documented behaviour — projectors must NOT assume
parallel-safety.

A projector raising an exception is logged but does not stop
subsequent projectors. Projectors are responsible for idempotency
(the dispatcher may re-deliver an event after a crash) and for
propagating ``correlation_id`` on any commands or events they emit.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from heddle.contrib.events.envelopes import EventEnvelope
    from heddle.contrib.events.event_log import EventLog


_log = logging.getLogger(__name__)


class Projector(ABC):
    """A consumer of :class:`EventEnvelope` from the dispatcher.

    Projectors are NOT aggregates — they don't have state subject to
    ``apply()`` discipline. Their job is to react to events:
    maintain membership views (P1), trigger cascade commands (P2),
    enforce finalization horizons (P3).

    :meth:`project` MUST be idempotent. Projectors that emit
    commands or events MUST propagate the source event's
    ``command_id`` / ``correlation_id`` for trace continuity.
    """

    @abstractmethod
    async def project(self, envelope: EventEnvelope) -> None:
        """Consume a single event envelope. Implementations MUST be idempotent."""


class EventDispatcher:
    """Subscribe to an :class:`EventLog` and fan out to projectors."""

    def __init__(self, event_log: EventLog) -> None:
        self._event_log = event_log
        self._projectors: list[Projector] = []
        self._running: dict[str, asyncio.Task[None]] = {}

    def register(self, projector: Projector) -> None:
        """Add a projector.

        Order matters — serial invocation in registration order.
        Re-registering the same instance is idempotent.
        """
        if projector not in self._projectors:
            self._projectors.append(projector)

    async def start(self, aggregate_type: str) -> None:
        """Begin dispatching events for an aggregate type.

        Idempotent: calling twice for the same type is a no-op.
        Sprint 2 spawns one ``asyncio.Task`` per type; Sprint 3 may
        swap to a JetStream durable consumer per type.
        """
        if aggregate_type in self._running:
            return
        task = asyncio.create_task(self._run(aggregate_type))
        self._running[aggregate_type] = task

    async def stop(self) -> None:
        """Cancel all dispatcher tasks and wait for them to finish."""
        for task in self._running.values():
            task.cancel()
        await asyncio.gather(*self._running.values(), return_exceptions=True)
        self._running.clear()

    async def _run(self, aggregate_type: str) -> None:
        try:
            async for envelope in self._event_log.subscribe(aggregate_type):
                for projector in self._projectors:
                    try:
                        await projector.project(envelope)
                    except Exception:
                        _log.exception(
                            "projector %s failed on event %s; continuing",
                            type(projector).__name__,
                            envelope.event_id,
                        )
        except asyncio.CancelledError:
            raise
