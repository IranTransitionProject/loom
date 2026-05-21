"""Test TaskWorker base class (unit tests, no infrastructure)."""

from unittest.mock import AsyncMock

import pytest

from heddle.core.envelope import wrap
from heddle.core.messages import ModelTier, TaskMessage, TaskResult, TaskStatus
from heddle.worker.base import TaskWorker

# --- Mock implementation ---


class EchoWorker(TaskWorker):
    """Simple test worker that echoes payload."""

    async def process(self, payload, metadata):
        return {
            "output": {"echo": payload.get("text", "")},
            "model_used": "echo-v1",
            "token_usage": {},
        }


class FailingWorker(TaskWorker):
    """Worker that always raises."""

    async def process(self, payload, metadata):
        raise RuntimeError("intentional failure")


class BadOutputWorker(TaskWorker):
    """Worker that returns output not matching schema."""

    async def process(self, payload, metadata):
        return {
            "output": {"wrong_field": "oops"},
            "model_used": None,
            "token_usage": None,
        }


class StatefulBadOutputWorker(TaskWorker):
    """Mutates instance state in process(), then returns invalid output.

    Used to pin the worker-statelessness invariant after the
    output-validation-failure path: process() ran, state was mutated,
    output validation rejected the result. ``reset()`` must still run.
    """

    async def process(self, payload, metadata):
        self.scratch.append(payload.get("text", ""))
        return {
            "output": {"wrong_field": "oops"},
            "model_used": None,
            "token_usage": None,
        }

    async def reset(self):
        self.reset_count += 1
        self.scratch = []


# --- Fixtures ---

ECHO_CONFIG = {
    "name": "echo_worker",
    "input_schema": {
        "type": "object",
        "required": ["text"],
        "properties": {"text": {"type": "string"}},
    },
    "output_schema": {
        "type": "object",
        "required": ["echo"],
        "properties": {"echo": {"type": "string"}},
    },
}


def _make_task(payload=None, worker_type="echo_worker"):
    task = TaskMessage(
        worker_type=worker_type,
        input=payload or {"text": "hello"},
        model_tier=ModelTier.LOCAL,
        parent_task_id="goal-123",
    )
    return wrap("core.TaskMessage", task).model_dump(mode="json")


# --- Tests ---


@pytest.mark.asyncio
async def test_task_worker_valid_input_output(tmp_path):
    """TaskWorker validates input, calls process(), validates output, publishes result."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(_yaml_dump(ECHO_CONFIG))

    worker = EchoWorker("test-worker", str(config_file))
    worker.publish = AsyncMock()

    await worker.handle_message(_make_task({"text": "hello"}))

    worker.publish.assert_called_once()
    call_args = worker.publish.call_args
    subject = call_args[0][0]
    result_data = call_args[0][1]

    assert subject == "heddle.results.goal-123"
    result = TaskResult(**result_data["payload"])
    assert result.status == TaskStatus.COMPLETED
    assert result.output == {"echo": "hello"}
    assert result.model_used == "echo-v1"
    assert result.processing_time_ms >= 0


@pytest.mark.asyncio
async def test_task_worker_input_validation_failure(tmp_path):
    """TaskWorker rejects invalid input and publishes FAILED result."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(_yaml_dump(ECHO_CONFIG))

    worker = EchoWorker("test-worker", str(config_file))
    worker.publish = AsyncMock()

    # Missing required "text" field
    await worker.handle_message(_make_task({"wrong": "field"}))

    worker.publish.assert_called_once()
    result = TaskResult(**worker.publish.call_args[0][1]["payload"])
    assert result.status == TaskStatus.FAILED
    assert "Input validation" in result.error


@pytest.mark.asyncio
async def test_task_worker_output_validation_failure(tmp_path):
    """TaskWorker rejects output that doesn't match schema."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(_yaml_dump(ECHO_CONFIG))

    worker = BadOutputWorker("test-worker", str(config_file))
    worker.publish = AsyncMock()

    await worker.handle_message(_make_task({"text": "hello"}))

    result = TaskResult(**worker.publish.call_args[0][1]["payload"])
    assert result.status == TaskStatus.FAILED
    assert "Output validation" in result.error


@pytest.mark.asyncio
async def test_task_worker_process_exception(tmp_path):
    """TaskWorker catches process() exceptions and publishes FAILED result."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(_yaml_dump(ECHO_CONFIG))

    worker = FailingWorker("test-worker", str(config_file))
    worker.publish = AsyncMock()

    await worker.handle_message(_make_task({"text": "hello"}))

    result = TaskResult(**worker.publish.call_args[0][1]["payload"])
    assert result.status == TaskStatus.FAILED
    assert "intentional failure" in result.error


