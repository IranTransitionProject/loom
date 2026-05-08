"""
Generic DuckDB query and analytics backend for Heddle workflows.

Provides a configurable action-dispatch query backend against any DuckDB
table. Supports full-text search (via DuckDB FTS), attribute filtering,
aggregate statistics, single-record retrieval, and vector similarity search.

Subclasses configure domain-specific behavior by passing constructor
parameters (table name, columns, filter definitions, etc.) rather than
overriding methods. For advanced customisation, override ``_get_handlers``
to add or replace action handlers.

Example worker config::

    processing_backend: "myapp.backends.MyQueryBackend"
    backend_config:
      db_path: "/tmp/workspace/data.duckdb"

See Also:
    heddle.worker.processor.SyncProcessingBackend -- base class for sync backends
    heddle.contrib.duckdb.DuckDBViewTool -- LLM-callable view tool
    heddle.contrib.duckdb.DuckDBVectorTool -- LLM-callable vector search tool
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb
import structlog

from heddle.worker.processor import BackendError, SyncProcessingBackend

if TYPE_CHECKING:
    from collections.abc import Callable

logger = structlog.get_logger()


class DuckDBQueryError(BackendError):
    """Raised when a DuckDB query operation fails.

    Wraps underlying DuckDB exceptions with a descriptive message
    and the original cause attached via ``__cause__``.
    """


class DuckDBQueryBackend(SyncProcessingBackend):
    """Generic action-dispatch query backend for DuckDB tables.

    Opens a read-only connection to the DuckDB database and dispatches
    to the appropriate query handler based on the ``action`` field in
    the payload.

    All queries use parameterized statements to prevent SQL injection.
    Results from search/filter actions exclude large content columns
    (configurable via ``full_text_column``) to keep messages small.

    Args:
        db_path: Path to the DuckDB database file.
        table_name: Table to query.
        result_columns: Columns returned in search/filter results.
        json_columns: Set of column names containing JSON strings
            that should be parsed back into Python objects on read.
        id_column: Primary key column name for the ``get`` action.
        full_text_column: Large content column included only in
            ``get`` results. Set to None if no such column exists.
        fts_fields: Comma-separated field names for DuckDB FTS
            ``match_bm25`` calls (e.g. ``"content,summary"``).
        filter_fields: Mapping of payload field names to SQL condition
            templates. Example: ``{"min_pages": "page_count >= ?"}``.
            Each key is checked in the payload; if present, its SQL
            template is added to the WHERE clause.
        stats_groups: Set of column names allowed as ``group_by``
            values for the ``stats`` action.
        stats_aggregates: SQL aggregate expressions for the stats
            query. Defaults to ``["COUNT(*) AS record_count"]``.
        default_order_by: ORDER BY clause for filter results.
        embedding_column: Column name for vector embeddings used
            in the ``vector_search`` action.
    """

    def __init__(
        self,
        db_path: str = "/tmp/workspace/data.duckdb",
        *,
        table_name: str = "documents",
        result_columns: list[str] | None = None,
        json_columns: set[str] | None = None,
        id_column: str = "id",
        full_text_column: str | None = "full_text",
        fts_fields: str = "full_text,summary",
        filter_fields: dict[str, str] | None = None,
        stats_groups: set[str] | None = None,
        stats_aggregates: list[str] | None = None,
        default_order_by: str = "rowid",
        embedding_column: str = "embedding",
    ) -> None:
        from heddle.contrib._sql_security import (
            validate_sql_identifier,
            validate_sql_identifier_list,
        )

        self.db_path = Path(db_path)
        # Validate identifier-shaped fields at construction so a typo
        # in YAML (or worse, a crafted value) fails fast rather than
        # silently corrupting an interpolated query.  ``stats_aggregates``
        # and ``default_order_by`` are intentionally free-form SQL
        # (callers ship things like ``"COUNT(*) AS n"``); we don't
        # validate those — they're a documented power-user surface.
        self.table_name = validate_sql_identifier(table_name, field="table_name")
        self.id_column = validate_sql_identifier(id_column, field="id_column")
        self.embedding_column = validate_sql_identifier(
            embedding_column, field="embedding_column"
        )
        if full_text_column is not None:
            self.full_text_column = validate_sql_identifier(
                full_text_column, field="full_text_column"
            )
        else:
            self.full_text_column = None
        # ``fts_fields`` is comma-separated, e.g. "full_text,summary".
        # Each name must be a valid identifier.  We re-join after
        # validation so the stored value is the canonical
        # whitespace-trimmed form.
        self.fts_fields = ",".join(
            validate_sql_identifier_list(fts_fields, field="fts_fields")
        )
        self.result_columns = result_columns or ["id"]
        self.json_columns = json_columns or set()
        self.filter_fields = filter_fields or {}
        self.stats_groups = stats_groups or set()
        self.stats_aggregates = stats_aggregates or ["COUNT(*) AS record_count"]
        self.default_order_by = default_order_by

    def _get_handlers(self) -> dict[str, Callable]:
        """Return action→handler mapping.

        Override in subclasses to add, remove, or replace action handlers.
        """
        return {
            "search": self._search,
            "filter": self._filter,
            "stats": self._stats,
            "get": self._get,
            "vector_search": self._vector_search,
        }

    def process_sync(self, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a query action against the DuckDB database.

        Args:
            payload: Must contain ``action`` (str). Additional fields
                depend on the action type.
            config: Worker config dict. May include ``db_path`` to
                override the constructor default.

        Returns:
            A dict with ``"output"`` (query results) and
            ``"model_used"`` (always ``"duckdb"``).

        Raises:
            ValueError: If the action is unknown.
            DuckDBQueryError: If the database query fails.
        """
        db_path = config.get("db_path", str(self.db_path))
        action = payload.get("action", "")

        handlers = self._get_handlers()

        handler = handlers.get(action)
        if not handler:
            raise ValueError(f"Unknown action '{action}'. Supported: {', '.join(handlers.keys())}")

        try:
            conn = duckdb.connect(db_path, read_only=True)
            try:
                # Load FTS extension for search queries.
                conn.execute("LOAD fts")
                result = handler(conn, payload)
            finally:
                conn.close()
        except (ValueError, DuckDBQueryError):
            raise
        except Exception as exc:
            raise DuckDBQueryError(f"Query failed (action={action}): {exc}") from exc

        return {"output": result, "model_used": "duckdb"}

    def _search(self, conn: duckdb.DuckDBPyConnection, payload: dict[str, Any]) -> dict[str, Any]:
        """Full-text search using DuckDB FTS extension.

        Uses BM25 scoring to rank records by relevance. Falls back to
        ILIKE when the FTS index is not available.

        Args:
            conn: Open DuckDB connection.
            payload: Must contain ``query`` (str). Optional ``limit``.
        """
        query = payload.get("query", "")
        limit = min(payload.get("limit", 20), 100)

        if not query.strip():
            return {"results": [], "total": 0}

        cols = ", ".join(f"d.{c}" for c in self.result_columns)

        # Derive the FTS index name from the table name.
        fts_func = f"fts_main_{self.table_name}.match_bm25"

        try:
            rows = conn.execute(
                f"""
                SELECT {cols}, fts.score
                FROM {self.table_name} d
                JOIN (
                    SELECT *,
                        {fts_func}({self.id_column}, ?, fields := '{self.fts_fields}') AS score
                    FROM {self.table_name}
                ) fts ON d.{self.id_column} = fts.{self.id_column}
                WHERE fts.score IS NOT NULL
                ORDER BY fts.score DESC
                LIMIT ?
                """,
                [query, limit],
            ).fetchall()
        except duckdb.Error:
            # FTS index may not exist yet. Fall back to ILIKE search
            # across the FTS fields.
            fts_cols = [f.strip() for f in self.fts_fields.split(",")]
            ilike_conditions = " OR ".join(f"d.{c} ILIKE ?" for c in fts_cols)
            ilike_params = [f"%{query}%" for _ in fts_cols]
            rows = conn.execute(
                f"""
                SELECT {cols}, 0.0 AS score
                FROM {self.table_name} d
                WHERE {ilike_conditions}
                ORDER BY d.{self.id_column} DESC
                LIMIT ?
                """,
                [*ilike_params, limit],
            ).fetchall()

        columns = [*self.result_columns, "score"]
        results = [self._row_to_dict(row, columns) for row in rows]

        return {"results": results, "total": len(results)}

    def _filter(self, conn: duckdb.DuckDBPyConnection, payload: dict[str, Any]) -> dict[str, Any]:
        """Filter records by attribute criteria.

        Applies conditions defined in ``filter_fields`` when the
        corresponding payload key is present.

        Args:
            conn: Open DuckDB connection.
            payload: Optional fields matching ``filter_fields`` keys.
        """
        conditions: list[str] = []
        params: list[Any] = []

        for field_name, sql_template in self.filter_fields.items():
            if field_name in payload:
                conditions.append(sql_template)
                params.append(payload[field_name])

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        limit = min(payload.get("limit", 20), 100)

        cols = ", ".join(self.result_columns)
        rows = conn.execute(
            f"SELECT {cols} FROM {self.table_name} {where}"
            f" ORDER BY {self.default_order_by} LIMIT ?",
            [*params, limit],
        ).fetchall()

        results = [self._row_to_dict(row, self.result_columns) for row in rows]

        count_row = conn.execute(
            f"SELECT COUNT(*) FROM {self.table_name} {where}",
            params,
        ).fetchone()
        total = count_row[0]

        return {"results": results, "total": total}

    def _stats(self, conn: duckdb.DuckDBPyConnection, payload: dict[str, Any]) -> dict[str, Any]:
        """Compute aggregate statistics grouped by a column.

        Args:
            conn: Open DuckDB connection.
            payload: Optional ``group_by`` (str). Must be one of
                the allowed ``stats_groups``.
        """
        if not self.stats_groups:
            raise ValueError("No stats_groups configured for this backend")

        default_group = next(iter(self.stats_groups))
        group_by = payload.get("group_by", default_group)

        if group_by not in self.stats_groups:
            raise ValueError(
                f"Invalid group_by '{group_by}'. Allowed: {', '.join(self.stats_groups)}"
            )

        agg_exprs = ", ".join(self.stats_aggregates)
        rows = conn.execute(
            f"""
            SELECT {group_by}, {agg_exprs}
            FROM {self.table_name}
            GROUP BY {group_by}
            ORDER BY {self.stats_aggregates[0].split(" AS ")[1]} DESC
            """,
        ).fetchall()

        # Build result dicts from column descriptions.
        col_names = [group_by] + [agg.split(" AS ")[1].strip() for agg in self.stats_aggregates]
        results = [dict(zip(col_names, row, strict=False)) for row in rows]

        total_row = conn.execute(f"SELECT COUNT(*) FROM {self.table_name}").fetchone()

        return {"results": results, "total": total_row[0]}

    def _get(self, conn: duckdb.DuckDBPyConnection, payload: dict[str, Any]) -> dict[str, Any]:
        """Retrieve a single record by ID, including full text if configured.

        Args:
            conn: Open DuckDB connection.
            payload: Must contain an ID field matching ``id_column``
                or ``"document_id"`` (for backward compatibility).

        Raises:
            ValueError: If no ID is provided.
            DuckDBQueryError: If the record is not found.
        """
        record_id = payload.get(self.id_column) or payload.get("document_id")
        if not record_id:
            raise ValueError(f"{self.id_column} is required for 'get' action")

        all_columns = list(self.result_columns)
        if self.full_text_column and self.full_text_column not in all_columns:
            all_columns = [*all_columns, self.full_text_column]

        cols = ", ".join(all_columns)
        row = conn.execute(
            f"SELECT {cols} FROM {self.table_name} WHERE {self.id_column} = ?",
            [record_id],
        ).fetchone()

        if not row:
            raise DuckDBQueryError(f"Record not found: {record_id}")

        return {"document": self._row_to_dict(row, all_columns)}

    def _vector_search(
        self, conn: duckdb.DuckDBPyConnection, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Semantic similarity search using vector embeddings.

        Generates a query embedding via Ollama, then compares it against
        stored record embeddings using cosine similarity.

        Args:
            conn: Open DuckDB connection.
            payload: Must contain ``query`` (str). Optional ``limit``,
                ``embedding`` config for Ollama model/url.
        """
        query_text = payload.get("query", "")
        limit = min(payload.get("limit", 5), 100)

        if not query_text.strip():
            return {"results": [], "total": 0}

        # Generate query embedding via Ollama
        embedding_config = payload.get("embedding", {})
        from heddle.worker.embeddings import OllamaEmbeddingProvider

        provider = OllamaEmbeddingProvider(
            model=embedding_config.get("model", "nomic-embed-text"),
            base_url=embedding_config.get("ollama_url"),
        )
        try:
            query_embedding = asyncio.run(provider.embed(query_text))
        except Exception as exc:
            raise DuckDBQueryError(f"Failed to generate query embedding: {exc}") from exc

        cols = ", ".join(f"d.{c}" for c in self.result_columns)

        rows = conn.execute(
            f"""
            SELECT {cols},
                   list_cosine_similarity(d.{self.embedding_column}, ?) AS similarity
            FROM {self.table_name} d
            WHERE d.{self.embedding_column} IS NOT NULL
            ORDER BY similarity DESC
            LIMIT ?
            """,
            [query_embedding, limit],
        ).fetchall()

        columns = [*self.result_columns, "similarity"]
        results = [self._row_to_dict(row, columns) for row in rows]

        return {"results": results, "total": len(results)}

    def _row_to_dict(self, row: tuple, columns: list[str]) -> dict[str, Any]:
        """Convert a DuckDB result row to a dict, parsing JSON columns.

        Args:
            row: Tuple of values from a DuckDB query.
            columns: Column names corresponding to the row values.

        Returns:
            A dict mapping column names to values, with JSON columns
            parsed back into Python objects and timestamps converted
            to ISO strings.
        """
        result: dict[str, Any] = {}

        for col, raw_val in zip(columns, row, strict=False):
            parsed_val = raw_val
            if col in self.json_columns and isinstance(parsed_val, str):
                with contextlib.suppress(json.JSONDecodeError):
                    parsed_val = json.loads(parsed_val)
            # Convert datetime to ISO string for JSON serialization.
            if hasattr(parsed_val, "isoformat"):
                parsed_val = str(parsed_val)
            result[col] = parsed_val

        return result
