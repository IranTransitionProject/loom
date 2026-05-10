"""
LLM backend adapters — uniform interface for local and API models.

Each backend wraps a specific LLM provider's API and normalizes the response
into a consistent dict format. Workers never call APIs directly; they always
go through a backend.

To add a new backend:
    1. Subclass LLMBackend
    2. Implement complete() returning the standard response dict
    3. Register it in cli/main.py's worker command (backend resolution by tier)

All backends use httpx with a 120s timeout. Adjust if your models are slow.

Tool-use support:
    Backends accept optional ``tools`` and ``messages`` parameters. When
    ``tools`` is provided, the LLM may return tool_calls instead of content.
    When ``messages`` is provided, it replaces the single user_message for
    multi-turn conversations (tool execution loop).
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()


class LLMBackend(ABC):
    """Common interface all model backends implement."""

    @abstractmethod
    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        max_tokens: int = 2000,
        temperature: float = 0.0,
        *,
        tools: list[dict[str, Any]] | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Complete an LLM request and return a normalized response dict.

        Args:
            system_prompt: System instructions for the LLM.
            user_message: User message (ignored when ``messages`` is provided).
            max_tokens: Maximum tokens in the response.
            temperature: Sampling temperature.
            tools: Optional list of tool definitions for function-calling.
            messages: Optional full message history for multi-turn. When
                provided, overrides ``user_message``.

        Returns:
            A dict with the following structure::

                {
                    "content": str | None,      # Text response (None if tool_calls)
                    "model": str,               # Model identifier
                    "prompt_tokens": int,
                    "completion_tokens": int,
                    "tool_calls": list | None,  # [{"id": str, "name": str, "arguments": dict}]
                    "stop_reason": str | None,  # "end_turn" | "tool_use"
                }
        """
        ...

    async def aclose(self) -> None:  # noqa: B027 — intentional no-op default
        """Release any I/O resources held by this backend.

        Subclasses that hold open connections (e.g. an ``httpx.AsyncClient``)
        override this to close them. Idempotent — safe to call more than
        once. The default is a no-op so test mocks and lightweight
        subclasses do not need to implement it.
        """


