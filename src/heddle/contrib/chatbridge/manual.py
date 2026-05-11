"""Manual chat bridge — human-in-the-loop adapter.

Allows a human participant to join a council discussion or other
multi-agent flow.  Two modes:

    1. **Callback mode** — provide an ``on_prompt`` async callable that
       receives the context and returns a response string.
    2. **Queue mode** — prompts are put onto an ``asyncio.Queue``, and
       responses are awaited from a separate response queue.

Both modes enforce a timeout to prevent indefinite blocking.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from heddle.contrib.chatbridge.base import ChatBridge, ChatResponse, SessionInfo

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class ManualChatBridge(ChatBridge):
    """Human-in-the-loop chat bridge.

    Args:
        on_prompt: Async callback ``(message, context, session_id) -> str``.
            If provided, this is called for each turn.
        prompt_queue: Queue where prompts are put for external consumption.
        response_queue: Queue where responses are expected.
        timeout_seconds: Max time to wait for a human response.
        system_prompt: System instructions (informational for the human).
    """

    bridge_type = "manual"

    def __init__(
        self,
        on_prompt: Callable[[str, dict, str], Awaitable[str]] | None = None,
        prompt_queue: asyncio.Queue | None = None,
        response_queue: asyncio.Queue | None = None,
        timeout_seconds: float = 300.0,
        system_prompt: str = "",
    ) -> None:
        super().__init__(system_prompt=system_prompt)
        self._on_prompt = on_prompt
        self._prompt_queue = prompt_queue
        self._response_queue = response_queue
        self._timeout = timeout_seconds

    async def send_turn(
        self,
        message: str,
        context: dict[str, Any],
        session_id: str,
    ) -> ChatResponse:
        """Request a human response for this turn.

        Session history is only updated after the human responds.
        Earlier the prompt was appended eagerly, so a timeout or
        queue failure left it in history; on retry the human would
        see two consecutive prompts.
        """
        session = self._get_or_create_session(session_id)
        prompt_entry = {"role": "system", "content": message}

        if self._on_prompt is not None:
            content = await asyncio.wait_for(
                self._on_prompt(message, context, session_id),
                timeout=self._timeout,
            )
        elif self._prompt_queue is not None and self._response_queue is not None:
            await self._prompt_queue.put(
                {
                    "message": message,
                    "context": context,
                    "session_id": session_id,
                }
            )
            content = await asyncio.wait_for(
                self._response_queue.get(),
                timeout=self._timeout,
            )
        else:
            msg = "ManualChatBridge needs either on_prompt or both prompt_queue and response_queue"
            raise ValueError(msg)

        # Both entries persist together once the human responded —
        # a timeout or queue failure before this point leaves
        # session.messages untouched.
        session.messages.append(prompt_entry)
        session.messages.append({"role": "human", "content": content})

        return ChatResponse(
            content=content,
            model="human",
            token_usage={},
            stop_reason="human_input",
            session_id=session_id,
        )

    async def get_session_info(self, session_id: str) -> SessionInfo:
        """Return session metadata."""
        session = self._sessions.get(session_id)
        return SessionInfo(
            session_id=session_id,
            bridge_type=self.bridge_type,
            model="human",
            message_count=len(session.messages) if session else 0,
            created_at=session.created_at if session else None,
        )
