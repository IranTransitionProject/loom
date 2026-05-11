"""Unit tests for heddle.contrib.lancedb.store — LanceDB vector store."""

import pytest


def _can_import_lancedb():
    try:
        import lancedb  # noqa: F401

        return True
    except ImportError:
        return False


@pytest.fixture
def lance_db_path(tmp_path):
    """Provide a temporary path for a LanceDB database."""
    return str(tmp_path / "test-vectors.lance")


def _mock_embed_batch(texts):
    """Return deterministic fake embeddings."""
    return [[float(i) / 10.0] * 8 for i in range(len(texts))]


def _make_text_chunk(chunk_id="c1", source_global_id="99:1", channel_id=99, text="test text"):
    """Create a TextChunk for testing."""
    from heddle.contrib.rag.schemas.chunk import TextChunk

    return TextChunk(
        chunk_id=chunk_id,
        source_global_id=source_global_id,
        source_channel_id=channel_id,
        source_channel_name="test_channel",
        text=text,
        char_start=0,
        char_end=len(text),
        chunk_index=0,
        total_chunks=1,
        timestamp_unix=1740826800,
    )


def _make_embedded_chunk(chunk_id="ec1", source_global_id="99:1", channel_id=99):
    """Create an EmbeddedChunk for testing."""
    from heddle.contrib.rag.schemas.embedding import EmbeddedChunk

    return EmbeddedChunk(
        chunk_id=chunk_id,
        source_global_id=source_global_id,
        source_channel_id=channel_id,
        text="pre-embedded test text",
        embedding=[0.1] * 8,
        model="test-model",
        dimensions=8,
    )


class TestLanceDBVectorStoreInterface:
    """Test that LanceDBVectorStore implements the VectorStore ABC."""

    def test_extends_vector_store(self):
        from heddle.contrib.lancedb.store import LanceDBVectorStore
        from heddle.contrib.rag.vectorstore.base import VectorStore

        assert issubclass(LanceDBVectorStore, VectorStore)

    @pytest.mark.skipif(
        not _can_import_lancedb(),
        reason="lancedb not installed",
    )
    def test_initialize_creates_db(self, lance_db_path):
        from heddle.contrib.lancedb.store import LanceDBVectorStore

        store = LanceDBVectorStore(db_path=lance_db_path)
        store.initialize()
        assert store.count() == 0
        store.close()

    @pytest.mark.skipif(
        not _can_import_lancedb(),
        reason="lancedb not installed",
    )
    def test_empty_stats(self, lance_db_path):
        from heddle.contrib.lancedb.store import LanceDBVectorStore

        store = LanceDBVectorStore(db_path=lance_db_path).initialize()
        stats = store.stats()
        assert stats["total_chunks"] == 0
        store.close()

    @pytest.mark.skipif(
        not _can_import_lancedb(),
        reason="lancedb not installed",
    )
    def test_add_embedded_chunks(self, lance_db_path):
        from heddle.contrib.lancedb.store import LanceDBVectorStore

        store = LanceDBVectorStore(db_path=lance_db_path).initialize()
        ec = _make_embedded_chunk()
        count = store.add_embedded_chunks([ec])
        assert count == 1
        assert store.count() == 1
        store.close()

    @pytest.mark.skipif(
        not _can_import_lancedb(),
        reason="lancedb not installed",
    )
    def test_get_chunk(self, lance_db_path):
        from heddle.contrib.lancedb.store import LanceDBVectorStore

        store = LanceDBVectorStore(db_path=lance_db_path).initialize()
        ec = _make_embedded_chunk(chunk_id="get-test")
        store.add_embedded_chunks([ec])

        result = store.get("get-test")
        assert result is not None
        assert result.chunk_id == "get-test"
        assert result.text == "pre-embedded test text"

        assert store.get("nonexistent") is None
        store.close()

    @pytest.mark.skipif(
        not _can_import_lancedb(),
        reason="lancedb not installed",
    )
    def test_delete_chunk(self, lance_db_path):
        from heddle.contrib.lancedb.store import LanceDBVectorStore

        store = LanceDBVectorStore(db_path=lance_db_path).initialize()
        ec = _make_embedded_chunk(chunk_id="del-test")
        store.add_embedded_chunks([ec])
        assert store.count() == 1

        assert store.delete("del-test") is True
        assert store.count() == 0
        assert store.delete("del-test") is False
        store.close()

    @pytest.mark.skipif(
        not _can_import_lancedb(),
        reason="lancedb not installed",
    )
    def test_delete_by_source(self, lance_db_path):
        from heddle.contrib.lancedb.store import LanceDBVectorStore

        store = LanceDBVectorStore(db_path=lance_db_path).initialize()
        ec1 = _make_embedded_chunk(chunk_id="s1-c1", source_global_id="99:1")
        ec2 = _make_embedded_chunk(chunk_id="s1-c2", source_global_id="99:1")
        ec3 = _make_embedded_chunk(chunk_id="s2-c1", source_global_id="99:2")
        store.add_embedded_chunks([ec1, ec2, ec3])
        assert store.count() == 3

        deleted = store.delete_by_source("99:1")
        assert deleted == 2
        assert store.count() == 1
        store.close()


