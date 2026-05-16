# Heddle v0.9.2 — Release Notes

- **Date:** 2026-05-11
- **Previous version:** v0.9.1 (2026-05-08)
- **Breaking changes:** Yes — message-level retry fields removed (see Migration §1)
- **Total commits since v0.9.1:** 160

## Summary

v0.9.2 closes the two-week consolidation arc that began with the
2026-05-08 Codex review and the 2026-05-10 follow-up review.
**Twelve of fourteen findings** in the prior review were already
addressed at code level when this cycle began; the remaining work
was about reliability, observability, type safety, and **publishing
the wire contract so other languages can participate in a Heddle
bus.**

Headline changes:

- **Pyright `strict` mode** on the gated runtime surface (core,
  bus, worker, orchestrator), with the four Unknown-type rules
  explicitly deferred. README gains a `pyright: strict` badge.
- **Wire contract published as JSON Schemas** under
  `schemas/v1/`, generated from the canonical Pydantic models on
  every commit and CI-gated for drift. Foreign-language SDKs
  (.NET, Swift, etc.) can now generate idiomatic typed wrappers
  from a stable contract — no custom IDL, no bespoke codegen.
- **Foreign-actor and gateway documentation** —
  `docs/foreign-actors.md` formalises `TaskMessage` / `TaskResult`
  as the wire envelope and points at off-the-shelf codegen.
  `docs/gateway-actors.md` documents three patterns for bridging
  non-NATS protocols (NATS-MQTT adapter, gateway actor, sidecar
  proxy).
- **`TypedTaskWorker[PayloadT, OutputT]` mixin** — Python workers
  that want strict typing on their domain payloads can opt in.
  ~30 LoC; existing untyped `TaskWorker` behaviour unchanged.
- **Council, RAG, ChatBridge hardening** — synthesis budgets,
  per-turn floor, sliding-window chunk overlap, OpenAI tool_calls
  handling, Anthropic API version pinning, rollback-on-failure
  across all bridges, and more.
- **Workshop NATS wiring** — dead-letter UI and `notify_reload`
  now connect to NATS end-to-end. mDNS skips loopback and
  advertises the actual bound port.
- **Documentation overhaul** — DESIGN_INVARIANTS split into
  framework-safety contracts + APPLICATION_PATTERNS; seven new
  ADRs (006–012); dark-mode variants for every architecture
  diagram with Material-aware theme switching.
- **Test-suite expansion** — J1–J9 regression tests pin specific
  named bugs from the prior reviews. 2 678 unit tests passing,
  91 % coverage gate held.

## Breaking changes

### 1. `TaskMessage.max_retries`, `TaskMessage.retry_count`, and `TaskStatus.RETRY` removed (K2 / ADR-012)

The three symbols were documented as a worker-side retry contract,
but no code path read or emitted them — they were aspirational shims
for a feature that was never built. Now that the wire schema is
published, leaving the dead fields in `schemas/v1/` would lock them
into foreign-language SDKs as a stable contract. They are removed
**before the first release that names `schemas/v1/` as stable.**

The retry semantics that actually exist in Heddle are unchanged:

- **Stage-level retry** continues to live in `PipelineOrchestrator`
  via the per-stage `max_retries: int` YAML field. Validated at
  point-of-use; `max_retries: -1` raises a `PipelineStageError`
  naming the stage. **No migration needed for pipeline YAML
  files that already use this field.**
- **Bus-level redelivery** continues to be handled by NATS
  queue-group semantics when a worker disconnects mid-task.
  Transparent to applications.

#### Migration

