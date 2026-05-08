"""OpenAI chat bridge — session-aware OpenAI/ChatGPT adapter.

Supports any OpenAI-compatible API (OpenAI, Azure OpenAI, etc.).
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import structlog

from heddle.contrib.chatbridge.base import ChatBridge, ChatResponse, SessionInfo

logger = structlog.get_logger()


class OpenAIChatBridge(ChatBridge):
    """OpenAI Chat Completions API with per-session conversation history.

    Args:
        api_key: OpenAI API key.  Falls back to ``OPENAI_API_KEY`` env.
        model: Model identifier (default: gpt-4o).
        base_url: API base URL (default: OpenAI).
        system_prompt: System instructions applied to all sessions.
        max_tokens: Default max tokens per turn.
    """

    bridge_type = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gpt-4o",
        base_url: str = "https://api.openai.com",
        system_prompt: str = "",
        max_tokens: int = 2000,
    ) -> None:
        super().__init__(system_prompt=system_prompt)
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._model = model
        self._max_tokens = max_tokens
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            timeout=120.0,
        )

    async def send_turn(
        self,
        message: str,
        context: dict[str, Any],
        session_id: str,
    ) -> ChatResponse:
        """Send a turn via OpenAI Chat Completions, accumulating history."""
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
            "max_tokens": self._max_tokens,
        }

        resp = await self._client.post("/v1/chat/completions", json=body)
        resp.raise_for_status()
        data = resp.json()

        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        content = message.get("content") or ""
        # Thinking-model quirk (mirrors OpenAICompatibleBackend in
        # heddle.worker.backends): some OpenAI-compatible providers
        # (LM Studio for qwen3.*/deepseek-r1, vLLM with a reasoning
        # parser, DeepSeek's first-party API) split the model's
        # chain-of-thought onto ``message.reasoning_content`` while
        # leaving ``message.content`` empty.  We rescue it so
        # callers don't get a silent empty string, and surface the
        # raw value on ``ChatResponse.reasoning_content`` so
        # operators can log or strip it.  See
        # docs/TROUBLESHOOTING.md "Thinking model returns empty
        # content" for provider knobs that disable the trace at
        # request time.
        #
        # TODO(thinking-config): expose a ``disable_thinking=True``
        # constructor flag (paired with the matching backend
        # parameter) that maps to provider-specific request params
        # — qwen ``extra_body={"enable_thinking": False}`` via
        # LM Studio / vLLM, OpenAI ``reasoning_effort="low"``, etc.
        # See the equivalent TODO in OpenAICompatibleBackend.
        reasoning_content = message.get("reasoning_content") or None
        if not content and reasoning_content:
            content = reasoning_content
            logger.info(
                "chatbridge.reasoning_content.rescue",
                bridge_type=self.bridge_type,
                model=self._model,
                response_model=data.get("model"),
                completion_tokens=data.get("usage", {}).get("completion_tokens", 0),
                max_tokens=self._max_tokens,
                reasoning_chars=len(reasoning_content),
            )

        # Append assistant response to session history.
        session.messages.append({"role": "assistant", "content": content})

        usage = data.get("usage", {})
        return ChatResponse(
            content=content,
            model=data.get("model", self._model),
            token_usage={
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
            },
            stop_reason=choice.get("finish_reason"),
            session_id=session_id,
            reasoning_content=reasoning_content,
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
