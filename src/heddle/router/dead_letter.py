"""
Dead-letter consumer — stores and replays unroutable/rate-limited tasks.

Subscribes to ``heddle.tasks.dead_letter`` and maintains a bounded in-memory
list of dead-letter entries. Each entry captures the original task data,
a timestamp, and the reason for dead-lettering.

The consumer can run standalone (via CLI ``heddle dead-letter monitor``) or
be embedded in the Workshop app for UI-based inspection and replay.

Replay re-publishes a dead-letter task back to ``heddle.tasks.incoming``
so the router can attempt routing again.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from heddle.core.actor import BaseActor

if TYPE_CHECKING:
    from heddle.bus.base import MessageBus

logger = structlog.get_logger()

# Subject where replayed tasks are re-published for the router to pick up.
INCOMING_SUBJECT = "heddle.tasks.incoming"


class ReplayRecord:
    """Record of a replayed dead-letter entry."""

    __slots__ = ("entry_id", "original_reason", "replayed_at", "task_id", "worker_type")

    def __init__(
        self,
        entry_id: str,
        task_id: str | None = None,
        worker_type: str | None = None,
        original_reason: str = "",
    ) -> None:
        self.entry_id = entry_id
        self.task_id = task_id
        self.worker_type = worker_type
        self.original_reason = original_reason
        self.replayed_at = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict for API/template consumption."""
        return {
            "entry_id": self.entry_id,
            "task_id": self.task_id,
            "worker_type": self.worker_type,
            "original_reason": self.original_reason,
            "replayed_at": self.replayed_at,
        }


class DeadLetterEntry:
    """A single dead-letter entry with metadata."""

    __slots__ = ("id", "original_task", "reason", "task_id", "timestamp", "worker_type")

    def __init__(
        self,
        original_task: dict[str, Any],
        reason: str,
        task_id: str | None = None,
        worker_type: str | None = None,
    ) -> None:
        self.id = str(uuid.uuid4())
        self.timestamp = datetime.now(UTC).isoformat()
        self.reason = reason
        self.task_id = task_id
        self.worker_type = worker_type
        self.original_task = original_task

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict for API/template consumption."""
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "reason": self.reason,
            "task_id": self.task_id,
            "worker_type": self.worker_type,
            "original_task": self.original_task,
        }


class DeadLetterConsumer(BaseActor):
    """Actor that consumes dead-letter messages and stores them in memory.

    Subscribes to ``heddle.tasks.dead_letter`` and maintains a bounded list
    of entries (most recent first). When the list exceeds ``max_size``,
    the oldest entries are evicted.

    Can be used standalone (via ``run()``) or embedded — call ``store()``
    directly to add entries without a subscription (used by the Workshop).

    Args:
        actor_id: Unique identifier for this actor instance.
        max_size: Maximum number of entries to retain (default 1000).
        nats_url: NATS server URL (used only when no ``bus`` is provided).
        bus: Optional injected message bus (for testing or embedding).
    """

    def __init__(
        self,
        actor_id: str = "dead-letter-consumer",
        max_size: int = 1000,
        nats_url: str = "nats://nats:4222",
        *,
        bus: MessageBus | None = None,
    ) -> None:
        super().__init__(actor_id=actor_id, nats_url=nats_url, bus=bus)
        self.max_size = max_size
        self._entries: list[DeadLetterEntry] = []
        self._replay_log: list[ReplayRecord] = []

    def set_bus(self, bus: MessageBus) -> None:
        """Replace the underlying message bus.

        Used by the Workshop lifespan to swap in a connected ``NATSBus``
        after construction.  The consumer is built outside the FastAPI
        lifespan so route handlers can close over it, but the bus is
        only known once the async lifespan starts.  Calling this from
        inside :meth:`run` after :meth:`subscribe` is undefined — only
        use it before subscription.
        """
        self._bus = bus

    async def handle_message(self, data: dict[str, Any]) -> None:
        """Process a dead-letter message from the bus.

        Extracts reason, task_id, and worker_type from the dead-letter
        envelope (as published by ``TaskRouter._dead_letter``), stores
        the entry, and logs the event.
        """
        reason = data.get("reason", "unknown")
        task_id = data.get("task_id")
        worker_type = data.get("worker_type")
        original_task = data.get("original_task", data)

        self.store(original_task, reason, task_id=task_id, worker_type=worker_type)

    def store(
        self,
        original_task: dict[str, Any],
        reason: str,
        *,
        task_id: str | None = None,
        worker_type: str | None = None,
    ) -> DeadLetterEntry:
        """Store a dead-letter entry.

        Inserts at the front of the list (most recent first) and evicts
        the oldest entry if ``max_size`` is exceeded.

        Returns the created entry.
        """
        entry = DeadLetterEntry(
            original_task=original_task,
            reason=reason,
            task_id=task_id,
            worker_type=worker_type,
        )

        # Insert at front (most recent first).
        self._entries.insert(0, entry)

        # Evict oldest if over capacity.
        if len(self._entries) > self.max_size:
            self._entries = self._entries[: self.max_size]

        logger.info(
            "dead_letter.received",
            task_id=task_id,
            worker_type=worker_type,
            reason=reason,
            total_entries=len(self._entries),
        )

        return entry

    def list_entries(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """Return a page of dead-letter entries (most recent first).

        Args:
            limit: Maximum number of entries to return.
            offset: Number of entries to skip from the start.

        Returns:
            List of entry dicts.
        """
        sliced = self._entries[offset : offset + limit]
        return [e.to_dict() for e in sliced]

    def count(self) -> int:
        """Return the total number of stored entries."""
        return len(self._entries)

    def clear(self) -> None:
        """Remove all stored entries."""
        self._entries.clear()
        logger.info("dead_letter.cleared")

    async def replay(self, entry_id: str, bus: MessageBus) -> bool:
        """Re-publish a dead-letter task back to ``heddle.tasks.incoming``.

        Finds the entry by ID, publishes its ``original_task`` to the
        incoming subject, removes it from the stored list, and records
        the replay in the audit log.

        Args:
            entry_id: The UUID of the entry to replay.
            bus: The message bus to publish the replayed task on.

        Returns:
            True if the entry was found and replayed, False otherwise.
        """
        for i, entry in enumerate(self._entries):
            if entry.id == entry_id:
                await bus.publish(INCOMING_SUBJECT, entry.original_task)
                self._entries.pop(i)

                # Record in audit log (bounded FIFO — Invariant 12).
                record = ReplayRecord(
                    entry_id=entry_id,
                    task_id=entry.task_id,
                    worker_type=entry.worker_type,
                    original_reason=entry.reason,
                )
                self._replay_log.append(record)
                if len(self._replay_log) > self.max_size:
                    # Oldest record is at the front; keep the most recent max_size.
                    self._replay_log = self._replay_log[-self.max_size :]

                logger.info(
                    "dead_letter.replayed",
                    entry_id=entry_id,
                    task_id=entry.task_id,
                    worker_type=entry.worker_type,
                )
                return True
        return False

    def replay_log(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the replay audit log (most recent first).

        Args:
            limit: Maximum number of records to return.

        Returns:
            List of replay record dicts.
        """
        # Most recent last in list, so reverse for display
        return [r.to_dict() for r in reversed(self._replay_log[-limit:])]

    def replay_count(self) -> int:
        """Return the total number of replayed entries."""
        return len(self._replay_log)
