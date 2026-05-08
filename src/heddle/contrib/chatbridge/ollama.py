"""Ollama chat bridge — session-aware local model adapter.

Wraps the Ollama ``/api/chat`` endpoint with per-session conversation
history for multi-turn local model interactions.
"""

from __future__ import annotations

from typing import Any

import httpx

from heddle.contrib.chatbridge.base import ChatBridge, ChatResponse, SessionInfo


class OllamaChatBridge(ChatBridge):
    """Ollama chat API with per-session conversation history.

    Args:
        model: Ollama model name (default: llama3.2:3b).
        base_url: Ollama server URL (default: http://localhost:11434).
        system_prompt: System instructions applied to all sessions.
        max_tokens: Default max tokens per turn (num_predict).
    """

    bridge_type = "ollama"

    def __init__(
        self,
        model: str = "llama3.2:3b",
        base_url: str = "http://localhost:11434",
        system_prompt: str = "",
        max_tokens: int = 2000,
    ) -> None:
        super().__init__(system_prompt=system_prompt)
        self._model = model
        self._max_tokens = max_tokens
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=120.0,
        )

    async def send_turn(
        self,
        message: str,
        context: dict[str, Any],
        session_id: str,
    ) -> ChatResponse:
        """Send a turn via Ollama /api/chat, accumulating history."""
        session = self._get_or_create_session(session_id)
        session.messages.append({"role": "user", "content": message})

        # Build messages array with system prompt prepended.
        api_messages: list[dict[str, str]] = []
        sys_prompt = session.system_prompt or self._system_prompt
        if sys_prompt:
            api_messages.append({"role": "system", "content": sys_prompt})
        api_messages.extend(session.messages)

        body: dict[str, Any] = {
            "model": self._model,
            "messages": api_messages,
            "stream": False,
            "options": {"num_predict": self._max_tokens},
        }

        resp = await self._client.post("/api/chat", json=body)
        resp.raise_for_status()
        data = resp.json()

        content = data.get("message", {}).get("content", "")

        # Append assistant response to session history.
        session.messages.append({"role": "assistant", "content": content})

        # Ollama provides token counts differently.
        prompt_tokens = data.get("prompt_eval_count", 0)
        completion_tokens = data.get("eval_count", 0)

        return ChatResponse(
            content=content,
            model=data.get("model", self._model),
            token_usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
            stop_reason="stop" if data.get("done") else None,
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
