"""
Test CLI commands (unit tests, no infrastructure).

Tests the Click CLI from heddle.cli.main using Click's CliRunner.
All async operations are mocked — no NATS or external services needed.

NOTE: Importing heddle.cli.main triggers a global structlog.configure() call.
We save and restore the structlog config to prevent pollution of other tests.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import click
import pytest
import structlog
from click.testing import CliRunner

# Save structlog config before importing cli (which reconfigures it globally).
_saved_structlog_config = structlog.get_config()

from heddle.cli.main import _load_processing_backend, cli  # noqa: E402

# Restore structlog config immediately after import.
structlog.configure(**_saved_structlog_config)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(path, content: str) -> str:
    """Write YAML content to a file and return the path as a string."""
    path.write_text(content)
    return str(path)


# ---------------------------------------------------------------------------
# _load_processing_backend — dotted path resolution
# ---------------------------------------------------------------------------


def test_load_backend_no_dot_raises():
    """A name without a dot is rejected with a ClickException."""
    with pytest.raises(click.ClickException, match="must be a fully qualified class path"):
        _load_processing_backend("NoDotName", {})


def test_load_backend_bad_module_raises():
    """An unimportable module path raises a ClickException."""
    with pytest.raises(click.ClickException, match="Cannot import backend module"):
        _load_processing_backend("totally.nonexistent.module.Backend", {})


def test_load_backend_bad_class_raises():
    """A valid module with a missing class raises a ClickException."""
    # os is a valid module but has no class called 'NoSuchClass'
    with pytest.raises(click.ClickException, match="Backend class 'NoSuchClass' not found"):
        _load_processing_backend("os.path.NoSuchClass", {})


def test_load_backend_valid_dotted_path():
    """A valid dotted path imports and instantiates the class."""
    # Use a known stdlib class as the backend — MagicMock is callable
    mock_class = MagicMock(return_value="instance")
    with patch("importlib.import_module") as mock_import:
        mock_module = MagicMock()
        mock_module.MyBackend = mock_class
        mock_import.return_value = mock_module

        result = _load_processing_backend("mypackage.backends.MyBackend", {})

    mock_import.assert_called_once_with("mypackage.backends")
    mock_class.assert_called_once_with()
    assert result == "instance"


def test_load_backend_passes_backend_config_as_kwargs():
    """backend_config from the worker config is passed as kwargs to the class."""
    mock_class = MagicMock(return_value="configured_instance")
    with patch("importlib.import_module") as mock_import:
        mock_module = MagicMock()
        mock_module.MyBackend = mock_class
        mock_import.return_value = mock_module

        config = {"backend_config": {"db_path": "/tmp/test.db", "timeout": 30}}
        result = _load_processing_backend("mypackage.backends.MyBackend", config)

    mock_class.assert_called_once_with(db_path="/tmp/test.db", timeout=30)
    assert result == "configured_instance"


def test_load_backend_no_backend_config_passes_empty():
    """Without backend_config in the worker config, empty kwargs are passed."""
    mock_class = MagicMock(return_value="default_instance")
    with patch("importlib.import_module") as mock_import:
        mock_module = MagicMock()
        mock_module.MyBackend = mock_class
        mock_import.return_value = mock_module

        _load_processing_backend("mypackage.backends.MyBackend", {})

    mock_class.assert_called_once_with()


# ---------------------------------------------------------------------------
# submit command — context parsing
# ---------------------------------------------------------------------------


def test_submit_parses_context_key_value(tmp_path):
    """submit with --context key=value parses into a context dict."""
    runner = CliRunner()

    with patch("heddle.cli.main.asyncio.run") as mock_run:
        result = runner.invoke(
            cli,
            [
                "submit",
                "Process document",
                "--nats-url",
                "nats://localhost:4222",
                "--context",
                "file_ref=test.pdf",
                "--context",
                "lang=en",
            ],
        )

    assert result.exit_code == 0
    mock_run.assert_called_once()


def test_submit_bad_context_no_equals():
    """submit with context missing '=' raises a ClickException."""
    runner = CliRunner()

    with patch("heddle.cli.main.asyncio.run"):
        result = runner.invoke(
            cli,
            [
                "submit",
                "Process document",
                "--context",
                "bad_context_no_equals",
            ],
        )

    assert result.exit_code != 0
    assert "key=value" in result.output


# ---------------------------------------------------------------------------
# processor command — missing processing_backend
# ---------------------------------------------------------------------------


def test_processor_missing_backend_raises(tmp_path):
    """processor command raises if config lacks processing_backend."""
    config_path = _write_yaml(
        tmp_path / "proc.yaml",
        "name: test_proc\ntier: local\nworker_kind: processor\n",
    )
    runner = CliRunner()

    with patch("heddle.cli.main.asyncio.run"):
        result = runner.invoke(
            cli,
            [
                "processor",
                "--skip-preflight",
                "--config",
                config_path,
                "--nats-url",
                "nats://localhost:4222",
            ],
        )

    assert result.exit_code != 0
    assert "processing_backend" in result.output


# ---------------------------------------------------------------------------
# help text smoke tests
# ---------------------------------------------------------------------------


def test_worker_help():
    """worker --help shows help text without errors."""
    runner = CliRunner()
    result = runner.invoke(cli, ["worker", "--help"])
    assert result.exit_code == 0
    assert "Start an LLM worker actor" in result.output


def test_router_help():
    """router --help shows help text without errors."""
    runner = CliRunner()
    result = runner.invoke(cli, ["router", "--help"])
    assert result.exit_code == 0
    assert "Start the deterministic task router" in result.output


def test_mcp_help():
    """mcp --help shows help text without errors."""
    runner = CliRunner()
    result = runner.invoke(cli, ["mcp", "--help"])
    assert result.exit_code == 0
    assert "Start an MCP server" in result.output


def test_pipeline_help():
    """pipeline --help shows help text without errors."""
    runner = CliRunner()
    result = runner.invoke(cli, ["pipeline", "--help"])
    assert result.exit_code == 0
    assert "Start a pipeline orchestrator" in result.output


def test_submit_help():
    """submit --help shows help text without errors."""
    runner = CliRunner()
    result = runner.invoke(cli, ["submit", "--help"])
    assert result.exit_code == 0
    assert "Submit a goal" in result.output


def test_orchestrator_help():
    """orchestrator --help shows help text without errors."""
    runner = CliRunner()
    result = runner.invoke(cli, ["orchestrator", "--help"])
    assert result.exit_code == 0
    assert "Start the dynamic LLM-based orchestrator" in result.output


def test_scheduler_help():
    """scheduler --help shows help text without errors."""
    runner = CliRunner()
    result = runner.invoke(cli, ["scheduler", "--help"])
    assert result.exit_code == 0
    assert "Start the time-driven scheduler" in result.output


def test_workshop_help():
    """workshop --help shows help text without errors."""
    runner = CliRunner()
    result = runner.invoke(cli, ["workshop", "--help"])
    assert result.exit_code == 0
    assert "Start the LLM Worker Workshop" in result.output


# ---------------------------------------------------------------------------
# worker command — full execution paths
# ---------------------------------------------------------------------------


def test_worker_loads_config_and_runs(tmp_path):
    """worker command reads YAML, builds backends, and starts the actor."""
    config_path = _write_yaml(
        tmp_path / "worker.yaml",
        "name: summarizer\ndefault_model_tier: local\nsystem_prompt: You are a summarizer.\n",
    )
    runner = CliRunner()

    with (
        patch.dict("os.environ", {"OLLAMA_URL": "http://localhost:11434"}, clear=False),
        patch("heddle.cli.main.asyncio.run") as mock_run,
    ):
        result = runner.invoke(
            cli,
            [
                "worker",
                "--skip-preflight",
                "--config",
                config_path,
                "--tier",
                "local",
                "--nats-url",
                "nats://localhost:4222",
            ],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    mock_run.assert_called_once()


def test_worker_tier_mismatch_warns(tmp_path):
    """worker logs warning when CLI --tier differs from config default_model_tier."""
    config_path = _write_yaml(
        tmp_path / "worker.yaml",
        "name: summarizer\ndefault_model_tier: local\nsystem_prompt: You are a summarizer.\n",
    )
    runner = CliRunner()

    with patch("heddle.cli.main.asyncio.run"):
        result = runner.invoke(
            cli,
            [
                "worker",
                "--skip-preflight",
                "--config",
                config_path,
                "--tier",
                "standard",
                "--nats-url",
                "nats://localhost:4222",
            ],
        )

    # Command still succeeds (warning doesn't block execution)
    assert result.exit_code == 0


def test_worker_no_backend_for_tier_warns(tmp_path):
    """worker warns when no backend is configured for the requested tier."""
    config_path = _write_yaml(
        tmp_path / "worker.yaml",
        "name: summarizer\nsystem_prompt: You are a summarizer.\n",
    )
    runner = CliRunner()

    # No env vars set, so no backends are configured
    with (
        patch("heddle.cli.main.asyncio.run"),
        patch.dict("os.environ", {}, clear=False),
    ):
        # Remove env vars that could configure backends
        import os

        old_ollama = os.environ.pop("OLLAMA_URL", None)
        old_anthropic = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            result = runner.invoke(
                cli,
                [
                    "worker",
                    "--skip-preflight",
                    "--config",
                    config_path,
                    "--tier",
                    "frontier",
                    "--nats-url",
                    "nats://localhost:4222",
                ],
            )
        finally:
            if old_ollama:
                os.environ["OLLAMA_URL"] = old_ollama
            if old_anthropic:
                os.environ["ANTHROPIC_API_KEY"] = old_anthropic

    assert result.exit_code == 0


def test_worker_with_anthropic_backend(tmp_path):
    """worker configures standard + frontier backends from ANTHROPIC_API_KEY."""
    config_path = _write_yaml(
        tmp_path / "worker.yaml",
        "name: summarizer\ndefault_model_tier: standard\nsystem_prompt: You are a summarizer.\n",
    )
    runner = CliRunner()

    import os

    old_ollama = os.environ.pop("OLLAMA_URL", None)
    old_anthropic = os.environ.get("ANTHROPIC_API_KEY")

    try:
        os.environ["ANTHROPIC_API_KEY"] = "test-key-123"
        with patch("heddle.cli.main.asyncio.run") as mock_run:
            result = runner.invoke(
                cli,
                [
                    "worker",
                    "--skip-preflight",
                    "--config",
                    config_path,
                    "--tier",
                    "standard",
                    "--nats-url",
                    "nats://localhost:4222",
                ],
            )
    finally:
        if old_ollama:
            os.environ["OLLAMA_URL"] = old_ollama
        if old_anthropic:
            os.environ["ANTHROPIC_API_KEY"] = old_anthropic
        elif "ANTHROPIC_API_KEY" in os.environ:
            del os.environ["ANTHROPIC_API_KEY"]

    assert result.exit_code == 0
    mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# processor command — full execution paths
# ---------------------------------------------------------------------------


def test_processor_loads_backend_and_runs(tmp_path):
    """processor command loads backend dynamically and starts the actor."""
    config_path = _write_yaml(
        tmp_path / "proc.yaml",
        "name: doc_extractor\nworker_kind: processor\nprocessing_backend: os.path.join\n",
    )
    runner = CliRunner()

    mock_backend = MagicMock()
    with (
        patch("heddle.cli.main._load_processing_backend", return_value=mock_backend),
        patch("heddle.cli.main.asyncio.run") as mock_run,
    ):
        result = runner.invoke(
            cli,
            [
                "processor",
                "--skip-preflight",
                "--config",
                config_path,
                "--nats-url",
                "nats://localhost:4222",
                "--tier",
                "local",
            ],
        )

    assert result.exit_code == 0
    mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# pipeline command
# ---------------------------------------------------------------------------


def test_pipeline_loads_config_and_runs(tmp_path):
    """pipeline command reads YAML and starts PipelineOrchestrator."""
    config_path = _write_yaml(
        tmp_path / "pipeline.yaml",
        "name: test_pipeline\npipeline_stages:\n  - name: stage1\n    worker_type: summarizer\n",
    )
    runner = CliRunner()

    with patch("heddle.cli.main.asyncio.run") as mock_run:
        result = runner.invoke(
            cli,
            [
                "pipeline",
                "--skip-preflight",
                "--config",
                config_path,
                "--nats-url",
                "nats://localhost:4222",
            ],
        )

    assert result.exit_code == 0
    mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# orchestrator command
# ---------------------------------------------------------------------------


def test_orchestrator_loads_config_and_runs(tmp_path):
    """orchestrator command reads YAML and starts OrchestratorActor."""
    config_path = _write_yaml(
        tmp_path / "orch.yaml",
        "name: test_orch\nsystem_prompt: You are helpful.\n",
    )
    runner = CliRunner()

    mock_actor = MagicMock()
    with (
        patch("heddle.cli.main.asyncio.run") as mock_run,
        patch("heddle.orchestrator.runner.OrchestratorActor", return_value=mock_actor),
    ):
        result = runner.invoke(
            cli,
            [
                "orchestrator",
                "--skip-preflight",
                "--config",
                config_path,
                "--nats-url",
                "nats://localhost:4222",
                "--redis-url",
                "",
            ],
        )

    assert result.exit_code == 0
    mock_run.assert_called_once()


def test_orchestrator_with_redis_import_error(tmp_path):
    """orchestrator continues without checkpointing if redis extras not installed."""
    config_path = _write_yaml(
        tmp_path / "orch.yaml",
        "name: test_orch\nsystem_prompt: You are helpful.\n",
    )
    runner = CliRunner()

    mock_actor = MagicMock()

    # Simulate ImportError for redis store by removing it from sys.modules
    import sys

    saved = sys.modules.pop("heddle.contrib.redis.store", "NOT_PRESENT")
    # Insert None to force ImportError on import
    sys.modules["heddle.contrib.redis.store"] = None

    try:
        with (
            patch("heddle.cli.main.asyncio.run") as mock_run,
            patch("heddle.orchestrator.runner.OrchestratorActor", return_value=mock_actor),
        ):
            result = runner.invoke(
                cli,
                [
                    "orchestrator",
                    "--skip-preflight",
                    "--config",
                    config_path,
                    "--nats-url",
                    "nats://localhost:4222",
                    "--redis-url",
                    "redis://localhost:6379",
                ],
            )
    finally:
        del sys.modules["heddle.contrib.redis.store"]
        if saved != "NOT_PRESENT":
            sys.modules["heddle.contrib.redis.store"] = saved

    assert result.exit_code == 0
    mock_run.assert_called_once()


def test_orchestrator_with_redis_store(tmp_path):
    """orchestrator creates RedisCheckpointStore when redis is available."""
    config_path = _write_yaml(
        tmp_path / "orch.yaml",
        "name: test_orch\nsystem_prompt: You are helpful.\nmax_concurrent_goals: 2\n",
    )
    runner = CliRunner()

    mock_store = MagicMock()
    mock_actor = MagicMock()
    with (
        patch("heddle.cli.main.asyncio.run") as mock_run,
        patch("heddle.contrib.redis.store.RedisCheckpointStore", return_value=mock_store),
        patch("heddle.orchestrator.runner.OrchestratorActor", return_value=mock_actor),
    ):
        result = runner.invoke(
            cli,
            [
                "orchestrator",
                "--skip-preflight",
                "--config",
                config_path,
                "--nats-url",
                "nats://localhost:4222",
                "--redis-url",
                "redis://localhost:6379",
            ],
        )

    assert result.exit_code == 0
    mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# scheduler command
# ---------------------------------------------------------------------------


def test_scheduler_loads_config_and_runs(tmp_path):
    """scheduler command reads YAML, validates, and starts SchedulerActor."""
    config_path = _write_yaml(
        tmp_path / "sched.yaml",
        "name: test_scheduler\nschedules:\n  - name: job1\n"
        "    interval_seconds: 60\n    dispatch_type: goal\n"
        "    goal: Check status\n",
    )
    runner = CliRunner()

    mock_actor = MagicMock()
    with (
        patch("heddle.cli.main.asyncio.run") as mock_run,
        patch("heddle.scheduler.config.validate_scheduler_config", return_value=[]),
        patch("heddle.scheduler.scheduler.SchedulerActor", return_value=mock_actor),
    ):
        result = runner.invoke(
            cli,
            [
                "scheduler",
                "--skip-preflight",
                "--config",
                config_path,
                "--nats-url",
                "nats://localhost:4222",
            ],
        )

    assert result.exit_code == 0
    mock_run.assert_called_once()


def test_scheduler_config_validation_errors(tmp_path):
    """scheduler command fails with ClickException when config has errors."""
    config_path = _write_yaml(
        tmp_path / "sched.yaml",
        "name: bad_scheduler\n",
    )
    runner = CliRunner()

    with patch(
        "heddle.scheduler.config.validate_scheduler_config",
        return_value=["Missing 'schedules' field", "Invalid cron expression"],
    ):
        result = runner.invoke(
            cli,
            [
                "scheduler",
                "--skip-preflight",
                "--config",
                config_path,
                "--nats-url",
                "nats://localhost:4222",
            ],
        )

    assert result.exit_code != 0
    assert "error(s)" in result.output


# ---------------------------------------------------------------------------
# router command
# ---------------------------------------------------------------------------


def test_router_loads_config_and_runs(tmp_path):
    """router command creates NATSBus + TaskRouter and runs."""
    runner = CliRunner()

    mock_router = MagicMock()
    mock_router.run = MagicMock(return_value=None)
    mock_router.process_messages = MagicMock(return_value=None)

    with (
        patch("heddle.cli.main.asyncio.run") as mock_run,
        patch("heddle.bus.nats_adapter.NATSBus"),
        patch("heddle.router.router.TaskRouter", return_value=mock_router),
    ):
        result = runner.invoke(
            cli,
            [
                "router",
                "--skip-preflight",
                "--config",
                "configs/router_rules.yaml",
                "--nats-url",
                "nats://localhost:4222",
            ],
        )

    assert result.exit_code == 0
    mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# submit command — async path
# ---------------------------------------------------------------------------


def test_submit_calls_asyncio_run(tmp_path):
    """submit command invokes asyncio.run with the _submit coroutine."""
    runner = CliRunner()

    with patch("heddle.cli.main.asyncio.run") as mock_run:
        result = runner.invoke(
            cli,
            [
                "submit",
                "Test goal",
                "--nats-url",
                "nats://localhost:4222",
            ],
        )

    assert result.exit_code == 0
    mock_run.assert_called_once()


def test_submit_with_multiple_context_pairs():
    """submit with multiple --context pairs all get parsed."""
    runner = CliRunner()

    with patch("heddle.cli.main.asyncio.run"):
        result = runner.invoke(
            cli,
            [
                "submit",
                "Multi context goal",
                "--nats-url",
                "nats://localhost:4222",
                "--context",
                "a=1",
                "--context",
                "b=2",
                "--context",
                "c=with=equals",
            ],
        )

    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# mcp command
# ---------------------------------------------------------------------------


def test_mcp_stdio_transport(tmp_path):
    """mcp command with stdio transport calls run_stdio."""
    config_path = _write_yaml(
        tmp_path / "mcp.yaml",
        "name: test_mcp\nnats_url: nats://localhost:4222\ntools:\n  workers: []\n",
    )
    runner = CliRunner()

    mock_server = MagicMock()
    mock_gateway = MagicMock()
    mock_gateway.tool_registry = {"tool1": MagicMock()}
    mock_gateway.resources = None

    with (
        patch("heddle.mcp.create_server", return_value=(mock_server, mock_gateway)),
        patch("heddle.mcp.run_stdio") as mock_run_stdio,
    ):
        result = runner.invoke(
            cli,
            [
                "mcp",
                "--skip-preflight",
                "--config",
                config_path,
                "--transport",
                "stdio",
            ],
        )

    assert result.exit_code == 0
    mock_run_stdio.assert_called_once_with(mock_server, mock_gateway)


def test_mcp_streamable_http_transport(tmp_path):
    """mcp command with streamable-http transport calls run_streamable_http."""
    config_path = _write_yaml(
        tmp_path / "mcp.yaml",
        "name: test_mcp\nnats_url: nats://localhost:4222\ntools:\n  workers: []\n",
    )
    runner = CliRunner()

    mock_server = MagicMock()
    mock_gateway = MagicMock()
    mock_gateway.tool_registry = {}
    mock_gateway.resources = MagicMock()  # resources enabled

    with (
        patch("heddle.mcp.create_server", return_value=(mock_server, mock_gateway)),
        patch("heddle.mcp.run_streamable_http") as mock_run_http,
    ):
        result = runner.invoke(
            cli,
            [
                "mcp",
                "--skip-preflight",
                "--config",
                config_path,
                "--transport",
                "streamable-http",
                "--host",
                "0.0.0.0",
                "--port",
                "9000",
            ],
        )

    assert result.exit_code == 0
    mock_run_http.assert_called_once_with(mock_server, mock_gateway, host="0.0.0.0", port=9000)


# ---------------------------------------------------------------------------
# workshop command
# ---------------------------------------------------------------------------


def test_workshop_starts_uvicorn(tmp_path):
    """workshop command creates the app and starts uvicorn."""
    runner = CliRunner()

    mock_app = MagicMock()
    with (
        patch("heddle.workshop.app.create_app", return_value=mock_app) as mock_create,
        patch("uvicorn.run") as mock_uvicorn,
    ):
        result = runner.invoke(
            cli,
            [
                "workshop",
                "--port",
                "9090",
                "--host",
                "0.0.0.0",
                "--configs-dir",
                "/tmp/configs",
                "--db-path",
                "/tmp/test.duckdb",
            ],
        )

    assert result.exit_code == 0
    mock_create.assert_called_once_with(
        configs_dir="/tmp/configs",
        db_path="/tmp/test.duckdb",
        nats_url=None,
        apps_dir="~/.heddle/apps",
    )
    mock_uvicorn.assert_called_once_with(mock_app, host="0.0.0.0", port=9090, log_level="info")


def test_workshop_with_nats_url(tmp_path):
    """workshop command passes nats_url to create_app when provided."""
    runner = CliRunner()

    mock_app = MagicMock()
    with (
        patch("heddle.workshop.app.create_app", return_value=mock_app) as mock_create,
        patch("uvicorn.run"),
    ):
        result = runner.invoke(
            cli,
            [
                "workshop",
                "--nats-url",
                "nats://localhost:4222",
            ],
        )

    assert result.exit_code == 0
    mock_create.assert_called_once_with(
        configs_dir="configs/",
        db_path="~/.heddle/workshop.duckdb",
        nats_url="nats://localhost:4222",
        apps_dir="~/.heddle/apps",
    )


# ---------------------------------------------------------------------------
# ui command
# ---------------------------------------------------------------------------


def test_ui_help():
    """ui --help shows help text without errors."""
    runner = CliRunner()
    result = runner.invoke(cli, ["ui", "--help"])
    assert result.exit_code == 0
    assert "terminal dashboard" in result.output.lower() or "dashboard" in result.output.lower()


def test_ui_import_error_when_tui_not_installed():
    """ui command exits with an error when TUI dependencies are missing."""
    runner = CliRunner()

    import sys

    # Force ImportError for the tui app module by blocking the import.
    saved = sys.modules.pop("heddle.tui.app", "NOT_PRESENT")
    sys.modules["heddle.tui.app"] = None  # type: ignore[assignment]

    try:
        result = runner.invoke(
            cli,
            ["ui", "--nats-url", "nats://localhost:4222"],
        )
    finally:
        del sys.modules["heddle.tui.app"]
        if saved != "NOT_PRESENT":
            sys.modules["heddle.tui.app"] = saved

    assert result.exit_code != 0
    assert "tui" in result.output.lower() or "tui" in (result.stderr or "").lower()


# ---------------------------------------------------------------------------
# mdns command
# ---------------------------------------------------------------------------


def test_mdns_help():
    """mdns --help shows help text without errors."""
    runner = CliRunner()
    result = runner.invoke(cli, ["mdns", "--help"])
    assert result.exit_code == 0
    assert "mDNS" in result.output or "mdns" in result.output.lower()


def test_mdns_import_error_when_zeroconf_not_installed():
    """mdns command raises ClickException when zeroconf/mDNS dependencies are missing."""
    runner = CliRunner()

    import sys

    saved = sys.modules.pop("heddle.discovery.mdns", "NOT_PRESENT")
    sys.modules["heddle.discovery.mdns"] = None  # type: ignore[assignment]

    try:
        result = runner.invoke(
            cli,
            ["mdns", "--workshop-port", "8080", "--nats-port", "4222"],
        )
    finally:
        del sys.modules["heddle.discovery.mdns"]
        if saved != "NOT_PRESENT":
            sys.modules["heddle.discovery.mdns"] = saved

    assert result.exit_code != 0
    assert "zeroconf" in result.output.lower() or "mdns" in result.output.lower()


# ---------------------------------------------------------------------------
# dead-letter monitor command
# ---------------------------------------------------------------------------


def test_dead_letter_monitor_help():
    """dead-letter monitor --help shows help text without errors."""
    runner = CliRunner()
    result = runner.invoke(cli, ["dead-letter", "monitor", "--help"])
    assert result.exit_code == 0
    assert "dead-letter" in result.output.lower() or "monitor" in result.output.lower()


def test_dead_letter_monitor_runs_with_mocked_consumer():
    """dead-letter monitor creates a DeadLetterConsumer and calls asyncio.run."""

    runner = CliRunner()

    mock_consumer = MagicMock()
    mock_cls = MagicMock(return_value=mock_consumer)

    # Patch the dead_letter module to expose DEAD_LETTER_SUBJECT (which lives in
    # router.py but the CLI imports from dead_letter — inject it so the import works).
    import heddle.router.dead_letter as _dl_mod

    _orig_subject = getattr(_dl_mod, "DEAD_LETTER_SUBJECT", "NOT_PRESENT")
    _orig_consumer = _dl_mod.DeadLetterConsumer
    _dl_mod.DEAD_LETTER_SUBJECT = "heddle.tasks.dead_letter"
    _dl_mod.DeadLetterConsumer = mock_cls

    try:
        with patch("heddle.cli.main.asyncio.run") as mock_run:
            result = runner.invoke(
                cli,
                [
                    "dead-letter",
                    "monitor",
                    "--nats-url",
                    "nats://localhost:4222",
                    "--max-size",
                    "500",
                ],
            )
    finally:
        _dl_mod.DeadLetterConsumer = _orig_consumer
        if _orig_subject == "NOT_PRESENT":
            if hasattr(_dl_mod, "DEAD_LETTER_SUBJECT"):
                del _dl_mod.DEAD_LETTER_SUBJECT
        else:
            _dl_mod.DEAD_LETTER_SUBJECT = _orig_subject

    assert result.exit_code == 0
    mock_cls.assert_called_once_with(
        actor_id="dead-letter-monitor",
        max_size=500,
        nats_url="nats://localhost:4222",
    )
    mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# submit command — async _submit body
# ---------------------------------------------------------------------------


def test_submit_async_body_publishes_goal():
    """submit command runs the async body: connect, publish, drain."""
    runner = CliRunner()

    mock_nc = MagicMock()

    async def _noop(*args, **kwargs):
        return None

    mock_nc.publish = MagicMock(side_effect=_noop)
    mock_nc.drain = MagicMock(side_effect=_noop)

    async def fake_connect(url):
        return mock_nc

    with patch("nats.connect", side_effect=fake_connect):
        result = runner.invoke(
            cli,
            [
                "submit",
                "Async body test goal",
                "--nats-url",
                "nats://localhost:4222",
                "--context",
                "key=value",
            ],
        )

    assert result.exit_code == 0
    assert "Submitted goal" in result.output


# ---------------------------------------------------------------------------
# pipeline — preflight check path
# ---------------------------------------------------------------------------


def test_pipeline_preflight_check_called(tmp_path):
    """pipeline without --skip-preflight invokes _run_preflight."""
    config_path = _write_yaml(
        tmp_path / "pipeline.yaml",
        "name: test_pipeline\npipeline_stages:\n  - name: stage1\n    worker_type: summarizer\n",
    )
    runner = CliRunner()

    with (
        patch("heddle.cli.main._run_preflight") as mock_preflight,
        patch("heddle.cli.main.asyncio.run"),
    ):
        result = runner.invoke(
            cli,
            [
                "pipeline",
                "--config",
                config_path,
                "--nats-url",
                "nats://localhost:4222",
            ],
        )

    assert result.exit_code == 0
    mock_preflight.assert_called_once_with("nats://localhost:4222", config=config_path)


def test_pipeline_preflight_skip_flag(tmp_path):
    """pipeline with --skip-preflight does NOT call _run_preflight."""
    config_path = _write_yaml(
        tmp_path / "pipeline.yaml",
        "name: test_pipeline\npipeline_stages:\n  - name: stage1\n    worker_type: summarizer\n",
    )
    runner = CliRunner()

    with (
        patch("heddle.cli.main._run_preflight") as mock_preflight,
        patch("heddle.cli.main.asyncio.run"),
    ):
        result = runner.invoke(
            cli,
            [
                "pipeline",
                "--skip-preflight",
                "--config",
                config_path,
                "--nats-url",
                "nats://localhost:4222",
            ],
        )

    assert result.exit_code == 0
    mock_preflight.assert_not_called()


# ---------------------------------------------------------------------------
# router — preflight check path
# ---------------------------------------------------------------------------


def test_router_preflight_check_called(tmp_path):
    """router without --skip-preflight invokes _run_preflight."""
    runner = CliRunner()

    mock_router = MagicMock()
    mock_router.run = MagicMock(return_value=None)
    mock_router.process_messages = MagicMock(return_value=None)

    with (
        patch("heddle.cli.main._run_preflight") as mock_preflight,
        patch("heddle.cli.main.asyncio.run") as mock_run,
        patch("heddle.bus.nats_adapter.NATSBus"),
        patch("heddle.router.router.TaskRouter", return_value=mock_router),
    ):
        result = runner.invoke(
            cli,
            [
                "router",
                "--config",
                "configs/router_rules.yaml",
                "--nats-url",
                "nats://localhost:4222",
            ],
        )

    assert result.exit_code == 0
    mock_preflight.assert_called_once_with(
        "nats://localhost:4222", config="configs/router_rules.yaml"
    )
    mock_run.assert_called_once()


def test_router_preflight_skip_flag(tmp_path):
    """router with --skip-preflight does NOT call _run_preflight."""
    runner = CliRunner()

    mock_router = MagicMock()
    mock_router.run = MagicMock(return_value=None)
    mock_router.process_messages = MagicMock(return_value=None)

    with (
        patch("heddle.cli.main._run_preflight") as mock_preflight,
        patch("heddle.cli.main.asyncio.run") as mock_run,
        patch("heddle.bus.nats_adapter.NATSBus"),
        patch("heddle.router.router.TaskRouter", return_value=mock_router),
    ):
        result = runner.invoke(
            cli,
            [
                "router",
                "--skip-preflight",
                "--config",
                "configs/router_rules.yaml",
                "--nats-url",
                "nats://localhost:4222",
            ],
        )

    assert result.exit_code == 0
    mock_preflight.assert_not_called()
    mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# _run_preflight — direct unit tests (lines 87-104)
# ---------------------------------------------------------------------------


def test_preflight_config_not_readable():
    """_run_preflight aborts when check_config_readable returns failure."""
    from heddle.cli.main import _run_preflight

    with (
        patch("heddle.cli.main.check_config_readable", return_value=(False, "Cannot read config")),
        pytest.raises(click.Abort),
    ):
        _run_preflight("nats://localhost:4222", config="bad.yaml")


def test_preflight_nats_fail():
    """_run_preflight aborts when NATS connectivity check fails."""
    from heddle.cli.main import _run_preflight

    with (
        patch("heddle.cli.main.check_config_readable", return_value=(True, "ok")),
        patch(
            "heddle.cli.main.check_nats_connectivity",
            AsyncMock(return_value=(False, "Connection refused")),
        ),
        pytest.raises(click.Abort),
    ):
        _run_preflight("nats://localhost:4222", config="good.yaml")


def test_preflight_nats_ok_with_env_warnings(capsys):
    """_run_preflight echoes env var warnings when check_env=True and tier set."""
    from heddle.cli.main import _run_preflight

    with (
        patch("heddle.cli.main.check_config_readable", return_value=(True, "ok")),
        patch(
            "heddle.cli.main.check_nats_connectivity",
            AsyncMock(return_value=(True, "NATS connected")),
        ),
        patch(
            "heddle.cli.main.check_env_vars",
            return_value=["ANTHROPIC_API_KEY not set"],
        ) as mock_env,
    ):
        _run_preflight("nats://localhost:4222", config="good.yaml", tier="standard", check_env=True)

    mock_env.assert_called_once_with("standard")


def test_preflight_nats_ok_no_env_check():
    """_run_preflight does not call check_env_vars when check_env=False."""
    from heddle.cli.main import _run_preflight

    with (
        patch("heddle.cli.main.check_config_readable", return_value=(True, "ok")),
        patch(
            "heddle.cli.main.check_nats_connectivity",
            AsyncMock(return_value=(True, "NATS connected")),
        ),
        patch("heddle.cli.main.check_env_vars") as mock_env,
    ):
        _run_preflight(
            "nats://localhost:4222", config="good.yaml", tier="standard", check_env=False
        )

    mock_env.assert_not_called()


# ---------------------------------------------------------------------------
# mdns command — async body (lines 838-862)
# ---------------------------------------------------------------------------


def test_mdns_runs_and_registers_services():
    """mdns command starts advertiser, registers workshop + nats, and stops."""
    import asyncio
    from unittest.mock import AsyncMock

    runner = CliRunner()

    mock_advertiser = MagicMock()
    mock_advertiser.start = AsyncMock()
    mock_advertiser.stop = AsyncMock()

    mock_mdns_module = MagicMock()
    mock_mdns_module.HeddleServiceAdvertiser.return_value = mock_advertiser

    import sys

    saved = sys.modules.get("heddle.discovery.mdns", "NOT_PRESENT")
    sys.modules["heddle.discovery.mdns"] = mock_mdns_module

    try:
        # Make the indefinite-block construct raise CancelledError immediately
        # so the command exits.  ``heddle mdns`` blocks via
        # ``asyncio.Event().wait()`` (was ``asyncio.sleep(float("inf"))`` —
        # changed for uvloop / Windows compatibility), so we patch the
        # Event class to short-circuit.
        with patch("asyncio.Event.wait", side_effect=asyncio.CancelledError):
            result = runner.invoke(
                cli,
                [
                    "mdns",
                    "--workshop-port",
                    "8080",
                    "--nats-port",
                    "4222",
                ],
            )
    finally:
        if saved == "NOT_PRESENT":
            del sys.modules["heddle.discovery.mdns"]
        else:
            sys.modules["heddle.discovery.mdns"] = saved

    assert result.exit_code == 0
    mock_mdns_module.HeddleServiceAdvertiser.assert_called_once()
    mock_advertiser.start.assert_awaited_once()
    mock_advertiser.register_workshop.assert_called_once_with(port=8080, host=None)
    mock_advertiser.register_nats.assert_called_once_with(port=4222, host=None)
    mock_advertiser.stop.assert_awaited_once()


def test_mdns_with_mcp_port():
    """mdns with --mcp-port > 0 also registers the MCP service."""
    import asyncio
    from unittest.mock import AsyncMock

    runner = CliRunner()

    mock_advertiser = MagicMock()
    mock_advertiser.start = AsyncMock()
    mock_advertiser.stop = AsyncMock()

    mock_mdns_module = MagicMock()
    mock_mdns_module.HeddleServiceAdvertiser.return_value = mock_advertiser

    import sys

    saved = sys.modules.get("heddle.discovery.mdns", "NOT_PRESENT")
    sys.modules["heddle.discovery.mdns"] = mock_mdns_module

    try:
        with patch("asyncio.Event.wait", side_effect=asyncio.CancelledError):
            result = runner.invoke(
                cli,
                [
                    "mdns",
                    "--workshop-port",
                    "8080",
                    "--nats-port",
                    "4222",
                    "--mcp-port",
                    "9000",
                ],
            )
    finally:
        if saved == "NOT_PRESENT":
            del sys.modules["heddle.discovery.mdns"]
        else:
            sys.modules["heddle.discovery.mdns"] = saved

    assert result.exit_code == 0
    mock_advertiser.register_mcp.assert_called_once_with(port=9000, host=None)


# ---------------------------------------------------------------------------
# worker command — config validation errors (lines 159-162)
# ---------------------------------------------------------------------------


def test_worker_config_validation_errors(tmp_path):
    """worker command fails with ClickException when config has validation errors."""
    config_path = _write_yaml(
        tmp_path / "worker.yaml",
        "not_a_valid_field: 123\n",
    )
    runner = CliRunner()

    with patch("heddle.cli.main.asyncio.run"):
        result = runner.invoke(
            cli,
            [
                "worker",
                "--skip-preflight",
                "--config",
                config_path,
                "--tier",
                "local",
                "--nats-url",
                "nats://localhost:4222",
            ],
        )

    assert result.exit_code != 0
    assert "error(s)" in result.output


# ---------------------------------------------------------------------------
# processor command — config validation errors (lines 257-260)
# ---------------------------------------------------------------------------


def test_processor_config_validation_errors(tmp_path):
    """processor command fails with ClickException when config has validation errors."""
    config_path = _write_yaml(
        tmp_path / "proc.yaml",
        "not_a_valid_field: 123\n",
    )
    runner = CliRunner()

    with patch("heddle.cli.main.asyncio.run"):
        result = runner.invoke(
            cli,
            [
                "processor",
                "--skip-preflight",
                "--config",
                config_path,
                "--nats-url",
                "nats://localhost:4222",
            ],
        )

    assert result.exit_code != 0
    assert "error(s)" in result.output


# ---------------------------------------------------------------------------
# worker command — preflight invoked (line 148)
# ---------------------------------------------------------------------------


def test_worker_preflight_check_called(tmp_path):
    """worker without --skip-preflight invokes _run_preflight with check_env=True."""
    config_path = _write_yaml(
        tmp_path / "worker.yaml",
        "name: summarizer\ndefault_model_tier: local\nsystem_prompt: You are a summarizer.\n",
    )
    runner = CliRunner()

    with (
        patch("heddle.cli.main._run_preflight") as mock_preflight,
        patch.dict("os.environ", {"OLLAMA_URL": "http://localhost:11434"}, clear=False),
        patch("heddle.cli.main.asyncio.run"),
    ):
        result = runner.invoke(
            cli,
            [
                "worker",
                "--config",
                config_path,
                "--tier",
                "local",
                "--nats-url",
                "nats://localhost:4222",
            ],
        )

    assert result.exit_code == 0
    mock_preflight.assert_called_once_with(
        "nats://localhost:4222", config=config_path, tier="local", check_env=True
    )


# ---------------------------------------------------------------------------
# processor command — preflight invoked (line 247)
# ---------------------------------------------------------------------------


def test_processor_preflight_check_called(tmp_path):
    """processor without --skip-preflight invokes _run_preflight with check_env=True."""
    config_path = _write_yaml(
        tmp_path / "proc.yaml",
        "name: doc_extractor\nworker_kind: processor\nprocessing_backend: os.path.join\n",
    )
    runner = CliRunner()

    mock_backend = MagicMock()
    with (
        patch("heddle.cli.main._run_preflight") as mock_preflight,
        patch("heddle.cli.main._load_processing_backend", return_value=mock_backend),
        patch("heddle.cli.main.asyncio.run"),
    ):
        result = runner.invoke(
            cli,
            [
                "processor",
                "--config",
                config_path,
                "--nats-url",
                "nats://localhost:4222",
                "--tier",
                "local",
            ],
        )

    assert result.exit_code == 0
    mock_preflight.assert_called_once_with(
        "nats://localhost:4222", config=config_path, tier="local", check_env=True
    )
