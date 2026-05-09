"""Tests for BaseActor (core/actor.py)."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time
from unittest.mock import MagicMock, patch

import pytest

from heddle.bus.memory import InMemoryBus
from heddle.core.actor import BaseActor

# ---------------------------------------------------------------------------
# Concrete test subclasses
# ---------------------------------------------------------------------------


class EchoActor(BaseActor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.received: list[dict] = []

    async def handle_message(self, data):
        self.received.append(data)


class FailingActor(BaseActor):
    async def handle_message(self, data):
        raise RuntimeError("boom")


class SlowActor(BaseActor):
    """Records timestamps to verify sequential processing order."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.order: list[int] = []

    async def handle_message(self, data):
        self.order.append(data["seq"])
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# 1. Constructor with explicit bus uses that bus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_constructor_with_explicit_bus():
    bus = InMemoryBus()
    actor = EchoActor("test-1", bus=bus)
    assert actor._bus is bus


# ---------------------------------------------------------------------------
# 2. Constructor without bus creates a bus (NATSBus)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_constructor_without_bus_sets_bus():
    with patch("heddle.core.actor.NATSBus", create=True) as _:
        # The import happens lazily inside __init__; just verify _bus is set.
        from heddle.bus.nats_adapter import NATSBus  # noqa: F401

        actor = EchoActor("test-2", nats_url="nats://localhost:4222")
        assert actor._bus is not None


# ---------------------------------------------------------------------------
# 3. connect delegates to bus.connect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_delegates_to_bus():
    bus = InMemoryBus()
    actor = EchoActor("test-3", bus=bus)
    assert not bus._connected
    await actor.connect()
    assert bus._connected


# ---------------------------------------------------------------------------
# 4. disconnect calls bus.close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_calls_bus_close():
    bus = InMemoryBus()
    actor = EchoActor("test-4", bus=bus)
    await actor.connect()
    assert bus._connected
    await actor.disconnect()
    assert not bus._connected


# ---------------------------------------------------------------------------
# 5. subscribe delegates to bus.subscribe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_delegates_to_bus():
    bus = InMemoryBus()
    actor = EchoActor("test-5", bus=bus)
    await actor.subscribe("test.subject")
    assert actor._sub is not None
    assert actor._sub.subject == "test.subject"
    assert "test.subject" in bus._subscribers


# ---------------------------------------------------------------------------
# 6. publish delegates to bus.publish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_delegates_to_bus():
    bus = InMemoryBus()
    actor = EchoActor("test-6", bus=bus)
    sub = await bus.subscribe("out.subject")
    await actor.publish("out.subject", {"key": "value"})
    msg = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
    assert msg == {"key": "value"}


# ---------------------------------------------------------------------------
# 7. _process_one calls handle_message with data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_one_calls_handle_message():
    bus = InMemoryBus()
    actor = EchoActor("test-7", bus=bus)
    actor._semaphore = asyncio.Semaphore(1)
    await actor._process_one({"hello": "world"})
    assert actor.received == [{"hello": "world"}]


# ---------------------------------------------------------------------------
# 8. _process_one catches exceptions — actor stays alive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_one_catches_exceptions():
    bus = InMemoryBus()
    actor = FailingActor("test-8", bus=bus)
    actor._semaphore = asyncio.Semaphore(1)
    # Should not raise — the error is logged but swallowed.
    await actor._process_one({"will": "fail"})


# ---------------------------------------------------------------------------
# 9. _request_shutdown sets _running=False and sets shutdown_event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_shutdown_sets_flags():
    bus = InMemoryBus()
    actor = EchoActor("test-9", bus=bus)
    actor._running = True
    actor._shutdown_event = asyncio.Event()
    assert not actor._shutdown_event.is_set()

    actor._request_shutdown(signal.SIGTERM)

    assert actor._running is False
    assert actor._shutdown_event.is_set()


# ---------------------------------------------------------------------------
# 10. max_concurrent=1 processes sequentially (verify order)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sequential_processing_preserves_order():
    bus = InMemoryBus()
    actor = SlowActor("test-10", bus=bus, max_concurrent=1)
    actor._semaphore = asyncio.Semaphore(1)

    for i in range(5):
        await actor._process_one({"seq": i})

    assert actor.order == [0, 1, 2, 3, 4]


