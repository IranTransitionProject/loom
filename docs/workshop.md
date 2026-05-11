# Worker Workshop — Design & Architecture

**Web-based tool for the LLM worker lifecycle: Define → Test → Evaluate → Compare → Deploy.**

---

## Overview

![Workshop UI](images/workshop-ui.svg)

The Workshop is a FastAPI web application that lets you build, test, evaluate,
and deploy LLM workers without touching the NATS actor mesh. It calls LLM
backends directly, validates I/O contracts, scores outputs against test suites,
tracks worker config versions in DuckDB, and edits pipeline stages with
dependency validation.

The key design constraint: **no NATS required**. The test bench and eval runner
bypass the bus, router, and actor lifecycle entirely — they call
`execute_with_tools()` on the LLM backend directly and validate the result
against the worker's I/O contracts. This makes the Workshop usable as a
standalone development tool even when no infrastructure is running.

> **Trust model.** The Workshop binds to `127.0.0.1` by default and
> ships open (no auth). Set `HEDDLE_WORKSHOP_TOKEN` before exposing
> any non-loopback bind. See [`SECURITY_MODEL.md`](SECURITY_MODEL.md)
> for the full per-surface trust contract (Workshop, MCP, NATS,
> knowledge silos, subprocess backends, secrets, retention).

---

## Architecture

```text
┌─────────────────────────────────────────────────────────────┐
│                  FastAPI Application (app.py)               │
│                                                             │
│   Jinja2 Templates + HTMX           Static Files           │
│   ┌─────────────────────┐            ┌────────────────────┐ │
│   │ workers/list        │            │ workshop.css       │ │
│   │ workers/detail      │            │ (Pico CSS v2 +     │ │
│   │ workers/test        │            │  dark mode +       │ │
│   │ workers/eval        │            │  responsive +      │ │
│   │ workers/eval_detail │            │  accessibility)    │ │
│   │ pipelines/list      │            └────────────────────┘ │
│   │ pipelines/editor    │                                   │
│   │ apps/list           │                                   │
│   │ apps/detail         │                                   │
│   │ partials/test_result│                                   │
│   └────────────────────┘                                    │
├─────────────────────────────────────────────────────────────┤
│                    Backend Components                       │
│                                                             │
│  ┌─────────────────┐  ┌──────────────┐  ┌────────────────┐  │
│  │ WorkerTestRunner│  │ EvalRunner   │  │ ConfigManager  │  │
│  │ (test_runner.py)│  │(eval_runner) │  │(config_manager)│  │
│  └───────┬─────────┘  └──────┬───────┘  └────────┬───────┘  │
│          │                  │                    │          │
│          │                  │          ┌─────────┴───────┐  │
│          │                  │          │ PipelineEditor  │  │
│          │                  │          │(pipeline_editor)│  │
│          │                  │          └─────────────────┘  │
│          │                  │                               │
│  ┌───────▼──────────────────▼─────────┐  ┌──────────────┐  │
│  │           WorkshopDB (db.py)       │  │  AppManager  │  │
│  │   worker_versions │ eval_runs      │  │(app_manager) │  │
│  │   eval_results    │ worker_metrics │  └──────────────┘  │
│  └────────────────────────────────────┘                     │
├─────────────────────────────────────────────────────────────┤
│               Heddle Core (reused)          Optional          │
│                                                             │
│  LLMBackend (backends.py)        HeddleServiceAdvertiser      │
│  execute_with_tools()            (discovery/mdns.py)        │
│  _extract_json()                                            │
│  _load_tool_providers()          validate_input/output()    │
│  build_backends_from_env()       validate_worker_config()   │
│  load_knowledge_silos()          validate_pipeline_config() │
│  AppManifest (manifest.py)       load_config()              │
│                                  PipelineOrchestrator.*     │
│                                  WorkspaceManager           │
└─────────────────────────────────────────────────────────────┘
```

### Dependency flow

```text
app.py
 ├── WorkerTestRunner(backends)            # needs LLM backends
 ├── EvalRunner(test_runner, db)           # wraps runner + persistence
 ├── AppManager(apps_dir)                  # ZIP deploy, list, remove
 ├── ConfigManager(configs_dir, db, extra) # filesystem + version tracking + app dirs
 ├── PipelineEditor                        # stateless, no constructor
 ├── RAGManager(store, registry)           # vector store + channel registry for RAG dashboard
 └── HeddleServiceAdvertiser                 # optional mDNS (if zeroconf installed)
```

