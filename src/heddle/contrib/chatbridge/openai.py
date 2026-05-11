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
        # Reject empty keys at construction so a misconfiguration
        # surfaces here rather than as a generic 401 on the first
        # send_turn call.  Earlier the bridge silently sent
        # ``Authorization: Bearer `` (empty) to OpenAI.
        if not self._api_key:
            from heddle.contrib.chatbridge.exceptions import ChatBridgeMisconfiguredError

            raise ChatBridgeMisconfiguredError(
                "OpenAIChatBridge: api_key is empty. Pass api_key=... "
                "or set the OPENAI_API_KEY env var."
            )
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
        """Send a turn via OpenAI Chat Completions, accumulating history.

        Session history is only updated after the API call returns
        successfully.  Earlier the user message was appended eagerly,
        so an HTTP failure left it in the session and the next turn
        sent two consecutive ``user`` messages — OpenAI accepts that
        shape but produces confused output.
        """
        session = self._get_or_create_session(session_id)
        # Build messages array with system prompt prepended; do not
        # mutate ``session.messages`` yet — only on success.
        user_msg = {"role": "user", "content": message}
        api_messages: list[dict[str, str]] = []
        sys_prompt = session.system_prompt or self._system_prompt
        if sys_prompt:
            api_messages.append({"role": "system", "content": sys_prompt})
        api_messages.extend(session.messages)
        api_messages.append(user_msg)

        body: dict[str, Any] = {
            "model": self._model,
            "messages": api_messages,
            "max_tokens": self._max_tokens,
        }

        resp = await self._client.post("/v1/chat/completions", json=body)
        resp.raise_for_status()
        data = resp.json()

        choice = data.get("choices", [{}])[0]
        # ``message`` is the local turn input; rebinding it from the
        # API response would shadow our own argument.  Use a distinct
        # name for the API-returned message dict so the local
        # ``message`` (and ``user_msg`` built from it) stay readable.
        api_message = choice.get("message", {})
        # D3: detect tool_calls and raise rather than returning an
        # empty assistant turn.  Some OpenAI-compatible providers
        # emit tool_calls with empty content; the bridge does not
        # currently implement tool execution, so silently returning
        # "" would make the agent appear to say nothing.  Raise a
        # typed error so the council loop (or other consumer) can
        # attribute the failure to the right cause.  Detect BEFORE
        # mutating ``session.messages`` so the bad turn doesn't
        # poison history on retry.
        if api_message.get("tool_calls"):
            from heddle.contrib.chatbridge.exceptions import UnsupportedToolUseError

            raise UnsupportedToolUseError(
                bridge="openai",
                model=self._model,
                tool_calls=api_message["tool_calls"],
            )
        content = api_message.get("content") or ""
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
        reasoning_content = api_message.get("reasoning_content") or None
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

        # Both messages persist together — only after the API call
        # succeeded and the tool_calls check passed — so a mid-call
        # failure leaves session.messages untouched.
        session.messages.append(user_msg)
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