| What you had | Replace with |
| --- | --- |
| `TaskMessage(worker_type="...", payload={...}, max_retries=N)` | `TaskMessage(worker_type="...", payload={...})` — set retries on the pipeline stage instead, not on the message |
| `TaskMessage(worker_type="...", payload={...}, retry_count=N)` | Drop the argument entirely. Never had any code-path consumer. |
| `if status == TaskStatus.RETRY:` | Remove the branch. RETRY was never emitted by any worker. |
| Custom test fixtures that constructed a `TaskResult(..., status=TaskStatus.RETRY, ...)` to exercise non-terminal handling | Use `TaskStatus.PENDING` or `TaskStatus.PROCESSING` — both are non-terminal and route to the same `in_flight` bucket in the synthesizer. |

Code search recipes to find affected sites:

```bash
grep -rn "max_retries\b" src/ tests/ | grep -v "stage.*max_retries\|YAML\|yaml"   # callsites
grep -rn "retry_count" src/ tests/                                                   # callsites
grep -rn "TaskStatus\.RETRY" src/ tests/                                            # status checks
```

No in-tree consumers outside the deleted fields' own tests touched
the dropped symbols.

#### Why now, not later

`schemas/v1/task_message.schema.json` is the foreign-language SDK
contract. If a foreign SDK is generated against a v1 that includes
the dead fields, removing them later requires a `schemas/v2/`
break. Removing them now is one commit; removing them later is a
migration. See ADR-012 for the full rejected-alternatives list.

### 2. Pyright now runs in `strict` mode

If your project shares the gated source tree with Heddle (subclasses
in `src/heddle/core`, `bus`, `worker`, or `orchestrator`), pyright
strict applies. Four `Unknown*` rules are explicitly downgraded to
`warning` to keep the noise manageable; the remaining strict rules
fire as errors.

Most likely surfaces in downstream code:

| Pyright complaint | What changed |
| --- | --- |
| `reportMissingTypeArgument` on `dict` / `list` / `asyncio.Task` | Bare generic types must now carry type arguments — `dict[str, Any]`, `list[str]`, `asyncio.Task[None]`. |
| `reportPrivateUsage` on cross-module `_helper` imports | Pyright now flags accesses to underscore-prefixed names across module boundaries. Either rename the helper (drop underscore) or annotate the access with `# pyright: ignore[reportPrivateUsage]` + a one-line rationale. |
| `reportUnnecessaryIsInstance` on tautological `isinstance(x, dict)` where `x: dict[str, Any]` | If the runtime check is real (e.g. the input came from YAML and might not be a dict), retype the parameter as `Any`. If the check is genuinely tautological, remove it. |

No public Python API of Heddle's gated packages changed shape; the
strict mode only tightens what was already typed.

## New capabilities

### Wire contract & cross-language interop

- `schemas/v1/{task_message,task_result,orchestrator_goal,checkpoint_state}.schema.json`
  exported from the canonical Pydantic models. CI fails on drift —
  edit a Pydantic model, run `uv run python tools/export_schemas.py`,
  commit the regenerated schemas.
- `docs/foreign-actors.md` documents the envelope semantics, the
  NATS subject conventions, the required behaviours
  (queue-group subscription, reset-between-tasks, skip-not-crash
  on malformed, OTel `traceparent` propagation), and the
  language-agnostic codegen recipes:

  ```bash
  # Swift Codable types
  quicktype --src schemas/v1/task_message.schema.json \
            --src-lang schema --lang swift --top-level TaskMessage \
            -o Sources/HeddleActor/TaskMessage.swift

  # .NET / C# with System.Text.Json
  quicktype --src schemas/v1/task_message.schema.json \
            --src-lang schema --lang csharp --framework SystemTextJson \
            --top-level TaskMessage \
            -o src/Heddle.Actor/TaskMessage.cs
  ```

- `docs/gateway-actors.md` covers three patterns for bridging
  non-NATS protocols: NATS-MQTT adapter (zero-code path when the
  external system speaks MQTT v3.1.1), gateway actor (HTTP
  webhook example included), and sidecar proxy (when the bridge
  is naturally co-located with one specific worker).

### Type safety

