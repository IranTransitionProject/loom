"""RejectionLog ABC, in-memory implementation, and the ``RejectionEnvelope``.

When a command is rejected by aggregate validation, the rejection
envelope is appended to :class:`RejectionLog`. Distinct from
:class:`heddle.contrib.events.event_log.EventLog`:

- No CAS — rejections aren't versioned per-aggregate.
- No per-aggregate ordering — append-order is all that's defined.
- A separate Sprint 3 ``JetStreamRejectionLog`` will use
  ``HEDDLE_REJECTIONS_{TYPE}`` streams so rejections can be queried
  independently and don't pollute the events stream.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from datetime import datetime  # noqa: TC003 - Pydantic field type, used at runtime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from heddle.contrib.events.envelopes import (
    CommandMessage,  # noqa: TC001 - Pydantic field type, used at runtime
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _uuid7() -> str:
    # Lazy import — keep bare-install path working.
    import uuid_utils

    return str(uuid_utils.uuid7())


class RejectionEnvelope(BaseModel):
    """The envelope written to :class:`RejectionLog` when a command is rejected."""

    rejection_id: str = Field(
        default_factory=_uuid7,
        description="UUIDv7. Globally unique rejection identifier.",
    )
    command: CommandMessage = Field(
        ..., description="The full command that was rejected."
    )
    reason: str = Field(
        ...,
        description=(
            "Machine-readable rejection reason — typically the "
            "uppercase code from CommandRejected.reason."
        ),
    )
    detail: str = Field(
        default="",
        description="Human-readable diagnostic detail. May be empty.",
    )
    rejected_at: datetime = Field(
        ..., description="When the rejection was recorded."
    )


class RejectionLog(ABC):
    """Append-only audit stream of rejected commands."""

    @abstractmethod
    async def append(self, envelope: RejectionEnvelope) -> None:
        """Append a rejection envelope to the log."""

    @abstractmethod
    def load(
        self, aggregate_type: str, aggregate_id: str | None = None
    ) -> AsyncIterator[RejectionEnvelope]:
        """Stream rejections in append-order, optionally filtered by id.

        There is no per-aggregate ordering for rejections; consumers
        get append-order on the underlying log.
        """


class InMemoryRejectionLog(RejectionLog):
    """Thread-safe in-memory RejectionLog for tests."""

    def __init__(self) -> None:
        self._rejections: list[RejectionEnvelope] = []
        self._lock = threading.Lock()

    async def append(self, envelope: RejectionEnvelope) -> None:
        """Append a rejection envelope under the internal lock."""
        with self._lock:
            self._rejections.append(envelope)

    async def load(
        self, aggregate_type: str, aggregate_id: str | None = None
    ) -> AsyncIterator[RejectionEnvelope]:
        """Yield rejections matching the optional aggregate filter, in append-order."""
        with self._lock:
            snapshot = list(self._rejections)
        for rej in snapshot:
            if rej.command.aggregate_type != aggregate_type:
                continue
            if (
                aggregate_id is not None
                and rej.command.aggregate_id != aggregate_id
            ):
                continue
            yield rej