class AnthropicBackend(LLMBackend):
    """Claude API via httpx (Messages API).

    Uses the Anthropic Messages API directly via httpx rather than the
    anthropic Python SDK — this keeps dependencies minimal and avoids
    version coupling.
    """

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514") -> None:
        self.api_key = api_key
        self.model = model
        self.client = httpx.AsyncClient(
            base_url="https://api.anthropic.com",
            headers={
                "x-api-key": api_key,
                # Anthropic API version — pinned for reproducibility.
                # See: https://docs.anthropic.com/en/api/versioning
                "anthropic-version": "2024-10-22",
                "content-type": "application/json",
            },
            timeout=120.0,
        )

    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        max_tokens: int = 2000,
        temperature: float = 0.0,
        *,
        tools: list[dict[str, Any]] | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Complete an LLM request via the Anthropic Messages API."""
        # Build messages array
        if messages is not None:
            api_messages = _anthropic_messages(messages)
        else:
            api_messages = [{"role": "user", "content": user_message}]

        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system_prompt,
            "messages": api_messages,
        }

        # Add tool definitions if provided
        if tools:
            body["tools"] = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("parameters", {"type": "object"}),
                }
                for t in tools
            ]

        resp = await self.client.post("/v1/messages", json=body)
        resp.raise_for_status()
        data = resp.json()

        # Parse response — may contain text blocks, tool_use blocks,
        # and (when extended thinking is enabled by the caller)
        # ``thinking`` blocks.  We currently only surface text and
        # tool_use; thinking blocks are silently dropped.  This is
        # safe because Anthropic's extended thinking is opt-in (the
        # request body must include ``thinking={"type": "enabled",
        # "budget_tokens": N}``), and Heddle does not enable it
        # today — see TODO below.
        #
        # TODO(anthropic-thinking): when callers can opt in to
        # Anthropic extended thinking via a backend parameter (e.g.
        # ``thinking_budget_tokens=4096``), surface the
        # ``thinking`` blocks separately on the response dict
        # (mirror the OpenAI-compat ``reasoning_content`` field).
        # See https://docs.anthropic.com/en/docs/build-with-claude/extended-thinking
        # Anthropic responses can contain multiple text blocks
        # interleaved with tool_use blocks (e.g. text → tool_use →
        # text, common when tool_use is enabled).  Earlier shape
        # assigned ``content = block["text"]``, overwriting on each
        # iteration, so any text after the first tool_use block was
        # silently lost.  Accumulate into a list and join at the end.
        text_parts: list[str] = []
        tool_calls = None

        for block in data.get("content", []):
            if block["type"] == "text":
                text_parts.append(block["text"])
            elif block["type"] == "tool_use":
                if tool_calls is None:
                    tool_calls = []
                tool_calls.append(
                    {
                        "id": block["id"],
                        "name": block["name"],
                        "arguments": block["input"],
                    }
                )

        # ``None`` when no text blocks were present preserves the
        # original contract (callers distinguish "no text" from "empty
        # text" — the OpenAI-compat backend returns None for tool-only
        # responses).
        content = "".join(text_parts) if text_parts else None

        return {
            "content": content,
            "model": data["model"],
            "prompt_tokens": data["usage"]["input_tokens"],
            "completion_tokens": data["usage"]["output_tokens"],
            "tool_calls": tool_calls,
            "stop_reason": data.get("stop_reason"),
            # OTel GenAI semantic convention metadata
            "gen_ai_system": "anthropic",
            "gen_ai_request_model": self.model,
            "gen_ai_response_model": data["model"],
            "gen_ai_request_temperature": temperature,
            "gen_ai_request_max_tokens": max_tokens,
        }

    async def aclose(self) -> None:
        """Close the underlying ``httpx.AsyncClient``."""
        await self.client.aclose()


class OllamaBackend(LLMBackend):
    """Local models via Ollama HTTP API.

    Default base_url points to K8s service name "ollama". For local dev,
    override with http://localhost:11434 (set OLLAMA_URL env var).

    Note: Ollama's token counts (prompt_eval_count, eval_count) may be
    absent for some models; we default to 0 in that case.

    Thinking-model quirk:
        Ollama serves several thinking-style models (qwen3*,
        deepseek-r1*, …) that emit their reasoning trace inline as
        ``<think>...</think>`` tags inside ``message.content``.  We
        currently pass the content through unmodified — the tags
        end up in the agent's response and downstream consumers
        (workers, judges) see them.  For most council use cases
        this is fine; for strict-JSON workers it can corrupt
        output.

    TODO(ollama-think-tags): expose a ``strip_think_tags=True``
    constructor flag (or surface ``reasoning_content`` similarly to
    :class:`OpenAICompatibleBackend`) that splits ``<think>...</think>``
    out of ``content`` and returns it on a separate response key.
    Most newer Ollama builds also support
    ``options.think: false`` (or model-specific
    ``chat_template_kwargs={"enable_thinking": false}``) to disable
    the trace at request time — wire that through the same flag.
    """

    def __init__(self, model: str = "llama3.2:3b", base_url: str = "http://ollama:11434") -> None:
        self.model = model
        self.client = httpx.AsyncClient(base_url=base_url, timeout=120.0)

    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        max_tokens: int = 2000,
        temperature: float = 0.0,
        *,
        tools: list[dict[str, Any]] | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Complete an LLM request via the Ollama HTTP API."""
        # Build messages array
        if messages is not None:
            api_messages = [
                {"role": "system", "content": system_prompt},
                *_ollama_messages(messages),
            ]
        else:
            api_messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ]

        body: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }

        # Add tool definitions if provided (OpenAI-compatible format)
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {"type": "object"}),
                    },
                }
                for t in tools
            ]

        resp = await self.client.post("/api/chat", json=body)
        resp.raise_for_status()
        data = resp.json()

        # Parse tool calls from Ollama response
        message = data.get("message", {})
        content = message.get("content") or None
        tool_calls = None

        raw_calls = message.get("tool_calls")
        if raw_calls:
            tool_calls = []
            for i, call in enumerate(raw_calls):
                func = call.get("function", {})
                tool_calls.append(
                    {
                        "id": f"call_{i}",
                        "name": func.get("name", ""),
                        "arguments": func.get("arguments", {}),
                    }
                )

        stop_reason = "tool_use" if tool_calls else "end_turn"

        return {
            "content": content,
            "model": self.model,
            "prompt_tokens": data.get("prompt_eval_count", 0),
            "completion_tokens": data.get("eval_count", 0),
            "tool_calls": tool_calls,
            "stop_reason": stop_reason,
            # OTel GenAI semantic convention metadata
            "gen_ai_system": "ollama",
            "gen_ai_request_model": self.model,
            "gen_ai_response_model": self.model,
            "gen_ai_request_temperature": temperature,
            "gen_ai_request_max_tokens": max_tokens,
        }

    async def aclose(self) -> None:
        """Close the underlying ``httpx.AsyncClient``."""
        await self.client.aclose()


