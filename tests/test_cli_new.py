"""
Tests for heddle new worker / heddle new pipeline — interactive scaffolding.

All tests use CliRunner with input= for interactive prompts and tmp_path for isolation.
"""

from __future__ import annotations

import structlog
import yaml
from click.testing import CliRunner

_saved_structlog_config = structlog.get_config()
from heddle.cli.new import _build_schema, _validate_name, new  # noqa: E402

structlog.configure(**_saved_structlog_config)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_validate_name_valid():
    assert _validate_name("my_worker") is None
    assert _validate_name("x") is None
    assert _validate_name("worker123") is None


def test_validate_name_invalid():
    assert _validate_name("") is not None
    assert _validate_name("MyWorker") is not None  # uppercase
    assert _validate_name("123worker") is not None  # starts with digit
    assert _validate_name("my-worker") is not None  # hyphens


def test_build_schema_single_field():
    schema = _build_schema("text")
    assert schema["required"] == ["text"]
    assert "text" in schema["properties"]
    assert schema["properties"]["text"]["type"] == "string"


def test_build_schema_multiple_fields():
    schema = _build_schema("text, language, confidence")
    assert schema["required"] == ["text", "language", "confidence"]
    assert len(schema["properties"]) == 3


def test_build_schema_empty():
    schema = _build_schema("")
    assert schema["required"] == []
    assert schema["properties"] == {}


# ---------------------------------------------------------------------------
# heddle new --help
# ---------------------------------------------------------------------------


def test_new_help():
    result = CliRunner().invoke(new, ["--help"])
    assert result.exit_code == 0
    assert "Scaffold" in result.output


def test_new_worker_help():
    result = CliRunner().invoke(new, ["worker", "--help"])
    assert result.exit_code == 0
    assert "worker config" in result.output


def test_new_pipeline_help():
    result = CliRunner().invoke(new, ["pipeline", "--help"])
    assert result.exit_code == 0
    assert "pipeline config" in result.output


# ---------------------------------------------------------------------------
# heddle new worker
# ---------------------------------------------------------------------------