# ---------------------------------------------------------------------------
# 11. Semaphore has correct value for max_concurrent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semaphore_respects_max_concurrent():
    bus = InMemoryBus()
    actor = EchoActor("test-11", bus=bus, max_concurrent=3)
    # Semaphore is created in run(); simulate that.
    actor._semaphore = asyncio.Semaphore(actor.max_concurrent)
    assert actor._semaphore._value == 3


# ---------------------------------------------------------------------------
# 12. _process_one acquires and releases semaphore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_one_acquires_and_releases_semaphore():
    bus = InMemoryBus()
    actor = EchoActor("test-12", bus=bus)
    actor._semaphore = asyncio.Semaphore(1)

    # Before: semaphore is available (value=1).
    assert actor._semaphore._value == 1
    await actor._process_one({"data": 1})
    # After: semaphore is released back (value=1).
    assert actor._semaphore._value == 1
    assert actor.received == [{"data": 1}]


# ---------------------------------------------------------------------------
# 13. run() processes messages then disconnects on shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_processes_messages_and_shuts_down():
    """run() processes messages from the bus and disconnects on shutdown."""
    bus = InMemoryBus()
    actor = EchoActor("test-13", bus=bus)

    async def _feed_and_shutdown():
        # Wait for actor to subscribe
        for _ in range(50):
            if actor._running:
                break
            await asyncio.sleep(0.01)

        # Feed messages
        await bus.publish("test.run", {"msg": 1})
        await bus.publish("test.run", {"msg": 2})
        # Give time to process
        await asyncio.sleep(0.05)
        # Trigger shutdown — sets _running=False
        actor._request_shutdown(signal.SIGTERM)
        # Send one more message to unblock the subscription iterator
        # (async for waits for next message; the _running check only fires
        # when a message arrives)
        await asyncio.sleep(0.01)
        if actor._sub:
            await actor._sub.unsubscribe()

    with patch.object(actor, "_install_signal_handlers"):
        task = asyncio.create_task(actor.run("test.run"))
        feeder = asyncio.create_task(_feed_and_shutdown())
        await asyncio.wait_for(asyncio.gather(task, feeder), timeout=3.0)

    assert {"msg": 1} in actor.received
    assert {"msg": 2} in actor.received
    assert not bus._connected


# ---------------------------------------------------------------------------
# 14. run() with max_concurrent > 1 fires concurrent tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_concurrent_processing():
    """With max_concurrent > 1, multiple messages are processed concurrently."""
    bus = InMemoryBus()
    actor = SlowActor("test-14", bus=bus, max_concurrent=3)

    async def _feed_and_shutdown():
        for _ in range(50):
            if actor._running:
                break
            await asyncio.sleep(0.01)

        # Feed 3 messages quickly
        for i in range(3):
            await bus.publish("test.concurrent", {"seq": i})
        await asyncio.sleep(0.15)
        actor._request_shutdown(signal.SIGTERM)
        await asyncio.sleep(0.01)
        if actor._sub:
            await actor._sub.unsubscribe()

    with patch.object(actor, "_install_signal_handlers"):
        task = asyncio.create_task(actor.run("test.concurrent"))
        feeder = asyncio.create_task(_feed_and_shutdown())
        await asyncio.wait_for(asyncio.gather(task, feeder), timeout=5.0)

    # All 3 should have been processed
    assert sorted(actor.order) == [0, 1, 2]


# ---------------------------------------------------------------------------
# 15. run() handles CancelledError gracefully
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_handles_cancelled_error():
    """Cancelling the run task should not raise."""
    bus = InMemoryBus()
    actor = EchoActor("test-15", bus=bus)

    with patch.object(actor, "_install_signal_handlers"):
        task = asyncio.create_task(actor.run("test.cancel"))
        # Wait for actor to start
        for _ in range(50):
            if actor._running:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert not actor._running


