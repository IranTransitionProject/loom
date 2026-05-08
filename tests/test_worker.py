"""Test LLMWorker (unit tests, no infrastructure)."""

import json
from unittest.mock import AsyncMock

import pytest
import yaml

from heddle.core.messages import ModelTier, TaskMessage, TaskResult, TaskStatus
from heddle.worker.runner import LLMWorker

# --- Mock LLM backend ---


class MockLLMBackend:
    """Fake LLM backend that returns a fixed JSON response."""

    def __init__(self, response_output=None, model="mock-llm"):
        self._output = response_output or {"summary": "test summary", "key_points": ["a"]}
        self._model = model

    async def complete(
        self, system_prompt, user_message, max_tokens=2000, temperature=0.0, **kwargs
    ):
        return {
            "content": json.dumps(self._output),
            "model": self._model,
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "tool_calls": None,
            "stop_reason": "end_turn",
        }


class BadJsonBackend:
    """Backend that returns non-JSON content."""

    async def complete(
        self, system_prompt, user_message, max_tokens=2000, temperature=0.0, **kwargs
    ):
        return {
            "content": "This is not JSON at all",
            "model": "bad-model",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "tool_calls": None,
            "stop_reason": "end_turn",
        }


# --- Config ---

LLM_CONFIG = {
    "name": "test_llm_worker",
    "system_prompt": "You are a test worker. Return JSON.",
    "default_model_tier": "local",
    "max_output_tokens": 500,
    "input_schema": {
        "type": "object",
        "required": ["text"],
        "properties": {"text": {"type": "string"}},
    },
    "output_schema": {
        "type": "object",
        "required": ["summary", "key_points"],
        "properties": {
            "summary": {"type": "string"},
            "key_points": {"type": "array"},
        },
    },
}


def _make_task(payload=None):
    return TaskMessage(
        worker_type="test_llm_worker",
        payload=payload or {"text": "hello world"},
        model_tier=ModelTier.LOCAL,
        parent_task_id="goal-789",
    ).model_dump(mode="json")


# --- Tests ---


@pytest.mark.asyncio
async def test_llm_worker_processes_task(tmp_path):
    """LLMWorker calls backend and publishes valid result."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(LLM_CONFIG))

    backends = {"local": MockLLMBackend()}
    worker = LLMWorker("llm-1", str(config_file), backends)
    worker.publish = AsyncMock()

    await worker.handle_message(_make_task())

    result = TaskResult(**worker.publish.call_args[0][1])
    assert result.status == TaskStatus.COMPLETED
    assert result.output == {"summary": "test summary", "key_points": ["a"]}
    assert result.model_used == "mock-llm"
    assert result.token_usage["prompt_tokens"] == 100
    assert result.token_usage["completion_tokens"] == 50


@pytest.mark.asyncio
async def test_llm_worker_no_backend_for_tier(tmp_path):
    """LLMWorker fails when no backend available for the requested tier."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(LLM_CONFIG))

    backends = {"standard": MockLLMBackend()}  # No "local" backend
    worker = LLMWorker("llm-1", str(config_file), backends)
    worker.publish = AsyncMock()

    await worker.handle_message(_make_task())

    result = TaskResult(**worker.publish.call_args[0][1])
    assert result.status == TaskStatus.FAILED
    assert "No backend for tier" in result.error


@pytest.mark.asyncio
async def test_llm_worker_non_json_response(tmp_path):
    """LLMWorker fails when backend returns non-JSON."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(LLM_CONFIG))

    backends = {"local": BadJsonBackend()}
    worker = LLMWorker("llm-1", str(config_file), backends)
    worker.publish = AsyncMock()

    await worker.handle_message(_make_task())

    result = TaskResult(**worker.publish.call_args[0][1])
    assert result.status == TaskStatus.FAILED
    assert "non-JSON" in result.error


@pytest.mark.asyncio
async def test_llm_worker_input_validation(tmp_path):
    """LLMWorker validates input before calling backend."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(LLM_CONFIG))

    backends = {"local": MockLLMBackend()}
    worker = LLMWorker("llm-1", str(config_file), backends)
    worker.publish = AsyncMock()

    await worker.handle_message(_make_task({"wrong": "field"}))

    result = TaskResult(**worker.publish.call_args[0][1])
    assert result.status == TaskStatus.FAILED
    assert "Input validation" in result.error