- `pyright: strict` on `core`, `bus`, `worker`, `orchestrator`.
  Four Unknown-type rules deferred to `warning` (documented in
  `[tool.pyright]` with a promotion-back-to-`error` roadmap as
  YAML/JSON/HTTP boundaries are annotated).
- `heddle.worker.typed.TypedTaskWorker[PayloadT, OutputT]` — a
  ~30-line mixin that wraps `TaskWorker.process` with
  Pydantic round-tripping. Subclasses declare
  `payload_model`/`output_model` Pydantic classes and implement
  `async def handle(self, payload: PayloadT, metadata: dict[str, Any]) -> OutputT`.
  The framework's existing JSON-Schema validation
  (`validate_input` / `validate_output`) runs FIRST — the typed
  models are a strictly local refinement, not a wire-contract
  loosening.

### Council framework

- **Synthesis budget + per-turn floor** (B1 / ADR-007):
  `CouncilConfig.synthesis_timeout_seconds` carves a dedicated
  budget for the facilitator's synthesis call; per-turn budgets
  below 5 s are rejected at config load.
- **Execution-path unification** (B1 / ADR-008): the
  `CouncilOrchestrator` (NATS path) and the `CouncilRunner`
  (CLI / MCP / tournament path) share one `call_with_budget`
  helper. A wedged backend can no longer hang any of CLI, MCP,
  tournament, or NATS execution.
- **Delphi alias map built once** (B3): per-matchup tournament
  uses a fresh `CouncilRunner` instance (B4) so shared mutable
  state can't leak across matchups.
- **Convergence parse hardened** (B2) against non-numeric judge
  scores.
- **Token accounting split** (B5): prompt vs completion tokens
  separated; lock-protected log injects no longer race.

### RAG / contrib

- **Sliding-window chunk overlap** (C1): `overlap_chars` is now
  applied to consecutive chunks rather than just stamped on the
  metadata (the prior bug retrieval-quality story).
- **Shared normalizer in `telegram_live`** (C3) — same
  RTL-aware normalisation as the batch ingestor.
- **Vector-store caching** (C4): `(class, db_path)` keyed cache
  replaces per-call DB-and-embedder construction.
- **DuckDB `get` returns full chunk** + **LanceDB stats avoids
  OOM** (C5a + C5b).
- **Chunker offset tracking** (C2) no longer round-trips through
  `str.find`.

### ChatBridge

- **Anthropic API version pinned** to `2023-06-01` (D1) — header
  stability across provider rollouts.
- **Rollback-on-failure across all four bridges** (D2) —
  Anthropic, OpenAI, Ollama, LMStudio, Manual. A failed API call
  leaves session state untouched; J6 regression tests pin every
  bridge.
- **OpenAI tool_calls handled** (D3) — surfaces as an
  `UnsupportedToolUseError` rather than silently dropping
  content.
- **API-key validation at construction** (D4) — bridges raise
  `ChatBridgeMisconfiguredError` immediately when the relevant
  env var is missing, instead of failing at first request.
- **Tournament concurrency** (B4) per-matchup `CouncilRunner`.

### Workshop

- **Wired to NATS end-to-end** (A1): `create_app()` accepts a
  `nats_url`; `DeadLetterConsumer` and `AppManager` get real
  bus access. The dead-letter UI is functional in production
  and `notify_reload()` actually broadcasts.
- **`/login` accepts token via POST body** (A3), not URL query —
  removes the previous "token in uvicorn access logs" exposure.
- **mDNS skips loopback bind + advertises actual port** (E3): no
  more `:8080` mis-advertisement when the workshop runs on a
  different port.
- **Single-worker assumption documented** (A4): WEB_CONCURRENCY
  > 1 with the default in-memory state would split workshop
  state across processes; the docs now flag this explicitly and
  the boot path warns at startup.
- **`WorkerTestRunner` shallow-copies caller's payload** (F5) —
  test runs no longer mutate the caller's input dict.

### Worker / contracts

- **Per-tool execution timeout, default 30 s** (F2). Wedged
  tool calls no longer hang the worker indefinitely.