# ---------------------------------------------------------------------------
# 16. run() disconnects even on error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_wakes_blocked_subscription_iterator():
    """Shutdown signal must wake an idle ``async for`` in run().

    ``_request_shutdown`` only flips ``_running`` and sets
    ``_shutdown_event``.  The message-loop's ``async for data in
    self._sub`` blocks on the iterator until a message arrives;
    without a wake-up race against the shutdown event, signaling
    SIGTERM into an idle actor leaves ``run()`` pending forever.

    The two existing concurrent-shutdown tests masked this bug by
    explicitly calling ``actor._sub.unsubscribe()`` after the signal
    — that ends the ``async for`` via ``StopAsyncIteration`` rather
    than exercising the production wake-up path.  This test does NOT
    unsubscribe.  Pre-fix, ``asyncio.wait_for(run_task, 1.0)`` raises
    ``TimeoutError``; post-fix, ``run()`` returns within tens of
    milliseconds.
    """
    bus = InMemoryBus()

    class IdleActor(BaseActor):
        async def handle_message(self, data):
            pass

    actor = IdleActor("idle-shutdown", bus=bus, max_concurrent=2)

    with patch.object(actor, "_install_signal_handlers"):
        run_task = asyncio.create_task(actor.run("test.idle"))
        for _ in range(50):
            if actor._running:
                break
            await asyncio.sleep(0.01)

        # Production path: SIGTERM hits the signal handler which calls
        # ``_request_shutdown``.  In tests we call it directly.  No
        # manual unsubscribe — that's the bug-masking step.
        actor._request_shutdown(signal.SIGTERM)

        # Poll for natural completion — do NOT use ``asyncio.wait_for``,
        # which would cancel the task on timeout and trigger the
        # ``except CancelledError: pass`` branch in ``run()``.  That
        # makes the task complete cleanly without ever exercising the
        # wake-up path, masking the bug.
        deadline = time.monotonic() + 1.0
        while not run_task.done() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)

        completed_naturally = run_task.done()

        # Cleanup: if run() didn't return on its own, end it now so
        # the test doesn't leak a pending task.
        if not run_task.done():
            run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run_task

    assert completed_naturally, (
        "run() did not return within 1s of _request_shutdown(); "
        "the subscription iterator was not woken up by the shutdown event"
    )
    assert not actor._running


@pytest.mark.asyncio
async def test_concurrent_shutdown_awaits_in_flight_tasks():
    """Shutdown drains in-flight handlers before disconnecting the bus.

    Pre-fix, ``run()``'s finally block disconnected the bus while
    ``max_concurrent>1`` background handler tasks were still running.
    Their ``handle_message`` calls (and any results they tried to
    publish) raced with bus shutdown — handlers either completed as
    orphans after ``run()`` returned, or hit close-related errors
    mid-publish.

    Post-fix, ``run()`` awaits ``_background_tasks`` with a configurable
    grace period before disconnecting.  Within the grace window,
    handlers that started before shutdown finish cleanly and their
    completion is observable BEFORE ``run()`` returns.
    """
    bus = InMemoryBus()

    class TimingActor(BaseActor):
        def __init__(self, *args, handler_delay: float = 0.2, **kwargs):
            super().__init__(*args, **kwargs)
            self.handler_finish_times: list[float] = []
            self.disconnect_time: float | None = None
            self._handlers_started = 0
            self.both_started = asyncio.Event()
            self._handler_delay = handler_delay

        async def handle_message(self, data):
            self._handlers_started += 1
            if self._handlers_started >= 2:
                self.both_started.set()
            await asyncio.sleep(self._handler_delay)
            self.handler_finish_times.append(time.monotonic())

        async def disconnect(self):
            self.disconnect_time = time.monotonic()
            await super().disconnect()

    actor = TimingActor("drain-test", bus=bus, max_concurrent=2, handler_delay=0.2)

    async def _feed_and_shutdown():
        for _ in range(50):
            if actor._running:
                break
            await asyncio.sleep(0.01)
        await bus.publish("test.drain", {"seq": 1})
        await bus.publish("test.drain", {"seq": 2})
        # Wait until both handlers are mid-sleep, THEN trigger shutdown.
        # This is the precondition the bug fires on: in-flight tasks
        # exist when ``finally`` runs.
        await actor.both_started.wait()
        actor._request_shutdown(signal.SIGTERM)
        # Unblock the message-loop iterator so finally can run.
        await asyncio.sleep(0.01)
        if actor._sub:
            await actor._sub.unsubscribe()

    with patch.object(actor, "_install_signal_handlers"):
        run_task = asyncio.create_task(actor.run("test.drain"))
        feeder = asyncio.create_task(_feed_and_shutdown())
        await asyncio.wait_for(asyncio.gather(run_task, feeder), timeout=5.0)

    # By the time run() returns, both in-flight handlers should have
    # completed.  Without the drain, they're orphan tasks that may not
    # have finished yet, so handler_finish_times length is < 2.
    assert len(actor.handler_finish_times) == 2, (
        f"Shutdown did not wait for in-flight handlers; only "
        f"{len(actor.handler_finish_times)}/2 had completed when "
        f"run() returned"
    )
    # Disconnect must have happened AFTER both handlers finished.
    assert actor.disconnect_time is not None
    assert actor.disconnect_time >= max(actor.handler_finish_times), (
        "Bus disconnected before in-flight handlers completed"
    )


