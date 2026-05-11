"""
LanceDB vector similarity search tool for LLM function-calling.

Uses embedding vectors stored in LanceDB to find semantically similar
records. Query text is embedded via Ollama at search time, then
compared against stored vectors using LanceDB's ANN search.

Example knowledge_silos config::

    knowledge_silos:
      - name: "similar_items"
        type: "tool"
        provider: "heddle.contrib.lancedb.LanceDBVectorTool"
        config:
          db_path: "/tmp/workspace/rag-vectors.lance"
          table_name: "rag_chunks"
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

import structlog

from heddle.worker.tools import SyncToolProvider

logger = structlog.get_logger()


class LanceDBVectorTool(SyncToolProvider):
    """Semantic similarity search over LanceDB vector embeddings.

    Generates a query embedding via Ollama, then uses LanceDB's ANN
    search to find the most similar records by their stored vectors.

    Args:
        db_path: Path to the LanceDB database directory.
        table_name: Table containing the records and embeddings.
        vector_column: Name of the column storing embedding vectors.
        result_columns: Columns to include in results. If None, returns
            chunk_id, text, source_channel_id, source_global_id.
        tool_name: Name exposed in the LLM tool definition.
        description: Description exposed in the LLM tool definition.
        embedding_model: Ollama model name for embedding generation.
        ollama_url: Optional custom Ollama server URL.
        max_results: Hard cap on returned results.
    """

    def __init__(
        self,
        db_path: str,
        table_name: str = "rag_chunks",
        vector_column: str = "vector",
        result_columns: list[str] | None = None,
        tool_name: str = "find_similar",
        description: str = "Find semantically similar records",
        embedding_model: str = "nomic-embed-text",
        ollama_url: str | None = None,
        max_results: int = 10,
    ) -> None:
        self.db_path = db_path
        self.table_name = table_name
        self.vector_column = vector_column
        self._result_columns = result_columns or [
            "chunk_id",
            "text",
            "source_channel_id",
            "source_global_id",
        ]
        self.tool_name = tool_name
        self.description = description
        self.embedding_model = embedding_model
        self.ollama_url = ollama_url
        self.max_results = max_results

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

    def execute_sync(self, arguments: dict[str, Any]) -> str:  # pragma: no cover
        """Embed the query and search for similar records."""
        query = arguments.get("query", "")
        limit = min(arguments.get("limit", 5), self.max_results)

        if not query.strip():
            return json.dumps({"results": [], "total": 0})

        query_embedding = self._embed_query(query)
        if query_embedding is None:
            return json.dumps({"error": "Failed to generate query embedding"})

        try:
            import lancedb

            db = lancedb.connect(self.db_path)
            if self.table_name not in db.table_names():
                return json.dumps({"results": [], "total": 0})

            table = db.open_table(self.table_name)
            raw_results = (
                table.search(query_embedding, vector_column_name=self.vector_column)
                .limit(limit)
                .to_list()
            )

            results = []
            for row in raw_results:
                record = {}
                for col in self._result_columns:
                    if col in row:
                        record[col] = row[col]
                distance = row.get("_distance", 1.0)
                record["similarity"] = max(0.0, 1.0 - distance)
                results.append(record)

            return json.dumps({"results": results, "total": len(results)}, default=str)

        except Exception as e:
            return json.dumps({"error": str(e)})

    def _embed_query(self, text: str) -> list[float] | None:
        """Generate embedding for the query text via the provider's sync path.

        Uses ``embed_sync`` rather than ``asyncio.run(provider.embed())``
        because the latter races with outer running loops in test
        harnesses (TestClient flows) and nested sync→async→sync chains.
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
                "lancedb_vector.embed_query_failed",
                error=str(exc),
            )
            return None
