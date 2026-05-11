"""Tests for DuckDBQueryBackend — generic action-dispatch query backend."""

import duckdb
import pytest

from heddle.contrib.duckdb import DuckDBQueryBackend, DuckDBQueryError


@pytest.fixture
def db_path(tmp_path):
    """Create a DuckDB database with a generic test table."""
    path = str(tmp_path / "test.duckdb")
    conn = duckdb.connect(path)

    conn.execute("""
        CREATE TABLE items (
            id VARCHAR PRIMARY KEY,
            name VARCHAR NOT NULL,
            category VARCHAR,
            description TEXT,
            full_text TEXT,
            score DOUBLE,
            active BOOLEAN,
            metadata JSON,
            embedding FLOAT[],
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        INSERT INTO items
            (id, name, category, description, full_text,
             score, active, metadata, embedding)
        VALUES
            ('i1', 'Widget A', 'tools', 'A useful widget',
             'Full text about widgets and building',
             0.95, true, '{"tags":["essential"]}',
             [0.9, 0.1, 0.0]),
            ('i2', 'Gadget B', 'electronics',
             'An electronic gadget',
             'Full text about gadgets and circuits',
             0.88, true, '{"tags":["popular"]}',
             [0.0, 0.9, 0.1]),
            ('i3', 'Tool C', 'tools', 'A power tool',
             'Full text about tools and construction',
             0.72, false, '{"tags":["heavy"]}', NULL)
    """)

    # Set up FTS.
    conn.execute("INSTALL fts")
    conn.execute("LOAD fts")
    try:
        conn.execute("""
            PRAGMA create_fts_index('items', 'id', 'full_text', 'description', overwrite=1)
        """)
    except duckdb.Error:
        pass

    conn.close()
    return path


@pytest.fixture
def backend(db_path):
    """Create a configured DuckDBQueryBackend for the test table."""
    return DuckDBQueryBackend(
        db_path=db_path,
        table_name="items",
        result_columns=[
            "id",
            "name",
            "category",
            "description",
            "score",
            "active",
            "metadata",
            "created_at",
        ],
        json_columns={"metadata"},
        id_column="id",
        full_text_column="full_text",
        fts_fields="full_text,description",
        filter_fields={
            "category": "category = ?",
            "active": "active = ?",
            "min_score": "score >= ?",
        },
        stats_groups={"category", "active"},
        stats_aggregates=["COUNT(*) AS item_count", "ROUND(AVG(score), 2) AS avg_score"],
        default_order_by="created_at DESC",
    )


@pytest.fixture
def config(db_path):
    """Worker config dict."""
    return {"db_path": db_path}


class TestValidation:
    """Tests for error handling and dispatch."""

    def test_error_hierarchy(self):
        from heddle.worker.processor import BackendError

        assert issubclass(DuckDBQueryError, BackendError)

    def test_result_columns_rejects_unsafe_identifier(self, db_path):
        """E2: each ``result_columns`` entry is validated at construction.

        Earlier ``result_columns`` was interpolated as
        ``f"d.{c}"`` / ``", ".join(...)`` into every SELECT, so a
        relaxed schema or a future caller bypassing the validator
        could smuggle SQL through an unquoted slot.  Now every entry
        must match the strict identifier regex.
        """
        with pytest.raises(ValueError, match="result_columns item"):
            DuckDBQueryBackend(
                db_path=db_path,
                table_name="records",
                id_column="id",
                full_text_column="full_text",
                fts_fields="full_text",
                result_columns=["id", "title; DROP TABLE records --"],
            )

    def test_unknown_action_raises(self, backend, config):
        with pytest.raises(ValueError, match="Unknown action"):
            backend.process_sync({"action": "invalid"}, config)


class TestSearch:
    """Tests for full-text search action."""

    def test_search_finds_matching_records(self, backend, config):
        result = backend.process_sync({"action": "search", "query": "widget"}, config)
        results = result["output"]["results"]
        assert len(results) >= 1
        names = [r["name"] for r in results]
        assert "Widget A" in names

    def test_search_empty_query(self, backend, config):
        result = backend.process_sync({"action": "search", "query": ""}, config)
        assert result["output"]["results"] == []
        assert result["output"]["total"] == 0

    def test_search_respects_limit(self, backend, config):
        result = backend.process_sync({"action": "search", "query": "text", "limit": 1}, config)
        assert len(result["output"]["results"]) <= 1

    def test_search_excludes_full_text(self, backend, config):
        result = backend.process_sync({"action": "search", "query": "widget"}, config)
        for r in result["output"]["results"]:
            assert "full_text" not in r

    def test_model_used_is_duckdb(self, backend, config):
        result = backend.process_sync({"action": "search", "query": "test"}, config)
        assert result["model_used"] == "duckdb"


