"""Tests for OpenAIChatBridge."""

from unittest.mock import AsyncMock, patch

import httpx

from heddle.contrib.chatbridge.openai import OpenAIChatBridge


def _mock_response(content="Sure!", prompt_tokens=40, completion_tokens=15):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "model": "gpt-4o",
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
        },
        request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
    )


class TestOpenAIChatBridge:
    async def test_send_turn_basic(self):
        bridge = OpenAIChatBridge(api_key="test-key")
        with patch.object(bridge._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = _mock_response("I think so.")
            resp = await bridge.send_turn("What's your view?", {}, "sess_1")

        assert resp.content == "I think so."
        assert resp.model == "gpt-4o"
        assert resp.token_usage["prompt_tokens"] == 40

    async def test_system_prompt_included(self):
        bridge = OpenAIChatBridge(api_key="test-key", system_prompt="You are a critic.")
        with patch.object(bridge._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = _mock_response()
            await bridge.send_turn("Hello", {}, "sess_1")

        body = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        # First message should be the system prompt.
        assert body["messages"][0]["role"] == "system"
        assert "critic" in body["messages"][0]["content"]

    async def test_messages_accumulate(self):
        bridge = OpenAIChatBridge(api_key="test-key")
        with patch.object(bridge._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = _mock_response("R1")
            await bridge.send_turn("M1", {}, "sess_1")
            mock_post.return_value = _mock_response("R2")
            await bridge.send_turn("M2", {}, "sess_1")

        info = await bridge.get_session_info("sess_1")
        assert info.message_count == 4  # 2 user + 2 assistant

    async def test_session_info_empty(self):
        bridge = OpenAIChatBridge(api_key="test-key")
        info = await bridge.get_session_info("nonexistent")
        assert info.message_count == 0


def _thinking_response(
    content: str = "",
    reasoning: str = "Thinking step by step...",
    prompt_tokens: int = 10,
    completion_tokens: int = 50,
) -> httpx.Response:
    """LM Studio-style response where a thinking model dumps everything into reasoning_content."""
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "reasoning_content": reasoning,
                    },
                    "finish_reason": "stop",
                }
            ],
            "model": "qwen3.5-9b",
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
        },
        request=httpx.Request("POST", "http://localhost:1234/v1/chat/completions"),
    )


class TestReasoningContentFallback:
    async def test_falls_back_when_content_empty(self):
        bridge = OpenAIChatBridge(api_key="test-key")
        with patch.object(bridge._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = _thinking_response(
                content="",
                reasoning="The answer is 42.",
            )
            resp = await bridge.send_turn("Q?", {}, "sess_1")
        assert resp.content == "The answer is 42."
        assert resp.reasoning_content == "The answer is 42."

    async def test_keeps_real_content_and_surfaces_reasoning(self):
        bridge = OpenAIChatBridge(api_key="test-key")
        with patch.object(bridge._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = _thinking_response(
                content="Final answer.",
                reasoning="Long internal monologue...",
            )
            resp = await bridge.send_turn("Q?", {}, "sess_1")
        assert resp.content == "Final answer."
        assert resp.reasoning_content == "Long internal monologue..."

    async def test_reasoning_absent_yields_none(self):
        bridge = OpenAIChatBridge(api_key="test-key")
        with patch.object(bridge._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = _mock_response("Hi")
            resp = await bridge.send_turn("Q?", {}, "sess_1")
        assert resp.content == "Hi"
        assert resp.reasoning_content is None

    async def test_session_history_uses_rescued_content(self):
        """When the bridge rescues content from reasoning, future turns must
        still see something for the previous assistant turn — otherwise
        multi-turn thinking-model conversations lose context."""
        bridge = OpenAIChatBridge(api_key="test-key")
        with patch.object(bridge._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = _thinking_response(content="", reasoning="Recovered.")
            await bridge.send_turn("Q?", {}, "sess_1")

        session = bridge._sessions["sess_1"]
        assistant_msgs = [m for m in session.messages if m["role"] == "assistant"]
        assert assistant_msgs[-1]["content"] == "Recovered."


class TestOpenAIChatBridgeReliability:
    """D2/D3/D4 regression tests."""

    def test_requires_api_key_at_construction(self, monkeypatch):
        """D4: empty api_key raises at __init__."""
        import pytest

        from heddle.contrib.chatbridge.exceptions import ChatBridgeMisconfiguredError

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(ChatBridgeMisconfiguredError, match="OPENAI_API_KEY"):
            OpenAIChatBridge()

    async def test_user_message_rolled_back_on_api_error(self):
        """D2: failed POST leaves session.messages untouched."""
        import pytest

        bridge = OpenAIChatBridge(api_key="test-key")
        with patch.object(bridge._client, "post", new_callable=AsyncMock) as ok_post:
            ok_post.return_value = _mock_response("seed")
            await bridge.send_turn("seed-user", {}, "sess_x")
        assert len(bridge._sessions["sess_x"].messages) == 2

        with patch.object(bridge._client, "post", new_callable=AsyncMock) as bad_post:
            bad_post.side_effect = RuntimeError("openai down")
            with pytest.raises(RuntimeError, match="openai down"):
                await bridge.send_turn("dropped-user", {}, "sess_x")
        assert len(bridge._sessions["sess_x"].messages) == 2
        assert all(m.get("content") != "dropped-user" for m in bridge._sessions["sess_x"].messages)

    async def test_raises_unsupported_tool_use_on_tool_calls(self):
        """D3: a response carrying ``tool_calls`` raises typed error.

        Earlier the bridge returned an empty assistant turn when the
        provider emitted tool_calls with empty content, so the agent
        appeared to say nothing.  Now the bridge raises so the
        consumer (council loop, MCP) can attribute the failure.
        Detection happens before session mutation so the bad turn
        doesn't poison history.
        """
        import pytest

        from heddle.contrib.chatbridge.exceptions import UnsupportedToolUseError

        bridge = OpenAIChatBridge(api_key="test-key")
        tool_call_payload = httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": "{}"},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "model": "gpt-4o",
                "usage": {"prompt_tokens": 5, "completion_tokens": 0},
            },
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        )
        with patch.object(bridge._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = tool_call_payload
            with pytest.raises(UnsupportedToolUseError) as excinfo:
                await bridge.send_turn("call a tool", {}, "sess_tc")
        assert excinfo.value.bridge == "openai"
        assert excinfo.value.tool_calls
        # Session history unchanged — bad turn did not poison the
        # session.
        assert (
            bridge._sessions.get("sess_tc") is None
            or len(bridge._sessions["sess_tc"].messages) == 0
        )