`create_app()` is the composition root. It creates all components, wires them
together, and defines all routes as closures that capture the shared instances.
A FastAPI lifespan context manager starts/stops mDNS advertisement when the
`zeroconf` package is installed.

---

## Source files

```text
src/heddle/workshop/
├── __init__.py           # Package docstring only
├── app.py                # FastAPI app factory (create_app), 36 route handlers, mDNS lifespan
├── app_manager.py        # AppManager — ZIP deploy, list, remove app bundles
├── test_runner.py        # WorkerTestRunner — single-payload LLM execution
├── eval_runner.py        # EvalRunner — batch test suite with scoring
├── config_manager.py     # ConfigManager — CRUD for YAML configs + multi-dir scanning
├── pipeline_editor.py    # PipelineEditor — stateless pipeline manipulation
├── rag_manager.py        # RAGManager — vector store + channel registry for RAG dashboard
├── db.py                 # WorkshopDB — DuckDB storage
├── templates/
│   ├── base.html         # Layout: sticky nav, theme toggle, skip link, Pico CSS, HTMX
│   ├── workers/
│   │   ├── list.html     # Worker table with Test/Eval actions, app source labels
│   │   ├── detail.html   # YAML editor, clone form, version history
│   │   ├── test.html     # Interactive test bench (HTMX form)
│   │   ├── eval.html     # Eval suite form + past runs table
│   │   └── eval_detail.html  # Per-case results with expandable details
│   ├── pipelines/
│   │   ├── list.html     # Pipeline table with app source labels
│   │   └── editor.html   # Dependency graph + stage operation forms
│   ├── rag/
│   │   ├── dashboard.html    # RAG overview: store stats, channel summary, quick search
│   │   ├── channels.html     # Channel grid with trust/bias badges, filterable
│   │   └── search.html       # Full semantic search interface
│   ├── apps/
│   │   ├── list.html     # Deployed apps table + ZIP upload form
│   │   └── detail.html   # App manifest viewer + entry configs + remove
│   └── partials/
│       ├── test_result.html       # HTMX fragment: test bench result card
│       ├── rag_stats.html         # HTMX fragment: vector store statistics
│       └── rag_search_result.html # HTMX fragment: search results list
└── static/
    └── workshop.css      # Pico CSS v2 overrides: dark mode, responsive,
                          #   accessibility (skip link, focus-visible, reduced motion,
                          #   high contrast), pipeline graph, print styles
```

---

## Component reference

### WorkerTestRunner (`test_runner.py`)

Executes a worker config against a single payload. Replicates the full
`LLMWorker.process()` flow without the actor lifecycle:

1. Validate worker config via `validate_worker_config()`
2. Validate input payload against `input_schema` via `validate_input()`
3. Build system prompt:
   - Inject knowledge silos (`load_knowledge_silos()`)
   - Inject legacy knowledge sources (`load_knowledge_sources()`)
   - Resolve file-ref fields via `WorkspaceManager`
4. Load tool providers from silos (`_load_tool_providers()`)
5. Resolve tier → backend (from `build_backends_from_env()` result)
6. Call `execute_with_tools()` — the standalone tool-use loop
7. Parse JSON from raw LLM response via `_extract_json()`
8. Validate output against `output_schema` via `validate_output()`

Returns a `WorkerTestResult` dataclass:

| Field | Type | Description |
|-------|------|-------------|
| `output` | `dict \| None` | Parsed JSON output |
| `raw_response` | `str \| None` | Raw LLM text |
| `validation_errors` | `list[str]` | Output schema violations |
| `input_validation_errors` | `list[str]` | Input schema violations |
| `token_usage` | `dict[str, int]` | `prompt_tokens`, `completion_tokens` |
| `latency_ms` | `int` | Wall-clock time |
| `model_used` | `str \| None` | Model identifier from backend |
| `error` | `str \| None` | Exception message if failed |
| `success` | `bool` (property) | True if no errors and valid output |

**Key design decisions:**

- Calls `execute_with_tools()` directly — this is a module-level function
  extracted from `LLMWorker` specifically for Workshop reuse.
- Catches all exceptions and returns them in `error` — never raises.
- Knowledge silo injection failures are logged but don't abort the test.