class TestFilter:
    """Tests for attribute filtering action."""

    def test_filter_by_category(self, backend, config):
        result = backend.process_sync({"action": "filter", "category": "tools"}, config)
        results = result["output"]["results"]
        assert len(results) >= 1
        assert all(r["category"] == "tools" for r in results)

    def test_filter_by_boolean(self, backend, config):
        result = backend.process_sync({"action": "filter", "active": True}, config)
        results = result["output"]["results"]
        assert len(results) >= 1
        assert all(r["active"] is True for r in results)

    def test_filter_by_range(self, backend, config):
        result = backend.process_sync({"action": "filter", "min_score": 0.9}, config)
        results = result["output"]["results"]
        assert len(results) >= 1
        assert all(r["score"] >= 0.9 for r in results)

    def test_filter_combined_criteria(self, backend, config):
        result = backend.process_sync(
            {"action": "filter", "category": "tools", "active": True}, config
        )
        results = result["output"]["results"]
        assert all(r["category"] == "tools" and r["active"] is True for r in results)

    def test_filter_no_criteria_returns_all(self, backend, config):
        result = backend.process_sync({"action": "filter"}, config)
        assert result["output"]["total"] == 3

    def test_filter_returns_total_count(self, backend, config):
        result = backend.process_sync({"action": "filter", "category": "tools"}, config)
        assert result["output"]["total"] >= 1


class TestStats:
    """Tests for aggregate statistics action."""

    def test_stats_by_category(self, backend, config):
        result = backend.process_sync({"action": "stats", "group_by": "category"}, config)
        results = result["output"]["results"]
        categories = {r["category"] for r in results}
        assert "tools" in categories
        assert "electronics" in categories

    def test_stats_includes_aggregates(self, backend, config):
        result = backend.process_sync({"action": "stats", "group_by": "category"}, config)
        for r in result["output"]["results"]:
            assert "item_count" in r
            assert "avg_score" in r

    def test_stats_by_boolean(self, backend, config):
        result = backend.process_sync({"action": "stats", "group_by": "active"}, config)
        assert len(result["output"]["results"]) >= 1

    def test_stats_invalid_group_raises(self, backend, config):
        with pytest.raises(ValueError, match="Invalid group_by"):
            backend.process_sync({"action": "stats", "group_by": "invalid_col"}, config)

    def test_stats_returns_total(self, backend, config):
        result = backend.process_sync({"action": "stats"}, config)
        assert result["output"]["total"] == 3


class TestGet:
    """Tests for single-record retrieval."""

    def test_get_by_id(self, backend, config):
        result = backend.process_sync({"action": "get", "id": "i1"}, config)
        doc = result["output"]["document"]
        assert doc["id"] == "i1"
        assert doc["name"] == "Widget A"

    def test_get_includes_full_text(self, backend, config):
        result = backend.process_sync({"action": "get", "id": "i1"}, config)
        doc = result["output"]["document"]
        assert "full_text" in doc
        assert "widget" in doc["full_text"].lower()

    def test_get_parses_json_columns(self, backend, config):
        result = backend.process_sync({"action": "get", "id": "i1"}, config)
        doc = result["output"]["document"]
        assert isinstance(doc["metadata"], dict)
        assert doc["metadata"]["tags"] == ["essential"]

    def test_get_not_found_raises(self, backend, config):
        with pytest.raises(DuckDBQueryError, match="Record not found"):
            backend.process_sync({"action": "get", "id": "nonexistent"}, config)

    def test_get_missing_id_raises(self, backend, config):
        with pytest.raises(ValueError, match="required"):
            backend.process_sync({"action": "get"}, config)

    def test_get_backward_compat_document_id(self, backend, config):
        """The 'document_id' payload key works as a fallback."""
        result = backend.process_sync({"action": "get", "document_id": "i2"}, config)
        assert result["output"]["document"]["id"] == "i2"


