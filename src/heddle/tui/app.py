"""Heddle TUI dashboard — real-time terminal view of the actor mesh.

Subscribes to NATS subjects and renders live updates:

- **Goals** panel: active goals with status and elapsed time
- **Tasks** panel: dispatched tasks with worker type, tier, status
- **Pipeline** panel: pipeline stage execution with progress
- **Events** log: scrolling event stream (all ``heddle.>`` subjects)

Architecture:
    The app connects to NATS in a background asyncio task and pushes
    structured events to Textual widgets via ``post_message``.  NATS
    subscription uses ``heddle.>`` wildcard to capture all Heddle traffic.
    The TUI is read-only — it observes but never publishes.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, Log, Static, TabbedContent, TabPane

if TYPE_CHECKING:
    import asyncio

# ---------------------------------------------------------------------------
# Domain models for tracked entities
# ---------------------------------------------------------------------------


@dataclass
class TrackedGoal:
    """In-flight goal observed via NATS messages."""

    goal_id: str
    instruction: str = ""
    status: str = "received"
    subtask_count: int = 0
    collected: int = 0
    started_at: float = 0.0
    elapsed_ms: int = 0

    def __post_init__(self) -> None:
        if not self.started_at:
            self.started_at = time.monotonic()


@dataclass
class TrackedTask:
    """In-flight task observed via NATS messages."""

    task_id: str
    goal_id: str = ""
    worker_type: str = ""
    tier: str = ""
    status: str = "dispatched"
    model_used: str = ""
    elapsed_ms: int = 0


@dataclass
class TrackedStage:
    """Pipeline stage observed via NATS messages."""

    stage_name: str
    goal_id: str = ""
    worker_type: str = ""
    status: str = "running"
    wall_time_ms: int = 0


@dataclass
class DashboardState:
    """Mutable state container for the TUI dashboard."""

    goals: dict[str, TrackedGoal] = field(default_factory=dict)
    tasks: dict[str, TrackedTask] = field(default_factory=dict)
    stages: list[TrackedStage] = field(default_factory=list)
    event_count: int = 0
    message_count: int = 0


# ---------------------------------------------------------------------------
# Custom Textual messages for NATS events
# ---------------------------------------------------------------------------


class NatsEvent(Message):
    """A NATS message received on a Heddle subject."""

    def __init__(self, subject: str, data: dict[str, Any]) -> None:
        super().__init__()
        self.subject = subject
        self.data = data


class NatsConnected(Message):
    """NATS connection established."""

    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url


class NatsDisconnected(Message):
    """NATS connection lost."""

    def __init__(self, reason: str = "") -> None:
        super().__init__()
        self.reason = reason


# ---------------------------------------------------------------------------
# Status bar widget
# ---------------------------------------------------------------------------


class StatusBar(Static):
    """Top-line status showing connection state and counters."""

    connected: reactive[bool] = reactive(False)
    nats_url: reactive[str] = reactive("")
    msg_count: reactive[int] = reactive(0)
    goal_count: reactive[int] = reactive(0)
    task_count: reactive[int] = reactive(0)

    def render(self) -> str:
        """Render the status bar content."""
        conn = f"🟢 {self.nats_url}" if self.connected else "🔴 disconnected"
        return (
            f" {conn}  │  "
            f"msgs: {self.msg_count}  │  "
            f"goals: {self.goal_count}  │  "
            f"tasks: {self.task_count}"
        )


# ---------------------------------------------------------------------------
# Main TUI application
# ---------------------------------------------------------------------------


class HeddleDashboard(App):
    """Real-time terminal dashboard for the Heddle actor mesh."""

    TITLE = "Heddle Dashboard"
    CSS = """
    StatusBar {
        height: 1;
        background: $primary-background;
        color: $text;
        padding: 0 1;
    }

    #goals-table, #tasks-table, #stages-table {
        height: 1fr;
    }

    #event-log {
        height: 1fr;
    }

    TabPane {
        padding: 0;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit", show=True),
        Binding("c", "clear_log", "Clear log", show=True),
        Binding("r", "refresh_tables", "Refresh", show=True),
    ]

    def __init__(self, nats_url: str = "nats://localhost:4222") -> None:
        super().__init__()
        self.nats_url = nats_url
        self.state = DashboardState()
        self._nats_task: asyncio.Task[None] | None = None

    def compose(self) -> ComposeResult:
        """Build the widget tree."""
        yield Header()
        yield StatusBar(id="status-bar")
        with TabbedContent():
            with TabPane("Goals", id="tab-goals"):
                yield DataTable(id="goals-table")
            with TabPane("Tasks", id="tab-tasks"):
                yield DataTable(id="tasks-table")
            with TabPane("Pipeline", id="tab-stages"):
                yield DataTable(id="stages-table")
            with TabPane("Events", id="tab-events"):
                yield Log(id="event-log", max_lines=2000)
        yield Footer()

    def on_mount(self) -> None:
        """Initialize tables and start NATS subscription."""
        # Goals table
        goals_table = self.query_one("#goals-table", DataTable)
        goals_table.add_columns(
            "Goal ID",
            "Instruction",
            "Status",
            "Subtasks",
            "Collected",
            "Elapsed",
        )

        # Tasks table
        tasks_table = self.query_one("#tasks-table", DataTable)
        tasks_table.add_columns("Task ID", "Goal", "Worker", "Tier", "Status", "Model", "Elapsed")

        # Stages table
        stages_table = self.query_one("#stages-table", DataTable)
        stages_table.add_columns("Stage", "Goal", "Worker", "Status", "Wall Time")

        # Start NATS listener
        self._start_nats_listener()

    @work(exclusive=True, thread=False)
    async def _start_nats_listener(self) -> None:
        """Connect to NATS and subscribe to all Heddle subjects."""
        try:
            import nats
        except ImportError:
            log = self.query_one("#event-log", Log)
            log.write_line("[ERROR] nats-py not installed. Run: uv sync --extra tui")
            return

        try:
            nc = await nats.connect(self.nats_url)
            self.post_message(NatsConnected(self.nats_url))
        except Exception as e:
            self.post_message(NatsDisconnected(str(e)))
            log = self.query_one("#event-log", Log)
            log.write_line(f"[ERROR] NATS connection failed: {e}")
            return

        try:
            # Subscribe to all heddle.> subjects
            sub = await nc.subscribe("heddle.>")
            async for msg in sub.messages:
                try:
                    data = json.loads(msg.data.decode())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                self.post_message(NatsEvent(msg.subject, data))
        except Exception as e:
            self.post_message(NatsDisconnected(str(e)))
        finally:
            if nc.is_connected:
                await nc.close()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    @on(NatsConnected)
    def _on_connected(self, event: NatsConnected) -> None:
        status = self.query_one("#status-bar", StatusBar)
        status.connected = True
        status.nats_url = event.url
        log = self.query_one("#event-log", Log)
        log.write_line(f"[CONNECTED] {event.url}")

    @on(NatsDisconnected)
    def _on_disconnected(self, event: NatsDisconnected) -> None:
        status = self.query_one("#status-bar", StatusBar)
        status.connected = False
        log = self.query_one("#event-log", Log)
        log.write_line(f"[DISCONNECTED] {event.reason}")

    @on(NatsEvent)
    def _on_nats_event(self, event: NatsEvent) -> None:
        """Route a NATS event to the appropriate handler and update UI."""
        self.state.message_count += 1
        subject = event.subject
        data = event.data

        # Update event log
        log = self.query_one("#event-log", Log)
        summary = self._summarize_event(subject, data)
        log.write_line(summary)

        # Route to specific handlers
        if subject == "heddle.goals.incoming":
            self._handle_goal_received(data)
        elif subject.startswith("heddle.results."):
            self._handle_result(subject, data)
        elif subject == "heddle.tasks.incoming":
            self._handle_task_dispatched(data)
        elif subject.startswith("heddle.tasks."):
            self._handle_task_routed(subject, data)

        # Update status bar counters
        status = self.query_one("#status-bar", StatusBar)
        status.msg_count = self.state.message_count
        status.goal_count = len(self.state.goals)
        status.task_count = len(self.state.tasks)

    # ------------------------------------------------------------------
    # NATS event handlers
    # ------------------------------------------------------------------

    def _handle_goal_received(self, data: dict[str, Any]) -> None:
        """Track a new goal from heddle.goals.incoming."""
        # Goals ride a WireEnvelope; the goal fields live in `payload`.
        body = data.get("payload", data)
        goal_id = body.get("goal_id", "unknown")
        instruction = body.get("instruction", "")[:80]
        goal = TrackedGoal(goal_id=goal_id, instruction=instruction)
        self.state.goals[goal_id] = goal
        self._refresh_goals_table()

    def _handle_task_dispatched(self, data: dict[str, Any]) -> None:
        """Track a task dispatched to heddle.tasks.incoming."""
        task_id = data.get("task_id", "unknown")
        task = TrackedTask(
            task_id=task_id,
            goal_id=data.get("parent_task_id", ""),
            worker_type=data.get("worker_type", ""),
            tier=data.get("model_tier", ""),
        )
        self.state.tasks[task_id] = task

        # Update parent goal subtask count
        parent = data.get("parent_task_id", "")
        if parent in self.state.goals:
            self.state.goals[parent].subtask_count += 1
            self._refresh_goals_table()

        self._refresh_tasks_table()

    def _handle_task_routed(self, subject: str, data: dict[str, Any]) -> None:
        """Update task when routed to a specific worker queue."""
        task_id = data.get("task_id", "unknown")
        if task_id in self.state.tasks:
            # Extract tier from subject: heddle.tasks.{worker_type}.{tier}
            parts = subject.split(".")
            if len(parts) >= 4:
                self.state.tasks[task_id].tier = parts[3]
            self.state.tasks[task_id].status = "routed"
            self._refresh_tasks_table()

    def _handle_result(self, subject: str, data: dict[str, Any]) -> None:
        """Handle a result arriving on heddle.results.{goal_id}."""
        task_id = data.get("task_id", "unknown")
        status = data.get("status", "unknown")
        elapsed = data.get("processing_time_ms", 0)

        # Update task
        if task_id in self.state.tasks:
            self.state.tasks[task_id].status = status
            self.state.tasks[task_id].elapsed_ms = elapsed
            self.state.tasks[task_id].model_used = data.get("model_used", "")
            self._refresh_tasks_table()

        # Update parent goal collected count
        prefix = "heddle.results."
        goal_id = subject.rsplit(".", maxsplit=1)[-1] if subject.startswith(prefix) else ""
        if goal_id in self.state.goals:
            goal = self.state.goals[goal_id]
            goal.collected += 1
            # If this result's task_id matches the goal_id, it's the final result
            if task_id == goal_id:
                goal.status = status
                goal.elapsed_ms = elapsed
            self._refresh_goals_table()

        # Check for pipeline stage info in output
        output = data.get("output") or {}
        if isinstance(output, dict) and "_timeline" in output:
            self._handle_timeline(goal_id, output["_timeline"])

    def _handle_timeline(self, goal_id: str, timeline: list[dict[str, Any]]) -> None:
        """Extract pipeline stage info from _timeline in results."""
        for entry in timeline:
            stage = TrackedStage(
                stage_name=entry.get("stage", "unknown"),
                goal_id=goal_id,
                worker_type=entry.get("worker_type", ""),
                status="completed" if entry.get("wall_time_ms") else "unknown",
                wall_time_ms=entry.get("wall_time_ms", 0),
            )
            self.state.stages.append(stage)
        self._refresh_stages_table()

    # ------------------------------------------------------------------
    # Table refresh methods
    # ------------------------------------------------------------------

    def _has_screen(self) -> bool:
        """Check whether the app has a mounted screen (safe for testing)."""
        try:
            _ = self.screen
        except Exception:
            return False
        return True

    def _refresh_goals_table(self) -> None:
        if not self._has_screen():
            return
        table = self.query_one("#goals-table", DataTable)
        table.clear()
        for goal in self.state.goals.values():
            elapsed = goal.elapsed_ms or int((time.monotonic() - goal.started_at) * 1000)
            table.add_row(
                goal.goal_id[:12],
                goal.instruction[:50],
                self._status_icon(goal.status),
                str(goal.subtask_count),
                str(goal.collected),
                f"{elapsed}ms",
            )

    def _refresh_tasks_table(self) -> None:
        if not self._has_screen():
            return
        table = self.query_one("#tasks-table", DataTable)
        table.clear()
        for task in self.state.tasks.values():
            table.add_row(
                task.task_id[:12],
                task.goal_id[:12],
                task.worker_type,
                task.tier,
                self._status_icon(task.status),
                task.model_used,
                f"{task.elapsed_ms}ms" if task.elapsed_ms else "…",
            )

    def _refresh_stages_table(self) -> None:
        if not self._has_screen():
            return
        table = self.query_one("#stages-table", DataTable)
        table.clear()
        for stage in self.state.stages[-50:]:  # Last 50 stages
            table.add_row(
                stage.stage_name,
                stage.goal_id[:12],
                stage.worker_type,
                self._status_icon(stage.status),
                f"{stage.wall_time_ms}ms" if stage.wall_time_ms else "…",
            )

    @staticmethod
    def _status_icon(status: str) -> str:
        """Map status strings to visual indicators."""
        icons = {
            "received": "📥",
            "dispatched": "📤",
            "routed": "🔀",
            "running": "⏳",
            "completed": "✅",
            "COMPLETED": "✅",
            "failed": "❌",
            "FAILED": "❌",
            "unknown": "❓",
        }
        return icons.get(status, status)

    @staticmethod
    def _summarize_event(subject: str, data: dict[str, Any]) -> str:
        """Create a one-line summary of a NATS event for the log."""
        # Enveloped messages (e.g. goals) carry their fields under `payload`;
        # bare messages expose them at the top level. Look in both.
        body = data.get("payload", data)
        parts = [subject]
        for key in ("goal_id", "task_id", "worker_type", "status", "instruction"):
            if key in body:
                val = str(body[key])
                if len(val) > 60:
                    val = val[:57] + "..."
                parts.append(f"{key}={val}")
        return "  ".join(parts)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_clear_log(self) -> None:
        """Clear the event log."""
        log = self.query_one("#event-log", Log)
        log.clear()

    def action_refresh_tables(self) -> None:
        """Force refresh all tables."""
        self._refresh_goals_table()
        self._refresh_tasks_table()
        self._refresh_stages_table()


def run_dashboard(nats_url: str = "nats://localhost:4222") -> None:
    """Launch the Heddle TUI dashboard.

    Args:
        nats_url: NATS server URL to connect to.
    """
    app = HeddleDashboard(nats_url=nats_url)
    app.run()