### EvalRunner (`eval_runner.py`)

Runs a list of test cases against a worker config with scoring.

**Inputs:**

- `config`: Worker config dict (same as YAML)
- `test_suite`: List of `{"name": str, "input": dict, "expected_output": dict}`
- `tier`: Model tier override
- `scoring`: `"field_match"`, `"exact_match"`, or `"llm_judge"`
- `max_concurrency`: Semaphore bound (default 3)
- `judge_backend`: LLM backend for `llm_judge` scoring (required when `scoring="llm_judge"`)
- `judge_prompt`: Custom system prompt for the judge LLM (optional, uses `DEFAULT_JUDGE_PROMPT` if not provided)

**Execution:**

1. Save worker config version to DB (deduplicates by SHA-256 hash)
2. Create eval run record in DB (with `scoring_method` in metadata)
3. Run all test cases concurrently (bounded by `asyncio.Semaphore`)
4. For each case: call `WorkerTestRunner.run()`, score, persist result
5. Update run summary with aggregated stats

**Scoring methods:**

| Method | Logic | Pass threshold |
|--------|-------|----------------|
| `field_match` | Fraction of expected fields matching actual output. Strings compared case-insensitively. Lists scored by subset overlap. | score >= 0.5 |
| `exact_match` | 1.0 if `expected == actual`, else 0.0 | score >= 0.5 |
| `llm_judge` | Separate LLM call evaluates output on correctness, completeness, and format compliance. Returns 0-to-1 score with reasoning. Handles markdown-fenced JSON responses and clamps scores to [0, 1]. | score >= 0.5 |

**Concurrency model:** `asyncio.gather()` with `asyncio.Semaphore(max_concurrency)`.
Nonlocal counters (`passed`, `failed`, `total_latency`) are safe because the
event loop is single-threaded — the semaphore only bounds concurrent backend
calls, not parallel threads.

### ConfigManager (`config_manager.py`)

CRUD for worker and pipeline YAML configs, backed by filesystem with optional
DuckDB version tracking.

**Workers:**

| Method | What it does |
|--------|-------------|
| `list_workers()` | Glob `configs/workers/*.yaml`, skip `_template.yaml`, return name/description/tier/kind |
| `get_worker(name)` | Load and parse YAML via `load_config()` |
| `get_worker_yaml(name)` | Return raw YAML text (for the editor textarea) |
| `save_worker(name, config)` | Validate via `validate_worker_config()`, write YAML, save version to DB |
| `clone_worker(src, new)` | Load source, change name, save as new |
| `delete_worker(name)` | Delete YAML file |
| `get_worker_version_history(name)` | Query DB for all versions |

**Pipelines:**

| Method | What it does |
|--------|-------------|
| `list_pipelines()` | Glob `configs/orchestrators/*.yaml`, return name/stage_count |
| `get_pipeline(name)` | Load and parse YAML |
| `save_pipeline(name, config)` | Validate via `validate_pipeline_config()`, write YAML |

**File layout convention:** Worker configs live in `configs/workers/{name}.yaml`.
Pipeline configs live in `configs/orchestrators/{name}.yaml`. The `configs_dir`
constructor arg points to the parent of both.

### PipelineEditor (`pipeline_editor.py`)

Stateless operations on pipeline config dicts. All methods are `@staticmethod`,
take a config dict, and return a modified deep copy. No filesystem I/O.

| Method | What it does |
|--------|-------------|
| `get_dependency_graph(config)` | Compute deps + execution levels using `PipelineOrchestrator._infer_dependencies()` and `_build_execution_levels()` |
| `insert_stage(config, stage_def, after)` | Insert a stage after a named stage (or at end) |
| `remove_stage(config, stage_name)` | Remove a stage; raises `ValueError` if other stages depend on it |
| `swap_worker(config, stage_name, new_type, new_tier)` | Replace `worker_type` (and optionally `model_tier`) on a stage |
| `add_parallel_branch(config, stage_def)` | Append a stage with only `goal.*` input mappings (Level 0) |
| `validate(config)` | `validate_pipeline_config()` + cycle detection via `_build_execution_levels()` |

**Dependency validation:** `remove_stage()` checks both `input_mapping` path
references and explicit `depends_on` lists. `add_parallel_branch()` rejects
stages whose `input_mapping` references existing stage names (must reference only
`goal.*`).

