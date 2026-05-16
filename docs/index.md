# Heddle

**Turn what you know into testable AI steps. Chain them into workflows.
Measure whether they work. Scale when ready.**

---

## What Heddle Does

Most AI tools give you one big prompt and one model. That works until it
doesn't — the prompt gets unwieldy, you can't test parts independently,
and asking the same model to review its own work doesn't catch real
problems.

Heddle splits AI work into focused **steps**. Each step has a clear job, a
typed contract (so you know what goes in and what comes out), and can use
a different model. You test steps individually, chain them into pipelines,
and measure whether changes help or hurt.

```text
  Document ──► Extract ──► Classify ──► Summarize ──► Report
                 │            │            │
                 │            │            └─ Claude Opus (complex reasoning)
                 │            └─ LM Studio / Ollama local (fast, free)
                 └─ LM Studio / Ollama local (fast, free)
```

Steps run in parallel when they can, and are tested with the built-in
Workshop web UI — all without deploying any infrastructure. When you're
ready to scale, Heddle adds a message bus (NATS) that connects everything
for production use.

## Try It in 60 Seconds

```bash
pip install heddle-ai[workshop]    # install from PyPI
heddle setup                       # configure (auto-detects LM Studio + Ollama)
heddle workshop                    # open the web UI at localhost:8080
```

Open your browser at `http://localhost:8080`, pick a worker (summarizer,
classifier, extractor, translator, qa, or reviewer), paste any text, and
click Run. No data files needed.

**Have Telegram exports?** Install with `pip install heddle-ai[rag]`
instead, then run `heddle rag ingest`, `heddle rag search`, and
`heddle rag serve` for full social media stream analysis.

## Three Ways to Use Heddle

**1. Workshop (no setup beyond install).** Test shipped workers in the
browser — paste text, get results. Six ready-made workers ship with Heddle.

**2. Build your own steps (guided).** Scaffold workers and pipelines
interactively with `heddle new worker` and `heddle new pipeline` — YAML
is generated for you. Test and evaluate in the Workshop web UI.

**3. Distributed infrastructure (production).** For teams, continuous
processing, or high-throughput scenarios: run the router, workers, and
pipeline orchestrator across machines. Scale any component by running
more copies — NATS load-balances automatically.

## Who This Is For

**Anyone hitting the limits of single-prompt AI.** Whether you're a
student comparing how different models answer questions, a teacher
grading essays and checking for bias, or a city clerk categorizing public
comments — if you need more than one AI step working together, Heddle
gives you a structured way to build that.

**Researchers and analysts** — process documents, extract data, build
analytical pipelines. Heddle's knowledge silos and blind audit pattern
let you get genuine adversarial review of AI-generated analysis — not the
pseudo-review you get when the same model checks its own work.

**AI engineers** — build multi-step LLM workflows with typed contracts,
tool-use, knowledge injection, and pipeline orchestration.

**Platform teams** — deploy to Kubernetes with rate limiting, model tier
management, dead-letter handling, and OpenTelemetry tracing.

## Key Features

| Feature | What It Does |
|---------|-------------|
| **6 Ready-Made Workers** | Summarizer, classifier, extractor, translator, QA, reviewer — chain them immediately |
| **Workshop** | Web UI for testing, evaluating, and comparing step outputs |
| **Built-in Evaluation** | Test suites, scoring, golden dataset baselines, regression detection |
| **Config-Driven** | Define workers in YAML — no Python code needed for LLM steps |
| **Knowledge Silos** | Per-worker access control; blind audit workers can't see what they're reviewing |
| **Pipeline Orchestration** | Chain steps with automatic dependency detection and parallelism |
| **Foreign-Language SDKs** | Build .NET and Swift processor workers with the companion [Heddle SDK](https://github.com/getheddle/heddle-sdk) |
| **Three Model Tiers** | Local (LM Studio or Ollama), Standard (Claude Sonnet), Frontier (Claude Opus) |
| **Document Processing** | PDF/DOCX extraction via MarkItDown (fast) or Docling (deep OCR) |
| **RAG Pipeline** | Telegram ingestion, chunking, vector search (DuckDB or LanceDB) |
| **Multi-Agent Councils** | Multi-round deliberation with debate, Delphi, and round-robin protocols |
| **ChatBridge Adapters** | Use Claude, GPT-4, LM Studio, Ollama, or humans as council participants |
| **MCP Gateway** | Expose any workflow as an MCP server with a single YAML config |
| **Config Wizard** | `heddle setup` auto-detects backends; `heddle new` scaffolds workers and pipelines |
| **Live Monitoring** | TUI dashboard, OpenTelemetry tracing, dead-letter inspection |
| **Deployment** | Docker Compose, Kubernetes manifests, mDNS discovery |

## Documentation

<!-- The "Documentation" tables below are kept in sync with the
     corresponding sections in the repo-root README.md. When editing one,
     mirror the change to the other. -->

Start here:

| Guide | Description |
|-------|-------------|
| **[Concepts](CONCEPTS.md)** | How Heddle works — the mental model in plain language |
| **[Getting Started](GETTING_STARTED.md)** | Install, configure, and get your first result |
| **[Why Heddle?](WHY_HEDDLE.md)** | How Heddle compares to other frameworks — and when not to use it |
| **[Workshop Tour](WORKSHOP_TOUR.md)** | What each Workshop screen does and when to use it |
| **[Configuration](CONFIG.md)** | `~/.heddle/config.yaml` reference and priority chain |
| **[CLI Reference](CLI_REFERENCE.md)** | Every command with every flag and default |
| **[Workers Reference](workers-reference.md)** | 6 shipped workers with I/O schemas and examples |

Go deeper:

| Guide | Description |
|-------|-------------|
| [RAG Pipeline](rag-howto.md) | Social media stream analysis end-to-end |
| [Multi-Agent Councils](council-howto.md) | Structured deliberation with multiple LLM agents |
| [Adversarial Review](BLIND_AUDIT.md) | Set up genuine blind review using knowledge silos |
| [Building Workflows](building-workflows.md) | Custom steps, pipelines, tools, knowledge |
| [Foreign-Language Actors](foreign-actors.md) | Build .NET, Swift, or other processor workers against Heddle's wire protocol |
| [Language SDKs](https://getheddle.dev/heddle-sdk/) | Companion .NET and Swift SDK documentation |
| [Gateway Actors](gateway-actors.md) | Bridge HTTP, MQTT, IoT, or other non-NATS systems into Heddle |
| [Workshop Architecture](workshop.md) | Web UI architecture and enhancement guide |
| [Architecture](ARCHITECTURE.md) | System design, message flow, NATS subjects |
| [Design Invariants](DESIGN_INVARIANTS.md) | Non-obvious design decisions (read before structural changes) |
| [Troubleshooting](TROUBLESHOOTING.md) | Common issues and solutions |
| [Deployment](LOCAL_DEPLOYMENT.md) | Local, Docker, and [Kubernetes](KUBERNETES.md) |

Tutorials (step-by-step, phased examples):

- **[Document Intake](tutorials/document-intake.md)** — Build a public comment pipeline: CSV reader, classifier, entity extractor, bias audit (three phases)
- **[Research Review](tutorials/research-review.md)** — Build a paper review pipeline: claim extraction, methodology review, blind adversarial audit (three phases)

Council showcases (runnable demos in the repo's `examples/` directory):

- **Town Hall Debate** — audience interjections during multi-agent deliberation
- **Debate Arena** — round-robin tournament with judge panels and scoring
- **Blind Taste Test** — anonymous LLM evaluation using the Delphi protocol
