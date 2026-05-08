# Heddle Design Invariants — Technical Reference

**Purpose:** This document describes the non-obvious design decisions, deliberate
constraints, and architectural invariants in the Heddle framework. It exists
because well-intentioned contributors — human or LLM — routinely propose
"improvements" that would break these invariants.

Read this before proposing structural changes to Heddle or any application built
on it. Every section explains *what* the invariant is, *why* it exists, and
*how it fails* if violated.

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

### 2. The router is deterministic — no LLM in the routing path

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

### 9. OpenTelemetry is optional via runtime feature detection

The `tracing/otel.py` module uses `contextlib.suppress(ImportError)` to
conditionally import OTel SDK. If not installed, a `_HAS_OTEL` flag stays
`False` and all public functions become no-ops. The module is always importable.

**Why:** Tracing is valuable but not required. Production code calls tracing
functions unconditionally without conditional imports. This keeps instrumentation
code clean while allowing bare-metal deployments without OTel.

**How it fails:** If you remove the `suppress(ImportError)`, any deployment
without `uv sync --extra otel` crashes at import time.

### 10. Condition evaluation: malformed → TRUE, missing path → FALSE

Pipeline stage condition evaluation has two distinct failure modes with
different defaults:

- **Malformed condition** (wrong format, not three tokens): defaults to
  **TRUE** (run the stage) and logs a warning. This prevents a typo in the
  condition syntax from silently dropping a stage.
