"""Tests for DuckDBVectorStore — embedded vector store for RAG chunks.

Uses in-memory DuckDB and pre-computed fake embeddings to avoid
requiring Ollama at test time.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from heddle.contrib.rag.schemas.chunk import ChunkStrategy, TextChunk
from heddle.contrib.rag.schemas.embedding import EmbeddedChunk, SimilarityResult
from heddle.contrib.rag.vectorstore.duckdb_store import DuckDBVectorStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path):
    """Return a path for a temporary DuckDB file."""
    return str(tmp_path / "test-vectors.duckdb")


@pytest.fixture
def store(tmp_db):
    """Create and initialize a DuckDBVectorStore."""
    s = DuckDBVectorStore(db_path=tmp_db)
    s.initialize()
    yield s
    s.close()


def _fake_embeddings(texts: list[str]) -> list[list[float]]:
    """Deterministic fake embeddings based on text length."""
    return [[float(len(t) % 10) / 10.0] * 8 for t in texts]


def _make_chunk(
    chunk_id: str = "c1",
    source_global_id: str = "post_1",
    source_channel_id: int = 100,
    text: str = "Test chunk text",
) -> TextChunk:
    return TextChunk(
        chunk_id=chunk_id,
        source_global_id=source_global_id,
        source_channel_id=source_channel_id,
        source_channel_name="TestChannel",
        text=text,
        char_start=0,
        char_end=len(text),
        chunk_index=0,
        total_chunks=1,
        strategy=ChunkStrategy.SENTENCE,
        timestamp_unix=1705320000,
    )


def _make_embedded_chunk(
    chunk_id: str = "ec1",
    text: str = "Embedded chunk",
    embedding: list[float] | None = None,
) -> EmbeddedChunk:
    return EmbeddedChunk(
        chunk_id=chunk_id,
        source_global_id="post_1",
        source_channel_id=100,
        text=text,
        embedding=embedding or [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8],
        model="test-model",
        dimensions=8,
    )


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


class TestInitialization:
    def test_initialize_creates_table(self, store):
        count = store.count()
        assert count == 0

    def test_initialize_returns_self(self, tmp_db):
        s = DuckDBVectorStore(db_path=tmp_db)
        result = s.initialize()
        assert result is s
        s.close()

    def test_close_twice(self, store):
        store.close()
        store.close()  # Should not raise


# ---------------------------------------------------------------------------
# Insert
# ---------------------------------------------------------------------------


class TestInsert:
    def test_add_embedded_chunks(self, store):
        chunks = [_make_embedded_chunk("ec1"), _make_embedded_chunk("ec2", text="other")]
        inserted = store.add_embedded_chunks(chunks)
        assert inserted == 2
        assert store.count() == 2

    def test_add_embedded_chunks_empty(self, store):
        assert store.add_embedded_chunks([]) == 0

    @patch(
        "heddle.contrib.rag.vectorstore.duckdb_store.DuckDBVectorStore._embed_texts",
        side_effect=_fake_embeddings,
    )
    def test_add_chunks(self, mock_embed, store):
        chunks = [_make_chunk("c1", text="hello"), _make_chunk("c2", text="world")]
        inserted = store.add_chunks(chunks)
        assert inserted == 2
        assert store.count() == 2

    def test_add_chunks_empty(self, store):
        assert store.add_chunks([]) == 0

    @patch(
        "heddle.contrib.rag.vectorstore.duckdb_store.DuckDBVectorStore._embed_texts",
        side_effect=RuntimeError("Ollama down"),
    )
    def test_add_chunks_embedding_failure(self, mock_embed, store):
        chunks = [_make_chunk("c1")]
        inserted = store.add_chunks(chunks)
        assert inserted == 0

    def test_add_embedded_chunk_upsert(self, store):
        """INSERT OR REPLACE should update existing chunks."""
        store.add_embedded_chunks([_make_embedded_chunk("ec1", text="first")])
        store.add_embedded_chunks([_make_embedded_chunk("ec1", text="second")])
        assert store.count() == 1
        chunk = store.get("ec1")
        assert chunk.text == "second"


# ---------------------------------------------------------------------------
# Get / Delete
# ---------------------------------------------------------------------------


class TestGetDelete:
    def test_get_existing(self, store):
        store.add_embedded_chunks([_make_embedded_chunk("ec1")])
        chunk = store.get("ec1")
        assert chunk is not None
        assert chunk.chunk_id == "ec1"
        assert chunk.text == "Embedded chunk"
        assert len(chunk.embedding) == 8

    def test_get_nonexistent(self, store):
        assert store.get("nonexistent") is None

    def test_get_returns_full_chunk_with_provenance(self, store):
        """Pin C5a: ``get`` returns every stored column, not a truncated subset.

        Earlier ``get`` selected only ``chunk_id``, ``source_global_id``,
        ``source_channel_id``, ``text``, ``embedding``, ``model``, and
        ``dimensions`` — silently dropping ``source_channel_name``,
        ``chunk_index``, ``total_chunks``, ``strategy``,
        ``timestamp_unix``, and ``embedded_at``.  Consumers that round-
        tripped a chunk through ``get`` saw schema defaults.
        """
        chunk = TextChunk(
            chunk_id="full_chunk",
            source_global_id="post_42",
            source_channel_id=999,
            source_channel_name="MyChannel",
            text="Round-trip provenance check.",
            char_start=10,
            char_end=37,
            chunk_index=3,
            total_chunks=7,
            strategy=ChunkStrategy.SENTENCE,
            timestamp_unix=1740000000,
        )

        # Patch the embedder at the store-level (``_get_embedder``)
        # to avoid the Ollama dependency in unit tests.
        class _FakeEmbedder:
            def embed_batch_sync(self, texts):
                return _fake_embeddings(list(texts))

        with patch.object(store, "_get_embedder", return_value=_FakeEmbedder()):
            store.add_chunks([chunk])

        got = store.get("full_chunk")
        assert got is not None
        assert got.chunk_id == "full_chunk"
        assert got.source_global_id == "post_42"
        # The dropped-then-restored fields all come back populated.
        assert got.source_channel_name == "MyChannel"
        assert got.chunk_index == 3
        assert got.total_chunks == 7
        assert got.strategy == "sentence"
        assert got.timestamp_unix == 1740000000
        assert got.embedded_at is not None

    def test_delete_existing(self, store):
        store.add_embedded_chunks([_make_embedded_chunk("ec1")])
        assert store.delete("ec1") is True
        assert store.count() == 0

    def test_delete_nonexistent(self, store):
        assert store.delete("nonexistent") is False

    def test_delete_by_source(self, store):
        chunks = [
            _make_embedded_chunk("ec1"),
            _make_embedded_chunk("ec2"),
        ]
        # Both have source_global_id="post_1"
        store.add_embedded_chunks(chunks)
        deleted = store.delete_by_source("post_1")
        assert deleted == 2
        assert store.count() == 0

    def test_delete_by_source_nonexistent(self, store):
        assert store.delete_by_source("nonexistent") == 0


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSearch:
    @patch(
        "heddle.contrib.rag.vectorstore.duckdb_store.DuckDBVectorStore._embed_texts",
    )
    def test_search_returns_results(self, mock_embed, store):
        # Insert chunks with known embeddings
        store.add_embedded_chunks(
            [
                _make_embedded_chunk(
                    "ec1",
                    text="earthquake damage report",
                    embedding=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                ),
                _make_embedded_chunk(
                    "ec2",
                    text="weather forecast",
                    embedding=[0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                ),
            ]
        )
        # Query embedding similar to ec1
        mock_embed.return_value = [[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]]
        results = store.search("earthquake", limit=2)
        assert len(results) >= 1
        assert results[0].chunk_id == "ec1"
        assert isinstance(results[0], SimilarityResult)

    @patch(
        "heddle.contrib.rag.vectorstore.duckdb_store.DuckDBVectorStore._embed_texts",
        return_value=[],
    )
    def test_search_no_embeddings(self, mock_embed, store):
        results = store.search("query")
        assert results == []

    @patch(
        "heddle.contrib.rag.vectorstore.duckdb_store.DuckDBVectorStore._embed_texts",
    )
    def test_search_min_score_filter(self, mock_embed, store):
        store.add_embedded_chunks(
            [
                _make_embedded_chunk("ec1", embedding=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
            ]
        )
        mock_embed.return_value = [[0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]]
        results = store.search("unrelated", min_score=0.99)
        assert len(results) == 0

    @patch(
        "heddle.contrib.rag.vectorstore.duckdb_store.DuckDBVectorStore._embed_texts",
    )
    def test_search_channel_filter(self, mock_embed, store):
        store.add_embedded_chunks(
            [
                EmbeddedChunk(
                    chunk_id="ec1",
                    source_global_id="p1",
                    source_channel_id=100,
                    text="a",
                    embedding=[1.0] * 8,
                    model="m",
                    dimensions=8,
                ),
                EmbeddedChunk(
                    chunk_id="ec2",
                    source_global_id="p2",
                    source_channel_id=200,
                    text="b",
                    embedding=[1.0] * 8,
                    model="m",
                    dimensions=8,
                ),
            ]
        )
        mock_embed.return_value = [[1.0] * 8]
        results = store.search("query", channel_ids=[100])
        assert all(r.source_channel_id == 100 for r in results)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestStats:
    def test_stats_empty(self, store):
        stats = store.stats()
        assert stats == {"total_chunks": 0}

    def test_stats_with_data(self, store):
        store.add_embedded_chunks(
            [
                _make_embedded_chunk("ec1"),
                _make_embedded_chunk("ec2"),
            ]
        )
        stats = store.stats()
        assert stats["total_chunks"] == 2
        assert stats["unique_posts"] == 1
        assert stats["unique_channels"] == 1
        assert "db_path" in stats
