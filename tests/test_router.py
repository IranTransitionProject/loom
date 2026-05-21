"""
Unit tests for TaskRouter and TokenBucketRateLimiter (router/router.py).

Tests cover:
- TokenBucketRateLimiter: acquire, refill, unknown tier
- TaskRouter.resolve_tier: override, default, unknown
- TaskRouter.route: happy path, malformed message, rate-limited, dead-letter
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any

import pytest
import yaml

from heddle.bus.memory import InMemoryBus
from heddle.core.envelope import wrap
from heddle.core.messages import ModelTier, TaskMessage
from heddle.router.router import DEAD_LETTER_SUBJECT, TaskRouter, TokenBucketRateLimiter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_rules(rules: dict[str, Any]) -> str:
    """Write router_rules.yaml to a temp file and return the path."""
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as f:
        yaml.dump(rules, f)
    return path


def _make_task_data(
    worker_type: str = "summarizer",
    tier: str = "local",
    **overrides: Any,
) -> dict[str, Any]:
    """Create a minimal valid TaskMessage wire-envelope dict."""
    task = TaskMessage(
        worker_type=worker_type,
        input={"text": "hello"},
        model_tier=ModelTier(tier),
    )
    data = wrap("core.TaskMessage", task).model_dump(mode="json")
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# TokenBucketRateLimiter tests
# ---------------------------------------------------------------------------


class TestTokenBucketRateLimiter:
    def test_acquire_within_capacity(self):
        limiter = TokenBucketRateLimiter({"local": {"max_concurrent": 3}})
        assert limiter.try_acquire("local") is True
        assert limiter.try_acquire("local") is True
        assert limiter.try_acquire("local") is True

    def test_acquire_exhausted(self):
        limiter = TokenBucketRateLimiter({"local": {"max_concurrent": 1}})
        assert limiter.try_acquire("local") is True
        assert limiter.try_acquire("local") is False

    def test_unknown_tier_always_passes(self):
        limiter = TokenBucketRateLimiter({"local": {"max_concurrent": 1}})
        assert limiter.try_acquire("unknown_tier") is True

    def test_default_capacity(self):
        """Missing max_concurrent defaults to 10."""
        limiter = TokenBucketRateLimiter({"local": {}})
        for _ in range(10):
            assert limiter.try_acquire("local") is True

    def test_empty_rate_limits(self):
        limiter = TokenBucketRateLimiter({})
        assert limiter.try_acquire("local") is True

    def test_try_acquire_thread_safe(self):
        """G4: concurrent thread-pool calls don't over-issue tokens.

        Earlier ``try_acquire`` did a read-modify-write on the bucket
        dict without a lock.  Cooperative asyncio made it effectively
        atomic, but thread-pool callers (Workshop test_runner) could
        race.  The new ``threading.Lock`` serialises the check-then-
        decrement so the invariant ``count(True) <= capacity`` holds.
        """
        import concurrent.futures

        capacity = 100
        limiter = TokenBucketRateLimiter({"local": {"max_concurrent": capacity}})

        # Fire many more attempts than the capacity from many
        # threads; the count of ``True`` returns must be exactly
        # ``capacity`` (not more — that's the race the lock closes).
        attempts = capacity * 4
        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as ex:
            results = list(ex.map(lambda _i: limiter.try_acquire("local"), range(attempts)))
        granted = sum(1 for r in results if r)
        assert granted == capacity


# ---------------------------------------------------------------------------
# TaskRouter.resolve_tier tests
# ---------------------------------------------------------------------------


class TestResolveTier:
    def test_no_override_uses_task_tier(self):
        rules_path = _write_rules({"tier_overrides": {}})
        try:
            router = TaskRouter(rules_path, InMemoryBus())
            task = TaskMessage(worker_type="summarizer", input={}, model_tier=ModelTier.LOCAL)
            assert router.resolve_tier(task) == ModelTier.LOCAL
        finally:
            os.unlink(rules_path)

    def test_override_takes_precedence(self):
        rules_path = _write_rules({"tier_overrides": {"summarizer": "frontier"}})
        try:
            router = TaskRouter(rules_path, InMemoryBus())
            task = TaskMessage(worker_type="summarizer", input={}, model_tier=ModelTier.LOCAL)
            assert router.resolve_tier(task) == ModelTier.FRONTIER
        finally:
            os.unlink(rules_path)

    def test_invalid_override_raises(self):
        rules_path = _write_rules({"tier_overrides": {"summarizer": "invalid_tier"}})
        try:
            router = TaskRouter(rules_path, InMemoryBus())
            task = TaskMessage(worker_type="summarizer", input={}, model_tier=ModelTier.LOCAL)
            with pytest.raises(ValueError):
                router.resolve_tier(task)
        finally:
            os.unlink(rules_path)


# ---------------------------------------------------------------------------
# TaskRouter.route tests
# ---------------------------------------------------------------------------


class TestRoute:
    @pytest.fixture
    def bus(self):
        return InMemoryBus()

    @pytest.fixture
    def rules_path(self):
        path = _write_rules(
            {
                "tier_overrides": {},
                "rate_limits": {
                    "local": {"max_concurrent": 10},
                    "standard": {"max_concurrent": 5},
                },
            }
        )
        yield path
        os.unlink(path)

    @pytest.mark.asyncio
    async def test_route_happy_path(self, bus, rules_path):
        """Valid task routed to correct subject."""
        router = TaskRouter(rules_path, bus)
        await bus.connect()

        # Subscribe to the expected destination
        sub = await bus.subscribe("heddle.tasks.summarizer.local")
        data = _make_task_data("summarizer", "local")

        await router.route(data)

        msg = await sub.__anext__()
        assert msg["payload"]["worker_type"] == "summarizer"

    @pytest.mark.asyncio
    async def test_route_malformed_message_dead_letters(self, bus, rules_path):
        """Invalid message goes to dead letter."""
        router = TaskRouter(rules_path, bus)
        await bus.connect()

        dl_sub = await bus.subscribe(DEAD_LETTER_SUBJECT)

        await router.route({"garbage": True})  # Missing required fields

        msg = await dl_sub.__anext__()
        assert "invalid_task_message" in msg["reason"]

    @pytest.mark.asyncio
    async def test_route_rate_limited_dead_letters(self, bus):
        """Rate-limited task goes to dead letter."""
        rules_path = _write_rules(
            {
                "tier_overrides": {},
                "rate_limits": {"local": {"max_concurrent": 1}},
            }
        )
        try:
            router = TaskRouter(rules_path, bus)
            await bus.connect()

            dl_sub = await bus.subscribe(DEAD_LETTER_SUBJECT)
            dest_sub = await bus.subscribe("heddle.tasks.summarizer.local")

            data1 = _make_task_data("summarizer", "local")
            data2 = _make_task_data("summarizer", "local")

            # First should succeed
            await router.route(data1)
            msg1 = await dest_sub.__anext__()
            assert msg1["payload"]["worker_type"] == "summarizer"

            # Second should be rate limited
            await router.route(data2)
            dl_msg = await dl_sub.__anext__()
            assert "rate_limited" in dl_msg["reason"]
        finally:
            os.unlink(rules_path)

    @pytest.mark.asyncio
    async def test_route_with_tier_override(self, bus):
        """Tier override redirects task to different tier subject."""
        rules_path = _write_rules(
            {
                "tier_overrides": {"summarizer": "frontier"},
                "rate_limits": {},
            }
        )
        try:
            router = TaskRouter(rules_path, bus)
            await bus.connect()

            sub = await bus.subscribe("heddle.tasks.summarizer.frontier")
            data = _make_task_data("summarizer", "local")  # Task says local

            await router.route(data)

            msg = await sub.__anext__()
            assert msg["payload"]["worker_type"] == "summarizer"
            assert msg["payload"]["model_tier"] == "local"  # Original tier preserved in message
        finally:
            os.unlink(rules_path)

    @pytest.mark.asyncio
    async def test_dead_letter_message_structure(self, bus, rules_path):
        """Verify dead-letter message contains expected fields."""
        router = TaskRouter(rules_path, bus)
        await bus.connect()

        dl_sub = await bus.subscribe(DEAD_LETTER_SUBJECT)
        await router.route({"bad": "data"})

        msg = await dl_sub.__anext__()
        assert "reason" in msg
        assert "original_task" in msg
        assert msg["original_task"] == {"bad": "data"}


# ---------------------------------------------------------------------------
# TaskRouter.run and process_messages tests
# ---------------------------------------------------------------------------


class TestRunAndProcessMessages:
    @pytest.mark.asyncio
    async def test_run_connects_and_subscribes(self):
        """run() connects the bus and subscribes to heddle.tasks.incoming."""
        rules_path = _write_rules({"tier_overrides": {}, "rate_limits": {}})
        try:
            bus = InMemoryBus()
            router = TaskRouter(rules_path, bus)
            await router.run()

            assert bus._connected
            assert router._sub is not None
            assert router._sub.subject == "heddle.tasks.incoming"
            await bus.close()
        finally:
            os.unlink(rules_path)

    @pytest.mark.asyncio
    async def test_process_messages_routes_multiple_tasks(self):
        """process_messages() routes all incoming tasks until cancelled."""
        rules_path = _write_rules({"tier_overrides": {}, "rate_limits": {}})
        try:
            bus = InMemoryBus()
            router = TaskRouter(rules_path, bus)
            await router.run()

            # Subscribe to the destination
            dest_sub = await bus.subscribe("heddle.tasks.summarizer.local")
            dest_sub2 = await bus.subscribe("heddle.tasks.classifier.standard")

            # Start processing in background
            process_task = asyncio.create_task(router.process_messages())

            # Publish tasks
            await bus.publish("heddle.tasks.incoming", _make_task_data("summarizer", "local"))
            await bus.publish("heddle.tasks.incoming", _make_task_data("classifier", "standard"))

            # Collect results
            msg1 = await asyncio.wait_for(dest_sub.__anext__(), timeout=2.0)
            msg2 = await asyncio.wait_for(dest_sub2.__anext__(), timeout=2.0)

            assert msg1["payload"]["worker_type"] == "summarizer"
            assert msg2["payload"]["worker_type"] == "classifier"

            # Clean up
            process_task.cancel()
            try:
                await process_task
            except asyncio.CancelledError:
                pass
            await bus.close()
        finally:
            os.unlink(rules_path)

    @pytest.mark.asyncio
    async def test_route_invalid_tier_override_dead_letters(self):
        """Invalid tier override sends task to dead letter."""
        rules_path = _write_rules(
            {
                "tier_overrides": {"summarizer": "nonexistent_tier"},
                "rate_limits": {},
            }
        )
        try:
            bus = InMemoryBus()
            router = TaskRouter(rules_path, bus)
            await bus.connect()

            dl_sub = await bus.subscribe(DEAD_LETTER_SUBJECT)
            data = _make_task_data("summarizer", "local")
            await router.route(data)

            msg = await dl_sub.__anext__()
            assert "unknown_tier" in msg["reason"]
            await bus.close()
        finally:
            os.unlink(rules_path)


# ---------------------------------------------------------------------------
# Queue-group / horizontal-scaling regression
#
# Without the ``router`` queue group, every router replica subscribes
# as an independent listener and receives every task — producing N-fold
# duplicate dispatch (and N-fold rate-limit consumption).  The fix is
# to subscribe with a queue group so NATS picks exactly one replica
# per message via round-robin.
#
# We rely on InMemoryBus's queue-group implementation, which mirrors
# NATS semantics: members of a group compete for messages in
# round-robin order.
# ---------------------------------------------------------------------------


class TestRouterQueueGroup:
    @pytest.mark.asyncio
    async def test_run_subscribes_with_router_queue_group(self):
        """``run()`` must subscribe to ``heddle.tasks.incoming`` with the
        ``router`` queue group.

        Pinning the actual queue group name guards against accidental
        rename — replicas using different group names form independent
        listener pools, restoring the bug.
        """
        from unittest.mock import AsyncMock

        rules_path = _write_rules({"tier_overrides": {}, "rate_limits": {}})
        try:
            bus = InMemoryBus()
            router = TaskRouter(rules_path, bus)
            # Wrap subscribe so we can inspect what was passed.
            real_subscribe = bus.subscribe
            calls: list[tuple[str, str | None]] = []

            async def spy_subscribe(subject: str, queue_group: str | None = None):
                calls.append((subject, queue_group))
                return await real_subscribe(subject, queue_group)

            bus.subscribe = AsyncMock(side_effect=spy_subscribe)

            await router.run()
            await bus.close()

            assert ("heddle.tasks.incoming", "router") in calls, (
                f"router must subscribe with queue_group='router'; got {calls}"
            )
        finally:
            os.unlink(rules_path)

    @pytest.mark.asyncio
    async def test_two_routers_each_task_dispatched_once(self):
        """Two routers in the same queue group dispatch each task exactly once.

        End-to-end regression test: publish one task to
        ``heddle.tasks.incoming`` while two router replicas are
        subscribed.  With the queue-group fix, NATS delivers to one
        replica only, so the destination subject sees exactly one
        forwarded message.  Without it, both replicas forward → the
        destination sees two duplicates.
        """
        rules_path = _write_rules({"tier_overrides": {}, "rate_limits": {}})
        try:
            bus = InMemoryBus()
            await bus.connect()
            router_a = TaskRouter(rules_path, bus)
            router_b = TaskRouter(rules_path, bus)

            await router_a.run()
            await router_b.run()

            dest_sub = await bus.subscribe("heddle.tasks.summarizer.local")

            # Both routers compete to consume the next incoming task.
            proc_a = asyncio.create_task(router_a.process_messages())
            proc_b = asyncio.create_task(router_b.process_messages())

            # Yield so the routers actually subscribe before we publish.
            await asyncio.sleep(0)

            data = _make_task_data("summarizer", "local")
            await bus.publish("heddle.tasks.incoming", data)

            # First dispatched message must arrive — collect it.
            first = await asyncio.wait_for(dest_sub.__anext__(), timeout=2.0)
            assert first["payload"]["worker_type"] == "summarizer"

            # No SECOND dispatch — assert by waiting briefly for another
            # message and confirming none arrives (timeout).
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(dest_sub.__anext__(), timeout=0.2)

            # Cleanup.
            proc_a.cancel()
            proc_b.cancel()
            for t in (proc_a, proc_b):
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            await bus.close()
        finally:
            os.unlink(rules_path)
