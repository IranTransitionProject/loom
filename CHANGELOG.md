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

### Added

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

### Changed

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

### Added

- `heddle.core.kvstore` — general-purpose TTL-aware key-value store
  abstraction. Exports `KeyValueStore` (ABC), `InMemoryKeyValueStore`
  and `ScopedKeyValueStore` implementations, plus a tiny domain
  registry (`register_domain`, `domain_prefix`, `make_key`, `scoped`)
  for managing canonical key prefixes. The `"checkpoint"` domain is
  registered at module import with prefix `"heddle:checkpoint:"`.
  Substrate for orchestrator checkpoints today; aggregate snapshots
  (`heddle.contrib.events`) and `ProcessorWorker` cross-process locks
  in upcoming work.

### Changed

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

Full historical release notes: [`RELEASE_NOTES_v0.9.2.md`](RELEASE_NOTES_v0.9.2.md).

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
    [`RELEASE_NOTES_v0.9.2.md`](RELEASE_NOTES_v0.9.2.md) §1.

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
