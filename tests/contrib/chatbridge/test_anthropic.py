"""Tests for AnthropicChatBridge."""

from unittest.mock import AsyncMock, patch

import httpx

from heddle.contrib.chatbridge.anthropic import AnthropicChatBridge


def _mock_response(content="Hello!", input_tokens=50, output_tokens=20):
    """Create a mock httpx response matching Anthropic Messages API."""
    return httpx.Response(
        200,
        json={
            "content": [{"type": "text", "text": content}],
            "model": "claude-sonnet-4-20250514",
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
            "stop_reason": "end_turn",
        },
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )


class TestAnthropicChatBridge:
    async def test_send_turn_basic(self):
        bridge = AnthropicChatBridge(api_key="test-key")
        with patch.object(bridge._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = _mock_response("I agree.")
            resp = await bridge.send_turn("What do you think?", {}, "sess_1")

        assert resp.content == "I agree."
        assert resp.model == "claude-sonnet-4-20250514"
        assert resp.token_usage["prompt_tokens"] == 50
        assert resp.session_id == "sess_1"

    async def test_messages_accumulate(self):
        bridge = AnthropicChatBridge(api_key="test-key")
        with patch.object(bridge._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = _mock_response("First response")
            await bridge.send_turn("First message", {}, "sess_1")

            mock_post.return_value = _mock_response("Second response")
            await bridge.send_turn("Second message", {}, "sess_1")

        # Verify the second call included the prior round's history
        # plus the new user message about to be answered.  D2 made
        # session history update only after the call returns, so the
        # second POST body contains 3 messages: prior user + prior
        # assistant + new user.  The new assistant lands on the
        # session after the response parses.
        last_call = mock_post.call_args
        body = last_call.kwargs.get("json") or last_call[1].get("json")
        messages = body["messages"]
        assert len(messages) == 3
        assert [m["role"] for m in messages] == ["user", "assistant", "user"]

    async def test_session_info(self):
        bridge = AnthropicChatBridge(api_key="test-key")
        with patch.object(bridge._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = _mock_response()
            await bridge.send_turn("Hello", {}, "sess_1")

        info = await bridge.get_session_info("sess_1")
        assert info.session_id == "sess_1"
        assert info.bridge_type == "anthropic"
        assert info.message_count == 2  # user + assistant

    async def test_close_session(self):
        bridge = AnthropicChatBridge(api_key="test-key")
        with patch.object(bridge._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = _mock_response()
            await bridge.send_turn("Hello", {}, "sess_1")

        await bridge.close_session("sess_1")
        info = await bridge.get_session_info("sess_1")
        assert info.message_count == 0

    async def test_separate_sessions(self):
        bridge = AnthropicChatBridge(api_key="test-key")
        with patch.object(bridge._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = _mock_response()
            await bridge.send_turn("Hello A", {}, "sess_a")
            await bridge.send_turn("Hello B", {}, "sess_b")

        info_a = await bridge.get_session_info("sess_a")
        info_b = await bridge.get_session_info("sess_b")
        assert info_a.message_count == 2  # user + assistant
        assert info_b.message_count == 2


class TestAnthropicChatBridgeReliability:
    """D1/D2/D4 regression tests."""

    def test_requires_api_key_at_construction(self, monkeypatch):
        """D4: empty api_key raises at __init__, not at first send_turn."""
        import pytest

        from heddle.contrib.chatbridge.exceptions import ChatBridgeMisconfiguredError

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(ChatBridgeMisconfiguredError, match="ANTHROPIC_API_KEY"):
            AnthropicChatBridge()

    def test_pins_stable_api_version(self):
        """D1: anthropic-version header is the stable 2023-06-01."""
        bridge = AnthropicChatBridge(api_key="test-key")
        assert bridge._client.headers["anthropic-version"] == "2023-06-01"

    async def test_user_message_rolled_back_on_api_error(self):
        """D2: a failed POST leaves session.messages untouched."""
        import pytest

        bridge = AnthropicChatBridge(api_key="test-key")
        # Seed a successful first turn so subsequent failures are visible.
        with patch.object(bridge._client, "post", new_callable=AsyncMock) as ok_post:
            ok_post.return_value = _mock_response("seed")
            await bridge.send_turn("seed-user", {}, "sess_x")
        assert len(bridge._sessions["sess_x"].messages) == 2

        # Now fail.  The failed user message must not stay in history.
        with patch.object(bridge._client, "post", new_callable=AsyncMock) as bad_post:
            bad_post.side_effect = RuntimeError("network down")
            with pytest.raises(RuntimeError, match="network down"):
                await bridge.send_turn("dropped-user", {}, "sess_x")
        assert len(bridge._sessions["sess_x"].messages) == 2
        # The dropped message did not poison history.
        assert all(m.get("content") != "dropped-user" for m in bridge._sessions["sess_x"].messages)

    async def test_surfaces_extended_thinking_blocks(self):
        """D1: ``thinking`` blocks land on ChatResponse.reasoning_content."""
        bridge = AnthropicChatBridge(api_key="test-key")
        resp_payload = {
            "content": [
                {"type": "thinking", "thinking": "internal monologue"},
                {"type": "text", "text": "visible answer"},
            ],
            "model": "claude-sonnet-4-20250514",
            "usage": {"input_tokens": 5, "output_tokens": 3},
            "stop_reason": "end_turn",
        }
        with patch.object(bridge._client, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = httpx.Response(
                200,
                json=resp_payload,
                request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
            )
            resp = await bridge.send_turn("Q?", {}, "sess_thinking")
        assert resp.content == "visible answer"
        assert resp.reasoning_content == "internal monologue"
