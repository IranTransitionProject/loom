"""
Workshop web application — FastAPI + HTMX + Jinja2.

Entry point: ``create_app()`` returns a configured FastAPI application.
Start via CLI: ``heddle workshop --port 8080``
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import structlog
from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from heddle.bus.memory import InMemoryBus
from heddle.router.dead_letter import DeadLetterConsumer

if TYPE_CHECKING:
    from heddle.bus.base import MessageBus
from heddle.worker.backends import build_backends_from_env
from heddle.workshop.app_manager import AppManager
from heddle.workshop.config_impact import get_impact
from heddle.workshop.config_manager import ConfigManager
from heddle.workshop.db import WorkshopDB
from heddle.workshop.eval_runner import EvalRunner
from heddle.workshop.pipeline_editor import PipelineEditor
from heddle.workshop.rag_manager import RAGManager
from heddle.workshop.security import (
    TOKEN_COOKIE_NAME,
    UploadTooLargeError,
    WorkshopAuth,
    enforce_same_origin,
    make_token_auth_middleware,
    read_upload_with_size_cap,
)
from heddle.workshop.test_runner import WorkerTestRunner

logger = structlog.get_logger()

# Paths relative to this file
_THIS_DIR = Path(__file__).parent
_TEMPLATES_DIR = _THIS_DIR / "templates"
_STATIC_DIR = _THIS_DIR / "static"


def _detect_available_backends() -> list[dict[str, str]]:
    """Detect available LLM backends from environment variables.

    Returns a list of dicts with 'name' and 'label' keys for display as
    badges on the test bench page.

    TODO(local-runtime-registry): the local-runtime branches below
    (LM Studio + Ollama) should iterate the planned ``LocalRuntime``
    registry once it lands.  See
    ``heddle.worker.backends._select_local_backend`` for the design.
    """
    available = []
    if os.getenv("ANTHROPIC_API_KEY"):
        available.append({"name": "anthropic", "label": "Anthropic (Claude)"})
    if os.getenv("LM_STUDIO_URL"):
        available.append({"name": "lmstudio", "label": "LM Studio (local)"})
    if os.getenv("OLLAMA_URL"):
        available.append({"name": "ollama", "label": "Ollama (local)"})
    if os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_BASE_URL"):
        available.append({"name": "openai", "label": "OpenAI-compatible"})
    return available


def _build_extra_config_dirs(app_mgr: AppManager) -> list[Path]:
    """Build the list of config dirs from all deployed apps."""
    dirs = []
    for manifest in app_mgr.list_apps():
        configs_dir = app_mgr.get_app_configs_dir(manifest.name)
        if configs_dir.exists():
            dirs.append(configs_dir)
    return dirs


async def _drain_dead_letter(consumer: DeadLetterConsumer, bus: MessageBus) -> None:
    """Subscribe to ``heddle.tasks.dead_letter`` and feed the consumer.

    Mirrors :func:`heddle.mcp.server._drain_dead_letter`: runs as a
    background task for the lifetime of the Workshop process so the
    Dead Letters UI sees live NATS traffic.  Cancelled by the
    lifespan on shutdown.
    """
    sub = await bus.subscribe("heddle.tasks.dead_letter")
    logger.info("workshop.dead_letter_subscribed")
    try:
        async for data in sub:
            await consumer.handle_message(data)
    except asyncio.CancelledError:
        raise
    finally:
        with contextlib.suppress(Exception):
            await sub.unsubscribe()


def create_app(  # noqa: PLR0915
    configs_dir: str = "configs/",
    db_path: str = "~/.heddle/workshop.duckdb",
    nats_url: str | None = None,
    apps_dir: str = "~/.heddle/apps",
    rag_db_path: str | None = None,
    rag_store_class: str | None = None,
    rag_channel_registry: str | None = None,
) -> FastAPI:
    """Create the Workshop FastAPI application.

    Args:
        configs_dir: Root directory containing ``workers/`` and ``orchestrators/``.
        db_path: DuckDB database path (``~`` is expanded).
        nats_url: Optional NATS URL.  When provided and reachable, the
            Workshop subscribes to ``heddle.tasks.dead_letter`` for the
            Dead Letters UI and publishes ``heddle.control.reload`` on
            app deploy/remove so running actors pick up the new
            configs.  When ``None`` (or NATS is unreachable), both
            surfaces silently fall back to a process-local
            ``InMemoryBus`` — the UI works but nothing crosses process
            boundaries.
        apps_dir: Root directory for deployed app bundles.
        rag_db_path: Optional vector store path for the RAG dashboard.
        rag_store_class: Optional dotted path to a VectorStore subclass.
        rag_channel_registry: Optional path to a channel registry YAML file.
    """
    # mDNS service discovery (optional — only if zeroconf is installed)
    _mdns_advertiser = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal _mdns_advertiser
        try:
            from heddle.discovery.mdns import HeddleServiceAdvertiser

            _mdns_advertiser = HeddleServiceAdvertiser()
            await _mdns_advertiser.start()
            _mdns_advertiser.register_workshop(port=8080)
            logger.info("workshop.mdns_enabled")
        except ImportError:
            logger.info("workshop.mdns_disabled", hint="Install heddle[mdns] for LAN discovery")

        # NATS wiring — deferred to lifespan because connect() is async
        # and we want a graceful fallback (rather than refusing to
        # start) if the bus is unreachable.  When connected, both the
        # dead-letter consumer (read path) and the AppManager (write
        # path for ``heddle.control.reload``) point at the same bus.
        nats_bus: MessageBus | None = None
        drain_task: asyncio.Task[None] | None = None
        if nats_url is not None:
            from heddle.bus.nats_adapter import NATSBus

            candidate = NATSBus(nats_url)
            try:
                await candidate.connect()
            except Exception as exc:
                logger.warning(
                    "workshop.nats.unavailable",
                    url=nats_url,
                    error=str(exc),
                    error_type=type(exc).__name__,
                    hint="Dead Letters UI and reload broadcast will use in-memory fallback",
                )
            else:
                nats_bus = candidate
                dead_letter_consumer.set_bus(nats_bus)
                app_mgr.set_bus(nats_bus)
                drain_task = asyncio.create_task(_drain_dead_letter(dead_letter_consumer, nats_bus))
                logger.info("workshop.nats.connected", url=nats_url)
        app.state.nats_bus = nats_bus  # type: ignore[attr-defined]

        try:
            yield
        finally:
            if drain_task is not None:
                drain_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await drain_task
            if nats_bus is not None:
                with contextlib.suppress(Exception):
                    await nats_bus.close()
            if _mdns_advertiser is not None:
                await _mdns_advertiser.stop()
            # Release httpx clients held by LLM backends.  The names below
            # are bound by the time lifespan's teardown runs (FastAPI calls
            # the lifespan generator only after create_app() returns).
            try:
                await test_runner.aclose()
            except Exception as exc:
                logger.warning("workshop.backends_close_failed", error=str(exc))

    app = FastAPI(title="Heddle Workshop", docs_url=None, redoc_url=None, lifespan=lifespan)

    # Same-origin enforcement on mutating requests.  The workshop is a
    # local developer dashboard, but any browser tab the developer has
    # open can issue cross-origin POSTs to ``http://localhost:8080``
    # while it's running, mutating config or wiping queues.  The
    # middleware rejects requests whose ``Origin`` (or ``Referer``
    # fallback) does not match the request's own host.  Browsers
    # always send ``Origin`` on cross-origin POSTs and the value
    # cannot be forged from page JS, so this is the standard CSRF
    # defence.  Non-browser clients (curl, Heddle CLI) send neither
    # header and pass through unchanged.
    app.middleware("http")(enforce_same_origin)

    # Optional token auth.  Reads ``HEDDLE_WORKSHOP_TOKEN`` once at
    # app construction; if the env var is set, mutating requests must
    # carry the token via ``Authorization: Bearer <token>`` header or
    # the ``heddle_workshop_token`` cookie.  No-op if the env var is
    # not set, preserving the safe-by-default loopback behaviour.
    # FastAPI runs middlewares in reverse-registration order, so this
    # outer layer rejects unauthorised requests before the same-origin
    # check runs — token rejection happens regardless of Origin, which
    # matches the operator's mental model ("auth blocks me first").
    auth = WorkshopAuth.from_env()
    app.state.auth = auth  # type: ignore[attr-defined]
    app.middleware("http")(make_token_auth_middleware(auth))

    # Static files
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Templates
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    # Initialize components
    db = WorkshopDB(db_path)
    backends = build_backends_from_env()
    test_runner = WorkerTestRunner(backends)
    eval_runner = EvalRunner(test_runner, db)
    app_mgr = AppManager(apps_dir=apps_dir)
    config_mgr = ConfigManager(configs_dir, db, extra_config_dirs=_build_extra_config_dirs(app_mgr))

    # Dead-letter consumer — always available for manual entry storage;
    # NATS subscription is handled externally if needed.
    dead_letter_consumer = DeadLetterConsumer(bus=InMemoryBus())
    app.state.dead_letter_consumer = dead_letter_consumer  # type: ignore[attr-defined]
    app.state.db = db  # type: ignore[attr-defined]

    # RAG manager — optional vector store + channel registry for RAG dashboard
    rag_store = None
    if rag_db_path:
        try:
            if rag_store_class:
                import importlib

                mod_path, cls_name = rag_store_class.rsplit(".", 1)
                store_cls = getattr(importlib.import_module(mod_path), cls_name)
            else:
                from heddle.contrib.rag.vectorstore.duckdb_store import DuckDBVectorStore

                store_cls = DuckDBVectorStore
            rag_store = store_cls(db_path=rag_db_path).initialize()
        except Exception as exc:
            logger.warning("workshop.rag_store_init_failed", error=str(exc))

    rag_mgr = RAGManager(
        store=rag_store,
        channel_registry_path=rag_channel_registry,
    )

    logger.info(
        "workshop.initialized",
        configs_dir=configs_dir,
        db_path=db_path,
        apps_dir=apps_dir,
        backends=list(backends.keys()),
        deployed_apps=len(app_mgr.list_apps()),
        rag_store=rag_db_path or "none",
        rag_channels=rag_mgr.channel_count(),
    )

    # Multi-worker uvicorn deployment is unsupported: deploy routes
    # mutate ``ConfigManager.extra_config_dirs`` and swap the
    # dead-letter bus in-place without locks, on the assumption that
    # route handlers serialise under a single FastAPI/Uvicorn worker.
    # If an operator sets WEB_CONCURRENCY > 1 (or runs uvicorn with
    # --workers N), surface the misconfiguration loudly so they can
    # fix it before state diverges between replicas.
    _web_concurrency = os.environ.get("WEB_CONCURRENCY", "1")
    try:
        _wc = int(_web_concurrency)
    except ValueError:
        _wc = 1
    if _wc > 1:
        logger.warning(
            "workshop.multi_worker_unsupported",
            web_concurrency=_wc,
            hint=(
                "Workshop assumes a single-worker process; deploy routes "
                "mutate process-local state without locks.  Set "
                "WEB_CONCURRENCY=1 or run uvicorn without --workers."
            ),
        )

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    @app.get("/", response_class=RedirectResponse)
    async def root():
        return RedirectResponse(url="/workers")

    @app.get("/health")
    async def health():
        return {"status": "ok", "backends": list(backends.keys())}

    def _set_login_cookie(response: RedirectResponse) -> RedirectResponse:
        # Cookie attributes: HttpOnly so page JS can't exfiltrate;
        # SameSite=Strict so a cross-site redirect can't initiate a
        # POST that pulls the cookie; Path=/ so it works on every
        # mutating route.  ``secure`` is left False because the
        # default bind is plain HTTP loopback — operators terminating
        # TLS in front should set it via reverse proxy.
        response.set_cookie(
            key=TOKEN_COOKIE_NAME,
            value=auth.token or "",
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.get("/login", response_class=HTMLResponse)
    async def login_form():
        """Render the token bootstrap form (or 404 when auth is disabled).

        The form POSTs the token in the request body so it never lands
        in uvicorn access logs.  Returns 404 when auth is disabled so
        this endpoint does not advertise its existence on open
        deployments.
        """
        if not auth.enabled:
            return JSONResponse({"error": "Not Found"}, status_code=404)
        # Inline form — deliberately minimal so the page renders
        # before any styling loads and the operator can paste the
        # token from a password manager without distraction.  No nav
        # chrome: the operator has no cookie yet, so the rest of the
        # workshop is gated anyway.
        return HTMLResponse(
            """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Heddle Workshop — Login</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css">
