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

---

## Issuer conventions (heddle.contrib.events)

Every event and command carried through `heddle.contrib.events` has a
`metadata.issued_by` field with one of six reserved prefixes. This
field is **semi-structured** — the prefix is constrained; everything
after is free-form.

| Prefix | Issuer | Example |
|---|---|---|
| `framework:` | Internal framework projectors (P1/P2/P3) and infrastructure | `framework:cascade`, `framework:horizon`, `framework:scope_membership`, `framework:bootstrap` |
| `observer:{name}` | Scheduled PF observers | `observer:pf_job_status`, `observer:pf_route_step` |
| `projector:{name}` | Application projectors emitting events as a side effect of projection | `projector:operation_labor_projector` |
| `user:badge:{id}` | Shop-floor operator action via badge scan | `user:badge:206` |
| `user:system:{component}` | Application-mediated, non-operator-specific | `user:system:shoppulse_admin`, `user:system:emergency_correction:{engineer_id}` |
| `bridge:{worker_type}` | (Post-M2) gateway/bridge translating LLM/processor worker results | `bridge:fault_classifier_llm` |

### Multi-segment suffixes

Each prefix governs only the leading segment(s) up to and including
its named scope. Everything after is opaque to the validator. For
example, `is_user_issuer` accepts any string starting with `user:` —
including multi-segment forms like:

- `user:badge:123`
- `user:system:emergency_correction:eng-42`
- `user:system:tool:abc:def`

Validators MUST NOT cap segment count. This is what allows the
emergency-correction runbook (§4.12 of the M2 architecture doc) to
encode an `{engineer_id}` after `user:system:emergency_correction`.

### Provenance enforcement

Aggregate `apply()` methods MAY enforce `issued_by` requirements for
specific event types. The canonical example: `InternalFinalized`
events MUST have `issued_by` starting with `framework:` — see
`Aggregate.apply()` (Sprint 2).

### Runtime check

```python
from heddle.contrib.events.issuer_conventions import (
    is_framework_issuer,
    is_observer_issuer,
    is_projector_issuer,
    is_user_issuer,
    is_system_issuer,
    is_bridge_issuer,
    is_recognized_issuer,
)
```

The `is_system_issuer` helper is a *strict subcheck* of
`is_user_issuer`: it accepts only `user:system:*` values. Code paths
that must reject operator-initiated commands (e.g.,
emergency-correction tooling) should use `is_system_issuer`, not
`is_user_issuer`.

## Aggregate base classes (heddle.contrib.events)

Sprint 2 of the M2 plan adds three abstract bases that concrete
aggregates (Sprint 4a) subclass:

- **`Aggregate`** — identity (`aggregate_type`, `aggregate_id`),
  monotonic `aggregate_version`, the snapshot-only N=512 dedup ring
  buffer, and the `apply()` discipline.
- **`IntervalAggregate`** — adds the
  `created` → `active` → `finalized` phase machine and the
  framework-supplied `apply_internal_finalized`. Concrete examples
  (Sprint 4a): `OperatorJobSession`, `Operation`.
- **`RootAggregate`** — adds the child registry that P2
  (`CascadeProjector`) reads when a root finalizes. Concrete
  example: `Job`.

### The `apply()` discipline

`apply()` is the sole state-mutation path. It MUST be deterministic
(same event sequence → same end state across replays), MUST NOT do
I/O, and MUST NOT call out to the bus. The order of checks inside
`apply()`:

1. **Provenance.** Events in `FRAMEWORK_ONLY_EVENT_TYPES` (currently
   just `InternalFinalized`) MUST carry `issued_by` starting with
   `framework:`. Otherwise `CorruptAggregateAlert` — the
   application-layer backstop for the Sprint 3 NATS publish ACL on
   `*.InternalFinalized` subjects.
2. **Version monotonicity.** `envelope.aggregate_version` must
   equal `self.aggregate_version + 1`. Checked *before* dispatching
   to the handler so a bad envelope cannot leave the aggregate in a
   partially-mutated state.
3. **Dispatch.** Look up `apply_<event_type_snake>` and invoke. A
   missing handler raises `UnknownEventVersionError` (forward-compat
   marker: a downgrade-from-newer-cluster scenario). Handler
   exceptions other than `AggregateInvariantError` /
   `CorruptAggregateAlert` are wrapped as `AggregateInvariantError`
   with the original cause chained.
4. **Commit.** Only after the handler returns cleanly:
   `self.aggregate_version = envelope.aggregate_version`.

### Snapshot-only dedup buffer

`Aggregate` keeps a `deque(maxlen=512)` of processed command IDs.
`has_processed(command_id)` is the dedup check; `mark_processed`
is called by `CommandHandler` post-commit. The buffer is
**snapshot-only** — pure event replay rebuilds an empty buffer.
This means cross-call dedup is structurally impossible until Sprint
3 ships the KV-snapshot path; Sprint 2's in-memory implementation
relies on receiver-side rejection (e.g., `ALREADY_FINALIZED`) for
idempotence.

## Aggregate registration

```python
from heddle.contrib.events.aggregate import RootAggregate
from heddle.contrib.events.registry import register_aggregate


@register_aggregate("Job")
class JobAggregate(RootAggregate):
    def apply_job_shipped_from_pf(self, payload, metadata):
        ...
```

