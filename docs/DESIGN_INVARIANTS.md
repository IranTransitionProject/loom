# Heddle Design Invariants — Framework Safety Contracts

**Purpose:** This document describes the non-obvious design decisions,
deliberate constraints, and architectural invariants enforced by Heddle's
framework code. It exists because well-intentioned contributors — human or
LLM — routinely propose "improvements" that would break these invariants.

Read this before proposing structural changes to Heddle. Every section
explains *what* the invariant is, *why* it exists, and *how it fails* if
violated.

**Scope.** This file covers invariants that are mechanically checked by the
framework — test suites, type checks, validators, or code-path structure
enforce them. Patterns that apply to *applications* built on Heddle (blind
audit pipelines, knowledge-silo discipline, behavioural-monitor isolation)
moved to [Application Patterns](APPLICATION_PATTERNS.md) on 2026-05-11; that
file is the new home for what used to be Part II.

---

## Part I — Framework Invariants

### 1. Worker statelessness is enforced, not optional

Workers process one task, then `reset()`. The `reset()` call is unconditional —
it executes even if the task raised an exception. There is no mechanism to carry
state between tasks because workers are deployed as NATS queue group replicas.

**Why:** If replica A processes task 1 and replica B processes task 2, any state
accumulated during task 1 is invisible to replica B. Stateful workers silently
diverge when horizontally scaled.

**How it fails:** Instance variables that persist across tasks produce correct
results in single-replica testing and corrupt results in multi-replica production.
The failure is silent and data-dependent — the hardest kind to diagnose.

**Paired ADR:** [ADR-001](adr/001-stateless-workers.md).

### 2. The router is deterministic — no LLM in the routing path {: #2-the-router-is-deterministic--no-llm-in-the-routing-path }

The `TaskRouter` dispatches by `worker_type` and `model_tier` using rules from
`router_rules.yaml`. It never calls an LLM.

**Why:** Routing must be fast (sub-millisecond), predictable, and auditable.
An LLM in the routing path would add latency, cost, and non-determinism to
every single task dispatch. The decomposer already chose the worker_type — the
router just delivers it.

**How it fails:** Adding "smart routing" (e.g., LLM-based worker selection)
creates a recursive dependency: the router needs an LLM call, which needs
routing, which needs an LLM call. It also makes dispatch latency unpredictable
and adds cost proportional to total task volume.

**Paired ADR:** [ADR-002](adr/002-deterministic-router.md).

### 3. Rate limiting is dispatch-side only

The token bucket in the router tracks dispatched tasks, not completed tasks.
When a task is published to a worker queue, a token is consumed immediately.
The bucket does not know when (or whether) the worker finishes.

**Why:** True backpressure would require completion callbacks from every worker,
adding a round-trip per task. The current design is a simple dispatch throttle
that prevents flooding worker queues. It is explicitly not a concurrency limiter.

**How it fails:** If you assume the rate limiter caps concurrent in-flight tasks,
you will over-provision workers. N dispatched tasks can all be in-flight
simultaneously if workers are slow. The bucket only prevents *new* dispatches
from exceeding the configured rate.

### 4. Config validation returns error lists, not exceptions

Every `validate_*` function in `config.py` returns `list[str]`. An empty list
means valid. A non-empty list contains all errors found.

**Why:** Different callers need different error handling. The CLI aborts on the
first error. The Workshop collects all errors and displays them together. Eval
runs might log and continue. Exceptions force a single handling strategy.

**How it fails:** If validation raises exceptions, you can only ever report the
first error. Users must fix one error, re-run, discover the next error, and
repeat — a frustrating cycle that compound validation avoids.

### 5. JSON Schema validation is intentionally shallow

`contracts.py` validates required fields and shallow types. It does not validate
nested objects, `$ref`, `allOf`/`oneOf`, or string format constraints. It does
not use the `jsonschema` library.

**Why:** Every worker has I/O schemas. The 90% case is "does this dict have the
right top-level keys with the right types?" Full JSON Schema validation would
add a dependency, increase per-message overhead, and encourage schema complexity
that LLMs struggle to satisfy. Shallow validation catches misconfigured workers;
deep validation is the LLM's job via system prompt instructions.

**How it fails:** If you add complex schemas (nested required fields, conditional
subschemas), the validator silently accepts invalid data. The contract is: keep
schemas shallow, and this validator is sufficient.

**Note on `schema_ref`:** The `input_schema_ref`/`output_schema_ref` feature
(v0.7.0) resolves Pydantic models to JSON Schema at config load time. It does
not change the validation depth — the resolved schema is still validated
shallowly by `contracts.py`. `schema_ref` is about *where schemas are defined*
(Python models vs. inline YAML), not *how deeply they are checked*.

