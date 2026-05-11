# RAG Pipeline

## Overview

`heddle.contrib.rag` is a contrib module for processing document streams through a multi-stage pipeline: ingest, normalize, multiplex, chunk, embed, and analyze.

Supported sources: Telegram JSON exports, CSV files, and plain text files or directories. The module is designed for Persian/RTL text but works with any language.

## Quick Start (CLI)

The fastest way to use the RAG pipeline — no Python code needed:

```bash
# 1. Install and configure
uv sync --extra rag
uv run heddle setup                    # interactive wizard

# 2. Ingest your data — pick one of:
uv run heddle rag ingest /path/to/exports/result*.json                  # Telegram
uv run heddle rag ingest comments.csv --text-column body                # CSV
uv run heddle rag ingest /path/to/transcripts/                          # plain text dir

# 3. Search
uv run heddle rag search "earthquake damage reports" --limit 10

# 4. Check statistics
uv run heddle rag stats

# 5. Open the web dashboard
uv run heddle rag serve --port 8080
```

Use `--store lancedb` for ANN search on larger datasets:

```bash
uv sync --extra lancedb
uv run heddle rag --store lancedb ingest /path/to/exports/*.json
```

See the [CLI Reference](CLI_REFERENCE.md#heddle-rag-group) for all options.

## Ingesting CSV files

Use `--text-column` to point at the column carrying the document text.
Optional flags pick up extra metadata and stable identifiers.

```bash
# Public-comment dataset where each row is a comment in the `body` column
uv run heddle rag ingest comments.csv --text-column body

# Use a stable id column and keep author/topic as metadata
uv run heddle rag ingest comments.csv \
    --text-column body \
    --id-column comment_id \
    --metadata-columns author,topic

# Tab-separated values
uv run heddle rag ingest survey.tsv --text-column response --delimiter $'\t'
```

How a row becomes a `NormalizedPost`:

- `text_clean` ← the value of `--text-column`
- `message_id` ← the `--id-column` value (or row index if not given)
- `source_channel_name` ← CSV file stem; `source_channel_id` ← stable hash of the
  resolved file path (so multiple CSVs stay distinguishable downstream)
- `timestamp` ← file mtime (used for windowing)
- columns named in `--metadata-columns` are attached as extras

Rows with empty text are skipped. Files that aren't valid UTF-8 are
re-decoded with `errors="replace"` and a warning is logged.

## Ingesting plain text files

Pass either a single file or a directory. With a directory, the
default glob is `**/*.txt` (recursive); override with `--text-glob`.

```bash
# A single transcript
uv run heddle rag ingest transcript.txt

# A folder of transcripts (recursive)
uv run heddle rag ingest /path/to/transcripts/

# A folder of Markdown notes only, non-recursive
uv run heddle rag ingest /path/to/notes/ --text-glob "*.md"
```

How a file becomes a `NormalizedPost`:

- One file = one document. The whole file body is the `text_clean`.
- `message_id` ← stable hash of the resolved file path
- `source_channel_name` ← directory name (or parent directory of a single file)
- The file path, size, and mtime are attached as extras (`file_path`,
  `file_size`).

Empty files are skipped. Encoding fallback works the same as for CSV.

## Installation

```bash
uv sync --extra rag              # DuckDB vector store backend
uv sync --extra lancedb          # LanceDB vector store backend (ANN search)
uv sync --extra telegram         # Live Telegram capture via Telethon
```

Dependencies added by `[rag]`: `duckdb>=1.0.0`, `requests>=2.31.0`.
Dependencies added by `[lancedb]`: `lancedb>=0.15.0`, `pyarrow>=15.0.0`.
Dependencies added by `[telegram]`: `telethon>=1.36.0`.

For embedding generation you also need a running Ollama server with `nomic-embed-text` (or another model):

```bash
ollama pull nomic-embed-text
```

## Architecture

```text
                    ┌── Telegram JSON ──► TelegramIngestor ──┐
                    │                                        ├──► NormalizedPost[]
                    └── Telegram Live ──► TelegramLiveIngestor┘
                         (Telethon)              │
                      ┌──────────────────────────┘
                      ▼
                  StreamMux ──────────────► MuxedStream
                  (chronological merge       (entries sorted by timestamp,
                   + time windowing)          assigned to 6h windows)
                      │
                      ▼
              SentenceChunker ──────────► TextChunk[]
                      │
              ┌───────┴───────┐
              ▼               ▼
     VectorStore (ABC)      LLM Analysis Actors
     ├─ DuckDBVectorStore   (TrendAnalyzer, CorroborationFinder,
     └─ LanceDBVectorStore   AnomalyDetector, DataExtractor)
```

All ingestors extend the `Ingestor` ABC (`ingestion/base.py`). All vector stores
extend the `VectorStore` ABC (`vectorstore/base.py`). Backends are configurable
via `ingestor_class` and `store_class` parameters.

## Quick Start (Python API)

For programmatic access — use the RAG classes directly without CLI or infrastructure:

```python
from datetime import timedelta
from heddle.contrib.rag.ingestion.telegram_ingestor import TelegramIngestor
from heddle.contrib.rag.mux.stream_mux import merge_from_ingestors
from heddle.contrib.rag.schemas.mux import MuxWindowConfig
from heddle.contrib.rag.chunker.sentence_chunker import ChunkConfig, chunk_mux_entry
from heddle.contrib.rag.vectorstore.duckdb_store import DuckDBVectorStore

# 1. Ingest
ingestors = [
    TelegramIngestor("exports/channel_1.json", min_text_len=10).load(),
    TelegramIngestor("exports/channel_2.json", min_text_len=10).load(),
]

# 2. Multiplex
window_config = MuxWindowConfig(window_duration=timedelta(hours=6))
stream = merge_from_ingestors(ingestors, window_config=window_config)
print(f"{stream.total_entries} posts in {len(stream.window_ids)} windows")

# 3. Chunk
cfg = ChunkConfig(target_chars=400, max_chars=600)
chunks = []
for entry in stream.entries:
    chunks.extend(chunk_mux_entry(entry, config=cfg))

# 4. Store + Embed
store = DuckDBVectorStore(
    db_path="/tmp/my-rag.duckdb",
    embedding_model="nomic-embed-text",
).initialize()

stored = store.add_chunks(chunks)  # embeds via Ollama
print(f"Stored {stored} chunks")

# 5. Search
results = store.search("earthquake damage reports", limit=5)
for r in results:
    print(f"  [{r.score:.3f}] {r.text[:80]}...")

store.close()
```

## Module Reference

### Schemas (`heddle.contrib.rag.schemas`)

All data models are Pydantic v2 BaseModels.

| Schema | Module | Purpose |
|--------|--------|---------|
| `NormalizedPost` | `schemas.post` | Canonical post representation across all sources |
| `Language` | `schemas.post` | Enum: `fa`, `ar`, `en`, `mixed`, `unknown` |
| `ChannelBias` | `schemas.post` | Enum: `state_media`, `independent`, `fact_check`, etc. |
| `ChannelEditorProfile` | `schemas.post` | Trust weight + bias classification per channel |
| `RawTelegramMessage` | `schemas.telegram` | Raw Telegram JSON message (polymorphic text field) |
| `TelegramChannel` | `schemas.telegram` | Telegram channel container |
| `MuxWindowConfig` | `schemas.mux` | Tumbling/sliding window configuration |
| `MuxEntry` | `schemas.mux` | Single entry in a muxed stream (wraps NormalizedPost) |
| `MuxedStream` | `schemas.mux` | Complete multiplexed stream with window assignments |
| `TextChunk` | `schemas.chunk` | Chunked text segment with provenance |
| `ChunkStrategy` | `schemas.chunk` | Enum: `sentence`, `paragraph`, `whole_post`, `fixed_char` |
| `EmbeddedChunk` | `schemas.embedding` | Chunk with embedding vector |
| `SimilarityResult` | `schemas.embedding` | Search result with cosine similarity score |
| `TrendSignal` | `schemas.analysis` | Detected trend across channels |
| `CorroborationMatch` | `schemas.analysis` | Cross-channel corroboration |
| `AnomalyFlag` | `schemas.analysis` | Statistical or semantic anomaly |
| `ExtractedData` | `schemas.analysis` | Structured data extraction results |

### Ingestion (`heddle.contrib.rag.ingestion`)

**Ingestor ABC** — All ingestors extend `Ingestor` from `ingestion/base.py`:

```python
from heddle.contrib.rag.ingestion.base import Ingestor

class MyIngestor(Ingestor):
    def load(self) -> "MyIngestor": ...
    def ingest(self) -> Generator[NormalizedPost, None, None]: ...
    # ingest_all() is provided by the base class
```

**TelegramIngestor** — Parses Telegram Desktop JSON exports.

```python
from heddle.contrib.rag.ingestion.telegram_ingestor import TelegramIngestor

ingestor = TelegramIngestor(
    source_path="result.json",
    min_text_len=10,       # skip messages shorter than this
).load()

# Properties
ingestor.channel_id    # int
ingestor.channel_name  # str

# Get all posts
posts = ingestor.ingest_all()  # list[NormalizedPost]
```

Handles:

- Polymorphic text fields (string or list of text/entity objects)
- Reaction counts (emoji + paid)
- Forward detection
- Service message filtering
- Media type detection

**DEFAULT_PROFILES** — Pre-configured channel bias profiles:

```python
from heddle.contrib.rag.ingestion.telegram_ingestor import DEFAULT_PROFILES

# FarsNews: state_media, trust_weight=0.3
# Iranwire: independent, trust_weight=0.8
# Hamkelasi: educational, trust_weight=0.7
# Factnameh: fact_check, trust_weight=0.9
```

### Text Normalization (`heddle.contrib.rag.tools`)

**RTL Normalizer** — Persian/Arabic text normalization:

```python
from heddle.contrib.rag.tools.rtl_normalizer import normalize

result = normalize(text)
result.text          # normalized text
result.hashtags      # extracted hashtags
result.mentions      # extracted mentions
result.links         # extracted URLs
result.has_emoji     # bool
```

Features: ZWNJ preservation, Arabic-to-Persian character substitution (ي→ی, ك→ک), digit normalization (٠-٩→0-9, ۰-۹→0-9), emoji handling, Telegram footer stripping.

**Temporal Batcher** — Time-window utilities:

```python
from heddle.contrib.rag.tools.temporal_batcher import (
    tumbling_windows, sliding_windows, daily_windows, describe_windows,
)

# Any object with a .timestamp (datetime) attribute works
windows = tumbling_windows(items, duration=timedelta(hours=6))
windows = sliding_windows(items, duration=timedelta(hours=6), step=timedelta(hours=1))
windows = daily_windows(items, tz_offset_hours=3.5)  # Iran Standard Time
```

### Stream Multiplexer (`heddle.contrib.rag.mux`)

**StreamMux** — Merges multiple channel streams into chronological order with window assignment:

```python
from heddle.contrib.rag.mux.stream_mux import StreamMux, merge_from_ingestors

# Manual usage
mux = StreamMux()
mux.add_stream(channel_1_posts)
mux.add_stream(channel_2_posts)
stream = mux.merge(window_config=MuxWindowConfig())

# Convenience function (from ingestors directly)
stream = merge_from_ingestors(ingestors, window_config=config)

# Access results
stream.total_entries    # int
stream.channel_count    # int
stream.time_span_hours  # float
stream.window_ids       # list[str]
stream.entries          # list[MuxEntry]
stream.windows()        # dict[str, list[MuxEntry]]
stream.window("w1")     # list[MuxEntry] for specific window
```

### Chunker (`heddle.contrib.rag.chunker`)

**SentenceChunker** — Persian-aware text chunking:

```python
from heddle.contrib.rag.chunker.sentence_chunker import ChunkConfig, chunk_post, chunk_mux_entry

cfg = ChunkConfig(
    target_chars=400,     # soft target
    max_chars=600,        # hard maximum
    overlap_chars=50,     # overlap between chunks
    min_chars=20,         # minimum chunk length
)

# From NormalizedPost
chunks = chunk_post(post, config=cfg)

# From MuxEntry
chunks = chunk_mux_entry(entry, config=cfg)
```

Splitting strategy: paragraphs first, then sentences (handles Persian sentence-enders), then fixed-char fallback.

### Vector Store (`heddle.contrib.rag.vectorstore`)

**DuckDBVectorStore** — Embedded vector database:

```python
from heddle.contrib.rag.vectorstore.duckdb_store import DuckDBVectorStore

store = DuckDBVectorStore(
    db_path="/tmp/rag.duckdb",
    embedding_model="nomic-embed-text",
    ollama_url="http://localhost:11434",
).initialize()

# Embed and store chunks (calls Ollama)
count = store.add_chunks(text_chunks, batch_size=64)

# Store pre-embedded chunks
count = store.add_embedded_chunks(embedded_chunks)

# Semantic search
results = store.search("earthquake reports", limit=10, channel_ids=[1006939659])

# CRUD
chunk = store.get("1:5:0")
store.delete("1:5:0")
store.delete_by_source("1:5")

# Stats
stats = store.stats()
store.close()
```

Uses `list_cosine_similarity` (not `array_cosine_similarity`) because DuckDB `FLOAT[]` is variable-length.

**LanceDBVectorStore** — ANN vector database (faster search for large datasets):

```python
from heddle.contrib.lancedb.store import LanceDBVectorStore

store = LanceDBVectorStore(
    db_path="/tmp/rag.lance",
    embedding_model="nomic-embed-text",
    ollama_url="http://localhost:11434",
).initialize()

# Same API as DuckDBVectorStore — both extend VectorStore ABC
count = store.add_chunks(text_chunks, batch_size=64)
results = store.search("earthquake reports", limit=10)
store.close()
```

Install: `uv sync --extra lancedb`

**VectorStore ABC** — Both stores implement `VectorStore` from `vectorstore/base.py`. Use the `store_class` parameter on `VectorStoreBackend` to switch:

```yaml
# In worker YAML config
backend_config:
  store_class: "heddle.contrib.lancedb.store.LanceDBVectorStore"
  db_path: "/tmp/rag-vectors.lance"
```

### Live Telegram Capture (`heddle.contrib.rag.ingestion.telegram_live`)

**TelegramLiveIngestor** — Monitors Telegram channels in real-time via Telethon:

```python
from heddle.contrib.rag.ingestion.telegram_live import TelegramLiveIngestor

ingestor = TelegramLiveIngestor(
    channels=["@farsna", "@IranIntl_Fa"],
    api_id=12345,
    api_hash="your_api_hash",
)
await ingestor.start()

# Posts are buffered; drain with ingest()
for post in ingestor.ingest():
    process(post)

# Check status
print(ingestor.status())

await ingestor.stop()
```

Requires: `uv sync --extra telegram` and Telegram API credentials
(`TELEGRAM_API_ID`, `TELEGRAM_API_HASH` env vars).

### Analysis Actors (`heddle.contrib.rag.analysis`)

LLM-powered analysis actors for trend detection, cross-channel corroboration, anomaly detection, and data extraction.

```python
from heddle.contrib.rag.analysis.llm_analyzers import (
    LLMBackend, TrendAnalyzer, CorroborationFinder,
    AnomalyDetector, DataExtractor,
)

# LLM backend (auto-routes by model prefix)
backend = LLMBackend(
    model="ollama:llama3.2:3b",           # local via Ollama
    # model="anthropic:claude-sonnet-4-20250514",   # cloud via Anthropic
)

# Trend analysis
analyzer = TrendAnalyzer(backend)
signals = analyzer.analyze(window_entries, window_id="w1",
                           window_start=start, window_end=end)

# Corroboration (cross-channel)
finder = CorroborationFinder(backend)
matches = finder.analyze(window_entries, ...)

# Anomaly detection
detector = AnomalyDetector(backend)
flags = detector.analyze(window_entries, ...)

# Data extraction (statistics, names, dates, locations)
extractor = DataExtractor(backend)
data = extractor.analyze(window_entries, ...)
```

## Heddle Pipeline Integration

The RAG module includes Heddle backend wrappers and YAML configs for running as distributed Heddle workers.

### Backend Wrappers (`heddle.contrib.rag.backends`)

| Backend | Worker Type | Wraps |
|---------|------------|-------|
| `IngestorBackend` | processor | Any `Ingestor` subclass (default: TelegramIngestor, configurable via `ingestor_class`) |
| `MuxBackend` | processor | StreamMux |
| `ChunkerBackend` | processor | SentenceChunker |
| `VectorStoreBackend` | processor | Any `VectorStore` subclass (default: DuckDBVectorStore, configurable via `store_class`) |

### Worker Configs

Pre-built YAML configs in `configs/workers/`:

```text
rag_ingestor.yaml       # Telegram ingestion
rag_mux.yaml            # Stream multiplexing
rag_chunker.yaml        # Sentence chunking
rag_vectorstore.yaml         # Vector store operations (DuckDB)
rag_vectorstore_lance.yaml   # Vector store operations (LanceDB)
rag_trend_analyzer.yaml # LLM trend analysis
```

### Running with Heddle Infrastructure

```bash
# Start NATS
docker run -p 4222:4222 nats:latest

# Start router
heddle router --nats-url nats://localhost:4222

# Start workers
heddle processor --config configs/workers/rag_ingestor.yaml --nats-url nats://localhost:4222
heddle processor --config configs/workers/rag_chunker.yaml --nats-url nats://localhost:4222
heddle processor --config configs/workers/rag_vectorstore.yaml --nats-url nats://localhost:4222

# Start pipeline
heddle pipeline --config configs/orchestrators/rag_pipeline.yaml --nats-url nats://localhost:4222

# Submit work
heddle submit "Ingest telegram export" \
  --context source_path=/data/exports/result-1.json \
  --nats-url nats://localhost:4222
```

### Pipeline Config

`configs/orchestrators/rag_pipeline.yaml` defines a 3-stage pipeline:

```text
ingest → chunk → vectorize
```

Each stage maps outputs to the next stage's inputs via `input_mapping`.

## Adding a New Source (non-Telegram)

To support a new data source, extend the `Ingestor` ABC:

```python
from heddle.contrib.rag.ingestion.base import Ingestor
from heddle.contrib.rag.schemas.post import NormalizedPost, Language

class MyIngestor(Ingestor):
    def __init__(self, source_path: str):
        self._path = source_path
        self._posts: list[NormalizedPost] = []

    def load(self) -> "MyIngestor":
        # Parse your data format
        for item in self._parse_source():
            self._posts.append(NormalizedPost(
                global_id=f"{self.channel_id}:{item['id']}",
                source_channel_id=self.channel_id,
                source_channel_name=self.channel_name,
                message_id=item["id"],
                timestamp=item["datetime"],  # timezone-aware datetime
                text_clean=item["text"],
                language=Language.PERSIAN,
            ))
        return self

    def ingest(self):
        yield from self._posts

    @property
    def channel_id(self) -> int: ...
    @property
    def channel_name(self) -> str: ...
```

The `Ingestor` ABC requires:

1. `load()` — parse source, return `self`
2. `ingest()` — yield `NormalizedPost` objects
3. `ingest_all()` is provided by the base class

Use with `IngestorBackend` by setting `ingestor_class` in the worker config:

```yaml
backend_config:
  ingestor_class: "mypackage.my_ingestor.MyIngestor"
```

## Testing

```bash
# Run all RAG tests
uv run pytest tests/contrib/rag/ -v

# Run LanceDB tests (requires lancedb installed)
uv run pytest tests/contrib/lancedb/ -v

# Run RAG Workshop tests
uv run pytest tests/test_workshop_rag.py -v

# Run specific module tests
uv run pytest tests/contrib/rag/test_schemas.py -v
uv run pytest tests/contrib/rag/test_ingestion.py -v
uv run pytest tests/contrib/rag/test_abstractions.py -v
uv run pytest tests/contrib/rag/test_telegram_live.py -v
uv run pytest tests/contrib/rag/test_mux.py -v
uv run pytest tests/contrib/rag/test_chunker.py -v
uv run pytest tests/contrib/rag/test_tools.py -v
uv run pytest tests/contrib/rag/test_backends.py -v
```

All tests run without infrastructure (no NATS, no Ollama, no DuckDB server). DuckDB runs in-memory for tests. LanceDB tests are skipped if `lancedb` is not installed.

## Demo

```bash
# Run the full pipeline with real Telegram data (no embeddings)
python examples/rag_demo.py

# With embeddings (requires Ollama running)
python examples/rag_demo.py --embed
```

## File Layout

```text
src/heddle/contrib/rag/
  __init__.py
  backends.py                    # Heddle SyncProcessingBackend wrappers (configurable classes)
  schemas/
    __init__.py                  # Re-exports all public schemas
    post.py                      # NormalizedPost, Language, ChannelBias
    telegram.py                  # RawTelegramMessage, TelegramChannel
    mux.py                       # MuxWindowConfig, MuxEntry, MuxedStream
    chunk.py                     # TextChunk, ChunkStrategy
    embedding.py                 # EmbeddedChunk, SimilarityResult
    analysis.py                  # TrendSignal, AnomalyFlag, ExtractedData, etc.
  ingestion/
    __init__.py
    base.py                      # Ingestor ABC
    telegram_ingestor.py         # TelegramIngestor(Ingestor) + DEFAULT_PROFILES
    telegram_live.py             # TelegramLiveIngestor(Ingestor) — Telethon live capture
    normalize.py                 # Shared normalization utilities
  mux/
    __init__.py
    stream_mux.py                # StreamMux, merge_from_ingestors
  chunker/
    __init__.py
    sentence_chunker.py          # ChunkConfig, chunk_post, chunk_mux_entry
  vectorstore/
    __init__.py
    base.py                      # VectorStore ABC
    duckdb_store.py              # DuckDBVectorStore(VectorStore) — exact cosine similarity
  analysis/
    __init__.py
    llm_analyzers.py             # LLMBackend, TrendAnalyzer, etc.
  tools/
    __init__.py
    rtl_normalizer.py            # normalize(), Persian/RTL text processing
    temporal_batcher.py          # tumbling_windows, sliding_windows, daily_windows

src/heddle/contrib/lancedb/
  __init__.py
  store.py                       # LanceDBVectorStore(VectorStore) — ANN search
  tool.py                        # LanceDBVectorTool — LLM function-calling tool
```
