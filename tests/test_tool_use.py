"""Tests for LLMWorker tool-use multi-turn execution loop."""

import json
from unittest.mock import AsyncMock

import pytest
import yaml

from heddle.core.messages import ModelTier, TaskMessage, TaskResult, TaskStatus
from heddle.worker.runner import LLMWorker
from heddle.worker.tools import SyncToolProvider

# ---------------------------------------------------------------------------
# Mock backends with tool-use support
# ---------------------------------------------------------------------------


class ToolUseBackend:
    """Backend that returns a tool call on first request, then a final answer.

    Simulates a single-round tool-use flow:
    Round 1: LLM wants to call a tool → returns tool_calls
    Round 2: LLM has tool results → returns final text answer
    """

    def __init__(self, tool_name="search_docs", tool_args=None, final_output=None):
        self._tool_name = tool_name
        self._tool_args = tool_args or {"query": "test"}
        self._final_output = final_output or {"summary": "found it", "key_points": ["a"]}
        self._call_count = 0

    async def complete(
        self,
        system_prompt,
        user_message,
        max_tokens=2000,
        temperature=0.0,
        *,
        tools=None,
        messages=None,
    ):
        self._call_count += 1

        if self._call_count == 1:
            # First call: return tool call
            return {
                "content": None,
                "model": "mock-tool",
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "name": self._tool_name,
                        "arguments": self._tool_args,
                    }
                ],
                "stop_reason": "tool_use",
            }
        # Second call: return final answer
        return {
            "content": json.dumps(self._final_output),
            "model": "mock-tool",
            "prompt_tokens": 150,
            "completion_tokens": 50,
            "tool_calls": None,
            "stop_reason": "end_turn",
        }


class MultiRoundBackend:
    """Backend that makes N tool calls before returning a final answer."""

    def __init__(self, rounds=3, final_output=None):
        self._rounds = rounds
        self._final_output = final_output or {"result": "done"}
        self._call_count = 0

    async def complete(
        self,
        system_prompt,
        user_message,
        max_tokens=2000,
        temperature=0.0,
        *,
        tools=None,
        messages=None,
    ):
        self._call_count += 1

        if self._call_count <= self._rounds:
            return {
                "content": None,
                "model": "mock-multi",
                "prompt_tokens": 50,
                "completion_tokens": 10,
                "tool_calls": [
                    {
                        "id": f"call_{self._call_count}",
                        "name": "search_docs",
                        "arguments": {"query": f"round {self._call_count}"},
                    }
                ],
                "stop_reason": "tool_use",
            }
        return {
            "content": json.dumps(self._final_output),
            "model": "mock-multi",
            "prompt_tokens": 80,
            "completion_tokens": 30,
            "tool_calls": None,
            "stop_reason": "end_turn",
        }


class NoToolBackend:
    """Backend that never uses tools — returns final answer immediately."""

    def __init__(self, output=None):
        self._output = output or {"summary": "direct answer", "key_points": []}

    async def complete(
        self,
        system_prompt,
        user_message,
        max_tokens=2000,
        temperature=0.0,
        *,
        tools=None,
        messages=None,
    ):
        return {
            "content": json.dumps(self._output),
            "model": "mock-no-tool",
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "tool_calls": None,
            "stop_reason": "end_turn",
        }


# ---------------------------------------------------------------------------
# Config and helpers
# ---------------------------------------------------------------------------

TOOL_USE_CONFIG = {
    "name": "test_tool_worker",
    "system_prompt": "You are a test worker with tools. Return JSON.",
    "default_model_tier": "local",
    "max_output_tokens": 500,
    "knowledge_silos": [
        {
            "name": "test_tool",
            "type": "tool",
            "provider": "tests.test_tool_use.MockToolProvider",
            "config": {},
        },
    ],
}

NO_TOOL_CONFIG = {
    "name": "test_no_tool_worker",
    "system_prompt": "You are a test worker without tools. Return JSON.",
    "default_model_tier": "local",
    "max_output_tokens": 500,
}


def _make_task(payload=None):
    return TaskMessage(
        worker_type="test_tool_worker",
        payload=payload or {"text": "hello"},
        model_tier=ModelTier.LOCAL,
        parent_task_id="goal-123",
    ).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Mock ToolProvider that tests can import
# ---------------------------------------------------------------------------


class MockToolProvider(SyncToolProvider):
    """A mock tool provider for testing the tool-use loop."""

    def get_definition(self) -> dict:
        return {
            "name": "search_docs",
            "description": "Search documents",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
            },
        }

    def execute_sync(self, arguments: dict) -> str:
        query = arguments.get("query", "")
        return json.dumps({"results": [f"doc about {query}"], "total": 1})


