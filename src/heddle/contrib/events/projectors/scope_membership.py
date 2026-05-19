"""P1: ScopeMembershipProjector — root → child membership view.

Per v7 §5.1. Tracks which child aggregates belong to which root.
In-memory in Sprint 2; Sprint 3 migrates to a KeyValueStore-backed
view. Consumed by P2 (:class:`CascadeProjector`) to know which
children to cascade-finalize.

Watches events of every :class:`RootAggregate` type. For each event
whose payload carries the reserved ``_child_membership`` key, the
projector updates its in-memory membership map.

Sprint 4a concrete aggregates emit child-membership signals via this
payload convention from inside ``apply_*()`` handlers.
"""

from __future__ import annotations

import threading

from heddle.contrib.events.dispatcher import Projector
from heddle.contrib.events.envelopes import EventEnvelope
from heddle.contrib.events.registry import is_root_type


CHILD_MEMBERSHIP_KEY = "_child_membership"
"""Reserved payload key.

Aggregate ``apply_*()`` handlers that add or remove children can
include::

    {"_child_membership": {"add": [{"type": "...", "id": "..."}],
                           "remove": [{"type": "...", "id": "..."}]}}

in the event payload. P1 reads this key and updates its view.
Underscore-prefixed so domain events don't collide.
"""


class ScopeMembershipProjector(Projector):
    """In-memory membership view for root → child relationships."""

    def __init__(self) -> None:
        # (root_type, root_id) -> child_type -> {child_id, ...}
        self._membership: dict[
            tuple[str, str], dict[str, set[str]]
        ] = {}
        self._lock = threading.Lock()

    async def project(self, envelope: EventEnvelope) -> None:
        if not is_root_type(envelope.aggregate_type):
            return
        info = envelope.payload.get(CHILD_MEMBERSHIP_KEY)
        if not info:
            return
        root_key = (envelope.aggregate_type, envelope.aggregate_id)
        with self._lock:
            ms = self._membership.setdefault(root_key, {})
            for add in info.get("add", []):
                ms.setdefault(add["type"], set()).add(add["id"])
            for rm in info.get("remove", []):
                if rm["type"] in ms:
                    ms[rm["type"]].discard(rm["id"])

    def children_of(
        self, root_type: str, root_id: str, child_type: str
    ) -> frozenset[str]:
        """Read-only view of children of a specific type."""
        with self._lock:
            return frozenset(
                self._membership.get((root_type, root_id), {}).get(
                    child_type, set()
                )
            )

    def all_children_of(
        self, root_type: str, root_id: str
    ) -> dict[str, frozenset[str]]:
        """Snapshot-style view of every child type and its members."""
        with self._lock:
            ms = self._membership.get((root_type, root_id), {})
            return {k: frozenset(v) for k, v in ms.items()}
