"""Unit tests for heddle.contrib.rag.backends — Heddle SyncProcessingBackend wrappers."""

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest


def _make_telegram_export(messages, channel_name="test", channel_id=99):
    """Create a minimal Telegram export JSON in a temp file."""
    data = {
        "name": channel_name,
        "type": "public_channel",
        "id": channel_id,
        "messages": messages,
    }
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(data, f)
    f.close()
    return Path(f.name)


def _make_post_dict(channel_id: int, msg_id: int, hour: int = 0) -> dict:
    """Create a NormalizedPost dict for testing."""
    from heddle.contrib.rag.schemas.post import NormalizedPost

    post = NormalizedPost(
        global_id=f"{channel_id}:{msg_id}",
        source_channel_id=channel_id,
        source_channel_name=f"ch_{channel_id}",
        message_id=msg_id,
        timestamp=datetime(2026, 3, 1, hour, tzinfo=UTC),
        text_clean=f"This is a test post {msg_id} from channel {channel_id} with enough text.",
    )
    return post.model_dump(mode="json")


class TestIngestorBackend:
    def test_basic_ingestion(self):
        from heddle.contrib.rag.backends import IngestorBackend

        path = _make_telegram_export(
            [
                {
                    "id": 1,
                    "type": "message",
                    "date": "2026-03-01T10:00:00",
                    "date_unixtime": "1740826800",
                    "text": "Hello world from Telegram channel test.",
                },
                {
                    "id": 2,
                    "type": "message",
                    "date": "2026-03-01T11:00:00",
                    "date_unixtime": "1740830400",
                    "text": "Another message with enough text content.",
                },
            ]
        )
        backend = IngestorBackend()
        result = backend.process_sync({"source_path": str(path)}, {})
        assert result["output"]["post_count"] == 2
        assert result["output"]["channel_id"] == 99
        assert result["output"]["channel_name"] == "test"
        assert len(result["output"]["posts"]) == 2

    def test_default_source_path(self):
        from heddle.contrib.rag.backends import IngestorBackend

        path = _make_telegram_export(
            [
                {
                    "id": 1,
                    "type": "message",
                    "date": "2026-03-01T10:00:00",
                    "date_unixtime": "1740826800",
                    "text": "Post with default source path for test.",
                },
            ]
        )
        backend = IngestorBackend(source_path=str(path))
        result = backend.process_sync({}, {})
        assert result["output"]["post_count"] == 1

    def test_missing_source_raises(self):
        from heddle.contrib.rag.backends import IngestorBackend

        backend = IngestorBackend()
        with pytest.raises(ValueError, match="source_path is required"):
            backend.process_sync({}, {})


class TestMuxBackend:
    def test_merge_channels(self):
        from heddle.contrib.rag.backends import MuxBackend

        ch1 = [_make_post_dict(1, 1, 0), _make_post_dict(1, 2, 2)]
        ch2 = [_make_post_dict(2, 1, 1), _make_post_dict(2, 2, 3)]

        backend = MuxBackend()
        result = backend.process_sync(
            {
                "posts_by_channel": [ch1, ch2],
                "window_hours": 6.0,
            },
            {},
        )
        assert result["output"]["total_entries"] == 4
        assert result["output"]["window_count"] >= 1

    def test_empty_channels_skipped(self):
        from heddle.contrib.rag.backends import MuxBackend

        ch1 = [_make_post_dict(1, 1, 0)]
        backend = MuxBackend()
        result = backend.process_sync(
            {
                "posts_by_channel": [[], ch1],
            },
            {},
        )
        assert result["output"]["total_entries"] == 1


class TestChunkerBackend:
    def test_basic_chunking(self):
        from heddle.contrib.rag.backends import ChunkerBackend

        posts = [_make_post_dict(1, 1)]
        backend = ChunkerBackend()
        result = backend.process_sync({"posts": posts}, {})
        assert result["output"]["chunk_count"] >= 1
        assert len(result["output"]["chunks"]) == result["output"]["chunk_count"]

    def test_custom_chunk_size(self):
        from heddle.contrib.rag.backends import ChunkerBackend

        posts = [_make_post_dict(1, 1)]
        backend = ChunkerBackend(target_chars=10, max_chars=30)
        result = backend.process_sync({"posts": posts}, {})
        # With very small chunk size, should produce more chunks
        assert result["output"]["chunk_count"] >= 1

    def test_empty_posts(self):
        from heddle.contrib.rag.backends import ChunkerBackend

        backend = ChunkerBackend()
        result = backend.process_sync({"posts": []}, {})
        assert result["output"]["chunk_count"] == 0