- **Subprocess `env_passthrough` opt-in** (E1): the subprocess
  backend used to inherit the entire workshop process
  environment (every API key, every token); it now requires an
  explicit allow-list.
- **JSON-Schema validation WARNs on uncovered keywords without
  `type`** (G8) — surfaces config drift (e.g. `oneOf` /
  `allOf` without an outer `type`) without breaking the
  shallow-validation contract.

### Orchestrator / pipeline

- **Parallel subtask dispatch** (G5): subtasks within one
  parallel level publish concurrently via `asyncio.gather` with
  `return_exceptions=True` — one failed publish doesn't drop
  the rest.
- **Decomposer rationale logged** (G9) on every dispatched task
  for permanent audit trails.
- **`max_retries: -1` rejected at point-of-use** (G7) — used to
  trip a `NameError` deep in the retry loop; now raises a clean
  `PipelineStageError` naming the stage and the bad value.
- **Condition-evaluation defaults flipped to fail-closed** (G7 /
  ADR-010): malformed condition strings and unknown operators
  default to FALSE (skip the stage) with a `pipeline.invalid_condition`
  log warning. Legacy fail-open behaviour available via
  `HEDDLE_STRICT_CONDITIONS=0` for one-release migration.
- **`ResultStream` timeout vs subscription-close distinguished**
  (G6): two distinct outcomes that used to surface as the same
  log event.
- **Tri-state synthesizer partition** (K2-prior / ADR-006):
  `succeeded` / `failed` / `in_flight`. Renames in-flight tasks
  separately in the synthesis prompt instead of relabelling
  them as `failed`.

### Bus / router

- **Backoff + parallel publish + thread-safe rate limiter**
  (G2 + G3 + G4) — the router's token-bucket is now safe under
  concurrent dispatch.
- **NATS float timeout preserved** through CLI argument parsing
  (G10).
- **mDNS name rewrites logged** (G10) so operators can see when
  workshop instances rename themselves on conflict.

### Security

- **Telethon session file `chmod 0o600` after Telethon creates
  it** (E4) — was previously inheriting umask.
- **SQL identifier defence-in-depth** (E2) on `result_columns`
  and LanceDB `channel_ids` — schema-based allow-list +
  runtime check.
- **Actor `_wait_next_message` suppress() excludes
  BaseException** (E5) — `KeyboardInterrupt` /`SystemExit` no
  longer get swallowed during shutdown.
- **mDNS service discovery section** (H16) added to
  `SECURITY_MODEL.md`.
- **NATS transport security** clarified in `SECURITY_MODEL.md`
  §10.

### Documentation

- **DESIGN_INVARIANTS split** (I7) into:
  - `docs/DESIGN_INVARIANTS.md` — framework-safety contracts
    (mechanically enforced).
  - `docs/APPLICATION_PATTERNS.md` — discipline rules for
    blind-audit / knowledge-silo applications (NOT mechanically
    enforced; the framework can't tell when an application
    violates these).
- **Seven new ADRs** (006–012):
  - **006:** Tri-state synthesizer partition.
  - **007:** Council synthesis budget + 5 s per-turn floor.
  - **008:** Council execution paths share one budget helper.
  - **009:** Per-goal state isolation, lockless concurrency.
  - **010:** Condition-eval defaults — fail-closed by default,
    env-gated legacy.
  - **011:** Pipeline parallel levels use FIRST_COMPLETED,
    not gather.
  - **012:** Drop message-level retry fields.
- **Dark-mode diagrams** — every architecture diagram has a
  `-dark.svg` sibling, switched at render time by Material's
  active palette. Both drawio-sourced and Python-generated
  diagrams.
- **Foreign-Language Actors** + **Gateway Actors** user-guide
  entries added.
- **WORKSHOP_TOUR / CLI_REFERENCE / SECURITY_MODEL / TROUBLESHOOTING /
  CODING_GUIDE** refreshed against the new strict-pyright,
  schema-export, and condition-default semantics.