class TestLanceDBVectorStoreImport:
    """Test import/export without requiring lancedb installed."""

    def test_import_path(self):
        """Verify the module can be imported (class definition only)."""
        # This just checks the module structure is valid Python
        from heddle.contrib.lancedb import store  # noqa: F401

    def test_tool_import(self):
        from heddle.contrib.lancedb import tool  # noqa: F401


# ---------------------------------------------------------------------------
# Closed-store branches: every public method must short-circuit cleanly when
# the underlying table is None (typically after close(), or before
# initialize() if the user constructs the store and never opens it).  None
# of these tests need lancedb installed because they never call initialize().
# ---------------------------------------------------------------------------


class TestLanceDBVectorStoreClosed:
    """Public methods must handle a None table gracefully."""

    def _closed_store(self):
        from heddle.contrib.lancedb.store import LanceDBVectorStore

        store = LanceDBVectorStore(db_path="/nonexistent/path")
        # No initialize() — _table stays None.
        return store

    def test_count_returns_zero(self):
        assert self._closed_store().count() == 0

    def test_get_returns_none(self):
        assert self._closed_store().get("anything") is None

    def test_delete_returns_false(self):
        assert self._closed_store().delete("anything") is False

    def test_delete_by_source_returns_zero(self):
        assert self._closed_store().delete_by_source("99:1") == 0

    def test_stats_reports_zero(self):
        stats = self._closed_store().stats()
        assert stats == {"total_chunks": 0}

    def test_add_embedded_chunks_empty_input(self):
        # Empty list is a fast path that never touches the table.
        assert self._closed_store().add_embedded_chunks([]) == 0


# ---------------------------------------------------------------------------
# Additional store branches that DO need lancedb installed.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _can_import_lancedb(), reason="lancedb not installed")
class TestLanceDBVectorStoreReopen:
    """Cover branches that only fire on a pre-populated DB.

    NOTE: a reopen-with-persistence test was tried here and failed —
    after first.close() + second.initialize() on the same db_path,
    count() returned 0 instead of the pre-close 1. This looks like a
    real persistence question in heddle.contrib.lancedb.store (or in
    how close() interacts with lancedb's connection lifecycle), not
    a test bug. Filed for a separate fix-then-test session; not
    bundled here per H session-starter guidance.
    """

    def test_stats_with_rows(self, lance_db_path):
        """stats() pandas-aggregate path when the table has rows."""
        from heddle.contrib.lancedb.store import LanceDBVectorStore

        store = LanceDBVectorStore(db_path=lance_db_path).initialize()
        store.add_embedded_chunks(
            [
                _make_embedded_chunk(chunk_id="a", source_global_id="1:1", channel_id=1),
                _make_embedded_chunk(chunk_id="b", source_global_id="1:2", channel_id=1),
                _make_embedded_chunk(chunk_id="c", source_global_id="2:1", channel_id=2),
            ]
        )

        stats = store.stats()
        assert stats["total_chunks"] == 3
        assert stats["unique_posts"] == 3
        assert stats["unique_channels"] == 2
        assert stats["db_path"] == str(lance_db_path)
        store.close()


