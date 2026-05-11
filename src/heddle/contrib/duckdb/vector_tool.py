"""
DuckDB vector similarity search tool for LLM function-calling.

Uses embedding vectors stored in DuckDB to find semantically similar
records. Query text is embedded via Ollama at search time, then
compared against stored vectors using DuckDB's ``list_cosine_similarity``.

Example knowledge_silos config::

    knowledge_silos:
      - name: "similar_items"
        type: "tool"
        provider: "heddle.contrib.duckdb.DuckDBVectorTool"
        config:
          db_path: "/tmp/workspace/data.duckdb"
          table_name: "documents"
          result_columns: ["id", "title", "summary", "created_at"]
          embedding_column: "embedding"
          tool_name: "find_similar"
          description: "Find records semantically similar to a query"
          embedding_model: "nomic-embed-text"

See Also:
    heddle.worker.embeddings -- OllamaEmbeddingProvider
    heddle.worker.tools -- SyncToolProvider base class
"""

from __future__ import annotations

import json
from typing import Any

import duckdb
import structlog

from heddle.worker.tools import SyncToolProvider

logger = structlog.get_logger()


class DuckDBVectorTool(SyncToolProvider):
    """Semantic similarity search over DuckDB vector embeddings.

    Generates a query embedding via Ollama, then uses DuckDB's
    ``list_cosine_similarity`` function to find the most similar
    records by their stored embedding vectors.

    Only records with non-null embeddings are searched.

    Args:
        db_path: Path to the DuckDB database file.
        table_name: Table containing the records and embeddings.
        result_columns: Columns to include in results. If None,
            introspects the table schema at first use, excluding
            the embedding column and any column named ``full_text``.
        embedding_column: Name of the column storing embedding vectors.
        tool_name: Name exposed in the LLM tool definition.
        description: Description exposed in the LLM tool definition.
        embedding_model: Ollama model name for embedding generation.
        ollama_url: Optional custom Ollama server URL.
        max_results: Hard cap on returned results.
    """

    def __init__(
        self,
        db_path: str,
        table_name: str = "documents",
        result_columns: list[str] | None = None,
        embedding_column: str = "embedding",
        tool_name: str = "find_similar",
        description: str = "Find semantically similar records",
        embedding_model: str = "nomic-embed-text",
        ollama_url: str | None = None,
        max_results: int = 10,
    ) -> None:
        from heddle.contrib._sql_security import validate_sql_identifier

        self.db_path = db_path
        # Identifiers flow into ``DESCRIBE`` and ``SELECT ... FROM
        # {table}`` statements as f-string interpolations.  Validate at
        # construction so misconfigured YAML rejects loudly instead of
        # silently corrupting (or hijacking) queries.
        self.table_name = validate_sql_identifier(table_name, field="table_name")
        self.embedding_column = validate_sql_identifier(embedding_column, field="embedding_column")
        self._result_columns = result_columns
        self.tool_name = tool_name
        self.description = description
        self.embedding_model = embedding_model
        self.ollama_url = ollama_url
        self.max_results = max_results

    @property
    def result_columns(self) -> list[str]:
        """Return result columns, introspecting on first access if needed."""
        if self._result_columns is None:
            self._result_columns = self._introspect_columns()
        return self._result_columns

    def _introspect_columns(self) -> list[str]:
        """Discover table columns, excluding embedding and full_text."""
        exclude = {self.embedding_column, "full_text"}
        try:
            conn = duckdb.connect(self.db_path, read_only=True)
            try:
                rows = conn.execute(f"DESCRIBE {self.table_name}").fetchall()
                return [
                    row[0]
                    for row in rows
                    if row[0] not in exclude and not row[1].upper().startswith("FLOAT[")
                ]
            finally:
                conn.close()
        except Exception as exc:
            logger.warning(
                "duckdb_vector.introspect_failed",
                table=self.table_name,
                error=str(exc),
            )
            return ["id"]

    def get_definition(self) -> dict[str, Any]:
        """Return tool definition for LLM function-calling."""
        return {
            "name": self.tool_name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language query to find similar records",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"Max results (default: 5, max: {self.max_results})",
                    },
                },
                "required": ["query"],
            },
        }

    def execute_sync(self, arguments: dict[str, Any]) -> str:
        """Embed the query and search for similar records."""
        query = arguments.get("query", "")
        limit = min(arguments.get("limit", 5), self.max_results)

        if not query.strip():
            return json.dumps({"results": [], "total": 0})

        # Generate query embedding via Ollama.
        query_embedding = self._embed_query(query)
        if query_embedding is None:
            return json.dumps({"error": "Failed to generate query embedding"})

        try:
            conn = duckdb.connect(self.db_path, read_only=True)
            try:
                result = self._similarity_search(conn, query_embedding, limit)
            finally:
                conn.close()
        except Exception as e:
            return json.dumps({"error": str(e)})

        return json.dumps(result, default=str)

    def _embed_query(self, text: str) -> list[float] | None:
        """Generate embedding for the query text via the provider's sync path.

        ``asyncio.run(provider.embed(...))`` was the old shape and broke
        the moment a caller invoked this from a thread that already had
        a live event loop (TestClient flows, nested sync→async→sync
        chains).  ``embed_sync`` uses a sync ``httpx.Client`` instead
        and is safe in every threading context.
        """
        from heddle.worker.embeddings import OllamaEmbeddingProvider

        provider = OllamaEmbeddingProvider(
            model=self.embedding_model,
            base_url=self.ollama_url,
        )
        try:
            return provider.embed_sync(text)
        except Exception as exc:
            logger.warning(
                "duckdb_vector.embed_query_failed",
                error=str(exc),
            )
            return None

    def _similarity_search(
        self,
        conn: duckdb.DuckDBPyConnection,
        query_embedding: list[float],
        limit: int,
    ) -> dict[str, Any]:
        """Run cosine similarity search against stored embeddings."""
        cols = ", ".join(self.result_columns)

        rows = conn.execute(
            f"""
            SELECT {cols},
                   list_cosine_similarity({self.embedding_column}, ?) AS similarity
            FROM {self.table_name}
            WHERE {self.embedding_column} IS NOT NULL
            ORDER BY similarity DESC
            LIMIT ?
            """,
            [query_embedding, limit],
        ).fetchall()

        result_cols = [*self.result_columns, "similarity"]
        results = [dict(zip(result_cols, row, strict=False)) for row in rows]

        return {"results": results, "total": len(results)}
