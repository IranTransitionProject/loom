"""
DuckDB view tool — exposes a DuckDB view as an LLM-callable tool.

When configured in a worker's knowledge_silos, this tool lets the LLM
query a read-only DuckDB view during reasoning. The LLM can search
(full-text) or list records from the view.

Example knowledge_silos config::

    knowledge_silos:
      - name: "catalog"
        type: "tool"
        provider: "heddle.contrib.duckdb.DuckDBViewTool"
        config:
          db_path: "/tmp/workspace/data.duckdb"
          view_name: "summaries"
          description: "Search and browse record summaries"
          max_results: 20

The tool auto-introspects the view's columns via DESCRIBE to build its
JSON Schema definition. Queries use parameterized SQL to prevent injection.
"""

from __future__ import annotations

import json
from typing import Any

import duckdb
import structlog

from heddle.contrib._sql_security import validate_sql_identifier
from heddle.worker.tools import SyncToolProvider

logger = structlog.get_logger()


class DuckDBViewTool(SyncToolProvider):
    """Expose a DuckDB view as an LLM-callable search/list tool.

    The tool dynamically introspects the view's column schema at
    instantiation time and builds a JSON Schema tool definition
    that the LLM can call.

    Supports two operations:
        - ``search``: Full-text ILIKE search across all text columns
        - ``list``: List recent records with optional column filters

    All queries are parameterized and results are capped at ``max_results``.
    """

    def __init__(
        self,
        db_path: str,
        view_name: str,
        description: str = "Query a database view",
        max_results: int = 20,
    ) -> None:
        self.db_path = db_path
        # Validate at construction so misconfigured YAML fails loudly
        # at startup rather than silently breaking (or worse, executing
        # an injected DDL fragment) later when ``DESCRIBE {view_name}``
        # is interpolated.
        self.view_name = validate_sql_identifier(view_name, field="view_name")
        self.description = description
        self.max_results = max_results
        self._columns: list[dict[str, str]] = []
        self._introspect()

    def _introspect(self) -> None:
        """Discover view columns via DESCRIBE."""
        try:
            conn = duckdb.connect(self.db_path, read_only=True)
            try:
                rows = conn.execute(f"DESCRIBE {self.view_name}").fetchall()
                self._columns = [{"name": row[0], "type": row[1]} for row in rows]
            finally:
                conn.close()
        except Exception as e:
            logger.warning(
                "duckdb_view.introspect_failed",
                view=self.view_name,
                error=str(e),
            )

    def get_definition(self) -> dict[str, Any]:
        """Build JSON Schema tool definition from view columns."""
        # Build filterable columns (non-text columns that might be useful for filtering)
        filter_props: dict[str, Any] = {}
        for col in self._columns:
            col_type = col["type"].upper()
            if col_type in ("VARCHAR", "TEXT"):
                filter_props[col["name"]] = {"type": "string"}
            elif col_type in ("INTEGER", "BIGINT", "SMALLINT", "TINYINT"):
                filter_props[col["name"]] = {"type": "integer"}
            elif col_type in ("DOUBLE", "FLOAT", "DECIMAL"):
                filter_props[col["name"]] = {"type": "number"}
            elif col_type == "BOOLEAN":
                filter_props[col["name"]] = {"type": "boolean"}

        return {
            "name": f"query_{self.view_name}",
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["search", "list"],
                        "description": "search: full-text search; list: browse records",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query (for search operation)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            f"Max results to return (default: 10, max: {self.max_results})"
                        ),
                    },
                    "filters": {
                        "type": "object",
                        "description": "Column filters (for list operation)",
                        "properties": filter_props,
                    },
                },
                "required": ["operation"],
            },
        }

    def execute_sync(self, arguments: dict[str, Any]) -> str:
        """Execute a query against the DuckDB view."""
        operation = arguments.get("operation", "list")
        limit = min(arguments.get("limit", 10), self.max_results)

        try:
            conn = duckdb.connect(self.db_path, read_only=True)
            try:
                if operation == "search":
                    result = self._search(conn, arguments, limit)
                else:
                    result = self._list(conn, arguments, limit)
            finally:
                conn.close()
        except Exception as e:
            return json.dumps({"error": str(e)})

        return json.dumps(result, default=str)

    def _search(
        self,
        conn: duckdb.DuckDBPyConnection,
        arguments: dict[str, Any],
        limit: int,
    ) -> dict[str, Any]:
        """Full-text search using ILIKE across text columns."""
        query = arguments.get("query", "")
        if not query.strip():
            return {"results": [], "total": 0}

        # Find text columns to search across
        text_cols = [c["name"] for c in self._columns if c["type"].upper() in ("VARCHAR", "TEXT")]

        if not text_cols:
            return {"results": [], "total": 0, "error": "No searchable text columns"}

        # Build OR conditions for each text column
        conditions = " OR ".join(f"{col} ILIKE ?" for col in text_cols)
        params = [f"%{query}%" for _ in text_cols]

        col_names = ", ".join(c["name"] for c in self._columns)
        rows = conn.execute(
            f"SELECT {col_names} FROM {self.view_name} WHERE {conditions} LIMIT ?",
            [*params, limit],
        ).fetchall()

        results = [dict(zip((c["name"] for c in self._columns), row, strict=False)) for row in rows]

        return {"results": results, "total": len(results)}

    def _list(
        self,
        conn: duckdb.DuckDBPyConnection,
        arguments: dict[str, Any],
        limit: int,
    ) -> dict[str, Any]:
        """List records with optional column filters."""
        filters = arguments.get("filters", {})
        conditions: list[str] = []
        params: list[Any] = []

        # Only allow filtering on actual view columns
        valid_cols = {c["name"] for c in self._columns}
        for col_name, value in filters.items():
            if col_name in valid_cols:
                conditions.append(f"{col_name} = ?")
                params.append(value)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        col_names = ", ".join(c["name"] for c in self._columns)

        rows = conn.execute(
            f"SELECT {col_names} FROM {self.view_name} {where} LIMIT ?",
            [*params, limit],
        ).fetchall()

        results = [dict(zip((c["name"] for c in self._columns), row, strict=False)) for row in rows]

        return {"results": results, "total": len(results)}