### Config Impact Analysis (`config_impact.py`)

Reverse-maps worker changes to their pipeline impact. Used by the worker
detail page to show affected pipelines and risk assessment.

| Function | What it does |
|----------|-------------|
| `get_worker_impact(worker_name, configs_dir, extra_dirs)` | Find all pipelines referencing a worker, trace downstream stages, assess risk |

**Impact result fields:**

| Field | Type | Description |
|-------|------|-------------|
| `affected_pipelines` | `list[str]` | Pipeline names that use this worker |
| `direct_stages` | `list[dict]` | Stages that directly invoke the worker |
| `downstream_stages` | `list[dict]` | Stages that depend on the worker's output (transitive) |
| `risk_level` | `str` | `"low"`, `"medium"`, or `"high"` — based on whether the worker has an `output_schema` |

**UI integration:** The worker detail page loads an impact panel asynchronously
via HTMX (`/workers/{name}/impact-panel`). A JSON API is also available at
`/workers/{name}/impact`.

### WorkshopDB (`db.py`)

DuckDB-backed persistence. Default path: `~/.heddle/workshop.duckdb`.
Use `:memory:` for tests.

**Tables:**

| Table | Purpose | Key columns |
|-------|---------|-------------|
| `worker_versions` | Config snapshot history | `worker_name`, `config_hash` (SHA-256 prefix), `config_yaml` |
| `eval_runs` | Eval suite execution summary | `worker_name`, `tier`, `status`, `passed_cases`/`failed_cases`, `avg_latency_ms` |
| `eval_results` | Per-case eval results | `run_id`, `case_name`, `input_payload`, `expected_output`, `actual_output`, `score`, `passed` |
| `eval_baselines` | Golden dataset baselines | `worker_name` (UNIQUE), `run_id`, `promoted_at`, `description` |
| `worker_metrics` | Aggregated live metrics | `worker_name`, `tier`, `request_count`, `success_count`, `avg_latency_ms`, `p95_latency_ms` |

**Version deduplication:** `save_worker_version()` hashes the YAML content
(SHA-256, first 16 chars) and skips insertion if a version with the same
`(worker_name, config_hash)` already exists. This means saving an unchanged
config is a no-op.

**Comparison:** `compare_eval_runs(run_id_a, run_id_b)` joins results by
`case_name` and returns a side-by-side structure for A/B display.

**Baselines:** `promote_baseline(worker_name, run_id)` marks an eval run as the
golden dataset baseline for a worker (one per worker, upserted).
`compare_against_baseline(worker_name, run_id)` compares a run against the
stored baseline. The eval detail page automatically shows regression/improvement
when a baseline exists. `remove_baseline(worker_name)` clears the baseline.

---

## Web layer

### Technology stack

| Layer | Technology | Role |
|-------|-----------|------|
| Server | FastAPI | Async routes, form handling, JSON responses |
| Templates | Jinja2 | Server-rendered HTML pages |
| Interactivity | HTMX 2.0 | Async form submissions (test bench), partial page updates |
| Styling | Pico CSS 2.0 | Classless semantic HTML styling |
| Custom CSS | `workshop.css` | Dark/light mode, responsive layout, accessibility, pipeline graph |

### Route map

