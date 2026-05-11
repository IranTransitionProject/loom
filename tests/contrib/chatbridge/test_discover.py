"""Tests for ``heddle.contrib.chatbridge.discover``."""

from __future__ import annotations

from heddle.contrib.chatbridge.anthropic import AnthropicChatBridge
from heddle.contrib.chatbridge.discover import chatbridge_spec, make_chatbridge
from heddle.contrib.chatbridge.lmstudio import LMStudioChatBridge
from heddle.contrib.chatbridge.ollama import OllamaChatBridge
from heddle.contrib.chatbridge.openai import OpenAIChatBridge


class TestChatbridgeSpec:
    def test_claude_prefix(self):
        path, kwargs = chatbridge_spec("claude-sonnet-4-20250514")
        assert path.endswith(".AnthropicChatBridge")
        assert kwargs == {"model": "claude-sonnet-4-20250514"}

    def test_anthropic_prefix_strips_provider(self):
        path, kwargs = chatbridge_spec("anthropic/claude-haiku-4-5-20251001")
        assert path.endswith(".AnthropicChatBridge")
        assert kwargs == {"model": "claude-haiku-4-5-20251001"}

    def test_gpt_prefix(self):
        path, kwargs = chatbridge_spec("gpt-4o")
        assert path.endswith(".OpenAIChatBridge")
        assert kwargs == {"model": "gpt-4o"}

    def test_openai_prefix_strips_provider(self):
        path, kwargs = chatbridge_spec("openai/gpt-4o-mini")
        assert path.endswith(".OpenAIChatBridge")
        assert kwargs == {"model": "gpt-4o-mini"}

    def test_lmstudio_prefix_strips_provider(self):
        path, kwargs = chatbridge_spec("lmstudio/google/gemma-3-4b")
        assert path.endswith(".LMStudioChatBridge")
        # First "/" is the provider separator; everything after is the model id.
        assert kwargs == {"model": "google/gemma-3-4b"}

    def test_unrecognized_falls_through_to_ollama(self):
        path, kwargs = chatbridge_spec("llama3.2:3b")
        assert path.endswith(".OllamaChatBridge")
        assert kwargs == {"model": "llama3.2:3b"}

    def test_case_insensitive_routing(self):
        path, _ = chatbridge_spec("CLAUDE-3-OPUS")
        assert path.endswith(".AnthropicChatBridge")

    def test_strips_surrounding_whitespace(self):
        path, kwargs = chatbridge_spec("  gpt-4o  ")
        assert path.endswith(".OpenAIChatBridge")
        assert kwargs == {"model": "gpt-4o"}


class TestMakeChatbridge:
    def test_builds_anthropic_instance(self, monkeypatch):
        # AnthropicChatBridge.__init__ now rejects empty api_key
        # (D4); set the env so the discover path can construct one.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        bridge = make_chatbridge("claude-sonnet-4-20250514")
        assert isinstance(bridge, AnthropicChatBridge)

    def test_builds_openai_instance(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        bridge = make_chatbridge("gpt-4o")
        assert isinstance(bridge, OpenAIChatBridge)

    def test_builds_lmstudio_instance(self):
        bridge = make_chatbridge("lmstudio/google/gemma-3-4b")
        assert isinstance(bridge, LMStudioChatBridge)

    def test_builds_ollama_instance(self):
        bridge = make_chatbridge("llama3.2:3b")
        assert isinstance(bridge, OllamaChatBridge)

    def test_extra_kwargs_override_defaults(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        bridge = make_chatbridge("gpt-4o", system_prompt="be helpful", max_tokens=42)
        assert isinstance(bridge, OpenAIChatBridge)
        # _system_prompt is the bridge-level default; per-session it's
        # set when a session is created.  We just confirm it stuck.
        assert bridge._system_prompt == "be helpful"
        assert bridge._max_tokens == 42
