"""Tests for LLM backend adapters (heddle.worker.backends).

Covers:
- Message format converters: _anthropic_messages, _ollama_messages, _openai_messages
- Backend complete() methods with mocked httpx responses
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from heddle.worker.backends import (
    AnthropicBackend,
    LMStudioBackend,
    OllamaBackend,
    OpenAICompatibleBackend,
    _anthropic_messages,
    _ollama_messages,
    _openai_messages,
    _select_local_backend,
    build_backends_from_env,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(json_data: dict) -> MagicMock:
    """Create a mock httpx.Response with .json() and .raise_for_status().

    Note: httpx Response.json() and .raise_for_status() are sync methods,
    so we use MagicMock (not AsyncMock) for the response object.
    """
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# _anthropic_messages
# ---------------------------------------------------------------------------


class TestAnthropicMessages:
    def test_user_passthrough(self):
        msgs = [{"role": "user", "content": "hello"}]
        result = _anthropic_messages(msgs)
        assert result == [{"role": "user", "content": "hello"}]

    def test_assistant_with_content_only(self):
        msgs = [{"role": "assistant", "content": "hi there"}]
        result = _anthropic_messages(msgs)
        assert result == [
            {"role": "assistant", "content": [{"type": "text", "text": "hi there"}]},
        ]

    def test_assistant_with_tool_calls_only(self):
        msgs = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "tc_1", "name": "search", "arguments": {"q": "cats"}},
                ],
            },
        ]
        result = _anthropic_messages(msgs)
        assert len(result) == 1
        blocks = result[0]["content"]
        assert len(blocks) == 1
        assert blocks[0] == {
            "type": "tool_use",
            "id": "tc_1",
            "name": "search",
            "input": {"q": "cats"},
        }

    def test_assistant_with_content_and_tool_calls(self):
        msgs = [
            {
                "role": "assistant",
                "content": "Let me search.",
                "tool_calls": [
                    {"id": "tc_1", "name": "search", "arguments": {"q": "dogs"}},
                ],
            },
        ]
        result = _anthropic_messages(msgs)
        blocks = result[0]["content"]
        assert len(blocks) == 2
        assert blocks[0] == {"type": "text", "text": "Let me search."}
        assert blocks[1]["type"] == "tool_use"
        assert blocks[1]["name"] == "search"

    def test_tool_result_maps_to_user_role(self):
        msgs = [
            {
                "role": "tool",
                "tool_call_id": "tc_1",
                "content": '{"results": []}',
            },
        ]
        result = _anthropic_messages(msgs)
        assert result[0]["role"] == "user"
        assert result[0]["content"] == [
            {
                "type": "tool_result",
                "tool_use_id": "tc_1",
                "content": '{"results": []}',
            },
        ]

    def test_multi_turn_conversation(self):
        msgs = [
            {"role": "user", "content": "search for cats"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "tc_1", "name": "search", "arguments": {"q": "cats"}}],
            },
            {"role": "tool", "tool_call_id": "tc_1", "content": "found 3 results"},
            {"role": "assistant", "content": "I found 3 results about cats."},
        ]
        result = _anthropic_messages(msgs)
        assert len(result) == 4
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert result[2]["role"] == "user"  # tool result -> user
        assert result[3]["role"] == "assistant"


# ---------------------------------------------------------------------------
# _ollama_messages
# ---------------------------------------------------------------------------


class TestOllamaMessages:
    def test_user_passthrough(self):
        msgs = [{"role": "user", "content": "hello"}]
        result = _ollama_messages(msgs)
        assert result == [{"role": "user", "content": "hello"}]

    def test_assistant_with_content(self):
        msgs = [{"role": "assistant", "content": "hi"}]
        result = _ollama_messages(msgs)
        assert result == [{"role": "assistant", "content": "hi"}]

    def test_assistant_with_tool_calls_no_id(self):
        """Ollama format uses 'function' dict without 'id' field."""
        msgs = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "tc_1", "name": "search", "arguments": {"q": "cats"}},
                ],
            },
        ]
        result = _ollama_messages(msgs)
        entry = result[0]
        assert entry["role"] == "assistant"
        assert "content" not in entry  # None content is omitted
        assert len(entry["tool_calls"]) == 1
        tc = entry["tool_calls"][0]
        # No "id" in Ollama format
        assert "id" not in tc
        assert tc["function"]["name"] == "search"
        assert tc["function"]["arguments"] == {"q": "cats"}

    def test_tool_result_has_role_tool(self):
        msgs = [
            {"role": "tool", "tool_call_id": "tc_1", "content": "result data"},
        ]
        result = _ollama_messages(msgs)
        assert result == [{"role": "tool", "content": "result data"}]
        # tool_call_id is NOT included in Ollama format
        assert "tool_call_id" not in result[0]


# ---------------------------------------------------------------------------
# _openai_messages
# ---------------------------------------------------------------------------


class TestOpenAIMessages:
    def test_user_passthrough(self):
        msgs = [{"role": "user", "content": "hello"}]
        result = _openai_messages(msgs)
        assert result == [{"role": "user", "content": "hello"}]

    def test_assistant_tool_calls_serialize_arguments(self):
        """OpenAI format serializes arguments to a JSON string."""
        msgs = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_abc", "name": "search", "arguments": {"q": "cats"}},
                ],
            },
        ]
        result = _openai_messages(msgs)
        tc = result[0]["tool_calls"][0]
        assert tc["id"] == "call_abc"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "search"
        # arguments must be a JSON string, not a dict
        assert isinstance(tc["function"]["arguments"], str)
        assert json.loads(tc["function"]["arguments"]) == {"q": "cats"}

    def test_tool_result_includes_tool_call_id(self):
        msgs = [
            {"role": "tool", "tool_call_id": "call_abc", "content": "some result"},
        ]
        result = _openai_messages(msgs)
        assert result == [
            {"role": "tool", "tool_call_id": "call_abc", "content": "some result"},
        ]

    def test_assistant_with_content_and_tool_calls(self):
        msgs = [
            {
                "role": "assistant",
                "content": "Searching now.",
                "tool_calls": [
                    {"id": "call_1", "name": "lookup", "arguments": {"key": "x"}},
                ],
            },
        ]
        result = _openai_messages(msgs)
        entry = result[0]
        assert entry["content"] == "Searching now."
        assert len(entry["tool_calls"]) == 1


# ---------------------------------------------------------------------------
# AnthropicBackend.complete
# ---------------------------------------------------------------------------


class TestAnthropicBackendComplete:
    @pytest.fixture
    def backend(self):
        return AnthropicBackend(api_key="test-key", model="claude-test")

    @pytest.mark.asyncio
    async def test_text_response(self, backend):
        api_data = {
            "model": "claude-test",
            "content": [{"type": "text", "text": "Hello world"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "stop_reason": "end_turn",
        }
        with patch.object(backend.client, "post", return_value=_mock_response(api_data)):
            result = await backend.complete("sys prompt", "user msg")

        assert result["content"] == "Hello world"
        assert result["model"] == "claude-test"
        assert result["prompt_tokens"] == 10
        assert result["completion_tokens"] == 5
        assert result["tool_calls"] is None
        assert result["stop_reason"] == "end_turn"
        # GenAI semantic convention metadata
        assert result["gen_ai_system"] == "anthropic"
        assert result["gen_ai_request_model"] == "claude-test"
        assert result["gen_ai_response_model"] == "claude-test"
        assert result["gen_ai_request_temperature"] == 0.0
        assert result["gen_ai_request_max_tokens"] == 2000

    @pytest.mark.asyncio
    async def test_tool_use_response(self, backend):
        api_data = {
            "model": "claude-test",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_123",
                    "name": "search",
                    "input": {"query": "cats"},
                },
            ],
            "usage": {"input_tokens": 20, "output_tokens": 15},
            "stop_reason": "tool_use",
        }
        with patch.object(backend.client, "post", return_value=_mock_response(api_data)):
            result = await backend.complete("sys", "msg", tools=[{"name": "search"}])

        assert result["content"] is None
        assert result["tool_calls"] == [
            {"id": "toolu_123", "name": "search", "arguments": {"query": "cats"}},
        ]
        assert result["stop_reason"] == "tool_use"

    @pytest.mark.asyncio
    async def test_pins_stable_api_version(self, backend):
        """D1: anthropic-version header is the documented stable 2023-06-01."""
        assert backend.client.headers["anthropic-version"] == "2023-06-01"

    @pytest.mark.asyncio
    async def test_extended_thinking_blocks_surfaced(self, backend):
        """D1: ``thinking`` blocks land on the response dict's ``thinking`` key.

        Earlier the parser filtered on ``type == "text"`` only and
        silently discarded any ``thinking`` block.  When a caller
        enables extended thinking (which the current Heddle API
        doesn't expose yet, but third-party callers can build via
        the SDK), the reasoning trace is now available.
        """
        api_data = {
            "model": "claude-test",
            "content": [
                {"type": "thinking", "thinking": "step 1: analyse..."},
                {"type": "thinking", "thinking": " step 2: conclude."},
                {"type": "text", "text": "Final answer."},
            ],
            "usage": {"input_tokens": 8, "output_tokens": 12},
            "stop_reason": "end_turn",
        }
        with patch.object(backend.client, "post", return_value=_mock_response(api_data)):
            result = await backend.complete("sys", "msg")
        assert result["content"] == "Final answer."
        assert result["thinking"] == "step 1: analyse... step 2: conclude."

    @pytest.mark.asyncio
    async def test_mixed_text_and_tool_use(self, backend):
        api_data = {
            "model": "claude-test",
            "content": [
                {"type": "text", "text": "Let me search."},
                {"type": "tool_use", "id": "toolu_1", "name": "search", "input": {"q": "x"}},
            ],
            "usage": {"input_tokens": 10, "output_tokens": 10},
            "stop_reason": "tool_use",
        }
        with patch.object(backend.client, "post", return_value=_mock_response(api_data)):
            result = await backend.complete("sys", "msg", tools=[{"name": "search"}])

        assert result["content"] == "Let me search."
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["name"] == "search"

    @pytest.mark.asyncio
    async def test_multiple_text_blocks_are_accumulated(self, backend):
        """Multiple text blocks in one response are concatenated, not lost.

        Regression test for the original bug: ``content = block["text"]``
        overwrote on each iteration, so any text after the first
        ``tool_use`` block was silently dropped.  Anthropic returns
        text → tool_use → text patterns whenever a model thinks aloud
        between tool calls; without the fix, the trailing text was
        lost on every such response.
        """
        api_data = {
            "model": "claude-test",
            "content": [
                {"type": "text", "text": "Let me check that. "},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "search",
                    "input": {"q": "x"},
                },
                {"type": "text", "text": "Calling search now."},
            ],
            "usage": {"input_tokens": 10, "output_tokens": 20},
            "stop_reason": "tool_use",
        }
        with patch.object(backend.client, "post", return_value=_mock_response(api_data)):
            result = await backend.complete("sys", "msg", tools=[{"name": "search"}])

        # Both text blocks must be preserved in order.
        assert result["content"] == "Let me check that. Calling search now."
        # Tool call still surfaces.
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["id"] == "toolu_1"

    @pytest.mark.asyncio
    async def test_text_only_no_tool_use_returns_none_content(self, backend):
        """A response with no text blocks returns ``content=None`` (unchanged)."""
        api_data = {
            "model": "claude-test",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "search",
                    "input": {"q": "x"},
                },
            ],
            "usage": {"input_tokens": 10, "output_tokens": 10},
            "stop_reason": "tool_use",
        }
        with patch.object(backend.client, "post", return_value=_mock_response(api_data)):
            result = await backend.complete("sys", "msg", tools=[{"name": "search"}])

        assert result["content"] is None  # not "" — distinguishes "no text" from "empty"

    @pytest.mark.asyncio
    async def test_tools_parameter_transforms_format(self, backend):
        """Verify that the tools list is transformed to Anthropic's schema format."""
        api_data = {
            "model": "claude-test",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 5, "output_tokens": 5},
            "stop_reason": "end_turn",
        }
        mock_post = AsyncMock(return_value=_mock_response(api_data))
        with patch.object(backend.client, "post", mock_post):
            await backend.complete(
                "sys",
                "msg",
                tools=[
                    {
                        "name": "my_tool",
                        "description": "A tool",
                        "parameters": {"type": "object", "properties": {"x": {"type": "string"}}},
                    }
                ],
            )

        call_body = mock_post.call_args[1]["json"]
        assert "tools" in call_body
        tool = call_body["tools"][0]
        assert tool["name"] == "my_tool"
        assert tool["description"] == "A tool"
        # Anthropic uses "input_schema" not "parameters"
        assert "input_schema" in tool
        assert "parameters" not in tool

    @pytest.mark.asyncio
    async def test_messages_parameter_uses_converter(self, backend):
        """When messages is provided, it should use _anthropic_messages instead of user_message."""
        api_data = {
            "model": "claude-test",
            "content": [{"type": "text", "text": "response"}],
            "usage": {"input_tokens": 5, "output_tokens": 5},
            "stop_reason": "end_turn",
        }
        mock_post = AsyncMock(return_value=_mock_response(api_data))
        with patch.object(backend.client, "post", mock_post):
            await backend.complete(
                "sys",
                "ignored_user_msg",
                messages=[{"role": "user", "content": "actual msg"}],
            )

        call_body = mock_post.call_args[1]["json"]
        assert call_body["messages"] == [{"role": "user", "content": "actual msg"}]