**Critical detail:** Boolean checks come before integer checks because Python's
`bool` is a subclass of `int`. Without this ordering, `True` validates as an
integer, and workers receive wrong types.

**Paired ADR:** [ADR-003](adr/003-shallow-json-schema.md).

### 6. Dependency inference from input_mapping is the parallelism mechanism

`PipelineOrchestrator` parses `input_mapping` paths to determine which stages
depend on which. A path like `stages.source_process.output.claims` creates a
dependency on the `source_process` stage. Paths starting with `goal.*` have no
inter-stage dependency. Kahn's topological sort groups independent stages into
execution levels that run concurrently via `asyncio.wait(FIRST_COMPLETED)`
(see Invariant 16 for the implementation rationale).

**Why:** Explicit `depends_on` annotations are error-prone and redundant — the
data flow already encodes the dependency graph. Auto-inference means pipeline
authors get parallelism for free when they design independent stages.

**How it fails:** If you modify `input_mapping` without understanding that it
defines the execution graph, you can accidentally serialize previously parallel
stages (performance regression) or parallelize stages that need sequencing
(data race, missing inputs).

### 7. Per-goal state isolation enables concurrency without locks

`OrchestratorActor` stores all mutable state inside per-goal `GoalState`
containers. There is no global mutable state, no shared counters, no locks.
When `max_concurrent_goals > 1`, goals run concurrently with zero
synchronization overhead.

**Why:** Locks serialize execution and create deadlock risk. Per-goal isolation
means concurrent goals are as independent as separate processes, but cheaper.

**How it fails:** Adding any shared mutable state (even an innocent counter or
metric accumulator) between goals re-introduces the need for synchronization.
A single shared list or dict without a lock will corrupt under concurrent access.
With a lock, you've created a serialization bottleneck that defeats the purpose
of concurrent goals.

**Paired ADR:** [ADR-009](adr/009-per-goal-state-isolation.md).

### 8. Malformed NATS messages are skipped, not crashed

`NATSBus` catches `json.JSONDecodeError` and `UnicodeDecodeError` on incoming
messages, logs a warning, and continues processing the subscription.

**Why:** A single corrupted message must not halt an entire worker. In production
with high message volume, transient corruption (network glitches, partial writes)
would repeatedly crash workers if treated as fatal.

**How it fails:** If you change this to raise an exception, one bad message kills
the subscription loop, and all subsequent valid messages go unprocessed until
the worker is restarted. The bad message remains in NATS, so the worker crashes
again on restart.

**Paired ADR:** [ADR-004](adr/004-skip-not-crash-on-malformed.md).

### 9. OpenTelemetry is optional via runtime feature detection

The `tracing/otel.py` module uses `contextlib.suppress(ImportError)` to
conditionally import OTel SDK. If not installed, a `_HAS_OTEL` flag stays
`False` and all public functions become no-ops. The module is always importable.

**Why:** Tracing is valuable but not required. Production code calls tracing
functions unconditionally without conditional imports. This keeps instrumentation
code clean while allowing bare-metal deployments without OTel.

**How it fails:** If you remove the `suppress(ImportError)`, any deployment
without `uv sync --extra otel` crashes at import time.

### 10. Condition evaluation: malformed → FALSE (skip), missing path → FALSE (skip) {: #10-condition-evaluation-malformed--false-skip-missing-path--false-skip }

Pipeline stage condition evaluation has three failure modes with a unified
fail-closed default:

- **Malformed condition** (wrong format, not three tokens): defaults to
  **FALSE** (skip the stage) and logs `pipeline.invalid_condition`. A typo in
  the condition syntax skips the stage rather than running it silently
  broadened.
