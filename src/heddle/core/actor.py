"""
Base actor class — the foundation of Heddle's actor model.

All Heddle actors (workers, orchestrators, routers) inherit from BaseActor.
This class handles the message bus subscription lifecycle, message dispatch,
signal-based shutdown, and error isolation. Each actor is an independent
process with no shared memory.

Design invariant: actors communicate ONLY through bus messages (see messages.py).
Direct method calls between actors are forbidden.

The message bus is pluggable via the ``bus`` constructor parameter. The default
is NATSBus (created from ``nats_url`` when no bus is provided). For testing,
pass an InMemoryBus instead.
"""

from __future__ import annotations

import asyncio
import signal
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

import structlog

from heddle.tracing import extract_trace_context, get_tracer

if TYPE_CHECKING:
    from heddle.bus.base import MessageBus, Subscription

logger = structlog.get_logger()
_tracer = get_tracer("heddle.actor")


class BaseActor(ABC):
    """
    Actor model base class.

    Each actor:
    - Subscribes to a message bus subject
    - Processes messages with configurable concurrency (default: 1 = strict ordering)
    - Communicates only through structured messages
    - Has isolated state (no shared memory)
    - Shuts down gracefully on SIGTERM/SIGINT

    Concurrency can be configured via max_concurrent. Values > 1 allow parallel
    message processing within a single actor instance — use with care, as it
    relaxes ordering guarantees. Horizontal scaling via queue groups (multiple
    replicas) is the preferred way to increase throughput while preserving
    per-message isolation.

    The message bus can be injected via the ``bus`` keyword argument. If omitted,
    a NATSBus is created from ``nats_url`` (backward-compatible default).
    """

    def __init__(
        self,
        actor_id: str,
        nats_url: str = "nats://nats:4222",
        max_concurrent: int = 1,
        *,
        bus: MessageBus | None = None,
        shutdown_grace_seconds: float = 5.0,
    ) -> None:
        self.actor_id = actor_id
        self.max_concurrent = max_concurrent
        # On shutdown, in-flight ``handle_message`` tasks (only created
        # when ``max_concurrent > 1``) get this many seconds to finish
        # before they're force-cancelled and the bus is closed.  5s is
        # tight enough for fast unit tests and loose enough for a
        # typical local LLM call.  Tune up for slow backends.
        self.shutdown_grace_seconds = shutdown_grace_seconds
        self._sub: Subscription | None = None
        self._control_sub: Subscription | None = None
        self._running = False
        self._shutdown_event: asyncio.Event | None = None
        # Semaphore is created at run() time inside the event loop
        self._semaphore: asyncio.Semaphore | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()

        if bus is not None:
            self._bus = bus
        else:
            from heddle.bus.nats_adapter import NATSBus

            self._bus = NATSBus(nats_url)

    async def connect(self) -> None:
        """Connect to the message bus."""
        await self._bus.connect()
        logger.info("actor.connected", actor_id=self.actor_id)

    async def disconnect(self) -> None:
        """Unsubscribe and close the message bus connection."""
        if self._control_sub:
            await self._control_sub.unsubscribe()
        if self._sub:
            await self._sub.unsubscribe()
        await self._bus.close()
        logger.info("actor.disconnected", actor_id=self.actor_id)

    async def subscribe(self, subject: str, queue_group: str | None = None) -> None:
        """Subscribe to a bus subject.

        Queue group enables competing consumers
        (multiple worker replicas share load).
        """
        self._sub = await self._bus.subscribe(subject, queue_group)
        logger.info("actor.subscribed", actor_id=self.actor_id, subject=subject)

    async def publish(self, subject: str, message: dict[str, Any]) -> None:
        """Publish a message to the given bus subject."""
        await self._bus.publish(subject, message)

    def _install_signal_handlers(self) -> None:
        """Register SIGTERM/SIGINT handlers for graceful shutdown.

        When a signal is received, the actor finishes processing any in-flight
        messages before disconnecting from the bus. This prevents message loss
        during container restarts or manual stops.
        """
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._request_shutdown, sig)

    def _request_shutdown(self, sig: signal.Signals) -> None:
        """Signal callback — sets the shutdown event to break the message loop."""
        logger.info("actor.shutdown_requested", actor_id=self.actor_id, signal=sig.name)
        self._running = False
        if self._shutdown_event:
            self._shutdown_event.set()

    async def _process_one(self, data: dict[str, Any]) -> None:
        """Process a single message with semaphore-bounded concurrency."""
        ctx = extract_trace_context(data)
        async with self._semaphore:
            with _tracer.start_as_current_span(
                f"{type(self).__name__}.process",
                context=ctx,
                attributes={"actor.id": self.actor_id},
            ) as span:
                try:
                    start = time.monotonic()
                    await self.handle_message(data)
                    elapsed = int((time.monotonic() - start) * 1000)
                    span.set_attribute("processing.time_ms", elapsed)
                    logger.info("actor.processed", actor_id=self.actor_id, ms=elapsed)
                except Exception as e:
                    # Individual message failures don't kill the actor loop.
                    # The actor stays alive to process subsequent messages.
                    span.record_exception(e)
                    logger.error("actor.error", actor_id=self.actor_id, error=str(e))

    async def on_reload(self) -> None:  # noqa: B027 — intentional no-op default for ABC
        """Config reload hook — called when a control reload message arrives.

        Subclasses override this to re-read their config from disk.
        The default implementation is a no-op.
        """

    async def _run_control_listener(self) -> None:
        """Background task that listens for control messages.

        Subscribes to ``heddle.control.reload`` (broadcast to all actors).
        When a ``{"action": "reload"}`` message arrives, calls ``on_reload()``.
        """
        try:
            self._control_sub = await self._bus.subscribe("heddle.control.reload")
            logger.info("actor.control_subscribed", actor_id=self.actor_id)
            async for data in self._control_sub:
                if not self._running:
                    break
                action = data.get("action")
                if action == "reload":
                    logger.info("actor.reload_requested", actor_id=self.actor_id)
                    try:
                        await self.on_reload()
                        logger.info("actor.reload_completed", actor_id=self.actor_id)
                    except Exception as e:
                        logger.error("actor.reload_failed", actor_id=self.actor_id, error=str(e))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("actor.control_listener_error", actor_id=self.actor_id, error=str(e))

    async def run(self, subject: str, queue_group: str | None = None) -> None:
        """Main actor loop — subscribe, process messages, and handle shutdown.

        This method blocks until a shutdown signal (SIGTERM/SIGINT) is received
        or the bus connection drops. Messages are processed with bounded
        concurrency controlled by max_concurrent (default 1 = strict ordering).

        A background control listener subscribes to ``heddle.control.reload``
        to support hot-reloading of actor configs without restart.

        Graceful shutdown sequence:
        1. Signal received -> _request_shutdown() sets the shutdown event
        2. Message loop breaks after finishing in-flight messages
        3. Control listener is cancelled
        4. Actor disconnects from the bus (drains pending publishes)
        """
        self._shutdown_event = asyncio.Event()
        self._semaphore = asyncio.Semaphore(self.max_concurrent)

        await self.connect()
        await self.subscribe(subject, queue_group)
        self._running = True
        self._install_signal_handlers()

        # Start the control listener as a background task.
        control_task = asyncio.create_task(self._run_control_listener())

        logger.info(
            "actor.running",
            actor_id=self.actor_id,
            subject=subject,
            max_concurrent=self.max_concurrent,
        )

        try:
            async for data in self._sub:
                if not self._running:
                    break
                if self.max_concurrent == 1:
                    # Sequential processing — strict mailbox semantics
                    await self._process_one(data)
                else:
                    # Concurrent processing — fire-and-forget within semaphore bound
                    task = asyncio.create_task(self._process_one(data))
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)
        except asyncio.CancelledError:
            pass  # Clean shutdown via task cancellation
        finally:
            self._running = False
            control_task.cancel()

            # Drain in-flight handlers before disconnecting the bus.
            # ``max_concurrent > 1`` fires handlers as background tasks;
            # without this drain, ``self.disconnect()`` closes the bus
            # while handlers are mid-publish, dropping their results
            # and surfacing as caller timeouts.  Within the grace
            # window, handlers that started before shutdown finish
            # cleanly; anything still running after the grace is
            # force-cancelled and logged so operators can see what
            # was lost.
            if self._background_tasks:
                in_flight = list(self._background_tasks)
                logger.info(
                    "actor.shutdown_draining",
                    actor_id=self.actor_id,
                    in_flight=len(in_flight),
                    grace_seconds=self.shutdown_grace_seconds,
                )
                _, pending = await asyncio.wait(in_flight, timeout=self.shutdown_grace_seconds)
                if pending:
                    logger.warning(
                        "actor.shutdown_force_cancel",
                        actor_id=self.actor_id,
                        forced=len(pending),
                        grace_seconds=self.shutdown_grace_seconds,
                    )
                    for t in pending:
                        t.cancel()
                    # Await cancellations so we don't leak unfinished
                    # tasks past run()'s return.
                    await asyncio.gather(*pending, return_exceptions=True)

            await self.disconnect()

    @abstractmethod
    async def handle_message(self, data: dict[str, Any]) -> None:
        """Process a single message. Subclasses implement this."""
        ...
