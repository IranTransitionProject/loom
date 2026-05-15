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

_Nothing yet — the changelog starts active tracking with v0.9.3._

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