# ---------------------------------------------------------------------------
# OllamaBackend.complete
# ---------------------------------------------------------------------------


class TestOllamaBackendComplete:
    @pytest.fixture
    def backend(self):
        return OllamaBackend(model="llama3:8b", base_url="http://localhost:11434")

    @pytest.mark.asyncio
    async def test_basic_response(self, backend):
        api_data = {
            "message": {"role": "assistant", "content": "Hello!"},
            "prompt_eval_count": 12,
            "eval_count": 8,
        }
        with patch.object(backend.client, "post", return_value=_mock_response(api_data)):
            result = await backend.complete("sys", "msg")

        assert result["content"] == "Hello!"
        assert result["model"] == "llama3:8b"
        assert result["prompt_tokens"] == 12
        assert result["completion_tokens"] == 8
        assert result["tool_calls"] is None
        assert result["stop_reason"] == "end_turn"
        # GenAI semantic convention metadata
        assert result["gen_ai_system"] == "ollama"
        assert result["gen_ai_request_model"] == "llama3:8b"
        assert result["gen_ai_response_model"] == "llama3:8b"
        assert result["gen_ai_request_temperature"] == 0.0
        assert result["gen_ai_request_max_tokens"] == 2000

    @pytest.mark.asyncio
    async def test_tool_calls_response(self, backend):
        api_data = {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "search", "arguments": {"q": "cats"}}},
                    {"function": {"name": "fetch", "arguments": {"url": "http://x"}}},
                ],
            },
            "prompt_eval_count": 20,
            "eval_count": 15,
        }
        with patch.object(backend.client, "post", return_value=_mock_response(api_data)):
            result = await backend.complete("sys", "msg", tools=[{"name": "search"}])

        assert result["content"] is None  # empty string -> None
        assert len(result["tool_calls"]) == 2
        assert result["tool_calls"][0] == {
            "id": "call_0",
            "name": "search",
            "arguments": {"q": "cats"},
        }
        assert result["tool_calls"][1]["id"] == "call_1"
        assert result["stop_reason"] == "tool_use"

    @pytest.mark.asyncio
    async def test_missing_token_counts_default_to_zero(self, backend):
        """Some Ollama models omit prompt_eval_count and eval_count."""
        api_data = {
            "message": {"role": "assistant", "content": "response"},
        }
        with patch.object(backend.client, "post", return_value=_mock_response(api_data)):
            result = await backend.complete("sys", "msg")

        assert result["prompt_tokens"] == 0
        assert result["completion_tokens"] == 0

    @pytest.mark.asyncio
    async def test_messages_parameter_prepends_system(self, backend):
        """When messages is provided, system prompt is prepended."""
        api_data = {
            "message": {"role": "assistant", "content": "ok"},
        }
        mock_post = AsyncMock(return_value=_mock_response(api_data))
        with patch.object(backend.client, "post", mock_post):
            await backend.complete(
                "system instructions",
                "ignored",
                messages=[{"role": "user", "content": "actual"}],
            )

        call_body = mock_post.call_args[1]["json"]
        assert call_body["messages"][0] == {"role": "system", "content": "system instructions"}
        assert call_body["messages"][1] == {"role": "user", "content": "actual"}

    @pytest.mark.asyncio
    async def test_tools_parameter_formats_as_function_type(self, backend):
        """Ollama tools should use the OpenAI-compatible function format."""
        api_data = {"message": {"role": "assistant", "content": "ok"}}
        mock_post = AsyncMock(return_value=_mock_response(api_data))
        with patch.object(backend.client, "post", mock_post):
            await backend.complete(
                "sys",
                "msg",
                tools=[{"name": "my_fn", "description": "desc", "parameters": {"type": "object"}}],
            )

        call_body = mock_post.call_args[1]["json"]
        tool = call_body["tools"][0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "my_fn"
        assert tool["function"]["description"] == "desc"


# ---------------------------------------------------------------------------
# OpenAICompatibleBackend.complete
# ---------------------------------------------------------------------------


class TestOpenAICompatibleBackendComplete:
    @pytest.fixture
    def backend(self):
        return OpenAICompatibleBackend(
            base_url="http://localhost:8000",
            api_key="test-key",
            model="gpt-test",
        )

    @pytest.mark.asyncio
    async def test_basic_response(self, backend):
        api_data = {
            "model": "gpt-test",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hello!"},
                    "finish_reason": "stop",
                },
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        with patch.object(backend.client, "post", return_value=_mock_response(api_data)):
            result = await backend.complete("sys", "msg")

        assert result["content"] == "Hello!"
        assert result["model"] == "gpt-test"
        assert result["prompt_tokens"] == 10
        assert result["completion_tokens"] == 5
        assert result["tool_calls"] is None
        assert result["stop_reason"] == "end_turn"
        # GenAI semantic convention metadata
        assert result["gen_ai_system"] == "openai"
        assert result["gen_ai_request_model"] == "gpt-test"
        assert result["gen_ai_response_model"] == "gpt-test"
        assert result["gen_ai_request_temperature"] == 0.0
        assert result["gen_ai_request_max_tokens"] == 2000

    @pytest.mark.asyncio
    async def test_tool_calls_with_string_arguments(self, backend):
        """OpenAI API returns arguments as JSON strings; backend should parse them."""
        api_data = {
            "model": "gpt-test",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "function": {
                                    "name": "search",
                                    "arguments": '{"query": "cats"}',
                                },
                            },
                        ],
                    },
                    "finish_reason": "tool_calls",
                },
            ],
            "usage": {"prompt_tokens": 15, "completion_tokens": 10},
        }
        with patch.object(backend.client, "post", return_value=_mock_response(api_data)):
            result = await backend.complete("sys", "msg", tools=[{"name": "search"}])

        assert result["tool_calls"] == [
            {"id": "call_abc", "name": "search", "arguments": {"query": "cats"}},
        ]
        assert result["stop_reason"] == "tool_use"

    @pytest.mark.asyncio
    async def test_tool_calls_with_dict_arguments(self, backend):
        """Some OpenAI-compatible servers return arguments as dicts already."""
        api_data = {
            "model": "gpt-test",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_xyz",
                                "function": {
                                    "name": "fetch",
                                    "arguments": {"url": "http://example.com"},
                                },
                            },
                        ],
                    },
                    "finish_reason": "tool_calls",
                },
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 8},
        }
        with patch.object(backend.client, "post", return_value=_mock_response(api_data)):
            result = await backend.complete("sys", "msg")

        # When arguments is already a dict, isinstance(args, str) is False,
        # so json.loads is skipped and the dict is used directly
        assert result["tool_calls"][0]["arguments"] == {"url": "http://example.com"}

    @pytest.mark.asyncio
    async def test_malformed_arguments_string_falls_back_to_raw(self, backend):
        """If arguments is a non-JSON string, it's wrapped as {"_raw": ...}."""
        api_data = {
            "model": "gpt-test",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_bad",
                                "function": {
                                    "name": "broken",
                                    "arguments": "not valid json{{{",
                                },
                            },
                        ],
                    },
                    "finish_reason": "tool_calls",
                },
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5},
        }
        with patch.object(backend.client, "post", return_value=_mock_response(api_data)):
            result = await backend.complete("sys", "msg")
        assert result["tool_calls"][0]["arguments"] == {"_raw": "not valid json{{{"}

    @pytest.mark.asyncio
    async def test_finish_reason_stop_maps_to_end_turn(self, backend):
        api_data = {
            "model": "gpt-test",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "done"},
                    "finish_reason": "stop",
                },
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        }
        with patch.object(backend.client, "post", return_value=_mock_response(api_data)):
            result = await backend.complete("sys", "msg")
        assert result["stop_reason"] == "end_turn"

    @pytest.mark.asyncio
    async def test_finish_reason_tool_calls_maps_to_tool_use(self, backend):
        api_data = {
            "model": "gpt-test",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": "c1", "function": {"name": "fn", "arguments": "{}"}},
                        ],
                    },
                    "finish_reason": "tool_calls",
                },
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5},
        }
        with patch.object(backend.client, "post", return_value=_mock_response(api_data)):
            result = await backend.complete("sys", "msg")
        assert result["stop_reason"] == "tool_use"

    @pytest.mark.asyncio
    async def test_finish_reason_length_maps_to_end_turn(self, backend):
        """Any finish_reason other than 'tool_calls' should map to 'end_turn'."""
        api_data = {
            "model": "gpt-test",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "truncated"},
                    "finish_reason": "length",
                },
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 100},
        }
        with patch.object(backend.client, "post", return_value=_mock_response(api_data)):
            result = await backend.complete("sys", "msg")
        assert result["stop_reason"] == "end_turn"

    @pytest.mark.asyncio
    async def test_missing_usage_defaults_to_zero(self, backend):
        api_data = {
            "model": "gpt-test",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                },
            ],
            # no "usage" key at all
        }
        with patch.object(backend.client, "post", return_value=_mock_response(api_data)):
            result = await backend.complete("sys", "msg")
        assert result["prompt_tokens"] == 0
        assert result["completion_tokens"] == 0

    @pytest.mark.asyncio
    async def test_model_falls_back_to_self_model(self, backend):
        """If the response doesn't include 'model', use the backend's configured model."""
        api_data = {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                },
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        with patch.object(backend.client, "post", return_value=_mock_response(api_data)):
            result = await backend.complete("sys", "msg")
        assert result["model"] == "gpt-test"
        assert result["gen_ai_response_model"] == "gpt-test"

    @pytest.mark.asyncio
    async def test_gen_ai_response_model_from_api(self, backend):
        """gen_ai_response_model should reflect the actual model from the API."""
        api_data = {
            "model": "gpt-4-turbo-actual",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                },
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        with patch.object(backend.client, "post", return_value=_mock_response(api_data)):
            result = await backend.complete("sys", "msg")
        assert result["gen_ai_request_model"] == "gpt-test"
        assert result["gen_ai_response_model"] == "gpt-4-turbo-actual"

    @pytest.mark.asyncio
    async def test_gen_ai_custom_temperature_and_max_tokens(self, backend):
        """gen_ai_request_temperature and gen_ai_request_max_tokens reflect call params."""
        api_data = {
            "model": "gpt-test",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                },
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        with patch.object(backend.client, "post", return_value=_mock_response(api_data)):
            result = await backend.complete("sys", "msg", max_tokens=500, temperature=0.7)
        assert result["gen_ai_request_temperature"] == 0.7
        assert result["gen_ai_request_max_tokens"] == 500

    @pytest.mark.asyncio
    async def test_messages_parameter_prepends_system(self, backend):
        api_data = {
            "model": "gpt-test",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                },
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        }
        mock_post = AsyncMock(return_value=_mock_response(api_data))
        with patch.object(backend.client, "post", mock_post):
            await backend.complete(
                "system instructions",
                "ignored",
                messages=[{"role": "user", "content": "real msg"}],
            )

        call_body = mock_post.call_args[1]["json"]
        assert call_body["messages"][0] == {"role": "system", "content": "system instructions"}
        assert call_body["messages"][1] == {"role": "user", "content": "real msg"}

    @pytest.mark.asyncio
    async def test_reasoning_content_fallback_when_content_empty(self, backend):
        """LM Studio thinking models return content="" + reasoning_content=...;
        the backend should fall back to reasoning so workers don't lose output."""
        api_data = {
            "model": "qwen3.5-9b",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "The answer is 42.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 4, "completion_tokens": 30},
        }
        with patch.object(backend.client, "post", return_value=_mock_response(api_data)):
            result = await backend.complete("sys", "msg")

        # content was rescued from reasoning_content
        assert result["content"] == "The answer is 42."
        # raw reasoning is also surfaced for callers that want to log/strip it
        assert result["reasoning_content"] == "The answer is 42."

    @pytest.mark.asyncio
    async def test_reasoning_content_does_not_overwrite_real_content(self, backend):
        """When content has data, it wins regardless of reasoning_content."""
        api_data = {
            "model": "qwen3.5-9b",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Final answer.",
                        "reasoning_content": "Long internal monologue...",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 4, "completion_tokens": 30},
        }
        with patch.object(backend.client, "post", return_value=_mock_response(api_data)):
            result = await backend.complete("sys", "msg")

        assert result["content"] == "Final answer."
        # reasoning is still surfaced separately
        assert result["reasoning_content"] == "Long internal monologue..."

    @pytest.mark.asyncio
    async def test_reasoning_content_absent_returns_none(self, backend):
        """Plain (non-thinking) responses get reasoning_content=None, not ''."""
        api_data = {
            "model": "gpt-test",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hi"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        with patch.object(backend.client, "post", return_value=_mock_response(api_data)):
            result = await backend.complete("sys", "msg")
        assert result["reasoning_content"] is None

    @pytest.mark.asyncio
    async def test_both_empty_yields_empty_content(self, backend):
        """If both fields are empty, content stays empty (no synthesis)."""
        api_data = {
            "model": "gpt-test",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "", "reasoning_content": ""},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }
        with patch.object(backend.client, "post", return_value=_mock_response(api_data)):
            result = await backend.complete("sys", "msg")
        # Empty string in OpenAI shape — caller can detect None vs ""
        assert result["content"] in ("", None)
        assert result["reasoning_content"] is None