- **Missing path** (path references a context key that doesn't exist):
  defaults to **FALSE** (skip the stage) and logs a warning. This is the
  expected behavior for conditional stages like `extract.output.needs_ocr == true`
  — if the upstream stage didn't produce the field, the condition is not met.

> **Summary: malformed condition → TRUE (run), missing path → FALSE (skip).**

**Why the split:** A *structural* error in the condition expression is almost
certainly a mistake that should not silently skip work. A *missing path*,
however, is the normal outcome when an upstream stage doesn't produce an
optional field — skipping is the expected semantics. Both cases log a warning
so typos are discoverable.

**How it fails:** If you change missing-path to `True`, every conditional
stage runs unconditionally when the upstream field is absent, which defeats
the purpose of conditions. If you change malformed to `False`, a syntax typo
silently removes a stage from execution.

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

---

## Part II — Application Design Patterns

These are architectural patterns that Heddle applications should follow when
building pipelines with epistemic constraints, blind audits, or information
barriers. They are not Heddle framework code — they are design principles that
emerge from how the framework is meant to be used.

### 17. Knowledge silo isolation is an epistemic quarantine, not a convenience grouping

When an application uses knowledge silos to implement information barriers
(e.g., between analytical workers and audit workers), silos marked as
isolated enforce epistemic quarantine. This is what makes blind audits
actually blind.

**Why:** If audit nodes can access the analytical framework they are supposed
to evaluate, they will pattern-match to existing conclusions and produce
pseudo-confirmatory "independent" judgments. The audit becomes epistemically
worthless — it tells you what you already believe, not whether what you
believe is correct.

**How it fails:** Adding domain knowledge to any audit node's `knowledge_sources`
breaks audit independence. The failure is invisible: audits still produce
professional-looking output, but their conclusions are contaminated by the
framework they were supposed to evaluate. There is no runtime error, no
warning, no indication that the audit is compromised.

**The trap:** It is tempting to "help" audit nodes by giving them more context
so they can be more informed. This is precisely the wrong thing to do. Blind
auditors must be knowledge-deprived by design.

### 18. Neutralization stages are audit firewalls, not text processors

When a pipeline implements blind auditing, a neutralization stage strips
domain-specific vocabulary before blind auditors see the text. The neutralizer
must have minimal knowledge — only the vocabulary mapping and procedural rules.

**Why:** If the neutralizer receives domain knowledge, it leaks domain-specific
framing into the "neutral" text. Audit nodes then receive text that, while
superficially generic, carries the structural fingerprint of domain conclusions.

**How it fails:**

- Adding entity registries to the neutralizer shifts it from lexical to
  semantic processing — a different epistemic role.
- Opaque identifiers (entity codes, reference IDs) must pass through unchanged.
  The neutralizer transforms vocabulary, not references.
- Removing the neutralizer entirely and sending raw text to auditors exposes
  every domain-specific term as a vector for framework contamination.

### 19. Blind auditors should have tiered knowledge deprivation

Not all audit nodes in a blind audit pipeline should be equally blind. Different
audit functions require different levels of knowledge deprivation:

- **Adversarial challengers** should be maximally blind — only procedural rules.
  Giving them evaluation rubrics shifts them from adversarial challenge to
  structured critique, which is a different cognitive function.
- **Structured auditors** (logic, perspective, methodology) need rubrics to know
  *what* to evaluate without knowing *what the domain framework says*.
- **Synthesis nodes** need audit outputs and decision logs to detect blind spots,
  but must not see the domain framework to avoid contamination.

**How it fails:** Adding any knowledge source to an adversarial challenger makes
it less adversarial. Adding domain content to structured auditors makes them
confirmatory. Adding domain content to synthesis nodes makes blind-spot
detection useless (it validates decisions against the framework that produced them).

### 20. Neutralization maps must be computed per-run, not pre-cached

When a neutralizer produces a reverse map (neutral term -> original domain term),
that map must be computed per document, not pre-cached globally.

**Why:** Terminology usage varies by document. A document about one topic uses
different domain terms than a document about another. A global reverse map
produces incorrect de-neutralization.

**How it fails:** Pre-computing a global reverse map produces wrong results for
documents that don't use all terms. Worse, it maps neutral terms back to wrong
domain terms when there are many-to-one mappings.

### 21. Quality gates should be content-driven, not intent-driven

When a pipeline uses flags to trigger escalation (e.g., routing to a more
expensive audit tier), the flag should be set based on the analytical worker's
assessment of content quality, not based on whether the user requested
escalation.

**Why:** Users may not recognize when their work has crossed a quality threshold
that warrants peer review. The flag is a content-quality signal, not a workflow
button.

**How it fails:** If the flag is set based on user intent ("publish this"), users
can bypass the audit pipeline by not requesting escalation. Conversely,
important analytical shifts skip audit because the user didn't ask for review.

### 22. Escalation thresholds gate expensive operations

When an adversarial challenge node produces a high-strength challenge that
triggers escalation (e.g., to manual review with an alternate LLM provider),
the threshold must be calibrated carefully.

**Why:** Escalation targets are expensive (different provider, human review,
longer cycles). The threshold must ensure genuine threats escalate while
routine challenges don't.

**How it fails:** Too low: false-positive escalations waste resources and erode
trust ("the system always escalates"). Too high: genuine analytical failures
pass through to publication.

### 23. Context flows through messages, not worker state

Session identifiers, request context, and inter-stage metadata must flow
through `input_mapping` template references, not through worker instance state.

**Why:** Workers are stateless (invariant #1). They cannot track sessions
internally. The pipeline's `input_mapping` is the mechanism for passing context
through a stateless execution chain.

**How it fails:**

- If an intermediate worker filters context fields from its output, downstream
  workers lose access and cross-cutting concerns (governance audits, session
  tracking) break silently.
- If context is added to worker instance state instead of flowing through
  messages, multi-replica deployments lose context tracking.

### 24. Behavioral monitors must be isolated from analytical content

When a pipeline includes a behavioral monitoring worker (e.g., monitoring
analyst fatigue, tunnel vision, or cognitive bias), that worker must NOT
have access to the analytical framework or domain database.

**Why:** If a behavioral monitor sees domain content, it evaluates whether the
user's *analysis* is correct rather than whether their *behavior* is healthy.
It becomes a second analytical worker with worse prompting instead of a
cognitive monitor.

**How it fails:** Adding domain content to a behavioral monitor means it flags
analytical disagreements as behavioral anomalies. "User spent 40 minutes on
entity X" gets flagged as tunnel vision even if entity X genuinely requires
deep analysis.

### 25. Universal silos must never contain domain-specific analytical content

If a silo is shared across all workers — including blind auditors — it must
contain only procedural and epistemic discipline rules (source evaluation
standards, neutrality requirements, anti-bias framing). Never analytical
conclusions, domain assessments, or entity evaluations.

**Why:** If a universal silo contains analytical content, blind auditors receive
domain context through the one channel that bypasses all isolation checks.

**How it fails:** Someone adds "current high-priority findings" to a universal
silo as "standing guidance." Now every blind auditor knows what the framework
considers important. Audit independence is destroyed through the one silo
everyone trusts.

### 26. There is no "improve the audit by giving auditors more information"

This is the most frequently proposed and most damaging class of "improvement"
to blind audit pipelines. The information asymmetry between sighted and blind
nodes is the mechanism, not the bug.

**The principle:** Audit quality comes from independence, not from information.
A well-informed auditor who has read the conclusions will confirm them. A blind
auditor who has not read the conclusions will challenge them on logical and
perspectival grounds. Both are necessary. Merging them destroys the one you
can't get any other way.

### 27. YAML configs are the right medium for what they describe

Heddle application configs are typically 80% natural-language system prompts and
20% structural configuration (schemas, mappings, silo assignments). A Python
DSL would help with the structural 20% but would make the prompt 80% harder
to read and edit.

**The right approach:** Generate the structural parts (I/O schemas, silo
references, pipeline topology) from typed Python models. Keep the natural
language in YAML where it's readable without a Python interpreter.

**How it fails:** Replacing all YAML with Python forces system prompts into
Python strings (escaping hell, no syntax highlighting, harder to diff). It
also removes the ability for non-developer domain experts to review and
suggest changes to worker behavior.

### 28. Single-writer processors must be truly single-instance

When a processor worker uses `serialize_writes=True` for a single-writer store
(DuckDB, file-based databases), the application must ensure exactly one instance
runs. The asyncio lock only serializes within one process (invariant #11).

Additionally, no other worker should bypass the designated writer to access the
store directly. The writer typically enforces validation, cross-referencing,
and governance triggers that direct access would skip.

**How it fails:** Running two instances causes write races. Bypassing the writer
skips validation and governance triggers (e.g., operation count thresholds that
fire audit escalations).

---

## Summary — Red Lines

These are things that must never happen, regardless of how reasonable they sound:

1. **Never add domain content to blind auditor knowledge sources.** Not even
   "just the entity names" or "just the high-level findings." Any domain
   leakage contaminates the audit.

2. **Never put LLM calls in the router.** Routing is deterministic and fast.
   Smart routing belongs in the decomposer.

3. **Never carry state between worker tasks.** If you need context, pass it
   through messages. Workers are stateless replicas.

4. **Never skip contract validation.** It's the only type-safe boundary between
   actors. Removing it for "performance" removes the only safety net.

5. **Never put analytical content in universal silos.** They are the one
   channel that reaches every node, including blind auditors.

6. **Never pre-compute neutralization reverse maps.** They must be per-document.

7. **Never change condition-evaluation defaults from TRUE.** Silent stage
   skips are worse than unnecessary stage runs.

8. **Never run multiple instances of a single-writer processor.** The
   per-instance lock does not protect across processes.

---

## Part III — Council & Multi-Agent Invariants

### 29. Council transcript is managed by the orchestrator, not by workers

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

### 30. Transcript visibility is a security boundary, not a convenience

The `sees_transcript_from` field on each agent config is a hard filter —
not a hint. When agent C's visibility is set to `["A"]`, agent C never
sees agent B's contributions. This applies regardless of protocol.

**Why:** This is the council equivalent of knowledge silo isolation.
In adversarial review, the critic must not see the architect's reasoning
before forming an independent assessment. In Delphi protocols, participants
must not know who wrote which position.

**How it fails:** Leaking full transcripts to all agents defeats the
purpose of structured debate and introduces anchoring bias.

### 31. ChatBridge session state lives in the bridge, not in Heddle

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

### 32. Convergence checks must be side-effect-free

Convergence detectors (`position_stability`, `llm_judge`) read the
transcript and produce a score. They never modify the transcript,
inject messages, or influence agent behavior directly. The facilitator
synthesis is a separate step that runs after the deliberation loop ends.

**Why:** Convergence detection is an observation, not an intervention.
If the convergence check could modify the transcript, it would be
possible for a runaway LLM judge to terminate discussions prematurely
by injecting "we all agree" into the record.