class TestRowToDict:
    """Tests for _row_to_dict helper."""

    def test_parses_json_columns(self, backend):
        row = ("i1", "Widget A", '{"tags": ["a"]}')
        columns = ["id", "name", "metadata"]
        result = backend._row_to_dict(row, columns)
        assert isinstance(result["metadata"], dict)

    def test_handles_invalid_json(self, backend):
        row = ("i1", "Widget A", "not-json")
        columns = ["id", "name", "metadata"]
        result = backend._row_to_dict(row, columns)
        assert result["metadata"] == "not-json"

    def test_converts_datetime_to_string(self, backend):
        from datetime import datetime

        now = datetime.now()
        row = ("i1", "Widget A", now)
        columns = ["id", "name", "created_at"]
        result = backend._row_to_dict(row, columns)
        assert isinstance(result["created_at"], str)


class TestCustomHandlers:
    """Tests for _get_handlers extensibility."""

    def test_get_handlers_returns_all_actions(self, backend):
        handlers = backend._get_handlers()
        assert "search" in handlers
        assert "filter" in handlers
        assert "stats" in handlers
        assert "get" in handlers
        assert "vector_search" in handlers

    def test_subclass_can_add_handlers(self, db_path):
        """Subclasses can add custom action handlers."""

        class ExtendedBackend(DuckDBQueryBackend):
            def _get_handlers(self):
                handlers = super()._get_handlers()
                handlers["custom"] = self._custom_action
                return handlers

            def _custom_action(self, conn, payload):
                return {"custom": True}

        backend = ExtendedBackend(
            db_path=db_path,
            table_name="items",
            result_columns=["id", "name"],
        )
        result = backend.process_sync({"action": "custom"}, {"db_path": db_path})
        assert result["output"]["custom"] is True


class TestConfigOverride:
    """Tests for config-driven behavior."""

    def test_db_path_from_config(self, db_path):
        """db_path in config overrides constructor default."""
        backend = DuckDBQueryBackend(
            db_path="/nonexistent/path.duckdb",
            table_name="items",
            result_columns=["id", "name"],
        )
        # Should work because config provides the correct path
        result = backend.process_sync(
            {"action": "filter"},
            {"db_path": db_path},
        )
        assert result["output"]["total"] == 3


# ---------------------------------------------------------------------------
# Coverage: process_sync generic exception wrapping (lines 165-166)
# ---------------------------------------------------------------------------


class TestProcessSyncExceptionWrapping:
    """Tests for the generic exception → DuckDBQueryError wrapping."""

    def test_generic_exception_wrapped_as_query_error(self, db_path):
        """Non-ValueError/non-DuckDBQueryError exceptions are wrapped."""

        class BrokenBackend(DuckDBQueryBackend):
            def _get_handlers(self):
                return {"boom": self._boom}

            def _boom(self, conn, payload):
                raise RuntimeError("unexpected failure")

        backend = BrokenBackend(
            db_path=db_path,
            table_name="items",
            result_columns=["id", "name"],
        )
        with pytest.raises(DuckDBQueryError, match=r"Query failed.*unexpected failure"):
            backend.process_sync({"action": "boom"}, {"db_path": db_path})

    def test_value_error_not_wrapped(self, db_path):
        """ValueError raised inside handler propagates as-is."""

        class ValErrBackend(DuckDBQueryBackend):
            def _get_handlers(self):
                return {"bad": self._bad}

            def _bad(self, conn, payload):
                raise ValueError("bad input")

        backend = ValErrBackend(
            db_path=db_path,
            table_name="items",
            result_columns=["id", "name"],
        )
        with pytest.raises(ValueError, match="bad input"):
            backend.process_sync({"action": "bad"}, {"db_path": db_path})


# ---------------------------------------------------------------------------
# Coverage: FTS fallback to ILIKE search (lines 207-213)
# ---------------------------------------------------------------------------


class TestSearchFTSFallback:
    """Tests for ILIKE fallback when FTS index is not available."""

    def test_search_falls_back_to_ilike(self, tmp_path):
        """When no FTS index exists, search uses ILIKE fallback."""
        path = str(tmp_path / "no_fts.duckdb")
        conn = duckdb.connect(path)
        conn.execute("""
            CREATE TABLE docs (
                id VARCHAR PRIMARY KEY,
                title VARCHAR,
                content TEXT
            )
        """)
        conn.execute("""
            INSERT INTO docs (id, title, content) VALUES
                ('d1', 'Alpha Report', 'Content about alpha testing'),
                ('d2', 'Beta Summary', 'Content about beta releases')
        """)
        conn.close()

        backend = DuckDBQueryBackend(
            db_path=path,
            table_name="docs",
            result_columns=["id", "title"],
            full_text_column="content",
            fts_fields="title,content",
        )

        # No FTS index created, so match_bm25 will fail → ILIKE fallback
        result = backend.process_sync(
            {"action": "search", "query": "alpha"},
            {"db_path": path},
        )
        results = result["output"]["results"]
        assert len(results) >= 1
        ids = [r["id"] for r in results]
        assert "d1" in ids