# ---------------------------------------------------------------------------
# OpenAICompatibleBackend base_url normalization
# ---------------------------------------------------------------------------


class TestOpenAICompatibleBackendBaseURL:
    def test_strips_trailing_v1(self):
        b = OpenAICompatibleBackend(base_url="http://localhost:1234/v1")
        assert b.base_url == "http://localhost:1234"

    def test_strips_trailing_slash(self):
        b = OpenAICompatibleBackend(base_url="http://localhost:1234/")
        assert b.base_url == "http://localhost:1234"

    def test_strips_v1_and_trailing_slash(self):
        b = OpenAICompatibleBackend(base_url="http://localhost:1234/v1/")
        assert b.base_url == "http://localhost:1234"

    def test_leaves_bare_host(self):
        b = OpenAICompatibleBackend(base_url="http://localhost:1234")
        assert b.base_url == "http://localhost:1234"


# ---------------------------------------------------------------------------
# LMStudioBackend
# ---------------------------------------------------------------------------


class TestLMStudioBackend:
    def test_default_base_url(self):
        backend = LMStudioBackend()
        assert backend.base_url == "http://localhost:1234"
        assert backend.model == "default"

    def test_custom_base_url_with_v1(self):
        backend = LMStudioBackend(base_url="http://example.com:1234/v1")
        assert backend.base_url == "http://example.com:1234"

    def test_gen_ai_system_is_lmstudio(self):
        backend = LMStudioBackend()
        assert backend.gen_ai_system == "lmstudio"

    @pytest.mark.asyncio
    async def test_response_reports_lmstudio_system(self):
        backend = LMStudioBackend(model="qwen")
        api_data = {
            "model": "qwen",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hi"},
                    "finish_reason": "stop",
                },
            ],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        }
        with patch.object(backend.client, "post", return_value=_mock_response(api_data)):
            result = await backend.complete("sys", "msg")

        assert result["content"] == "Hi"
        assert result["gen_ai_system"] == "lmstudio"
        # Other fields still follow OpenAI shape.
        assert result["prompt_tokens"] == 4
        assert result["completion_tokens"] == 2

    @pytest.mark.asyncio
    async def test_posts_to_v1_chat_completions(self):
        """Verify the URL doubling bug we normalized away does not return."""
        backend = LMStudioBackend(base_url="http://localhost:1234/v1")
        api_data = {
            "model": "qwen",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hi"},
                    "finish_reason": "stop",
                },
            ],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        }
        mock_post = AsyncMock(return_value=_mock_response(api_data))
        with patch.object(backend.client, "post", mock_post):
            await backend.complete("sys", "msg")

        # The path passed to the client should be /v1/chat/completions —
        # base_url was stripped of /v1, so the final URL is .../v1/...
        # not .../v1/v1/...
        called_path = mock_post.call_args[0][0]
        assert called_path == "/v1/chat/completions"


