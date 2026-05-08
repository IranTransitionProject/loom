# Contrib Modules

The `heddle.contrib` package contains optional integrations that extend Heddle's
capabilities. Each module requires its own optional dependency extra.

| Module | Extra | Purpose |
|--------|-------|---------|
| `contrib.council` | `council` | Multi-round agent deliberation framework |
| `contrib.chatbridge` | `chatbridge` | External chat/LLM session adapters |
| `contrib.duckdb` | `duckdb` | Embedded analytics and vector search |
| `contrib.lancedb` | `lancedb` | ANN vector search via LanceDB |
| `contrib.redis` | `redis` | Production checkpoint persistence |
| `contrib.rag` | `rag` | Social media stream RAG pipeline |

See [Council How-To](../council-howto.md) for the council and chatbridge guide.
See [RAG How-To](../rag-howto.md) for the RAG pipeline guide.

## Council — Multi-Round Deliberation

Run structured team discussions where multiple LLM agents debate
iteratively with pluggable protocols and convergence detection.

::: heddle.contrib.council.runner
    options:
      members: [CouncilRunner]

::: heddle.contrib.council.config
    options:
      members: [CouncilConfig, load_council_config]

::: heddle.contrib.council.schemas
    options:
      members: [AgentConfig, CouncilResult, ConvergenceResult, TranscriptEntry]

## ChatBridge — External Chat Adapters

Session-aware adapters for Claude, OpenAI, Ollama, and human-in-the-loop
participation. Each adapter maintains per-session conversation history.

::: heddle.contrib.chatbridge.base
    options:
      members: [ChatBridge, ChatResponse, SessionInfo]

::: heddle.contrib.chatbridge.anthropic
    options:
      members: [AnthropicChatBridge]

::: heddle.contrib.chatbridge.openai
    options:
      members: [OpenAIChatBridge]

::: heddle.contrib.chatbridge.ollama
    options:
      members: [OllamaChatBridge]

::: heddle.contrib.chatbridge.manual
    options:
      members: [ManualChatBridge]

::: heddle.contrib.chatbridge.worker
    options:
      members: [ChatBridgeBackend]

## Valkey/Redis Store

Production checkpoint store using Redis/Valkey. Replaces the default
in-memory store for persistent orchestrator checkpoints.

::: heddle.contrib.redis.store

## DuckDB Query Backend

Action-dispatch query backend for DuckDB. Supports full-text search,
filtering, statistics, single-row get, and vector similarity search.

::: heddle.contrib.duckdb.query_backend

## DuckDB View Tool

Read-only DuckDB view exposed as an LLM-callable tool. Workers can query
structured data during processing.

::: heddle.contrib.duckdb.view_tool

## DuckDB Vector Tool

Semantic similarity search via DuckDB embeddings, exposed as an LLM tool.

::: heddle.contrib.duckdb.vector_tool

## LanceDB Vector Store

ANN vector storage and search via LanceDB. Faster than DuckDB for large
datasets. Implements the `VectorStore` ABC.

::: heddle.contrib.lancedb.store

## LanceDB Vector Tool

Semantic similarity search via LanceDB, exposed as an LLM tool.

::: heddle.contrib.lancedb.tool