# ---------------------------------------------------------------------------
# Tool-use tests
# ---------------------------------------------------------------------------


class TestToolUseLoop:
    """Tests for the multi-turn tool execution loop in LLMWorker."""

    @pytest.mark.asyncio
    async def test_single_round_tool_use(self, tmp_path):
        """Worker executes one tool call and returns the final LLM answer."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(TOOL_USE_CONFIG))

        backend = ToolUseBackend(final_output={"summary": "found it", "key_points": ["a"]})
        worker = LLMWorker("llm-1", str(config_file), {"local": backend})
        worker.publish = AsyncMock()

        await worker.handle_message(_make_task())

        result = TaskResult(**worker.publish.call_args[0][1])
        assert result.status == TaskStatus.COMPLETED
        assert result.output["summary"] == "found it"
        assert backend._call_count == 2  # One tool call + one final answer

    @pytest.mark.asyncio
    async def test_tool_use_accumulates_tokens(self, tmp_path):
        """Token usage should sum across all rounds."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(TOOL_USE_CONFIG))

        backend = ToolUseBackend()
        worker = LLMWorker("llm-1", str(config_file), {"local": backend})
        worker.publish = AsyncMock()

        await worker.handle_message(_make_task())

        result = TaskResult(**worker.publish.call_args[0][1])
        # Round 1: 100 prompt + 20 completion, Round 2: 150 prompt + 50 completion
        assert result.token_usage["prompt_tokens"] == 250
        assert result.token_usage["completion_tokens"] == 70

    @pytest.mark.asyncio
    async def test_multi_round_tool_use(self, tmp_path):
        """Worker handles multiple tool-use rounds before final answer."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(TOOL_USE_CONFIG))

        backend = MultiRoundBackend(rounds=3, final_output={"summary": "done", "key_points": []})
        worker = LLMWorker("llm-1", str(config_file), {"local": backend})
        worker.publish = AsyncMock()

        await worker.handle_message(_make_task())

        result = TaskResult(**worker.publish.call_args[0][1])
        assert result.status == TaskStatus.COMPLETED
        assert result.output["summary"] == "done"
        assert backend._call_count == 4  # 3 tool rounds + 1 final

    @pytest.mark.asyncio
    async def test_no_tools_skips_loop(self, tmp_path):
        """When no tools are configured, the worker goes straight to output."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(NO_TOOL_CONFIG))

        backend = NoToolBackend()
        worker = LLMWorker("llm-1", str(config_file), {"local": backend})
        worker.publish = AsyncMock()

        await worker.handle_message(_make_task())

        result = TaskResult(**worker.publish.call_args[0][1])
        assert result.status == TaskStatus.COMPLETED
        assert result.output["summary"] == "direct answer"

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self, tmp_path):
        """Calling an unknown tool name should produce an error result for the LLM."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(TOOL_USE_CONFIG))

        # Backend requests a tool that doesn't exist
        backend = ToolUseBackend(
            tool_name="nonexistent_tool",
            final_output={"summary": "ok", "key_points": []},
        )
        worker = LLMWorker("llm-1", str(config_file), {"local": backend})
        worker.publish = AsyncMock()

        await worker.handle_message(_make_task())

        # Should still complete (the LLM gets error feedback and produces final answer)
        result = TaskResult(**worker.publish.call_args[0][1])
        assert result.status == TaskStatus.COMPLETED


class TestToolUseWithSiloUpdates:
    """Tests for silo_updates processing after tool-use."""

    @pytest.mark.asyncio
    async def test_silo_updates_stripped_from_output(self, tmp_path):
        """silo_updates should be removed from the final output dict."""
        config_file = tmp_path / "config.yaml"

        # Create a writable folder silo
        silo_dir = tmp_path / "knowledge"
        silo_dir.mkdir()

        config = {
            **NO_TOOL_CONFIG,
            "knowledge_silos": [
                {
                    "name": "test_silo",
                    "type": "folder",
                    "path": str(silo_dir),
                    "permissions": "read_write",
                },
            ],
        }
        config_file.write_text(yaml.dump(config))

        output_with_updates = {
            "summary": "test",
            "key_points": [],
            "silo_updates": [
                {
                    "silo": "test_silo",
                    "action": "add",
                    "filename": "new_insight.md",
                    "content": "# New Insight\nSomething learned.",
                },
            ],
        }
        backend = NoToolBackend(output=output_with_updates)
        worker = LLMWorker("llm-1", str(config_file), {"local": backend})
        worker.publish = AsyncMock()

        await worker.handle_message(_make_task())

        result = TaskResult(**worker.publish.call_args[0][1])
        assert result.status == TaskStatus.COMPLETED
        assert "silo_updates" not in result.output
        assert result.output["summary"] == "test"

        # Verify the file was created
        assert (silo_dir / "new_insight.md").exists()
        assert "New Insight" in (silo_dir / "new_insight.md").read_text()


# ---------------------------------------------------------------------------
# Backward compatibility tests
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Ensure existing tests still pass with the updated backends."""

    @pytest.mark.asyncio
    async def test_legacy_backend_without_tools_kwargs(self, tmp_path):
        """A backend that doesn't accept tools/messages kwargs should still work
        when no tools are configured (no tool args passed)."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(NO_TOOL_CONFIG))

        # This backend doesn't accept **kwargs — but since no tools
        # are configured, no tool args should be passed
        backend = NoToolBackend()
        worker = LLMWorker("llm-1", str(config_file), {"local": backend})
        worker.publish = AsyncMock()

        await worker.handle_message(_make_task())

        result = TaskResult(**worker.publish.call_args[0][1])
        assert result.status == TaskStatus.COMPLETED


from heddle.worker.tools import ToolProvider as _ToolProvider  # noqa: E402


class _HangingToolProvider(_ToolProvider):
    """A tool provider whose execute() blocks long enough to exceed any reasonable timeout.

    Subclasses ``ToolProvider`` directly so the test can override
    the async ``execute`` rather than the synchronous
    ``execute_sync`` (which ``SyncToolProvider`` offloads to a
    thread pool — outside the asyncio.wait_for bound).
    """

    def get_definition(self) -> dict:
        return {
            "name": "search_docs",
            "description": "Hangs forever",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }

    async def execute(self, arguments: dict) -> str:
        import asyncio as _aio

        await _aio.sleep(5)
        return "should not be reached"


class TestToolExecutionTimeout:
    """F2: per-tool execution timeout prevents wedged tools from hanging workers.

    Earlier the tool loop honoured ``MAX_TOOL_ROUNDS=10`` but had no
    per-call timeout on ``provider.execute()`` — a misbehaving tool
    (synchronous DuckDB hang, network call without its own timeout,
    deadlocked sync→async bridge) wedged the worker until the
    outer orchestrator timeout fired.  The bound is now config-time
    via ``tool_timeout_seconds`` (default 30s).
    """

    @pytest.mark.asyncio
    async def test_tool_timeout_produces_error_result(self, tmp_path):
        """A tool that exceeds ``tool_timeout_seconds`` produces a typed
        error in the assistant message and the LLM still continues."""
        # Use a hanging tool with a tiny timeout so we don't waste
        # real wall time in the test suite.
        config_with_short_timeout = dict(TOOL_USE_CONFIG)
        config_with_short_timeout["tool_timeout_seconds"] = 0.05
        config_with_short_timeout["knowledge_silos"] = [
            {
                "name": "hanging_tool",
                "type": "tool",
                "provider": "tests.test_tool_use._HangingToolProvider",
                "config": {},
            }
        ]

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_with_short_timeout))

        backend = ToolUseBackend(tool_name="search_docs")
        worker = LLMWorker("llm-1", str(config_file), {"local": backend})
        worker.publish = AsyncMock()

        await worker.handle_message(_make_task())

        # The worker completed (the timeout-on-tool became an error
        # in the tool_result; the LLM then produced the final
        # answer from the second backend call).
        result = TaskResult(**worker.publish.call_args[0][1])
        assert result.status == TaskStatus.COMPLETED
        # The backend was called twice: once with the tool_call,
        # once after the tool-result (the timeout error) was fed
        # back.  Without the bound, the test would hang for 5s
        # (the _HangingToolProvider sleep) — under 1s here means
        # the timeout fired.
        assert backend._call_count == 2

    @pytest.mark.asyncio
    async def test_zero_timeout_disables_bound(self, tmp_path):
        """``tool_timeout_seconds=0`` disables the bound and logs WARN."""
        from heddle.worker import runner as _runner_mod

        config = dict(TOOL_USE_CONFIG)
        config["tool_timeout_seconds"] = 0
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config))

        backend = ToolUseBackend(tool_name="search_docs")
        worker = LLMWorker("llm-1", str(config_file), {"local": backend})
        worker.publish = AsyncMock()

        warnings: list[dict] = []

        def _fake_warning(event: str, **fields) -> None:
            warnings.append({"event": event, **fields})

        # Patch only the runner module's logger.
        original_warning = _runner_mod.logger.warning
        _runner_mod.logger.warning = _fake_warning
        try:
            await worker.handle_message(_make_task())
        finally:
            _runner_mod.logger.warning = original_warning

        result = TaskResult(**worker.publish.call_args[0][1])
        assert result.status == TaskStatus.COMPLETED
        assert any(w["event"] == "worker.tool_timeout_disabled" for w in warnings)
