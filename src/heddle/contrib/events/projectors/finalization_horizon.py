"""P3: FinalizationHorizonProjector — STUB.

Per v7 §5.1 and Sprint 2 plan: P3's atomicity-window mechanism
requires a Valkey lease, which arrives in Sprint 3. Sprint 2 ships
ONLY the class signature so callers can::

    from heddle.contrib.events.projectors.finalization_horizon import (
        FinalizationHorizonProjector,
    )

without an ImportError, and so the package ``__init__`` re-export
is honest.

Until Sprint 3, this projector is a no-op — :meth:`project` returns
immediately without consuming the event.

Sprint 3 will:

- Take a Valkey / redis-py ``KeyValueStore`` in ``__init__``.
- Maintain per-aggregate-type horizon timers.
- Emit ``InternalFinalize`` commands with
  ``issued_by='framework:horizon'`` for aggregates that pass their
  horizon without finalising organically.
- Coordinate with P2 via the atomicity-window mechanism (Valkey
  lease) to avoid double-finalisation races.

DO NOT use this projector in production paths until Sprint 3 lands.
The end-to-end demo scenario test in Sprint 2 wires P1 + P2 only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from heddle.contrib.events.dispatcher import Projector

if TYPE_CHECKING:
    from heddle.contrib.events.envelopes import EventEnvelope


class FinalizationHorizonProjector(Projector):
    """P3 STUB. See module docstring for the Sprint 3 plan."""

    async def project(self, envelope: EventEnvelope) -> None:
        """No-op. Real implementation in Sprint 3."""
        return
