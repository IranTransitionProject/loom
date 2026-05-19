"""Aggregate base classes for heddle.contrib.events.

See ``heddle-contrib-events-m2-architecture-v7.md`` §4.5 for the
design. Three classes:

- :class:`Aggregate`: abstract base. Identity + version + dedup buffer +
  apply() discipline.
- :class:`IntervalAggregate`: adds a phase machine
  (``created`` → ``active`` → ``finalized``) and the
  ``apply_internal_finalized`` handler.
- :class:`RootAggregate`: adds a child registry; the cascade trigger
  for P2 reads from this registry.

Concrete aggregates (Sprint 4a) subclass :class:`IntervalAggregate` or
:class:`RootAggregate`, register via
``@register_aggregate("Type")``, and define:

- ``handle_<command_type_snake>(payload, metadata) -> (event_type, event_payload)``
- ``apply_<event_type_snake>(payload, metadata) -> None``
"""

from __future__ import annotations

import re
from abc import ABC
from collections import deque
from typing import TYPE_CHECKING, Any, ClassVar

from heddle.contrib.events.errors import (
    AggregateInvariantError,
    CorruptAggregateAlert,
    UnknownEventVersionError,
)
from heddle.contrib.events.issuer_conventions import is_framework_issuer

if TYPE_CHECKING:
    from heddle.contrib.events.envelopes import EventEnvelope, EventMetadata


PROCESSED_COMMAND_RING_SIZE: int = 512
"""Per-aggregate command-id dedup ring size.

~20x headroom over peak observed burst (worst-realistic Operation
chaotic-shift workload is ~25-250 commands/aggregate/active window).
Snapshot-only persistence; never reconstructed from event replay --
see v7 §4.5 on ring-buffer rebuild semantics.
"""

FRAMEWORK_ONLY_EVENT_TYPES: frozenset[str] = frozenset({"InternalFinalized"})
"""Event types whose ``issued_by`` MUST start with ``framework:``.

Enforced by :meth:`Aggregate.apply` as the application-layer backstop
for the Sprint 3 NATS publish ACL. See v7 §4.5 belt-and-suspenders note.
"""


_FIRST_CAP_RE = re.compile(r"(.)([A-Z][a-z]+)")
_ALL_CAP_RE = re.compile(r"([a-z0-9])([A-Z])")


def snake_case(camel: str) -> str:
    """Convert CamelCase event/command types to ``apply_*`` / ``handle_*`` names.

    Examples::

        JobClockedIn         -> job_clocked_in
        InternalFinalized    -> internal_finalized
        JobShippedFromPF     -> job_shipped_from_pf
        OperationLaborRecorded -> operation_labor_recorded
    """
    s1 = _FIRST_CAP_RE.sub(r"\1_\2", camel)
    return _ALL_CAP_RE.sub(r"\1_\2", s1).lower()


class Aggregate(ABC):  # noqa: B024 - construction blocked via runtime check in __init__
    """Abstract aggregate base.

    Concrete subclasses MUST set the ``aggregate_type`` ClassVar (via
    ``@register_aggregate("Type")``) and MUST implement ``apply_*()``
    methods for each event type they emit.

    State mutation discipline:

    - ``apply()`` returns nothing; it mutates ``self``.
    - ``apply()`` MUST be deterministic — same event sequence yields
      the same end state across replays.
    - ``apply()`` MUST NOT perform I/O or call out to bus/transport.
    - ``handle_*()`` methods produce events but do NOT mutate state;
      ``apply()`` is invoked separately by :class:`CommandHandler`
      after the event is durably appended.
    """

    aggregate_type: ClassVar[str] = ""
    """Set by ``@register_aggregate("Type")``. Empty string means
    "unregistered abstract base"; constructing an unregistered concrete
    aggregate raises ``RuntimeError``."""

    def __init__(self, aggregate_id: str) -> None:
        if not self.aggregate_type:
            raise RuntimeError(
                f"{type(self).__name__} is not registered. "
                f"Use @register_aggregate('Type') on the class."
            )
        self.aggregate_id: str = aggregate_id
        self.aggregate_version: int = 0
        self._processed_command_ids: deque[str] = deque(maxlen=PROCESSED_COMMAND_RING_SIZE)

    # ---- replay + state ---------------------------------------------------

    def apply(self, envelope: EventEnvelope) -> None:
        """Apply an event to mutate aggregate state.

        Called by :class:`CommandHandler` post-append (live path) and
        by replay during ``_load_or_create`` (rebuild path). MUST NOT
        do I/O. MUST be deterministic.

        Provenance check: events in :data:`FRAMEWORK_ONLY_EVENT_TYPES`
        MUST carry an ``issued_by`` starting with ``framework:``. A
        forged event with a user/observer/bridge issuer raises
        :class:`CorruptAggregateAlert`.

        Order of checks:

        1. Provenance (framework-only event types).
        2. Version monotonicity. Checked *before* dispatch so a bad
           envelope cannot leave the aggregate in a partially-mutated
           state.
        3. Handler dispatch. Missing handler raises
           :class:`UnknownEventVersionError`.
        4. Version commit (only after handler returns cleanly).
        """
        if envelope.event_type in FRAMEWORK_ONLY_EVENT_TYPES and not is_framework_issuer(
            envelope.metadata.issued_by
        ):
            raise CorruptAggregateAlert(
                f"event {envelope.event_id} of type {envelope.event_type!r} "
                f"has non-framework issued_by="
                f"{envelope.metadata.issued_by!r}; likely forged. "
                f"See v7 §4.12 manual recovery runbook."
            )

        if envelope.aggregate_version != self.aggregate_version + 1:
            raise AggregateInvariantError(
                f"event aggregate_version={envelope.aggregate_version} "
                f"does not follow current version {self.aggregate_version}"
            )

        method_name = f"apply_{snake_case(envelope.event_type)}"
        handler = getattr(self, method_name, None)
        if handler is None:
            raise UnknownEventVersionError(
                f"{type(self).__name__} has no {method_name} "
                f"(event_version={envelope.event_version}). This "
                f"aggregate doesn't know how to apply "
                f"{envelope.event_type!r}."
            )

        try:
            handler(envelope.payload, envelope.metadata)
        except (AggregateInvariantError, CorruptAggregateAlert):
            raise
        except Exception as exc:
            raise AggregateInvariantError(f"{method_name} raised: {exc}") from exc

        self.aggregate_version = envelope.aggregate_version

    # ---- dedup buffer -----------------------------------------------------

    def has_processed(self, command_id: str) -> bool:
        """True iff this ``command_id`` is in the dedup buffer."""
        return command_id in self._processed_command_ids

    def mark_processed(self, command_id: str) -> None:
        """Record that ``command_id`` has been processed.

        Called by :class:`CommandHandler` post-commit (NOT by
        :meth:`apply`). The buffer is snapshot-only — pure event
        replay rebuilds an empty buffer; only snapshot restores carry
        buffer state. See v7 §4.5.
        """
        self._processed_command_ids.append(command_id)

    # ---- snapshot ---------------------------------------------------------

    def to_snapshot(self) -> dict[str, Any]:
        """Serialize aggregate state for snapshot persistence.

        Includes the dedup buffer. Concrete subclasses MUST override
        and call ``super().to_snapshot()`` to include base state.
        """
        return {
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "aggregate_version": self.aggregate_version,
            "processed_command_ids": list(self._processed_command_ids),
        }

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> Aggregate:
        """Restore an aggregate from a snapshot dict.

        Concrete subclasses MUST override to also restore their
        domain state.
        """
        instance = cls(aggregate_id=data["aggregate_id"])
        instance.aggregate_version = int(data["aggregate_version"])
        instance._processed_command_ids = deque(
            data.get("processed_command_ids", []),
            maxlen=PROCESSED_COMMAND_RING_SIZE,
        )
        return instance


