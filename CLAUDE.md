# CLAUDE.md — Heddle

## What this is

Heddle is an actor-based framework for orchestrating multiple LLM agents via NATS messaging. It replaces monolithic LLM contexts with narrowly-scoped **stateless workers** coordinated through a message bus.

See `docs/ARCHITECTURE.md` for the module map, `docs/DESIGN_INVARIANTS.md` before structural changes, `docs/CODING_GUIDE.md` for standards, and `docs/TROUBLESHOOTING.md` for common issues.

## Project layout

```text
src/heddle/
  core/         # messages, config, contracts, workspace
  worker/       # LLMWorker, ProcessorWorker, backends, tools, knowledge
  orchestrator/ # OrchestratorActor, PipelineOrchestrator, decomposer, synthesizer
  scheduler/    # SchedulerActor (cron + interval dispatch)
  router/       # deterministic router, token-bucket rate limiter, dead-letter
  bus/          # MessageBus ABC — InMemoryBus (tests), NATSBus (production)
  mcp/          # MCP gateway (FastMCP 3.x), Workshop + session bridges
  workshop/     # Worker lifecycle web UI (FastAPI + HTMX + Jinja2)
  contrib/      # duckdb, lancedb, redis, rag, docproc, council, chatbridge, subprocess
  cli/          # Click CLI entry points
configs/        # Worker/orchestrator/scheduler/MCP/council YAML configs
tests/          # 82 test files, 1844 unit tests (90% coverage)
```

## Non-negotiable design rules

- **Workers are stateless.** `reset()` runs after every task. No state carries between tasks.
- **Router is deterministic.** No LLM in the routing path. Routes by `worker_type` + `model_tier` from `configs/router_rules.yaml`. Unroutable → `heddle.tasks.dead_letter`.
- **Three model tiers:** `local` (LM Studio or Ollama), `standard` (Claude Sonnet), `frontier` (Claude Opus). When both `LM_STUDIO_URL` and `OLLAMA_URL` are set, LM Studio wins; override with `HEDDLE_LOCAL_BACKEND=ollama`.
- **Typed Pydantic messages only.** `TaskMessage`, `TaskResult`, `OrchestratorGoal` from `core/messages.py`. No raw dicts between actors.
- **Strict I/O contracts.** Per-worker JSON Schema in YAML, or via `input_schema_ref`/`output_schema_ref` (Pydantic dotted path → JSON Schema via `config.resolve_schema_refs()` at load time).
- **InMemoryBus for all unit tests.** Tests must not require NATS. Tests using `NATSBus` directly need `@pytest.mark.integration`.

## What NOT to do

- Don't add shared mutable state between workers. Workers are isolated actors.
- Don't put LLM logic in the router. Deterministic routing only.
- Don't merge worker configs into a monolithic prompt. Each worker stays narrow.
- Don't skip I/O contract validation — it's the only safety net between actors.
- Don't import `heddle.contrib.*` from core modules. Core ← contrib direction only.

## NATS subject conventions

```text
heddle.tasks.incoming               # router picks up tasks
heddle.tasks.{worker_type}.{tier}   # workers subscribe with queue groups
heddle.tasks.dead_letter            # unroutable / rate-limited tasks
heddle.results.{goal_id}            # results back to orchestrators
heddle.results.default              # results from standalone tasks
heddle.goals.incoming               # top-level goals for orchestrators
heddle.control.reload               # config hot-reload broadcast
```

## Non-obvious backend behaviours

- **OpenAI-compatible base URL:** `OpenAICompatibleBackend` strips a trailing `/v1` from base URLs so `/v1/chat/completions` doesn't double up. Providers like LM Studio document `http://host:port/v1` — pass that directly, it normalizes.
- **Thinking-model content rescue:** qwen3.x, deepseek-r1, and similar models put their answer on `message.reasoning_content` (leaving `content` empty). `OpenAICompatibleBackend` and `OpenAIChatBridge` fall back to `reasoning_content` automatically and log `reasoning_content.rescue` at info level.
- **Ollama think tags:** Ollama-served thinking models embed `<think>…</think>` inline in `content` rather than splitting it. `OllamaBackend` passes content through unmodified — agents see the tags.

## Session-starter queue

`session-starters/` (gitignored) holds the user's queue of design-chat starters and Claude Code prompts. One file per queued session, sortable-letter-prefixed (`A-…`, `B-…`). Read for context when the user references "the next session" or a specific letter; never commit; never echo contents into commit messages verbatim.

## Build and test

```bash
uv sync --all-extras                                             # Python 3.11+ required
uv run pytest tests/ -v -m "not integration and not deepeval"   # unit tests, no infra
uv run pytest tests/ -v -m integration                          # needs NATS running
uv run ruff check src/ tests/                                    # lint
uv run ruff format --check src/ tests/                          # format check
uv run heddle validate configs/workers/*.yaml                   # validate worker configs
uv run heddle new worker                                        # scaffold new worker config
HEDDLE_TRACE=1 uv run heddle pipeline ...                       # full I/O debug logging
```

Full CLI: `uv run heddle --help`
MCP gateway: `docs/building-workflows.md` Part 11
Workshop: `docs/workshop.md`
Kubernetes: `docs/KUBERNETES.md`
App deployment: `docs/APP_DEPLOYMENT.md`
