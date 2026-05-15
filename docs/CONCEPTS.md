# Concepts — How Heddle Works

## The big idea

Instead of cramming everything into one giant AI prompt, Heddle splits work into
small, focused steps. Each step does one thing well — summarize, classify,
extract entities, convert a PDF. Steps can run in parallel, use different AI
models, and be tested independently.

Think of it like an assembly line: raw material goes in one end, each station
does its part, and a finished product comes out the other end.

```text
  Document ──► Chunk ──► Embed ──► Analyze ──► Report
                  │                    │
                  └── (these can run on different models)
```

Why does this matter? Because a single giant prompt hits limits fast: it
forgets context, mixes up tasks, and is impossible to debug. Splitting the
work means each piece stays small, testable, and reliable.

---

## Core concepts

### Steps (workers)

A **step** is a focused AI task with a clear job:

- **What it does:** Takes defined inputs, produces defined outputs.
- **Example:** Give it a block of text, get back a summary and key points.
- **How you define it:** A YAML file with a system prompt, input/output
  contracts, and a model tier. No Python code needed for LLM steps.

There are two flavors:

| Type | Does what | Example |
|------|-----------|---------|
| LLM step | Calls an AI model | Summarize, classify, extract |
| Processor step | Runs code, no AI needed | Parse a PDF, chunk text, store embeddings |

Each step processes one task and resets. No state carries between tasks —
this keeps things predictable and testable.

> Heddle terminology: steps are called **workers**.

### Workflows (pipelines)

A **workflow** chains steps together so data flows from one to the next:

```text
  Ingest ──► Chunk ──► Embed ──► Store
    │           │         │         │
    │           │         │         └─ save to vector database
    │           │         └─ convert text to embeddings
    │           └─ split into small pieces
    └─ read raw data from source
```

- Steps that don't depend on each other run **in parallel** automatically.
- Heddle figures out the dependencies from your configuration — you don't
  need to wire them by hand.
- If a step fails, the workflow reports which step broke and why.

> Heddle terminology: workflows are called **pipelines**.

### Models

Heddle supports three tiers of AI model. Each step can use a different one:

| Tier | What it is | Best for | Cost |
|------|-----------|----------|------|
| **Local** | Runs on your machine via LM Studio or Ollama (LM Studio wins when both are set) | Simple tasks (chunking, classification) | Free |
| **Standard** | Claude Sonnet (cloud API) | Most analytical tasks | Per-token |
| **Frontier** | Claude Opus (cloud API) | Complex reasoning, synthesis | Per-token |

The rule of thumb: use the cheapest model that does the job well. Reserve
frontier for the hard stuff.

> Heddle terminology: this is called the **model tier**.

### The message bus (you can skip this)

When running in production, Heddle connects its pieces through a message bus
(NATS). You do **not** need to understand this to get started:

- **For development:** Workshop and `heddle rag` work without it.
- **For production:** NATS connects workers, the router, and orchestrators
  so they can scale independently.

Come back to this when you need to deploy to a team or run continuously.

---

## Two ways to use Heddle

### Direct mode (no infrastructure)

The fastest path. No servers, no message bus, no containers.

```bash
# 1. Set up (interactive wizard — detects LM Studio and Ollama,
#    LM Studio wins by default if both; sets paths and API keys)
uv run heddle setup

# 2. Ingest data
uv run heddle rag ingest /path/to/data/*.json

# 3. Search
uv run heddle rag search "earthquake damage reports"

# 4. Open the web dashboard
uv run heddle rag serve
```

You also get **Workshop**, a web UI for building and testing individual
steps without any infrastructure:

```bash
uv run heddle workshop --port 8080
```

Best for: getting started, research, solo development, testing new steps.

### Infrastructure mode (NATS)

For teams, production, or continuous processing. Workers, router, and
orchestrator communicate through a message bus:

```text
  ┌──────────┐     ┌──────────┐     ┌──────────────┐
  │  Submit   │────►│  Router  │────►│  Worker(s)   │
  │  a goal   │     │(dispatch)│     │ (do the work)│
  └──────────┘     └──────────┘     └──────┬───────┘
                                           │
                                    ┌──────▼───────┐
                                    │ Orchestrator  │
                                    │ (collect &    │
                                    │  synthesize)  │
                                    └──────────────┘
```

- Scale any piece independently by running more copies.
- Monitor everything with the TUI dashboard (`uv run heddle ui`).
- Schedule recurring jobs with the built-in scheduler.

Best for: production, multi-user, continuous processing, team deployments.

---

## Configuration

All settings live in one place: `~/.heddle/config.yaml`, created by
`uv run heddle setup`.

**Priority order** (highest wins):

1. CLI flags (`--tier local`)
2. Environment variables (`OLLAMA_URL=...`)
3. Config file (`~/.heddle/config.yaml`)
4. Built-in defaults

The config file stores your model preferences, API keys, data paths,
and default behaviors. You can always override any setting at the command
line without editing the file.

---

## What's next

- **[Getting Started](GETTING_STARTED.md)** — install and run your first
  pipeline in five minutes.
- **[Building Workflows](building-workflows.md)** — create custom steps
  and chain them into pipelines.
- **[RAG Pipeline Guide](rag-howto.md)** — set up the social media analysis
  pipeline.