# ---------------------------------------------------------------------------
# LanceDBVectorTool — tool definition surface (does not require lancedb).
# execute_sync / _embed_query are pragma-no-cover (they hit ANN search and
# Ollama), but the LLM-facing schema must always be testable.
# ---------------------------------------------------------------------------


class TestLanceDBVectorToolDefinition:
    """Cover __init__ defaults and get_definition payload."""

    def test_init_defaults(self):
        from heddle.contrib.lancedb.tool import LanceDBVectorTool

        tool = LanceDBVectorTool(db_path="/tmp/x.lance")
        assert tool.db_path == "/tmp/x.lance"
        assert tool.table_name == "rag_chunks"
        assert tool.vector_column == "vector"
        assert tool.tool_name == "find_similar"
        assert tool.embedding_model == "nomic-embed-text"
        assert tool.ollama_url is None
        assert tool.max_results == 10
        assert tool._result_columns == [
            "chunk_id",
            "text",
            "source_channel_id",
            "source_global_id",
        ]

    def test_init_overrides(self):
        from heddle.contrib.lancedb.tool import LanceDBVectorTool

        tool = LanceDBVectorTool(
            db_path="/tmp/y.lance",
            table_name="custom_chunks",
            vector_column="emb",
            result_columns=["chunk_id", "text"],
            tool_name="search_corpus",
            description="custom desc",
            embedding_model="all-minilm",
            ollama_url="http://other:11434",
            max_results=25,
        )
        assert tool.table_name == "custom_chunks"
        assert tool.vector_column == "emb"
        assert tool.tool_name == "search_corpus"
        assert tool.embedding_model == "all-minilm"
        assert tool.ollama_url == "http://other:11434"
        assert tool.max_results == 25
        assert tool._result_columns == ["chunk_id", "text"]

    def test_get_definition_schema(self):
        from heddle.contrib.lancedb.tool import LanceDBVectorTool

        tool = LanceDBVectorTool(
            db_path="/tmp/z.lance",
            tool_name="find_alike",
            description="locate similar posts",
            max_results=7,
        )
        defn = tool.get_definition()

        assert defn["name"] == "find_alike"
        assert defn["description"] == "locate similar posts"
        params = defn["parameters"]
        assert params["type"] == "object"
        assert params["required"] == ["query"]
        assert "query" in params["properties"]
        assert params["properties"]["query"]["type"] == "string"
        # max_results from constructor surfaces in the limit's description.
        assert "max: 7" in params["properties"]["limit"]["description"]


class TestChannelIdFilter:
    """E2: ``_build_channel_id_filter`` rejects non-integer ids.

    Earlier the search method interpolated ``channel_ids`` into a
    LanceDB filter expression directly.  The shipped YAML schema
    constrains the field to integers, but a relaxed schema or a
    new caller bypassing the validator could splice a filter
    fragment through that slot.  The helper now casts every entry
    to ``int`` before interpolation and raises on anything else.
    """

    def test_int_ids_produce_or_clause(self):
        from heddle.contrib.lancedb.store import _build_channel_id_filter

        assert (
            _build_channel_id_filter([1, 2, 3])
            == "source_channel_id = 1 OR source_channel_id = 2 OR source_channel_id = 3"
        )

    def test_int_like_strings_are_cast(self):
        from heddle.contrib.lancedb.store import _build_channel_id_filter

        # int(str(n)) round-trips digit-only strings — the shipped
        # YAML produces ints so this case never hits in practice,
        # but the cast is permissive enough not to break it.
        assert _build_channel_id_filter(["42"]) == "source_channel_id = 42"

    def test_non_integer_raises(self):
        import pytest

        from heddle.contrib.lancedb.store import _build_channel_id_filter

        with pytest.raises(ValueError, match="channel_ids must be integers"):
            _build_channel_id_filter([1, "DROP TABLE x"])

    def test_none_value_raises(self):
        import pytest

        from heddle.contrib.lancedb.store import _build_channel_id_filter

        with pytest.raises(ValueError, match="channel_ids must be integers"):
            _build_channel_id_filter([1, None])