### Dependency bumps (potentially breaking for downstream apps)

The following dependencies took major-version steps. All landed
through dependabot PRs with full CI passing on every Python
version (3.11 / 3.12 / 3.13). If your application pins narrower
ranges, you'll need to widen them:

| Package | Was | Now |
| --- | --- | --- |
| `croniter` (dev) | `>=2.0.0` | `>=6.2.2` |
| `textual` (TUI) | `>=3.0.0` | `>=8.2.5` |
| `pytest-asyncio` (dev) | `>=0.23` | `>=1.3.0` |
| `redis` (contrib) | `>=5.0.0` | `>=7.4.0` |
| `structlog` (core) | `>=24.1.0` | `>=25.5.0` |
| `python-multipart` (workshop) | `>=0.0.9` | `>=0.0.28` |
| `opentelemetry-sdk` (otel) | `>=1.20.0` | `>=1.41.1` |
| `docling` (docproc) | `>=2.0.0` | `>=2.93.0` |
| `jinja2` (workshop) | `>=3.1.0` | `>=3.1.6` |
| `ollama` | `>=0.3.0` | `>=0.6.2` |

The 2 678-unit-test suite passes against the new versions; no
behavioural regressions surfaced. Action-version bumps in CI
workflows (`actions/setup-uv@7`, `actions/setup-python@6`,
`actions/deploy-pages@5`, `actions/upload-pages-artifact@5`)
landed alongside.

## Bug fixes (operator-visible)

- **Workshop dead-letter UI** showed empty results in production
  because `DeadLetterConsumer` was never connected to NATS (A1).
- **Workshop `notify_reload`** was a documented no-op (A1).
- **Council CLI / MCP / tournament hangs** on a wedged local
  backend because per-turn timeouts only applied to the NATS
  execution path (B1).
- **RAG `overlap_chars`** was configured but never applied; the
  field was stamped on every chunk's metadata while chunk
  construction ignored it (C1).
- **Vector-store recreation per call** in `VectorStoreBackend`
  (C4) — caused noticeable latency in tight retrieval loops.
- **OpenAI ChatBridge silently dropped `tool_calls`** when the
  model returned tool calls with empty `content` (D3).
- **`ChatBridge.send_turn`** left the failing user message in
  session history on API failure across all four bridges (D2).
- **Subprocess backend** inherited every environment variable
  (every API key, every token) from the workshop process by
  default (E1).
- **Telethon session file** was readable by other local users
  because the umask-derived permissions weren't tightened (E4).
- **`/login?token=…`** wrote the token into uvicorn access logs
  (A3).
- **Pipeline `max_retries=-1`** tripped a `NameError` deep in the
  retry loop with no useful traceback (G7).
- **mDNS advertised hardcoded port** when workshop bound to a
  non-default port (E3).
- **WorkerTestRunner** mutated the caller's payload dict (F5).

## Migration Quick-Reference

Most applications need **no code changes**. The breaking
changes are surgical:

1. **`TaskMessage(... max_retries=N)`** — drop the argument. Set
   retries on the pipeline stage YAML instead. If you weren't
   using stage-level retries, none were happening anyway.
2. **`TaskMessage(... retry_count=N)`** — drop the argument.
   Was dead.
3. **`TaskStatus.RETRY`** references — drop them. Was never
   emitted.
4. **Condition strings in pipeline YAML** — verify they're
   well-formed (`<path> <op> <value>` with `==` or `!=`). The
   post-G7 default skips malformed conditions instead of
   running the stage. Set `HEDDLE_STRICT_CONDITIONS=0` for
   one-release migration if a transition window is needed.
5. **Subprocess workers that relied on inherited env vars** —
   declare them explicitly in the worker config under
   `env_passthrough:`.
6. **Pyright-strict surface** — see Breaking Changes §2 above.
7. **Dependency pins** — widen if narrower than the new
   floors.

