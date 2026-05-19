"""Framework projectors for ``heddle.contrib.events``.

P1 (:class:`ScopeMembershipProjector`) and P2
(:class:`CascadeProjector`) are complete in Sprint 2.
P3 (:class:`FinalizationHorizonProjector`) is a stub; the full
implementation lands in Sprint 3 alongside the Valkey
atomicity-window mechanism.

Issued-by conventions:

- Framework projectors P1/P2/P3 use ``issued_by='framework:<name>'``.
- Application projectors that emit events as a side effect of
  projection use ``issued_by='projector:<name>'``.
"""

from heddle.contrib.events.projectors.cascade import (
    CASCADE_ISSUED_BY,
    CascadeProjector,
    deterministic_cascade_id,
)
from heddle.contrib.events.projectors.finalization_horizon import (
    FinalizationHorizonProjector,
)
from heddle.contrib.events.projectors.scope_membership import (
    CHILD_MEMBERSHIP_KEY,
    ScopeMembershipProjector,
)

__all__ = [
    "CASCADE_ISSUED_BY",
    "CHILD_MEMBERSHIP_KEY",
    "CascadeProjector",
    "FinalizationHorizonProjector",
    "ScopeMembershipProjector",
    "deterministic_cascade_id",
]
