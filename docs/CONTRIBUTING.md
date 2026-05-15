# Contributing

Thank you for your interest in contributing to Heddle.

---

## Before You Contribute

**Read [`GOVERNANCE.md`](https://github.com/getheddle/heddle/blob/main/GOVERNANCE.md) first.** This project has a mission constraint:
reliable, well-tested, openly available infrastructure for AI workflow orchestration.
Contributions that compromise the framework's generality, introduce vendor lock-in,
or bypass architectural invariants will not be merged regardless of their other merits.

---

## Contributor License Agreement (CLA)

**All contributors must sign the CLA before any pull request is merged.**

This is not negotiable and is not bureaucratic friction — it exists to preserve
the project's ability to offer alternative licensing to organizations that cannot
accept copyleft terms, while keeping the public license open for everyone else.

**What the CLA does:**

- Grants the project the right to sublicense your contribution
- Does NOT transfer your copyright — you retain full ownership of your work
- Applies to all future contributions once signed

**How to sign:**
The CLA bot will prompt you automatically when you open your first pull request.
Sign electronically in that flow. It takes under a minute.

If you have questions about the CLA before contributing, contact: <admin@getheddle.dev>

---

## Technical Standards

Contributions must adhere to the project's architectural invariants:

**Worker statelessness:**
Workers process one task and reset. No state carries between tasks — this is
enforced, not optional. Contributions that introduce shared mutable state
between workers will be rejected.

**Typed messages:**
All inter-actor communication uses typed Pydantic messages (`TaskMessage`,
`TaskResult`, `OrchestratorGoal`, `CheckpointState`). Raw dictionaries or
untyped payloads are not acceptable.

**I/O contract validation:**
Workers have strict I/O contracts validated by `core/contracts.py`. Every
worker must define input and output schemas in its YAML config, and all
outputs must conform to the declared schema.

**Deterministic routing:**
The router does not use an LLM. It routes by `worker_type` and `model_tier`
using rules in `configs/router_rules.yaml`. Do not add LLM logic to the router.

**Test coverage:**
All new functionality must include unit tests. Tests must pass without
infrastructure (NATS, Valkey, Ollama). Use `InMemoryBus` and
`InMemoryCheckpointStore` for testing.

**Observability:**
New code that calls LLM backends should follow the OpenTelemetry GenAI semantic
conventions for span attributes (`gen_ai.system`, `gen_ai.request.model`,
`gen_ai.usage.*`, etc.). See [Architecture — Distributed Tracing](ARCHITECTURE.md#distributed-tracing-tracing)
for the full list of required attributes.

**Code style:**
All contributions must conform to the project's coding standards defined in
[`CODING_GUIDE.md`](CODING_GUIDE.md). Key requirements: Google-style docstrings,
type annotations on all public functions, ruff for formatting and linting.

**LLM Quality Tests:**
Use the `@pytest.mark.deepeval` marker for tests that evaluate LLM output quality
using the DeepEval framework. These tests use a local Ollama model as a judge and
auto-skip when DeepEval or Ollama are unavailable, so they never break CI.

Guidelines:

- Use `GEval` with explicit `criteria` for reliable results with local models
- Keep thresholds moderate (0.6–0.7) — local judge models are less precise than cloud APIs
- Import `skip_no_deepeval` from `tests.conftest` and apply it alongside the `deepeval` marker
- Reference: `tests/test_deepeval_worker.py` for examples

---

## What We Need Most

- New worker configurations for specific domains
- New contrib packages (databases, search engines, monitoring integrations)
- Config validation improvements and new config types
- Workshop MetricsCollector for live NATS metrics in the dashboard
- MCP gateway examples and transport extensions
- Documentation improvements and examples
- Bug reports with reproducible steps

---

## What We Are Not Looking For

- Shared mutable state between workers
- LLM logic in the router
- Monolithic worker configs that combine multiple responsibilities
- Contributions that skip I/O contract validation
- Dependencies on specific LLM providers without abstraction

---

## Pull Request Process

1. Fork the repo
2. Make changes in a feature branch
3. Sign the CLA when prompted
4. Ensure all tests pass: `uv run pytest tests/ -v -m "not integration"`
5. Lint and format your code: `uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/`
6. Read the [Coding Guide](CODING_GUIDE.md) if this is your first contribution
7. Submit a pull request with a clear description of what changed and why

Expect review feedback focused on architectural compliance and test coverage.

---

## AI-Assisted Development

This project uses Claude (Anthropic) as a development tool. The repo's
agent layer is split across two files plus a sibling repo:

- [`AGENTS.md`](https://github.com/getheddle/heddle/blob/main/AGENTS.md)
  is the source of truth for repo-specific agent guidance (project
  layout, runtime quirks, build/test, review checklist).
- [`CLAUDE.md`](https://github.com/getheddle/heddle/blob/main/CLAUDE.md)
  is a thin Claude-specific pointer.
- Cross-repo guidance — philosophy, the framework invariants, the
  wire-protocol contract map, and the shared skills + subagents —
  lives in the sibling
  [`heddle-agent-toolkit`](https://github.com/getheddle/heddle-agent-toolkit)
  repo. Clone it as a sibling and run its `install.sh` to populate
  this repo's `.claude/`.

AI-generated code is subject to the same standards as human contributions: typed
messages, stateless workers, validated I/O contracts, and test coverage.

---

## Contact

<admin@getheddle.dev>
