"""
Unit tests for SchedulerActor (scheduler/scheduler.py) and config
validation (scheduler/config.py).

Tests cover:
- Config validation: required keys, mutual exclusivity, dispatch types
- Schedule parsing: cron vs interval entries, goal/task config
- Dispatch: goal → heddle.goals.incoming, task → heddle.tasks.incoming
- Timer loop: interval firing, next_fire advancement, shutdown
- Lifecycle: handle_message no-op, run/shutdown

All tests use InMemoryBus -- no NATS or external infrastructure required.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time

import pytest
import yaml

from heddle.bus.memory import InMemoryBus
from heddle.core.messages import (
    ModelTier,
    OrchestratorGoal,
    TaskMessage,
    TaskPriority,
)
from heddle.scheduler.config import validate_scheduler_config
from heddle.scheduler.scheduler import ScheduleEntry, SchedulerActor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path, schedules=None, name="test-scheduler"):
    """Write a minimal scheduler config to a temp file."""
    config = {
        "name": name,
        "schedules": schedules or [],
    }
    config_file = tmp_path / "scheduler.yaml"
    config_file.write_text(yaml.dump(config))
    return str(config_file)


def _goal_schedule(
    name="test_goal",
    cron=None,
    interval_seconds=None,
    instruction="Do something",
    context=None,
    priority="normal",
):
    """Build a goal schedule entry dict."""
    entry = {
        "name": name,
        "dispatch_type": "goal",
        "goal": {
            "instruction": instruction,
            "context": context or {},
            "priority": priority,
        },
    }
    if cron is not None:
        entry["cron"] = cron
    if interval_seconds is not None:
        entry["interval_seconds"] = interval_seconds
    return entry


def _task_schedule(
    name="test_task",
    cron=None,
    interval_seconds=None,
    worker_type="summarizer",
    payload=None,
    model_tier="local",
    priority="normal",
):
    """Build a task schedule entry dict."""
    entry = {
        "name": name,
        "dispatch_type": "task",
        "task": {
            "worker_type": worker_type,
            "payload": payload or {},
            "model_tier": model_tier,
            "priority": priority,
        },
    }
    if cron is not None:
        entry["cron"] = cron
    if interval_seconds is not None:
        entry["interval_seconds"] = interval_seconds
    return entry


# ===========================================================================
# Config validation tests
# ===========================================================================


class TestValidateSchedulerConfig:
    """Tests for validate_scheduler_config()."""

    def test_valid_cron_schedule(self):
        config = {
            "name": "sched",
            "schedules": [_goal_schedule(cron="0 * * * *")],
        }
        assert validate_scheduler_config(config) == []

    def test_valid_interval_schedule(self):
        config = {
            "name": "sched",
            "schedules": [_task_schedule(interval_seconds=60)],
        }
        assert validate_scheduler_config(config) == []

    def test_empty_schedules_is_valid(self):
        config = {"name": "sched", "schedules": []}
        assert validate_scheduler_config(config) == []

    def test_missing_name_key(self):
        config = {"schedules": []}
        errors = validate_scheduler_config(config)
        assert any("missing required key 'name'" in e for e in errors)

    def test_missing_schedules_key(self):
        config = {"name": "sched"}
        errors = validate_scheduler_config(config)
        assert any("missing required key 'schedules'" in e for e in errors)

    def test_not_a_dict(self):
        errors = validate_scheduler_config("not a dict")
        assert len(errors) == 1
        assert "expected dict" in errors[0]

    def test_schedule_entry_missing_name(self):
        config = {
            "name": "sched",
            "schedules": [
                {"dispatch_type": "goal", "cron": "* * * * *", "goal": {"instruction": "x"}}
            ],
        }
        errors = validate_scheduler_config(config)
        assert any("missing required key 'name'" in e for e in errors)

    def test_schedule_entry_missing_dispatch_type(self):
        config = {
            "name": "sched",
            "schedules": [{"name": "x", "cron": "* * * * *"}],
        }
        errors = validate_scheduler_config(config)
        assert any("missing required key 'dispatch_type'" in e for e in errors)

    def test_both_cron_and_interval(self):
        entry = _goal_schedule(cron="0 * * * *", interval_seconds=60)
        config = {"name": "sched", "schedules": [entry]}
        errors = validate_scheduler_config(config)
        assert any("cannot specify both" in e for e in errors)

    def test_neither_cron_nor_interval(self):
        entry = {"name": "x", "dispatch_type": "goal", "goal": {"instruction": "y"}}
        config = {"name": "sched", "schedules": [entry]}
        errors = validate_scheduler_config(config)
        assert any("must specify either" in e for e in errors)

    def test_invalid_dispatch_type(self):
        entry = {"name": "x", "dispatch_type": "invalid", "cron": "* * * * *"}
        config = {"name": "sched", "schedules": [entry]}
        errors = validate_scheduler_config(config)
        assert any("must be 'goal' or 'task'" in e for e in errors)

    def test_goal_dispatch_missing_goal_config(self):
        entry = {"name": "x", "dispatch_type": "goal", "cron": "* * * * *"}
        config = {"name": "sched", "schedules": [entry]}
        errors = validate_scheduler_config(config)
        assert any("requires a 'goal' dict" in e for e in errors)

    def test_goal_dispatch_missing_instruction(self):
        entry = {"name": "x", "dispatch_type": "goal", "cron": "* * * * *", "goal": {"context": {}}}
        config = {"name": "sched", "schedules": [entry]}
        errors = validate_scheduler_config(config)
        assert any("missing 'instruction'" in e for e in errors)

    def test_task_dispatch_missing_task_config(self):
        entry = {"name": "x", "dispatch_type": "task", "cron": "* * * * *"}
        config = {"name": "sched", "schedules": [entry]}
        errors = validate_scheduler_config(config)
        assert any("requires a 'task' dict" in e for e in errors)

    def test_task_dispatch_missing_worker_type(self):
        entry = {"name": "x", "dispatch_type": "task", "cron": "* * * * *", "task": {"payload": {}}}
        config = {"name": "sched", "schedules": [entry]}
        errors = validate_scheduler_config(config)
        assert any("missing 'worker_type'" in e for e in errors)

    def test_invalid_cron_expression(self):
        entry = _goal_schedule(cron="not a cron")
        config = {"name": "sched", "schedules": [entry]}
        errors = validate_scheduler_config(config)
        assert any("invalid cron expression" in e for e in errors)

    def test_negative_interval(self):
        entry = _task_schedule(interval_seconds=-5)
        config = {"name": "sched", "schedules": [entry]}
        errors = validate_scheduler_config(config)
        assert any("positive number" in e for e in errors)

    def test_zero_interval(self):
        entry = _task_schedule(interval_seconds=0)
        config = {"name": "sched", "schedules": [entry]}
        errors = validate_scheduler_config(config)
        assert any("positive number" in e for e in errors)

    def test_entry_not_a_dict(self):
        config = {"name": "sched", "schedules": ["not a dict"]}
        errors = validate_scheduler_config(config)
        assert any("expected dict" in e for e in errors)


# ===========================================================================
# Schedule parsing tests
# ===========================================================================


class TestParseSchedules:
    """Tests for SchedulerActor._parse_schedules()."""

    def test_parse_cron_entry(self):
        raw = [_goal_schedule(name="cron_test", cron="0 9 * * *")]
        entries = SchedulerActor._parse_schedules(raw)
        assert len(entries) == 1
        assert entries[0].name == "cron_test"
        assert entries[0].cron == "0 9 * * *"
        assert entries[0].interval_seconds is None
        assert entries[0].dispatch_type == "goal"

    def test_parse_interval_entry(self):
        raw = [_task_schedule(name="int_test", interval_seconds=120)]
        entries = SchedulerActor._parse_schedules(raw)
        assert len(entries) == 1
        assert entries[0].interval_seconds == 120
        assert entries[0].cron is None
        assert entries[0].dispatch_type == "task"

    def test_parse_goal_config(self):
        raw = [_goal_schedule(cron="* * * * *", instruction="test instr", context={"k": "v"})]
        entries = SchedulerActor._parse_schedules(raw)
        assert entries[0].goal_config["instruction"] == "test instr"
        assert entries[0].goal_config["context"] == {"k": "v"}

    def test_parse_task_config(self):
        raw = [_task_schedule(cron="* * * * *", worker_type="checker", payload={"x": 1})]
        entries = SchedulerActor._parse_schedules(raw)
        assert entries[0].task_config["worker_type"] == "checker"
        assert entries[0].task_config["payload"] == {"x": 1}

    def test_parse_multiple_entries(self):
        raw = [
            _goal_schedule(name="a", cron="0 * * * *"),
            _task_schedule(name="b", interval_seconds=30),
        ]
        entries = SchedulerActor._parse_schedules(raw)
        assert len(entries) == 2
        assert entries[0].name == "a"
        assert entries[1].name == "b"


# ===========================================================================
# Dispatch tests
# ===========================================================================


class TestDispatch:
    """Tests for _dispatch_goal() and _dispatch_task()."""

    @pytest.fixture
    def actor(self, tmp_path):
        """Create a SchedulerActor with InMemoryBus."""
        config_file = _write_config(tmp_path)
        bus = InMemoryBus()
        return SchedulerActor("test-sched", config_file, bus=bus)

    @pytest.mark.asyncio
    async def test_dispatch_goal_publishes_to_goals_incoming(self, actor):
        await actor._bus.connect()
        sub = await actor._bus.subscribe("heddle.goals.incoming")

        entry = ScheduleEntry(
            name="g1",
            cron=None,
            interval_seconds=60,
            dispatch_type="goal",
            goal_config={"instruction": "Test goal", "context": {"a": 1}},
        )
        await actor._dispatch_goal(entry)

        msg = await sub.__anext__()
        goal = OrchestratorGoal(**msg)
        assert goal.instruction == "Test goal"
        assert goal.context == {"a": 1}
        assert goal.priority == TaskPriority.NORMAL

    @pytest.mark.asyncio
    async def test_dispatch_goal_with_priority(self, actor):
        await actor._bus.connect()
        sub = await actor._bus.subscribe("heddle.goals.incoming")

        entry = ScheduleEntry(
            name="g2",
            cron=None,
            interval_seconds=60,
            dispatch_type="goal",
            goal_config={"instruction": "High pri", "priority": "high"},
        )
        await actor._dispatch_goal(entry)

        msg = await sub.__anext__()
        goal = OrchestratorGoal(**msg)
        assert goal.priority == TaskPriority.HIGH

    @pytest.mark.asyncio
    async def test_dispatch_goal_generates_unique_ids(self, actor):
        await actor._bus.connect()
        sub = await actor._bus.subscribe("heddle.goals.incoming")

        entry = ScheduleEntry(
            name="g3",
            cron=None,
            interval_seconds=60,
            dispatch_type="goal",
            goal_config={"instruction": "Repeat"},
        )
        await actor._dispatch_goal(entry)
        await actor._dispatch_goal(entry)

        msg1 = await sub.__anext__()
        msg2 = await sub.__anext__()
        assert msg1["goal_id"] != msg2["goal_id"]

    @pytest.mark.asyncio
    async def test_dispatch_task_publishes_to_tasks_incoming(self, actor):
        await actor._bus.connect()
        sub = await actor._bus.subscribe("heddle.tasks.incoming")

        entry = ScheduleEntry(
            name="t1",
            cron=None,
            interval_seconds=60,
            dispatch_type="task",
            task_config={
                "worker_type": "summarizer",
                "payload": {"text": "hello"},
                "model_tier": "local",
            },
        )
        await actor._dispatch_task(entry)

        msg = await sub.__anext__()
        task = TaskMessage(**msg)
        assert task.worker_type == "summarizer"
        assert task.payload == {"text": "hello"}
        assert task.model_tier == ModelTier.LOCAL

    @pytest.mark.asyncio
    async def test_dispatch_task_includes_schedule_metadata(self, actor):
        await actor._bus.connect()
        sub = await actor._bus.subscribe("heddle.tasks.incoming")

        entry = ScheduleEntry(
            name="meta_test",
            cron=None,
            interval_seconds=60,
            dispatch_type="task",
            task_config={"worker_type": "w", "payload": {}},
        )
        await actor._dispatch_task(entry)

        msg = await sub.__anext__()
        assert msg["metadata"]["scheduled_by"] == "meta_test"

    @pytest.mark.asyncio
    async def test_dispatch_task_with_priority_and_tier(self, actor):
        await actor._bus.connect()
        sub = await actor._bus.subscribe("heddle.tasks.incoming")

        entry = ScheduleEntry(
            name="t2",
            cron=None,
            interval_seconds=60,
            dispatch_type="task",
            task_config={
                "worker_type": "w",
                "payload": {},
                "model_tier": "standard",
                "priority": "critical",
            },
        )
        await actor._dispatch_task(entry)

        msg = await sub.__anext__()
        task = TaskMessage(**msg)
        assert task.model_tier == ModelTier.STANDARD
        assert task.priority == TaskPriority.CRITICAL

    @pytest.mark.asyncio
    async def test_fire_schedule_routes_goal(self, actor):
        await actor._bus.connect()
        sub = await actor._bus.subscribe("heddle.goals.incoming")

        entry = ScheduleEntry(
            name="route_goal",
            cron=None,
            interval_seconds=60,
            dispatch_type="goal",
            goal_config={"instruction": "routed"},
        )
        await actor._fire_schedule(entry)

        msg = await sub.__anext__()
        assert msg["instruction"] == "routed"

    @pytest.mark.asyncio
    async def test_fire_schedule_routes_task(self, actor):
        await actor._bus.connect()
        sub = await actor._bus.subscribe("heddle.tasks.incoming")

        entry = ScheduleEntry(
            name="route_task",
            cron=None,
            interval_seconds=60,
            dispatch_type="task",
            task_config={"worker_type": "w", "payload": {}},
        )
        await actor._fire_schedule(entry)

        msg = await sub.__anext__()
        assert msg["worker_type"] == "w"

    @pytest.mark.asyncio
    async def test_fire_schedule_error_does_not_crash(self, actor):
        """An exception in dispatch is caught, not propagated."""
        await actor._bus.connect()

        entry = ScheduleEntry(
            name="bad",
            cron=None,
            interval_seconds=60,
            dispatch_type="task",
            task_config=None,  # Will cause KeyError in _dispatch_task
        )
        # Should not raise
        await actor._fire_schedule(entry)


# ===========================================================================
# Timer loop and fire time tests
# ===========================================================================


class TestTimerAndFireTimes:
    """Tests for _initialize_fire_times, _advance_next_fire, _timer_loop."""

    def test_initialize_fire_times_interval(self, tmp_path):
        schedules = [_task_schedule(interval_seconds=120)]
        config_file = _write_config(tmp_path, schedules)
        actor = SchedulerActor("s1", config_file, bus=InMemoryBus())

        before = time.monotonic()
        actor._initialize_fire_times()
        after = time.monotonic()

        entry = actor._schedules[0]
        assert before + 120 <= entry.next_fire <= after + 120

    def test_initialize_fire_times_cron(self, tmp_path):
        schedules = [_goal_schedule(cron="0 * * * *")]
        config_file = _write_config(tmp_path, schedules)
        actor = SchedulerActor("s2", config_file, bus=InMemoryBus())

        before = time.monotonic()
        actor._initialize_fire_times()

        entry = actor._schedules[0]
        # Cron "0 * * * *" fires at top of hour — next fire is at most ~3600s away
        assert entry.next_fire > before
        assert entry.next_fire <= before + 3600 + 1

    def test_advance_next_fire_interval(self, tmp_path):
        """Advance increments by exactly ``interval_seconds`` (no drift).

        ``_advance_next_fire`` is now relative to the prior ``next_fire``,
        not to ``time.monotonic()``.  Resetting from ``now`` would add
        the timer-loop sleep + dispatch latency to every cycle, drifting
        the schedule indefinitely.  This test pins the increment to the
        exact interval.
        """
        schedules = [_task_schedule(interval_seconds=30)]
        config_file = _write_config(tmp_path, schedules)
        actor = SchedulerActor("s3", config_file, bus=InMemoryBus())

        entry = actor._schedules[0]
        # Seed a realistic prior fire time slightly in the future so the
        # catch-up branch is not exercised here (covered separately).
        entry.next_fire = time.monotonic() + 5.0
        prev = entry.next_fire

        actor._advance_next_fire(entry)

        assert entry.next_fire == prev + 30.0, (
            f"interval drift: expected {prev + 30.0}, got {entry.next_fire}"
        )

    def test_advance_next_fire_no_drift_over_many_cycles(self, tmp_path):
        """Drift regression: 1000 cycles of an interval schedule should
        produce a fire-time exactly N * interval past the start.

        Pre-fix shape (``next_fire = now + interval``) added the
        sample-clock latency to every iteration, so an "every 60s"
        schedule could drift to >60s and accumulate over time.  The
        fixed shape (``next_fire += interval``) keeps cycle time
        exactly ``interval_seconds`` regardless of dispatch latency.
        """
        schedules = [_task_schedule(interval_seconds=0.5)]
        config_file = _write_config(tmp_path, schedules)
        actor = SchedulerActor("s_drift", config_file, bus=InMemoryBus())

        entry = actor._schedules[0]
        # Start far in the future so no catch-up branch fires.
        start = time.monotonic() + 10_000.0
        entry.next_fire = start

        for _ in range(1000):
            actor._advance_next_fire(entry)

        # 1000 increments of 0.5 = 500.0 exactly.  No tolerance — the
        # whole point is that there is no drift to bound.
        assert entry.next_fire == start + 500.0, (
            f"interval schedule drifted: expected {start + 500.0}, "
            f"got {entry.next_fire} ({entry.next_fire - (start + 500.0)} off)"
        )

    def test_advance_next_fire_catches_up_after_stall(self, tmp_path):
        """If the actor stalled and is several intervals behind, the next
        fire jumps to the next future slot rather than firing back-to-back.

        Avoids flooding downstream consumers after a debugger pause or
        a slow handler.
        """
        schedules = [_task_schedule(interval_seconds=10)]
        config_file = _write_config(tmp_path, schedules)
        actor = SchedulerActor("s_catchup", config_file, bus=InMemoryBus())

        entry = actor._schedules[0]
        now = time.monotonic()
        entry.next_fire = now - 95.0  # ~9 intervals behind

        actor._advance_next_fire(entry)

        # New next_fire must be in the future, not ~9 increments back.
        assert entry.next_fire > now
        # And must align with the original cadence (some integer number of
        # intervals past the stale next_fire).  ``time.monotonic()`` is a
        # float, so the subtraction here accumulates ~1e-14 of rounding;
        # round before the modulo to keep the assertion deterministic
        # across CI runners (this previously flaked on GitHub Actions
        # 3.11 with ``offset == 100.00000000000001``).
        offset = entry.next_fire - (now - 95.0)
        assert round(offset, 6) % 10 == 0, (
            f"catch-up broke cadence: offset {offset} not a multiple of 10"
        )

    def test_advance_next_fire_cron(self, tmp_path):
        schedules = [_goal_schedule(cron="*/5 * * * *")]
        config_file = _write_config(tmp_path, schedules)
        actor = SchedulerActor("s4", config_file, bus=InMemoryBus())

        entry = actor._schedules[0]
        before = time.monotonic()
        actor._advance_next_fire(entry)

        # "*/5 * * * *" = every 5 minutes, next fire is at most ~300s away
        assert entry.next_fire > before
        assert entry.next_fire <= before + 300 + 1

    @pytest.mark.asyncio
    async def test_timer_loop_fires_interval(self, tmp_path):
        """Verify the timer loop fires an interval schedule."""
        schedules = [_task_schedule(name="fast", interval_seconds=0.05)]
        config_file = _write_config(tmp_path, schedules)
        bus = InMemoryBus()
        await bus.connect()
        actor = SchedulerActor("s5", config_file, bus=bus)

        sub = await bus.subscribe("heddle.tasks.incoming")

        # Set next_fire to now (so it fires immediately)
        actor._schedules[0].next_fire = time.monotonic()
        actor._running = True

        # Run timer loop briefly
        async def run_briefly():
            task = asyncio.create_task(actor._timer_loop())
            await asyncio.sleep(0.15)
            actor._running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await run_briefly()

        # Should have fired at least once
        msg = await sub.__anext__()
        task_msg = TaskMessage(**msg)
        assert task_msg.worker_type == "summarizer"

    @pytest.mark.asyncio
    async def test_timer_loop_respects_running_flag(self, tmp_path):
        """Timer loop exits when _running is set to False."""
        config_file = _write_config(tmp_path)
        actor = SchedulerActor("s6", config_file, bus=InMemoryBus())
        actor._running = False

        # Should exit almost immediately
        await asyncio.wait_for(actor._timer_loop(), timeout=2.0)


# ===========================================================================
# handle_message and lifecycle tests
# ===========================================================================


class TestHandleMessage:
    """Tests for handle_message() no-op."""

    @pytest.mark.asyncio
    async def test_handle_message_is_noop(self, tmp_path):
        config_file = _write_config(tmp_path)
        actor = SchedulerActor("s7", config_file, bus=InMemoryBus())
        # Should not raise or publish anything
        await actor.handle_message({"ping": True})


class TestLifecycle:
    """Tests for SchedulerActor run/shutdown lifecycle."""

    @pytest.mark.asyncio
    async def test_shutdown_cancels_timer_task(self, tmp_path):
        """Simulate shutdown and verify timer task is cancelled."""
        config_file = _write_config(tmp_path, [_task_schedule(interval_seconds=60)])
        bus = InMemoryBus()
        await bus.connect()
        actor = SchedulerActor("s8", config_file, bus=bus)

        # Manually set up the actor state as run() would
        actor._shutdown_event = asyncio.Event()
        actor._semaphore = asyncio.Semaphore(1)
        actor._running = True
        actor._initialize_fire_times()
        actor._timer_task = asyncio.create_task(actor._timer_loop())

        # Verify timer is running
        assert not actor._timer_task.done()

        # Request shutdown
        actor._running = False
        actor._timer_task.cancel()
        try:
            await actor._timer_task
        except asyncio.CancelledError:
            pass

        assert actor._timer_task.done()

    def test_actor_loads_config(self, tmp_path):
        """Actor correctly loads and parses YAML config."""
        schedules = [
            _goal_schedule(name="a", cron="0 * * * *"),
            _task_schedule(name="b", interval_seconds=300),
        ]
        config_file = _write_config(tmp_path, schedules, name="my-sched")
        actor = SchedulerActor("s9", config_file, bus=InMemoryBus())

        assert actor.config["name"] == "my-sched"
        assert len(actor._schedules) == 2
        assert actor._schedules[0].name == "a"
        assert actor._schedules[1].name == "b"

    @pytest.mark.asyncio
    async def test_run_lifecycle_with_shutdown(self, tmp_path):
        """run() starts timer, processes subscription, and shuts down cleanly."""
        schedules = [_task_schedule(name="fast", interval_seconds=60)]
        config_file = _write_config(tmp_path, schedules)
        bus = InMemoryBus()
        actor = SchedulerActor("s-run", config_file, bus=bus)

        from unittest.mock import patch as _patch

        async def run_and_stop():
            # Wait for actor to start
            for _ in range(50):
                if actor._running:
                    break
                await asyncio.sleep(0.01)

            assert actor._running
            assert actor._timer_task is not None
            assert not actor._timer_task.done()

            # Trigger shutdown and unsubscribe to unblock the iteration loop
            actor._request_shutdown(signal.SIGTERM)
            await asyncio.sleep(0.01)
            if actor._sub:
                await actor._sub.unsubscribe()

        with _patch.object(actor, "_install_signal_handlers"):
            task = asyncio.create_task(actor.run("heddle.scheduler.test"))
            stopper = asyncio.create_task(run_and_stop())
            await asyncio.wait_for(asyncio.gather(task, stopper), timeout=5.0)

        assert not actor._running
        assert not bus._connected


# ===========================================================================
# Edge case dispatch tests
# ===========================================================================


class TestDispatchEdgeCases:
    @pytest.mark.asyncio
    async def test_unknown_dispatch_type_logs_error(self, tmp_path):
        """An entry with unknown dispatch_type is handled without crashing."""
        config_file = _write_config(tmp_path)
        bus = InMemoryBus()
        await bus.connect()
        actor = SchedulerActor("s-bad-type", config_file, bus=bus)

        entry = ScheduleEntry(
            name="bad",
            cron=None,
            interval_seconds=60,
            dispatch_type="bogus",
        )
        # Should not raise — unknown type is logged
        await actor._fire_schedule(entry)

    @pytest.mark.asyncio
    async def test_dispatch_goal_with_empty_config(self, tmp_path):
        """_dispatch_goal handles missing goal_config gracefully (defaults)."""
        config_file = _write_config(tmp_path)
        bus = InMemoryBus()
        await bus.connect()
        actor = SchedulerActor("s-empty-cfg", config_file, bus=bus)

        sub = await bus.subscribe("heddle.goals.incoming")

        entry = ScheduleEntry(
            name="empty",
            cron=None,
            interval_seconds=60,
            dispatch_type="goal",
            goal_config=None,  # No config — should use defaults
        )
        await actor._dispatch_goal(entry)

        msg = await sub.__anext__()
        goal = OrchestratorGoal(**msg)
        assert goal.instruction == ""
        assert goal.context == {}

    @pytest.mark.asyncio
    async def test_advance_next_fire_no_cron_no_interval(self, tmp_path):
        """_advance_next_fire with neither cron nor interval is a no-op."""
        config_file = _write_config(tmp_path)
        actor = SchedulerActor("s-noop", config_file, bus=InMemoryBus())

        entry = ScheduleEntry(
            name="noop",
            cron=None,
            interval_seconds=None,
            dispatch_type="goal",
        )
        original_fire = entry.next_fire
        actor._advance_next_fire(entry)
        assert entry.next_fire == original_fire


# ---------------------------------------------------------------------------
# Control-listener / on_reload regression tests
#
# An earlier shape of ``SchedulerActor.run()`` overrode the base method
# but never started the control listener — so ``heddle.control.reload``
# broadcasts were silently ignored and the scheduler was permanently
# frozen at startup config.  These tests pin both the listener wiring
# and the on_reload behaviour.
# ---------------------------------------------------------------------------


class TestSchedulerOnReload:
    @pytest.mark.asyncio
    async def test_on_reload_re_reads_config_and_swaps_schedules(self, tmp_path):
        """on_reload() re-parses the YAML and replaces self._schedules."""
        config_file = _write_config(
            tmp_path,
            [_task_schedule(name="initial", interval_seconds=60)],
        )
        actor = SchedulerActor("s-reload", config_file, bus=InMemoryBus())
        assert [s.name for s in actor._schedules] == ["initial"]

        # Rewrite the config file with a new schedule list.
        new_yaml = yaml.dump(
            {
                "name": "test-scheduler",
                "schedules": [
                    _task_schedule(name="renamed", interval_seconds=30),
                    _goal_schedule(name="added", interval_seconds=120),
                ],
            }
        )
        from pathlib import Path

        Path(config_file).write_text(new_yaml)

        await actor.on_reload()

        assert [s.name for s in actor._schedules] == ["renamed", "added"]
        # And the new entries have valid (non-zero, in-the-future) fire times.
        for entry in actor._schedules:
            assert entry.next_fire > 0

    @pytest.mark.asyncio
    async def test_run_starts_control_listener(self, tmp_path):
        """``run()`` must subscribe to ``heddle.control.reload`` and
        invoke ``on_reload()`` when a reload broadcast arrives.

        Regression test: the previous ``run()`` override never started
        the control listener, so config hot-reload was silently
        disabled.  This test publishes a reload message and asserts
        ``on_reload`` was awaited.
        """
        config_file = _write_config(
            tmp_path,
            [_task_schedule(name="x", interval_seconds=600)],
        )
        bus = InMemoryBus()
        await bus.connect()
        actor = SchedulerActor("s-ctrl", config_file, bus=bus)

        reload_called = asyncio.Event()
        original_on_reload = actor.on_reload

        async def spy_on_reload():
            reload_called.set()
            await original_on_reload()

        actor.on_reload = spy_on_reload  # type: ignore[method-assign]

        # Run the actor in the background.  We can't easily install
        # signal handlers in a test, so we drive shutdown via _running.
        run_task = asyncio.create_task(actor.run("heddle.scheduler.s-ctrl"))

        # Give the actor time to subscribe to both the main subject AND
        # heddle.control.reload before we publish the reload broadcast.
        # Without the listener wired up, this publish has no subscriber
        # and the message is dropped — reload_called will never set and
        # the wait_for below will time out (which is exactly the failure
        # mode the test catches).
        try:
            for _ in range(50):
                await asyncio.sleep(0.02)
                if any("heddle.control.reload" in subj for subj in bus._subscribers):
                    break

            await bus.publish("heddle.control.reload", {"action": "reload"})

            await asyncio.wait_for(reload_called.wait(), timeout=2.0)
        finally:
            actor._running = False
            if actor._shutdown_event is not None:
                actor._shutdown_event.set()
            # Force the subscription loop to exit by closing the bus.
            await bus.close()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(run_task, timeout=2.0)
            if not run_task.done():
                run_task.cancel()
                with contextlib.suppress(BaseException):
                    await run_task


# ---------------------------------------------------------------------------
# Event-loop responsiveness during expansion
# ---------------------------------------------------------------------------


class TestExpansionDoesNotBlockEventLoop:
    @pytest.mark.asyncio
    async def test_blocking_expansion_runs_concurrently_with_other_tasks(
        self, tmp_path, monkeypatch
    ):
        """A slow ``expand_from`` callable must not freeze the event loop.

        Regression test for the original quality issue: the expansion
        function was called inline with ``self._run_expansion(...)``,
        so any user-defined I/O it performed (DB query, HTTP probe)
        blocked the timer loop AND any other in-flight coroutine.
        Now wrapped in ``asyncio.to_thread``, the loop stays responsive.

        We measure wall-clock time of running ``_fire_schedule`` and a
        sleep concurrently via :func:`asyncio.gather`.  Both take ~0.3s
        of work each.  With the fix they run in parallel (total ~0.3s);
        without it the sync ``time.sleep`` inside the expansion blocks
        the loop and the awaitable ``asyncio.sleep`` cannot make
        progress until the expansion returns (total ~0.6s).
        """
        sleep_seconds = 0.3

        def slow_expansion():
            time.sleep(sleep_seconds)
            return [{"k": "v"}]

        # Monkey-patch importlib so the dotted path resolves to our
        # local function without needing a real module on disk.
        import importlib

        fake_mod = type("M", (), {"slow_fn": slow_expansion})
        monkeypatch.setattr(
            importlib,
            "import_module",
            lambda name: fake_mod if name == "fake.mod" else __import__(name),
        )

        schedules = [
            {
                "name": "s_expand",
                "interval_seconds": 60,
                "dispatch_type": "task",
                "task": {"worker_type": "summarizer", "payload": {}},
                "expand_from": "fake.mod.slow_fn",
            }
        ]
        config_file = _write_config(tmp_path, schedules)
        bus = InMemoryBus()
        await bus.connect()
        actor = SchedulerActor("s_expand", config_file, bus=bus)

        start = time.monotonic()
        await asyncio.gather(
            actor._fire_schedule(actor._schedules[0]),
            asyncio.sleep(sleep_seconds),
        )
        elapsed = time.monotonic() - start

        # Concurrent execution: total ≈ max(0.3, 0.3) = 0.3s plus a
        # little overhead.  Sequential (blocked) execution: ≈ 0.6s.
        # We assert below the midpoint to leave clear separation
        # between the two cases.
        assert elapsed < sleep_seconds * 1.5, (
            f"event loop was blocked: gather took {elapsed:.3f}s for two "
            f"concurrent {sleep_seconds}s tasks (expected ~{sleep_seconds}s, "
            f"sequential would be ~{sleep_seconds * 2}s)"
        )