class OpenAICompatibleBackend(LLMBackend):
    """Any OpenAI-compatible API (vLLM, llama.cpp server, LiteLLM, LM Studio, etc.).

    The ``base_url`` may be the host (e.g. ``http://localhost:8000``) or
    include a trailing ``/v1`` (e.g. ``http://localhost:1234/v1``) — the
    latter is normalized so that requests still hit ``/v1/chat/completions``.
    """

    # GenAI semantic-conventions ``gen_ai.system`` value reported in
    # response dicts.  Subclasses (e.g. :class:`LMStudioBackend`) override.
    gen_ai_system: str = "openai"

    def __init__(self, base_url: str, api_key: str = "not-needed", model: str = "default") -> None:
        self.model = model
        # Strip a trailing ``/v1`` (and any stray trailing slash) so the
        # ``/v1/chat/completions`` path we POST to does not end up doubled.
        normalized = base_url.rstrip("/")
        if normalized.endswith("/v1"):
            normalized = normalized[: -len("/v1")]
        self.base_url = normalized
        self.client = httpx.AsyncClient(
            base_url=normalized,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=120.0,
        )

    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        max_tokens: int = 2000,
        temperature: float = 0.0,
        *,
        tools: list[dict[str, Any]] | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Complete an LLM request via an OpenAI-compatible API."""
        # Build messages array
        if messages is not None:
            api_messages = [
                {"role": "system", "content": system_prompt},
                *_openai_messages(messages),
            ]
        else:
            api_messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ]

        body: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        # Add tool definitions if provided
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {"type": "object"}),
                    },
                }
                for t in tools
            ]

        resp = await self.client.post("/v1/chat/completions", json=body)
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage", {})

        # Parse response
        choice = data["choices"][0]
        message = choice.get("message", {})
        content = message.get("content")
        # Thinking-model quirk: several OpenAI-compatible providers
        # (LM Studio for qwen3.*/deepseek-r1, vLLM with a reasoning
        # parser, DeepSeek's first-party API) split the model's
        # chain-of-thought onto ``message.reasoning_content`` while
        # leaving ``message.content`` empty.  Naïve OpenAI parsers
        # treat the empty string as the model's reply and drop the
        # actual output on the floor.  We rescue it so workers
        # don't silently lose output, AND surface the raw value on
        # the response dict so callers can log or strip it.
        #
        # The rescued content is the ENTIRE thinking trace, which
        # is verbose internal monologue — not what an operator
        # usually wants displayed to end-users.  See
        # docs/TROUBLESHOOTING.md "Thinking model returns empty
        # content" for the available knobs (qwen ``/no_think`` /
        # ``enable_thinking: false``, deepseek-r1 prompt-side
        # disable, OpenAI ``reasoning_effort``, etc.).
        #
        # TODO(thinking-config): expose a ``disable_thinking=True``
        # constructor flag that maps to provider-appropriate
        # request params (``extra_body={"enable_thinking": False}``
        # for qwen via vLLM/LM Studio, ``reasoning_effort="low"``
        # for OpenAI o-series) so callers can opt out of the
        # reasoning trace at request time instead of paying for
        # tokens we then have to rescue.  The OpenAI Chat
        # Completions schema does not standardize this — it has to
        # be provider-specific.
        reasoning_content = message.get("reasoning_content") or None
        if not content and reasoning_content:
            content = reasoning_content
            # Operator-relevant signal: the model produced no
            # visible answer and we are surfacing its monologue as
            # a substitute.  Useful for spotting silently-broken
            # configs (max_tokens too low, system prompt
            # interfering, etc.) without paging on every successful
            # call.
            logger.info(
                "backend.reasoning_content.rescue",
                gen_ai_system=self.gen_ai_system,
                model=self.model,
                response_model=data.get("model"),
                completion_tokens=data.get("usage", {}).get("completion_tokens", 0),
                max_tokens=max_tokens,
                reasoning_chars=len(reasoning_content),
            )
        tool_calls = None

        raw_calls = message.get("tool_calls")
        if raw_calls:
            tool_calls = []
            for call in raw_calls:
                func = call.get("function", {})
                args = func.get("arguments", "{}")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"_raw": args}
                tool_calls.append(
                    {
                        "id": call.get("id", ""),
                        "name": func.get("name", ""),
                        "arguments": args,
                    }
                )

        finish_reason = choice.get("finish_reason", "stop")
        stop_reason = "tool_use" if finish_reason == "tool_calls" else "end_turn"

        return {
            "content": content,
            "model": data.get("model", self.model),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "tool_calls": tool_calls,
            "stop_reason": stop_reason,
            "reasoning_content": reasoning_content,
            # OTel GenAI semantic convention metadata
            "gen_ai_system": self.gen_ai_system,
            "gen_ai_request_model": self.model,
            "gen_ai_response_model": data.get("model", self.model),
            "gen_ai_request_temperature": temperature,
            "gen_ai_request_max_tokens": max_tokens,
        }

    async def aclose(self) -> None:
        """Close the underlying ``httpx.AsyncClient``."""
        await self.client.aclose()


class LMStudioBackend(OpenAICompatibleBackend):
    """Local models via LM Studio's OpenAI-compatible API.

    LM Studio runs a local HTTP server (default port 1234) that exposes
    an OpenAI-compatible ``/v1/chat/completions`` endpoint backed by
    its MLX or llama.cpp runtime.  Any model loaded in LM Studio's UI
    is reachable through this backend.

    Args:
        model: Model identifier as shown by ``GET /v1/models`` (or
            ``"default"`` to use whichever model LM Studio routes to).
        base_url: LM Studio server URL.  Both ``http://localhost:1234``
            and ``http://localhost:1234/v1`` are accepted; the trailing
            ``/v1`` is normalized away.
        api_key: Ignored by LM Studio but sent as a Bearer token for
            wire compatibility.
    """

    gen_ai_system = "lmstudio"

    def __init__(
        self,
        model: str = "default",
        base_url: str = "http://localhost:1234/v1",
        api_key: str = "not-needed",
    ) -> None:
        super().__init__(base_url=base_url, api_key=api_key, model=model)


# ---------------------------------------------------------------------------
# Message format helpers
# ---------------------------------------------------------------------------


def _anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert internal message format to Anthropic Messages API format."""
    result = []
    for msg in messages:
        role = msg["role"]

        if role == "user":
            result.append({"role": "user", "content": msg["content"]})

        elif role == "assistant":
            # May contain text and/or tool_calls
            content_blocks = []
            if msg.get("content"):
                content_blocks.append({"type": "text", "text": msg["content"]})
            content_blocks.extend(
                {
                    "type": "tool_use",
                    "id": call["id"],
                    "name": call["name"],
                    "input": call["arguments"],
                }
                for call in msg.get("tool_calls", [])
            )
            result.append({"role": "assistant", "content": content_blocks})

        elif role == "tool":
            result.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg["tool_call_id"],
                            "content": msg["content"],
                        }
                    ],
                }
            )

    return result