# ---------------------------------------------------------------------------
# build_backends_from_env / _select_local_backend
# ---------------------------------------------------------------------------


class TestSelectLocalBackend:
    def test_neither_set(self, monkeypatch):
        monkeypatch.delenv("LM_STUDIO_URL", raising=False)
        monkeypatch.delenv("OLLAMA_URL", raising=False)
        monkeypatch.delenv("HEDDLE_LOCAL_BACKEND", raising=False)
        assert _select_local_backend() is None

    def test_ollama_only(self, monkeypatch):
        monkeypatch.delenv("LM_STUDIO_URL", raising=False)
        monkeypatch.setenv("OLLAMA_URL", "http://localhost:11434")
        monkeypatch.delenv("HEDDLE_LOCAL_BACKEND", raising=False)
        backend = _select_local_backend()
        assert isinstance(backend, OllamaBackend)

    def test_lm_studio_only(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_URL", raising=False)
        monkeypatch.setenv("LM_STUDIO_URL", "http://localhost:1234/v1")
        monkeypatch.delenv("HEDDLE_LOCAL_BACKEND", raising=False)
        backend = _select_local_backend()
        assert isinstance(backend, LMStudioBackend)

    def test_both_set_lm_studio_wins_by_default(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_URL", "http://localhost:11434")
        monkeypatch.setenv("LM_STUDIO_URL", "http://localhost:1234/v1")
        monkeypatch.delenv("HEDDLE_LOCAL_BACKEND", raising=False)
        backend = _select_local_backend()
        assert isinstance(backend, LMStudioBackend)

    def test_explicit_ollama_preference(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_URL", "http://localhost:11434")
        monkeypatch.setenv("LM_STUDIO_URL", "http://localhost:1234/v1")
        monkeypatch.setenv("HEDDLE_LOCAL_BACKEND", "ollama")
        backend = _select_local_backend()
        assert isinstance(backend, OllamaBackend)

    def test_explicit_lmstudio_preference(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_URL", "http://localhost:11434")
        monkeypatch.setenv("LM_STUDIO_URL", "http://localhost:1234/v1")
        monkeypatch.setenv("HEDDLE_LOCAL_BACKEND", "lmstudio")
        backend = _select_local_backend()
        assert isinstance(backend, LMStudioBackend)

    def test_explicit_preference_unsatisfied_returns_none(self, monkeypatch):
        """If user demands LM Studio but it's not configured, fall through to None."""
        monkeypatch.setenv("OLLAMA_URL", "http://localhost:11434")
        monkeypatch.delenv("LM_STUDIO_URL", raising=False)
        monkeypatch.setenv("HEDDLE_LOCAL_BACKEND", "lmstudio")
        assert _select_local_backend() is None

    def test_lm_studio_model_env_override(self, monkeypatch):
        monkeypatch.setenv("LM_STUDIO_URL", "http://localhost:1234/v1")
        monkeypatch.setenv("LM_STUDIO_MODEL", "qwen/qwen2.5-7b")
        monkeypatch.delenv("HEDDLE_LOCAL_BACKEND", raising=False)
        backend = _select_local_backend()
        assert isinstance(backend, LMStudioBackend)
        assert backend.model == "qwen/qwen2.5-7b"


class TestBuildBackendsFromEnv:
    def test_includes_local_when_lm_studio_set(self, monkeypatch):
        monkeypatch.setenv("LM_STUDIO_URL", "http://localhost:1234/v1")
        monkeypatch.delenv("OLLAMA_URL", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("HEDDLE_LOCAL_BACKEND", raising=False)
        backends = build_backends_from_env()
        assert "local" in backends
        assert isinstance(backends["local"], LMStudioBackend)
        assert "standard" not in backends

    def test_includes_anthropic_tiers_when_key_set(self, monkeypatch):
        monkeypatch.delenv("LM_STUDIO_URL", raising=False)
        monkeypatch.delenv("OLLAMA_URL", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        backends = build_backends_from_env()
        assert "standard" in backends
        assert "frontier" in backends
        assert isinstance(backends["standard"], AnthropicBackend)


class TestOllamaBackendUrlResolution:
    """D5: OllamaBackend honours OLLAMA_URL like the embedding provider does."""

    def test_explicit_base_url_wins(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_URL", "http://env-host:11434")
        backend = OllamaBackend(base_url="http://explicit:11434")
        assert str(backend.client.base_url) == "http://explicit:11434"

    def test_env_var_used_when_no_explicit(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_URL", "http://env-host:11434")
        backend = OllamaBackend()
        assert str(backend.client.base_url) == "http://env-host:11434"

    def test_falls_back_to_container_default(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_URL", raising=False)
        backend = OllamaBackend()
        assert str(backend.client.base_url) == "http://ollama:11434"


class TestBuildBackendsFromEnvLogging:
    """D5: malformed config.yaml emits a WARN, doesn't silently no-op."""

    def test_config_load_failure_logged(self, monkeypatch, capsys):
        # Force load_config to raise so the except branch runs.
        def _raise(*_a, **_kw):
            raise RuntimeError("malformed yaml")

        monkeypatch.setattr("heddle.cli.config.load_config", _raise)
        monkeypatch.delenv("LM_STUDIO_URL", raising=False)
        monkeypatch.delenv("OLLAMA_URL", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        build_backends_from_env()
        # structlog renders to stdout in the project's default
        # configuration; the event name is the marker an operator
        # would grep for.
        out = capsys.readouterr().out
        assert "backends.config_load_failed" in out
        assert "malformed yaml" in out
