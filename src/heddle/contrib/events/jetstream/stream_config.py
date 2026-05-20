"""Idempotent JetStream stream configuration helpers.

Per v7 §4.6 / Sprint 3 brief T8/T9. Each aggregate_type gets three
dedicated streams: events, commands, rejections.

Stream naming
-------------
``HEDDLE_EVENTS_{TYPE_UPPER}``  / subject ``heddle.events.{type}.>``
``HEDDLE_COMMANDS_{TYPE_UPPER}`` / subject ``heddle.commands.{type}.>``
``HEDDLE_REJECTIONS_{TYPE_UPPER}`` / subject ``heddle.rejections.{type}.>``

Defaults: file storage, single replica. The **event stream is the
source of truth**: unbounded age, ``discard=new`` (reject new writes
when full rather than silently drop the oldest events — replay needs
those first), and a 10m duplicate window for opportunistic dedup.
Command and rejection streams keep age-based retention (7d / 30d) with
``discard=old``.

The helpers are **idempotent**: ``add_stream`` returns the existing
stream config if the (name, subjects) match. Diverging existing
configs raise; the operator is expected to update the stream
manually rather than have application code silently retro-fit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType, StreamConfig

if TYPE_CHECKING:
    from nats.js import JetStreamContext


# The event log is never aged out: it is the source of truth for replay
# and time-travel. 0 = unbounded.
DEFAULT_EVENTS_MAX_AGE_SECONDS: int = 0
EVENT_STREAM_DUPLICATE_WINDOW_SECONDS: int = 10 * 60
DEFAULT_COMMANDS_MAX_AGE_SECONDS: int = 7 * 24 * 3600
DEFAULT_REJECTIONS_MAX_AGE_SECONDS: int = 30 * 24 * 3600


def _stream_name(prefix: str, aggregate_type: str) -> str:
    return f"HEDDLE_{prefix}_{aggregate_type.upper()}"


def event_subject(aggregate_type: str, aggregate_id: str, event_type: str) -> str:
    """Subject for a single EventEnvelope publish."""
    return f"heddle.events.{aggregate_type}.{aggregate_id}.{event_type}"


def event_subject_prefix(aggregate_type: str) -> str:
    """Wildcard subject covering every event for one aggregate_type."""
    return f"heddle.events.{aggregate_type}.>"


def command_subject(aggregate_type: str, aggregate_id: str, command_type: str) -> str:
    """Subject for a CommandMessage publish."""
    return f"heddle.commands.{aggregate_type}.{aggregate_id}.{command_type}"


def command_subject_prefix(aggregate_type: str) -> str:
    """Wildcard subject covering every command for one aggregate_type."""
    return f"heddle.commands.{aggregate_type}.>"


def rejection_subject(aggregate_type: str, aggregate_id: str, command_type: str) -> str:
    """Subject for a RejectionEnvelope publish."""
    return f"heddle.rejections.{aggregate_type}.{aggregate_id}.{command_type}"


def rejection_subject_prefix(aggregate_type: str) -> str:
    """Wildcard subject covering every rejection for one aggregate_type."""
    return f"heddle.rejections.{aggregate_type}.>"


async def _ensure_stream(
    js: JetStreamContext,
    *,
    name: str,
    subjects: list[str],
    max_age_seconds: int,
    storage: StorageType = StorageType.FILE,
    replicas: int = 1,
    discard: DiscardPolicy = DiscardPolicy.OLD,
    duplicate_window_seconds: int = 0,
) -> None:
    # max_msgs is intentionally left unset: nats-py's unbounded sentinel
    # is None (the field is omitted). The contract's "max_msgs: 0" means
    # "unbounded"; a literal 0 risks being read as "zero messages".
    config = StreamConfig(
        name=name,
        subjects=subjects,
        retention=RetentionPolicy.LIMITS,
        max_age=max_age_seconds * 1_000_000_000,  # nanoseconds; 0 = unbounded
        storage=storage,
        num_replicas=replicas,
        discard=discard,
        duplicate_window=duplicate_window_seconds * 1_000_000_000,  # nanoseconds
    )
    await js.add_stream(config=config)  # type: ignore[reportUnknownMemberType]


async def ensure_event_stream(
    js: JetStreamContext,
    aggregate_type: str,
    max_age_seconds: int = DEFAULT_EVENTS_MAX_AGE_SECONDS,
    storage: StorageType = StorageType.FILE,
    replicas: int = 1,
) -> None:
    """Create or update the ``HEDDLE_EVENTS_{TYPE}`` stream.

    The event stream is the source of truth: unbounded age and
    ``discard=new`` so storage pressure never silently drops the oldest
    events (which replay needs first), plus a 10m duplicate window.
    """
    await _ensure_stream(
        js,
        name=_stream_name("EVENTS", aggregate_type),
        subjects=[event_subject_prefix(aggregate_type)],
        max_age_seconds=max_age_seconds,
        storage=storage,
        replicas=replicas,
        discard=DiscardPolicy.NEW,
        duplicate_window_seconds=EVENT_STREAM_DUPLICATE_WINDOW_SECONDS,
    )


async def ensure_command_stream(
    js: JetStreamContext,
    aggregate_type: str,
    max_age_seconds: int = DEFAULT_COMMANDS_MAX_AGE_SECONDS,
    storage: StorageType = StorageType.FILE,
    replicas: int = 1,
) -> None:
    """Create or update the ``HEDDLE_COMMANDS_{TYPE}`` stream."""
    await _ensure_stream(
        js,
        name=_stream_name("COMMANDS", aggregate_type),
        subjects=[command_subject_prefix(aggregate_type)],
        max_age_seconds=max_age_seconds,
        storage=storage,
        replicas=replicas,
    )


async def ensure_rejection_stream(
    js: JetStreamContext,
    aggregate_type: str,
    max_age_seconds: int = DEFAULT_REJECTIONS_MAX_AGE_SECONDS,
    storage: StorageType = StorageType.FILE,
    replicas: int = 1,
) -> None:
    """Create or update the ``HEDDLE_REJECTIONS_{TYPE}`` stream."""
    await _ensure_stream(
        js,
        name=_stream_name("REJECTIONS", aggregate_type),
        subjects=[rejection_subject_prefix(aggregate_type)],
        max_age_seconds=max_age_seconds,
        storage=storage,
        replicas=replicas,
    )