def _ollama_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert internal message format to Ollama /api/chat format."""
    result = []
    for msg in messages:
        role = msg["role"]

        if role == "user":
            result.append({"role": "user", "content": msg["content"]})

        elif role == "assistant":
            entry: dict[str, Any] = {"role": "assistant"}
            if msg.get("content"):
                entry["content"] = msg["content"]
            if msg.get("tool_calls"):
                entry["tool_calls"] = [
                    {
                        "function": {
                            "name": call["name"],
                            "arguments": call["arguments"],
                        },
                    }
                    for call in msg["tool_calls"]
                ]
            result.append(entry)

        elif role == "tool":
            result.append(
                {
                    "role": "tool",
                    "content": msg["content"],
                }
            )

    return result


def _openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert internal message format to OpenAI /v1/chat/completions format."""
    result = []
    for msg in messages:
        role = msg["role"]

        if role == "user":
            result.append({"role": "user", "content": msg["content"]})

        elif role == "assistant":
            entry: dict[str, Any] = {"role": "assistant"}
            if msg.get("content"):
                entry["content"] = msg["content"]
            if msg.get("tool_calls"):
                entry["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(call["arguments"]),
                        },
                    }
                    for call in msg["tool_calls"]
                ]
            result.append(entry)

        elif role == "tool":
            result.append(
                {
                    "role": "tool",
                    "tool_call_id": msg["tool_call_id"],
                    "content": msg["content"],
                }
            )

    return result


