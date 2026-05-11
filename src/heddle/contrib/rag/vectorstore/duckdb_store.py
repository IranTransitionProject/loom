"""DuckDB-backed vector store for embedded text chunks.

Stores EmbeddedChunk records in a DuckDB table with a FLOAT[] column for the
embedding vector.  Supports:
  - Batch insertion of EmbeddedChunk objects
  - Cosine similarity search (returns SimilarityResult)
  - Full-text search via DuckDB FTS extension (optional)
  - Basic CRUD (get, delete by chunk_id)

Uses Heddle's :class:`OllamaEmbeddingProvider` (default) or
:class:`OpenAICompatibleEmbeddingProvider` (e.g. for LM Studio) for
query embedding generation.

No external vector DB required — DuckDB handles both structured data and
vector similarity in a single embedded database.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..schemas.embedding import EmbeddedChunk, SimilarityResult
from .base import VectorStore


def _utcnow() -> datetime:
    return datetime.now(UTC)


if TYPE_CHECKING:
    from ..schemas.chunk import TextChunk

logger = logging.getLogger(__name__)


class DuckDBVectorStore(VectorStore):
    """
    Embedded vector store backed by DuckDB.

    Usage::

        store = DuckDBVectorStore("/tmp/rag.duckdb")
        store.initialize()

        # Embed and store chunks
        store.add_chunks(chunks, embedding_model="nomic-embed-text")

        # Search
        results = store.search("earthquake damage", limit=5)

        store.close()
    """

    TABLE_NAME = "rag_chunks"

    def __init__(
        self,
        db_path: str = "/tmp/rag-vectors.duckdb",
        embedding_model: str = "nomic-embed-text",
        ollama_url: str = "http://localhost:11434",
        embedding_backend: str = "ollama",
        embedding_url: str | None = None,
    ) -> None:
        """Initialize the store.

        Args:
            db_path: DuckDB file path.
            embedding_model: Model name passed to the embedding provider.
            ollama_url: Ollama base URL (used when
                ``embedding_backend == "ollama"``).
            embedding_backend: ``"ollama"`` or ``"openai-compatible"``.
                The latter routes through
                :class:`OpenAICompatibleEmbeddingProvider`, which works
                with LM Studio, OpenAI, vLLM, TEI, etc.
            embedding_url: Base URL for OpenAI-compatible embeddings
                (e.g. ``http://localhost:1234/v1`` for LM Studio).
                Required when ``embedding_backend == "openai-compatible"``.
        """
        self.db_path = Path(db_path)
        self.embedding_model = embedding_model
        self.ollama_url = ollama_url
        self.embedding_backend = embedding_backend
        self.embedding_url = embedding_url
        self._conn: Any = None
        self._embedding_dim: int | None = None

    def initialize(self) -> DuckDBVectorStore:
        """Create the database table if it doesn't exist."""
        import duckdb

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(str(self.db_path))

        self._conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                chunk_id          VARCHAR PRIMARY KEY,
                source_global_id  VARCHAR NOT NULL,
                source_channel_id INTEGER NOT NULL,
                source_channel_name VARCHAR DEFAULT '',
                text              VARCHAR NOT NULL,
                char_start        INTEGER DEFAULT 0,
                char_end          INTEGER DEFAULT 0,
                chunk_index       INTEGER DEFAULT 0,
                total_chunks      INTEGER DEFAULT 1,
                strategy          VARCHAR DEFAULT 'sentence',
                timestamp_unix    INTEGER DEFAULT 0,
                embedding         FLOAT[],
                embedding_model   VARCHAR DEFAULT '',
                embedding_dim     INTEGER DEFAULT 0,
                embedded_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        logger.info("Initialized vector store at %s", self.db_path)
        return self

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    def _get_embedder(self) -> Any:
        """Lazy-load the embedding provider configured for this store."""
        if self.embedding_backend == "openai-compatible":
            from heddle.worker.embeddings import OpenAICompatibleEmbeddingProvider

            return OpenAICompatibleEmbeddingProvider(
                model=self.embedding_model,
                base_url=self.embedding_url,
            )

        from heddle.worker.embeddings import OllamaEmbeddingProvider

        return OllamaEmbeddingProvider(
            model=self.embedding_model,
            base_url=self.ollama_url,
        )

    def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a batch of texts synchronously.

        Uses the provider's ``embed_batch_sync`` path — matches the
        :class:`heddle.contrib.lancedb.store.LanceDBStore` shape so the
        two RAG vector stores have the same call discipline.
        """
        embedder = self._get_embedder()
        embeddings = embedder.embed_batch_sync(texts)

        if embeddings and self._embedding_dim is None:
            self._embedding_dim = len(embeddings[0])

        return embeddings

    # ------------------------------------------------------------------
    # Insert
    # ------------------------------------------------------------------

    def add_chunks(
        self,
        chunks: list[TextChunk],
        batch_size: int = 64,
    ) -> int:
        """Embed and insert TextChunk objects. Returns count of inserted rows."""
        if not chunks:
            return 0

        total_inserted = 0
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i : i + batch_size]
            texts = [c.text for c in batch]

            try:
                embeddings = self._embed_texts(texts)
            except Exception as exc:
                logger.error("Embedding batch %d failed: %s", i // batch_size, exc)
                continue

            for chunk, emb in zip(batch, embeddings, strict=False):
                try:
                    self._conn.execute(
                        f"""INSERT OR REPLACE INTO {self.TABLE_NAME}
                            (chunk_id, source_global_id, source_channel_id,
                             source_channel_name, text, char_start, char_end,
                             chunk_index, total_chunks, strategy, timestamp_unix,
                             embedding, embedding_model, embedding_dim)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        [
                            chunk.chunk_id,
                            chunk.source_global_id,
                            chunk.source_channel_id,
                            chunk.source_channel_name,
                            chunk.text,
                            chunk.char_start,
                            chunk.char_end,
                            chunk.chunk_index,
                            chunk.total_chunks,
                            chunk.strategy.value
                            if hasattr(chunk.strategy, "value")
                            else str(chunk.strategy),
                            chunk.timestamp_unix,
                            emb,
                            self.embedding_model,
                            len(emb),
                        ],
                    )
                    total_inserted += 1
                except Exception as exc:
                    logger.warning("Insert failed for chunk %s: %s", chunk.chunk_id, exc)

        logger.info("Inserted %d / %d chunks into %s", total_inserted, len(chunks), self.TABLE_NAME)
        return total_inserted

    def add_embedded_chunks(self, chunks: list[EmbeddedChunk]) -> int:
        """Insert pre-embedded chunks (no embedding generation needed)."""
        if not chunks:
            return 0

        inserted = 0
        for ec in chunks:
            try:
                self._conn.execute(
                    f"""INSERT OR REPLACE INTO {self.TABLE_NAME}
                        (chunk_id, source_global_id, source_channel_id,
                         text, embedding, embedding_model, embedding_dim)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    [
                        ec.chunk_id,
                        ec.source_global_id,
                        ec.source_channel_id,
                        ec.text,
                        ec.embedding,
                        ec.model,
                        ec.dimensions,
                    ],
                )
                inserted += 1
            except Exception as exc:
                logger.warning("Insert failed for chunk %s: %s", ec.chunk_id, exc)

        return inserted

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        limit: int = 10,
        min_score: float = 0.0,
        channel_ids: list[int] | None = None,
    ) -> list[SimilarityResult]:
        """
        Semantic similarity search.

        Args:
            query:       Natural language query (embedded via Ollama)
            limit:       Maximum results to return
            min_score:   Minimum cosine similarity threshold
            channel_ids: Optional filter by source channel

        Returns:
            List of SimilarityResult sorted by descending similarity
        """
        embeddings = self._embed_texts([query])
        if not embeddings:
            return []

        query_emb = embeddings[0]

        where_clauses = ["embedding IS NOT NULL"]
        params: list[Any] = []

        if channel_ids:
            placeholders = ", ".join("?" for _ in channel_ids)
            where_clauses.append(f"source_channel_id IN ({placeholders})")
            params.extend(channel_ids)

        where_sql = " AND ".join(where_clauses)

        rows = self._conn.execute(
            f"""
            SELECT chunk_id, text, source_channel_id, source_global_id,
                   source_channel_name, timestamp_unix, strategy,
                   list_cosine_similarity(embedding, ?) AS similarity
            FROM {self.TABLE_NAME}
            WHERE {where_sql}
            ORDER BY similarity DESC
            LIMIT ?
            """,
            [query_emb, *params, limit],
        ).fetchall()

        results: list[SimilarityResult] = []
        for row in rows:
            score = float(row[7]) if row[7] is not None else 0.0
            if score < min_score:
                continue
            results.append(
                SimilarityResult(
                    chunk_id=row[0],
                    text=row[1],
                    score=score,
                    source_channel_id=row[2],
                    source_global_id=row[3],
                    metadata={
                        "source_channel_name": row[4],
                        "timestamp_unix": row[5],
                        "strategy": row[6],
                    },
                )
            )

        return results

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def count(self) -> int:
        """Return total number of stored chunks."""
        row = self._conn.execute(f"SELECT COUNT(*) FROM {self.TABLE_NAME}").fetchone()
        return row[0] if row else 0

    def get(self, chunk_id: str) -> EmbeddedChunk | None:
        """Retrieve a single embedded chunk by ID.

        Returns every column the row stores — earlier this method
        selected seven columns and silently dropped
        ``source_channel_name``, ``chunk_index``, ``total_chunks``,
        ``strategy``, ``timestamp_unix``, and ``embedded_at`` so any
        consumer that round-tripped a chunk through ``get`` saw
        defaults in those positions.
        """
        row = self._conn.execute(
            f"""SELECT chunk_id, source_global_id, source_channel_id,
                       source_channel_name, text, embedding, embedding_model,
                       embedding_dim, chunk_index, total_chunks, strategy,
                       timestamp_unix, embedded_at
                FROM {self.TABLE_NAME} WHERE chunk_id = ?""",
            [chunk_id],
        ).fetchone()

        if not row:
            return None

        return EmbeddedChunk(
            chunk_id=row[0],
            source_global_id=row[1],
            source_channel_id=row[2],
            source_channel_name=row[3] or "",
            text=row[4],
            embedding=list(row[5]) if row[5] else [],
            model=row[6] or "",
            dimensions=row[7] or 0,
            chunk_index=row[8] or 0,
            total_chunks=row[9] or 1,
            strategy=row[10] or "sentence",
            timestamp_unix=row[11] or 0,
            embedded_at=row[12] if row[12] is not None else _utcnow(),
        )

    def delete(self, chunk_id: str) -> bool:
        """Delete a chunk by ID. Returns True if a row was deleted."""
        result = self._conn.execute(
            f"DELETE FROM {self.TABLE_NAME} WHERE chunk_id = ? RETURNING chunk_id",
            [chunk_id],
        ).fetchone()
        return result is not None

    def delete_by_source(self, source_global_id: str) -> int:
        """Delete all chunks for a given source post. Returns count."""
        rows = self._conn.execute(
            f"DELETE FROM {self.TABLE_NAME} WHERE source_global_id = ? RETURNING chunk_id",
            [source_global_id],
        ).fetchall()
        return len(rows)

    def stats(self) -> dict[str, Any]:
        """Return summary statistics about the store."""
        row = self._conn.execute(f"""
            SELECT COUNT(*),
                   COUNT(DISTINCT source_global_id),
                   COUNT(DISTINCT source_channel_id),
                   MIN(timestamp_unix),
                   MAX(timestamp_unix)
            FROM {self.TABLE_NAME}
        """).fetchone()

        if not row or row[0] == 0:
            return {"total_chunks": 0}

        return {
            "total_chunks": row[0],
            "unique_posts": row[1],
            "unique_channels": row[2],
            "earliest_timestamp": row[3],
            "latest_timestamp": row[4],
            "db_path": str(self.db_path),
        }
