"""JetStreamEventLog — production :class:`EventLog` backed by NATS JetStream.

Subject convention:
    ``heddle.events.{aggregate_type}.{aggregate_id}.{event_type}``

Streams (one per aggregate_type):
    ``HEDDLE_EVENTS_{TYPE_UPPER}``

CAS append
----------
The ``Nats-Expected-Last-Subject-Sequence`` header is set to the
``expected_version`` argument (the aggregate version BEFORE this event).
JetStream rejects publishes whose stream-side last sequence for the
subject differs from the header value — that rejection is translated
into :class:`ConcurrencyError`.

``expected_version=None`` skips the CAS header. Used by PF observers
that ingest from an external source-of-truth.

Load
----
A one-shot ordered consumer with subject filter
``heddle.events.{type}.{id}.>`` streams events in stream-order. Only
events whose ``aggregate_version > from_version`` are yielded.

Subscribe
---------
A push subscription with a durable consumer name keyed on the
aggregate_type. Returns an async iterator whose underlying subscription
is already registered when ``subscribe()`` returns, per the Sprint 3
R4 contract.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, cast

import nats.errors
from nats.js.errors import APIError

from heddle.contrib.events.envelopes import EventEnvelope
from heddle.contrib.events.errors import ConcurrencyError
from heddle.contrib.events.event_log import EventLog
from heddle.contrib.events.jetstream.stream_config import (
    event_subject,
    event_subject_prefix,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from nats.js import JetStreamContext


# JetStream returns this APIError code on Nats-Expected-Last-* mismatch.
_WRONG_LAST_SEQ_ERROR_CODE: int = 10071


class JetStreamEventLog(EventLog):
    """Production :class:`EventLog` backed by NATS JetStream.

    Construction does NOT verify that the underlying stream exists.
    Operators call :func:`ensure_event_stream` once at startup for each
    aggregate_type used in the deployment.
    """

    def __init__(self, js: JetStreamContext) -> None:
        self._js = js

    async def append(
        self, envelope: EventEnvelope, expected_version: int | None
    ) -> None:
        """Publish ``envelope`` with optional CAS on ``aggregate_version``."""
        subject = event_subject(
            envelope.aggregate_type, envelope.aggregate_id, envelope.event_type
        )
        payload = envelope.model_dump_json().encode()
        headers: dict[str, str] = {}
        if expected_version is not None:
            headers["Nats-Expected-Last-Subject-Sequence"] = str(expected_version)
        try:
            await self._js.publish(subject, payload, headers=headers or None)
        except APIError as exc:
            if exc.err_code == _WRONG_LAST_SEQ_ERROR_CODE:
                raise ConcurrencyError(
                    f"append for {envelope.aggregate_type}:"
                    f"{envelope.aggregate_id} expected_version="
                    f"{expected_version} but JetStream rejected with "
                    f"wrong-last-sequence ({exc.description})"
                ) from exc
            raise

    async def load(
        self,
        aggregate_type: str,
        aggregate_id: str,
        from_version: int = 0,
    ) -> AsyncIterator[EventEnvelope]:
        """Stream events for one aggregate ordered by ``aggregate_version``.

        Implemented as a one-shot pull subscription with subject filter
        on ``heddle.events.{type}.{id}.>``. The subscription is torn
        down when iteration ends.
        """
        sub = await self._js.pull_subscribe(
            subject=f"heddle.events.{aggregate_type}.{aggregate_id}.>",
            durable=None,
        )
        try:
            while True:
                try:
                    msgs = await sub.fetch(batch=64, timeout=0.25)
                except nats.errors.TimeoutError:
                    return
                if not msgs:
                    return
                for msg in msgs:
                    await msg.ack()  # type: ignore[reportUnknownMemberType]
                    data = cast("bytes", msg.data)  # type: ignore[reportUnknownMemberType]
                    envelope = EventEnvelope.model_validate_json(data)
                    if envelope.aggregate_version > from_version:
                        yield envelope
        finally:
            await sub.unsubscribe()

    async def subscribe(
        self, aggregate_type: str
    ) -> AsyncIterator[EventEnvelope]:
        """Return an async iterator of newly-appended events for ``aggregate_type``.

        The underlying push subscription is established BEFORE this
        method returns — callers may publish immediately afterwards
        and be sure of delivery.
        """
        sub = await self._js.subscribe(
            subject=event_subject_prefix(aggregate_type),
        )
        return self._iterate_subscription(sub)

    async def _iterate_subscription(self, sub: object) -> AsyncIterator[EventEnvelope]:
        try:
            while True:
                msg = await sub.next_msg(timeout=None)  # type: ignore[attr-defined]
                await msg.ack()  # type: ignore[reportUnknownMemberType]
                data = cast("bytes", msg.data)  # type: ignore[reportUnknownMemberType]
                yield EventEnvelope.model_validate_json(data)
        finally:
            with contextlib.suppress(Exception):
                await sub.unsubscribe()  # type: ignore[attr-defined]