@pytest.mark.asyncio
async def test_task_worker_resets_after_publish_failure(tmp_path):
    """F1: reset() runs even when ``_publish_result`` raises after ``process()``.

    Pins Invariant 1 (worker statelessness) on the publish-failure path.
    The earlier review (REPOSITORY_REVIEW_2026-05-10.md §3.4) noted
    that the invalid-output reset path was pinned (above), but a
    failure in the bus publish *after* process() succeeded had no
    regression test — a future refactor that moved
    ``_publish_result`` outside the ``try`` block would silently
    break statelessness without failing any test.

    Strategy: wire a publish stub that always raises, send a valid
    task that succeeds at the output-validation stage, and assert
    ``reset()`` still ran exactly once despite the publish exception
    propagating.
    """

    # Use the echo worker's schema but a stateful subclass so we can
    # observe reset_count.
    class StatefulEchoWorker(TaskWorker):
        async def process(self, payload, metadata):
            self.scratch.append(payload.get("text", ""))
            return {
                "output": {"echo": payload.get("text", "")},
                "model_used": None,
                "token_usage": None,
            }

        async def reset(self):
            self.reset_count += 1
            self.scratch = []

    config_file = tmp_path / "config.yaml"
    config_file.write_text(_yaml_dump(ECHO_CONFIG))

    worker = StatefulEchoWorker("test-worker", str(config_file))
    worker.scratch = []
    worker.reset_count = 0
    # Publish always raises — this is the failure path being pinned.
    worker.publish = AsyncMock(side_effect=RuntimeError("bus down"))

    # The first publish (TaskStatus.COMPLETED) raises; the outer
    # ``except`` then tries to publish TaskStatus.FAILED, which
    # also raises and propagates out of ``handle_message``.  The
    # ``finally`` reset() must still run before the exception
    # escapes — that's the contract this test pins.
    with pytest.raises(RuntimeError, match="bus down"):
        await worker.handle_message(_make_task({"text": "hello"}))

    assert worker.reset_count == 1
    assert worker.scratch == []


@pytest.mark.asyncio
async def test_task_worker_resets_after_invalid_output(tmp_path):
    """reset() runs even when output validation rejects a mutated state.

    Pins Invariant 1 (worker statelessness): after process() runs, reset()
    must execute regardless of whether output validation passes. Without
    a try/finally around the body, the early ``return`` on output-validation
    failure bypasses reset() and leaks process()-mutated state into the
    next task.
    """
    config_file = tmp_path / "config.yaml"
    config_file.write_text(_yaml_dump(ECHO_CONFIG))

    worker = StatefulBadOutputWorker("test-worker", str(config_file))
    worker.scratch = []
    worker.reset_count = 0
    worker.publish = AsyncMock()

    await worker.handle_message(_make_task({"text": "hello"}))

    # process() ran and mutated state; output failed validation; reset must still run.
    result = TaskResult(**worker.publish.call_args[0][1]["payload"])
    assert result.status == TaskStatus.FAILED
    assert "Output validation" in result.error
    assert worker.reset_count == 1
    assert worker.scratch == []


@pytest.mark.asyncio
async def test_task_worker_empty_schema(tmp_path):
    """TaskWorker with no schemas skips validation."""
    config = {"name": "permissive_worker"}
    config_file = tmp_path / "config.yaml"
    config_file.write_text(_yaml_dump(config))

    worker = EchoWorker("test-worker", str(config_file))
    worker.publish = AsyncMock()

    await worker.handle_message(_make_task({"anything": "goes"}))

    result = TaskResult(**worker.publish.call_args[0][1]["payload"])
    assert result.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_task_worker_result_subject_default(tmp_path):
    """Result published to heddle.results.default when no parent_task_id."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(_yaml_dump({"name": "test"}))

    worker = EchoWorker("test-worker", str(config_file))
    worker.publish = AsyncMock()

    task = wrap(
        "core.TaskMessage",
        TaskMessage(
            worker_type="test",
            input={"text": "hello"},
            parent_task_id=None,
        ),
    ).model_dump(mode="json")
    await worker.handle_message(task)

    subject = worker.publish.call_args[0][0]
    assert subject == "heddle.results.default"


# --- Helpers ---


def _yaml_dump(data):
    import yaml

    return yaml.dump(data)