def _select_local_backend() -> LLMBackend | None:
    """Pick a backend for the ``local`` tier based on env vars.

    Resolution rules:

    - If ``HEDDLE_LOCAL_BACKEND`` is ``"lmstudio"``, use LM Studio (and
      fail open if its URL is missing — the worker will simply not get
      a local-tier backend rather than silently falling back to Ollama).
    - If ``HEDDLE_LOCAL_BACKEND`` is ``"ollama"``, use Ollama similarly.
    - Otherwise pick LM Studio when ``LM_STUDIO_URL`` is set, else
      Ollama when ``OLLAMA_URL`` is set, else nothing.

    Returns the configured backend or ``None`` if none can be resolved.

    TODO(local-runtime-registry): replace this hardcoded resolver with
    a data-driven registry of local LLM runtimes.

    Why: every new local-tier runtime currently requires touching
    ~7 files in lockstep — this resolver, ``HeddleConfig`` + ``_ENV_MAP``
    in ``cli/config.py``, the ``cli/setup.py`` wizard, ``cli/preflight.py``,
    ``workshop/app.py``'s backend-badges, ``mcp/session_bridge.py``'s
    health checks, and ``cli/rag.py``'s embedding resolver — plus tests
    and docs.  This has now happened twice (Ollama → +LM Studio); a
    third runtime (Exo, vLLM, MLX-server, llama.cpp server, …) is the
    right time to refactor.

    Proposed shape (do NOT implement until needed):

        @dataclass(frozen=True)
        class LocalRuntime:
            key: str                  # "ollama", "lmstudio", "exo", ...
            label: str                # "LM Studio (local)"
            backend_cls: type[LLMBackend]
            url_env: str              # "LM_STUDIO_URL"
            model_env: str            # "LM_STUDIO_MODEL"
            default_url: str          # "http://localhost:1234/v1"
            default_model: str        # "default"
            embedding_provider: str   # "openai-compatible" | "ollama"
            # Probe returning (reachable, list_of_model_ids).  Used by
            # the setup wizard and Workshop badges.
            probe: Callable[[str], tuple[bool, list[str]]]

        LOCAL_RUNTIMES: tuple[LocalRuntime, ...] = (
            LocalRuntime("lmstudio", ...),  # OpenAICompatibleBackend subclass
            LocalRuntime("ollama",   ...),  # OllamaBackend
        )

    Migration touchpoints (each becomes a tight loop over the registry):
      - this resolver — 5 lines: iterate, honour HEDDLE_LOCAL_BACKEND,
        fall through to the first runtime whose ``url_env`` is set.
      - ``cli/config.py``: ``HeddleConfig`` fields + ``_ENV_MAP``
        become generated from the registry (or kept hand-written but
        validated against it).
      - ``cli/setup.py``: replace the per-runtime wizard sections with
        a single loop over ``LOCAL_RUNTIMES``.
      - ``cli/preflight.py:check_env_vars`` for ``tier == "local"``.
      - ``workshop/app.py:_detect_available_backends``.
      - ``cli/rag.py:_resolve_embedding_settings`` reads
        ``embedding_provider`` from the chosen runtime instead of
        branching on ``local_backend``.
      - ``mcp/session_bridge.py``: parallel ``_check_*`` methods become
        one loop.

    Adding a new HTTP/OpenAI-compatible runtime (Exo on port 52415,
    vLLM, llama.cpp ``server`` mode, LiteLLM proxy, TGI, …) then
    becomes a single ``LocalRuntime`` entry plus a thin
    ``OpenAICompatibleBackend`` subclass — no other file changes.

    Future runtimes that the registry alone does NOT cover (and would
    need ABC extensions on :class:`LLMBackend`):
      - Non-HTTP / in-process Python (MLX, transformers, mlc-llm):
        need lifecycle hooks (``load`` / ``unload``) on the backend,
        plus a sync-in-async bridge.  Out of scope for the registry.
      - Streaming-first runtimes: need a ``complete_stream()`` method
        returning an async iterator.  Out of scope for the registry.
      - Tool-use formats that diverge from Anthropic / OpenAI / Ollama
        schemas: add another ``_xxx_messages()`` helper in this file
        and a per-runtime translator hook on the backend.
    """
    lm_studio_url = os.getenv("LM_STUDIO_URL")
    ollama_url = os.getenv("OLLAMA_URL")
    preference = os.getenv("HEDDLE_LOCAL_BACKEND", "").strip().lower()

    def _make_lmstudio() -> LLMBackend | None:
        if not lm_studio_url:
            return None
        return LMStudioBackend(
            model=os.getenv("LM_STUDIO_MODEL", "default"),
            base_url=lm_studio_url,
        )

    def _make_ollama() -> LLMBackend | None:
        if not ollama_url:
            return None
        return OllamaBackend(
            model=os.getenv("OLLAMA_MODEL", "llama3.2:3b"),
            base_url=ollama_url,
        )

    if preference == "lmstudio":
        return _make_lmstudio()
    if preference == "ollama":
        return _make_ollama()

    # No explicit preference — LM Studio wins when both are set (newer
    # default), otherwise fall through to whichever is configured.
    return _make_lmstudio() or _make_ollama()


