# CLI Reference

All commands are invoked as `heddle COMMAND [OPTIONS]`. Commands are grouped by
whether they need a running NATS server.

## Overview

| Command | Purpose | NATS Required |
|---|---|---|
| `heddle setup` | Interactive configuration wizard | No |
| `heddle new worker` | Scaffold a new worker config | No |
| `heddle new pipeline` | Scaffold a new pipeline config | No |
| `heddle validate` | Validate config files | No |
| `heddle rag ingest` | Ingest Telegram exports into vector store | No |
| `heddle rag search` | Semantic search | No |
| `heddle rag stats` | Vector store statistics | No |
| `heddle rag serve` | Workshop with RAG dashboard | No |
| `heddle worker` | LLM worker actor | Yes |
| `heddle processor` | Non-LLM processor worker | Yes |
| `heddle pipeline` | Pipeline orchestrator | Yes |
| `heddle orchestrator` | Dynamic LLM orchestrator | Yes |
| `heddle scheduler` | Time-driven task scheduler | Yes |
| `heddle router` | Deterministic task router | Yes |
| `heddle submit` | Submit a goal | Yes |
| `heddle mcp` | MCP server gateway | Yes |
| `heddle workshop` | Worker Workshop web UI | No |
| `heddle ui` | Terminal dashboard (TUI) | Yes |
| `heddle mdns` | mDNS service discovery | No |
| `heddle dead-letter monitor` | Dead-letter queue consumer | Yes |
| `heddle council run` | Multi-agent council deliberation | No |
| `heddle council validate` | Validate a council config | No |

---

## Getting Started Commands (no NATS)

### heddle setup

Interactive wizard that detects your environment and writes a config file.

```bash
heddle setup [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--config-path` | `~/.heddle/config.yaml` | Config file path |
| `--non-interactive` | `false` | Skip prompts, use defaults |

Steps performed:

1. Detects LM Studio at `localhost:1234/v1` and Ollama at `localhost:11434`
2. If both are configured, asks which serves the local tier
3. Prompts for Anthropic API key (optional)
4. Picks an embedding model (LM Studio: lists loaded embed models; Ollama: pulls if missing)
5. Scans for Telegram export files
6. Writes `~/.heddle/config.yaml`

---

### heddle new (group)

Scaffold new worker and pipeline configs interactively. YAML is generated
from your answers — you don't need to write it from scratch.

#### heddle new worker

```bash
heddle new worker [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--name` | *(prompted)* | Worker name (lowercase, underscores) |
| `--kind` | *(prompted)* | `llm` or `processor` |
| `--tier` | *(prompted)* | `local`, `standard`, or `frontier` |
| `--configs-dir` | `configs/` | Where to write the config |
| `--non-interactive` | `false` | Use defaults, skip prompts |

Prompts for: name, kind, system prompt (or editor), model tier, input/output
field names, timeout. Validates before writing.

#### heddle new pipeline

```bash
heddle new pipeline [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--name` | *(prompted)* | Pipeline name |
| `--configs-dir` | `configs/` | Where to write the config |
| `--non-interactive` | `false` | Use defaults, skip prompts |

Lists available workers, then prompts to add stages in a loop. For each stage:
worker type, stage name, input mapping (guided path syntax). Validates the
pipeline graph before writing.

---

### heddle validate

Validate worker, pipeline, and orchestrator configs without starting any
infrastructure. Auto-detects config type from file content.

```bash
heddle validate [OPTIONS] [PATHS...]
```

| Option | Default | Description |
|---|---|---|
| `--all` | `false` | Validate all configs in `--configs-dir` |
| `--configs-dir` | `configs/` | Root config directory |

Examples:

```bash
heddle validate configs/workers/my_worker.yaml       # single file
heddle validate configs/workers/*.yaml               # multiple files
heddle validate --all                                 # everything in configs/
```

Exit code 0 if all valid, 1 if any errors. Colored output with per-file
pass/fail indicators.

---

### heddle rag (group)

All RAG subcommands share a set of group-level options.

```bash
heddle rag [GROUP-OPTIONS] COMMAND
```

**Group options** (inherited by every subcommand):

| Option | Default | Description |
|---|---|---|
| `--config-path` | `~/.heddle/config.yaml` | Config file path |
| `--db-path` | *(from config)* | Override vector store path |
| `--store` | *(from config)* | Backend: `duckdb` or `lancedb` |
| `--ollama-url` | *(from config)* | Override Ollama URL |
| `--lm-studio-url` | *(from config)* | Override LM Studio URL (e.g. `http://localhost:1234/v1`) |
| `--embedding-backend` | *(auto)* | Force `ollama` or `openai-compatible` |
| `--embedding-model` | *(from config)* | Override embedding model |

