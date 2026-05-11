"""LanceDB-backed vector store for embedded text chunks.

Stores EmbeddedChunk records in a LanceDB table with native vector columns.
Supports:
  - Batch insertion of TextChunk objects (with embedding generation)
  - Pre-embedded chunk insertion
  - Approximate Nearest Neighbor (ANN) similarity search
  - Metadata filtering (e.g. by channel_id)
  - Basic CRUD (get, delete by chunk_id)

Uses Heddle's :class:`OllamaEmbeddingProvider` (default) or
:class:`OpenAICompatibleEmbeddingProvider` (e.g. for LM Studio) for
query embedding generation.

LanceDB provides ANN indexing for faster search over large datasets compared
to exact cosine similarity in DuckDB.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from heddle.contrib._sql_security import escape_sql_string_literal
from heddle.contrib.rag.schemas.embedding import EmbeddedChunk, SimilarityResult
from heddle.contrib.rag.vectorstore.base import VectorStore

if TYPE_CHECKING:
    from heddle.contrib.rag.schemas.chunk import TextChunk

logger = logging.getLogger(__name__)


def _build_channel_id_filter(channel_ids: list[int]) -> str:
    """Build a LanceDB ``where`` predicate from a list of channel ids.

    ``channel_ids`` flows from a worker config payload and is
    interpolated into the filter expression.  Cast each id to
    ``int`` so a non-integer value (e.g. a string smuggling a
    filter fragment) raises before being spliced into the
    expression.  Earlier the schema validator on the shipped YAML
    constrained ``channel_ids`` to integers, but a relaxed schema
    or a new caller bypassing the validator could reopen the
    injection slot.
    """
    try:
        ids = [int(cid) for cid in channel_ids]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"channel_ids must be integers; got {channel_ids!r}") from exc
    return " OR ".join(f"source_channel_id = {cid}" for cid in ids)


class LanceDBVectorStore(VectorStore):
    """
    Embedded vector store backed by LanceDB.

    Usage::

        store = LanceDBVectorStore("/tmp/rag-vectors.lance")
        store.initialize()

        # Embed and store chunks
        store.add_chunks(chunks)

        # Search
        results = store.search("earthquake damage", limit=5)

        store.close()
    """

    TABLE_NAME = "rag_chunks"

    def __init__(
        self,
        db_path: str = "/tmp/rag-vectors.lance",
        embedding_model: str = "nomic-embed-text",
        ollama_url: str = "http://localhost:11434",
        embedding_backend: str = "ollama",
        embedding_url: str | None = None,
    ) -> None:
        """Initialize the store.

        See :class:`heddle.contrib.rag.vectorstore.duckdb_store.DuckDBVectorStore`
        for parameter semantics.  ``embedding_backend`` selects between
        :class:`OllamaEmbeddingProvider` and
        :class:`OpenAICompatibleEmbeddingProvider` (LM Studio etc.).
        """
        self.db_path = Path(db_path)
        self.embedding_model = embedding_model
        self.ollama_url = ollama_url
        self.embedding_backend = embedding_backend
        self.embedding_url = embedding_url
        self._db: Any = None
        self._table: Any = None
        self._embedding_dim: int | None = None

    def initialize(self) -> LanceDBVectorStore:
        """Open or create the LanceDB database and table."""
        import lancedb

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = lancedb.connect(str(self.db_path))

        # Check if table already exists
        if self.TABLE_NAME in self._db.list_tables():
            self._table = self._db.open_table(self.TABLE_NAME)
        else:
            self._table = None  # Created on first insert (need schema from data)

        logger.info("Initialized LanceDB vector store at %s", self.db_path)
        return self

    def close(self) -> None:
        """Close the database connection."""
        self._table = None
        self._db = None

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    def _get_embedder(self) -> Any:  # pragma: no cover
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

        Uses the provider's ``embed_batch_sync`` path.  The previous
        shape (``loop.run_until_complete`` on a fresh event loop)
        worked in isolation but mishandled provider-owned resources
        on subsequent calls and was awkward to test.
        """
        embedder = self._get_embedder()
        embeddings = embedder.embed_batch_sync(texts)

        if embeddings and self._embedding_dim is None:
            self._embedding_dim = len(embeddings[0])

        return embeddings

    # ------------------------------------------------------------------
    # Insert
    # ------------------------------------------------------------------

    def _ensure_table(self, records: list[dict[str, Any]]) -> bool:
        """Create the table from records if it doesn't exist yet.

        Returns True if the table was just created (records already inserted),
        False if it already existed (caller must add records separately).
        """
        if self._table is not None or not records:
            return False
        self._table = self._db.create_table(self.TABLE_NAME, records)
        return True

    def add_chunks(  # pragma: no cover
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

            records = []
            for chunk, emb in zip(batch, embeddings, strict=False):
                records.append(
                    {
                        "chunk_id": chunk.chunk_id,
                        "source_global_id": chunk.source_global_id,
                        "source_channel_id": chunk.source_channel_id,
                        "source_channel_name": chunk.source_channel_name,
                        "text": chunk.text,
                        "char_start": chunk.char_start,
                        "char_end": chunk.char_end,
                        "chunk_index": chunk.chunk_index,
                        "total_chunks": chunk.total_chunks,
                        "strategy": chunk.strategy.value
                        if hasattr(chunk.strategy, "value")
                        else str(chunk.strategy),
                        "timestamp_unix": chunk.timestamp_unix,
                        "vector": emb,
                        "embedding_model": self.embedding_model,
                        "embedding_dim": len(emb),
                    }
                )

            try:
                created = self._ensure_table(records)
                if not created and self._table is not None and records:
                    self._table.add(records)
                total_inserted += len(records)
            except Exception as exc:
                logger.warning("Insert batch %d failed: %s", i // batch_size, exc)

        logger.info("Inserted %d / %d chunks into %s", total_inserted, len(chunks), self.TABLE_NAME)
        return total_inserted

    def add_embedded_chunks(self, chunks: list[EmbeddedChunk]) -> int:
        """Insert pre-embedded chunks (no embedding generation needed)."""
        if not chunks:
            return 0

        records = [
            {
                "chunk_id": ec.chunk_id,
                "source_global_id": ec.source_global_id,
                "source_channel_id": ec.source_channel_id,
                "text": ec.text,
                "vector": ec.embedding,
                "embedding_model": ec.model,
                "embedding_dim": ec.dimensions,
                "source_channel_name": "",
                "char_start": 0,
                "char_end": 0,
                "chunk_index": 0,
                "total_chunks": 1,
                "strategy": "sentence",
                "timestamp_unix": 0,
            }
            for ec in chunks
        ]

        try:
            created = self._ensure_table(records)
            if not created and self._table is not None:
                self._table.add(records)
            return len(records)
        except Exception as exc:
            logger.warning("Insert pre-embedded chunks failed: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(  # pragma: no cover
        self,
        query: str,
        limit: int = 10,
        min_score: float = 0.0,
        channel_ids: list[int] | None = None,
    ) -> list[SimilarityResult]:
        """
        Semantic similarity search using LanceDB ANN.

        Args:
            query:       Natural language query (embedded via Ollama)
            limit:       Maximum results to return
            min_score:   Minimum cosine similarity threshold
            channel_ids: Optional filter by source channel

        Returns:
            List of SimilarityResult sorted by descending similarity
        """
        if self._table is None:
            return []

        embeddings = self._embed_texts([query])
        if not embeddings:
            return []

        query_emb = embeddings[0]

        search_query = self._table.search(query_emb, vector_column_name="vector").limit(limit)

        if channel_ids:
            filter_expr = _build_channel_id_filter(channel_ids)
            search_query = search_query.where(f"({filter_expr})")

        try:
            raw_results = search_query.to_list()
        except Exception as exc:
            logger.error("LanceDB search failed: %s", exc)
            return []

        results: list[SimilarityResult] = []
        for row in raw_results:
            # LanceDB returns _distance (L2) by default; for cosine metric
            # it returns 1 - cosine_similarity, so score = 1 - _distance
            distance = row.get("_distance", 1.0)
            score = max(0.0, 1.0 - distance)

            if score < min_score:
                continue

            results.append(
                SimilarityResult(
                    chunk_id=row["chunk_id"],
                    text=row["text"],
                    score=score,
                    source_channel_id=row["source_channel_id"],
                    source_global_id=row["source_global_id"],
                    metadata={
                        "source_channel_name": row.get("source_channel_name", ""),
                        "timestamp_unix": row.get("timestamp_unix", 0),
                        "strategy": row.get("strategy", ""),
                    },
                )
            )

        return results

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def count(self) -> int:
        """Return total number of stored chunks."""
        if self._table is None:
            return 0
        return self._table.count_rows()

    def get(self, chunk_id: str) -> EmbeddedChunk | None:
        """Retrieve a single embedded chunk by ID."""
        if self._table is None:
            return None

        # ``chunk_id`` is user data (not an identifier) flowing into a
        # LanceDB single-quoted string literal.  Escape ``'`` → ``''``
        # so a quote in the id can't break out of the literal.
        try:
            results = (
                self._table.search()
                .where(f"chunk_id = '{escape_sql_string_literal(chunk_id)}'")
                .limit(1)
                .to_list()
            )
        except Exception:
            return None

        if not results:
            return None

        row = results[0]
        return EmbeddedChunk(
            chunk_id=row["chunk_id"],
            source_global_id=row["source_global_id"],
            source_channel_id=row["source_channel_id"],
            text=row["text"],
            embedding=list(row.get("vector", [])),
            model=row.get("embedding_model", ""),
            dimensions=row.get("embedding_dim", 0),
        )

    def delete(self, chunk_id: str) -> bool:
        """Delete a chunk by ID. Returns True if a row was deleted."""
        if self._table is None:
            return False

        before = self._table.count_rows()
        try:
            self._table.delete(f"chunk_id = '{escape_sql_string_literal(chunk_id)}'")
        except Exception as exc:
            logger.warning("Delete failed for chunk %s: %s", chunk_id, exc)
            return False
        return self._table.count_rows() < before

    def delete_by_source(self, source_global_id: str) -> int:
        """Delete all chunks for a given source post. Returns count."""
        if self._table is None:
            return 0

        before = self._table.count_rows()
        try:
            self._table.delete(
                f"source_global_id = '{escape_sql_string_literal(source_global_id)}'"
            )
        except Exception as exc:
            logger.warning("Delete by source failed for %s: %s", source_global_id, exc)
            return 0
        return before - self._table.count_rows()

    def stats(self) -> dict[str, Any]:
        """Return summary statistics about the store.

        Earlier ``to_pandas()`` on the full table OOM'd on large
        stores (a 10M-chunk LanceDB table with 768-dim float32
        vectors is ~30 GB once materialised).  We now project to
        scalar columns only via ``search().select(...).to_list()``
        so the ``vector`` column is never copied out of LanceDB.
        Falls back to a row count if even the projected scan
        fails (e.g. backend doesn't support the API combination).
        """
        if self._table is None:
            return {"total_chunks": 0}

        try:
            total = self._table.count_rows()
            if total == 0:
                return {"total_chunks": 0}

            # Project only the cheap scalar columns.  The
            # ``search()`` API is the vector-similarity entry point;
            # called without a vector query it scans the table, and
            # ``select`` constrains the columns LanceDB materialises.
            rows = (
                self._table.search()
                .select(["source_global_id", "source_channel_id", "timestamp_unix"])
                .limit(total)
                .to_list()
            )

            unique_posts: set[str] = set()
            unique_channels: set[int] = set()
            earliest = None
            latest = None
            for row in rows:
                unique_posts.add(row["source_global_id"])
                unique_channels.add(row["source_channel_id"])
                ts = row["timestamp_unix"]
                if earliest is None or ts < earliest:
                    earliest = ts
                if latest is None or ts > latest:
                    latest = ts

            return {
                "total_chunks": total,
                "unique_posts": len(unique_posts),
                "unique_channels": len(unique_channels),
                "earliest_timestamp": int(earliest) if earliest is not None else 0,
                "latest_timestamp": int(latest) if latest is not None else 0,
                "db_path": str(self.db_path),
            }
        except Exception as exc:
            logger.warning("Stats query failed: %s", exc)
            return {"total_chunks": self.count(), "db_path": str(self.db_path)}
