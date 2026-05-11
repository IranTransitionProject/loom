"""Contract tests for async-client lifecycle (``aclose`` discipline).

Several Heddle classes hold a long-lived ``httpx.AsyncClient`` (LLM
backends, embedding providers, chat bridges) or a Redis connection
pool (RedisCheckpointStore). Each MUST expose an idempotent
``aclose()`` that releases the underlying client; owners MUST call it
on shutdown.  Without this discipline, sockets and tasks leak across
actor restarts and test sessions.

This module enforces the contract for every canonical concrete class
shipped today.  When a new client class is added (e.g. a vLLM
backend, a Cohere chat bridge), append it to the parametrize lists
below — that is the only place the lifecycle expectation is encoded,
and the failing import keeps the omission visible at code-review
time.

What we check, per class:

1. ``aclose`` is callable and returns an awaitable.
2. After ``aclose()`` the underlying client is actually closed
   (``httpx.AsyncClient.is_closed`` is True; mocked Redis client's
   ``aclose`` was awaited).
3. ``aclose()`` is idempotent — a second call must not raise.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, patch

import pytest

from heddle.contrib.chatbridge.anthropic import AnthropicChatBridge
from heddle.contrib.chatbridge.lmstudio import LMStudioChatBridge
from heddle.contrib.chatbridge.ollama import OllamaChatBridge
from heddle.contrib.chatbridge.openai import OpenAIChatBridge
from heddle.contrib.redis.store import RedisCheckpointStore
from heddle.worker.backends import (
    AnthropicBackend,
    LMStudioBackend,
    OllamaBackend,
    OpenAICompatibleBackend,
)
from heddle.worker.embeddings import (
    OllamaEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
)

# ---------------------------------------------------------------------------
# Backends — heddle.worker.backends
# ---------------------------------------------------------------------------


def _backend_factories() -> list[tuple[str, callable]]:
    """Return (name, zero-arg factory) pairs for each LLMBackend subclass.

    Factories use safe URLs/keys — they construct the client but never
    issue a request, so no network is needed.
    """
    return [
        ("AnthropicBackend", lambda: AnthropicBackend(api_key="test-key")),
        ("OllamaBackend", lambda: OllamaBackend(base_url="http://test:11434")),
        (
            "OpenAICompatibleBackend",
            lambda: OpenAICompatibleBackend(base_url="http://test:1234"),
        ),
        ("LMStudioBackend", lambda: LMStudioBackend(base_url="http://test:1234")),
    ]


@pytest.mark.parametrize(
    ("label", "factory"),
    _backend_factories(),
    ids=[name for name, _ in _backend_factories()],
)
class TestLLMBackendLifecycle:
    """Every LLMBackend implementation honours the aclose contract."""

    @pytest.mark.asyncio
    async def test_aclose_returns_awaitable(self, label, factory):
        backend = factory()
        coro = backend.aclose()
        assert inspect.iscoroutine(coro), f"{label}.aclose() must return a coroutine"
        await coro

    @pytest.mark.asyncio
    async def test_aclose_closes_httpx_client(self, label, factory):
        backend = factory()
        assert not backend.client.is_closed, f"{label} client closed before aclose()"
        await backend.aclose()
        assert backend.client.is_closed, f"{label}.aclose() did not close httpx client"

    @pytest.mark.asyncio
    async def test_aclose_is_idempotent(self, label, factory):
        backend = factory()
        await backend.aclose()
        # A second aclose() on a closed httpx.AsyncClient is a documented
        # no-op; this test guards against subclasses adding extra cleanup
        # that explodes on a second call.
        await backend.aclose()


# ---------------------------------------------------------------------------
# Embedding providers — heddle.worker.embeddings
# ---------------------------------------------------------------------------


def _embedding_factories() -> list[tuple[str, callable]]:
    return [
        (
            "OllamaEmbeddingProvider",
            lambda: OllamaEmbeddingProvider(base_url="http://test:11434"),
        ),
        (
            "OpenAICompatibleEmbeddingProvider",
            lambda: OpenAICompatibleEmbeddingProvider(base_url="http://test:1234/v1"),
        ),
    ]


@pytest.mark.parametrize(
    ("label", "factory"),
    _embedding_factories(),
    ids=[name for name, _ in _embedding_factories()],
)
class TestEmbeddingProviderLifecycle:
    @pytest.mark.asyncio
    async def test_aclose_returns_awaitable(self, label, factory):
        provider = factory()
        coro = provider.aclose()
        assert inspect.iscoroutine(coro), f"{label}.aclose() must return a coroutine"
        await coro

    @pytest.mark.asyncio
    async def test_aclose_closes_httpx_client(self, label, factory):
        provider = factory()
        assert not provider._client.is_closed
        await provider.aclose()
        assert provider._client.is_closed

    @pytest.mark.asyncio
    async def test_aclose_is_idempotent(self, label, factory):
        provider = factory()
        await provider.aclose()
        await provider.aclose()

    @pytest.mark.asyncio
    async def test_aclose_closes_sync_client_when_created(self, label, factory):
        """If ``embed_sync`` has been used, ``aclose`` must close the sync client too.

        The sync client is lazy.  Force creation via ``_get_sync_client``
        and confirm ``aclose`` actually closed it (``is_closed`` flips).
        """
        provider = factory()
        sync_client = provider._get_sync_client()
        assert not sync_client.is_closed
        await provider.aclose()
        assert sync_client.is_closed

    @pytest.mark.asyncio
    async def test_aclose_with_no_sync_client_is_safe(self, label, factory):
        """``aclose`` on a provider that never used embed_sync still works."""
        provider = factory()
        assert provider._sync_client is None
        await provider.aclose()  # must not raise


# ---------------------------------------------------------------------------
# Chat bridges — heddle.contrib.chatbridge.*
# ---------------------------------------------------------------------------


def _bridge_factories() -> list[tuple[str, callable]]:
    return [
        ("AnthropicChatBridge", lambda: AnthropicChatBridge(api_key="test-key")),
        (
            "OllamaChatBridge",
            lambda: OllamaChatBridge(base_url="http://test:11434"),
        ),
        (
            "OpenAIChatBridge",
            lambda: OpenAIChatBridge(api_key="test-key", base_url="http://test"),
        ),
        ("LMStudioChatBridge", lambda: LMStudioChatBridge(base_url="http://test:1234")),
    ]


@pytest.mark.parametrize(
    ("label", "factory"),
    _bridge_factories(),
    ids=[name for name, _ in _bridge_factories()],
)
class TestChatBridgeLifecycle:
    @pytest.mark.asyncio
    async def test_aclose_returns_awaitable(self, label, factory):
        bridge = factory()
        coro = bridge.aclose()
        assert inspect.iscoroutine(coro), f"{label}.aclose() must return a coroutine"
        await coro

    @pytest.mark.asyncio
    async def test_aclose_closes_httpx_client(self, label, factory):
        bridge = factory()
        assert not bridge._client.is_closed
        await bridge.aclose()
        assert bridge._client.is_closed

    @pytest.mark.asyncio
    async def test_aclose_clears_sessions(self, label, factory):
        bridge = factory()
        # Seed a session via the internal cache (no network needed).
        bridge._get_or_create_session("seeded")
        assert "seeded" in bridge._sessions
        await bridge.aclose()
        assert bridge._sessions == {}, f"{label}.aclose() must clear in-memory sessions"

    @pytest.mark.asyncio
    async def test_aclose_is_idempotent(self, label, factory):
        bridge = factory()
        await bridge.aclose()
        await bridge.aclose()


# ---------------------------------------------------------------------------
# CheckpointStore — heddle.contrib.redis.store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redis_checkpoint_store_aclose_closes_client():
    """RedisCheckpointStore.aclose() must close the underlying redis client.

    Verifies the call wires through to ``redis.aclose()`` (the
    redis-py async API).
    """
    mock_client = AsyncMock()
    mock_client.aclose = AsyncMock()
    with patch("heddle.contrib.redis.store.redis.from_url", return_value=mock_client):
        store = RedisCheckpointStore("redis://localhost:6379")
    await store.aclose()
    mock_client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_redis_checkpoint_store_aclose_is_idempotent():
    """Repeated aclose() calls must not raise."""
    mock_client = AsyncMock()
    mock_client.aclose = AsyncMock()
    with patch("heddle.contrib.redis.store.redis.from_url", return_value=mock_client):
        store = RedisCheckpointStore("redis://localhost:6379")
    await store.aclose()
    await store.aclose()
    assert mock_client.aclose.await_count == 2


# ---------------------------------------------------------------------------
# ABC default no-op aclose — guards the "test mocks need not implement it"
# contract.  If anyone re-flags these as @abstractmethod, every existing
# mock subclass breaks; this test is a tripwire.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_backend_abc_aclose_default_is_noop():
    from heddle.worker.backends import LLMBackend

    class Minimal(LLMBackend):
        async def complete(self, *a, **kw):
            return {"content": "", "model": "x", "prompt_tokens": 0, "completion_tokens": 0}

    await Minimal().aclose()  # must not raise


@pytest.mark.asyncio
async def test_embedding_provider_abc_aclose_default_is_noop():
    from heddle.worker.embeddings import EmbeddingProvider

    class Minimal(EmbeddingProvider):
        async def embed(self, text):
            return []

        async def embed_batch(self, texts):
            return []

        def embed_sync(self, text):
            return []

        def embed_batch_sync(self, texts):
            return []

        @property
        def dimensions(self):
            return 0

    await Minimal().aclose()


@pytest.mark.asyncio
async def test_chat_bridge_abc_aclose_clears_sessions():
    from heddle.contrib.chatbridge.base import ChatBridge, ChatResponse, SessionInfo

    class Minimal(ChatBridge):
        async def send_turn(self, message, context, session_id):
            return ChatResponse(content="ok", session_id=session_id)

        async def get_session_info(self, session_id):
            return SessionInfo(session_id=session_id, bridge_type="minimal")

    bridge = Minimal()
    bridge._get_or_create_session("s1")
    await bridge.aclose()
    assert bridge._sessions == {}


@pytest.mark.asyncio
async def test_processing_backend_abc_aclose_default_is_noop():
    from heddle.worker.processor import ProcessingBackend

    class Minimal(ProcessingBackend):
        async def process(self, payload, config):
            return {"output": {}, "model_used": None}

    await Minimal().aclose()


@pytest.mark.asyncio
async def test_checkpoint_store_abc_aclose_default_is_noop():
    from heddle.orchestrator.store import InMemoryCheckpointStore

    # InMemoryCheckpointStore inherits the ABC default and should
    # remain a no-op — the in-memory dict has nothing to close.
    await InMemoryCheckpointStore().aclose()
