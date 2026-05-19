"""JetStream-backed production implementations for ``heddle.contrib.events``.

Per Sprint 3 brief T8/T9. Mirrors the in-memory implementations in
:mod:`heddle.contrib.events.event_log` and
:mod:`heddle.contrib.events.rejection_log` so application code is
indifferent to the backend.

Imports are kept lazy: a bare ``pip install heddle-ai`` without a
running NATS server can still import the rest of ``heddle.contrib.events``
because the JetStream symbols are only resolved when the user names them.
"""

from heddle.contrib.events.jetstream.connection import (
    JetStreamConnection,
    connect_jetstream,
)
from heddle.contrib.events.jetstream.event_log import JetStreamEventLog
from heddle.contrib.events.jetstream.rejection_log import JetStreamRejectionLog
from heddle.contrib.events.jetstream.stream_config import (
    ensure_command_stream,
    ensure_event_stream,
    ensure_rejection_stream,
)

__all__ = [
    "JetStreamConnection",
    "JetStreamEventLog",
    "JetStreamRejectionLog",
    "connect_jetstream",
    "ensure_command_stream",
    "ensure_event_stream",
    "ensure_rejection_stream",
]
