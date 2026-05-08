"""
Embedding provider abstraction for vector generation.

Workers and tools generate vector embeddings from text via an
:class:`EmbeddingProvider`.  Two implementations ship with Heddle:

- :class:`OllamaEmbeddingProvider` — Ollama's ``/api/embed`` endpoint.
- :class:`OpenAICompatibleEmbeddingProvider` — any
  ``/v1/embeddings`` endpoint (LM Studio, OpenAI, vLLM, TEI, …).

Example usage::

    provider = OllamaEmbeddingProvider(model="nomic-embed-text")
    vector = await provider.embed("some text to embed")
    vectors = await provider.embed_batch(["text 1", "text 2"])

    # LM Studio (or any OpenAI-compatible /v1/embeddings server):
    provider = OpenAICompatibleEmbeddingProvider(
        model="text-embedding-nomic-embed-text-v1.5",
        base_url="http://localhost:1234/v1",
    )
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod

import httpx
import structlog

logger = structlog.get_logger()


class EmbeddingProvider(ABC):
    """Common interface for generating vector embeddings from text."""

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """Return embedding vector for the given text."""
        ...

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Return embedding vectors for a batch of texts."""
        ...

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Return the dimensionality of embeddings produced by this provider."""
        ...

    async def aclose(self) -> None:  # noqa: B027 — intentional no-op default
        """Release any I/O resources held by this provider.

        Subclasses that hold open connections (e.g. an
        ``httpx.AsyncClient``) override this to close them. Idempotent —
        safe to call more than once. Default is a no-op.
        """


class OllamaEmbeddingProvider(EmbeddingProvider):
    """Generate embeddings via Ollama's /api/embed endpoint.

    Uses the Ollama embedding API which supports both single and batch
    embedding generation. Dimensions are detected lazily from the first
    embedding call and cached.

    Args:
        model: Embedding model name (default: "nomic-embed-text").
        base_url: Ollama server URL. Falls back to OLLAMA_URL env var,
            then "http://localhost:11434".
    """

    def __init__(
        self,
        model: str = "nomic-embed-text",
        base_url: str | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url or os.environ.get("OLLAMA_URL") or "http://localhost:11434"
        self._dimensions: int | None = None
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=120.0)

    async def embed(self, text: str) -> list[float]:
        """Generate embedding for a single text string."""
        resp = await self._client.post(
            "/api/embed",
            json={"model": self.model, "input": text},
        )
        resp.raise_for_status()
        data = resp.json()
        embedding = data["embeddings"][0]

        # Cache dimensions from first call
        if self._dimensions is None:
            self._dimensions = len(embedding)

        return embedding

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts in one call.

        Ollama's /api/embed supports batch input via the ``input`` field
        accepting a list of strings.
        """
        if not texts:
            return []

        resp = await self._client.post(
            "/api/embed",
            json={"model": self.model, "input": texts},
        )
        resp.raise_for_status()
        data = resp.json()
        embeddings = data["embeddings"]

        if self._dimensions is None and embeddings:
            self._dimensions = len(embeddings[0])

        return embeddings

    @property
    def dimensions(self) -> int:
        """Return embedding dimensionality (detected from first call)."""
        if self._dimensions is None:
            raise RuntimeError(
                "Embedding dimensions not yet known. Call embed() or embed_batch() first."
            )
        return self._dimensions

    async def aclose(self) -> None:
        """Close the underlying ``httpx.AsyncClient``."""
        await self._client.aclose()


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    """Generate embeddings via any OpenAI-compatible ``/v1/embeddings`` API.

    Works with LM Studio, OpenAI, vLLM, Text Embeddings Inference (TEI),
    LiteLLM, and any other server that speaks the OpenAI embeddings
    schema.  Batch input is sent as a list under ``input`` per the
    OpenAI spec; the server is expected to return ``data`` as a list of
    objects with an ``embedding`` field.

    Args:
        model: Embedding model name as exposed by the server's
            ``/v1/models`` endpoint (e.g.
            ``"text-embedding-nomic-embed-text-v1.5"`` for LM Studio,
            ``"text-embedding-3-small"`` for OpenAI).
        base_url: Server base URL.  Both ``http://host:port`` and
            ``http://host:port/v1`` are accepted; the trailing ``/v1``
            is normalized away.  Falls back to ``LM_STUDIO_URL`` env
            var, then ``http://localhost:1234/v1``.
        api_key: Sent as a Bearer token.  LM Studio ignores it; OpenAI
            requires a real key.  Falls back to ``OPENAI_API_KEY`` env.
    """

    def __init__(
        self,
        model: str = "text-embedding-nomic-embed-text-v1.5",
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.model = model
        raw = base_url or os.environ.get("LM_STUDIO_URL") or "http://localhost:1234/v1"
        normalized = raw.rstrip("/")
        if normalized.endswith("/v1"):
            normalized = normalized[: -len("/v1")]
        self.base_url = normalized
        self._dimensions: int | None = None
        key = api_key or os.environ.get("OPENAI_API_KEY") or "not-needed"
        self._client = httpx.AsyncClient(
            base_url=normalized,
            headers={"Authorization": f"Bearer {key}"},
            timeout=120.0,
        )

    async def embed(self, text: str) -> list[float]:
        """Generate embedding for a single text string."""
        resp = await self._client.post(
            "/v1/embeddings",
            json={"model": self.model, "input": text},
        )
        resp.raise_for_status()
        data = resp.json()
        embedding = data["data"][0]["embedding"]

        if self._dimensions is None:
            self._dimensions = len(embedding)

        return embedding

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a batch of texts in one call."""
        if not texts:
            return []

        resp = await self._client.post(
            "/v1/embeddings",
            json={"model": self.model, "input": texts},
        )
        resp.raise_for_status()
        data = resp.json()
        # OpenAI spec: data is a list of {object, index, embedding}.
        # Sort by index to be safe (most servers return in order, but
        # the spec does not strictly guarantee it).
        items = sorted(data["data"], key=lambda d: d.get("index", 0))
        embeddings = [item["embedding"] for item in items]

        if self._dimensions is None and embeddings:
            self._dimensions = len(embeddings[0])

        return embeddings

    @property
    def dimensions(self) -> int:
        """Return embedding dimensionality (detected from first call)."""
        if self._dimensions is None:
            raise RuntimeError(
                "Embedding dimensions not yet known. Call embed() or embed_batch() first."
            )
        return self._dimensions

    async def aclose(self) -> None:
        """Close the underlying ``httpx.AsyncClient``."""
        await self._client.aclose()
