# Changelog

All notable changes to Heddle are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/).

CHANGELOG updates are **required** for commits that add, change,
deprecate, remove, or fix user-facing behaviour. Documentation-only
changes, internal refactors with no behavioural delta, and CI/build
adjustments are exempt. See `AGENTS.md` "Review checklist" for the
rule and `docs/CONTRIBUTING.md` for contributor-facing guidance.

## [Unreleased]

### Deprecated

- `HEDDLE_TRACE` environment variable, renamed to
  `HEDDLE_PIPELINE_VERBOSE`. The new name disambiguates it from
  OpenTelemetry tracing — `HEDDLE_TRACE` had been confusing new
  contributors who assumed it controlled span emission (it doesn't;
  it controls full pipeline-payload logging in `_summarize`, which
  is orthogonal to OTel). `HEDDLE_TRACE` continues to work as a
  legacy alias and emits a one-time `pipeline.env_var_deprecated`
  warning at process startup if set without the new name; remove
  the alias when downstream deployments have migrated. The Python
  module attribute `HEDDLE_TRACE` is preserved as an alias for
  `HEDDLE_PIPELINE_VERBOSE` so any code importing the constant
  continues to work. Resolves OTel-audit W2. Docs updated in
  `AGENTS.md`, `docs/CLI_REFERENCE.md`, `docs/TROUBLESHOOTING.md`,
  and `docs/SECURITY_MODEL.md` to use the new name and explicitly
  call out that it's *not* the OTel switch.

### Changed

- **`heddle.contrib.events.event_log.EventLog.subscribe` signature
  (R4).** Now `async def` returning `AsyncIterator` — the
  underlying subscription is registered BEFORE the method returns,
  so callers may publish immediately afterwards and be guaranteed
  delivery. Eliminates the Sprint 2 J5 race that required
  `_wait_for_subscriber` polling helpers. `EventDispatcher.start`
  is updated to match.
- **`CommandHandler.__init__` keyword arguments.** Adds optional
  `cache`, `snapshot_store`, `dedup_publisher`, `dedup_subscriber`,
  `snapshot_every_n`. All default to Null/None for back-compat with
  Sprint 2 in-memory tests.
- **`CascadeProjector.__init__`** accepts an optional `kv` argument
  enabling lease integration. `kv=None` (the default) preserves
  Sprint 2 behaviour.