| Method | Path | Handler | Template | Description |
|--------|------|---------|----------|-------------|
| GET | `/` | `root` | — | Redirect to `/workers` |
| GET | `/health` | `health` | — | JSON: `{status, backends}` |
| GET | `/login` | `login_form` | inline HTML | Token bootstrap form (404 when `HEDDLE_WORKSHOP_TOKEN` is unset) |
| POST | `/login` | `login_submit` | — | Validate token from form body, set cookie (redirect 303) |
| GET | `/workers` | `workers_list` | `workers/list.html` | Worker table |
| GET | `/workers/{name}` | `worker_detail` | `workers/detail.html` | Config editor + version history |
| POST | `/workers/{name}` | `worker_save` | — | Save edited YAML (redirect 303) |
| POST | `/workers/{name}/clone` | `worker_clone` | — | Clone worker (redirect 303) |
| GET | `/workers/{name}/test` | `worker_test` | `workers/test.html` | Test bench form |
| POST | `/workers/{name}/test/run` | `worker_test_run` | `partials/test_result.html` | HTMX: execute test, return result card |
| GET | `/workers/{name}/eval` | `worker_eval` | `workers/eval.html` | Eval dashboard + run form |
| POST | `/workers/{name}/eval/run` | `worker_eval_run` | — | Run eval suite (redirect 303) |
| GET | `/workers/{name}/eval/{run_id}` | `worker_eval_detail` | `workers/eval_detail.html` | Per-case results + baseline comparison |
| POST | `/workers/{name}/eval/{run_id}/promote-baseline` | `worker_promote_baseline` | -- | Promote run as baseline (redirect 303) |
| POST | `/workers/{name}/eval/remove-baseline` | `worker_remove_baseline` | -- | Remove worker baseline (redirect 303) |
| POST | `/workers/{name}/validate` | `worker_validate` | `partials/validation_errors.html` | HTMX: validate posted YAML, return inline error list |
| GET | `/workers/{name}/impact` | `worker_impact` | — | JSON: config impact analysis |
| GET | `/workers/{name}/impact-panel` | `worker_impact_panel` | — | HTMX: impact analysis panel |
| GET | `/pipelines` | `pipelines_list` | `pipelines/list.html` | Pipeline table |
| GET | `/pipelines/{name}` | `pipeline_detail` | `pipelines/editor.html` | Dep graph + stage operations |
| POST | `/pipelines/{name}/stage` | `pipeline_stage_edit` | — | Insert/remove/swap/branch (redirect 303) |
| GET | `/pipelines/{name}/graph` | `pipeline_graph` | — | JSON: dependency graph |
| GET | `/apps` | `apps_list` | `apps/list.html` | Deployed apps + upload form |
| GET | `/apps/{name}` | `app_detail` | `apps/detail.html` | App manifest viewer |
| POST | `/apps/deploy` | `app_deploy` | `apps/preview.html` | Upload ZIP, render capability preview (or 303 if `?auto_approve=1`) |
| POST | `/apps/deploy/confirm/{token}` | `app_deploy_confirm` | — | Finalize a staged deploy (redirect 303) |
| POST | `/apps/deploy/cancel/{token}` | `app_deploy_cancel` | — | Discard a staged deploy (redirect 303) |
| POST | `/apps/{name}/remove` | `app_remove` | — | Remove deployed app (redirect 303) |
| GET | `/rag` | `rag_dashboard` | `rag/dashboard.html` | RAG overview: store stats, channels, quick search |
| GET | `/rag/channels` | `rag_channels` | `rag/channels.html` | Channel grid with metadata + filtering |
| GET | `/rag/search` | `rag_search` | `rag/search.html` | Full semantic search interface |
| POST | `/rag/search/run` | `rag_search_run` | `partials/rag_search_result.html` | HTMX: execute vector search |
| GET | `/rag/store/stats` | `rag_store_stats` | `partials/rag_stats.html` | HTMX: vector store statistics |
| GET | `/dead-letters` | `dead_letters_list` | `dead_letters.html` | Dead-letter entries + replay audit log |
| POST | `/dead-letters/{index}/replay` | `dead_letter_replay` | — | Replay entry to incoming (redirect 303) |
| POST | `/dead-letters/clear` | `dead_letters_clear` | — | Clear all entries (redirect 303) |

### HTMX pattern

Several routes return HTML fragments meant to be swapped into a target
element rather than re-render the page. The endpoints currently using
this pattern:

- `POST /workers/{name}/test/run` → `partials/test_result.html` —
  test bench result card (PASS/FAIL badge, token counts, output JSON,
  validation errors, raw response)
- `POST /workers/{name}/validate` → `partials/validation_errors.html` —
  inline YAML validation error list while editing
- `GET /workers/{name}/impact-panel` → `partials/impact_panel.html` —
  config impact analysis panel
- `GET /rag/store/stats` → `partials/rag_stats.html` —
  vector store statistics
- `POST /rag/search/run` → `partials/rag_search_result.html` —
  semantic search result list

Test-bench flow as a worked example:

1. User fills payload JSON and selects tier in `workers/test.html`
2. Form has `hx-post="/workers/{name}/test/run"` and `hx-target="#test-result"`
3. Server calls `WorkerTestRunner.run()` (may take seconds for LLM call)
4. Server returns `partials/test_result.html` — an `<article>` card
5. HTMX swaps the card into `#test-result` div without a full page reload
6. Loading indicator `#spinner` shows `aria-busy="true"` during the request