## Downstream notes — Baft (`IranTransitionProject/baft`)

Audit of the sibling `baft` repo (the primary downstream
application). **Net impact: zero blocking changes.** Several
adoptions are available:

### What Baft must update

| Item | Reason | Effort |
| --- | --- | --- |
| `[![Built on Heddle v0.9.2]` badge in `README.md` | Already `v0.9.2` — no change needed. | 0 |
| `pyproject.toml` `heddle-ai[...]` extras | No new extras required; same set works. | 0 |
| `structlog>=24.0` floor in `baft/pyproject.toml` | Heddle now requires `>=25.5.0`. Bump baft's floor to match. | 1 line |
| `croniter>=2.0` floor (dev) in `baft/pyproject.toml` | Heddle now requires `>=6.2.2`. Bump baft's floor to match. | 1 line |
| `pytest-asyncio>=0.23` floor (dev) | Heddle now requires `>=1.3.0`. Bump baft's floor. | 1 line |

### What Baft should verify

- **Pipeline conditions** in `configs/orchestrators/itp_*.yaml`:
  - `stages.synthesize.output.audit_report.escalation_required == true`
  - `stages.analyze.output.analytical_output.publication_flag == true`
  - `stages.cross_validate.output.validation_result.overall_status != 'FAIL'`
  All three parse cleanly under the new fail-closed default
  (well-formed three-token expressions with `==`/`!=`). The
  third one uses a **single-quoted string literal** `'FAIL'`,
  which Heddle's evaluator treats as the literal string
  `"'FAIL'"` (with the quotes included). If the upstream
  stage emits the value `"FAIL"` (unquoted), the comparison
  `"FAIL" != "'FAIL'"` is **always True** — the stage will
  always run. This is a pre-existing baft logic issue, not a
  v0.9.2 regression, but the new strict default makes it more
  important to know about. Either drop the quotes (`!= FAIL`)
  or emit the value with embedded quotes from the upstream
  stage.

- **Workshop `0.0.0.0` bind** in `charts/baft/templates/workshop.yaml`
  and `mcp.yaml`. The bind is correct for in-cluster deployment
  but post-E3 the mDNS advertisement now uses the actual
  outward-facing IP rather than the hardcoded `8080`. Verify
  the LAN discovery still finds the workshop after the upgrade
  (it should — the change is strictly an improvement).

- **`tests/test_new_heddle_features.py`** — exercises Heddle
  features explicitly (OTel, per-stage retries, ResultStream,
  config-impact, eval baselines, dead-letter replay). The
  per-stage `max_retries` test is **stage-level**, not the
  dropped `TaskMessage.max_retries`, so it's unaffected. The
  ResultStream test should still pass — no API changes.

### What Baft can newly adopt

These are opt-in upgrades, not migration requirements:

1. **TypedTaskWorker for the baft workers.** Baft's twelve
   workers (`sp_source_processor`, `ia_intelligence_analyst`,
   `tn_terminology_neutralizer`, `la_logic_auditor`, etc.) all
   have rich, well-defined input/output schemas in their YAML
   configs. Subclassing `TypedTaskWorker[PayloadT, OutputT]`
   instead of `TaskWorker` would give every baft worker:
   - Pydantic-typed `payload` access in the worker's
     `handle()` method (no more `payload.get("text", "")`).
   - Pyright-strict coverage on the worker's domain logic.
   - Output validation against a Pydantic model (in addition
     to the existing JSON-Schema validation).

   Adoption can be incremental — one worker at a time, since
   `TaskWorker` and `TypedTaskWorker` coexist.

