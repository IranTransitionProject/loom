# Worker

The `heddle.worker` package implements the two types of Heddle workers:

- **LLM Workers** (`runner.py`) — call language models with system prompts,
  tool-use loops, and JSON parsing. Used for summarization, classification,
  extraction, and analysis tasks.
- **Processor Workers** (`processor.py`) — run arbitrary Python code (no LLM).
  Used for data transformation, ingestion, and integration tasks.

Both types are stateless: they process one task, return a result, and reset.

See [Building Workflows](../building-workflows.md) for the user-facing guide.

## Base

Abstract base class for all workers (`TaskWorker`).

::: heddle.worker.base

## Runner

`LLMWorker` — the main LLM worker actor. Includes `execute_with_tools()`,
the standalone tool-use loop shared with the Workshop test bench.

::: heddle.worker.runner

## Backends

LLM backend implementations: `AnthropicBackend`, `OllamaBackend`,
`OpenAICompatibleBackend`. Plus `build_backends_from_env()` for automatic
backend detection from environment variables and `~/.heddle/config.yaml`.

::: heddle.worker.backends

## Processor

`ProcessorWorker` and `SyncProcessingBackend` ABC for non-LLM workers.
Includes `serialize_writes` option and `BackendError` hierarchy.

::: heddle.worker.processor

## Tools

`ToolProvider` ABC and `SyncToolProvider` for LLM function-calling tools.
Workers can expose tools that the LLM calls during processing (max 10 rounds).

::: heddle.worker.tools

## Knowledge

Knowledge silo loading, injection into system prompts, and write-back.
Supports read-only, read-write, and tool-based knowledge sources.

::: heddle.worker.knowledge

## Embeddings

`EmbeddingProvider` ABC and `OllamaEmbeddingProvider` for text embedding
generation via Ollama's `/api/embed` endpoint.

::: heddle.worker.embeddings
