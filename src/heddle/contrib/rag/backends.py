"""Heddle SyncProcessingBackend wrappers for the RAG pipeline stages.

Each backend wraps a standalone class (TelegramIngestor, StreamMux,
SentenceChunker) and exposes it through the standard ``process_sync()``
interface used by Heddle's ProcessorWorker.

Usage in a worker YAML config::

    processing_backend: "heddle.contrib.rag.backends.IngestorBackend"
    backend_config:
      source_path: "/data/exports/result-1.json"

These backends are CPU-bound (no LLM calls) so they extend
SyncProcessingBackend which automatically offloads to a thread pool.
"""

from __future__ import annotations

import threading
from typing import Any

import structlog

from heddle.contrib.rag.chunker.sentence_chunker import ChunkConfig, chunk_post
from heddle.contrib.rag.ingestion.telegram_ingestor import TelegramIngestor
from heddle.contrib.rag.mux.stream_mux import StreamMux
from heddle.contrib.rag.schemas.mux import MuxWindowConfig
from heddle.contrib.rag.schemas.post import NormalizedPost
from heddle.worker.processor import SyncProcessingBackend

logger = structlog.get_logger()


class IngestorBackend(SyncProcessingBackend):
    """Heddle backend for ingestion (Telegram JSON by default, configurable).

    Payload keys:
        source_path (str): Path to the source file.

    Config keys:
        ingestor_class (str): Dotted path to an Ingestor subclass.
            Default: ``heddle.contrib.rag.ingestion.telegram_ingestor.TelegramIngestor``

    Output:
        posts (list[dict]): List of NormalizedPost dicts.
        channel_id (int): Channel ID from the export.
        channel_name (str): Channel name from the export.
        post_count (int): Number of posts produced.
    """

    def __init__(
        self,
        source_path: str | None = None,
        ingestor_class: str | None = None,
        **kwargs: Any,
    ) -> None:
        self._default_source = source_path
        self._ingestor_class_path = ingestor_class

    def _resolve_ingestor_class(self) -> type:
        """Resolve ingestor class from dotted path or return default."""
        if not self._ingestor_class_path:
            return TelegramIngestor
        import importlib

        module_path, class_name = self._ingestor_class_path.rsplit(".", 1)
        mod = importlib.import_module(module_path)
        return getattr(mod, class_name)

    def process_sync(self, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        """Ingest a source file and return normalized posts."""
        source = payload.get("source_path") or self._default_source
        if not source:
            raise ValueError("source_path is required")

        ingestor_cls = self._resolve_ingestor_class()
        ingestor = ingestor_cls(source).load()
        posts = ingestor.ingest_all()

        return {
            "output": {
                "posts": [p.model_dump(mode="json") for p in posts],
                "channel_id": getattr(ingestor, "channel_id", None),
                "channel_name": getattr(ingestor, "channel_name", None),
                "post_count": len(posts),
            },
            "model_used": getattr(ingestor, "__class__", type(ingestor)).__name__,
        }


class MuxBackend(SyncProcessingBackend):
    """Heddle backend for stream multiplexing.

    Payload keys:
        posts_by_channel (list[list[dict]]): List of channel post lists.
        window_hours (float): Window duration in hours (default: 6).
        sliding_step_hours (float | None): Step for sliding windows.

    Output:
        stream (dict): MuxedStream dict.
        total_entries (int): Total entries in the merged stream.
        window_count (int): Number of windows assigned.
    """

    def process_sync(self, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        """Multiplex channel post streams into a merged stream."""
        posts_by_channel = payload.get("posts_by_channel", [])
        window_hours = payload.get("window_hours", 6.0)
        sliding_step = payload.get("sliding_step_hours")

        from datetime import timedelta

        window_config = MuxWindowConfig(
            window_duration=timedelta(hours=window_hours),
            step=timedelta(hours=sliding_step) if sliding_step else None,
        )

        mux = StreamMux()
        for channel_posts_raw in posts_by_channel:
            posts = [NormalizedPost(**p) for p in channel_posts_raw]
            if posts:
                mux.add_stream(posts)

        stream = mux.merge(window_config=window_config)

        return {
            "output": {
                "stream": stream.model_dump(mode="json"),
                "total_entries": stream.total_entries,
                "window_count": len(stream.window_ids),
            },
            "model_used": "stream-mux",
        }


class ChunkerBackend(SyncProcessingBackend):
    """Heddle backend for sentence-level text chunking.

    Payload keys:
        posts (list[dict]): List of NormalizedPost dicts to chunk.
        target_chars (int): Soft target chars per chunk (default: 400).
        max_chars (int): Hard max chars per chunk (default: 600).

    Output:
        chunks (list[dict]): List of TextChunk dicts.
        chunk_count (int): Total chunks produced.
    """

    def __init__(
        self,
        target_chars: int = 400,
        max_chars: int = 600,
        **kwargs: Any,
    ) -> None:
        self._default_config = ChunkConfig(
            target_chars=target_chars,
            max_chars=max_chars,
        )

    def process_sync(self, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        """Chunk normalized posts into text segments."""
        posts_raw = payload.get("posts", [])
        chunk_cfg = ChunkConfig(
            target_chars=payload.get("target_chars", self._default_config.target_chars),
            max_chars=payload.get("max_chars", self._default_config.max_chars),
        )

        all_chunks = []
        for p in posts_raw:
            post = NormalizedPost(**p)
            chunks = chunk_post(post, config=chunk_cfg)
            all_chunks.extend(chunks)

        return {
            "output": {
                "chunks": [c.model_dump(mode="json") for c in all_chunks],
                "chunk_count": len(all_chunks),
            },
            "model_used": "sentence-chunker",
        }


class VectorStoreBackend(SyncProcessingBackend):
    """Heddle backend for vector store operations.

    Payload keys:
        action (str): "store" | "search" | "stats"
        For "store":
            chunks (list[dict]): TextChunk dicts to embed and store
        For "search":
            query (str): Search query
            limit (int): Max results (default: 10)
            channel_ids (list[int]): Optional channel filter
        For "stats":
            (no additional keys)

    Config keys:
        store_class (str): Dotted path to a VectorStore subclass.
            Default: ``heddle.contrib.rag.vectorstore.duckdb_store.DuckDBVectorStore``

    Output varies by action.
    """

    def __init__(
        self,
        db_path: str = "/tmp/rag-vectors.duckdb",
        embedding_model: str = "nomic-embed-text",
        ollama_url: str = "http://localhost:11434",
        store_class: str | None = None,
        **kwargs: Any,
    ) -> None:
        self._db_path = db_path
        self._embedding_model = embedding_model
        self._ollama_url = ollama_url
        self._store_class_path = store_class
        # ``process_sync`` previously opened + closed the store on
        # every call.  For a search-heavy worker that's one DuckDB /
        # LanceDB open per query plus a fresh embedder instantiation
        # per query — leaks FDs under load and adds noticeable
        # latency.  Cache by ``(store_class, db_path)`` since those
        # are the only knobs that vary per call; the lock makes the
        # check-then-open sequence safe under concurrent thread-pool
        # dispatch from ``SyncProcessingBackend``.
        self._store_cache: dict[tuple[type, str], Any] = {}
        self._cache_lock = threading.Lock()

    def _resolve_store_class(self) -> type:
        """Resolve store class from dotted path or return default."""
        if not self._store_class_path:
            from heddle.contrib.rag.vectorstore.duckdb_store import DuckDBVectorStore

            return DuckDBVectorStore
        import importlib

        module_path, class_name = self._store_class_path.rsplit(".", 1)
        mod = importlib.import_module(module_path)
        return getattr(mod, class_name)

    def _get_or_open_store(self, store_cls: type, db_path: str) -> Any:
        """Return a cached store for ``(store_cls, db_path)`` or open one.

        Caller holds no lock; this method acquires the cache lock for
        the check-then-open sequence so concurrent thread-pool calls
        don't each open their own duplicate store.
        """
        key = (store_cls, db_path)
        with self._cache_lock:
            cached = self._store_cache.get(key)
            if cached is not None:
                return cached
            store = store_cls(
                db_path=db_path,
                embedding_model=self._embedding_model,
                ollama_url=self._ollama_url,
            ).initialize()
            self._store_cache[key] = store
            return store

    async def aclose(self) -> None:
        """Close every cached vector store."""
        with self._cache_lock:
            cached = list(self._store_cache.values())
            self._store_cache.clear()
        for store in cached:
            try:
                store.close()
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "vectorstore_backend.close_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

    def process_sync(self, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        """Execute a vector store operation (store, search, or stats)."""
        from heddle.contrib.rag.schemas.chunk import TextChunk

        action = payload.get("action", "")
        db_path = config.get("db_path", self._db_path)

        store_cls = self._resolve_store_class()
        store = self._get_or_open_store(store_cls, db_path)

        store_name = store_cls.__name__.replace("VectorStore", "").lower() or "vector"

        if action == "store":
            chunks = [TextChunk(**c) for c in payload.get("chunks", [])]
            count = store.add_chunks(chunks)
            return {
                "output": {"stored_count": count, "total": store.count()},
                "model_used": f"{store_name}+{self._embedding_model}",
            }

        if action == "search":
            query = payload.get("query", "")
            limit = payload.get("limit", 10)
            channel_ids = payload.get("channel_ids")
            results = store.search(query, limit=limit, channel_ids=channel_ids)
            return {
                "output": {
                    "results": [r.model_dump(mode="json") for r in results],
                    "count": len(results),
                },
                "model_used": f"{store_name}+{self._embedding_model}",
            }

        if action == "stats":
            return {
                "output": store.stats(),
                "model_used": store_name,
            }

        raise ValueError(f"Unknown action '{action}'. Supported: store, search, stats")
