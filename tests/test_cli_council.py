"""
Tests for heddle council command group — run, validate.

All tests use Click's CliRunner with mocked backends and runner.
No external services needed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import structlog
from click.testing import CliRunner

_saved_structlog_config = structlog.get_config()
from heddle.cli.council import council  # noqa: E402

structlog.configure(**_saved_structlog_config)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_council_config_yaml() -> str:
    """Return valid council config YAML."""
    return (
        "name: test_council\n"
        "protocol: round_robin\n"
        "max_rounds: 3\n"
        "convergence:\n"
        "  method: none\n"
        "agents:\n"
        "  - name: agent_a\n"
        "    worker_type: reviewer\n"
        "    tier: standard\n"
        "    role: Analyst\n"
        "  - name: agent_b\n"
        "    worker_type: reviewer\n"
        "    tier: standard\n"
        "    role: Critic\n"
        "facilitator:\n"
        "  tier: standard\n"
    )


def _make_mock_result(**overrides):
    """Create a mock CouncilResult."""
    from heddle.contrib.council.schemas import CouncilResult

    defaults = {
        "topic": "Test topic",
        "rounds_completed": 2,
        "converged": True,
        "convergence_score": 0.85,
        "synthesis": "The team agreed on approach X.",
        "transcript": [],
        "agent_summaries": {"agent_a": "Position A", "agent_b": "Position B"},
        "total_token_usage": {"prompt_tokens": 500, "completion_tokens": 300},
        "elapsed_ms": 1234,
    }
    defaults.update(overrides)
    return CouncilResult(**defaults)


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------


def test_council_help():
    """council --help shows group help."""
    result = CliRunner().invoke(council, ["--help"])
    assert result.exit_code == 0
    assert "Multi-agent council" in result.output


def test_council_run_help():
    """council run --help shows usage."""
    result = CliRunner().invoke(council, ["run", "--help"])
    assert result.exit_code == 0
    assert "Run a council discussion" in result.output


def test_council_validate_help():
    """council validate --help shows usage."""
    result = CliRunner().invoke(council, ["validate", "--help"])
    assert result.exit_code == 0
    assert "Validate a council config" in result.output


# ---------------------------------------------------------------------------
# council run
# ---------------------------------------------------------------------------


def test_run_with_topic_string(tmp_path):
    """council run with --topic string wires through to CouncilRunner.run()."""
    cfg = tmp_path / "council.yaml"
    cfg.write_text(_make_council_config_yaml())

    mock_result = _make_mock_result()
    mock_runner = MagicMock()
    mock_runner.run = AsyncMock(return_value=mock_result)
    mock_runner.aclose = AsyncMock()
    backend = MagicMock(aclose=AsyncMock())

    with (
        patch(
            "heddle.worker.backends.build_backends_from_env",
            return_value={"standard": backend},
        ),
        patch("heddle.contrib.council.runner.CouncilRunner", return_value=mock_runner),
    ):
        result = CliRunner().invoke(
            council,
            ["run", str(cfg), "--topic", "Should we adopt microservices?"],
        )

    assert result.exit_code == 0, result.output
    assert "Synthesis" in result.output
    assert "The team agreed" in result.output
    assert "2 round(s)" in result.output
    mock_runner.run.assert_called_once()
    # Lifecycle assertions: shutdown must release both runner-cached
    # bridges and any owned backend httpx clients.
    mock_runner.aclose.assert_awaited_once()
    backend.aclose.assert_awaited_once()


def test_run_with_topic_file(tmp_path):
    """council run with --topic pointing to a file reads file contents."""
    cfg = tmp_path / "council.yaml"
    cfg.write_text(_make_council_config_yaml())

    topic_file = tmp_path / "topic.txt"
    topic_file.write_text("Should we migrate to Kubernetes?")

    mock_result = _make_mock_result(topic="Should we migrate to Kubernetes?")
    mock_runner = MagicMock()
    mock_runner.run = AsyncMock(return_value=mock_result)
    mock_runner.aclose = AsyncMock()

    with (
        patch(
            "heddle.worker.backends.build_backends_from_env",
            return_value={"standard": MagicMock(aclose=AsyncMock())},
        ),
        patch("heddle.contrib.council.runner.CouncilRunner", return_value=mock_runner),
    ):
        result = CliRunner().invoke(
            council,
            ["run", str(cfg), "--topic", str(topic_file)],
        )

    assert result.exit_code == 0, result.output
    # Verify the file content was used as topic.
    call_kwargs = mock_runner.run.call_args
    assert "Kubernetes" in call_kwargs[0][0] or "Kubernetes" in call_kwargs.kwargs.get("topic", "")


def test_run_with_output(tmp_path):
    """council run with --output writes JSON result."""
    cfg = tmp_path / "council.yaml"
    cfg.write_text(_make_council_config_yaml())

    output_path = tmp_path / "result.json"

    mock_result = _make_mock_result()
    mock_runner = MagicMock()
    mock_runner.run = AsyncMock(return_value=mock_result)
    mock_runner.aclose = AsyncMock()

    with (
        patch(
            "heddle.worker.backends.build_backends_from_env",
            return_value={"standard": MagicMock(aclose=AsyncMock())},
        ),
        patch("heddle.contrib.council.runner.CouncilRunner", return_value=mock_runner),
    ):
        result = CliRunner().invoke(
            council,
            ["run", str(cfg), "--topic", "Test", "--output", str(output_path)],
        )

    assert result.exit_code == 0, result.output
    assert output_path.exists()

    import json

    data = json.loads(output_path.read_text())
    assert data["topic"] == "Test topic"
    assert data["rounds_completed"] == 2


def test_run_with_rounds_override(tmp_path):
    """council run with --rounds overrides config max_rounds."""
    cfg = tmp_path / "council.yaml"
    cfg.write_text(_make_council_config_yaml())

    mock_result = _make_mock_result()
    mock_runner = MagicMock()
    mock_runner.run = AsyncMock(return_value=mock_result)
    mock_runner.aclose = AsyncMock()

    with (
        patch(
            "heddle.worker.backends.build_backends_from_env",
            return_value={"standard": MagicMock(aclose=AsyncMock())},
        ),
        patch("heddle.contrib.council.config.load_council_config") as mock_load,
        patch("heddle.contrib.council.runner.CouncilRunner", return_value=mock_runner),
    ):
        agent_a, agent_b = MagicMock(), MagicMock()
        agent_a.name = "agent_a"
        agent_b.name = "agent_b"
        mock_config = MagicMock()
        mock_config.name = "test"
        mock_config.agents = [agent_a, agent_b]
        mock_config.protocol = "round_robin"
        mock_config.max_rounds = 3
        mock_config.convergence = MagicMock()
        mock_load.return_value = mock_config

        result = CliRunner().invoke(
            council,
            ["run", str(cfg), "--topic", "Test", "--rounds", "5"],
        )

    assert result.exit_code == 0, result.output
    assert mock_config.max_rounds == 5


def test_run_with_no_convergence(tmp_path):
    """council run with --no-convergence sets method to none."""
    cfg = tmp_path / "council.yaml"
    cfg.write_text(_make_council_config_yaml())

    mock_result = _make_mock_result()
    mock_runner = MagicMock()
    mock_runner.run = AsyncMock(return_value=mock_result)
    mock_runner.aclose = AsyncMock()

    with (
        patch(
            "heddle.worker.backends.build_backends_from_env",
            return_value={"standard": MagicMock(aclose=AsyncMock())},
        ),
        patch("heddle.contrib.council.config.load_council_config") as mock_load,
        patch("heddle.contrib.council.runner.CouncilRunner", return_value=mock_runner),
    ):
        agent_a, agent_b = MagicMock(), MagicMock()
        agent_a.name = "agent_a"
        agent_b.name = "agent_b"
        mock_config = MagicMock()
        mock_config.name = "test"
        mock_config.agents = [agent_a, agent_b]
        mock_config.protocol = "round_robin"
        mock_config.max_rounds = 3
        mock_config.convergence = MagicMock()
        mock_load.return_value = mock_config

        result = CliRunner().invoke(
            council,
            ["run", str(cfg), "--topic", "Test", "--no-convergence"],
        )

    assert result.exit_code == 0, result.output
    assert mock_config.convergence.method == "none"


def test_run_no_backends(tmp_path):
    """council run with no backends available fails with a clear error."""
    cfg = tmp_path / "council.yaml"
    cfg.write_text(_make_council_config_yaml())

    with patch("heddle.worker.backends.build_backends_from_env", return_value={}):
        result = CliRunner().invoke(
            council,
            ["run", str(cfg), "--topic", "Test"],
        )

    assert result.exit_code != 0
    assert "No LLM backends available" in result.output


# ---------------------------------------------------------------------------
# council validate
# ---------------------------------------------------------------------------


def test_validate_valid_config(tmp_path):
    """council validate on a valid config exits 0."""
    cfg = tmp_path / "council.yaml"
    cfg.write_text(_make_council_config_yaml())

    result = CliRunner().invoke(council, ["validate", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "✓" in result.output
    assert "[council]" in result.output
    assert "test_council" in result.output
    assert "2" in result.output  # agent count


def test_validate_invalid_config(tmp_path):
    """council validate on invalid config exits 1."""
    cfg = tmp_path / "bad_council.yaml"
    cfg.write_text("name: bad_council\nagents:\n  - name: only_one\n    worker_type: reviewer\n")

    result = CliRunner().invoke(council, ["validate", str(cfg)])
    assert result.exit_code == 1
    assert "✗" in result.output


def test_validate_invalid_yaml(tmp_path):
    """council validate on malformed YAML exits 1."""
    cfg = tmp_path / "broken.yaml"
    cfg.write_text("{{not valid yaml")

    result = CliRunner().invoke(council, ["validate", str(cfg)])
    assert result.exit_code == 1
    assert "Invalid YAML" in result.output


def test_validate_empty_file(tmp_path):
    """council validate on empty file exits 1."""
    cfg = tmp_path / "empty.yaml"
    cfg.write_text("")

    result = CliRunner().invoke(council, ["validate", str(cfg)])
    assert result.exit_code == 1
    assert "empty" in result.output


def test_validate_shows_tier_availability(tmp_path):
    """council validate reports tier availability from env."""
    cfg = tmp_path / "council.yaml"
    cfg.write_text(_make_council_config_yaml())

    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}, clear=False):
        result = CliRunner().invoke(council, ["validate", str(cfg)])

    assert result.exit_code == 0, result.output
    assert "standard" in result.output


# ---------------------------------------------------------------------------
# Lifecycle — aclose must run even when the council raises
# ---------------------------------------------------------------------------


def test_run_closes_resources_when_council_run_raises(tmp_path):
    """If CouncilRunner.run() raises, the CLI must still aclose the runner
    and every backend.  Without the try/finally wrapper, a failed run
    would silently leak httpx clients (the previous code path had this
    bug — this test pins the fix).
    """
    cfg = tmp_path / "council.yaml"
    cfg.write_text(_make_council_config_yaml())

    mock_runner = MagicMock()
    mock_runner.run = AsyncMock(side_effect=RuntimeError("council blew up"))
    mock_runner.aclose = AsyncMock()
    backend = MagicMock(aclose=AsyncMock())

    with (
        patch(
            "heddle.worker.backends.build_backends_from_env",
            return_value={"standard": backend},
        ),
        patch("heddle.contrib.council.runner.CouncilRunner", return_value=mock_runner),
    ):
        result = CliRunner().invoke(
            council,
            ["run", str(cfg), "--topic", "Anything"],
        )

    # The exception bubbles up — what matters is cleanup ran first.
    assert result.exit_code != 0
    mock_runner.aclose.assert_awaited_once()
    backend.aclose.assert_awaited_once()


def test_run_continues_cleanup_when_one_backend_aclose_raises(tmp_path):
    """A flaky backend.aclose() must not stop other backends from being closed."""
    cfg = tmp_path / "council.yaml"
    cfg.write_text(_make_council_config_yaml())

    mock_result = _make_mock_result()
    mock_runner = MagicMock()
    mock_runner.run = AsyncMock(return_value=mock_result)
    mock_runner.aclose = AsyncMock()
    flaky = MagicMock(aclose=AsyncMock(side_effect=RuntimeError("flaky")))
    healthy = MagicMock(aclose=AsyncMock())

    with (
        patch(
            "heddle.worker.backends.build_backends_from_env",
            return_value={"standard": flaky, "frontier": healthy},
        ),
        patch("heddle.contrib.council.runner.CouncilRunner", return_value=mock_runner),
    ):
        result = CliRunner().invoke(
            council,
            ["run", str(cfg), "--topic", "Anything"],
        )

    assert result.exit_code == 0, result.output
    mock_runner.aclose.assert_awaited_once()
    flaky.aclose.assert_awaited_once()
    healthy.aclose.assert_awaited_once()