class IntervalAggregate(Aggregate):
    """Aggregate with a finalization phase machine.

    Phases: ``created`` → ``active`` → ``finalized``. Once finalized,
    no further commands are accepted (:class:`CommandHandler`
    enforces). Finalization emits an ``InternalFinalized`` event with
    ``issued_by`` starting with ``framework:``.

    Concrete examples (Sprint 4a): ``OperatorJobSession``,
    ``Operation`` (when modelled as bounded intervals).
    """

    def __init__(self, aggregate_id: str) -> None:
        super().__init__(aggregate_id)
        self.phase: str = "created"

    def apply_internal_finalized(self, payload: dict[str, Any], metadata: EventMetadata) -> None:
        """Apply the framework-internal finalization event.

        Provenance is already checked by :meth:`Aggregate.apply`.
        Re-applying ``InternalFinalized`` to an already-finalized
        aggregate is a no-op state-wise — handles the ring-buffer
        overflow case from v7 §4.11 where a duplicate cascade command
        slips past the dedup buffer.
        """
        if self.phase == "finalized":
            return
        self.phase = "finalized"

    def to_snapshot(self) -> dict[str, Any]:
        """Extend the base snapshot with the phase field."""
        data = super().to_snapshot()
        data["phase"] = self.phase
        return data

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> IntervalAggregate:
        """Restore phase in addition to the base aggregate state."""
        instance = super().from_snapshot(data)
        assert isinstance(instance, IntervalAggregate)
        instance.phase = str(data.get("phase", "created"))
        return instance


class RootAggregate(IntervalAggregate):
    """Aggregate that owns child aggregates and may trigger cascade.

    When a root finalizes (``active`` → ``finalized``), P2
    (:class:`CascadeProjector`) cascades finalization to every
    registered child not yet finalized.

    Concrete example (Sprint 4a): ``Job`` (owns ``Operation`` children).
    """

    def __init__(self, aggregate_id: str) -> None:
        super().__init__(aggregate_id)
        self._children: dict[str, set[str]] = {}
        # child_type -> {child_aggregate_id, ...}

    def register_child(self, child_type: str, child_id: str) -> None:
        """Record that this root owns a child aggregate.

        Called by ``apply_*()`` handlers when domain events imply a
        new child membership. NOT a public command-facing method —
        concrete aggregates call this from inside ``apply_*()``.
        """
        self._children.setdefault(child_type, set()).add(child_id)

    def children_of(self, child_type: str) -> frozenset[str]:
        """Read-only view of registered child IDs by type."""
        return frozenset(self._children.get(child_type, set()))

    def all_children(self) -> dict[str, frozenset[str]]:
        """Snapshot-style view of every child type and its members."""
        return {k: frozenset(v) for k, v in self._children.items()}

    def to_snapshot(self) -> dict[str, Any]:
        """Extend the interval snapshot with the children registry."""
        data = super().to_snapshot()
        data["children"] = {k: sorted(v) for k, v in self._children.items()}
        return data

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> RootAggregate:
        """Restore the children registry in addition to interval state."""
        instance = super().from_snapshot(data)
        assert isinstance(instance, RootAggregate)
        instance._children = {k: set(v) for k, v in data.get("children", {}).items()}
        return instance
