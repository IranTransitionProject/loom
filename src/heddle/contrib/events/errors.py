"""Errors raised by the heddle.contrib.events runtime.

All errors here inherit from a common :class:`HeddleEventsError` so
callers can catch the whole family or any individual case. See
``heddle-contrib-events-m2-architecture-v7.md`` §4.5 and §4.6 for the
behavioural contract — particularly which layer raises which error.
"""

from __future__ import annotations


class HeddleEventsError(Exception):
    """Base class for all heddle.contrib.events errors."""


class UnknownEventVersionError(HeddleEventsError):
    """An event was loaded with a version this aggregate can't apply.

    Raised by :meth:`Aggregate.apply` when ``event.event_version``
    exceeds the highest version the aggregate's apply() handler can
    replay. Forward-compat marker: typically a downgrade-from-newer-
    cluster scenario.
    """


class AggregateInvariantError(HeddleEventsError):
    """An aggregate-state invariant would be violated by applying this event.

    Raised by :meth:`Aggregate.apply` when an event would put the
    aggregate in a state that violates a class invariant. Distinct
    from :class:`CommandRejected`, which is raised earlier by
    command-handler logic before any event is produced.
    """


class CommandRejected(HeddleEventsError):  # noqa: N818 - name fixed by v7 §4.5 wire contract
    """A command was rejected by aggregate validation, before any event was produced.

    Raised by command-handler methods on the aggregate. The
    :class:`CommandHandler` catches this, writes a rejection envelope
    to the :class:`RejectionLog`, and re-raises to the caller. Carries
    a machine-readable ``reason`` code and a human-readable ``detail``.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class ConcurrencyError(HeddleEventsError):
    """Optimistic concurrency check failed during :meth:`EventLog.append`.

    Raised when ``expected_version`` passed to ``EventLog.append()``
    doesn't match the aggregate's current persisted version.
    """


class BusResultTimeoutError(HeddleEventsError):
    """A synchronous bus request timed out waiting for a result.

    Raised by Sprint 3 NATS request/reply paths. Defined here in
    Sprint 2 so the error hierarchy is complete from the start.
    """


class CorruptAggregateAlert(HeddleEventsError):  # noqa: N818 - name fixed by v7 §4.5 wire contract
    """An aggregate's event log contains a forged or impossible event.

    Raised at :meth:`Aggregate.apply` time when an ``InternalFinalized``
    event is found with ``metadata.issued_by`` that doesn't start with
    ``framework:``. The application-layer backstop for the Sprint 3
    NATS publish ACL on ``*.InternalFinalized`` subjects — see v7 §4.5
    Note.

    When raised by ``load()`` / ``replay()``, the
    :class:`CommandHandler` should surface this prominently and refuse
    to process further commands against the affected aggregate until
    the v7 §4.12 manual recovery runbook is invoked.
    """
