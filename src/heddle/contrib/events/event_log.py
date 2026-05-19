"""EventLog ABC and in-memory implementation.

EventLog is the per-aggregate-type append-only event store (v7 §4.6).
Sprint 3 ships ``JetStreamEventLog`` as the production implementation;
Sprint 2 ships :class:`InMemoryEventLog` for tests and the
framework→app coherence guard.

Contract:

- ``append(envelope, expected_version)`` — CAS append. ``None`` skips
  the version check (creation path, used by PF observers).
- ``load(aggregate_type, aggregate_id, from_version=0)`` — async
  stream of envelopes in aggregate_version order, with
  ``aggregate_version > from_version``.
- ``subscribe(aggregate_type)`` — async method returning an
  :class:`AsyncIterator` of newly-appended events. The underlying
  subscription is registered BEFORE this method returns; callers may
  publish to ``aggregate_type`` immediately after ``await subscribe(...)``
  and be sure those events will be delivered. Iteration is forever
  unless cancelled.
"""

from __future__ import annotations

import asyncio
import threading
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from heddle.contrib.events.errors import ConcurrencyError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from heddle.contrib.events.envelopes import EventEnvelope


class EventLog(ABC):
    """Per-aggregate-type append-only event store."""

    @abstractmethod
    async def append(self, envelope: EventEnvelope, expected_version: int | None) -> None:
        """Append an event with optimistic concurrency control.

        ``expected_version`` semantics:

        - ``None`` — no check. Used by PF observers that create an
          aggregate from existing PF state and trust their source.
        - ``N`` — current persisted version MUST be exactly ``N``;
          otherwise :class:`ConcurrencyError`.

        Regardless of ``expected_version``, ``envelope.aggregate_version``
        MUST equal current_version + 1. The two checks compose: passing
        ``expected_version=None`` with an out-of-order envelope still
        fails the monotonicity check.
        """

    @abstractmethod
    def load(
        self,
        aggregate_type: str,
        aggregate_id: str,
        from_version: int = 0,
    ) -> AsyncIterator[EventEnvelope]:
        """Stream events for an aggregate, ordered by aggregate_version."""

    @abstractmethod
    async def subscribe(self, aggregate_type: str) -> AsyncIterator[EventEnvelope]:
        """Subscribe to live events for an aggregate type.

        Returns an async iterator that yields envelopes as they're
        appended. The underlying subscription is ALREADY REGISTERED
        with the log by the time this method returns; callers do NOT
        need to await the first yield to be sure registration is
        complete. Iteration is forever unless the consumer cancels.
        Cancellation cleans up subscriber state.
        """


class InMemoryEventLog(EventLog):
    """Thread-safe in-memory EventLog for tests.

    Stores events in a dict keyed by ``(aggregate_type, aggregate_id)``.
    Subscribe broadcasts via a per-subscriber ``asyncio.Queue``.

    NOT suitable for production — process-local, no persistence.
    ``JetStreamEventLog`` (Sprint 3) is the real one.
    """

    def __init__(self) -> None:
        self._events: dict[tuple[str, str], list[EventEnvelope]] = {}
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[asyncio.Queue[EventEnvelope]]] = {}

    async def append(self, envelope: EventEnvelope, expected_version: int | None) -> None:
        """Append an envelope with the CAS+monotonicity contract from the ABC."""
        key = (envelope.aggregate_type, envelope.aggregate_id)
        with self._lock:
            current = self._events.get(key, [])
            current_version = current[-1].aggregate_version if current else 0
            if expected_version is not None and expected_version != current_version:
                raise ConcurrencyError(
                    f"append for {envelope.aggregate_type}:"
                    f"{envelope.aggregate_id} expected_version="
                    f"{expected_version} but current_version="
                    f"{current_version}"
                )
            if envelope.aggregate_version != current_version + 1:
                raise ConcurrencyError(
                    f"envelope aggregate_version="
                    f"{envelope.aggregate_version} does not follow "
                    f"current_version={current_version}"
                )
            self._events.setdefault(key, []).append(envelope)
            subs = list(self._subscribers.get(envelope.aggregate_type, []))

        # Broadcast outside the lock — never await under it.
        for q in subs:
            await q.put(envelope)

    async def load(
        self,
        aggregate_type: str,
        aggregate_id: str,
        from_version: int = 0,
    ) -> AsyncIterator[EventEnvelope]:
        """Yield stored events for ``(aggregate_type, aggregate_id)`` in order."""
        key = (aggregate_type, aggregate_id)
        with self._lock:
            # Snapshot to avoid yielding under the lock.
            events = list(self._events.get(key, []))
        for ev in events:
            if ev.aggregate_version > from_version:
                yield ev

    async def subscribe(self, aggregate_type: str) -> AsyncIterator[EventEnvelope]:
        """Register a subscription then return an iterator over new events.

        Registration is synchronous w.r.t. this method's return — the
        caller's queue is in ``self._subscribers[aggregate_type]`` by the
        time ``await subscribe(...)`` resolves, so any subsequent
        ``append()`` is guaranteed delivery.
        """
        q: asyncio.Queue[EventEnvelope] = asyncio.Queue()
        with self._lock:
            self._subscribers.setdefault(aggregate_type, []).append(q)
        return self._iterate_subscription(q, aggregate_type)

    async def _iterate_subscription(
        self,
        q: asyncio.Queue[EventEnvelope],
        aggregate_type: str,
    ) -> AsyncIterator[EventEnvelope]:
        try:
            while True:
                envelope = await q.get()
                yield envelope
        finally:
            with self._lock:
                subs = self._subscribers.get(aggregate_type, [])
                if q in subs:
                    subs.remove(q)