- **Missing path** (path references a context key that doesn't exist):
  defaults to **FALSE** (skip the stage) and logs
  `pipeline.condition_missing_path`. The expected behavior for conditional
  stages whose upstream didn't produce the optional field.
- **Unknown operator** (anything other than `==` / `!=`): defaults to
  **FALSE** (skip the stage) and logs `pipeline.unsupported_operator`.

> **Summary: any uncertainty in condition evaluation skips the stage and
> logs a warning.**

Legacy fail-open behaviour (malformed → TRUE, unknown operator → TRUE) is
opt-in via `HEDDLE_STRICT_CONDITIONS=0` for one-release migration of
pipelines that depend on the prior shape.

**Why:** The pre-G7 shape defaulted malformed → TRUE so a typo couldn't drop
a stage. Production experience surfaced the opposite failure mode: a missing
space in `extract.output.x==true` silently broadened the pipeline. Fail-closed
makes both error modes equally visible — the operator sees "stage didn't
run" plus `pipeline.invalid_condition` in the logs.

**How it fails:** If you change missing-path or malformed-condition to TRUE
by default, every conditional stage runs unconditionally when the upstream
field is absent (defeats the purpose of conditions) or when the YAML has a
typo (broadens the pipeline silently).

**Paired ADR:** [ADR-010](adr/010-condition-eval-defaults.md).

### 11. ProcessorWorker serialize_writes is per-instance only

`SyncProcessingBackend` with `serialize_writes=True` uses an `asyncio.Lock`
to serialize calls within a single process. It does NOT protect against
concurrent writes from multiple instances.

**Why:** Cross-process locking requires external coordination (file locks, Valkey
locks) which adds infrastructure dependencies. The design contract is: run
exactly one processor instance for single-writer backends like DuckDB.

**How it fails:** Running two processor instances with `serialize_writes=True`
against the same DuckDB file causes database corruption. The per-instance lock
is useless — each instance has its own lock.

### 12. Dead-letter store is bounded with FIFO eviction

`DeadLetterConsumer` stores at most `max_size` entries (default 1000), discarding
oldest entries when full. Entries are inserted most-recent-first.

**Why:** In a system producing many errors, unbounded dead-letter storage
becomes a memory leak. Operators care about recent failures; ancient failures
are diagnosable from logs.

**How it fails:** Removing the size limit turns dead-letter storage into
runaway memory consumption. Under sustained error conditions (e.g., a
misconfigured worker), the store grows indefinitely until OOM.

### 13. Path traversal protection uses resolved absolute paths

`WorkspaceManager` canonicalizes both the workspace root and the requested
file path with `.resolve()` before comparing. This catches `../` traversal
and symlink escapes.

**Why:** Workers can resolve `file://` references in their payloads. Without
canonicalization, `../../etc/passwd` or a symlink pointing outside the
workspace would grant arbitrary filesystem access.

**How it fails:** Removing `.resolve()` allows symlinks that point outside the
workspace to be read. Comparing un-resolved paths allows `../` traversal.

### 14. InMemoryBus exists for testing, not as a feature

`InMemoryBus` is a synchronous in-process message bus with no network
dependency. It exists so that the full test suite runs without NATS.

**Why:** Tests must be fast and infrastructure-free. `InMemoryBus` has the
same interface as `NATSBus` but delivers messages within the process.

**How it fails:** If someone uses `InMemoryBus` in production, they lose:
queue group load balancing, multi-process scaling, persistence, and
failure isolation. Everything runs in one process with one failure domain.

### 15. ResultStream is single-use and subscription-scoped

`ResultStream` owns a bus subscription for its lifetime. It can be iterated
exactly once — calling `collect_all()` or `async for` a second time raises
`RuntimeError`. This is not a limitation; it prevents the subtle bug where
two consumers compete for messages from the same subscription.

**Why:** NATS subscriptions are stateful — messages are consumed destructively.
If two iterators shared a subscription, each would see a random subset of
results. Single-use enforcement makes this impossible.

**How it fails:** Allowing reuse would produce "missing result" bugs that only
manifest under concurrent load (when the second iteration races the first).

### 16. Pipeline parallel levels use FIRST_COMPLETED, not gather

Within a parallel level, `PipelineOrchestrator` uses
`asyncio.wait(FIRST_COMPLETED)` in a loop rather than `asyncio.gather`.
This enables incremental progress reporting — each stage's result is logged
and stored in context as soon as it completes, rather than waiting for the
entire level.

**Why:** In baft's audit pipeline (LA, PA, RT parallel), the slowest auditor
previously blocked progress reporting for all three. With `FIRST_COMPLETED`,
the Workshop and MCP bridge see each stage complete in real time.

**How it fails:** Using `gather` is functionally correct but observationally
opaque — all three stages appear to complete simultaneously at the moment the
slowest one finishes. The latency is the same; only the observability differs.

**Paired ADR:** [ADR-011](adr/011-first-completed-vs-gather.md).

### 17. Subscribe before publish for orchestrator → worker request-reply {: #17-subscribe-before-publish-for-orchestrator--worker-request-reply }

When an orchestrator dispatches a task and waits for the matching result
over NATS, it subscribes to `heddle.results.{goal_id}` **before** publishing
on `heddle.tasks.incoming`. The shared helper
`heddle.orchestrator.dispatch.dispatch_and_wait_for_result` codifies the
subscribe → publish → wait sequence, and both `PipelineOrchestrator` and
`CouncilOrchestrator` route through it.

**Why:** NATS is at-most-once. If the worker finishes between `publish` and
`subscribe`, the result is delivered to nobody and the orchestrator times
out with no error on the worker side. Subscribing first guarantees the
subscription is live before any worker can respond.

**How it fails:** A publish-then-subscribe orchestrator races every fast
worker. The symptom is a caller timeout while the worker logs a successful
completion — intermittent, load-dependent, and one of the hardest classes
of bug to reproduce.

**Paired ADR:** [ADR-005](adr/005-subscribe-before-publish.md).

---

## Part II — Council & Multi-Agent Invariants

These invariants govern Heddle's council framework (`contrib/council`). They
are framework-enforced like Part I — testable, mechanically validated.

### 18. Council transcript is managed by the orchestrator, not by workers

Workers participating in a council discussion remain fully stateless.
The multi-round loop, transcript accumulation, and context injection
all live in `CouncilOrchestrator` (or `CouncilRunner`). Workers receive
a single `TaskMessage` with the relevant transcript excerpt in the
payload, process it, and reset.

**Why:** This preserves Invariant 1 (worker statelessness). If workers
tracked their own position across rounds, horizontal scaling would
silently break — replica A might process round 1 while replica B
processes round 2, and replica B would have no memory of round 1.

**How it fails:** Attempting to store "my previous position" in a worker
instance variable produces correct results in single-replica testing
and incoherent debates in production.

### 19. Transcript visibility is a security boundary, not a convenience

The `sees_transcript_from` field on each agent config is a hard filter —
not a hint. When agent C's visibility is set to `["A"]`, agent C never
sees agent B's contributions. This applies regardless of protocol.

**Why:** This is the council equivalent of knowledge silo isolation.
In adversarial review, the critic must not see the architect's reasoning
before forming an independent assessment. In Delphi protocols, participants
must not know who wrote which position.

**How it fails:** Leaking full transcripts to all agents defeats the
purpose of structured debate and introduces anchoring bias.

### 20. ChatBridge session state lives in the bridge, not in Heddle

ChatBridge adapters maintain per-session conversation history
internally (or in the external provider's API). The `ChatBridgeBackend`
wrapper is a standard `ProcessingBackend` — stateless from Heddle's
perspective. The session state is keyed by `session_id` and managed
by the bridge implementation.

**Why:** This keeps the Heddle worker layer clean while supporting
multi-turn conversations with external LLMs. The bridge is the adapter
boundary — Heddle doesn't need to know whether the backing LLM is
Claude, GPT-4, Ollama, or a human.

**How it fails:** Storing bridge session state in the worker or
orchestrator would couple Heddle's lifecycle management to external
provider session semantics.

### 21. Convergence checks must be side-effect-free

Convergence detectors (`position_stability`, `llm_judge`) read the
transcript and produce a score. They never modify the transcript,
inject messages, or influence agent behavior directly. The facilitator
synthesis is a separate step that runs after the deliberation loop ends.

**Why:** Convergence detection is an observation, not an intervention.
If the convergence check could modify the transcript, it would be
possible for a runaway LLM judge to terminate discussions prematurely
by injecting "we all agree" into the record.

---

## Summary — Framework Red Lines {: #summary--framework-red-lines }

These are framework-level constraints — every one of them is mechanically
checked by tests, validators, or code-path structure. Violating any of them
breaks the framework's correctness contract, regardless of how reasonable
the change sounds:

1. **Never put LLM calls in the router.** Routing is deterministic and fast.
   Smart routing belongs in the decomposer. (Invariant 2)
2. **Never carry state between worker tasks.** If you need context, pass it
   through messages. Workers are stateless replicas. (Invariant 1)
3. **Never skip contract validation.** It's the only type-safe boundary
   between actors. Removing it for "performance" removes the only safety
   net. (Invariant 5)
4. **Never change condition-evaluation defaults from FALSE.** Silent
   over-execution from typos is worse than visible skips with a warning.
   (Invariant 10)
5. **Never run multiple instances of a single-writer processor.** The
   per-instance lock does not protect across processes. (Invariant 11)
6. **Never publish before subscribing in request-reply.** NATS is at-most-once;
   the race is silent and load-dependent. (Invariant 17)
7. **Never leak full transcripts to all council agents.** `sees_transcript_from`
   is a security boundary, not a hint. (Invariant 19)
8. **Never let a convergence detector mutate the transcript.** Detection is
   observation, not intervention. (Invariant 21)

**Application-level red lines** (knowledge-silo isolation, blind-audit
discipline, behavioural-monitor isolation) live in
[Application Patterns](APPLICATION_PATTERNS.md). They are not mechanically
enforced — applications that violate them produce contaminated outputs
without any framework error.