def test_new_worker_non_interactive(tmp_path):
    """Non-interactive creates a minimal valid worker."""
    result = CliRunner().invoke(
        new,
        [
            "worker",
            "--non-interactive",
            "--name",
            "test_worker",
            "--kind",
            "llm",
            "--tier",
            "local",
            "--configs-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Created" in result.output

    # Verify the file was written
    dest = tmp_path / "workers" / "test_worker.yaml"
    assert dest.exists()

    config = yaml.safe_load(dest.read_text())
    assert config["name"] == "test_worker"
    assert config["default_model_tier"] == "local"
    assert config["reset_after_task"] is True
    assert "input_schema" in config
    assert "output_schema" in config
    assert "system_prompt" in config


def test_new_worker_llm_interactive(tmp_path):
    """Interactive LLM worker creation with prompted inputs."""
    # Inputs: name, kind=llm, tier=local, system prompt, input fields, output fields, timeout
    inputs = "\n".join(
        [
            "my_analyzer",  # name
            "llm",  # kind
            "standard",  # tier
            "Analyze the input text and extract key themes.",  # system prompt
            "text,language",  # input fields
            "themes,confidence",  # output fields
            "45",  # timeout
        ]
    )
    result = CliRunner().invoke(
        new,
        ["worker", "--configs-dir", str(tmp_path)],
        input=inputs,
    )
    assert result.exit_code == 0, result.output
    assert "Created" in result.output

    config = yaml.safe_load((tmp_path / "workers" / "my_analyzer.yaml").read_text())
    assert config["name"] == "my_analyzer"
    assert config["default_model_tier"] == "standard"
    assert "text" in config["input_schema"]["required"]
    assert "language" in config["input_schema"]["required"]
    assert "themes" in config["output_schema"]["required"]
    assert config["timeout_seconds"] == 45


def test_new_worker_processor_interactive(tmp_path):
    """Interactive processor worker creation."""
    inputs = "\n".join(
        [
            "my_processor",  # name
            "processor",  # kind
            "mypackage.backend.MyBackend",  # processing_backend
            "data",  # input fields
            "processed",  # output fields
            "120",  # timeout
        ]
    )
    result = CliRunner().invoke(
        new,
        ["worker", "--configs-dir", str(tmp_path)],
        input=inputs,
    )
    assert result.exit_code == 0, result.output

    config = yaml.safe_load((tmp_path / "workers" / "my_processor.yaml").read_text())
    assert config["worker_kind"] == "processor"
    assert config["processing_backend"] == "mypackage.backend.MyBackend"


def test_new_worker_name_conflict(tmp_path):
    """Existing file causes an error."""
    workers_dir = tmp_path / "workers"
    workers_dir.mkdir(parents=True)
    (workers_dir / "existing.yaml").write_text("name: existing\n")

    result = CliRunner().invoke(
        new,
        [
            "worker",
            "--non-interactive",
            "--name",
            "existing",
            "--configs-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_new_worker_invalid_name():
    """Invalid name is rejected."""
    result = CliRunner().invoke(
        new,
        [
            "worker",
            "--non-interactive",
            "--name",
            "Invalid-Name",
            "--configs-dir",
            "/tmp",
        ],
    )
    assert result.exit_code != 0
    assert "lowercase" in result.output


# ---------------------------------------------------------------------------
# heddle new pipeline
# ---------------------------------------------------------------------------


def test_new_pipeline_non_interactive(tmp_path):
    """Non-interactive creates a minimal pipeline."""
    # Create a worker config so the pipeline can reference it
    workers_dir = tmp_path / "workers"
    workers_dir.mkdir(parents=True)
    (workers_dir / "summarizer.yaml").write_text("name: summarizer\nsystem_prompt: test\n")

    result = CliRunner().invoke(
        new,
        [
            "pipeline",
            "--non-interactive",
            "--name",
            "test_pipeline",
            "--configs-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Created" in result.output

    dest = tmp_path / "orchestrators" / "test_pipeline.yaml"
    assert dest.exists()

    config = yaml.safe_load(dest.read_text())
    assert config["name"] == "test_pipeline"
    assert len(config["pipeline_stages"]) == 1


def test_new_pipeline_interactive_two_stages(tmp_path):
    """Interactive pipeline with two stages."""
    # Create worker configs
    workers_dir = tmp_path / "workers"
    workers_dir.mkdir(parents=True)
    (workers_dir / "summarizer.yaml").write_text("name: summarizer\nsystem_prompt: test\n")
    (workers_dir / "classifier.yaml").write_text("name: classifier\nsystem_prompt: test\n")

    # Inputs for 2 stages:
    # Stage 1: worker_type, stage_name, mapping (field=path, empty to finish), add another?
    # Stage 2: worker_type, stage_name, mapping, add another?
    # Then timeout
    inputs = "\n".join(
        [
            "test_pipe",  # pipeline name
            "summarizer",  # stage 1 worker_type
            "summarize",  # stage 1 name
            "text=goal.context.text",  # mapping pair
            "",  # end mapping
            "y",  # add another stage
            "classifier",  # stage 2 worker_type
            "classify",  # stage 2 name
            "text=summarize.output.summary",  # mapping pair
            "",  # end mapping
            "n",  # no more stages
            "300",  # timeout
        ]
    )
    result = CliRunner().invoke(
        new,
        ["pipeline", "--configs-dir", str(tmp_path)],
        input=inputs,
    )
    assert result.exit_code == 0, result.output
    assert "summarize → classify" in result.output

    config = yaml.safe_load((tmp_path / "orchestrators" / "test_pipe.yaml").read_text())
    assert len(config["pipeline_stages"]) == 2
    assert config["pipeline_stages"][0]["name"] == "summarize"
    assert config["pipeline_stages"][1]["name"] == "classify"
    assert config["pipeline_stages"][1]["input_mapping"]["text"] == "summarize.output.summary"


def test_new_pipeline_lists_workers(tmp_path):
    """Interactive mode lists available workers."""
    workers_dir = tmp_path / "workers"
    workers_dir.mkdir(parents=True)
    (workers_dir / "alpha.yaml").write_text("name: alpha\n")
    (workers_dir / "beta.yaml").write_text("name: beta\n")

    # Non-interactive to avoid the stage prompts, just check listing
    result = CliRunner().invoke(
        new,
        [
            "pipeline",
            "--non-interactive",
            "--name",
            "test_pipe",
            "--configs-dir",
            str(tmp_path),
        ],
    )
    # Non-interactive uses the first available worker
    assert result.exit_code == 0, result.output


def test_new_pipeline_name_conflict(tmp_path):
    """Existing file causes an error."""
    orch_dir = tmp_path / "orchestrators"
    orch_dir.mkdir(parents=True)
    (orch_dir / "existing.yaml").write_text("name: existing\n")

    result = CliRunner().invoke(
        new,
        [
            "pipeline",
            "--non-interactive",
            "--name",
            "existing",
            "--configs-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code != 0
    assert "already exists" in result.output


# ---------------------------------------------------------------------------
# Default-path coverage (H5): non-interactive scaffolds with all options elided
# ---------------------------------------------------------------------------


def test_list_workers_empty_when_directory_missing(tmp_path):
    """_list_workers returns [] when configs/workers does not exist."""
    from heddle.cli.new import _list_workers

    # tmp_path/workers doesn't exist
    assert _list_workers(tmp_path) == []


def test_list_workers_skips_template_and_dotfiles(tmp_path):
    """_list_workers ignores _template.yaml and hidden files."""
    from heddle.cli.new import _list_workers

    workers = tmp_path / "workers"
    workers.mkdir()
    (workers / "summarizer.yaml").write_text("name: summarizer\n")
    (workers / "_template.yaml").write_text("name: _template\n")
    (workers / ".hidden.yaml").write_text("name: .hidden\n")
    (workers / "classifier.yaml").write_text("name: classifier\n")

    assert _list_workers(tmp_path) == ["classifier", "summarizer"]


def test_new_worker_non_interactive_all_defaults(tmp_path):
    """Non-interactive without --name/--kind/--tier uses the fallbacks
    (my_worker / llm / local). Pins the default config that the next-step
    docs reference.
    """
    result = CliRunner().invoke(
        new,
        [
            "worker",
            "--non-interactive",
            "--configs-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    dest = tmp_path / "workers" / "my_worker.yaml"
    assert dest.exists()

    config = yaml.safe_load(dest.read_text())
    assert config["name"] == "my_worker"
    assert config["default_model_tier"] == "local"
    assert config["timeout_seconds"] == 30
    assert "my_worker worker" in config["system_prompt"]


def test_new_worker_non_interactive_processor(tmp_path):
    """Non-interactive --kind=processor uses the placeholder backend
    string; the scaffold is intentionally NOT runnable until the
    operator points it at a real backend.
    """
    result = CliRunner().invoke(
        new,
        [
            "worker",
            "--non-interactive",
            "--name",
            "auto_proc",
            "--kind",
            "processor",
            "--configs-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output

    config = yaml.safe_load((tmp_path / "workers" / "auto_proc.yaml").read_text())
    assert config["worker_kind"] == "processor"
    assert config["processing_backend"] == "mypackage.backend.MyBackend"
    assert config["timeout_seconds"] == 60


def test_new_pipeline_non_interactive_default_name(tmp_path):
    """Non-interactive without --name uses the my_pipeline fallback."""
    result = CliRunner().invoke(
        new,
        ["pipeline", "--non-interactive", "--configs-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output

    dest = tmp_path / "orchestrators" / "my_pipeline.yaml"
    assert dest.exists()
    config = yaml.safe_load(dest.read_text())
    assert config["name"] == "my_pipeline"
    # No workers available -> falls back to "summarizer" placeholder.
    assert config["pipeline_stages"][0]["worker_type"] == "summarizer"


def test_new_pipeline_non_interactive_invalid_name_rejected(tmp_path):
    """Pipeline name validation matches worker name validation."""
    result = CliRunner().invoke(
        new,
        [
            "pipeline",
            "--non-interactive",
            "--name",
            "BAD-NAME",
            "--configs-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code != 0
    assert "lowercase" in result.output


# ---------------------------------------------------------------------------
# Interactive validation/retry paths
# ---------------------------------------------------------------------------


def test_new_pipeline_interactive_stage_name_retry(tmp_path):
    """Invalid stage name surfaces an inline error and re-prompts.

    Asserts the validation error message appears in the output (so the
    operator sees what was wrong) and that the eventually-accepted
    stage name lands in the YAML.
    """
    workers_dir = tmp_path / "workers"
    workers_dir.mkdir(parents=True)
    (workers_dir / "summarizer.yaml").write_text("name: summarizer\n")

    inputs = "\n".join(
        [
            "test_pipe",  # pipeline name
            "summarizer",  # worker_type
            "Bad-Stage",  # stage_name (INVALID — uppercase/hyphen)
            # The "continue" branch loops back to the start of stage
            # construction, so the worker_type prompt fires again.
            "summarizer",  # worker_type (retry)
            "good_stage",  # stage_name (VALID)
            "text=goal.context.text",  # mapping
            "",  # end mapping
            "n",  # no more stages
            "300",  # timeout
        ]
    )
    result = CliRunner().invoke(new, ["pipeline", "--configs-dir", str(tmp_path)], input=inputs)
    assert result.exit_code == 0, result.output
    # Error message surfaced.
    assert "lowercase" in result.output or "underscores" in result.output

    config = yaml.safe_load((tmp_path / "orchestrators" / "test_pipe.yaml").read_text())
    assert config["pipeline_stages"][0]["name"] == "good_stage"


def test_new_pipeline_interactive_mapping_format_retry(tmp_path):
    """Malformed mapping pair surfaces an error and re-prompts within the
    inner loop (without restarting the stage).
    """
    workers_dir = tmp_path / "workers"
    workers_dir.mkdir(parents=True)
    (workers_dir / "summarizer.yaml").write_text("name: summarizer\n")

    inputs = "\n".join(
        [
            "test_pipe",  # pipeline name
            "summarizer",  # worker_type
            "summarize",  # stage_name
            "no_equals_sign",  # INVALID mapping pair
            "text=goal.context.text",  # VALID mapping pair
            "",  # end mapping
            "n",  # no more stages
            "300",  # timeout
        ]
    )
    result = CliRunner().invoke(new, ["pipeline", "--configs-dir", str(tmp_path)], input=inputs)
    assert result.exit_code == 0, result.output
    assert "field=path" in result.output

    config = yaml.safe_load((tmp_path / "orchestrators" / "test_pipe.yaml").read_text())
    assert config["pipeline_stages"][0]["input_mapping"]["text"] == "goal.context.text"


def test_new_pipeline_interactive_first_stage_default_mapping(tmp_path):
    """First stage with empty mapping line falls back to text=goal.context.text."""
    workers_dir = tmp_path / "workers"
    workers_dir.mkdir(parents=True)
    (workers_dir / "summarizer.yaml").write_text("name: summarizer\n")

    inputs = "\n".join(
        [
            "test_pipe",  # pipeline name
            "summarizer",  # worker_type
            "summarize",  # stage_name
            "",  # no mapping entered (uses default)
            "n",  # no more stages
            "300",  # timeout
        ]
    )
    result = CliRunner().invoke(new, ["pipeline", "--configs-dir", str(tmp_path)], input=inputs)
    assert result.exit_code == 0, result.output
    assert "default: text=goal.context.text" in result.output

    config = yaml.safe_load((tmp_path / "orchestrators" / "test_pipe.yaml").read_text())
    assert config["pipeline_stages"][0]["input_mapping"] == {"text": "goal.context.text"}


# ---------------------------------------------------------------------------
# End-to-end: scaffold a worker, then validate it through heddle validate.
# Pins the "scaffold output is always validateable" promise from the
# session-starter spec.
# ---------------------------------------------------------------------------


def test_scaffolded_worker_validates_via_cli(tmp_path):
    """Non-interactive scaffold + heddle validate round-trip on the same file."""
    from heddle.cli.main import cli

    runner = CliRunner()

    # Step 1: scaffold a worker.
    scaffold = runner.invoke(
        new,
        [
            "worker",
            "--non-interactive",
            "--name",
            "scaffold_demo",
            "--kind",
            "llm",
            "--tier",
            "local",
            "--configs-dir",
            str(tmp_path),
        ],
    )
    assert scaffold.exit_code == 0, scaffold.output

    # Step 2: validate the generated file through the public CLI.
    dest = tmp_path / "workers" / "scaffold_demo.yaml"
    validated = runner.invoke(cli, ["validate", str(dest)])
    assert validated.exit_code == 0, validated.output


def test_scaffolded_pipeline_validates_via_cli(tmp_path):
    """Non-interactive pipeline scaffold + heddle validate round-trip."""
    from heddle.cli.main import cli

    # The pipeline scaffold references a worker — write a minimal valid one.
    workers_dir = tmp_path / "workers"
    workers_dir.mkdir(parents=True)
    worker_yaml = {
        "name": "summarizer",
        "system_prompt": "Summarize the input.",
        "input_schema": {
            "type": "object",
            "required": ["text"],
            "properties": {"text": {"type": "string"}},
        },
        "output_schema": {
            "type": "object",
            "required": ["summary"],
            "properties": {"summary": {"type": "string"}},
        },
        "default_model_tier": "local",
        "max_input_tokens": 8000,
        "max_output_tokens": 1000,
        "timeout_seconds": 30,
    }
    (workers_dir / "summarizer.yaml").write_text(yaml.safe_dump(worker_yaml))

    runner = CliRunner()
    scaffold = runner.invoke(
        new,
        [
            "pipeline",
            "--non-interactive",
            "--name",
            "scaffold_pipe",
            "--configs-dir",
            str(tmp_path),
        ],
    )
    assert scaffold.exit_code == 0, scaffold.output

    dest = tmp_path / "orchestrators" / "scaffold_pipe.yaml"
    validated = runner.invoke(cli, ["validate", str(dest)])
    assert validated.exit_code == 0, validated.output