#### heddle rag ingest

Ingest Telegram JSON exports into the vector store.

```bash
heddle rag ingest [OPTIONS] PATHS...
```

| Option | Default | Description |
|---|---|---|
| `--embed / --no-embed` | `--embed` | Generate embeddings via the configured backend (LM Studio or Ollama) |
| `--window-hours` | `6` | Time window size in hours |
| `--chunk-target` | `400` | Target chunk size in chars |
| `--chunk-max` | `600` | Maximum chunk size in chars |

`PATHS` accepts one or more directories or `result.json` files.

#### heddle rag search

Run a semantic similarity search against the vector store.

```bash
heddle rag search [OPTIONS] QUERY
```

| Option | Default | Description |
|---|---|---|
| `--limit`, `-n` | `10` | Max results |
| `--min-score` | `0.0` | Minimum similarity score |

#### heddle rag stats

Print vector store statistics (row count, embedding dimensions, disk size).

```bash
heddle rag stats
```

No additional options beyond the group options.

#### heddle rag serve

Start the Workshop web UI with the RAG dashboard enabled.

```bash
heddle rag serve [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--port` | `8080` | Workshop port |
| `--host` | `127.0.0.1` | Bind address |

---

## Infrastructure Commands (NATS required)

All infrastructure commands connect to NATS and typically accept `--nats-url`
and `--skip-preflight`. Most also require a `--config` YAML file that describes
the actor.

### heddle worker

Start an LLM worker actor.

```bash
heddle worker --config PATH [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--config` | *(required)* | Worker config YAML |
| `--nats-url` | `nats://nats:4222` | NATS server URL |
| `--tier` | `standard` | Model tier: `local`, `standard`, `frontier` |
| `--skip-preflight` | `false` | Skip connectivity checks |

### heddle processor

Start a non-LLM processor worker (e.g. extractors, validators).

```bash
heddle processor --config PATH [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--config` | *(required)* | Processor config YAML |
| `--nats-url` | `nats://nats:4222` | NATS server URL |
| `--tier` | `local` | Model tier |
| `--skip-preflight` | `false` | Skip connectivity checks |

### heddle pipeline

Start a pipeline orchestrator that chains workers in sequence.

```bash
heddle pipeline --config PATH [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--config` | *(required)* | Pipeline config YAML |
| `--nats-url` | `nats://nats:4222` | NATS server URL |
| `--skip-preflight` | `false` | Skip connectivity checks |

### heddle orchestrator

Start the dynamic LLM orchestrator (goal decomposition, delegation).

```bash
heddle orchestrator --config PATH [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--config` | *(required)* | Orchestrator config YAML |
| `--nats-url` | `nats://nats:4222` | NATS server URL |
| `--redis-url` | `redis://redis:6379` | Redis URL (state store) |
| `--skip-preflight` | `false` | Skip connectivity checks |

### heddle scheduler

Start the time-driven task scheduler.

```bash
heddle scheduler --config PATH [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--config` | *(required)* | Scheduler config YAML |
| `--nats-url` | `nats://nats:4222` | NATS server URL |
| `--skip-preflight` | `false` | Skip connectivity checks |

### heddle router

Start the deterministic task router.

```bash
heddle router [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--config` | `configs/router_rules.yaml` | Router rules YAML |
| `--nats-url` | `nats://nats:4222` | NATS server URL |
| `--skip-preflight` | `false` | Skip connectivity checks |

### heddle submit

Submit a goal to the orchestrator for processing.

```bash
heddle submit GOAL [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--nats-url` | `nats://nats:4222` | NATS server URL |
| `--context` | *(none)* | Key=value pairs (repeatable) |

Example:

```bash
heddle submit "Process document" --context file_ref=test.pdf --context lang=en
```

!!! warning "`--context` values are not redacted"
    Anything passed to `--context` flows into the NATS message that
    workers see, and may end up in actor logs, dead-letter entries,
    and eval-result rows. Treat values the same way you'd treat
    anything written to stdout — don't put secrets (API keys,
    tokens, passwords) on the command line. Use env-var
    references in the worker config or knowledge silos instead.

---

## UI & Discovery Commands

### heddle workshop

Start the Worker Workshop web UI.

```bash
heddle workshop [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--port` | `8080` | HTTP port |
| `--host` | `127.0.0.1` | Bind address |
| `--configs-dir` | `configs/` | Directory of worker configs |
| `--db-path` | `~/.heddle/workshop.duckdb` | Workshop database |
| `--nats-url` | *(none)* | Optional NATS URL for dead-letter inbox and reload broadcast |
| `--apps-dir` | `~/.heddle/apps` | Custom apps directory |

#### Workshop security

