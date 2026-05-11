"""Test message schema validation and serialization."""

from heddle.core.messages import (
    ModelTier,
    OrchestratorGoal,
    TaskMessage,
    TaskResult,
    TaskStatus,
    parse_task_result,
)


def test_task_message_defaults():
    msg = TaskMessage(worker_type="summarizer", payload={"text": "hello"})
    assert msg.task_id  # Auto-generated
    assert msg.model_tier == ModelTier.STANDARD
    assert msg.priority == "normal"


def test_task_message_custom_fields():
    msg = TaskMessage(
        worker_type="classifier",
        payload={"text": "test", "categories": ["a", "b"]},
        model_tier=ModelTier.LOCAL,
        priority="high",
    )
    assert msg.model_tier == ModelTier.LOCAL
    assert msg.priority == "high"
    assert msg.worker_type == "classifier"


def test_task_result_serialization():
    result = TaskResult(
        task_id="abc",
        worker_type="summarizer",
        status=TaskStatus.COMPLETED,
        output={"summary": "test"},
    )
    data = result.model_dump(mode="json")
    restored = TaskResult(**data)
    assert restored.output == {"summary": "test"}


def test_task_result_failed():
    result = TaskResult(
        task_id="def",
        worker_type="extractor",
        status=TaskStatus.FAILED,
        error="Something went wrong",
    )
    assert result.status == TaskStatus.FAILED
    assert result.error == "Something went wrong"
    assert result.output is None


def test_task_message_roundtrip():
    msg = TaskMessage(
        worker_type="summarizer",
        payload={"text": "hello world"},
        model_tier=ModelTier.FRONTIER,
    )
    data = msg.model_dump(mode="json")
    restored = TaskMessage(**data)
    assert restored.task_id == msg.task_id
    assert restored.worker_type == msg.worker_type
    assert restored.model_tier == ModelTier.FRONTIER
    assert restored.payload == {"text": "hello world"}


# --- request_id field tests ---


def test_task_message_request_id_default_none():
    """request_id defaults to None for backward compatibility."""
    msg = TaskMessage(worker_type="summarizer", payload={"text": "hello"})
    assert msg.request_id is None


def test_task_message_request_id_set():
    """request_id can be explicitly set."""
    msg = TaskMessage(
        worker_type="summarizer",
        payload={"text": "hello"},
        request_id="req-123",
    )
    assert msg.request_id == "req-123"


def test_task_message_request_id_roundtrip():
    """request_id survives serialization/deserialization."""
    msg = TaskMessage(
        worker_type="summarizer",
        payload={"text": "hello"},
        request_id="req-abc",
    )
    data = msg.model_dump(mode="json")
    restored = TaskMessage(**data)
    assert restored.request_id == "req-abc"


def test_task_message_request_id_absent_in_dict():
    """Omitting request_id from a dict still creates a valid TaskMessage."""
    data = {
        "worker_type": "summarizer",
        "payload": {"text": "hello"},
    }
    msg = TaskMessage(**data)
    assert msg.request_id is None


def test_orchestrator_goal_request_id_default_none():
    """OrchestratorGoal.request_id defaults to None for backward compatibility."""
    goal = OrchestratorGoal(instruction="do something")
    assert goal.request_id is None


def test_orchestrator_goal_request_id_set():
    """OrchestratorGoal.request_id can be explicitly set."""
    goal = OrchestratorGoal(instruction="do something", request_id="req-goal-1")
    assert goal.request_id == "req-goal-1"


def test_orchestrator_goal_request_id_roundtrip():
    """OrchestratorGoal.request_id survives serialization/deserialization."""
    goal = OrchestratorGoal(instruction="do something", request_id="req-goal-2")
    data = goal.model_dump(mode="json")
    restored = OrchestratorGoal(**data)
    assert restored.request_id == "req-goal-2"


# --- parse_task_result helper tests ---


def test_parse_task_result_valid_returns_model():
    """A well-formed payload parses without warnings."""
    data = {
        "task_id": "abc",
        "worker_type": "summarizer",
        "status": "completed",
        "output": {"summary": "ok"},
    }
    parsed = parse_task_result(data, log_event="test.parse_error")
    assert parsed is not None
    assert parsed.task_id == "abc"
    assert parsed.status == TaskStatus.COMPLETED
    assert parsed.output == {"summary": "ok"}


def test_parse_task_result_invalid_returns_none(caplog):
    """A malformed payload returns None and logs the configured event.

    Both call sites (``dispatch.parse_error`` and
    ``result_stream.parse_error``) depend on the helper logging the
    event name they pass, so existing log queries keep working.
    """
    bad = {"task_id": "x"}  # missing required fields
    parsed = parse_task_result(bad, log_event="dispatch.parse_error", task_id="x")
    assert parsed is None


def test_parse_task_result_invalid_status_returns_none():
    """An invalid status enum value is treated as a skip, not a fatal."""
    bad = {
        "task_id": "y",
        "worker_type": "summarizer",
        "status": "bogus-status",
    }
    parsed = parse_task_result(bad, log_event="result_stream.parse_error")
    assert parsed is None


def test_parse_task_result_forwards_extra_log_fields(monkeypatch):
    """``extra_log_fields`` flow to the underlying logger call.

    ``ResultStream`` relies on this to keep its bound ``subject`` and
    ``expected`` fields on the parse-error event after extraction.
    """
    from heddle.core import messages as messages_mod

    captured: dict[str, object] = {}

    def _fake_warning(event: str, **fields: object) -> None:
        captured["event"] = event
        captured.update(fields)

    monkeypatch.setattr(messages_mod.logger, "warning", _fake_warning)

    parsed = parse_task_result(
        {"task_id": "z"},
        log_event="result_stream.parse_error",
        task_id="z",
        subject="heddle.results.goal-1",
        expected=3,
    )
    assert parsed is None
    assert captured["event"] == "result_stream.parse_error"
    assert captured["task_id"] == "z"
    assert captured["subject"] == "heddle.results.goal-1"
    assert captured["expected"] == 3
    assert "error" in captured