@pytest.mark.asyncio
async def test_llm_worker_output_validation(tmp_path):
    """LLMWorker validates LLM output against schema."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(LLM_CONFIG))

    # Backend returns valid JSON but wrong schema
    backends = {"local": MockLLMBackend(response_output={"bad": "schema"})}
    worker = LLMWorker("llm-1", str(config_file), backends)
    worker.publish = AsyncMock()

    await worker.handle_message(_make_task())

    result = TaskResult(**worker.publish.call_args[0][1])
    assert result.status == TaskStatus.FAILED
    assert "Output validation" in result.error


# --- File-ref resolution tests ---


@pytest.mark.asyncio
async def test_llm_worker_resolves_file_refs(tmp_path):
    """LLMWorker resolves file_ref fields and injects content into payload."""
    # Create workspace with a JSON file
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    extracted_data = {"text": "hello world", "page_count": 1}
    (workspace / "doc_extracted.json").write_text(json.dumps(extracted_data))

    # Config with file-ref resolution enabled
    config = {
        **LLM_CONFIG,
        "workspace_dir": str(workspace),
        "resolve_file_refs": ["file_ref"],
        "input_schema": {
            "type": "object",
            "required": ["file_ref"],
            "properties": {
                "file_ref": {"type": "string"},
                "file_ref_content": {"type": "object"},
            },
        },
        "output_schema": {
            "type": "object",
            "required": ["summary", "key_points"],
            "properties": {
                "summary": {"type": "string"},
                "key_points": {"type": "array"},
            },
        },
    }
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(config))

    # Spy backend that captures what it received
    received_prompts = {}

    class SpyLLMBackend:
        async def complete(
            self, system_prompt, user_message, max_tokens=2000, temperature=0.0, **kwargs
        ):
            received_prompts["user_message"] = user_message
            return {
                "content": json.dumps({"summary": "test", "key_points": ["a"]}),
                "model": "mock",
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "tool_calls": None,
                "stop_reason": "end_turn",
            }

    backends = {"local": SpyLLMBackend()}
    worker = LLMWorker("llm-1", str(config_file), backends)
    worker.publish = AsyncMock()

    task = TaskMessage(
        worker_type="test_llm_worker",
        payload={"file_ref": "doc_extracted.json"},
        model_tier=ModelTier.LOCAL,
        parent_task_id="goal-789",
    ).model_dump(mode="json")

    await worker.handle_message(task)

    # The user_message sent to the LLM should contain the resolved content
    user_msg = json.loads(received_prompts["user_message"])
    assert user_msg["file_ref"] == "doc_extracted.json"
    assert user_msg["file_ref_content"] == extracted_data


# --- Knowledge injection tests ---


@pytest.mark.asyncio
async def test_llm_worker_loads_knowledge_sources(tmp_path):
    """LLMWorker prepends knowledge sources to system prompt."""
    # Create a knowledge file
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    (knowledge_dir / "context.md").write_text("# Important Context\nThis is background knowledge.")

    config = {
        **LLM_CONFIG,
        "knowledge_sources": [
            {"path": str(knowledge_dir / "context.md"), "inject_as": "reference"},
        ],
    }
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(config))

    received_prompts = {}

    class SpyLLMBackend:
        async def complete(
            self, system_prompt, user_message, max_tokens=2000, temperature=0.0, **kwargs
        ):
            received_prompts["system_prompt"] = system_prompt
            return {
                "content": json.dumps({"summary": "test", "key_points": ["a"]}),
                "model": "mock",
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "tool_calls": None,
                "stop_reason": "end_turn",
            }

    backends = {"local": SpyLLMBackend()}
    worker = LLMWorker("llm-1", str(config_file), backends)
    worker.publish = AsyncMock()

    await worker.handle_message(_make_task())

    # System prompt should contain knowledge text prepended
    sys_prompt = received_prompts["system_prompt"]
    assert "Important Context" in sys_prompt
    assert "background knowledge" in sys_prompt
    # Original system prompt should still be there
    assert "You are a test worker" in sys_prompt


# ---------------------------------------------------------------------------
# Shutdown lifecycle (LLMWorker.disconnect closes its owned backends)
# ---------------------------------------------------------------------------


class _CloseRecordingBackend:
    """Minimal LLMBackend stand-in that records aclose() calls.

    Not registered as an :class:`LLMBackend` subclass on purpose: we
    only exercise the disconnect path, which uses duck-typed
    ``aclose``.
    """

    def __init__(self) -> None:
        self.aclose_called = 0

    async def complete(self, *a, **kw):  # pragma: no cover — not exercised
        return {
            "content": "",
            "model": "x",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "tool_calls": None,
            "stop_reason": "end_turn",
        }

    async def aclose(self) -> None:
        self.aclose_called += 1


@pytest.mark.asyncio
async def test_llm_worker_disconnect_closes_all_backends(tmp_path):
    """LLMWorker.disconnect() must aclose every backend in self.backends."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(LLM_CONFIG))

    local = _CloseRecordingBackend()
    standard = _CloseRecordingBackend()
    backends = {"local": local, "standard": standard}
    worker = LLMWorker("llm-1", str(config_file), backends)
    # Stub out the bus side of disconnect — the lifecycle test is
    # focused on backend cleanup, not NATS plumbing.
    worker._bus.close = AsyncMock()

    await worker.disconnect()

    assert local.aclose_called == 1
    assert standard.aclose_called == 1
    worker._bus.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_llm_worker_disconnect_continues_when_backend_aclose_raises(
    tmp_path, caplog
):
    """A failure in one backend's aclose must not prevent the others from closing.

    The disconnect contract is best-effort: each backend is closed
    independently and failures are logged at warning level.
    """
    import logging

    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(LLM_CONFIG))

    class FlakyBackend(_CloseRecordingBackend):
        async def aclose(self) -> None:
            self.aclose_called += 1
            raise RuntimeError("client already closed")

    flaky = FlakyBackend()
    healthy = _CloseRecordingBackend()
    worker = LLMWorker(
        "llm-1",
        str(config_file),
        {"local": flaky, "standard": healthy},
    )
    worker._bus.close = AsyncMock()

    with caplog.at_level(logging.WARNING):
        await worker.disconnect()

    assert flaky.aclose_called == 1
    assert healthy.aclose_called == 1, (
        "second backend must still be closed after the first one raises"
    )


@pytest.mark.asyncio
async def test_llm_worker_disconnect_closes_backends_even_if_bus_close_raises(tmp_path):
    """A bus-close failure must NOT swallow the backend-cleanup pass."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(LLM_CONFIG))

    backend = _CloseRecordingBackend()
    worker = LLMWorker("llm-1", str(config_file), {"local": backend})
    worker._bus.close = AsyncMock(side_effect=RuntimeError("bus exploded"))

    with pytest.raises(RuntimeError, match="bus exploded"):
        await worker.disconnect()

    # The contract: backends are closed in the finally block, so
    # they get closed even when the bus disconnect raises.
    assert backend.aclose_called == 1