The decorator sets the class's `aggregate_type` ClassVar and adds
the class to the process-global `AGGREGATE_REGISTRY`. Re-registering
the same `(name, class)` is a no-op; re-registering the same name
with a different class raises `ValueError` to prevent silent
shadowing.

`@register_aggregate` was chosen over a `__init_subclass__` hook so
the wiring is explicit (greppable, debuggable) and abstract bases
can't accidentally register themselves.

`get_aggregate_class(name)` returns the class or raises `KeyError`.
`is_root_type(name)` returns True iff the registered class subclasses
`RootAggregate` — used by P1 and P2 to decide whether to maintain
membership / cascade.

## EventLog and RejectionLog

`EventLog` is the per-aggregate-type append-only event store.
Sprint 2 ships `InMemoryEventLog`; Sprint 3 swaps in
`JetStreamEventLog` without changing the ABC surface.

- `append(envelope, expected_version)` — CAS append. `None` skips
  the version check (used by Sprint 4a PF observers that fabricate
  envelopes from PF rows without prior state). Envelope-level
  monotonicity (`aggregate_version == current + 1`) is ALWAYS
  enforced.
- `load(aggregate_type, aggregate_id, from_version=0)` — async
  stream in aggregate_version order, yielding events with
  `aggregate_version > from_version`.
- `subscribe(aggregate_type)` — async stream of newly-appended
  events for the given type. Yields forever unless cancelled.

`RejectionLog` is the parallel append-only audit stream for
rejected commands. No CAS, no per-aggregate ordering. The Sprint 3
JetStream version uses `HEDDLE_REJECTIONS_{TYPE}` streams so
rejections can be queried independently. `RejectionEnvelope` is
exported to `schemas/v1/rejection_envelope.schema.json`.

## CommandHandler flow

`CommandHandler.handle(cmd)` is the nine-step orchestration per
v7 §4.6:

1. Look up the aggregate class via the registry.
2. Rebuild the aggregate from event-log replay (no snapshot path
   in Sprint 2; Sprint 3 adds the KV snapshot fast path).
3. `has_processed(command_id)` — if True, scan log for the matching
   event and return it (idempotent retry). The fall-through branch
   (buffer says yes but event missing) handles a Sprint 3 snapshot
   edge case.
4. Validate `expected_aggregate_version` if not None →
   `ConcurrencyError` on mismatch.
5. Dispatch to `handle_<command_type_snake>` →
   `AttributeError` if missing.
6. Handler returns `(event_type, event_payload)` or raises
   `CommandRejected`.
7. On `CommandRejected`: append `RejectionEnvelope` to the
   `RejectionLog`, re-raise.
8. Build `EventEnvelope` (new event_id, version=current+1,
   propagating `command_id` / `correlation_id` / `issued_by` from
   the command).
9. `event_log.append(envelope, expected_version=current_version)`,
   then `aggregate.apply(envelope)`, then
   `aggregate.mark_processed(cmd.command_id)`. Return the envelope.

## EventDispatcher and framework projectors

`EventDispatcher` subscribes to `EventLog` per aggregate type and
fans events out to registered projectors **serially** in
registration order. A projector raising an exception is logged but
does not stop subsequent projectors; projectors must NOT assume
parallel-safety.

`Projector.project()` must be idempotent — the dispatcher may
re-deliver an event after a crash. Projectors emitting commands or
events propagate the source event's `command_id` /
`correlation_id` for trace continuity.

The three framework projectors:

- **P1 `ScopeMembershipProjector`** — complete. Maintains the
  `(root_type, root_id) → child_type → {child_ids}` view in memory
  by reading the reserved `_child_membership` payload key on root
  events.
- **P2 `CascadeProjector`** — complete. On a root's
  `InternalFinalized` event, fans out `InternalFinalize` commands
  to all registered children with `issued_by='framework:cascade'`
  and a deterministic `command_id` over
  `(root_id, child_id, root_event_id)`. `CommandRejected` /
  `ConcurrencyError` are swallowed (cascade is opportunistic).
- **P3 `FinalizationHorizonProjector`** — STUB in Sprint 2. ABC +
  empty `project()`. Full implementation in Sprint 3 alongside the
  Valkey atomicity-window mechanism.

Application projectors that emit events as a side effect of
projection use `issued_by='projector:<name>'`; framework projectors
P1/P2/P3 use `issued_by='framework:<name>'`.

## Test surface

Two paths for tests that touch the events runtime:

- **Downstream apps** import factories and reusable fake aggregates
  from `heddle.contrib.events.testing` — `make_event`,
  `make_command`, `FakeIntervalAggregate`, `FakeRootAggregate`.
  These are part of the package's public surface.
- **heddle's own tests** use pytest fixtures from
  `heddle/tests/fixtures.py`: `registry_isolation`,
  `in_memory_event_log`, `in_memory_rejection_log`,
  `command_handler`, `membership_projector`, `wired_dispatcher`
  (pre-wired with P1 + P2). The fixtures are star-imported by
  `tests/conftest.py` so they're available tree-wide.

`registry_isolation` snapshots `AGGREGATE_REGISTRY` at fixture
setup and restores at teardown. Tests that register fake aggregates
should depend on this fixture (e.g., via
`pytest.mark.usefixtures("registry_isolation")`) to avoid leaking
registrations across tests.