2. **JSON-Schema-driven inter-stage validation.** Baft's
   pipeline stages pass structured data between SP → IA → XV →
   DE etc. The `input_mapping` fields reference these
   structures. If baft published its own
   `baft-schemas/v1/*.schema.json` alongside Heddle's, the
   inter-stage contracts would become first-class artifacts —
   testable in isolation, generatable into typed views for any
   future consumer (e.g. a Swift client app), and CI-checkable
   for drift the same way Heddle's are.

   The mechanism is the same `tools/export_schemas.py` pattern:
   a `tools/export_baft_schemas.py` that dumps baft's Pydantic
   payload/output models to `baft-schemas/v1/`, plus a
   `lint`-job drift check.

3. **Pyright strict on baft.** Baft has no `[tool.pyright]`
   block in its `pyproject.toml` today. Adopting Heddle's
   strict-with-deferred-Unknowns shape on baft's own packages
   would catch the same class of bugs Heddle now catches. The
   baft team can pick the gated surface package by package, as
   Heddle did.

4. **Foreign-actor gateway for ITP-external sources.** Baft's
   architecture supports Telegram ingestion via Heddle's
   `contrib.rag.ingestion.telegram_live`. Future external
   sources (RSS, mailing lists, custom REST APIs) are natural
   fits for the **gateway actor pattern** documented in
   `docs/gateway-actors.md`: a Heddle actor that translates
   the external protocol into `TaskMessage`s on
   `heddle.tasks.incoming`. No new framework abstraction
   needed; baft can ship gateways under
   `src/baft/gateways/<source>.py`.

5. **Dark-mode docs** are now available in Heddle's
   `mkdocs-material` config. Baft's docs use the same theme;
   adopting the same `docs/stylesheets/theme-aware-images.css`
   pattern (60 LoC) gives baft dark-mode parity for its own
   architecture diagrams.

6. **Council framework features** — if baft's audit pipeline
   evolves toward multi-agent deliberation (the council
   framework matured significantly in v0.9.2 with the
   synthesis-budget, per-turn-floor, and execution-path
   unification work), the framework is now production-ready
   for baft's adversarial-review use case in a way it wasn't
   in v0.9.1.

### Recommended baft v0.3.x → v0.4.x sequence

1. **Patch (v0.3.1):** Bump heddle to v0.9.2 + widen dep
   floors. No worker changes. No config changes. Verify the
   condition-string `'FAIL'` quoting in
   `itp_standard.yaml`.
2. **Minor (v0.4.0):** Pick one worker (probably `sp_source_processor`
   — simplest input/output) and migrate to
   `TypedTaskWorker`. Establish the pattern. Add
   `baft-schemas/v1/` export tooling and CI drift gate.
3. **Subsequent minors:** Migrate remaining workers one at a
   time. Adopt pyright-strict on `src/baft/` (basic mode
   first, then strict-with-deferred-Unknowns mirroring
   Heddle's approach).

No step is forced by Heddle v0.9.2; all are pure adoption upside.

## Verification

Local verification against this commit:

```text
uv run pytest tests/ -m "not integration and not deepeval"
    → 2678 passed in 35.85s

uv run --with pyright pyright
    → 0 errors, 177 warnings (all Unknown* type rules, deferred)

uv run ruff check src/ tests/ tools/
    → All checks passed

uv run ruff format --check src/ tests/ tools/
    → 274 files already formatted

uvx rumdl check docs/
    → No issues found in 59 files

uv run mkdocs build --strict
    → exit 0

uv run python tools/export_schemas.py --check
    → no drift
```

CI status on `main` (commit `6a013cb`): **CI ✅ · Docs ✅ · CodeQL ✅**

## Acknowledgements

The two-review consolidation arc that produced this release relied
heavily on the reviews themselves (`REPOSITORY_REVIEW_2026-05-08.md`
and `REPOSITORY_REVIEW_2026-05-10.md`), which catalogued specific
named issues with code references and severity ranks. Every
session letter A–K maps back to a section of one of those
documents; the session-starter files (gitignored) tracked the
queue.

ADRs 006–012 capture the design rationale for the load-bearing
decisions made during this cycle so that future contributors can
find the *why* alongside the *what*.