class TestVectorStoreBackend:
    def test_stats_action(self, tmp_path):
        from heddle.contrib.rag.backends import VectorStoreBackend

        db_path = str(tmp_path / "test.duckdb")
        backend = VectorStoreBackend(db_path=db_path)
        result = backend.process_sync({"action": "stats"}, {"db_path": db_path})
        assert result["output"]["total_chunks"] == 0

    def test_unknown_action_raises(self, tmp_path):
        from heddle.contrib.rag.backends import VectorStoreBackend

        db_path = str(tmp_path / "test.duckdb")
        backend = VectorStoreBackend(db_path=db_path)
        with pytest.raises(ValueError, match="Unknown action"):
            backend.process_sync({"action": "unknown"}, {"db_path": db_path})

    def test_store_is_cached_across_calls(self, tmp_path):
        """Pin C4: repeated process_sync against same db_path reuses the store.

        Earlier the backend opened + closed the store on every call.
        Under search-heavy load that leaks FDs and adds connection
        overhead on the hot path.  The cache reuses one
        ``DuckDBVectorStore`` for all calls with the same key.
        """
        from heddle.contrib.rag.backends import VectorStoreBackend

        db_path = str(tmp_path / "test.duckdb")
        backend = VectorStoreBackend(db_path=db_path)
        backend.process_sync({"action": "stats"}, {"db_path": db_path})
        backend.process_sync({"action": "stats"}, {"db_path": db_path})
        # One cached store, not two distinct opens.
        assert len(backend._store_cache) == 1
        # Same instance both times.
        cached_keys = list(backend._store_cache.keys())
        store = backend._store_cache[cached_keys[0]]
        assert store is backend._get_or_open_store(*cached_keys[0])

    def test_aclose_closes_every_cached_store(self, tmp_path):
        """``aclose`` releases all cached stores so the worker can shut down clean."""
        import asyncio

        from heddle.contrib.rag.backends import VectorStoreBackend

        db_path = str(tmp_path / "test.duckdb")
        backend = VectorStoreBackend(db_path=db_path)
        backend.process_sync({"action": "stats"}, {"db_path": db_path})
        assert backend._store_cache  # populated

        asyncio.run(backend.aclose())
        assert backend._store_cache == {}


class TestVectorStoreDirectly:
    """Test DuckDBVectorStore directly (not through backend wrapper)."""

    def test_initialize_and_close(self, tmp_path):
        from heddle.contrib.rag.vectorstore.duckdb_store import DuckDBVectorStore

        store = DuckDBVectorStore(db_path=str(tmp_path / "test.duckdb"))
        store.initialize()
        assert store.count() == 0
        store.close()

    def test_add_and_get_embedded_chunks(self, tmp_path):
        from heddle.contrib.rag.schemas.embedding import EmbeddedChunk
        from heddle.contrib.rag.vectorstore.duckdb_store import DuckDBVectorStore

        store = DuckDBVectorStore(db_path=str(tmp_path / "test.duckdb"))
        store.initialize()

        ec = EmbeddedChunk(
            chunk_id="1:1:0",
            source_global_id="1:1",
            source_channel_id=1,
            text="test chunk",
            embedding=[0.1, 0.2, 0.3],
            model="test-model",
            dimensions=3,
        )
        count = store.add_embedded_chunks([ec])
        assert count == 1
        assert store.count() == 1

        retrieved = store.get("1:1:0")
        assert retrieved is not None
        assert retrieved.chunk_id == "1:1:0"
        assert retrieved.text == "test chunk"
        assert len(retrieved.embedding) == 3

        store.close()

    def test_delete_chunk(self, tmp_path):
        from heddle.contrib.rag.schemas.embedding import EmbeddedChunk
        from heddle.contrib.rag.vectorstore.duckdb_store import DuckDBVectorStore

        store = DuckDBVectorStore(db_path=str(tmp_path / "test.duckdb"))
        store.initialize()

        ec = EmbeddedChunk(
            chunk_id="1:1:0",
            source_global_id="1:1",
            source_channel_id=1,
            text="test",
            embedding=[0.1, 0.2],
            model="m",
            dimensions=2,
        )
        store.add_embedded_chunks([ec])
        assert store.delete("1:1:0") is True
        assert store.count() == 0
        assert store.delete("nonexistent") is False
        store.close()

    def test_delete_by_source(self, tmp_path):
        from heddle.contrib.rag.schemas.embedding import EmbeddedChunk
        from heddle.contrib.rag.vectorstore.duckdb_store import DuckDBVectorStore

        store = DuckDBVectorStore(db_path=str(tmp_path / "test.duckdb"))
        store.initialize()

        for i in range(3):
            ec = EmbeddedChunk(
                chunk_id=f"1:1:{i}",
                source_global_id="1:1",
                source_channel_id=1,
                text=f"chunk {i}",
                embedding=[0.1, 0.2],
                model="m",
                dimensions=2,
            )
            store.add_embedded_chunks([ec])

        deleted = store.delete_by_source("1:1")
        assert deleted == 3
        assert store.count() == 0
        store.close()

    def test_stats(self, tmp_path):
        from heddle.contrib.rag.schemas.embedding import EmbeddedChunk
        from heddle.contrib.rag.vectorstore.duckdb_store import DuckDBVectorStore

        store = DuckDBVectorStore(db_path=str(tmp_path / "test.duckdb"))
        store.initialize()

        for cid in [1, 2]:
            ec = EmbeddedChunk(
                chunk_id=f"{cid}:1:0",
                source_global_id=f"{cid}:1",
                source_channel_id=cid,
                text="text",
                embedding=[0.1],
                model="m",
                dimensions=1,
            )
            store.add_embedded_chunks([ec])

        stats = store.stats()
        assert stats["total_chunks"] == 2
        assert stats["unique_posts"] == 2
        assert stats["unique_channels"] == 2
        store.close()