- `heddle/src/heddle/worker/embeddings.py` module docstring gains a
  "Statelessness convention" section documenting the implicit rule
  that `EmbeddingProvider` subclasses are stateless-by-convention
  with respect to the worker lifecycle: providers sit outside the
  per-task `reset()` boundary (they're injected and reused), so
  only instance state that's stable for the provider's lifetime
  (e.g. cached `_dimensions` derived from the configured model) is
  allowed. Caches that affect correctness — anything that could
  yield a different result depending on prior tasks — must be
  promoted to the worker, where the framework's stateless-worker
  invariant guarantees `reset()` between tasks. Resolves
  Invariant-audit W1.
- `docs/releases/README.md` gains a "Workflow" section pointing at
  the new `RELEASING.md`, plus explicit `gh release create` and
  `gh release edit` commands for attaching and refreshing release
  notes. Prior version implied the workflow without naming the
  commands.
- Per-release notes moved from repo-root `RELEASE_NOTES_v0.9.2.md` to
  `docs/releases/v0.9.2.md`. The root-level location didn't scale
  past one release and sat outside the MkDocs tree. New convention
  documented in [`docs/releases/README.md`](docs/releases/README.md):
  one file per release named `vX.Y.Z.md`, frozen at release time,
  written only when a release needs more than a CHANGELOG entry can
  carry (breaking-change migration guide, major version, multi-
  subsystem narrative). Routine releases stay CHANGELOG-only.
  Cross-references in this file updated; no other paths reference the
  old location.
- `TaskWorker._publish_result` now injects `_trace_context` into the
  outgoing `TaskResult` payload so the return path is symmetric with
  the outbound `TaskMessage`. Today no orchestrator-side consumer
  reads the field — the trace tree already forms correctly via the
  outbound chain (`extract_trace_context` at the worker entry sets
  the worker span's parent). The injection exists so any future
  consumer-side span (in `orchestrator/dispatch.py` or elsewhere)
  can parent under the worker span. No behavioural change for
  existing OTel users; no behaviour for users without OTel
  installed (the injector no-ops). See `OTEL_AUDIT_2026-05-15.md` S1
  for the rationale.
- `heddle/k8s/kustomization.yaml` header now explicitly marks the
  bundled manifests as **Minikube / local-development only** and
  documents the steps required to fork them for production
  (pin `heddle-*:latest` to a released tag; remove
  `imagePullPolicy: Never`; push to a real registry). The previous
  header mentioned `imagePullPolicy: Never` only as a one-line note;
  k8s-fluent readers were misreading the dev convention as a
  configuration bug. No manifest behaviour changed.
- Store abstraction lifted from `heddle.orchestrator.store` to
  `heddle.core.kvstore` and renamed for generality. Backward-compat
  aliases preserve every public name through the v0.x series and will
  be removed at v1.0:
  - `heddle.orchestrator.store.CheckpointStore` → alias of `KeyValueStore`
  - `heddle.orchestrator.store.InMemoryCheckpointStore` → alias of `InMemoryKeyValueStore`
  - `heddle.contrib.redis.store.RedisCheckpointStore` → alias of `RedisKeyValueStore`
- `CheckpointManager` now looks up its key prefix via
  `domain_prefix("checkpoint")` instead of hardcoding it. On-disk keys
  are **bit-exact unchanged**; existing data in Valkey is unaffected.

### Added

- **`heddle.contrib.events` Sprint 3 — production runtime.** Replaces
  the Sprint 2 in-memory machinery with JetStream + Valkey backings
  and the coordination mechanisms the in-memory paths didn't need.
  Per architecture v7 §6 Sprint 3 plus Sprint 2 feedback R1, R4,
  R5, R7. Components:
  - `JetStreamEventLog` / `JetStreamRejectionLog` —
    production `EventLog` / `RejectionLog` over NATS JetStream.
    CAS append via `Nats-Expected-Last-Subject-Sequence` (APIError
    code 10071 → `ConcurrencyError`). Idempotent stream-config
    helpers `ensure_event_stream` / `ensure_command_stream` /
    `ensure_rejection_stream`.
  - `AggregateCache` — process-local LRU (default 1024).
    `CommandHandler` consults cache → snapshot → log replay; in-
    process dedup now works across `handle()` calls.
  - `SnapshotStore` — adapts `KeyValueStore` to aggregate snapshots
    at `heddle:events:snapshot:{type}:{id}`. Count-based snapshot-
    on-write (every N events).
  - Hybrid `mark_processed` — `DedupPublisher` / `DedupSubscriber`
    on subject `heddle.dedup.{type}.{id}` (NATS core, not
    JetStream). `NullDedupPublisher` / `NullDedupSubscriber` for
    tests; `NatsDedupPublisher` / `NatsDedupSubscriber` for
    production.
  - `finalization_lease` — Valkey SETNX EX lease at
    `heddle:events:horizon:{type}:{id}` (TTL 30s). P2 and P3 both
    attempt it before publishing `InternalFinalize`; loser logs and
    skips. Cascade's deterministic command_id mechanism preserved
    as defense-in-depth.
  - Real `FinalizationHorizonProjector` (P3) replaces the Sprint 2
    stub. Per-aggregate `asyncio.Task` timers; configurable via
    `IntervalAggregate.HORIZON_TIMEOUT_SECONDS` ClassVar
    (default 24h). `issued_by="framework:horizon"`.
  - `KeyValueStore.set_if_not_exists` — new abstract method on the
    KV ABC. Implementations: in-memory (single-event-loop atomic),
    scoped (delegates with prefix), Redis (`SET NX EX`).
  - SLI instrumentation hooks (`heddle.contrib.events.sli`) —
    OpenTelemetry-compatible `Recorder` Protocol; three histogram
    observations (command handle, dispatcher fan-out, lease
    acquisition). Default no-op recorder; `install_recorder` for
    application-installed exporters.
- Per-package coverage gates (K4 closeout). The global
  `fail_under = 91` in `[tool.coverage.report]` is unchanged; in
  addition, `[tool.heddle.coverage-gates]` now holds per-package
  floors, set at introduction (2026-05-19) to
  `floor(current_branch_aware_coverage) - 2`. The new
  `tools/check_coverage_gates.py` reads
  `pytest --cov-report=json` output and enforces the table; CI
  runs it after the existing pytest+coverage step. Ratchet rule
  ("sustained ≥3pp above floor across two PRs → raise to
  `floor(current) - 1`; never lower without an ADR") documented
  in `docs/CODING_GUIDE.md` "Coverage gates and the ratchet
  rule". 20 packages gated; `tui` left ungated (terminal
  interaction tests out of unit-test surface).
- `heddle.contrib.events` package skeleton (Sprint 1 of M2 plan):
  - `EventEnvelope` and `CommandMessage` Pydantic models in
    `heddle.contrib.events.envelopes`, with associated
    `EventMetadata` and `CommandMetadata`. (Initially landed in
    `heddle.core.messages` during Sprint 1; relocated to
    `heddle.contrib.events.envelopes` at the start of Sprint 2.
    Vendored JSON schemas are byte-identical.)
  - `heddle.contrib.events.subjects` — helpers for the
    `heddle.events.*`, `heddle.commands.*`, and `heddle.rejections.*`
    NATS subjects and `HEDDLE_EVENTS_*` / `HEDDLE_COMMANDS_*` /
    `HEDDLE_REJECTIONS_*` JetStream stream names. (Initially at
    `heddle.core.subjects`; relocated alongside the envelopes.)
  - `heddle.contrib.events.issuer_conventions` — six `is_*_issuer`
    validators (framework, observer, projector, user, system, bridge)
    plus `is_recognized_issuer`. Multi-segment suffixes accepted; no
    fixed segment cap.
  - JSON Schemas exported to `schemas/v1/event_envelope.schema.json`
    and `schemas/v1/command_message.schema.json`. The
    `tools/export_schemas.py --check` drift gate covers both.
  - Issuer-conventions section added to `docs/CONCEPTS.md`.
  - `uuid_utils >= 0.10.0` added as an `events` optional extra. Bare
    install is unaffected; only constructing an `EventEnvelope` /
    `CommandMessage` without an explicit id requires the extra. Refs:
    `heddle-contrib-events-m2-architecture-v7.md` §4.1, §6 Sprint 1.
- `heddle.contrib.events` runtime (Sprint 2 of M2 plan):
  - `heddle.contrib.events.errors` — six errors per v7 §4.5/§4.6,
    all inheriting `HeddleEventsError`: `UnknownEventVersionError`,
    `AggregateInvariantError`, `CommandRejected(reason, detail)`,
    `ConcurrencyError`, `BusResultTimeoutError`,
    `CorruptAggregateAlert`. `BusResultTimeoutError` is defined now
    so the hierarchy is complete from the start; it's raised by
    Sprint 3 NATS request/reply paths.
  - `heddle.contrib.events.aggregate` — three base classes:
    `Aggregate` (apply() discipline: deterministic, no I/O, mutate
    self only; snapshot-only N=512 dedup ring buffer; CAS via
    aggregate_version monotonicity; apply()-time provenance check
    on `InternalFinalized` events as the application-layer backstop
    for the Sprint 3 NATS publish ACL), `IntervalAggregate`
    (`created` → `active` → `finalized` phase machine, no-op on
    duplicate `InternalFinalized` per v7 §4.11), and `RootAggregate`
    (child registry that feeds P2 cascade). `snake_case` helper is
    public — used by both `apply()` and `CommandHandler` for
    `apply_*` / `handle_*` dispatch.
  - `heddle.contrib.events.registry` — `AGGREGATE_REGISTRY` and the
    `@register_aggregate("Type")` decorator. `get_aggregate_class()`
    raises `KeyError` for unknown types; `is_root_type()` feeds P1
    and P2 dispatch.
  - `heddle.contrib.events.event_log` — `EventLog` ABC and
    `InMemoryEventLog`. `append()` is CAS by `expected_version`;
    `None` skips the check (used by Sprint 4a PF observers
    fabricating envelopes from PF state), but envelope-level
    monotonicity is always enforced. `load()` is async-iter,
    `subscribe()` is asyncio.Queue-backed with cleanup on
    cancellation.
  - `heddle.contrib.events.rejection_log` — `RejectionLog` ABC,
    `InMemoryRejectionLog`, and the `RejectionEnvelope` Pydantic
    model (rejection_id UUIDv7, full command, reason, detail,
    rejected_at). JSON Schema exported to
    `schemas/v1/rejection_envelope.schema.json`; drift gate
    extended to cover it.
  - `heddle.contrib.events.command_handler` — `CommandHandler`
    base with the v7 §4.6 nine-step orchestration (load, replay,
    dedup check, version check, dispatch, append with CAS, apply,
    mark_processed; CommandRejected -> RejectionLog + re-raise).
    Metadata propagation: command_id and correlation_id from the
    command land on the produced event.
  - `heddle.contrib.events.dispatcher` — `Projector` ABC and
    `EventDispatcher` with serial fan-out per aggregate_type. A
    projector raising an exception is logged but does not stop
    subsequent projectors. `start()` is idempotent per type.
  - `heddle.contrib.events.projectors`:
    - `ScopeMembershipProjector` (P1) — complete. In-memory
      root → child membership view via the reserved
      `_child_membership` payload key. Sprint 3 migrates to a
      KV-backed view.
    - `CascadeProjector` (P2) — complete. On a root's
      `InternalFinalized` event, fans out `InternalFinalize`
      commands to all registered children. `command_id` is
      deterministic over `(root_id, child_id, root_event_id)` via
      sha256[:16] formatted as UUID, so retries dedupe naturally.
      Idempotence in Sprint 2 in-memory comes from the receiving
      aggregate rejecting `InternalFinalize` when already finalized
      (CommandRejected, swallowed). Sprint 3 will route the
      deterministic id through JetStream `Nats-Msg-Id` dedup.
    - `FinalizationHorizonProjector` (P3) — STUB. ABC + empty
      class so dependents import cleanly. Full implementation in
      Sprint 3 alongside the Valkey atomicity-window mechanism;
      docstring is the single source of truth for the Sprint 3
      plan and a forcing-function test (`test_docstring_marks_
      stub_for_sprint_3`) prevents the stub from graduating
      silently.
  - `heddle.contrib.events.testing` — public test utilities for
    downstream apps: `make_event`, `make_command`,
    `FakeIntervalAggregate` (registered "FakeInterval"),
    `FakeRootAggregate` (registered "FakeRoot").
  - `heddle/tests/fixtures.py` — pytest fixtures internal to
    heddle's test suite: `registry_isolation`, `in_memory_event_log`,
    `in_memory_rejection_log`, `command_handler`,
    `membership_projector`, `wired_dispatcher` (pre-wired with
    P1 + P2). Star-imported by `tests/conftest.py` for tree-wide
    discovery.
  - End-to-end demo scenario test
    (`tests/contrib/events/test_demo_scenario.py`) exercising every
    Sprint 2 component together: registry, both logs, handler,
    dispatcher, P1, P2, aggregate apply() discipline, dedup, and
    rejection path. The regression sentinel for Sprint 2.
  - Concepts doc extended with sections on the aggregate base
    classes, registration, EventLog/RejectionLog, CommandHandler
    flow, EventDispatcher, and the test surface. Refs:
    `heddle-contrib-events-m2-architecture-v7.md` §4.5–§4.9, §5.1,
    §6 Sprint 2.
- Four more framework metrics (completing OTel-audit S4's five-
  instrument plan; the first one, `heddle.tasks.completed`, landed
  in the previous commit):
  - `heddle.tasks.received` (counter, unit `1`, attrs `worker_type`,
    `model_tier`) — wired at the top of `TaskWorker.handle_message`
    after envelope parse. Paired with `heddle.tasks.completed`,
    gives operators per-worker in-flight task depth via
    `received - completed`.
  - `heddle.task.duration` (histogram, unit `ms`, attrs
    `worker_type`, `model_tier`, `status`) — recorded in
    `_publish_result` on every terminal-status path. The previous
    handler only computed elapsed-ms on the success path;
    `handle_message` now passes `elapsed=` on all paths
    (input-validation failure, output-validation failure,
    exception), so the histogram captures worst-case latency under
    load rather than happy-path-only.
  - `heddle.bus.publish.latency` (histogram, unit `ms`, attrs
    `messaging.destination.name`, `messaging.system`) — wired in
    `BaseActor.publish`. Uses OTel's `messaging.*` semantic
    conventions. `messaging.system` is the bus class name with
    `Bus` suffix stripped, lowercased (e.g. `"nats"`,
    `"in_memory"`), so dashboards can split per-transport.
  - `heddle.orchestrator.goals.received` (counter, unit `1`, attr
    `orchestrator_name`) — wired at the top of both
    `PipelineOrchestrator.handle_message` and
    `OrchestratorActor.handle_message`. `orchestrator_name` is the
    `actor_id`, so dashboards split per-orchestrator volume.
- `heddle.tracing.metrics` — new module shipping the OTel **metrics**
  surface for Heddle. Companion to `heddle.tracing.otel` (spans);
  shares the same no-op-when-absent pattern (every instrument call
  is safe with OTel uninstalled). Exposes:
  - `init_metrics(service_name, *, endpoint)` — sets up the OTLP
    metrics exporter. Idempotent. Called automatically from
    `init_tracing` so consumers keep one entry point.
  - `get_meter(name)` — returns a real Meter when OTel is installed,
    a no-op stand-in otherwise.
  - `record_task_completed(worker_type, model_tier, status, *, count)`
    — records the first framework instrument:
    `heddle.tasks.completed` (counter, unit=`1`, attributes
    `worker_type` / `model_tier` / `status`). Wired into
    `TaskWorker._publish_result` so every task contributes regardless
    of success/failure. Operators querying their backend
    (Prometheus, Grafana, etc.) can now split task counts by status
    without log scraping. Resolves the first of OTel-audit S4's five
    instruments; the remaining four (`heddle.tasks.received`,
    `heddle.task.duration`, `heddle.bus.publish.latency`,
    `heddle.orchestrator.goals.received`) land in a follow-up
    commit. The instrument-name table in the module docstring
    documents what's reserved as the surface grows. Attribute schema
    follows OTel semantic conventions for `messaging.*` (bus) and
    heddle-native names elsewhere.
- `heddle.tracing.otel.status()` — Python API returning a snapshot
  of the current tracing configuration as a dict:
  `{enabled, service_name, endpoint, exporter_class}`. Returns a
  shallow copy of internal module state populated on successful
  `init_tracing` calls; safe to mutate without side effects.
  Addresses the inspectability-of-defaults philosophy guardrail
  ("every default the system picks must be visible somewhere the
  user can read"): operators and consumers can ask "is OTel active?
  what endpoint? what exporter?" without process introspection.
  Resolves OTel-audit W1's Python-API half. The audit also proposed
  surfacing this on a `heddle status` CLI subcommand, but a general
  `heddle status` command does not exist today and creating one
  just for OTel would be premature scope. A `TODO(cli)` marker in
  the `status()` docstring records that the future `heddle status`
  command should call this function.
- `tools/check_envelope_convention.py` + CI integration —
  machine-checkable enforcement of the underscore-prefix
  middleware-lane convention documented in
  `heddle-workspace/anchors/CONTRACT_MAP.md` "Reserved middleware
  lane." Four rules: (1) no Pydantic field in
  `heddle.core.messages` may start with `_`; (2) tagged middleware
  modules (allowlist starts with `heddle.tracing.otel`) may only
  read/write `_`-prefixed keys on their carrier; (3) JSON schemas
  must not declare `_`-prefixed properties; (4) any schema setting
  `additionalProperties: false` must include
  `patternProperties: {"^_": {}}` to preserve the middleware lane.
  Added as a step in `.github/workflows/ci.yml` lint job; runnable
  locally via `uv run python tools/check_envelope_convention.py`.
  Resolves audit-question Q1 (M2 in `INVARIANT_AUDIT_2026-05-15.md`)
  via approach (A) — document and enforce the convention rather than
  hoist `_trace_context` into the schema. Rationale: keeps schemas
  focused on the application contract, matches the "shallow JSON
  Schema validation" invariant, and avoids the precedent of hoisting
  every future middleware field (correlation ID, tenant ID, etc.).
- `tests/test_envelope_convention.py` — four runtime tests pinning
  the convention's behaviour: `model_dump()` emits no `_`-prefixed
  keys; wire dicts carrying `_trace_context` round-trip through
  `model_validate` without raising or contaminating the typed
  envelope; Pydantic `extra` policy tolerates unknown `_*` keys
  (fails loudly if anyone flips `model_config` to `extra="forbid"`);
  `inject_trace_context` / `extract_trace_context` only touch
  `_trace_context` on the carrier. The lint script catches structural
  drift; these tests catch runtime regressions a structural check
  can't see.
- `heddle.tracing.otel.trace_correlation_processor` — structlog
  processor that tags log records with the active OTel span's
  `trace_id` (32-char hex) and `span_id` (16-char hex). Wired into
  the CLI's `structlog.configure(...)` call in `cli/main.py` so every
  log produced by a CLI-launched actor is automatically correlated
  to its trace. No-op when OTel isn't installed or when no span is
  active — safe to install unconditionally. Hex encoding matches the
  W3C traceparent convention used by most OTel backends. Library
  consumers configuring structlog elsewhere can opt in by importing
  the processor. See `OTEL_AUDIT_2026-05-15.md` S5 for the rationale.
- `tests/test_tracing_e2e.py` — end-to-end OTel propagation tests
  using real OTel SDK components (`TracerProvider` +
  `InMemorySpanExporter`) instead of the mock-only carriers in
  `tests/test_tracing.py`. Four tests guard against:
  - regressions to `extract_trace_context` at the worker entry
    (`actor.py:222`) that would silently fragment traces, and
  - regressions to `TaskWorker._publish_result`'s
    `inject_trace_context` call (the return-path symmetry added
    in the OTel-audit S1 fix above) — verified by reverting the
    inject and watching the test fail with the expected
    assertion message, then restoring.
  Test gracefully `importorskip`s when the `otel` extra isn't
  installed. See `OTEL_AUDIT_2026-05-15.md` S2 for the rationale.
- `heddle.core.kvstore` — general-purpose TTL-aware key-value store
  abstraction. Exports `KeyValueStore` (ABC), `InMemoryKeyValueStore`
  and `ScopedKeyValueStore` implementations, plus a tiny domain
  registry (`register_domain`, `domain_prefix`, `make_key`, `scoped`)
  for managing canonical key prefixes. The `"checkpoint"` domain is
  registered at module import with prefix `"heddle:checkpoint:"`.
  Substrate for orchestrator checkpoints today; aggregate snapshots
  (`heddle.contrib.events`) and `ProcessorWorker` cross-process locks
  in upcoming work.
- `docs/RELEASING.md` — agent-runnable, end-to-end release workflow
  covering version bump, CHANGELOG close-out, optional per-release
  notes file, tag, GitHub Release creation, and automated PyPI
  publish via trusted publishing
  (`.github/workflows/publish.yml`). Includes the post-release
  `gh release edit --notes-file` refresh step so the GitHub Release
  body doesn't drift from the in-repo source after typo fixes, and
  an explicit "what an agent should NOT do unilaterally" section
  (version bumps, tag pushes, direct `uv publish`, force-pushes).
  Cross-referenced from `AGENTS.md` "Repo pointers" and from
  `docs/releases/README.md`; added to the MkDocs Development nav.

## [0.9.2] — 2026-05-11

Full historical release notes: [`docs/releases/v0.9.2.md`](docs/releases/v0.9.2.md).

### Added

- **Wire contract published as JSON Schemas** under `schemas/v1/`,
  generated from the canonical Pydantic models and CI-gated for drift.
  Foreign-language SDKs can now generate idiomatic typed wrappers from
  a stable contract.
- `docs/foreign-actors.md` formalising `TaskMessage` / `TaskResult` as
  the wire envelope; `docs/gateway-actors.md` documenting three
  patterns for bridging non-NATS protocols (NATS-MQTT adapter, gateway
  actor, sidecar proxy).
- `TypedTaskWorker[PayloadT, OutputT]` mixin for Python workers that
  want strict typing on their domain payloads. Existing untyped
  `TaskWorker` behaviour unchanged.
- Pyright `strict` mode on the gated runtime surface (core, bus,
  worker, orchestrator); README badge added.
- Workshop NATS wiring: dead-letter UI and `notify_reload` now connect
  to NATS end-to-end.
- Seven new ADRs (006–012); dark-mode variants for every architecture
  diagram with Material-aware theme switching.
- J1–J9 regression tests pinning specific bugs identified in prior reviews.

### Changed

- `DESIGN_INVARIANTS.md` split into framework-safety contracts +
  `APPLICATION_PATTERNS.md` for application-level patterns.
- mDNS skips loopback interfaces and advertises the actual bound port.
- Council, RAG, ChatBridge hardening: synthesis budgets, per-turn
  floor, sliding-window chunk overlap, OpenAI `tool_calls` handling,
  Anthropic API version pinning, rollback-on-failure across all bridges.

### Removed (breaking)

- `TaskMessage.max_retries`, `TaskMessage.retry_count`, and
  `TaskStatus.RETRY`. Aspirational shims for a feature never built; no
  code paths read or emitted them. Removed before the first stable
  `schemas/v1/` release so foreign SDKs don't inherit dead fields.
  - **Migration:** stage-level retry continues to live in
    `PipelineOrchestrator` via the per-stage `max_retries: int` YAML
    field. Bus-level redelivery continues via NATS queue-group
    semantics. Full migration guide in
    [`docs/releases/v0.9.2.md`](docs/releases/v0.9.2.md) §1.

### Test coverage

2,678 unit tests passing; 91% coverage gate held.

## Earlier releases

Detailed notes for releases prior to v0.9.2 live in git tag metadata.
Tag dates and one-line summaries:

| Tag | Date | Summary |
|---|---|---|
| **v0.9.1** | 2026-05-08 | Codex review pass |
| **v0.9.0** | pre-2026-05-08 | (see `git show v0.9.0`) |
| **v0.8.0** | (see git) | MCP Workshop tools release |
| **v0.6.0** | 2026-03-20 | Evaluation, tracing, config tooling |
| **v0.4.0** | 2026-03-19 | App deployment, mDNS discovery, concurrent sessions |
| **v0.3.0** | 2026-03-13 | First versioned release |

Reconstruct details via `git log <prev-tag>..<tag>` or `git show <tag>`.

[Unreleased]: https://github.com/getheddle/heddle/compare/v0.9.2...HEAD
[0.9.2]: https://github.com/getheddle/heddle/releases/tag/v0.9.2
