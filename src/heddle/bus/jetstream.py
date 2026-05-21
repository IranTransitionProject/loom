"""Generic JetStream primitives — durable messaging on NATS for ``heddle.core``.

JetStream is a committed core substrate: durable, replayable, audit-grade
message history for the SMB-business audience (DESIGN_INVARIANTS #24). This
module owns the *generic* JetStream mechanics with **no domain semantics**:

- **stream management** — idempotent :func:`ensure_stream`,
- **dedup + CAS publish** — :func:`publish` (``Nats-Msg-Id`` server-side
  dedup and ``Nats-Expected-Last-Subject-Sequence`` optimistic concurrency),
- **a durable pull-consumer base** — :func:`pull`.

Contrib modules (e.g. ``heddle.contrib.events``) *specialize* over these:
they build subject names, pick stream names, and map the generic
:class:`WrongLastSequenceError` raised here onto their own domain errors
(e.g. ``ConcurrencyError``). The dependency is one-way — this module never
imports contrib (DESIGN_INVARIANTS #23).

The :class:`~heddle.bus.base.MessageBus` ABC remains the fire-and-forget
(at-most-once) transport abstraction. These helpers are the *durable* path
layered directly on a live ``nats.js.JetStreamContext``; durability-needing
specializations use them directly rather than through ``MessageBus``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import nats.errors
from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType, StreamConfig
from nats.js.errors import APIError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from nats.aio.msg import Msg
    from nats.js import JetStreamContext
    from nats.js.api import PubAck


# JetStream header that carries the dedup id. When the target stream has a
# non-zero ``duplicate_window``, a second publish with the same value inside
# the window is recorded once and acked with ``PubAck.duplicate=True``.
_MSG_ID_HEADER = "Nats-Msg-Id"

# JetStream header for compare-and-swap append. JetStream rejects a publish
# whose stream-side last sequence for the subject differs from this value.
_CAS_HEADER = "Nats-Expected-Last-Subject-Sequence"

# APIError ``err_code`` JetStream returns on a ``Nats-Expected-Last-*``
# mismatch (the CAS rejection).
_WRONG_LAST_SEQ_ERROR_CODE = 10071


class JetStreamError(Exception):
    """Base class for generic JetStream-primitive errors."""


class WrongLastSequenceError(JetStreamError):
    """A CAS publish was rejected: the subject's last sequence didn't match.

    Raised by :func:`publish` when ``expected_last_subject_sequence`` does not
    equal JetStream's current last sequence for the subject. Specializations
    translate this into their own domain concurrency error.
    """

    def __init__(
        self,
        subject: str,
        expected_last_subject_sequence: int,
        description: str = "",
    ) -> None:
        self.subject = subject
        self.expected_last_subject_sequence = expected_last_subject_sequence
        self.description = description
        super().__init__(
            f"CAS publish to {subject!r} rejected: "
            f"expected_last_subject_sequence={expected_last_subject_sequence} "
            f"did not match JetStream's current last sequence"
            + (f" ({description})" if description else "")
        )


async def ensure_stream(
    js: JetStreamContext,
    *,
    name: str,
    subjects: list[str],
    max_age_seconds: int = 0,
    storage: StorageType = StorageType.FILE,
    replicas: int = 1,
    discard: DiscardPolicy = DiscardPolicy.OLD,
    duplicate_window_seconds: int = 0,
) -> None:
    """Create the stream if absent; a no-op when it already matches.

    Idempotent: ``add_stream`` returns the existing stream when the
    ``(name, subjects)`` and config match, so calling this once per process
    startup is safe. A diverging existing config raises — operators update a
    stream deliberately, not by having application code retro-fit it silently.

    ``max_age_seconds=0`` means unbounded retention. ``max_msgs`` is left
    unset (nats-py's unbounded sentinel is ``None``; a literal ``0`` risks
    being read as "zero messages"). ``duplicate_window_seconds`` enables
    server-side dedup of repeated ``Nats-Msg-Id`` values within the window.

    Durations are passed in **seconds**: ``StreamConfig`` declares ``max_age``
    and ``duplicate_window`` in seconds and converts them to nanoseconds itself
    in ``as_dict`` (nats-py ``_to_nanoseconds``). Pre-multiplying here would
    double-convert and overflow Go's ``time.Duration`` server-side.
    """
    config = StreamConfig(
        name=name,
        subjects=subjects,
        retention=RetentionPolicy.LIMITS,
        max_age=max_age_seconds,  # seconds; 0 = unbounded (nats-py converts to ns)
        storage=storage,
        num_replicas=replicas,
        discard=discard,
        duplicate_window=duplicate_window_seconds,  # seconds (nats-py converts to ns)
    )
    await js.add_stream(config=config)  # type: ignore[reportUnknownMemberType]


async def publish(
    js: JetStreamContext,
    subject: str,
    payload: bytes,
    *,
    msg_id: str | None = None,
    expected_last_subject_sequence: int | None = None,
    headers: dict[str, str] | None = None,
) -> PubAck:
    """Publish ``payload`` to ``subject`` with optional dedup and CAS.

    - ``msg_id`` sets the ``Nats-Msg-Id`` header. With a stream
      ``duplicate_window``, a repeat within the window is acked with
      ``PubAck.duplicate=True`` and stored once.
    - ``expected_last_subject_sequence`` sets the CAS header. A mismatch
      raises :class:`WrongLastSequenceError`. ``None`` skips the CAS header
      (unconditional append).

    Returns the ``PubAck`` (whose ``duplicate`` flag reports server-side
    dedup). Other JetStream ``APIError`` codes propagate unchanged.
    """
    merged: dict[str, str] = dict(headers or {})
    if msg_id is not None:
        merged[_MSG_ID_HEADER] = msg_id
    if expected_last_subject_sequence is not None:
        merged[_CAS_HEADER] = str(expected_last_subject_sequence)

    try:
        return await js.publish(subject, payload, headers=merged or None)
    except APIError as exc:
        if exc.err_code == _WRONG_LAST_SEQ_ERROR_CODE:
            # expected_last_subject_sequence is non-None on this path: the
            # CAS header is the only thing that elicits err_code 10071.
            raise WrongLastSequenceError(
                subject,
                expected_last_subject_sequence or 0,
                exc.description or "",
            ) from exc
        raise


async def pull(
    js: JetStreamContext,
    *,
    subject: str,
    durable: str | None = None,
    batch: int = 64,
    timeout: float = 0.25,
) -> AsyncIterator[Msg]:
    """Drain messages for ``subject`` via a (optionally durable) pull consumer.

    Yields raw JetStream messages in stream order and returns once the
    subject is momentarily exhausted (a ``fetch`` timeout or empty batch).
    The **caller MUST ack** each yielded message — this base does not ack, so
    that specializations can ack-on-success and let unacked messages redeliver
    on a durable consumer. Pass ``durable`` to reuse a named consumer across
    restarts; ``None`` creates an ephemeral one torn down on exit.
    """
    sub = await js.pull_subscribe(subject=subject, durable=durable)
    try:
        while True:
            try:
                msgs = await sub.fetch(batch=batch, timeout=timeout)
            except nats.errors.TimeoutError:
                return
            if not msgs:
                return
            for msg in msgs:
                yield msg
    finally:
        await sub.unsubscribe()
