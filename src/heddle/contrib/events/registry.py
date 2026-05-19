"""Aggregate registry for heddle.contrib.events.

Concrete aggregates register via decorator::

    @register_aggregate("Job")
    class JobAggregate(RootAggregate):
        ...

Registration happens at class-definition time. The
:class:`CommandHandler` looks up aggregate classes by ``aggregate_type``
string carried on the command.

The registry is process-global. Tests that register fake aggregates
MUST use the ``registry_isolation`` pytest fixture from
``heddle/tests/fixtures.py`` to prevent cross-test pollution.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from heddle.contrib.events.aggregate import Aggregate, RootAggregate

if TYPE_CHECKING:
    from collections.abc import Callable

_AggregateCls = TypeVar("_AggregateCls", bound=type[Aggregate])


AGGREGATE_REGISTRY: dict[str, type[Aggregate]] = {}
"""Mapping ``aggregate_type`` string → aggregate class.

Populated by :func:`register_aggregate`. Treat as read-only at
runtime; tests reset it via ``registry_isolation``.
"""


def register_aggregate(
    aggregate_type: str,
) -> Callable[[_AggregateCls], _AggregateCls]:
    """Class decorator that registers an :class:`Aggregate` subclass.

    Sets the class's ``aggregate_type`` ClassVar and adds it to
    :data:`AGGREGATE_REGISTRY`. Raises if ``aggregate_type`` is
    already registered to a *different* class (prevents silent
    shadowing); re-registering the same class with the same name is
    a no-op so module reloads stay tolerant.

    Example::

        @register_aggregate("Job")
        class JobAggregate(RootAggregate):
            def apply_job_shipped_from_pf(self, payload, metadata):
                ...
    """

    def decorator(cls: _AggregateCls) -> _AggregateCls:
        if not issubclass(cls, Aggregate):
            raise TypeError(
                f"@register_aggregate target must subclass Aggregate; got {cls.__name__}"
            )
        existing = AGGREGATE_REGISTRY.get(aggregate_type)
        if existing is not None and existing is not cls:
            raise ValueError(
                f"aggregate_type {aggregate_type!r} already registered "
                f"to {existing.__name__}; cannot reassign to {cls.__name__}"
            )
        cls.aggregate_type = aggregate_type
        AGGREGATE_REGISTRY[aggregate_type] = cls
        return cls

    return decorator


def get_aggregate_class(aggregate_type: str) -> type[Aggregate]:
    """Look up an aggregate class by type string. Raises ``KeyError``."""
    try:
        return AGGREGATE_REGISTRY[aggregate_type]
    except KeyError as exc:
        raise KeyError(
            f"no aggregate registered for type {aggregate_type!r}. "
            f"Did the module defining it get imported?"
        ) from exc


def is_root_type(aggregate_type: str) -> bool:
    """True iff the registered aggregate is a :class:`RootAggregate`.

    Used by P1 (:class:`ScopeMembershipProjector`) and P2
    (:class:`CascadeProjector`) to decide whether to maintain a
    child-registry membership view or to fan out finalization.
    """
    cls = get_aggregate_class(aggregate_type)
    return issubclass(cls, RootAggregate)