All non-fragment forms use standard POST → 303 redirect → GET (PRG pattern).

### Template hierarchy

```text
base.html                       # <html>, sticky nav, theme toggle, skip link, <main>, <footer>
├── workers/list.html           # Table of workers (with app source labels)
├── workers/detail.html         # YAML editor + clone + version history
├── workers/test.html           # Test bench form + #test-result target (aria-live)
├── workers/eval.html           # Eval form + past runs table
├── workers/eval_detail.html    # Per-case results + expandable details
├── pipelines/list.html         # Table of pipelines (with app source labels)
├── pipelines/editor.html       # Dep graph + 4 stage operation forms
├── rag/dashboard.html          # RAG overview: store stats, channels, quick search
├── rag/channels.html           # Channel grid with trust/bias badges
├── rag/search.html             # Full semantic search interface
├── apps/list.html              # Deployed apps table + ZIP upload form
├── apps/detail.html            # App manifest viewer + entry configs + remove
└── dead_letters.html           # Dead-letter entries + replay audit log

partials/
├── test_result.html            # HTMX fragment (no base.html extends)
├── rag_stats.html              # HTMX fragment: vector store statistics
└── rag_search_result.html      # HTMX fragment: search results list
```

All full-page templates extend `base.html` and set `active_nav` for nav
highlighting. The partial template is standalone (no `{% extends %}`).

---

## CLI entry point

```bash
uv run heddle workshop [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--port` | `8080` | HTTP server port |
| `--host` | `127.0.0.1` | Bind address |
| `--configs-dir` | `configs/` | Root directory for worker/pipeline YAML |
| `--db-path` | `~/.heddle/workshop.duckdb` | DuckDB database path |
| `--nats-url` | None | NATS URL for live metrics (optional) |
| `--apps-dir` | `~/.heddle/apps` | Root directory for deployed app bundles |

The CLI command creates the app via `create_app()` and runs it under Uvicorn.

