"""ChatBridge ABC and shared data models.

All chat bridges implement :class:`ChatBridge`, which provides a
session-aware interface for multi-turn conversations with external
LLM providers or human participants.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class ChatResponse(BaseModel):
    """Response from a chat bridge turn.

    For "thinking" / chain-of-thought models served by some
    OpenAI-compatible providers (LM Studio, vLLM with reasoning
    models, DeepSeek, …), the visible answer may be empty while the
    model produces a long internal monologue.  When that happens,
    bridges fall back to using the reasoning text as ``content`` so
    downstream consumers see *something*; the raw monologue is still
    available on ``reasoning_content`` for callers that want to
    surface it separately (or strip it from logs).
    """

    content: str
    model: str | None = None
    token_usage: dict[str, int] = Field(default_factory=dict)
    stop_reason: str | None = None
    session_id: str = ""
    # Raw reasoning trace from thinking-style models, when the
    # provider exposes it as a separate field (e.g. LM Studio's
    # ``message.reasoning_content``).  ``None`` when the provider
    # does not split reasoning from content.
    reasoning_content: str | None = None


class SessionInfo(BaseModel):
    """Metadata about an active chat session."""

    session_id: str
    bridge_type: str
    model: str | None = None
    message_count: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


@dataclass
class _Session:
    """Internal session state — tracks messages and metadata."""

    session_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    system_prompt: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class ChatBridge(ABC):
    """Abstract base for external chat session adapters.

    Each bridge maintains per-session conversation history.  The worker
    itself remains stateless (per Heddle invariants) — the state lives in
    the bridge's internal session dict or in the external provider's
    session.

    Subclasses must implement :meth:`send_turn`,
    :meth:`get_session_info`, and :meth:`close_session`.
    """

    def __init__(self, system_prompt: str = "") -> None:
        self._sessions: dict[str, _Session] = {}
        self._system_prompt = system_prompt

    def _get_or_create_session(self, session_id: str) -> _Session:
        """Get existing session or create a new one."""
        if session_id not in self._sessions:
            self._sessions[session_id] = _Session(
                session_id=session_id,
                system_prompt=self._system_prompt,
            )
        return self._sessions[session_id]

    @abstractmethod
    async def send_turn(
        self,
        message: str,
        context: dict[str, Any],
        session_id: str,
    ) -> ChatResponse:
        """Send a message and get a response.

        Args:
            message: The user message for this turn.
            context: Additional context (round metadata, topic, etc.).
            session_id: Identifies the persistent conversation session.

        Returns:
            The assistant's response as a :class:`ChatResponse`.
        """

    @abstractmethod
    async def get_session_info(self, session_id: str) -> SessionInfo:
        """Return metadata about a session."""

    async def close_session(self, session_id: str) -> None:
        """Clean up session state."""
        self._sessions.pop(session_id, None)

    async def aclose(self) -> None:
        """Release any I/O resources held by this bridge.

        Subclasses that hold open connections (e.g. an
        ``httpx.AsyncClient``) override this to close them.  Idempotent —
        safe to call more than once.  The default clears any in-memory
        session state; subclasses that override should call ``super()``
        last so sessions are cleared after their resources are released.
        """
        self._sessions.clear()
