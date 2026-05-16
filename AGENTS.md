# AGENTS.md — Heddle

Heddle is the canonical Python runtime for the `getheddle` project
family: an actor-based framework for orchestrating multiple LLM agents
via NATS. It replaces monolithic LLM contexts with narrowly-scoped
**stateless workers** coordinated through a message bus.

This file is the source of truth for agent guidance in *this* repo.
Cross-repo guidance, invariants, philosophy, and the wire-protocol
contract live in
**[`heddle-agent-toolkit/`](../heddle-agent-toolkit/)** —
read those before structural work.

## Toolkit install

The toolkit is sibling to this repo. To populate `.claude/skills/` and
`.claude/agents/` from a fresh clone:

```bash
git clone https://github.com/getheddle/heddle-agent-toolkit.git ../heddle-agent-toolkit
../heddle-agent-toolkit/install.sh .
```

Until the toolkit is published, contributors will need a local sibling
checkout. The skills (`/heddle-orient`, `/heddle-preflight`, etc.) and
subagents (`heddle-architect`, `heddle-invariant-guard`,
`heddle-contract-reviewer`) named in this doc all come from there.

## Read first

### From the toolkit (shared across `getheddle/*`)

- `heddle-agent-toolkit/anchors/ECOSYSTEM.md` — where this repo sits.
- `heddle-agent-toolkit/anchors/PHILOSOPHY.md` — design opinions.
- `heddle-agent-toolkit/anchors/INVARIANTS.md` — non-negotiable rules
  (and the cross-repo additions C1–C7).
- `heddle-agent-toolkit/anchors/CONTRACT_MAP.md` — wire protocol,
  subjects, schema flow.

### From this repo (heddle-specific)

- `docs/ARCHITECTURE.md` — module map.
- `docs/DESIGN_INVARIANTS.md` — 21 framework-internal invariants
  (canonical detail; toolkit INVARIANTS.md points here).
- `docs/APPLICATION_PATTERNS.md` — blind audit, knowledge silos,
  behavioural-monitor isolation (application-level, not framework).
- `docs/CODING_GUIDE.md` — Python style and standards.
- `docs/TROUBLESHOOTING.md` — common issues and resolutions.

## Project layout

```text
src/heddle/
  core/         messages, config, contracts, workspace
  worker/       LLMWorker, ProcessorWorker, backends, tools, knowledge
  orchestrator/ OrchestratorActor, PipelineOrchestrator, decomposer, synthesizer
  scheduler/    SchedulerActor (cron + interval dispatch)
  router/       deterministic router, token-bucket rate limiter, dead-letter
  bus/          MessageBus ABC — InMemoryBus (tests), NATSBus (production)
  mcp/          MCP gateway (FastMCP 3.x), Workshop + session bridges
  workshop/     Worker lifecycle web UI (FastAPI + HTMX + Jinja2)
  contrib/      duckdb, lancedb, redis, rag, docproc, council, chatbridge, subprocess
  cli/          Click CLI entry points
configs/        Worker/orchestrator/scheduler/MCP/council YAML configs
tests/          unit tests (91%+ coverage, no NATS required)
```

## Three model tiers (heddle-specific configuration)

`local` (LM Studio or Ollama), `standard` (Claude Sonnet), `frontier`
(Claude Opus). When both `LM_STUDIO_URL` and `OLLAMA_URL` are set, LM
Studio wins; override with `HEDDLE_LOCAL_BACKEND=ollama`.

## Non-obvious backend behaviours

These are runtime quirks that don't fit elsewhere and are easy to miss.

- **OpenAI-compatible base URL:** `OpenAICompatibleBackend` strips a
  trailing `/v1` from base URLs so `/v1/chat/completions` doesn't double
  up. Providers like LM Studio document `http://host:port/v1` — pass
  that directly, it normalizes.
- **Thinking-model content rescue:** qwen3.x, deepseek-r1, and similar
  models put their answer on `message.reasoning_content` (leaving
  `content` empty). `OpenAICompatibleBackend` and `OpenAIChatBridge` fall
  back to `reasoning_content` automatically and log
  `reasoning_content.rescue` at info level.
- **Ollama think tags:** Ollama-served thinking models embed
  `<think>…</think>` inline in `content` rather than splitting it.
  `OllamaBackend` passes content through unmodified — agents see the
  tags.

## Build and test

```bash
uv sync --all-extras                                             # Python 3.11+
uv run pytest tests/ -v -m "not integration and not deepeval"   # unit, no infra
uv run pytest tests/ -v -m integration                          # needs NATS
uv run ruff check src/ tests/                                    # lint
uv run ruff format --check src/ tests/                          # format check
uv run pyright src/                                              # type check (strict)
uv run heddle validate configs/workers/*.yaml                   # validate configs
uv run heddle new worker                                        # scaffold a worker
HEDDLE_PIPELINE_VERBOSE=1 uv run heddle pipeline ...            # full pipeline payload logging (legacy alias: HEDDLE_TRACE)
```

The toolkit's `/heddle-preflight` skill runs the standard pre-commit
subset and reports pass/fail.

## Repo pointers (deep docs)

- Full CLI: `uv run heddle --help`
- MCP gateway: `docs/building-workflows.md` Part 12
- Workshop UI: `docs/workshop.md`
- Kubernetes: `docs/KUBERNETES.md`
- App deployment: `docs/APP_DEPLOYMENT.md`
- **Cutting a release / publishing to PyPI:** [`docs/RELEASING.md`](docs/RELEASING.md)
  is agent-runnable end-to-end (version bump, CHANGELOG close-out,
  tag, GitHub Release, automated PyPI publish via trusted publishing).

## Review checklist (this repo)

Before committing a structural change:

- Have you read `docs/DESIGN_INVARIANTS.md` for the area you're touching?
- Have you run `/heddle-preflight` (or its commands manually)?
- If you touched `core/messages.py` or `schemas/v1/*`, have you run
  `/heddle-contract-sync` from `../heddle-sdk/`?
- For non-trivial changes: did you spawn `heddle-architect` to surface
  the design plan first?
- For structural diffs: did you spawn `heddle-invariant-guard` to verify
  the eight red lines?
- **Does this commit add, change, deprecate, remove, or fix
  user-facing behaviour?** If yes, add an entry under `[Unreleased]` in
  [`CHANGELOG.md`](CHANGELOG.md) (Added / Changed / Deprecated /
  Removed / Fixed / Security). Documentation-only changes, internal
  refactors with no behavioural delta, and CI/build adjustments are
  exempt.
