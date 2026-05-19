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

## Sprint 2 → Sprint 3 behavior boundary

These behaviors of `heddle.contrib.events` are described in
`heddle-contrib-events-m2-architecture-v7.md` and were partially
implemented in Sprint 2 (in-memory) and completed in Sprint 3
(JetStream + Valkey + cache + snapshot). The table captures
exactly what observably changes.

| Behavior | Sprint 2 (in-memory) | Sprint 3 (production) |
|---|---|---|
| Dedup within a single `handle()` call | ✓ via in-memory buffer | ✓ unchanged |
| Dedup across `handle()` calls in same process | ✗ aggregate reloaded each call; buffer reset | ✓ aggregate cached (T2); buffer survives |
| Dedup across processes | ✗ N/A | ✓ via published `mark_processed` events to `heddle.dedup.{type}.{id}` (T4) updating cached aggregates in other processes |
| Aggregate rebuild from empty log | empty dedup buffer (per v7 §4.5) | empty dedup buffer (unchanged) |
| Aggregate rebuild after snapshot exists | N/A (no snapshot store) | dedup buffer restored from snapshot |
| Aggregate rebuild from log replay only (no snapshot) | structurally the only path | last-resort path; buffer empty (v7 §4.5 — buffer is NEVER reconstructed from event replay alone) |
| EventDispatcher subscription race | ✗ `start(type)` returned before subscription registered (callers needed polling helper) | ✓ `start(type)` awaits readiness (T1) |
| Finalization atomicity (P2 vs P3) | ✗ P3 was a stub; only P2 fired | ✓ both projectors gated by Valkey lease on `heddle:events:horizon:{type}:{id}`; loser preempted (T5/T6/T7) |
| Cascade idempotence | ✓ via deterministic command_id (sha256-derived) | ✓ unchanged; lease (T5) adds audit-trail clarity |
| Forged `*.InternalFinalized` defense | application-layer only — `apply()` provenance check + `CorruptAggregateAlert` | application-layer (unchanged) + NATS publish ACL (T10/ADR-013) blocking forged publishes upstream |

**The v7 invariant is preserved.** The dedup buffer is restored
from snapshot or stays empty after pure-replay rebuilds — *never*
reconstructed from replayed events' metadata. Cross-process dedup
is via `mark_processed` events updating cached buffers, not via
replay-derivation.

**Sprint 3 makes these signals observable.** The Sprint 2 J4
internal contradiction (snapshot-only buffer claim plus cross-call
dedup expectation) resolves once snapshots ship. Three paired tests
document the shift:

- `test_dedup_was_per_call_in_sprint_2` — pins Sprint 2 behavior
  (no cache, no snapshot) for regression purposes.
- `test_dedup_works_within_call_via_cache` — Sprint 3 cache alone
  (default-on) makes in-process dedup work.
- `test_dedup_works_cross_call_with_snapshot` — Sprint 3 positive
  case across CommandHandler instances.

## Aggregate cache (Sprint 3)