The Workshop binds to `127.0.0.1` by default and ships with no auth —
appropriate for a developer dashboard on a single workstation. Before
exposing it on a non-loopback address (`--host 0.0.0.0`, a LAN IP, a
container's external interface), set `HEDDLE_WORKSHOP_TOKEN` so every
mutating request requires the token via `Authorization: Bearer` or
the session cookie:

```bash
export HEDDLE_WORKSHOP_TOKEN="$(openssl rand -hex 32)"
heddle workshop --host 0.0.0.0 --port 8080
```

A non-loopback bind without `HEDDLE_WORKSHOP_TOKEN` is a P1
misconfiguration. The CLI emits a `workshop.insecure_bind` warning at
startup; the operator must fix it before the process is reachable from
the network. See [`SECURITY_MODEL.md`](SECURITY_MODEL.md) §2 for the
full trust model and the
[`runbooks/deploy-workshop-safely.md`](runbooks/deploy-workshop-safely.md)
checklist for the deploy flow.

### heddle ui

Launch the terminal dashboard (TUI) for monitoring actors in real time.

```bash
heddle ui [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--nats-url` | `nats://localhost:4222` | NATS server URL |

### heddle mdns

Broadcast mDNS service records so other Heddle nodes can discover this machine.

```bash
heddle mdns [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--workshop-port` | `8080` | Advertised Workshop port |
| `--nats-port` | `4222` | Advertised NATS port |
| `--mcp-port` | `0` | Advertised MCP port (0 = disabled) |
| `--host` | *(auto-detect)* | Hostname to advertise |

### heddle mcp

Start the MCP (Model Context Protocol) server gateway.

```bash
heddle mcp --config PATH [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--config` | *(required)* | MCP config YAML |
| `--transport` | `stdio` | Transport: `stdio` or `streamable-http` |
| `--host` | `127.0.0.1` | Bind address (streamable-http only) |
| `--port` | `8000` | Port (streamable-http only) |
| `--skip-preflight` | `false` | Skip connectivity checks |

### heddle dead-letter monitor

Consume and display messages from the dead-letter queue.

```bash
heddle dead-letter monitor [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--nats-url` | `nats://nats:4222` | NATS server URL |
| `--max-size` | `1000` | Max messages to retain in memory |

---

## Pre-flight Checks

Infrastructure commands (`worker`, `processor`, `pipeline`, `orchestrator`,
`scheduler`, `router`, `mcp`) run pre-flight checks before starting:

1. **Config readability** (hard fail) -- the config YAML at `--config` must
   exist and be readable.
2. **NATS connectivity** (hard fail) -- verifies the NATS server at
   `--nats-url` accepts a connection.
3. **Environment variables** (warnings only) -- emits per-tier warnings when
   `LM_STUDIO_URL` / `OLLAMA_URL` / `ANTHROPIC_API_KEY` aren't set for the
   model tier the command will use.

Pass `--skip-preflight` to bypass these checks. Useful in CI or when you
know the infrastructure is already running.

---

## Environment Variables

| Variable | Affects | Description |
|---|---|---|
| `LM_STUDIO_URL` | All commands | LM Studio base URL (e.g. `http://localhost:1234/v1`) |
| `LM_STUDIO_MODEL` | `worker`, `processor` | LM Studio model id (from `/v1/models`) |
| `OLLAMA_URL` | All commands | Ollama base URL (overrides config) |
| `OLLAMA_MODEL` | `worker`, `processor` | Default Ollama model name |
| `HEDDLE_LOCAL_BACKEND` | All commands | `lmstudio` or `ollama` — which serves the local tier when both URLs are set (default: `lmstudio`) |
| `HEDDLE_EMBEDDING_BACKEND` | RAG ingest/search | `openai-compatible` or `ollama` — overrides the auto-selected embedding backend |
| `ANTHROPIC_API_KEY` | `worker`, `orchestrator` | Anthropic API key for cloud models |
| `FRONTIER_MODEL` | `worker` | Model name for the frontier tier |
| `OPENAI_API_KEY` | `worker` | OpenAI-compatible API key |
| `OPENAI_BASE_URL` | `worker` | OpenAI-compatible base URL |
| `HEDDLE_PIPELINE_VERBOSE` | `pipeline` orchestrator | Log full payload data in pipeline summaries (`1` / `true`). **Orthogonal to OTel tracing** — this controls the pipeline's payload logging, not span emission. Legacy alias: `HEDDLE_TRACE` (deprecated; emits a warning if used). |
| `HEDDLE_TRACE_CONTENT` | All infrastructure | Include LLM prompt/completion text as events on `llm.call` spans when OTel is active. **Different from `HEDDLE_PIPELINE_VERBOSE`** — this is the OTel content-recording switch. |
