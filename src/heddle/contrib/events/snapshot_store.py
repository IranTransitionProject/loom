"""SnapshotStore — adapts :class:`KeyValueStore` to aggregate snapshots.

Snapshots are taken every :data:`SNAPSHOT_EVERY_N` events (default 100)
on the cached aggregate; time-based triggering is recorded as a knob
but not yet implemented (Sprint 3 defers it).

Stored as a JSON serialization of ``aggregate.to_snapshot()``.

Key format
----------
- Snapshot blob: ``heddle:events:snapshot:{aggregate_type}:{aggregate_id}``
- Snapshot version: ``heddle:events:snapshot:version:{aggregate_type}:{aggregate_id}``

The version key lets ``CommandHandler._load_or_create`` replay only
events with ``aggregate_version > snapshot_version``.

Buffer invariant
----------------
The dedup buffer is **never** reconstructed from event replay (v7
§4.5). Snapshot is the only persistence path. A pure-replay rebuild
(no snapshot) starts with an empty buffer; this is correct per v7 and
captured in the Sprint 2 → Sprint 3 boundary table.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from heddle.contrib.events.aggregate import Aggregate
    from heddle.core.kvstore import KeyValueStore


SNAPSHOT_EVERY_N: int = 100
"""Take a snapshot every N events. Tuneable per aggregate type via
``Aggregate.SNAPSHOT_EVERY_N`` ClassVar in Sprint 4a domain code."""

SNAPSHOT_EVERY_T_SECONDS: float = 300.0
"""Take a snapshot after T seconds since the last one, regardless of
event count. **Recorded but not enforced in Sprint 3** — count-based
only. Time-based triggering is a follow-up."""


def _snapshot_key(aggregate_type: str, aggregate_id: str) -> str:
    return f"heddle:events:snapshot:{aggregate_type}:{aggregate_id}"


def _version_key(aggregate_type: str, aggregate_id: str) -> str:
    return f"heddle:events:snapshot:version:{aggregate_type}:{aggregate_id}"


class SnapshotStore:
    """Aggregate snapshot persistence over a :class:`KeyValueStore`."""

    def __init__(self, kv: "KeyValueStore") -> None:
        self._kv = kv

    async def load(
        self, cls: "type[Aggregate]", aggregate_id: str
    ) -> "Aggregate | None":
        """Restore an aggregate from snapshot, or ``None`` if absent."""
        raw = await self._kv.get(_snapshot_key(cls.aggregate_type, aggregate_id))
        if raw is None:
            return None
        data = json.loads(raw)
        return cls.from_snapshot(data)

    async def save(self, aggregate: "Aggregate") -> None:
        """Persist a snapshot. Idempotent at a given aggregate_version."""
        data = aggregate.to_snapshot()
        await self._kv.set(
            _snapshot_key(aggregate.aggregate_type, aggregate.aggregate_id),
            json.dumps(data),
        )
        await self._kv.set(
            _version_key(aggregate.aggregate_type, aggregate.aggregate_id),
            str(aggregate.aggregate_version),
        )

    async def version(self, aggregate_type: str, aggregate_id: str) -> int:
        """Return aggregate_version at last snapshot, or 0 if none."""
        raw = await self._kv.get(_version_key(aggregate_type, aggregate_id))
        return int(raw) if raw else 0
