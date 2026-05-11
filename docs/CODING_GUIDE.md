# Heddle Coding, Documentation & Style Guide

This guide defines the coding, commenting, and documentation standards for all
Heddle contributors. Code that does not conform will be flagged during review or
by the automated linter.

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) first for architectural invariants and
the CLA. This guide covers the **how** of writing code; `CONTRIBUTING.md` covers
the **what** is acceptable.

---

## Table of Contents

1. [Python Version and Language Features](#python-version-and-language-features)
2. [Code Formatting](#code-formatting)
3. [Naming Conventions](#naming-conventions)
4. [Import Style](#import-style)
5. [Type Annotations](#type-annotations)
6. [Module Docstrings](#module-docstrings)
7. [Class Docstrings](#class-docstrings)
8. [Function and Method Docstrings](#function-and-method-docstrings)
9. [Inline Comments](#inline-comments)
10. [Error Handling](#error-handling)
11. [Logging](#logging)
12. [Testing](#testing)
13. [YAML Configuration](#yaml-configuration)
14. [Git Workflow](#git-workflow)
15. [Reference Examples](#reference-examples)

---

## Python Version and Language Features

- **Python 3.11+** is required. Use modern syntax freely:
  - `X | Y` union syntax (not `Union[X, Y]`)
  - `dict[str, Any]` lowercase generics (not `Dict[str, Any]`)
  - `from __future__ import annotations` at the top of every module (PEP 563
    deferred evaluation — keeps runtime import cost low and avoids forward-ref
    issues)
- **Pydantic v2** for all data models.
- **asyncio** for all I/O-bound code. Actors are async; blocking calls must be
  offloaded to a thread pool (see `SyncProcessingBackend`).

---

## Code Formatting

All formatting is enforced by **ruff** (configured in `pyproject.toml`).

| Rule | Setting |
|------|---------|
| Line length | 100 characters |
| Indentation | 4 spaces (no tabs) |
| Quotes | Double quotes (`"`) preferred |
| Trailing commas | Required in multi-line constructs |
| Blank lines | 2 between top-level definitions, 1 within classes |

Run the formatter before committing:

```bash
uv run ruff format src/ tests/
uv run ruff check src/ tests/ --fix
```

---

## Naming Conventions

| Element | Convention | Example |
|---------|-----------|---------|
| Modules | `snake_case` | `nats_adapter.py` |
| Classes | `PascalCase` | `PipelineOrchestrator` |
| Functions / methods | `snake_case` | `resolve_tier()` |
| Constants | `UPPER_SNAKE_CASE` | `DEAD_LETTER_SUBJECT` |
| Private members | Leading underscore | `_running`, `_refill()` |
| Type variables | `PascalCase` + `T` suffix | `MessageT` |
| Pydantic fields | `snake_case` | `worker_type`, `goal_id` |
| NATS subjects | `dot.separated.lowercase` | `heddle.tasks.incoming` |
| CLI commands | `kebab-case` (Click default) | `heddle workshop` |
| Config keys (YAML) | `snake_case` | `max_concurrent_goals` |

**Abbreviations:** Avoid unless universally understood (`url`, `id`, `db`).
Spell out domain terms (`orchestrator` not `orch`, `message` not `msg` — except
in `structlog` event names where brevity is expected).

---

## Import Style

Imports are organized into four groups separated by blank lines, sorted
alphabetically within each group:

```python
# 1. __future__ imports (always first)
from __future__ import annotations

# 2. Standard library
import asyncio
import json
from typing import Any

# 3. Third-party
import structlog
import yaml
from pydantic import BaseModel, Field

# 4. Local (heddle package)
from heddle.core.actor import BaseActor
from heddle.core.messages import TaskMessage, TaskResult
```

**Rules:**

- Use `from X import Y` for specific names; use `import X` for namespaces you
  reference multiple times (e.g., `import json` then `json.loads()`).
- Never use wildcard imports (`from X import *`).
- Conditional imports (for optional dependencies) go at point of use, not at
  module top:

```python
# Good — only imported when needed, avoids hard dep
async def process(self, ...):
    from heddle.worker.knowledge import load_knowledge_silos
    ...
```

- Ruff enforces import sorting automatically via `isort` rules.

---

## Type Annotations

- **Annotate all public function signatures** (parameters and return types).
- Private helper functions: annotations recommended but not strictly required.
- Use `Any` sparingly — prefer specific types. `dict[str, Any]` is acceptable
  for JSON-like data flowing across actor boundaries.
- Use `| None` instead of `Optional[X]`.
- For callback types, use `collections.abc.Callable` (not `typing.Callable`).

```python
# Good
async def call_worker(
    self,
    worker_type: str,
    payload: dict[str, Any],
    tier: str = "standard",
    timeout: float = 60.0,
) -> dict[str, Any]:
    ...

# Bad — missing return type, uses old-style Optional
def call_worker(self, worker_type, payload, tier="standard", timeout=60.0):
    ...
```

### Type checking gate

CI runs **pyright** in `basic` mode against a narrow surface — only the
"hot path" runtime packages:

- `src/heddle/core/`
- `src/heddle/bus/`
- `src/heddle/worker/`
- `src/heddle/orchestrator/`

Configuration lives under `[tool.pyright]` in `pyproject.toml`. The gated
list is intentionally smaller than the full source tree; widen it in
follow-up work as type coverage on other packages matures (`router/`,
`scheduler/`, `mcp/`, `workshop/`).

**Strictness policy:**

- `typeCheckingMode = "basic"` today — surface obvious bugs without
  drowning in nominally-typed dynamism. Tighten to `standard` once the
  gated dirs are clean under stricter rules.
- `reportMissingImports = "warning"` — optional/contrib imports inside
  the gated dirs shouldn't fail the build on a partial install.

**Suppressing diagnostics:**

- Prefer a real fix (narrow with `assert`, use a walrus, etc.) over a
  suppression.
- When suppression is unavoidable, use `# pyright: ignore[<rule>]` with
  a comment explaining *why* and (ideally) a TODO linking the follow-up.
- Never blanket `# type: ignore` — always specify the rule.

Run locally with `uv run --with pyright pyright`.

---

## Module Docstrings

Every `.py` file **must** have a module-level docstring immediately after the
`"""` opening. This is the single most important piece of documentation — it
tells a reader *what this file does* and *why it exists* without reading any
code.

**Required elements:**

1. **One-line summary** — what the module does.
2. **Context paragraph** — where this fits in the architecture, what depends on
   it, and what it depends on.
3. **Design notes** (if applicable) — why a particular approach was chosen, any
   invariants maintained, known limitations.
4. **See also** (if applicable) — related modules for navigation.

**Template:**

```python
"""
One-line summary of what this module does.

Longer description providing architectural context: how this module fits into
Heddle, what calls it, what it calls. Explain the core abstraction or pattern.

Design note: why this approach was chosen over alternatives. For example,
why we use our own JSON Schema validator instead of the jsonschema library.

See also:
    heddle.core.messages — the message types this module processes
    heddle.bus.nats_adapter — the production bus implementation
"""
```

**Good example** (from `core/actor.py`):

```python
"""
Base actor class — the foundation of Heddle's actor model.

All Heddle actors (workers, orchestrators, routers) inherit from BaseActor.
This class handles the message bus subscription lifecycle, message dispatch,
signal-based shutdown, and error isolation. Each actor is an independent
process with no shared memory.

Design invariant: actors communicate ONLY through bus messages (see messages.py).
Direct method calls between actors are forbidden.

The message bus is pluggable via the ``bus`` constructor parameter. The default
is NATSBus (created from ``nats_url`` when no bus is provided). For testing,
pass an InMemoryBus instead.
"""
```

---

## Class Docstrings

Every public class must have a docstring explaining:

1. **What it is** (one-line summary).
2. **How to use it** — constructor parameters, key methods, expected lifecycle.
3. **Invariants** — what guarantees it maintains (e.g., "stateless between
   tasks", "thread-safe", "not safe for concurrent use").

Use reStructuredText-style cross-references for related classes:

```python
class PipelineOrchestrator(BaseActor):
    """
    Pipeline orchestrator with automatic stage parallelism.

    Processes an OrchestratorGoal by running it through a series of stages
    organized into execution levels based on their dependencies. Stages
    within the same level run concurrently; levels execute sequentially.
    Stage outputs are accumulated in a context dict and can be referenced
    by subsequent stages via input_mapping.
    """
```

**Private/internal classes:** A brief one-line docstring is sufficient.

---

## Function and Method Docstrings

### When to write a docstring

| Visibility | Rule |
|-----------|------|
| Public API (no underscore) | **Always** — full docstring |
| Protected (`_single_underscore`) | Required if non-trivial (>10 lines or complex logic) |
| Private (`__double_underscore`) | Optional — brief comment often suffices |
| Dunder methods (`__init__`, `__aiter__`) | Required if they accept non-obvious parameters |

### Docstring format

Use **Google-style** docstrings (rendered via mkdocstrings):

```python
async def call_worker(
    self,
    worker_type: str,
    payload: dict[str, Any],
    tier: str = "standard",
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Dispatch a task to a worker and wait for the result.

    Publishes a TaskMessage to heddle.tasks.incoming and subscribes to
    the result subject. Blocks until a matching TaskResult arrives or
    the timeout expires.

    Args:
        worker_type: Which worker config to dispatch to (e.g., "summarizer").
        payload: Structured input conforming to the worker's input_schema.
        tier: Model tier override. Defaults to "standard".
        timeout: Maximum seconds to wait for a result.

    Returns:
        The worker's output dict (the ``output`` field of TaskResult).

    Raises:
        BridgeTimeoutError: If no result arrives within ``timeout`` seconds.
        BridgeError: If the worker returns a FAILED status.
    """
```

### Rules

- **First line** is a concise imperative summary ("Dispatch a task", not
  "Dispatches a task" or "This method dispatches a task").
- **Blank line** between summary and body.
- **Args section**: list every parameter (except `self`/`cls`). Include types
  only if they add clarity beyond the annotation.
- **Returns section**: describe the return value structure. For dicts, mention
  key fields.
- **Raises section**: list exceptions the caller should handle. Omit generic
  exceptions that indicate bugs (e.g., `TypeError`).
- Keep docstrings **accurate** — an outdated docstring is worse than none. If
  you change a function's behavior, update the docstring in the same commit.

---

## Inline Comments

### When to comment

- **Why, not what.** Don't restate the code. Explain the reasoning behind a
  non-obvious choice.
- **Gotchas and edge cases** — especially Python quirks (e.g., `bool` is a
  subclass of `int`).
- **TODO markers** — use `# TODO(scope): ...` for planned future work,
  where `scope` is a short tag (issue number, review section, or feature
  name) that lets the next reader trace it back to the source review or
  tracking issue.
- **Performance notes** — if code is written a certain way for performance,
  say so.

### Style

```python
# Good — explains WHY
# Reject bools masquerading as ints (bool is a subclass of int)
if isinstance(value, bool) or not isinstance(value, int):

# Bad — restates the code
# Check if the value is a bool or not an int
if isinstance(value, bool) or not isinstance(value, int):
```

```python
# Good — marks a design decision
# Sequential processing — strict mailbox semantics
await self._process_one(data)

# Good — TODO with strategy reference
# TODO: Strategy A — streaming result collection
```

### Section headers

For long methods or complex logic, use section comment headers:

```python
# ------------------------------------------------------------------
# Dependency inference and execution level construction
# ------------------------------------------------------------------
```

Keep these consistent: 70-char dashes, no blank line before the first line
after the header.

### Lint suppressions (`# noqa`)

Every `# noqa` should carry its **why** inline. The rule code alone is not
self-documenting — six months later it's unclear whether the suppression is
load-bearing or stale.

```python
# Good — rule + reason
async def aclose(self) -> None:  # noqa: B027 — intentional no-op default for ABC
global _TRACING_INITIALIZED  # noqa: PLW0603 — module-level singleton flag is the simplest idempotency guard

# Bad — bare suppression
async def on_reload(self) -> None:  # noqa: B027
```

**Established suppressions in this codebase**, why they exist, and what would
break if they were removed:

| Rule | Where | Reason | Removing it breaks |
|------|-------|--------|---------------------|
| `B027` | ABC `aclose` defaults (worker/backends, worker/processor, worker/embeddings, orchestrator/store) | Empty async no-op is the *intended* default — concrete subclasses without I/O state should not be required to override. The contract test in `tests/test_async_client_lifecycle.py` calls `aclose` on every backend — those calls only succeed if the ABC defines the method as a real coroutine. | Test mocks and lightweight subclasses; the lifecycle contract test starts requiring stub overrides everywhere. |
| `B027` | `BaseActor.on_reload` (core/actor.py) | Hot-reload hook — the default is "do nothing" so actors that don't read config from disk inherit the no-op. | Any actor that doesn't override `on_reload` would need an explicit empty-body subclass. |
| `PLW0603` | `_TRACING_INITIALIZED` in tracing/otel.py | Module-level singleton flag is the simplest idempotency guard for `init_tracing`. The function is the only writer; encapsulating it in a class adds indirection without payoff. | Re-entrant `init_tracing` calls would re-trigger the OTel "Overriding TracerProvider not allowed" warning. |
| `PLR0912` / `PLR0915` | Long config validators and CLI run commands | Linear top-down flow that's clearer as one function than as a chain of helpers — splitting hurts readability without reducing complexity. | Config validation paths become harder to audit — the noqa is a deliberate choice over a refactor. |
| `ARG001` | FastAPI route handlers in `workshop/app.py` | FastAPI requires the `request: Request` parameter even when the body doesn't use it. | Removing the parameter changes the route signature and breaks dependency injection. |

When you add a new suppression, append a `—` followed by the reason on the
same line, and (if the reason has subtle implications) consider adding a row
to the table above.

---

## Error Handling

- **Raise specific exceptions**, not generic `Exception` or `RuntimeError`.
  Define custom exception classes for each module's failure modes:

```python
class PipelineStageError(Exception):
    """Raised when a pipeline stage fails or times out."""

    def __init__(self, stage_name: str, message: str):
        self.stage_name = stage_name
        super().__init__(message)
```

- **Don't silence exceptions** without logging:

```python
# Good
except Exception as e:
    logger.error("actor.error", actor_id=self.actor_id, error=str(e))

# Bad
except Exception:
    pass
```

- **Actor isolation**: individual message failures must not crash the actor
  loop. Catch at the message handler level and log.
- **Validate at boundaries**: check inputs at actor/API boundaries (contract
  validation, message parsing), trust internal code.

---

## Logging

Use **structlog** everywhere. Never use `print()` for operational output.

### Event naming convention

```text
{component}.{action}
```

Examples: `actor.connected`, `router.dead_letter`, `worker.tool_round`,
`pipeline.stage_completed`.

### Log levels

| Level | Use for |
|-------|---------|
| `debug` | Internal state details (message contents, intermediate values) |
| `info` | Normal operational events (connected, subscribed, task routed) |
| `warning` | Recoverable issues (unknown tool, condition parse failure, rate limit) |
| `error` | Failures that affect the current operation (task failed, backend error) |

### Structured fields

Always pass context as keyword arguments, not interpolated strings:

```python
# Good
logger.info("router.routing", task_id=task.task_id, tier=tier.value)

# Bad
logger.info(f"Routing task {task.task_id} to tier {tier.value}")
```

---

## Testing

### File naming

- Test file: `test_{module_name}.py` (mirrors `src/heddle/{package}/{module}.py`)
- Test class: `Test{ClassName}` (e.g., `TestPipelineOrchestrator`)
- Test function: `test_{behavior_description}` (e.g.,
  `test_stage_timeout_produces_failed_result`)

### Test organization

```python
"""Tests for heddle.orchestrator.pipeline — PipelineOrchestrator."""
import pytest

from heddle.orchestrator.pipeline import PipelineOrchestrator, PipelineStageError


class TestBuildExecutionLevels:
    """Execution level construction from dependency graphs."""

    def test_independent_stages_in_single_level(self):
        ...

    def test_circular_dependency_raises(self):
        ...


class TestExecuteStage:
    """Single-stage execution with mocked bus."""

    @pytest.fixture
    def pipeline(self):
        ...
```

### Rules

- **No infrastructure required** for unit tests. Use `InMemoryBus` and
  `InMemoryCheckpointStore`.
- Mark integration tests with `@pytest.mark.integration`.
- **Every new feature** must include unit tests. PRs without tests for new code
  will not be merged.
- **Test the contract, not the implementation.** If you test internal methods
  directly, your tests are coupled to implementation details.
- Use `pytest.fixture` for shared setup. Keep fixtures close to where they're
  used (same file or `conftest.py`).
- `asyncio_mode = "auto"` is configured — async test functions work without the
  `@pytest.mark.asyncio` decorator.

### Coverage

**Total threshold: 91%** (enforced in CI via `fail_under` in
`[tool.coverage.report]`). Raise only when the current run sustains
the new floor with margin.

**Per-package floors** (documented expectations — pytest-cov does
not gate per package, so these are enforced by review, not CI):

| Package | Floor | Rationale |
| --- | --- | --- |
| `core/`, `bus/`, `worker/`, `orchestrator/`, `router/` | 95% | Hot-path runtime; regressions hit every user. |
| `cli/`, `workshop/`, `mcp/`, `scheduler/` | 85% | Operator-facing surfaces; many paths exercised end-to-end. |
| `contrib/` | 70% | Optional integrations; some require external infra to fully cover. |

A package below its floor is a release blocker unless explicitly
waived in the PR description. Today `core/` sits at ~94% — the delta
is concentrated in `core/config.py`; lifting it is queued as part of
the ongoing core/contracts hardening work tracked in the repository
review reports.

Use `# pragma: no cover` only for truly unreachable code (e.g.,
`if __name__ == "__main__"` guards, `TYPE_CHECKING` blocks).

---

## Dependency Security

CI runs `pip-audit` against the resolved environment (`uv sync --all-extras`)
on every push and PR. The `audit` job fails when any dependency has a known
CVE with a fix available upstream — unless the CVE id is in the explicit
baseline at `.github/pip-audit-ignore.txt`.

**Baseline maintenance:**

- Dependabot (`.github/dependabot.yml`) opens weekly PRs for `pip` and
  `github-actions`. Reviewers must remove the corresponding lines from
  `pip-audit-ignore.txt` when the bump lands.
- Adding an id to the baseline without an accompanying tracking note
  (issue link, PR number, or "next Dependabot cycle") is grounds to block
  the PR. The baseline is not a snooze button.
- A new advisory that has no fix yet is *not* a CI failure (pip-audit
  only fails on fixable vulns); it goes into a tracking issue instead.

**Optional-extra surface:**

The set of installable extras directly determines vulnerability surface.
Operators picking a minimal install can reduce exposure:

| Extra | Pulls heaviest deps | Notes |
| --- | --- | --- |
| `docproc` (`docling`) | `lxml`, `pillow`, `transformers`, `accelerate` | Largest surface — skip if you don't need PDF/DOCX ingestion. |
| `workshop` | `fastapi`, `uvicorn`, `python-multipart` | Operator UI; gate behind `HEDDLE_WORKSHOP_TOKEN` (see SECURITY_MODEL). |
| `mcp` | `fastmcp` (+ auth deps like `authlib`) | MCP gateway surface; only install on hosts that serve MCP. |
| `rag` | `duckdb`, `requests` | Smaller surface; mostly local DB / Ollama HTTP. |
| `lancedb` | `lancedb`, `pyarrow` | Native libs; review for arch-specific advisories. |
| `mdns`, `otel`, `tui`, `telegram`, `chatbridge` | Small, narrow surface | Install only when the corresponding feature is in use. |

The `dependencies` block in `pyproject.toml` is the always-installed floor:
`nats-py`, `pydantic`, `pyyaml`, `httpx`, `tiktoken`, `click`, `structlog`.

---

## YAML Configuration

Worker and pipeline configs live in `configs/`. Follow these conventions:

- **Top-level keys** in `snake_case`.
- **Include a comment header** explaining what the config does:

```yaml
# summarizer.yaml — Summarize text into structured output.
# Tier: local (Ollama). See _template.yaml for all available keys.
name: summarizer
worker_type: summarizer
```

- **Schema fields** (`input_schema`, `output_schema`) must be valid JSON Schema
  (type: object, with properties and required arrays).
- **Keep configs narrow** — one responsibility per worker. Don't combine
  summarization and classification in a single config.

---

## Git Workflow

- **Branch naming:** `feature/description`, `fix/description`,
  `docs/description`.
- **Commit messages:** imperative mood, concise summary line (<72 chars), with
  optional body explaining why:

```text
Add dependency inference to PipelineOrchestrator

Stages now auto-infer dependencies from input_mapping paths instead of
requiring explicit depends_on lists. Uses Kahn's algorithm for
topological sort to detect cycles at config load time.
```

- **One logical change per commit.** Don't mix refactors with features.
- Run `uv run ruff check src/ && uv run pytest tests/ -v -m "not integration"`
  before pushing.

### Pre-push checklist for lifecycle-critical code

The full pytest suite (~40s) is **mandatory before push** when a change
touches any of:

- async-blocking primitives (`asyncio.sleep`, `asyncio.Event`, `asyncio.wait`,
  cancellation, timeouts)
- module-level singleton state (init_tracing, lazy global caches)
- the bus (`bus/`, `MessageBus` ABC) — pre-connect / connect / close ordering
- actor lifecycle (`core/actor.py`, `worker/base.py`, orchestrator `handle_message`)
- CLI entry points (`cli/main.py`) that block until SIGINT/SIGTERM
- mocks-as-test-double surface in `tests/test_*.py`

These areas have non-local interactions that file-scoped tests miss.
Mutation tests catch the bug being fixed; only the full suite catches
adjacent tests that depended on the *previous* behaviour. Two CI runs
(`513391c` test flake hidden by an unrelated job, `0ef39a2` mdns CLI
test hung for 1h36m) were caused by skipping this step.

When `replace_all=True` on test mocks doesn't cover every site
(different surrounding context per call), grep the entire test tree for
the old pattern after the production change to be sure no stale mock
survives.

---

## Reference Examples

The following source files exemplify these standards and should be used as
references when writing new code:

| Pattern | Reference file |
|---------|---------------|
| Module docstring with architecture context | `src/heddle/orchestrator/pipeline.py` |
| Class with lifecycle documentation | `src/heddle/core/actor.py` |
| Function with Args/Returns/Raises | `src/heddle/worker/runner.py` (`execute_with_tools`) |
| Custom exception hierarchy | `src/heddle/orchestrator/pipeline.py` |
| ABC with usage examples in docstring | `src/heddle/bus/base.py` |
| Pydantic models with field documentation | `src/heddle/core/messages.py` |
| Section headers in long classes | `src/heddle/orchestrator/pipeline.py` |
| Structured logging conventions | `src/heddle/router/router.py` |
| Contract validation with design rationale | `src/heddle/core/contracts.py` |
| Contrib backend with config docs | `src/heddle/contrib/rag/backends.py` |
