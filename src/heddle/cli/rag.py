"""
Zero-config RAG pipeline CLI.

Runs the full RAG pipeline directly — no NATS, no router, no worker actors.
Uses classes from heddle.contrib.rag with sensible defaults.

Commands::

    heddle rag ingest <paths>...   # Ingest Telegram JSON, CSV, or plain text
    heddle rag search <query>      # Semantic search
    heddle rag stats               # Store statistics
    heddle rag serve               # Start Workshop with RAG dashboard
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import click

from heddle.cli.config import DEFAULT_CONFIG_PATH, HeddleConfig, resolve_config

# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------


def _resolve_store_class(config: HeddleConfig) -> str:
    """Return the dotted class path for the configured vector store."""
    if config.rag_vector_store == "lancedb":
        return "heddle.contrib.lancedb.store.LanceDBVectorStore"
    return "heddle.contrib.rag.vectorstore.duckdb_store.DuckDBVectorStore"


def _resolve_embedding_settings(config: HeddleConfig) -> dict[str, Any]:
    """Pick the embedding backend + URL + model for the RAG pipeline.

    Selection rules (highest wins):

    1. Explicit ``embedding_backend`` in config / ``HEDDLE_EMBEDDING_BACKEND``
       env var (``"ollama"`` or ``"openai-compatible"``).
    2. ``local_backend`` preference (``HEDDLE_LOCAL_BACKEND``) when set.
    3. LM Studio URL when configured (newer default).
    4. Ollama URL when configured.
    5. Fall back to Ollama defaults.

    TODO(local-runtime-registry): once the ``LocalRuntime`` registry
    lands in ``heddle.worker.backends``, replace this branching with a
    lookup that reads ``embedding_provider`` and the URL straight from
    the chosen runtime entry.  No more separate
    ``HEDDLE_EMBEDDING_BACKEND`` knob needed for the common case.
    """
    backend = (config.embedding_backend or "").strip().lower() or None

    if backend is None:
        local_pref = (config.local_backend or "").strip().lower() or None
        if local_pref == "lmstudio" and config.lm_studio_url:
            backend = "openai-compatible"
        elif local_pref == "ollama" and config.ollama_url:
            backend = "ollama"
        elif config.lm_studio_url:
            backend = "openai-compatible"
        else:
            backend = "ollama"

    if backend == "openai-compatible":
        url = config.lm_studio_url or "http://localhost:1234/v1"
        # When the user hasn't customised the model away from Ollama's
        # default, switch to a sensible LM Studio embedding model.
        model = (
            config.embedding_model
            if config.embedding_model and config.embedding_model != "nomic-embed-text"
            else "text-embedding-nomic-embed-text-v1.5"
        )
        return {
            "embedding_backend": "openai-compatible",
            "embedding_url": url,
            "embedding_model": model,
            "ollama_url": config.ollama_url or "http://localhost:11434",
        }

    return {
        "embedding_backend": "ollama",
        "embedding_url": None,
        "embedding_model": config.embedding_model,
        "ollama_url": config.ollama_url or "http://localhost:11434",
    }


def _open_store(config: HeddleConfig) -> Any:
    """Instantiate and initialize a VectorStore from config."""
    import importlib

    class_path = _resolve_store_class(config)
    module_path, class_name = class_path.rsplit(".", 1)
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)

    db_path = str(Path(config.rag_db_path).expanduser())
    settings = _resolve_embedding_settings(config)
    store = cls(db_path=db_path, **settings)
    return store.initialize()


# ---------------------------------------------------------------------------
# RAG command group
# ---------------------------------------------------------------------------


@click.group()
@click.option("--config-path", default=DEFAULT_CONFIG_PATH, help="Config file path.")
@click.option("--db-path", default=None, help="Override vector store path.")
@click.option("--store", default=None, type=click.Choice(["duckdb", "lancedb"]))
@click.option("--ollama-url", default=None, help="Override Ollama URL.")
@click.option(
    "--lm-studio-url",
    default=None,
    help="Override LM Studio URL (e.g. http://localhost:1234/v1).",
)
@click.option(
    "--embedding-backend",
    default=None,
    type=click.Choice(["ollama", "openai-compatible"]),
    help="Force a specific embedding backend (otherwise auto-selected).",
)
@click.option("--embedding-model", default=None, help="Override embedding model.")
@click.pass_context
def rag(
    ctx: click.Context,
    config_path: str,
    db_path: str | None,
    store: str | None,
    ollama_url: str | None,
    lm_studio_url: str | None,
    embedding_backend: str | None,
    embedding_model: str | None,
) -> None:
    """RAG pipeline — ingest, search, and serve. No NATS needed."""
    overrides: dict[str, Any] = {}
    if db_path:
        overrides["rag_db_path"] = db_path
    if store:
        overrides["rag_vector_store"] = store
    if ollama_url:
        overrides["ollama_url"] = ollama_url
    if lm_studio_url:
        overrides["lm_studio_url"] = lm_studio_url
    if embedding_backend:
        overrides["embedding_backend"] = embedding_backend
    if embedding_model:
        overrides["embedding_model"] = embedding_model
    ctx.ensure_object(dict)
    ctx.obj["config"] = resolve_config(cli_overrides=overrides, config_path=config_path)


# ---------------------------------------------------------------------------
# heddle rag ingest
# ---------------------------------------------------------------------------


@rag.command()
@click.argument("paths", nargs=-1, required=True, type=click.Path(exists=True))
@click.option("--embed/--no-embed", default=True, help="Generate embeddings via Ollama.")
@click.option("--window-hours", default=6, type=int, help="Time window size in hours.")
@click.option("--chunk-target", default=400, type=int, help="Target chunk size in chars.")
@click.option("--chunk-max", default=600, type=int, help="Maximum chunk size in chars.")
@click.option(
    "--text-column",
    default=None,
    help="CSV: column with document text (required for .csv inputs).",
)
@click.option("--id-column", default=None, help="CSV: column for stable doc IDs.")
@click.option(
    "--metadata-columns",
    default=None,
    help="CSV: comma-separated columns to keep as metadata.",
)
@click.option("--delimiter", default=",", help="CSV: field delimiter.")
@click.option(
    "--text-glob",
    default="**/*.txt",
    help="Plain text: glob pattern when path is a directory.",
)
@click.pass_context
def ingest(  # noqa: PLR0915
    ctx: click.Context,
    paths: tuple[str, ...],
    embed: bool,
    window_hours: int,
    chunk_target: int,
    chunk_max: int,
    text_column: str | None,
    id_column: str | None,
    metadata_columns: str | None,
    delimiter: str,
    text_glob: str,
) -> None:
    """Ingest documents into the vector store.

    Supports Telegram JSON exports, CSV files, and plain text files or
    directories.
    """
    from datetime import timedelta

    from heddle.contrib.rag.chunker.sentence_chunker import ChunkConfig, chunk_mux_entry
    from heddle.contrib.rag.ingestion.csv_ingestor import CsvIngestor
    from heddle.contrib.rag.ingestion.telegram_ingestor import TelegramIngestor
    from heddle.contrib.rag.ingestion.text_ingestor import PlainTextIngestor
    from heddle.contrib.rag.mux.stream_mux import merge_from_ingestors
    from heddle.contrib.rag.schemas.mux import MuxWindowConfig

    config: HeddleConfig = ctx.obj["config"]

    click.echo()
    click.echo(click.style("  Heddle RAG — Ingest", fg="cyan", bold=True))
    click.echo()

    metadata_cols = (
        [c.strip() for c in metadata_columns.split(",") if c.strip()] if metadata_columns else None
    )

    # Step 1: Ingest
    click.echo("  [1/4] Loading sources...")
    ingestors = []
    for path in paths:
        p = Path(path)
        suffix = p.suffix.lower()

        t0 = time.perf_counter()
        if p.is_dir():
            ingestor = PlainTextIngestor(p, glob=text_glob).load()
        elif suffix == ".json":
            ingestor = TelegramIngestor(str(p), min_text_len=10).load()
        elif suffix == ".csv":
            if not text_column:
                raise click.ClickException(
                    f"CSV file '{p}' requires --text-column to identify the text field."
                )
            ingestor = CsvIngestor(
                source_path=p,
                text_column=text_column,
                id_column=id_column,
                metadata_columns=metadata_cols,
                delimiter=delimiter,
            ).load()
        elif suffix in (".txt", ".md"):
            ingestor = PlainTextIngestor(p).load()
        else:
            raise click.ClickException(
                f"Unsupported file type for '{p}': {suffix or '<no extension>'}. "
                "Supported: .json, .csv, .txt, .md, or a directory."
            )

        posts = ingestor.ingest_all()
        elapsed = time.perf_counter() - t0
        click.echo(f"    {ingestor.channel_name:<30} {len(posts):>5} posts  ({elapsed:.2f}s)")
        ingestors.append(ingestor)

    if not ingestors:
        raise click.ClickException("No valid sources found.")

    # Step 2: Multiplex
    click.echo(f"\n  [2/4] Multiplexing ({window_hours}h windows)...")
    t0 = time.perf_counter()
    window_config = MuxWindowConfig(window_duration=timedelta(hours=window_hours))
    stream = merge_from_ingestors(ingestors, window_config=window_config)
    elapsed = time.perf_counter() - t0
    click.echo(
        f"    {stream.total_entries} entries, {len(stream.window_ids)} windows, "
        f"{stream.channel_count} channels  ({elapsed:.3f}s)"
    )

    # Step 3: Chunk
    click.echo(f"\n  [3/4] Chunking (target={chunk_target}, max={chunk_max})...")
    chunk_cfg = ChunkConfig(target_chars=chunk_target, max_chars=chunk_max)
    all_chunks = []
    t0 = time.perf_counter()
    for entry in stream.entries:
        all_chunks.extend(chunk_mux_entry(entry, config=chunk_cfg))
    elapsed = time.perf_counter() - t0
    avg_len = sum(len(c.text) for c in all_chunks) / max(len(all_chunks), 1)
    click.echo(f"    {len(all_chunks)} chunks, avg {avg_len:.0f} chars  ({elapsed:.3f}s)")

    # Step 4: Store
    db_path_display = str(Path(config.rag_db_path).expanduser())
    click.echo(f"\n  [4/4] Storing in {config.rag_vector_store} ({db_path_display})...")

    if embed:
        store = _open_store(config)
        t0 = time.perf_counter()
        count = store.add_chunks(all_chunks, batch_size=64)
        elapsed = time.perf_counter() - t0
        click.echo(f"    Embedded & stored: {count} chunks ({elapsed:.1f}s)")
        store.close()
    else:
        from heddle.contrib.rag.schemas.embedding import EmbeddedChunk

        store = _open_store(config)
        embedded = [
            EmbeddedChunk(
                chunk_id=c.chunk_id,
                source_global_id=c.source_global_id,
                source_channel_id=c.source_channel_id,
                text=c.text,
                embedding=[],
                model="none",
                dimensions=0,
            )
            for c in all_chunks
        ]
        t0 = time.perf_counter()
        count = store.add_embedded_chunks(embedded)
        elapsed = time.perf_counter() - t0
        click.echo(f"    Stored (no embeddings): {count} chunks ({elapsed:.1f}s)")
        click.echo("    Tip: Use --embed to generate embeddings via Ollama")
        store.close()

    # Summary
    click.echo()
    click.echo(
        click.style(
            f"  ✓ {stream.channel_count} channels → {stream.total_entries} posts → "
            f"{len(all_chunks)} chunks → {config.rag_vector_store}",
            fg="green",
        )
    )
    click.echo()


# ---------------------------------------------------------------------------
# heddle rag search
# ---------------------------------------------------------------------------


@rag.command()
@click.argument("query")
@click.option("--limit", "-n", default=10, type=int, help="Max results.")
@click.option("--min-score", default=0.0, type=float, help="Minimum similarity score.")
@click.pass_context
def search(ctx: click.Context, query: str, limit: int, min_score: float) -> None:
    """Search the vector store."""
    config: HeddleConfig = ctx.obj["config"]
    store = _open_store(config)

    try:
        results = store.search(query, limit=limit, min_score=min_score)
    finally:
        store.close()

    if not results:
        click.echo("No results found.")
        return

    click.echo()
    click.echo(click.style(f"  {len(results)} result(s) for: {query}", fg="cyan", bold=True))
    click.echo()

    for i, r in enumerate(results, 1):
        score_color = "green" if r.score >= 0.7 else "yellow" if r.score >= 0.4 else "red"
        click.echo(click.style(f"  [{r.score:.3f}]", fg=score_color) + f"  #{i}")
        click.echo(f"    Source: {r.source_global_id}  Channel: {r.source_channel_id}")
        # Truncate text to 200 chars for display
        text = r.text.replace("\n", " ")
        if len(text) > 200:
            text = text[:200] + "..."
        click.echo(f"    {text}")
        click.echo()


# ---------------------------------------------------------------------------
# heddle rag stats
# ---------------------------------------------------------------------------


@rag.command()
@click.pass_context
def stats(ctx: click.Context) -> None:
    """Show vector store statistics."""
    config: HeddleConfig = ctx.obj["config"]
    store = _open_store(config)

    try:
        store_stats = store.stats()
    finally:
        store.close()

    click.echo()
    click.echo(click.style("  Vector Store Statistics", fg="cyan", bold=True))
    click.echo(f"  Store: {config.rag_vector_store} ({config.rag_db_path})")
    click.echo()

    for k, v in store_stats.items():
        click.echo(f"    {k}: {v}")
    click.echo()


# ---------------------------------------------------------------------------
# heddle rag serve
# ---------------------------------------------------------------------------


@rag.command()
@click.option("--port", default=8080, type=int, help="Workshop port.")
@click.option("--host", default="127.0.0.1", help="Bind address.")
@click.pass_context
def serve(ctx: click.Context, port: int, host: str) -> None:
    """Start Workshop with RAG dashboard pre-configured."""
    config: HeddleConfig = ctx.obj["config"]

    # Apply config to env so build_backends_from_env() picks up keys
    from heddle.cli.config import apply_config_to_env

    apply_config_to_env(config)

    from heddle.workshop.app import create_app

    store_class = _resolve_store_class(config) if config.rag_vector_store != "duckdb" else None

    app = create_app(
        rag_db_path=str(Path(config.rag_db_path).expanduser()),
        rag_store_class=store_class,
        host=host,
        port=port,
    )

    click.echo()
    click.echo(click.style("  Heddle RAG — Workshop", fg="cyan", bold=True))
    click.echo(f"  Store: {config.rag_vector_store} ({config.rag_db_path})")
    click.echo(f"  URL:   http://{host}:{port}/rag")
    click.echo()

    import uvicorn

    uvicorn.run(app, host=host, port=port)
