"""
NATS message bus adapter — the default transport layer for Heddle communication.

All inter-actor communication flows through this adapter. Actors never
touch NATS directly; they use the MessageBus interface (or BaseActor's
publish/subscribe wrappers, which delegate here).

Subject naming convention:
    heddle.tasks.incoming          — Router's inbox (all task dispatch goes here first)
    heddle.tasks.{worker_type}.{tier} — Worker queues (router publishes here)
    heddle.results.{goal_id}       — Results routed back to orchestrators
    heddle.results.default         — Results with no parent_task_id
    heddle.goals.incoming          — Pipeline orchestrator's inbox
    heddle.control.{actor_id}      — Control messages (shutdown, status) [not yet used]
    heddle.events                  — System-wide events (logging, metrics) [not yet used]

Connection defaults:
    reconnect_time_wait=1s, max_reconnect_attempts=60 — totals ~60s of retry.
    If NATS is down longer than that, the actor will crash and needs restart.
    Disconnect and reconnect events are logged for operational visibility.

Delivery semantics:
    At-most-once. If no subscriber is listening when a message is published,
    the message is silently dropped. NATS JetStream would add persistence
    but is not yet configured.

NOTE: All messages are JSON-serialized dicts. Binary payloads are not supported.
      Large data should be passed via file references (workspace directory), not
      inline in messages.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import nats
import nats.errors
import structlog

from heddle.bus.base import MessageBus, Subscription

if TYPE_CHECKING:
    from nats.aio.client import Client as NATSClient

logger = structlog.get_logger()

# Reconnection defaults — fixed-interval retry (nats-py
# ``reconnect_time_wait`` is a constant per-attempt sleep, not an
# exponential backoff).  1s x 60 attempts ~ 60s total recovery window.
_RECONNECT_INITIAL_WAIT = 1  # seconds
_RECONNECT_MAX_ATTEMPTS = 60

# Errors that mean the subscription will never deliver another message.
# Any other exception from ``next_msg`` is treated as transient and the
# loop continues after a short backoff (see NATSSubscription.__anext__).
_TERMINAL_SUB_ERRORS: tuple[type[BaseException], ...] = (
    nats.errors.ConnectionClosedError,
    nats.errors.BadSubscriptionError,
    nats.errors.ConnectionDrainingError,
)

# Sleep between retries after a transient ``next_msg`` error.  Short
# enough to recover quickly; long enough to avoid busy-looping if the
# error persists.
_TRANSIENT_RETRY_SLEEP = 0.1  # seconds


class NATSSubscription(Subscription):
    """Wraps a nats-py subscription as an async iterator of parsed dicts."""

    def __init__(self, nats_sub: Any) -> None:
        self._sub = nats_sub

    async def unsubscribe(self) -> None:
        """Unsubscribe from the underlying NATS subscription."""
        await self._sub.unsubscribe()

    def __aiter__(self) -> NATSSubscription:
        return self

    async def __anext__(self) -> dict[str, Any]:
        """Yield the next message, JSON-decoded.

        Blocks until a message arrives.

        Termination — only the connection or subscription being closed
        ends iteration.  Earlier shapes raised ``StopAsyncIteration``
        from any ``next_msg`` exception, so a single transient
        nats-py error (slow consumer signal, momentary connection
        state, parse hiccup) silently killed the actor's only inbound
        channel forever.  We now distinguish:

        - **Terminal** (subscription closed, connection closed/drained)
          → raise ``StopAsyncIteration`` so callers exit cleanly.
        - **Transient** (anything else, e.g. ``SlowConsumerError``,
          ``StaleConnectionError``) → log a warning, sleep briefly to
          avoid busy-looping if the error persists, then retry.
        - ``asyncio.CancelledError`` → re-raise so shutdown propagates.

        Malformed (non-JSON) messages are also skipped, with a
        warning, so a single corrupt payload doesn't terminate the
        subscription.
        """
        while True:
            try:
                msg = await self._sub.next_msg(timeout=None)
            except asyncio.CancelledError:
                # Shutdown signal — must propagate.
                raise
            except _TERMINAL_SUB_ERRORS as e:
                logger.info(
                    "nats.subscription_terminated",
                    error=str(e),
                    error_type=type(e).__name__,
                )
                raise StopAsyncIteration from e
            except Exception as e:
                logger.warning(
                    "nats.subscription_transient_error",
                    error=str(e),
                    error_type=type(e).__name__,
                )
                await asyncio.sleep(_TRANSIENT_RETRY_SLEEP)
                continue

            try:
                return json.loads(msg.data.decode())
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                logger.warning(
                    "nats.malformed_message_skipped",
                    error=str(e),
                    error_type=type(e).__name__,
                    subject=msg.subject,
                    data_length=len(msg.data),
                )
                # Skip this message and wait for the next one.
                continue


class NATSBus(MessageBus):
    """NATS-backed MessageBus implementation.

    Provides three messaging patterns:
    - publish(): Fire-and-forget (tasks, results)
    - subscribe(): Async iterator with optional queue groups for load balancing
    - request(): Request-reply for synchronous-style calls (not yet used by any actor)

    Delivery semantics: at-most-once. Messages published with no active
    subscriber are silently dropped.
    """

    def __init__(self, url: str = "nats://nats:4222") -> None:
        self.url = url
        self._nc: NATSClient | None = None

    def _require_connected(self) -> NATSClient:
        """Return the live NATS client or raise a clear error.

        Without this guard, calls to :meth:`publish` / :meth:`subscribe` /
        :meth:`request` before :meth:`connect` raised
        ``AttributeError: 'NoneType' object has no attribute …`` — opaque
        and easy to miss in error logs.  This raises ``RuntimeError`` with
        the offending bus URL so the misuse is obvious.
        """
        if self._nc is None:
            raise RuntimeError(f"NATSBus({self.url!r}) is not connected — call connect() first")
        return self._nc

    async def connect(self) -> None:
        """Connect to the NATS server with reconnection and event logging.

        Reconnection: 1s interval, up to 60 attempts (~60s of total retry).
        Disconnect/reconnect events are logged for operational visibility.
        """

        async def _on_reconnect(_nc: Any) -> None:
            logger.info("bus.reconnected", url=self.url)

        async def _on_disconnect(_nc: Any) -> None:
            logger.warning("bus.disconnected", url=self.url)

        self._nc = await nats.connect(
            self.url,
            reconnect_time_wait=_RECONNECT_INITIAL_WAIT,
            max_reconnect_attempts=_RECONNECT_MAX_ATTEMPTS,
            reconnected_cb=_on_reconnect,
            disconnected_cb=_on_disconnect,
        )
        logger.info("bus.connected", url=self.url)

    async def close(self) -> None:
        """Drain and close the NATS connection."""
        if self._nc:
            await self._nc.drain()

    async def publish(self, subject: str, data: dict[str, Any]) -> None:
        """Publish a JSON-serialized dict to a NATS subject.

        NOTE: No delivery guarantee — if no subscriber is listening,
        the message is silently dropped. NATS JetStream would add
        persistence but is not yet configured.
        """
        nc = self._require_connected()
        await nc.publish(subject, json.dumps(data).encode())

    async def subscribe(
        self,
        subject: str,
        queue_group: str | None = None,
    ) -> NATSSubscription:
        """Subscribe to a subject, returning an async-iterable NATSSubscription.

        Queue group enables competing consumers for horizontal scaling.
        """
        nc = self._require_connected()
        if queue_group:
            nats_sub = await nc.subscribe(subject, queue=queue_group)
        else:
            nats_sub = await nc.subscribe(subject)
        return NATSSubscription(nats_sub)

    async def request(
        self, subject: str, data: dict[str, Any], timeout: float = 30.0
    ) -> dict[str, Any]:
        """Request-reply pattern for synchronous-style calls.

        NOTE: Not currently used by any Heddle actor. Available for future
        use cases like health checks or synchronous worker queries.
        Raises nats.errors.TimeoutError if no reply within timeout.
        """
        nc = self._require_connected()
        resp = await nc.request(
            subject,
            json.dumps(data).encode(),
            timeout=timeout,
        )
        return json.loads(resp.data.decode())