`AggregateCache` is a process-local LRU keyed on
`(aggregate_type, aggregate_id)`. Default size: 1024. Configurable
via the `cache` argument to `CommandHandler.__init__`; pass
`AggregateCache(max_size=0)` to disable caching entirely (useful
for reproducing Sprint 2's reload-every-call semantics in tests).

Cache eviction fires the `on_evict` callback, which the
CommandHandler wires to `DedupSubscriber.unsubscribe` so that
cross-process dedup subscriptions don't outlive their aggregate.

## Snapshot store (Sprint 3)

`SnapshotStore` adapts the existing `KeyValueStore` to aggregate
snapshots. Keys:

- Blob: `heddle:events:snapshot:{aggregate_type}:{aggregate_id}`
- Version: `heddle:events:snapshot:version:{aggregate_type}:{aggregate_id}`

The version key lets `CommandHandler._load_or_create` replay only
events with `aggregate_version > snapshot_version`. Snapshot-on-
write is count-based — every `SNAPSHOT_EVERY_N` events on the
cached aggregate. The brief's time-based trigger
(`SNAPSHOT_EVERY_T_SECONDS`) is recorded as a constant but not
enforced in Sprint 3.

The dedup buffer survives snapshot round-trips. A pure-replay
rebuild (no snapshot) starts with an empty buffer — v7 §4.5
invariant.

## Hybrid dedup (Sprint 3)

After a successful `handle()` the in-memory aggregate's
`mark_processed(command_id)` is called synchronously (same-process
dedup works immediately). The command_id is then published to
`heddle.dedup.{type}.{id}` on NATS core (not JetStream — these
events don't need durability).

Any CommandHandler instance with the same aggregate in its cache
has a NATS-core subscription on that subject; on receive, it calls
`cached_aggregate.mark_processed(command_id)`. Cross-process dedup
correctness is best-effort, bounded by cache hit rate and snapshot
recency.

`NullDedupPublisher` / `NullDedupSubscriber` are the defaults —
single-process deployments and tests pay no NATS cost.

## Finalization lease (Sprint 3)

`finalization_lease` is an async context manager that claims a
Valkey lease keyed on `heddle:events:horizon:{type}:{id}` via the
`KeyValueStore.set_if_not_exists` primitive (atomic SETNX EX with
TTL). Default TTL: 30 seconds.

P2 (cascade) and P3 (horizon) both attempt the lease before
publishing `InternalFinalize`. Whichever claims first wins; the
other logs and skips. The lease is **not** released in the normal
flow — releasing early invites re-claim races. Crashed projectors
recover automatically after TTL.

The lease is for **audit clarity**, not state correctness. The
receiving aggregate's `apply_internal_finalized` already no-ops on
already-finalized aggregates; the lease prevents redundant writes
plus gives a clean "who finalized this" signal in the audit trail.

## P3 horizon timers (Sprint 3)

`FinalizationHorizonProjector` maintains one `asyncio.Task` per
active aggregate. Each `IntervalAggregate` subclass may override
`HORIZON_TIMEOUT_SECONDS` as a ClassVar; the default is 24 hours.
Sprint 4a domain aggregates (e.g., `OperatorJobSession`) will set
their own.

Timers are armed when P3 observes any event on an
`IntervalAggregate`-typed aggregate (proxy for "active"), cancelled
when an `InternalFinalized` is observed. `shutdown()` cancels all
timers cleanly on dispatcher stop.

## JetStream subjects and streams (Sprint 3)

| Subject pattern | Used for | Transport |
|---|---|---|
| `heddle.events.{type}.{id}.{event_type}` | EventEnvelope publish | JetStream (`HEDDLE_EVENTS_{TYPE}`) |
| `heddle.commands.{type}.{id}.{command_type}` | CommandMessage publish | JetStream (`HEDDLE_COMMANDS_{TYPE}`) |
| `heddle.rejections.{type}.{id}.{command_type}` | RejectionEnvelope publish | JetStream (`HEDDLE_REJECTIONS_{TYPE}`) |
| `heddle.dedup.{type}.{id}` | mark_processed publish | NATS core (ephemeral) |
| `heddle:events:snapshot:{type}:{id}` | Aggregate snapshot blob | Valkey KV (string) |
| `heddle:events:horizon:{type}:{id}` | Finalization lease | Valkey KV (SET NX EX) |

Streams are configured idempotently via the
`heddle.contrib.events.jetstream.ensure_event_stream` /
`ensure_command_stream` / `ensure_rejection_stream` helpers. Defaults:
file storage, single replica, age-based retention (7 days for events
and commands, 30 days for rejections).

CAS append for events uses the `Nats-Expected-Last-Subject-Sequence`
header. JetStream's wrong-last-sequence error (APIError code 10071)
is translated to `ConcurrencyError`.

## SLI signals (Sprint 3)

`heddle.contrib.events.sli` provides three OpenTelemetry-compatible
histogram observations:

| Signal | Labels |
|---|---|
| `command_handle_duration` | `aggregate_type`, `command_type`, `outcome` (success / rejected / concurrency_error / error) |
| `dispatcher_fan_out_duration` | `aggregate_type` |
| `lease_acquisition_duration` | `aggregate_type`, `projector_name`, `outcome` (claimed / preempted) |

Applications install a `Recorder` once at startup via
`install_recorder(recorder)`. The default is a no-op; tests can use
a `_CapturingRecorder` (or write their own) to assert specific
observations fire.

## NATS auth and publish ACLs (Sprint 3)

See [ADR-013](adr/013-nats-auth-model.md) and the
[`nats-acl-configuration` runbook](runbooks/nats-acl-configuration.md).
Four roles: `framework`, `application`, `observer`, `workshop`.
Only `framework` may publish `*.InternalFinalized` — the structural
defense behind the `Aggregate.apply()` provenance check.

## The `snake_case` convention

Cross-module framework helpers within a `heddle.contrib.<pkg>`
package use **no** underscore prefix. Module-local helpers keep
the leading underscore. Precedent: Sprint 2 J3 promoted
`_snake_case` → `snake_case` on `heddle.contrib.events.aggregate`
when `CommandHandler` started importing it from another module.