> **Run as a single-worker process.** The deploy path mutates
> `ConfigManager.extra_config_dirs` and swaps the dead-letter bus
> in-place without locks, on the assumption that route handlers
> serialise under a single FastAPI/Uvicorn worker. Running under
> `--workers N` (or `WEB_CONCURRENCY=N`) where `N > 1` will produce
> diverging state between replicas and the workshop logs a
> `workshop.multi_worker_unsupported` warning at startup if it
> detects the configuration.
>
> RAG-specific configuration (`rag_db_path`, `rag_store_class`,
> `rag_channel_registry`) is exposed only as `create_app()`
> constructor arguments and is wired up by the `heddle rag serve`
> command — not by `heddle workshop`. See
> [CLI Reference](CLI_REFERENCE.md#heddle-rag-serve) for the
> RAG-aware variant.

### LLM backend resolution

Backends are resolved from environment variables via `build_backends_from_env()`:

| Env var | Tier | Backend |
|---------|------|---------|
| `OLLAMA_URL` | `local` | `OllamaBackend` |
| `OLLAMA_MODEL` | — | Override Ollama model (default: `llama3.2:3b`) |
| `ANTHROPIC_API_KEY` | `standard` + `frontier` | `AnthropicBackend` |
| `FRONTIER_MODEL` | — | Override frontier model (default: `claude-opus-4-20250514`) |

If no env vars are set, `backends` is empty and all test/eval runs will fail
with "No backend for tier" errors. The `/health` endpoint reports available
backends.

---

## Data model

### DuckDB schema (ER diagram)

```text
worker_versions           eval_runs                 eval_results
─────────────────         ─────────────             ──────────────
id (PK)                   id (PK)                   id (PK)
worker_name          ┌──▶ worker_version_id    ┌──▶ run_id (FK)
config_hash (UNIQUE) │    worker_name          │    case_name
config_yaml          │    tier                 │    input_payload
created_at           │    started_at           │    expected_output
description          │    completed_at         │    actual_output
                     │    status               │    raw_response
                     │    total_cases          │    validation_errors
                     │    passed_cases         │    score
                     │    failed_cases         │    score_details
                     │    avg_latency_ms       │    latency_ms
                     │    avg_prompt_tokens    │    prompt_tokens
                     │    avg_completion_tokens│    completion_tokens
                     │    metadata             │    model_used
                     │                         │    passed
                     │                         │    error
                     │                         │
eval_baselines       │                         │
──────────────       │                         │
id (PK)              │                         │
worker_name (UNIQUE) │                         │
run_id ──────────────┘                         │
promoted_at                                    │
description                                    │
                                               │
worker_metrics                                 │
──────────────                                 │
id (PK)              └── (joined via           └── (FK relationship)
worker_name              worker_version_id)
tier
recorded_at
window_seconds
request_count
success_count
failure_count
avg_latency_ms
p95_latency_ms
avg_prompt_tokens
avg_completion_tokens
```

### AppManager (`app_manager.py`)

Manages deployed Heddle app bundles (ZIP archives) in `~/.heddle/apps/`.

| Method | What it does |
|--------|-------------|
| `list_apps()` | Scan apps dir, load manifest from each subdirectory |
| `get_app(name)` | Load a single app's `AppManifest` |
| `get_app_configs_dir(name)` | Return `~/.heddle/apps/{name}/configs/` path |
| `deploy_app(zip_path)` | Validate ZIP structure + manifest, extract to apps dir |
| `remove_app(name)` | Delete app directory |
| `notify_reload()` | Publish `{"action": "reload"}` to `heddle.control.reload` |

**ZIP deployment flow:**

1. Validate ZIP contains `manifest.yaml` at root
2. Parse + validate manifest via `AppManifest` Pydantic model
3. Security check: reject paths with `..` or absolute paths
4. Verify all referenced config files exist in the ZIP
5. Extract to `~/.heddle/apps/{app_name}/`
6. Warn about Python packages needing manual install
7. Publish reload notification to NATS control channel

After deployment, `ConfigManager.extra_config_dirs` is refreshed so app
workers/pipelines appear alongside base configs in the Workers/Pipelines lists.

### mDNS Service Discovery

When the optional `zeroconf` package is installed (`pip install heddle-ai[mdns]`),
the Workshop automatically advertises itself on the local network via mDNS/Bonjour.

The integration uses a FastAPI lifespan context manager:

- **On startup:** Creates `HeddleServiceAdvertiser`, registers Workshop HTTP service
- **On shutdown:** Unregisters all services, closes zeroconf

If `zeroconf` is not installed, the Workshop logs a hint and continues normally.

The standalone `heddle mdns` CLI command can advertise Workshop, NATS, and MCP
services without running the Workshop itself.

### Unique constraints

- `worker_versions`: `UNIQUE (worker_name, config_hash)` — deduplicates
  identical configs.
- All primary keys are UUID v4 strings.

---

## Reused Heddle internals

The Workshop reuses core Heddle functions rather than reimplementing them. This
keeps the test bench semantically identical to production worker execution.

| Function / Class | Source | Used by Workshop for |
|-----------------|--------|---------------------|
| `execute_with_tools()` | `worker/runner.py` | Full LLM call with tool-use loop |
| `_extract_json()` | `worker/runner.py` | JSON parsing from LLM response |
| `_load_tool_providers()` | `worker/runner.py` | Loading silo-based tools |
| `build_backends_from_env()` | `worker/backends.py` | Resolving available LLM backends |
| `validate_input()` | `core/contracts.py` | Input schema validation |
| `validate_output()` | `core/contracts.py` | Output schema validation |
| `validate_worker_config()` | `core/config.py` | Worker config structure validation |
| `validate_pipeline_config()` | `core/config.py` | Pipeline config validation |
| `load_config()` | `core/config.py` | YAML loading |
| `load_knowledge_silos()` | `worker/knowledge.py` | Knowledge silo injection |
| `load_knowledge_sources()` | `worker/knowledge.py` | Legacy knowledge injection |
| `WorkspaceManager` | `core/workspace.py` | File-ref resolution |
| `PipelineOrchestrator._infer_dependencies()` | `orchestrator/pipeline.py` | Dependency graph computation |
| `PipelineOrchestrator._build_execution_levels()` | `orchestrator/pipeline.py` | Topological sort for execution levels |

---

## Enhancement guide

### Adding a new scoring method

1. Write a function in `eval_runner.py` matching the signature:

   ```python
   def _score_my_method(expected: dict, actual: dict) -> tuple[float, dict]:
       # Return (score_0_to_1, {"method": "my_method", ...details})
   ```

   For async scoring methods (like `_score_llm_judge`), the signature becomes:

   ```python
   async def _score_my_method(expected, actual, *, backend, ...) -> tuple[float, dict]:
   ```

2. Add a branch in `EvalRunner.run_suite()`:

   ```python
   if scoring == "my_method":
       score_fn = _score_my_method
   ```

3. Add an `<option>` in `workers/eval.html` scoring `<select>`.

### Adding a new page

1. Create template in `templates/{section}/{page}.html` extending `base.html`.
2. Add route handler in `app.py` inside `create_app()`.
3. If it needs HTMX partial updates, add a `partials/` template and use
   `hx-post`/`hx-target`.

### Adding a new pipeline stage operation

1. Add a `@staticmethod` method in `PipelineEditor`.
2. Add an `elif action == "my_action"` branch in `pipeline_stage_edit()` route.
3. Add a `<details>` form block in `pipelines/editor.html`.

### Adding live metrics via NATS

The `nats_url` parameter is plumbed through `create_app()` but not yet wired.
Implementation plan:

1. Create `MetricsCollector` class that subscribes to `heddle.results.*`.
2. On each result, compute window aggregates and call
   `WorkshopDB.save_worker_metric()`.
3. Initialize in `create_app()` when `nats_url` is not None.
4. Add a `/metrics` page with time-series charts (latency, throughput,
   error rate) per worker.

### Customizing the LLM judge

The `llm_judge` scoring method is built in. To customize:

1. Pass a custom `judge_prompt` to `EvalRunner.run_suite()` or set it in the
   Workshop eval form. The default prompt (`DEFAULT_JUDGE_PROMPT`) evaluates
   correctness, completeness, and format compliance.
2. The judge backend is selected automatically by the Workshop (prefers
   `standard` tier, falls back to first available). Programmatically, pass
   any `LLMBackend` instance as `judge_backend`.
3. Judge results (score, reasoning, per-criteria scores, token usage) are
   stored in `eval_results.score_details` JSON column.

### Extending the frontend

The Workshop uses **Pico CSS v2** for classless styling with extensive custom
CSS in `workshop.css` for:

- **Dark/light mode** — auto-detects `prefers-color-scheme`, with a manual
  toggle button that persists to `localStorage`
- **Responsive layout** — tables scroll horizontally on mobile, grids stack
  vertically below 576px, nav compresses
- **Accessibility** — skip-to-content link, `focus-visible` outlines,
  `prefers-reduced-motion` disables animations, `prefers-contrast: more`
  adds thicker borders, `aria-live` regions for HTMX results, proper
  ARIA landmarks and labels throughout
- **Print stylesheet** — hides nav/buttons/forms, expands all `<details>`

For richer interactivity (e.g., drag-and-drop pipeline editor, live charts):

1. Keep the HTMX pattern for server-driven updates.
2. For client-side-only widgets, add `<script>` blocks in specific templates.
3. Avoid introducing a build step — the Workshop should remain zero-build.

---

## Testing

Workshop tests are in `tests/`:

| File | What it tests |
|------|--------------|
| `test_workshop_runner.py` | `WorkerTestRunner` with mock backends |
| `test_workshop_db.py` | `WorkshopDB` schema, CRUD, dedup, comparison |
| `test_workshop_eval.py` | `EvalRunner` scoring (field_match, exact_match, llm_judge), concurrency, DB persistence |
| `test_workshop_config.py` | `ConfigManager` CRUD, validation, cloning |
| `test_workshop_pipeline_editor.py` | `PipelineEditor` insert/remove/swap/branch/validate |
| `test_app_manifest.py` | `AppManifest` validation, loading, error cases |
| `test_app_manager.py` | `AppManager` ZIP deploy, list, remove, reload notification |
| `test_workshop_app.py` | Workshop HTTP routes (baselines, dead-letter replay, basic routes) |
| `test_workshop_rag.py` | RAGManager (channels, search, registry loading) + RAG HTTP routes |
| `test_config_impact.py` | Config impact analysis (worker→pipeline reverse mapping) |

All tests use in-memory DuckDB (`:memory:`) and mock LLM backends. No
infrastructure needed.

```bash
# Run workshop tests only
uv run pytest tests/test_workshop_*.py -v

# Run all tests
uv run pytest tests/ -v -m "not integration"
```

---

*For general Heddle architecture, see [Architecture](ARCHITECTURE.md).
For building workers and pipelines, see [Building Workflows](building-workflows.md).*
