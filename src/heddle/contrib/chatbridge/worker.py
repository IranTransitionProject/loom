"""ChatBridgeBackend — wraps a ChatBridge as a Heddle ProcessingBackend.

This enables any ChatBridge adapter to be used as a standard Heddle worker
via YAML config alone, without writing Python code::

    name: "external_gpt4"
    processing_backend: "heddle.contrib.chatbridge.worker.ChatBridgeBackend"
    processing_config:
      bridge_class: "heddle.contrib.chatbridge.openai.OpenAIChatBridge"
      model: "gpt-4o"
      api_key_env: "OPENAI_API_KEY"
"""

from __future__ import annotations

import importlib
import os
from typing import Any

import structlog

from heddle.worker.processor import BackendError, SyncProcessingBackend

logger = structlog.get_logger()


class ChatBridgeBackendError(BackendError):
    """Error from ChatBridge processing."""


class ChatBridgeBackend(SyncProcessingBackend):
    """ProcessingBackend that delegates to a ChatBridge adapter.

    The bridge class is dynamically imported from config, enabling
    YAML-only configuration of external chat agents.

    Config keys:
        bridge_class: Dotted import path to a ChatBridge subclass.
        api_key_env: Optional env var name for API key.
        system_prompt: Optional system prompt for the bridge.
        **kwargs: Passed to the bridge constructor.
    """

    def __init__(self, **config: Any) -> None:
        super().__init__(serialize_writes=False)
        self._config = config
        self._bridge = self._create_bridge(config)

    @staticmethod
    def _create_bridge(config: dict[str, Any]) -> Any:
        """Dynamically import and instantiate the bridge class."""
        bridge_class_path = config.get("bridge_class")
        if not bridge_class_path:
            msg = "ChatBridgeBackend requires 'bridge_class' in config"
            raise ChatBridgeBackendError(msg)

        module_path, class_name = bridge_class_path.rsplit(".", 1)
        try:
            module = importlib.import_module(module_path)
            bridge_cls = getattr(module, class_name)
        except (ImportError, AttributeError) as e:
            msg = f"Failed to import bridge class '{bridge_class_path}': {e}"
            raise ChatBridgeBackendError(msg) from e

        # Build constructor kwargs from config.
        kwargs: dict[str, Any] = {}

        # Resolve API key from env var if specified.
        api_key_env = config.get("api_key_env")
        if api_key_env:
            kwargs["api_key"] = os.environ.get(api_key_env, "")

        for key in ("model", "base_url", "system_prompt", "max_tokens"):
            if key in config:
                kwargs[key] = config[key]

        return bridge_cls(**kwargs)

    def process_sync(
        self,
        payload: dict[str, Any],
        config: dict[str, Any],
    ) -> dict[str, Any]:
        """Not used — we override process() for async bridge calls."""
        msg = "ChatBridgeBackend.process_sync should not be called directly"
        raise NotImplementedError(msg)

    async def process(
        self,
        payload: dict[str, Any],
        config: dict[str, Any],
    ) -> dict[str, Any]:
        """Delegate to the bridge's send_turn method.

        Expects payload to contain:
            - ``message`` or the entire payload as the message
            - ``_session_id`` (optional, defaults to "default")
            - ``_context`` (optional, additional context dict)
        """
        session_id = payload.get("_session_id", "default")
        context = payload.get("_context", {})

        # Use 'message' field if present, otherwise serialize full payload.
        import json

        message = payload.get("message")
        if message is None:
            # Remove internal fields before serializing.
            clean = {k: v for k, v in payload.items() if not k.startswith("_")}
            message = json.dumps(clean, ensure_ascii=False, indent=2)

        try:
            response = await self._bridge.send_turn(message, context, session_id)
        except Exception as e:
            msg = f"ChatBridge send_turn failed: {e}"
            raise ChatBridgeBackendError(msg) from e

        return {
            "output": {
                "content": response.content,
                "model": response.model,
                "session_id": response.session_id,
            },
            "model_used": response.model,
            "token_usage": response.token_usage,
        }

    async def aclose(self) -> None:
        """Close the wrapped :class:`ChatBridge` (releases httpx clients)."""
        close = getattr(self._bridge, "aclose", None)
        if close is not None:
            await close()
