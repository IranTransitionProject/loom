# Feedback — `heddle-contrib-events-m2-architecture.md`

**Reviewer pass:** Heddle invariants + cross-repo contract rules + architectural assessment.
**Revision:** v2 (second pass, after invariant-guard + architect review).
**Date:** 2026-05-15.
**Verdict:** Architecture is sound. Zero hard invariant violations. The single largest miss in the v1 feedback was the **`EventDispatcher` delivery-semantics bridge** (JetStream at-least-once → heddle bus at-most-once), which now sits at the top of the concern list. One v1 concern (snapshot-domain side-effect-on-import) is demoted to a paragraph nit after verifying that `core/kvstore.py` itself uses the same import-time pattern for `checkpoint`. The Sprint 5 direction-rule risk and the JetStream-subjects-as-wire-contract question stand.

**Spec context has drifted since it was written.** The architecture doc was authored before two later-same-day toolkit changes formalized the *Heddle workspace* convention (toolkit commits `52b2dd2` and `9a8bd2a`, plus heddle's `3d5bc59` CHANGELOG discipline). The spec frames itself as single-repo work and doesn't use the new vocabulary. A dedicated section below ("Spec context drift — workspace formalization") names the framing edits needed; none are sprint-blockers.

---

## Real concerns

### 1. `EventDispatcher` silently downgrades delivery semantics  *(new in v2 — Sprint 2 blocker)*

JetStream is at-least-once. The heddle bus (`heddle.tasks.*` / `heddle.results.*`) is at-most-once. §3.6 places the `EventDispatcher` as a bridge between the two and asserts that projectors must be idempotent. Idempotency is **necessary but not sufficient**: it defends against duplicates, not against losses.

The failure mode the spec doesn't name: dispatcher consumes event from JetStream → publishes a `TaskMessage` to `heddle.tasks.projector.*` → acks the JetStream consumer → projector worker crashes before `apply_event()` runs. The event is now lost from the projector's perspective, even though JetStream considers delivery successful. No idempotency on the projector can rescue this case because the event never reached `apply_event()`.

Two clean resolutions:

- **(a)** `EventDispatcher` holds the JetStream consumer ack until the matching `heddle.results.{task_id}` response arrives. Turns the whole hop into at-least-once. This is what §3.7's "projector ack" entry implies but the prose never wires.
- **(b)** Accept the gap and document it as an explicit M2 limitation. Reasonable only if projector use cases are limited to read-models that get reconciled by other means.

**Decision before Sprint 2 starts; implementation lands in Sprint 3** alongside `JetStreamEventLog`. Sprint 2 needs the decision because the `Projector` base-class docstring describes the resulting delivery contract (at-least-once vs best-effort-with-losses); the wiring itself is meaningless until JetStream is real.

### 2. Direction-rule risk — Sprint 5 (CLI + Workshop tab)

(Substance unchanged from v1; severity confirmed by both reviewers.) Heddle's contrib→core direction rule forbids `heddle.cli` or `heddle.workshop` from importing `heddle.contrib.events`. Sprint 5 ships CLI subcommands and a Workshop tab; a naive implementation imports contrib from core — direct violation.

Three resolutions, in order of preference:

- **(a) Plugin / extension API in core.** `heddle.cli.plugins`, `heddle.workshop.tabs` — contrib registers itself. Highest one-time cost, lowest ongoing cost.
- **(b) Ship CLI subtree and Workshop tab inside `heddle.contrib.events`, discovered at runtime.** Core enumerates installed contrib packages and mounts their entry points.
- **(c) Co-locate (`heddle.contrib.events.cli`, `heddle.contrib.events.workshop`) and accept that the CLI/Workshop bits only appear when contrib is imported.** Cheapest; weakest integration; works for M2 demo but does not scale.

**Decision needed before Sprint 2**, not Sprint 5: the plugin contract shapes what `CommandHandler.aggregate_class` and `Aggregate.aggregate_type` must expose for introspection, and Sprint 2 lands those base classes.

### 3. JetStream subjects are cross-language wire contract too

(Substance unchanged from v1.) §3.7 reads as if `heddle.events.*` subjects are internal Python plumbing. They aren't: any foreign-SDK projector or command emitter must construct these byte-identically. Cross-repo rule C2 (subject names byte-identical across languages) applies.

Decision needed:

- **(a)** Event subjects are Python-only for M2; foreign-SDK projector support is deferred. State this explicitly in §3.7.
- **(b)** Event subjects are part of the cross-language wire contract from day one. Sprint 1's "vendor downstream into heddle-sdk" then includes the Subjects helper `events.*` namespace, not just the `EventEnvelope` schema.

Either is defensible; (a) is the smaller M2 lift. Leaving it implicit until Sprint 4 is the failure mode.

---

## Demoted concerns

### Snapshot domain registration as import side-effect *(was v1 concern #3; demoted)*

After verifying against `core/kvstore.py`: `register_domain()` is at module scope and **the module itself registers `("checkpoint", "heddle:checkpoint:")` at import time** (kvstore.py:184). The module docstring at line 17 documents import-time registration as the canonical pattern. So `heddle/contrib/events/__init__.py` registering `snapshot` at import is **following the convention, not inventing one**.

The valid sub-point survives as a paragraph-level confirmation:

- Add a one-line comment to `heddle/contrib/events/__init__.py` saying this matches the `checkpoint`-domain pattern in core.
- Add a one-test assertion that `register_domain` is idempotent under re-import (only a test-hygiene concern, not architectural).

---

## Spec context drift — workspace formalization

The spec lists "Depends on: Heddle PR #30 (`KeyValueStore` refactor) — merged" in its header and acknowledges one foreign sibling (ShopPulse) in §4 and Sprint 4. Since the spec was authored the same day, three additional changes landed that the spec doesn't yet reflect. None block; all are framing/vocabulary edits that the next revision should fold in so reviewers and Sprint authors see the change set as workspace-spanning rather than single-repo.

### What changed since the spec was written

- **Workspace convention is now formal.** `heddle-workspace/anchors/WORKSPACE.md` defines a parent directory holding the `getheddle/*` repos and consuming apps as flat siblings, with three-tier detection (`.heddle-workspace.yaml` manifest → marker-repo fallback → single-repo). All three subagents and the multi-repo skills detect workspace mode and group output by repo when a change set spans siblings. The `.heddle-workspace.yaml` manifest, `heddle workspace` CLI, and `HEDDLE_WORKSPACE` env-var are marked **designed-not-implemented** in `WORKSPACE.md` — referenced concepts only, not yet built.
- **`heddle-invariant-guard` has a new "Framework → app coherence" rule.** It catches half-landed framework changes that strand consuming-app configs (e.g., heddle ships a new envelope; heddle-sdk or ShopPulse haven't picked it up, so the app's worker configs reference symbols the framework no longer exposes).
- **`heddle-architect`'s plan template gains an "App-level impact" section**, and `heddle-preflight` runs per changed sibling in workspace mode rather than just in the working-directory repo.
- **CHANGELOG discipline is now mandatory** (`heddle/3d5bc59`): user-facing behaviour changes must add an `[Unreleased]` entry, per AGENTS.md and `docs/CONTRIBUTING.md`. Sprint §5 already lists "CHANGELOG entry" per sprint — that's now a hard requirement, not a courtesy.

### Where the spec needs to reflect this

- **Header.** "Depends on" should list, alongside PR #30, the workspace formalization (toolkit `52b2dd2`/`9a8bd2a`) and the CHANGELOG discipline (`3d5bc59`). It frames why each Sprint touches multiple siblings rather than one repo.
- **§5 — App-level impact bullet per sprint.** Each sprint currently lists files and verification but rolls the cross-sibling work into prose. Following the new architect template, add one bullet per sprint:
  - Sprint 1: heddle (envelope + schema), heddle-sdk (vendor schema; vendor subject namespace if Q7 picks (b)). ShopPulse: none.
  - Sprint 2: heddle (contrib package + base classes). ShopPulse: none — `OperatorSession` is a test fixture, not yet wired (per the Sprint 4 interleave above).
  - Sprint 3: heddle (`JetStreamEventLog`, `EventDispatcher` ack wiring). heddle-sdk: optional projector consumer adapters per Q7. ShopPulse: docker-compose JetStream volume.
  - Sprint 4: heddle (concrete aggregate). ShopPulse (sibling app): refactor M1 in-memory preferences to use `OperatorSession` via the marked seam; optional `latest_active_session_by_badge_id` projector per gap G2.
  - Sprint 5: heddle (CLI plugin host + Workshop tab host, per Q6 outcome). `heddle.contrib.events` (CLI subtree + Workshop tab implementations).
- **§5 — Preflight runs per changed sibling.** The "Verification" lines currently read as if the test suite of a single repo is what runs. In workspace mode, `heddle-preflight` runs in each touched sibling. Sprint 1: preflight in heddle AND heddle-sdk. Sprint 4: preflight in heddle AND ShopPulse. Spell that out so it doesn't become a Sprint-author surprise.
- **Framework → app coherence guard for Sprint 1.** `EventEnvelope` lands in heddle and is vendored into heddle-sdk in the same sprint. If Q7 picks (b) (subjects are cross-language wire contract), the Subjects helper update is in the same scope. Half-landing — heddle ships the schema but heddle-sdk lags — is exactly the failure mode the new invariant-guard rule exists to catch. Make it explicit in Sprint 1's done-criterion that *both* repos go green together; a Sprint 1 that lands only heddle is incomplete.
- **`.heddle-workspace.yaml` is designed-not-implemented; don't accidentally depend on it.** The spec doesn't reference the manifest, which is correct for M2. Worth a one-line confirmation in §7 ("Out of scope for M2") so a future reader doesn't think the omission was an oversight: *"Workspace-manifest-driven discovery (the designed-not-implemented `.heddle-workspace.yaml`) is not required for M2; detection-by-marker-repo is sufficient for the contrib package's discovery needs."*
- **Optional but useful: workspace-relative paths in the spec.** Where the doc says `heddle/contrib/events/handler.py`, the new convention prefers workspace-relative paths (`heddle/src/heddle/contrib/events/handler.py`). Minor; flagged only because the architect's plan template now uses this convention by default, so Sprint specs derived from this doc will need translation otherwise.

### What this does *not* change

- No new invariant violations. The workspace convention is *additive* — it formalizes what was already practice.
- No sprint reordering implied. The Sprint 2 → Sprint 4 interleave proposed above stands; workspace formalization just gives it cleaner vocabulary (`OperatorSession`-as-Sprint-2-fixture lives in `heddle/tests/`, then promotes to a shipped aggregate in Sprint 4, then ShopPulse — the sibling — picks it up).
- No Q6/Q7/Q10 reframing. The three Sprint-1-blockers are unchanged in substance.

---

## Paragraph-level edits to land before Sprint 2

Five places where the architecture doc needs a sentence or two to make implicit contracts explicit. Each is small but each closes a question that would otherwise surface during implementation.

### §3.4 — `Aggregate._pending` is per-task

The pattern is sound: `_load_aggregate()` always constructs a fresh `Aggregate` (via snapshot read or `cls(aggregate_id)`); `_pending` is initialised in `__init__`; `aggregate._pending.clear()` runs after commit. Add one line: *"Aggregates are constructed per task and never cached on the worker — caching them on `self` would break Invariant 1 (stateless workers)."* The future contributor who reaches for `self._last_aggregate` as a debug aid is the audience.

### §3.5 — `CommandHandler.reset()` is a no-op

There is no inter-task state on the handler, so `reset()` does nothing. Stating that explicitly lets a reviewer confirm Invariant 1 was considered without having to derive it from the absence of instance attributes. One line.

### §3.5 — Worker contract validation

`TaskWorker` provides shallow contract validation at the message boundary. The spec defines `CommandHandler.process()` and `Projector.process()` but doesn't say either invokes the validator. Either it's automatic via the `TaskWorker` superclass (likely; confirm in Sprint 2 spec) or it must be called explicitly. Worth a sentence so it's not discovered during code review.

### §3.6 — JetStream consumer resume position

`EventDispatcher` uses pull consumers. Defaults to `DeliverAll` for fresh consumers, "from last ack" for durable consumers. Neither is wrong, but both must be intentional. One sentence: *"`EventDispatcher` uses durable pull consumers with explicit ack; restart resumes from last ack."*

### §3.1 — `TODO(events-2.0)` block lands verbatim in Sprint 2

The seam comment in §3.1 (envelope choice — `TaskMessage` today, `CommandMessage` later, with four numbered trigger conditions) is load-bearing: it makes the architectural debt visible, names the conditions for repaying it, and tells the future maintainer what the split looks like before they have to design it from scratch. Copy it verbatim into Sprint 2's `heddle/contrib/events/handler.py` docstring — do not paraphrase. The trigger conditions are the point; a "see the design doc" pointer rots.

### §3.5 — N-replicas vs single-replica intent for `commandhandler.*`

Optimistic concurrency via `Nats-Expected-Last-Subject-Sequence` sidesteps Invariant 11 (no multi-instance `serialize_writes=True`) by making the store CAS-safe rather than the worker single-instance. This is the inverse of the typical `serialize_writes=True` pattern. Worth recording the intent — *"N replicas in a queue group, occasional retry on `ConcurrencyError`"* — rather than implying it.

---

## Gaps in the proposal (not covered by v1 feedback)

### G1. Rejected commands have no audit story

Section 2 motivates the package partly with "audit trail without trying." But a precondition failure in `handle_badgein` raises, becomes `TaskResult(status=ERROR)`, and **nothing is recorded** in the event log. That defeats the audit motivation for the case that arguably matters most (an operator tried to badge in to a station they aren't authorized for). Two paths:

- Emit an `OperatorBadgeInRejected` event. Clean for audit; messy for replay semantics (rejection events have no state effect).
- Write rejections to a parallel subject like `heddle.events.rejections.{aggregate_type}.{aggregate_id}.{command}`.

Pick one and document it. Must be decided by Sprint 4.

### G2. "Current session by badge" projector is M2-critical, *and* the §4 command table is internally inconsistent

§4 hand-waves: *"A read model (projector, M3) will maintain `latest_active_session_by_badge_id` in Valkey so the API can answer 'what session is operator 206 currently in?'"*

Two problems compound:

- **Precondition-table bug.** §4 lists `BadgeIn`'s precondition as *"Aggregate is new OR last session is closed."* With per-session aggregates and `session_id == aggregate_id`, **every `BadgeIn` creates a new aggregate** — so the "aggregate is new" branch is always true, and the "last session is closed" branch can only ever be evaluated cross-aggregate. The table doesn't capture this. Fix the table to say what it actually means: *"no active session exists for `badge_id`"* — a cross-aggregate predicate.
- **Dependency inversion.** That cross-aggregate predicate requires either (i) scanning the event log by `badge_id` (acceptable at one shop's scale but not as a documented pattern), or (ii) consulting the `latest_active_session_by_badge_id` read model the doc defers to M3. So the M3 projector is actually an M2 dependency.

Resolutions:

- Move the `latest_active_session_by_badge_id` projector to Sprint 4 and fix the precondition wording.
- OR keep the projector deferred, document explicitly that M2 precondition checks scan the event log, mark this as a known scale limit, and still fix the precondition wording.

### G3. Projector observability is missing

Sprint 5 ships `heddle events list / show / replay` for inspecting events. Nothing inspects the *projector side*: consumer lag, last ack'd `event_id`, error count, current consumer position. `heddle events streams` is mentioned in passing in Sprint 3 but never specced. The runbook story for "the badge-in/out display is stale" needs to start with "check projector lag." Spec what `heddle events streams` shows in Sprint 3.

### G4. `event_version=2` breakage signal

The field is reserved; no upcaster infra ships in M2. The user-facing failure mode when a v2 event arrives at v1-only `apply()` is: Pydantic validation error inside the event payload model → `CommandHandler` crashes → NATS redelivers forever. Spec the typed failure path: `apply()` raises `UnknownEventVersionError`; the dispatcher routes such failures to a DLQ stream rather than redelivering. One paragraph in §3.4.

### G5. Multi-stream projector cursor reconciliation

`Projector.aggregate_types: ClassVar[list[str]]` suggests a single projector may span multiple aggregate streams. With one-stream-per-aggregate-type (§3.2), that's N JetStream consumers with N independent cursors. Cross-stream ordering and restart positioning aren't specced. Acceptable to defer to M3, but say so.

### G6. Router rule cardinality grows O(commands), not O(aggregates)

§3.1's trigger-4 condition for repaying the `CommandMessage` debt is *"the `router_rules.yaml` entry list for `commandhandler.*` workers grows past ~20."* Three aggregates with five commands each lands at fifteen — close enough that the next aggregate triggers the split. Worth noting in §3.1 that O(commands) not O(aggregates) is the right thing to count.

---

## Q1–Q5 default check

The architecture doc's defaults in §6, re-checked:

- **Q1 (typed `EventMetadata`):** Default (b) **tightens** beyond the established `TaskMessage.metadata: dict[str, Any]` pattern. This is defensible — event metadata has stronger conventions (`command_id`, `correlation_id`, `actor`) than task metadata — but the divergence is deliberate, not accidental. Call it out explicitly in §6 so reviewers don't flag (a) as conformant-by-omission.
- **Q2 (UUIDv7 via `uuid_utils`):** Right. Don't bump the Python floor for one envelope.
- **Q3 (silent dedup):** Default (a) accepts a real silent-success failure mode. **Prefer (c)** — surface `Pub.Ack.Duplicate` as a soft warning. One extra log line, removes the silent failure.
- **Q4 (JSON snapshots):** Right.
- **Q5 (Pydantic event payloads nested on Aggregate):** Right despite the awkward read — binds payload schema to the aggregate that owns the event, which is where the upcaster will live.

---

## Confirmations (invariants the proposal upholds well)

Calling these out so reviewers can see each invariant was considered.

- **Invariant 1 (stateless workers)** — load / process / commit / discard with a fresh aggregate per task. Modulo the §3.4 / §3.5 paragraph edits above, fully preserved.
- **Invariant 2 (deterministic router)** — `commandhandler.{aggregate}.{command}.{tier}` and `projector.{name}.{tier}` are pure `worker_type` patterns; no payload inspection, no LLM step. Clean.
- **Invariant 11 (single-writer for `serialize_writes=True`)** — sidestepped via CAS on `Nats-Expected-Last-Subject-Sequence`. The store is multi-writer-safe, so handlers can scale.
- **Invariant 17 (subscribe-before-publish)** — preserved at the bus layer (`EventDispatcher` is the publisher, projector is subscribed before dispatch). The JetStream resume-position note (§3.6 edit above) closes the corresponding question on the JetStream side.
- **Cross-repo C1 (schema source of truth)** — `EventEnvelope` originates in `heddle.core.messages`, exports via the existing tool, vendors into heddle-sdk in Sprint 1. Textbook.
- **Tests default to InMemory** — Sprint 3 marks JetStream tests as `@pytest.mark.integration`. Sprint 4's done-criterion (*"a complete `OperatorSession` lifecycle runs end-to-end against the real NATS+Valkey infrastructure"*) is by definition an integration test; mark it the same way so it doesn't get added to the default unit suite.
- **Solo / SMB / on-prem orientation** — single-host JetStream, no k8s assumption, multi-NATS clustering explicitly deferred. Right stance.
- **Progressive disclosure** — `InMemoryEventLog` for zero-config; `JetStreamEventLog` opt-in via config; CLI for inspection; Workshop tab read-only in M2. Defaults are visible and inspectable.

---

## Sprint sequencing — adjustments

Two adjustments to the sprint plan in §5:

### Interleave `OperatorSession` into Sprint 2 as a test fixture

The current plan defers the concrete aggregate to Sprint 4. But `OperatorSession`'s shape is what reveals whether the `Aggregate` / `CommandHandler` base classes (Sprint 2) have the right ergonomics — especially around Q5 (Pydantic event payloads nested on the aggregate). Discovering the shape against `InMemoryEventLog` is cheap. Concretely: write `OperatorSession` + its three commands as a Sprint 2 test fixture (not a shipped aggregate), then promote it to a shipped aggregate in Sprint 4 once JetStream is real. This catches ergonomic friction before the abstractions calcify.

### Sprint 3 must pin `EventDispatcher` ack semantics, not just land `JetStreamEventLog`

If Real Concern #1 (delivery-semantics bridge) is resolved as option (a) — hold JetStream ack until bus result returns — that wiring is Sprint 3 work, not a Sprint 4 afterthought. Add it to Sprint 3's done-criterion.

### Sprint 1 vendoring scope depends on Real Concern #3

If event subjects are part of the cross-language wire contract (option (b) on Real Concern #3), Sprint 1's "vendor downstream into heddle-sdk" must include the Subjects helper `events.*` namespace, not just the `EventEnvelope` schema. If Python-only for M2 (option (a)), Sprint 1's scope is unchanged.

---

## Follow-up questions to resolve before Sprint 1

Renumbered from v1; new questions appended.

- **Q6: CLI/Workshop plugin/extension contract.** (Real Concern #2.) Pick (a), (b), or (c) and document the contract in the architecture doc, not in Sprint 5.
- **Q7: Foreign-SDK access to event subjects.** (Real Concern #3.) Decide whether event subjects are Python-only for M2 or part of the cross-language wire contract from day one.
- **Q8: Snapshot domain registration nit.** (Demoted concern.) Add a one-line comment in `heddle/contrib/events/__init__.py` noting the pattern matches core's `checkpoint` domain; add an idempotency test.
- **Q9: `CommandHandler` retry policy on `ConcurrencyError`.** Spec says "let NATS redeliver via queue group" for M2. What's the redeliver cap? Does the command land in a DLQ? Does the orchestrator see a typed failure? The failure mode needs to be visible to operators.
- **Q10 (new): `EventDispatcher` ack coordination.** (Real Concern #1.) Choose (a) hold-ack-until-bus-result or (b) accept the gap explicitly. Decision before Sprint 2 starts (drives the `Projector` base-class docstring); implementation in Sprint 3.
- **Q11 (new): Rejected-command audit policy.** (Gap G1.) Emit a `*Rejected` event or write to a parallel rejections subject. Must be decided by Sprint 4.
- **Q12 (new): `event_version` mismatch error handling.** (Gap G4.) Typed `UnknownEventVersionError` + DLQ rather than infinite redelivery. Sprint 2 ergonomics — the base class needs the error type defined.
- **Q13 (new): `latest_active_session_by_badge_id` placement.** (Gap G2.) Sprint 4 projector, or M2 precondition-by-event-log-scan with documented scale limit.

---

## Summary

**Three things must be resolved in the architecture doc before Sprint 1:**

1. **`EventDispatcher` ack coordination** (Q10) — shapes the `Projector` idempotency / loss contract.
2. **CLI / Workshop plugin contract direction** (Q6) — shapes what Sprint 2's base classes expose.
3. **Event-subjects-as-wire-contract scope** (Q7) — shapes Sprint 1's vendoring boundary.

The rest are paragraph-level edits (§3.4 stateless-aggregate note, §3.5 `reset()` no-op + N-replicas intent, §3.6 JetStream resume position, Sprint 4 `@pytest.mark.integration`) or sprint-specific work that can be deferred to its sprint with a recorded follow-up. The earlier concern about snapshot-domain registration was overstated and is demoted to a paragraph nit.

**Counts:** 0 invariant violations, 3 Sprint-1-blocking decisions, 6 paragraph-level edits, 6 new gaps in the proposal (G1–G6), 8 invariant confirmations, 1 spec-context-drift section (workspace formalization, framing only).
