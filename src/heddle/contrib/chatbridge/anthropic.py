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

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-sonnet-4-20250514",
        system_prompt: str = "",
        max_tokens: int = 2000,
    ) -> None:
        super().__init__(system_prompt=system_prompt)
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._model = model
        self._max_tokens = max_tokens
        self._client = httpx.AsyncClient(
            base_url="https://api.anthropic.com",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": "2024-10-22",
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
        """Send a turn to Claude, accumulating session messages."""
        session = self._get_or_create_session(session_id)
        session.messages.append({"role": "user", "content": message})

        body: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": session.system_prompt or self._system_prompt,
            "messages": session.messages,
        }

        resp = await self._client.post("/v1/messages", json=body)
        resp.raise_for_status()
        data = resp.json()

        # Extract text content from response blocks.
        content = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                content += block.get("text", "")

        # Append assistant response to session history.
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