def build_backends_from_env() -> dict[str, LLMBackend]:
    """Build LLM backends from environment variables and ``~/.heddle/config.yaml``.

    Resolution priority: env vars > config.yaml > built-in defaults.

    Resolves available backends based on which env vars are set:

    - ``LM_STUDIO_URL`` → :class:`LMStudioBackend` for the ``local`` tier
    - ``LM_STUDIO_MODEL`` → Override LM Studio model (default: ``default``)
    - ``OLLAMA_URL`` → :class:`OllamaBackend` for the ``local`` tier
    - ``OLLAMA_MODEL`` → Override Ollama model (default: ``llama3.2:3b``)
    - ``HEDDLE_LOCAL_BACKEND`` → ``"lmstudio"`` or ``"ollama"`` to pick
      explicitly when both URLs are set.  Defaults to LM Studio.
    - ``ANTHROPIC_API_KEY`` → :class:`AnthropicBackend` for ``standard``
      + ``frontier``
    - ``FRONTIER_MODEL`` → Override frontier model (default:
      ``claude-opus-4-20250514``)

    Returns:
        Dict mapping tier name → LLMBackend instance. May be empty if no
        environment variables are set.
    """
    # Load config.yaml defaults (best-effort; env vars still override)
    try:
        from heddle.cli.config import apply_config_to_env, load_config

        config = load_config()
        apply_config_to_env(config)
    except Exception:
        pass

    backends: dict[str, LLMBackend] = {}

    local_backend = _select_local_backend()
    if local_backend is not None:
        backends["local"] = local_backend

    if anthropic_key := os.getenv("ANTHROPIC_API_KEY"):
        backends["standard"] = AnthropicBackend(api_key=anthropic_key)
        backends["frontier"] = AnthropicBackend(
            api_key=anthropic_key,
            model=os.getenv("FRONTIER_MODEL", "claude-opus-4-20250514"),
        )

    return backends