</head>
<body>
<main class="container" style="max-width: 28rem;">
<hgroup>
  <h1>Heddle Workshop</h1>
  <p>Paste your <code>HEDDLE_WORKSHOP_TOKEN</code> to set a session cookie.</p>
</hgroup>
<form action="/login" method="post" autocomplete="off">
  <label>Token
    <input type="password" name="token" required autofocus>
  </label>
  <button type="submit">Sign in</button>
</form>
</main>
</body>
</html>"""
        )

    @app.post("/login")
    async def login_submit(request: Request):
        """Validate the token and set the session cookie.

        Token comes from the POST form body so it never appears in
        access logs.  Returns 404 when auth is disabled, 401 on
        mismatch, and 303 to ``/`` on success (which rewrites to
        ``/workers``).
        """
        if not auth.enabled:
            return JSONResponse({"error": "Not Found"}, status_code=404)
        form = await request.form()
        provided_raw = form.get("token", "")
        provided = provided_raw if isinstance(provided_raw, str) else ""
        if not provided or not auth.request_token_matches_value(provided):
            return JSONResponse({"error": "Invalid token"}, status_code=401)
        return _set_login_cookie(RedirectResponse(url="/", status_code=303))

    # --- Workers ---

    @app.get("/workers", response_class=HTMLResponse)
    async def workers_list(request: Request):
        workers = config_mgr.list_workers()
        return templates.TemplateResponse(
            request,
            "workers/list.html",
            {
                "workers": workers,
            },
        )

    @app.get("/workers/{name}", response_class=HTMLResponse)
    async def worker_detail(request: Request, name: str):
        try:
            config = config_mgr.get_worker(name)
            yaml_content = config_mgr.get_worker_yaml(name)
        except FileNotFoundError:
            return HTMLResponse("Worker not found", status_code=404)
        versions = config_mgr.get_worker_version_history(name)
        return templates.TemplateResponse(
            request,
            "workers/detail.html",
            {
                "config": config,
                "yaml_content": yaml_content,
                "name": name,
                "versions": versions,
            },
        )

    @app.post("/workers/{name}/validate", response_class=HTMLResponse)
    async def worker_validate(request: Request, name: str):  # noqa: ARG001
        """HTMX endpoint: validate YAML content and return inline errors."""
        form = await request.form()
        import yaml

        errors = []
        try:
            config = yaml.safe_load(form["yaml_content"])
            if config is None:
                errors.append("YAML content is empty")
            elif not isinstance(config, dict):
                errors.append("YAML must be a mapping (key-value pairs)")
            else:
                # Run config validation without saving
                from heddle.core.config import validate_worker_config

                validation_errors = validate_worker_config(config)
                errors.extend(validation_errors)
        except yaml.YAMLError as e:
            errors.append(f"Invalid YAML syntax: {e}")

        return templates.TemplateResponse(
            request,
            "partials/validation_errors.html",
            {"errors": errors},
        )

    @app.get("/workers/{name}/impact", response_class=JSONResponse)
    async def worker_impact(name: str):
        """Return config impact analysis for a worker (JSON API)."""
        return JSONResponse(get_impact(name, config_mgr))

    @app.get("/workers/{name}/impact-panel", response_class=HTMLResponse)
    async def worker_impact_panel(request: Request, name: str):
        """HTMX endpoint: render impact analysis as an HTML partial."""
        impact = get_impact(name, config_mgr)
        return templates.TemplateResponse(
            request,
            "partials/impact_panel.html",
            {"impact": impact},
        )

    @app.post("/workers/{name}", response_class=HTMLResponse)
    async def worker_save(request: Request, name: str):
        form = await request.form()
        import yaml

        try:
            config = yaml.safe_load(form["yaml_content"])
        except Exception as e:
            return HTMLResponse(f"Invalid YAML: {e}", status_code=400)
        errors = config_mgr.save_worker(name, config, description=form.get("description"))
        if errors:
            return HTMLResponse(f"Validation errors: {'; '.join(errors)}", status_code=400)
        return RedirectResponse(url=f"/workers/{name}", status_code=303)

    @app.post("/workers/{name}/clone", response_class=RedirectResponse)
    async def worker_clone(request: Request, name: str):
        form = await request.form()
        new_name = form["new_name"]
        errors = config_mgr.clone_worker(name, new_name)
        if errors:
            return HTMLResponse(f"Clone failed: {'; '.join(errors)}", status_code=400)
        return RedirectResponse(url=f"/workers/{new_name}", status_code=303)

    # --- Test Bench ---

    @app.get("/workers/{name}/test", response_class=HTMLResponse)
    async def worker_test(request: Request, name: str):
        try:
            config = config_mgr.get_worker(name)
        except FileNotFoundError:
            return HTMLResponse("Worker not found", status_code=404)
        return templates.TemplateResponse(
            request,
            "workers/test.html",
            {
                "config": config,
                "name": name,
                "available_tiers": list(backends.keys()),
                "available_backends": _detect_available_backends(),
            },
        )

    @app.post("/workers/{name}/test/run", response_class=HTMLResponse)
    async def worker_test_run(request: Request, name: str):
        form = await request.form()
        try:
            config = config_mgr.get_worker(name)
            payload = json.loads(form["payload"])
            tier = form.get("tier") or None
        except FileNotFoundError:
            return HTMLResponse("Worker not found", status_code=404)
        except json.JSONDecodeError as e:
            return templates.TemplateResponse(
                request,
                "partials/test_result.html",
                {
                    "error": f"Invalid JSON payload: {e}",
                },
            )

        result = await test_runner.run(config, payload, tier=tier)
        return templates.TemplateResponse(
            request,
            "partials/test_result.html",
            {
                "result": result,
            },
        )

    # --- Eval ---

    @app.get("/workers/{name}/eval", response_class=HTMLResponse)
    async def worker_eval(request: Request, name: str):
        try:
            config = config_mgr.get_worker(name)
        except FileNotFoundError:
            return HTMLResponse("Worker not found", status_code=404)
        runs = db.get_eval_runs(name)
        return templates.TemplateResponse(
            request,
            "workers/eval.html",
            {
                "config": config,
                "name": name,
                "runs": runs,
                "available_tiers": list(backends.keys()),
            },
        )

    @app.post("/workers/{name}/eval/run", response_class=HTMLResponse)
    async def worker_eval_run(request: Request, name: str):
        form = await request.form()
        try:
            config = config_mgr.get_worker(name)
            import yaml

            suite = yaml.safe_load(form["test_suite"])
            if not isinstance(suite, list):
                raise ValueError("Test suite must be a YAML list")
            tier = form.get("tier") or None
            scoring = form.get("scoring", "field_match")
        except Exception as e:
            return HTMLResponse(f"Error: {e}", status_code=400)

        # For LLM judge, pick a backend (prefer standard, fall back to first available)
        judge_backend = None
        if scoring == "llm_judge":
            judge_backend = backends.get("standard") or next(iter(backends.values()), None)
            if not judge_backend:
                return HTMLResponse("No LLM backend available for judge scoring", status_code=400)

        run_id = await eval_runner.run_suite(
            config, suite, tier=tier, scoring=scoring, judge_backend=judge_backend
        )
        return RedirectResponse(url=f"/workers/{name}/eval/{run_id}", status_code=303)

    @app.get("/workers/{name}/eval/{run_id}", response_class=HTMLResponse)
    async def worker_eval_detail(request: Request, name: str, run_id: str):
        runs = db.get_eval_runs(name)
        run = next((r for r in runs if r["id"] == run_id), None)
        if not run:
            return HTMLResponse("Eval run not found", status_code=404)
        results = db.get_eval_results(run_id)
        baseline = db.get_baseline(name)
        baseline_comparison = None
        if baseline and baseline["run_id"] != run_id:
            baseline_comparison = db.compare_against_baseline(name, run_id)
        return templates.TemplateResponse(
            request,
            "workers/eval_detail.html",
            {
                "name": name,
                "run": run,
                "results": results,
                "baseline": baseline,
                "baseline_comparison": baseline_comparison,
            },
        )

    @app.post("/workers/{name}/eval/{run_id}/promote-baseline", response_class=RedirectResponse)
    async def worker_promote_baseline(request: Request, name: str, run_id: str):
        form = await request.form()
        description = form.get("description", "")
        db.promote_baseline(name, run_id, description=description or None)
        return RedirectResponse(url=f"/workers/{name}/eval/{run_id}", status_code=303)

    @app.post("/workers/{name}/eval/remove-baseline", response_class=RedirectResponse)
    async def worker_remove_baseline(name: str):
        db.remove_baseline(name)
        return RedirectResponse(url=f"/workers/{name}/eval", status_code=303)

    # --- Pipelines ---

    @app.get("/pipelines", response_class=HTMLResponse)
    async def pipelines_list(request: Request):
        pipelines = config_mgr.list_pipelines()
        return templates.TemplateResponse(
            request,
            "pipelines/list.html",
            {
                "pipelines": pipelines,
            },
        )

    @app.get("/pipelines/{name}", response_class=HTMLResponse)
    async def pipeline_detail(request: Request, name: str):
        try:
            config = config_mgr.get_pipeline(name)
        except FileNotFoundError:
            return HTMLResponse("Pipeline not found", status_code=404)
        graph = PipelineEditor.get_dependency_graph(config)
        workers = config_mgr.list_workers()
        return templates.TemplateResponse(
            request,
            "pipelines/editor.html",
            {
                "config": config,
                "name": name,
                "graph": graph,
                "workers": workers,
            },
        )

    @app.post("/pipelines/{name}/stage", response_class=HTMLResponse)
    async def pipeline_stage_edit(request: Request, name: str):
        form = await request.form()
        action = form["action"]
        try:
            config = config_mgr.get_pipeline(name)

            if action == "insert":
                import yaml

                stage_def = yaml.safe_load(form["stage_yaml"])
                after = form.get("after_stage") or None
                config = PipelineEditor.insert_stage(config, stage_def, after)
            elif action == "remove":
                config = PipelineEditor.remove_stage(config, form["stage_name"])
            elif action == "swap":
                config = PipelineEditor.swap_worker(
                    config,
                    form["stage_name"],
                    form["new_worker_type"],
                    form.get("new_tier") or None,
                )
            elif action == "branch":
                import yaml

                stage_def = yaml.safe_load(form["stage_yaml"])
                config = PipelineEditor.add_parallel_branch(config, stage_def)

            errors = config_mgr.save_pipeline(name, config)
            if errors:
                return HTMLResponse(f"Validation errors: {'; '.join(errors)}", status_code=400)

        except (ValueError, FileNotFoundError) as e:
            return HTMLResponse(f"Error: {e}", status_code=400)

        return RedirectResponse(url=f"/pipelines/{name}", status_code=303)

    @app.get("/pipelines/{name}/graph", response_class=JSONResponse)
    async def pipeline_graph(name: str):
        try:
            config = config_mgr.get_pipeline(name)
        except FileNotFoundError:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return PipelineEditor.get_dependency_graph(config)

    # --- Apps ---

    @app.get("/apps", response_class=HTMLResponse)
    async def apps_list(request: Request):
        apps = app_mgr.list_apps()
        error = request.query_params.get("error")
        return templates.TemplateResponse(
            request,
            "apps/list.html",
            {"apps": apps, "error": error},
        )

    @app.get("/apps/{name}", response_class=HTMLResponse)
    async def app_detail(request: Request, name: str):
        try:
            manifest = app_mgr.get_app(name)
        except (FileNotFoundError, ValueError):
            return HTMLResponse("App not found", status_code=404)
        return templates.TemplateResponse(
            request,
            "apps/detail.html",
            {"manifest": manifest},
        )

    def _forbidden_names_excluding_self(manifest_name: str) -> dict[str, set[str]]:
        """Build the deploy-collision forbidden set, excluding this app's own stems.

        ``existing_config_stems`` covers base + every deployed app; the
        app being (re)deployed should not collide with its own
        previously-deployed stems, so subtract them.  Used by both the
        direct deploy path and the two-step preview/confirm path.
        """
        forbidden = config_mgr.existing_config_stems()
        existing_app_dir = app_mgr.get_app_configs_dir(manifest_name)
        if not existing_app_dir.exists():
            return forbidden
        own_workers = (
            {p.stem for p in (existing_app_dir / "workers").glob("*.yaml")}
            if (existing_app_dir / "workers").exists()
            else set()
        )
        own_pipelines = (
            {p.stem for p in (existing_app_dir / "orchestrators").glob("*.yaml")}
            if (existing_app_dir / "orchestrators").exists()
            else set()
        )
        return {
            "workers": forbidden["workers"] - own_workers,
            "pipelines": forbidden["pipelines"] - own_pipelines,
        }

    async def _deploy_from_path(zip_path: Path, manifest_name: str):
        """Run the actual deploy + reload + redirect dance.

        Used by both the auto-approve direct path and the
        ``/apps/deploy/confirm/{token}`` path.  Returns the FastAPI
        response (RedirectResponse on success or AppDeployError redirect).
        """
        from heddle.workshop.app_manager import AppDeployError

        try:
            manifest = app_mgr.deploy_app(
                zip_path,
                forbidden_config_names=_forbidden_names_excluding_self(manifest_name),
            )
        except AppDeployError as e:
            # urlencode the message — error strings can contain ``&``
            # (would inject extra query params) or ``#`` (would create
            # a fragment), corrupting the redirect target.
            qs = urlencode({"error": str(e)})
            return RedirectResponse(url=f"/apps?{qs}", status_code=303)

        # Refresh ConfigManager's extra config dirs
        config_mgr.extra_config_dirs = _build_extra_config_dirs(app_mgr)
        # Notify running actors to reload
        await app_mgr.notify_reload()
        return RedirectResponse(url=f"/apps/{manifest.name}", status_code=303)

    @app.post("/apps/deploy")
    async def app_deploy(request: Request):
        form = await request.form()
        zip_file: UploadFile = form["zip_file"]
        # ``auto_approve=1`` (query param) skips the preview page so CI /
        # scripted deploys can keep their one-shot flow.  Browser deploys
        # default to the two-step preview.
        auto_approve = request.query_params.get("auto_approve") == "1"

        if not zip_file.filename or not zip_file.filename.endswith(".zip"):
            return RedirectResponse(url="/apps?error=File+must+be+a+.zip+archive", status_code=303)

        # Stream the upload to a temp file with a size cap.  Earlier
        # shape did ``await zip_file.read()`` which materialises the
        # whole upload in memory and OOMs the worker on multi-GB
        # POSTs.  ``read_upload_with_size_cap`` streams in 1 MiB
        # chunks and aborts as soon as the cap is exceeded.
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        cleanup_tmp = True
        try:
            try:
                await read_upload_with_size_cap(zip_file, tmp_path)
            except UploadTooLargeError as exc:
                logger.warning(
                    "workshop.upload_rejected_too_large",
                    filename=zip_file.filename,
                    max_bytes=exc.max_bytes,
                )
                return RedirectResponse(
                    url=f"/apps?error=Upload+exceeds+{exc.max_bytes}+bytes",
                    status_code=303,
                )

            from heddle.workshop.app_manager import AppDeployError

            try:
                pending_manifest = app_mgr.peek_manifest(tmp_path)
            except AppDeployError as e:
                qs = urlencode({"error": str(e)})
                return RedirectResponse(url=f"/apps?{qs}", status_code=303)

            if auto_approve:
                return await _deploy_from_path(tmp_path, pending_manifest.name)

            # Two-step path: build the capability summary, stage the ZIP,
            # render the preview.  ``stage_zip`` MOVES the temp file into
            # the staging dir, so suppress the finally-clause cleanup.
            import zipfile as _zf_mod

            from heddle.workshop.app_capabilities import summarize_capabilities

            with _zf_mod.ZipFile(tmp_path, "r") as zf:
                summary = summarize_capabilities(zf, pending_manifest)

            token = app_mgr.stage_zip(tmp_path)
            cleanup_tmp = False
            existing_app_dir = app_mgr.get_app_configs_dir(pending_manifest.name)
            return templates.TemplateResponse(
                request,
                "apps/preview.html",
                {
                    "manifest": pending_manifest,
                    "summary": summary,
                    "token": token,
                    "is_redeploy": existing_app_dir.exists(),
                },
            )
        finally:
            if cleanup_tmp:
                tmp_path.unlink(missing_ok=True)

    @app.post("/apps/deploy/confirm/{token}")
    async def app_deploy_confirm(token: str):
        """Finalize a previously-staged deploy.

        Looks up the staged ZIP by token, runs ``deploy_app`` (which
        re-validates and re-checks collisions), and removes the staging
        dir on success.  On failure, the staging dir is also removed —
        the operator must re-upload to retry, ensuring stale state can't
        accumulate.
        """
        from heddle.workshop.app_manager import AppDeployError

        try:
            staged_zip = app_mgr.staged_zip_path(token)
        except AppDeployError as e:
            qs = urlencode({"error": str(e)})
            return RedirectResponse(url=f"/apps?{qs}", status_code=303)

        try:
            pending_manifest = app_mgr.peek_manifest(staged_zip)
            response = await _deploy_from_path(staged_zip, pending_manifest.name)
        finally:
            app_mgr.discard_staged(token)
        return response

    @app.post("/apps/deploy/cancel/{token}")
    async def app_deploy_cancel(token: str):
        """Discard a staged deploy without committing it."""
        app_mgr.discard_staged(token)
        return RedirectResponse(url="/apps", status_code=303)

    @app.post("/apps/{name}/remove", response_class=RedirectResponse)
    async def app_remove(name: str):
        try:
            app_mgr.remove_app(name)
            config_mgr.extra_config_dirs = _build_extra_config_dirs(app_mgr)
            await app_mgr.notify_reload()
        except FileNotFoundError:
            pass
        return RedirectResponse(url="/apps", status_code=303)

    # --- RAG ---

    @app.get("/rag", response_class=HTMLResponse)
    async def rag_dashboard(request: Request):
        stats = rag_mgr.get_store_stats()
        faction_groups = rag_mgr.get_channels_by_faction()
        return templates.TemplateResponse(
            request,
            "rag/dashboard.html",
            {
                "stats": stats,
                "channel_count": rag_mgr.channel_count(),
                "verified_count": rag_mgr.verified_channel_count(),
                "faction_groups": faction_groups,
            },
        )

    @app.get("/rag/channels", response_class=HTMLResponse)
    async def rag_channels(request: Request):
        return templates.TemplateResponse(
            request,
            "rag/channels.html",
            {"channels": rag_mgr.channels},
        )

    @app.get("/rag/search", response_class=HTMLResponse)
    async def rag_search(request: Request):
        return templates.TemplateResponse(
            request,
            "rag/search.html",
            {},
        )

    @app.post("/rag/search/run", response_class=HTMLResponse)
    async def rag_search_run(request: Request):
        form = await request.form()
        query = form.get("query", "").strip()
        if not query:
            return templates.TemplateResponse(
                request,
                "partials/rag_search_result.html",
                {"results": [], "error": "Query is required."},
            )

        limit = int(form.get("limit", 10))
        min_score = float(form.get("min_score", 0.0))

        channel_ids_raw = form.get("channel_ids", "").strip()
        channel_ids = None
        if channel_ids_raw:
            try:
                channel_ids = [int(x.strip()) for x in channel_ids_raw.split(",") if x.strip()]
            except ValueError:
                return templates.TemplateResponse(
                    request,
                    "partials/rag_search_result.html",
                    {"results": [], "error": "Invalid channel IDs format."},
                )

        results = rag_mgr.search(query, limit=limit, min_score=min_score, channel_ids=channel_ids)
        return templates.TemplateResponse(
            request,
            "partials/rag_search_result.html",
            {"results": results, "error": None},
        )

    @app.get("/rag/store/stats", response_class=HTMLResponse)
    async def rag_store_stats(request: Request):
        stats = rag_mgr.get_store_stats()
        return templates.TemplateResponse(
            request,
            "partials/rag_stats.html",
            {"stats": stats},
        )

    # --- Dead Letters ---

    @app.get("/dead-letters", response_class=HTMLResponse)
    async def dead_letters_list(request: Request):
        entries = dead_letter_consumer.list_entries(limit=100)
        total = dead_letter_consumer.count()
        replay_log = dead_letter_consumer.replay_log(limit=50)
        replay_count = dead_letter_consumer.replay_count()
        return templates.TemplateResponse(
            request,
            "dead_letters.html",
            {
                "entries": entries,
                "total": total,
                "replay_log": replay_log,
                "replay_count": replay_count,
            },
        )

    @app.post("/dead-letters/{entry_id}/replay", response_class=RedirectResponse)
    async def dead_letter_replay(entry_id: str):
        # The path carries the entry UUID (stable across page reloads).
        # Earlier shape took a positional ``{index}`` and read the real
        # UUID from a hidden form field, so the path parameter was
        # ignored and clicking Replay on a list that had since shifted
        # could not target a specific entry — there was no entry whose
        # position matched the URL.  The bus comes from
        # ``dead_letter_consumer`` so replays publish to the same bus
        # the consumer was wired to in lifespan (NATSBus when
        # connected, InMemoryBus fallback otherwise).
        await dead_letter_consumer.replay(entry_id, dead_letter_consumer._bus)
        return RedirectResponse(url="/dead-letters", status_code=303)

    @app.post("/dead-letters/clear", response_class=RedirectResponse)
    async def dead_letters_clear():
        dead_letter_consumer.clear()
        return RedirectResponse(url="/dead-letters", status_code=303)

    return app