# ---------------------------------------------------------------------------
# Coverage: stats with no stats_groups (line 276)
# ---------------------------------------------------------------------------


class TestStatsNoGroups:
    """Test stats raises ValueError when no stats_groups configured."""

    def test_stats_no_groups_raises(self, db_path):
        backend = DuckDBQueryBackend(
            db_path=db_path,
            table_name="items",
            result_columns=["id", "name"],
            stats_groups=set(),
        )
        with pytest.raises(ValueError, match="No stats_groups configured"):
            backend.process_sync({"action": "stats"}, {"db_path": db_path})


# ---------------------------------------------------------------------------
# Coverage: _get with full_text_column not in result_columns (line 321->324)
# ---------------------------------------------------------------------------


class TestGetFullTextAppended:
    """Test _get appends full_text_column when not in result_columns."""

    def test_get_appends_full_text_column(self, db_path):
        # result_columns does NOT include 'full_text'
        backend = DuckDBQueryBackend(
            db_path=db_path,
            table_name="items",
            result_columns=["id", "name"],
            full_text_column="full_text",
        )
        result = backend.process_sync({"action": "get", "id": "i1"}, {"db_path": db_path})
        doc = result["output"]["document"]
        assert "full_text" in doc
        assert "widget" in doc["full_text"].lower()

    def test_get_no_full_text_column(self, db_path):
        """When full_text_column is None, it is not appended."""
        backend = DuckDBQueryBackend(
            db_path=db_path,
            table_name="items",
            result_columns=["id", "name"],
            full_text_column=None,
        )
        result = backend.process_sync({"action": "get", "id": "i1"}, {"db_path": db_path})
        doc = result["output"]["document"]
        assert "full_text" not in doc


# ---------------------------------------------------------------------------
# Coverage: _vector_search (lines 348-384)
# ---------------------------------------------------------------------------


class TestVectorSearch:
    """Tests for vector_search action."""

    def test_vector_search_empty_query(self, backend, config):
        result = backend.process_sync(
            {"action": "vector_search", "query": ""},
            config,
        )
        assert result["output"]["results"] == []
        assert result["output"]["total"] == 0

    def test_vector_search_whitespace_query(self, backend, config):
        result = backend.process_sync(
            {"action": "vector_search", "query": "   "},
            config,
        )
        assert result["output"]["results"] == []

    def test_vector_search_embedding_failure(self, backend, config, monkeypatch):
        """When embedding generation fails, DuckDBQueryError is raised."""

        class FakeProvider:
            def __init__(self, **kw):
                pass

            def embed_sync(self, text):
                raise RuntimeError("Ollama unreachable")

        monkeypatch.setattr(
            "heddle.worker.embeddings.OllamaEmbeddingProvider",
            FakeProvider,
        )

        with pytest.raises(DuckDBQueryError, match="Failed to generate query embedding"):
            backend.process_sync(
                {"action": "vector_search", "query": "test query"},
                config,
            )

    def test_vector_search_success(self, backend, config, monkeypatch):
        """Successful vector search with mocked embedding provider."""

        class FakeProvider:
            def __init__(self, **kw):
                pass

            def embed_sync(self, text):
                return [0.8, 0.2, 0.0]

        monkeypatch.setattr(
            "heddle.worker.embeddings.OllamaEmbeddingProvider",
            FakeProvider,
        )

        result = backend.process_sync(
            {"action": "vector_search", "query": "widgets", "limit": 2},
            config,
        )
        results = result["output"]["results"]
        # i1 has embedding [0.9, 0.1, 0.0] — closest to [0.8, 0.2, 0.0]
        # i2 has embedding [0.0, 0.9, 0.1]
        # i3 has NULL embedding — excluded
        assert len(results) <= 2
        assert all("similarity" in r for r in results)
        # Most similar should be first (cosine similarity ordering)
        if len(results) >= 2:
            assert results[0]["similarity"] >= results[1]["similarity"]
