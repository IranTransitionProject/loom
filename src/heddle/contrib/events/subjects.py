"""Subject helpers for the heddle.contrib.events wire contract.

These functions construct the canonical NATS subjects for events,
commands, and rejections. They are mirrored in heddle-sdk (Swift +
.NET) and form part of the cross-language wire contract.

Subject patterns (heddle-contrib-events-m2-architecture-v7.md §4.10)::

    heddle.events.{aggregate_type}.{aggregate_id}.{event_type}
    heddle.commands.{aggregate_type}.{aggregate_id}.{command_type}
    heddle.rejections.{aggregate_type}.{aggregate_id}.{command_type}

Stream names follow the pattern ``HEDDLE_EVENTS_{TYPE_UPPER}``,
``HEDDLE_COMMANDS_{TYPE_UPPER}``, ``HEDDLE_REJECTIONS_{TYPE_UPPER}``.

NOT included here: ``heddle.tasks.*`` / ``heddle.results.*`` /
``heddle.goals.*`` — those live in :mod:`heddle.bus.nats_adapter`
(router-dispatched worker subjects, distinct from event-sourcing
subjects).
"""

from __future__ import annotations

_NATS_TOKEN_FORBIDDEN = frozenset({".", " ", "*", ">"})


def _validate_token(name: str, label: str) -> None:
    """Reject names containing characters that would break NATS subject parsing.

    NATS subject tokens are dot-separated; ``*`` and ``>`` are wildcards.
    Empty tokens are also invalid.
    """
    if not name:
        raise ValueError(f"{label} must not be empty")
    bad = {c for c in name if c in _NATS_TOKEN_FORBIDDEN}
    if bad:
        raise ValueError(f"{label}={name!r} contains forbidden NATS subject chars: {sorted(bad)}")


def event_subject(aggregate_type: str, aggregate_id: str, event_type: str) -> str:
    """Build the publish subject for an event envelope.

    Example::

        >>> event_subject("Job", "39174-004", "JobShippedFromPF")
        'heddle.events.Job.39174-004.JobShippedFromPF'
    """
    _validate_token(aggregate_type, "aggregate_type")
    _validate_token(aggregate_id, "aggregate_id")
    _validate_token(event_type, "event_type")
    return f"heddle.events.{aggregate_type}.{aggregate_id}.{event_type}"


def command_subject(aggregate_type: str, aggregate_id: str, command_type: str) -> str:
    """Build the publish subject for a command message.

    Example::

        >>> command_subject("Operation", "39174-004:laser-cut", "RecordLabor")
        'heddle.commands.Operation.39174-004:laser-cut.RecordLabor'
    """
    _validate_token(aggregate_type, "aggregate_type")
    _validate_token(aggregate_id, "aggregate_id")
    _validate_token(command_type, "command_type")
    return f"heddle.commands.{aggregate_type}.{aggregate_id}.{command_type}"


def rejection_subject(aggregate_type: str, aggregate_id: str, command_type: str) -> str:
    """Build the publish subject for a rejection envelope."""
    _validate_token(aggregate_type, "aggregate_type")
    _validate_token(aggregate_id, "aggregate_id")
    _validate_token(command_type, "command_type")
    return f"heddle.rejections.{aggregate_type}.{aggregate_id}.{command_type}"


def event_stream_name(aggregate_type: str) -> str:
    """JetStream stream name for events of a given aggregate type.

    Example: ``event_stream_name("Job") -> "HEDDLE_EVENTS_JOB"``.
    """
    _validate_token(aggregate_type, "aggregate_type")
    return f"HEDDLE_EVENTS_{aggregate_type.upper()}"


def command_stream_name(aggregate_type: str) -> str:
    """JetStream stream name for commands of a given aggregate type."""
    _validate_token(aggregate_type, "aggregate_type")
    return f"HEDDLE_COMMANDS_{aggregate_type.upper()}"


def rejection_stream_name(aggregate_type: str) -> str:
    """JetStream stream name for rejections of a given aggregate type."""
    _validate_token(aggregate_type, "aggregate_type")
    return f"HEDDLE_REJECTIONS_{aggregate_type.upper()}"


def event_subject_filter(aggregate_type: str) -> str:
    """Wildcard subject for subscribing to all events of an aggregate type.

    Example: ``event_subject_filter("Job") -> "heddle.events.Job.>"``.
    """
    _validate_token(aggregate_type, "aggregate_type")
    return f"heddle.events.{aggregate_type}.>"


def command_subject_filter(aggregate_type: str) -> str:
    """Wildcard subject for subscribing to all commands of an aggregate type."""
    _validate_token(aggregate_type, "aggregate_type")
    return f"heddle.commands.{aggregate_type}.>"


def rejection_subject_filter(aggregate_type: str) -> str:
    """Wildcard subject for subscribing to all rejections of an aggregate type."""
    _validate_token(aggregate_type, "aggregate_type")
    return f"heddle.rejections.{aggregate_type}.>"
