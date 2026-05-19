"""Hybrid mark_processed publish/subscribe for cross-process dedup.

Per Sprint 2 feedback R7 and Sprint 3 brief T4. After a successful
``CommandHandler.handle()`` the in-memory aggregate's dedup buffer is
updated synchronously (same-process correctness, preserved from
Sprint 2). A ``mark_processed`` event is then published to
``heddle.dedup.{aggregate_type}.{aggregate_id}`` so that *other*
CommandHandler instances which have the aggregate cached can update
their buffer too.

NATS core (not JetStream) is the right transport: these events are
optimization-only — they don't need durability, and a dropped one
just means a fallback through the snapshot path on the next replay.

Two implementations ship:

- :class:`NullDedupPublisher` — no-op. Default for single-process tests.
- :class:`NatsDedupPublisher` — production. Publishes to NATS core.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nats.aio.client import Client as NATSClient


def dedup_subject(aggregate_type: str, aggregate_id: str) -> str:
    """The NATS subject for ``mark_processed`` events of one aggregate."""
    return f"heddle.dedup.{aggregate_type}.{aggregate_id}"


class DedupPublisher(ABC):
    """Publishes ``mark_processed`` events for cross-process dedup."""

    @abstractmethod
    async def publish(
        self,
        aggregate_type: str,
        aggregate_id: str,
        command_id: str,
    ) -> None:
        """Announce that ``command_id`` was processed for the aggregate."""


class NullDedupPublisher(DedupPublisher):
    """No-op. For tests and single-process deployments."""

    async def publish(
        self,
        aggregate_type: str,
        aggregate_id: str,
        command_id: str,
    ) -> None:
        """No-op; the dedup announcement is dropped."""
        return


class NatsDedupPublisher(DedupPublisher):
    """Publishes ``mark_processed`` events to NATS core."""

    def __init__(self, nc: NATSClient) -> None:
        self._nc = nc

    async def publish(
        self,
        aggregate_type: str,
        aggregate_id: str,
        command_id: str,
    ) -> None:
        """Publish a JSON ``mark_processed`` message to the dedup subject."""
        payload = json.dumps(
            {
                "command_id": command_id,
                "marked_at": datetime.now(UTC).isoformat(),
            }
        ).encode()
        await self._nc.publish(dedup_subject(aggregate_type, aggregate_id), payload)
