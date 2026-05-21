"""Tests for scheduler expand_from feature — multi-session dispatch."""

import asyncio
import tempfile
from typing import Any

import pytest
import yaml

from heddle.bus.memory import InMemoryBus
from heddle.scheduler.scheduler import ScheduleEntry, SchedulerActor


def _make_scheduler_config(tmp_path=None):
    """Create a minimal scheduler config file and return its path."""
    cfg = {"name": "test_scheduler", "schedules": []}
    if tmp_path:
        path = tmp_path / "scheduler.yaml"
    else:
        f = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w")
        f.write(yaml.dump(cfg))
        f.close()
        return f.name
    path.write_text(yaml.dump(cfg))
    return str(path)


# ---------------------------------------------------------------------------
# Mock expansion functions (used via dotted path import)
# ---------------------------------------------------------------------------


def mock_get_two_sessions() -> list[dict[str, Any]]:
    return [
        {"session_monitor_request": {"session_id": "s1", "transcript": "..."}},
        {"session_monitor_request": {"session_id": "s2", "transcript": "..."}},
    ]


def mock_get_no_sessions() -> list[dict[str, Any]]:
    return []


def mock_return_bad_type() -> str:
    return "not a list"


def mock_raises_on_call() -> list[dict[str, Any]]:
    raise RuntimeError("expansion function blew up")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSchedulerExpansion:
    def test_run_expansion_success(self):
        result = SchedulerActor._run_expansion(
            "tests.test_scheduler_expansion.mock_get_two_sessions",
            "test_schedule",
        )
        assert len(result) == 2
        assert result[0]["session_monitor_request"]["session_id"] == "s1"

    def test_run_expansion_empty(self):
        result = SchedulerActor._run_expansion(
            "tests.test_scheduler_expansion.mock_get_no_sessions",
            "test_schedule",
        )
        assert result == []

    def test_run_expansion_bad_return_type(self):
        result = SchedulerActor._run_expansion(
            "tests.test_scheduler_expansion.mock_return_bad_type",
            "test_schedule",
        )
        assert result == []

    def test_run_expansion_import_error(self):
        result = SchedulerActor._run_expansion(
            "nonexistent.module.func",
            "test_schedule",
        )
        assert result == []

    def test_run_expansion_no_dot(self):
        result = SchedulerActor._run_expansion(
            "no_dots_here",
            "test_schedule",
        )
        assert result == []

    def test_run_expansion_call_raises(self):
        """Expansion function that raises is caught and returns empty list."""
        result = SchedulerActor._run_expansion(
            "tests.test_scheduler_expansion.mock_raises_on_call",
            "test_schedule",
        )
        assert result == []

    def test_run_expansion_attribute_error(self):
        """Missing attribute on valid module returns empty list."""
        result = SchedulerActor._run_expansion(
            "tests.test_scheduler_expansion.nonexistent_func",
            "test_schedule",
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_fire_schedule_with_expansion(self):
        """expand_from dispatches one task per expansion result."""
        bus = InMemoryBus()
        await bus.connect()
        sub = await bus.subscribe("heddle.tasks.incoming")

        config_path = _make_scheduler_config()
        actor = SchedulerActor(
            actor_id="test-scheduler",
            config_path=config_path,
            bus=bus,
        )
        actor._running = True

        entry = ScheduleEntry(
            name="sa_monitor",
            cron=None,
            interval_seconds=900,
            dispatch_type="task",
            task_config={
                "worker_type": "sa_session_advisor",
                "payload": {},
                "model_tier": "local",
            },
            expand_from="tests.test_scheduler_expansion.mock_get_two_sessions",
        )

        await actor._fire_schedule(entry)

        # Should have dispatched 2 tasks
        msg1 = await asyncio.wait_for(sub._queue.get(), timeout=1.0)
        msg2 = await asyncio.wait_for(sub._queue.get(), timeout=1.0)

        assert msg1["worker_type"] == "sa_session_advisor"
        assert msg2["worker_type"] == "sa_session_advisor"
        # Expansion context merged into payload
        assert msg1["payload"]["session_monitor_request"]["session_id"] == "s1"
        assert msg2["payload"]["session_monitor_request"]["session_id"] == "s2"

        await bus.close()

    @pytest.mark.asyncio
    async def test_fire_schedule_with_expansion_goal(self):
        """expand_from dispatches one goal per expansion result."""
        bus = InMemoryBus()
        await bus.connect()
        sub = await bus.subscribe("heddle.goals.incoming")

        config_path = _make_scheduler_config()
        actor = SchedulerActor(
            actor_id="test-scheduler-goal",
            config_path=config_path,
            bus=bus,
        )
        actor._running = True

        entry = ScheduleEntry(
            name="goal_expand",
            cron=None,
            interval_seconds=60,
            dispatch_type="goal",
            goal_config={"instruction": "Analyze session", "context": {}},
            expand_from="tests.test_scheduler_expansion.mock_get_two_sessions",
        )

        await actor._fire_schedule(entry)

        # Should have dispatched 2 goals
        msg1 = await asyncio.wait_for(sub._queue.get(), timeout=1.0)
        msg2 = await asyncio.wait_for(sub._queue.get(), timeout=1.0)

        assert msg1["payload"]["instruction"] == "Analyze session"
        assert msg2["payload"]["instruction"] == "Analyze session"
        # Expansion context merged into goal context
        assert msg1["payload"]["context"]["session_monitor_request"]["session_id"] == "s1"
        assert msg2["payload"]["context"]["session_monitor_request"]["session_id"] == "s2"

        await bus.close()

    @pytest.mark.asyncio
    async def test_fire_schedule_with_empty_expansion(self):
        """Empty expansion result dispatches nothing."""
        bus = InMemoryBus()
        await bus.connect()
        sub = await bus.subscribe("heddle.tasks.incoming")

        config_path = _make_scheduler_config()
        actor = SchedulerActor(
            actor_id="test-scheduler",
            config_path=config_path,
            bus=bus,
        )
        actor._running = True

        entry = ScheduleEntry(
            name="sa_monitor",
            cron=None,
            interval_seconds=900,
            dispatch_type="task",
            task_config={
                "worker_type": "sa_session_advisor",
                "payload": {},
                "model_tier": "local",
            },
            expand_from="tests.test_scheduler_expansion.mock_get_no_sessions",
        )

        await actor._fire_schedule(entry)

        # Nothing should be in the queue
        assert sub._queue.empty()
        await bus.close()


class TestScheduleEntryParsing:
    def test_expand_from_parsed(self):
        raw = [
            {
                "name": "test",
                "interval_seconds": 60,
                "dispatch_type": "task",
                "task": {"worker_type": "test_worker"},
                "expand_from": "myapp.sessions.get_active",
            }
        ]
        entries = SchedulerActor._parse_schedules(raw)
        assert entries[0].expand_from == "myapp.sessions.get_active"

    def test_expand_from_defaults_none(self):
        raw = [
            {
                "name": "test",
                "interval_seconds": 60,
                "dispatch_type": "task",
                "task": {"worker_type": "test_worker"},
            }
        ]
        entries = SchedulerActor._parse_schedules(raw)
        assert entries[0].expand_from is None
