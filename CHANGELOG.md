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

### Added

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
  `heddle-agent-toolkit/anchors/CONTRACT_MAP.md` "Reserved middleware
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

### Added

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

### Changed

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
