"""Typed exceptions raised by ChatBridge implementations.

Bridges raise these so consumers (council runners, MCP, tournament
matchups) can attribute failures to specific causes without parsing
strings.
"""

from __future__ import annotations

from typing import Any


class ChatBridgeError(Exception):
    """Base for all ChatBridge-raised exceptions."""


class ChatBridgeMisconfiguredError(ChatBridgeError):
    """Raised at construction when a bridge cannot be safely used.

    The classic case is an empty API key: earlier the bridge would
    silently send ``Authorization: Bearer `` to the upstream API and
    fail with a generic 401 on the first call.  Raising at
    ``__init__`` surfaces the misconfiguration where it can be
    attributed to the constructor site.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class UnsupportedToolUseError(ChatBridgeError):
    """Raised when a provider returns ``tool_calls`` the bridge cannot dispatch.

    ChatBridges do not currently implement tool execution — the
    Heddle worker runtime owns the tool loop.  When a provider
    response carries ``tool_calls`` (typically when the model was
    advertised tools that the bridge wasn't aware of), the bridge
    raises this rather than returning an empty assistant turn.
    """

    def __init__(
        self,
        bridge: str,
        model: str,
        tool_calls: list[Any] | None = None,
    ) -> None:
        self.bridge = bridge
        self.model = model
        self.tool_calls = tool_calls or []
        names = ", ".join(tc.get("function", {}).get("name", "?") for tc in self.tool_calls)
        super().__init__(
            f"{bridge}({model}) returned tool_calls but the bridge does not "
            f"execute tools: {names or 'unnamed'}.  Disable tool advertisement "
            f"on the model side, or implement tool dispatch on the bridge."
        )
