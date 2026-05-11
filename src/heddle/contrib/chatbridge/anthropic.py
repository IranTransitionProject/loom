"""Anthropic (Claude) chat bridge — session-aware Claude API adapter.

Unlike :class:`heddle.worker.backends.AnthropicBackend` which is stateless
per-call, this bridge accumulates messages per session, enabling
multi-turn conversations with Claude.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from heddle.contrib.chatbridge.base import ChatBridge, ChatResponse, SessionInfo


class AnthropicChatBridge(ChatBridge):
    """Claude API with per-session conversation history.

    Args:
        api_key: Anthropic API key.  Falls back to ``ANTHROPIC_API_KEY`` env.
        model: Model identifier (default: claude-sonnet-4-20250514).
        system_prompt: System instructions applied to all sessions.
        max_tokens: Default max tokens per turn.
    """

    bridge_type = "anthropic"

    # Pinned to the documented stable version per
    # https://docs.anthropic.com/en/api/versioning .  Earlier the
    # bridge used the ``2024-10-22`` beta header, which Anthropic
    # can retire without warning.
    ANTHROPIC_API_VERSION: str = "2023-06-01"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-sonnet-4-20250514",
        system_prompt: str = "",
        max_tokens: int = 2000,
    ) -> None:
        super().__init__(system_prompt=system_prompt)
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        # Reject empty keys at construction so a misconfiguration
        # surfaces here rather than as a generic 401 on the first
        # send_turn call.  Earlier the bridge silently sent
        # ``x-api-key: `` (empty) to Anthropic.
        if not self._api_key:
            from heddle.contrib.chatbridge.exceptions import ChatBridgeMisconfiguredError

            raise ChatBridgeMisconfiguredError(
                "AnthropicChatBridge: api_key is empty. Pass api_key=... "
                "or set the ANTHROPIC_API_KEY env var."
            )
        self._model = model
        self._max_tokens = max_tokens
        self._client = httpx.AsyncClient(
            base_url="https://api.anthropic.com",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": self.ANTHROPIC_API_VERSION,
                "content-type": "application/json",
            },
            timeout=120.0,
        )

    async def send_turn(
        self,
        message: str,
        context: dict[str, Any],
        session_id: str,
    ) -> ChatResponse:
        """Send a turn to Claude, accumulating session messages.

        Session history is only updated after the API call returns.
        Earlier the user message was appended eagerly, so an HTTP
        failure left it in ``session.messages``; the next turn then
        sent two consecutive ``user`` messages, which Claude rejects.
        """
        session = self._get_or_create_session(session_id)
        # Build the request with the new user message included but
        # do not mutate ``session.messages`` until the call returns.
        user_msg = {"role": "user", "content": message}
        api_messages = [*session.messages, user_msg]

        body: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": session.system_prompt or self._system_prompt,
            "messages": api_messages,
        }

        resp = await self._client.post("/v1/messages", json=body)
        resp.raise_for_status()
        data = resp.json()

        # Extract text content and ``thinking`` content from response
        # blocks separately.  Extended-thinking blocks land on
        # ``reasoning_content`` so consumers that surface
        # OpenAI-compat reasoning (``message.reasoning_content``) and
        # Anthropic extended thinking can read both via the same
        # ChatResponse field.
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        for block in data.get("content", []):
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "thinking":
                thinking_parts.append(block.get("thinking", ""))
        content = "".join(text_parts)
        reasoning = "".join(thinking_parts) if thinking_parts else None

        # Both messages persist together — only after the API call
        # succeeded — so a mid-call failure leaves session.messages
        # untouched (no dangling user message on retry).
        session.messages.append(user_msg)
        session.messages.append({"role": "assistant", "content": content})

        usage = data.get("usage", {})
        return ChatResponse(
            content=content,
            model=data.get("model", self._model),
            token_usage={
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
            },
            stop_reason=data.get("stop_reason"),
            session_id=session_id,
            reasoning_content=reasoning,
        )

    async def get_session_info(self, session_id: str) -> SessionInfo:
        """Return session metadata."""
        session = self._sessions.get(session_id)
        info = SessionInfo(
            session_id=session_id,
            bridge_type=self.bridge_type,
            model=self._model,
            message_count=len(session.messages) if session else 0,
        )
        if session:
            info.created_at = session.created_at
        return info

    async def aclose(self) -> None:
        """Close the underlying ``httpx.AsyncClient`` and clear sessions."""
        await self._client.aclose()
        await super().aclose()