@pytest.mark.asyncio
async def test_concurrent_shutdown_force_cancels_after_grace():
    """Handlers exceeding the grace timeout are cancelled and logged.

    A handler that doesn't finish within ``shutdown_grace_seconds``
    is force-cancelled so ``run()`` doesn't block forever.  The
    cancellation propagates as a ``CancelledError`` inside the
    handler; the actor logs ``actor.shutdown_force_cancel`` with the
    count of forced tasks.
    """
    bus = InMemoryBus()

    class HungActor(BaseActor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.handler_started = asyncio.Event()
            self.cancelled = False

        async def handle_message(self, data):
            self.handler_started.set()
            try:
                await asyncio.sleep(10.0)  # would exceed grace
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    actor = HungActor("force-cancel-test", bus=bus, max_concurrent=2, shutdown_grace_seconds=0.1)

    async def _feed_and_shutdown():
        for _ in range(50):
            if actor._running:
                break
            await asyncio.sleep(0.01)
        await bus.publish("test.force", {"seq": 1})
        await actor.handler_started.wait()
        actor._request_shutdown(signal.SIGTERM)
        await asyncio.sleep(0.01)
        if actor._sub:
            await actor._sub.unsubscribe()

    with patch.object(actor, "_install_signal_handlers"):
        run_task = asyncio.create_task(actor.run("test.force"))
        feeder = asyncio.create_task(_feed_and_shutdown())
        # Total wait must exceed grace (0.1s) but stay well under
        # the handler's 10s sleep — confirms the actor shut down
        # via grace timeout, not by waiting for the sleep.
        await asyncio.wait_for(asyncio.gather(run_task, feeder), timeout=2.0)

    assert actor.cancelled, "Hung handler was not force-cancelled after grace expired"


@pytest.mark.asyncio
async def test_run_disconnects_on_exit():
    """run() calls disconnect in finally block regardless of exit reason."""
    bus = InMemoryBus()
    actor = EchoActor("test-16", bus=bus)

    with patch.object(actor, "_install_signal_handlers"):
        task = asyncio.create_task(actor.run("test.disconnect"))
        for _ in range(50):
            if actor._running:
                break
            await asyncio.sleep(0.01)

        assert bus._connected
        actor._request_shutdown(signal.SIGTERM)
        # Unsubscribe to unblock the iteration loop
        await asyncio.sleep(0.01)
        if actor._sub:
            await actor._sub.unsubscribe()
        await asyncio.wait_for(task, timeout=3.0)

    # Bus should be disconnected after run() exits
    assert not bus._connected


# ---------------------------------------------------------------------------
# 17. _install_signal_handlers registers SIGTERM and SIGINT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_signal_handlers():
    """_install_signal_handlers registers handlers for SIGTERM and SIGINT."""
    bus = InMemoryBus()
    actor = EchoActor("test-17", bus=bus)

    mock_loop = MagicMock()
    with patch("asyncio.get_running_loop", return_value=mock_loop):
        actor._install_signal_handlers()

    assert mock_loop.add_signal_handler.call_count == 2
    registered_signals = {call.args[0] for call in mock_loop.add_signal_handler.call_args_list}
    assert signal.SIGTERM in registered_signals
    assert signal.SIGINT in registered_signals
