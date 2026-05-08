"""Tests for ConfigManager (workshop/config_manager.py)."""

from __future__ import annotations

import pytest
import yaml

from heddle.workshop.config_manager import ConfigManager
from heddle.workshop.db import WorkshopDB

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_worker(configs_dir, name, config=None):
    """Write a worker config YAML to the workers directory."""
    if config is None:
        config = {
            "name": name,
            "system_prompt": f"You are {name}.",
            "input_schema": {"type": "object", "required": ["text"]},
        }
    workers_dir = configs_dir / "workers"
    workers_dir.mkdir(parents=True, exist_ok=True)
    path = workers_dir / f"{name}.yaml"
    path.write_text(yaml.dump(config))
    return path


def _write_pipeline(configs_dir, name, stages=None):
    """Write a pipeline config YAML to the orchestrators directory."""
    config = {
        "name": name,
        "pipeline_stages": stages
        or [
            {
                "name": "stage_1",
                "worker_type": "extractor",
                "input_mapping": {"file": "goal.context.file"},
            },
        ],
    }
    orch_dir = configs_dir / "orchestrators"
    orch_dir.mkdir(parents=True, exist_ok=True)
    path = orch_dir / f"{name}.yaml"
    path.write_text(yaml.dump(config))
    return path


# ---------------------------------------------------------------------------
# Worker CRUD tests
# ---------------------------------------------------------------------------


class TestWorkerCRUD:
    def test_list_workers(self, tmp_path):
        _write_worker(tmp_path, "summarizer")
        _write_worker(tmp_path, "classifier")
        mgr = ConfigManager(str(tmp_path))

        workers = mgr.list_workers()
        names = [w["name"] for w in workers]
        assert "summarizer" in names
        assert "classifier" in names
        assert len(workers) == 2

    def test_list_workers_skips_template(self, tmp_path):
        _write_worker(tmp_path, "summarizer")
        _write_worker(tmp_path, "_template")
        mgr = ConfigManager(str(tmp_path))

        workers = mgr.list_workers()
        assert len(workers) == 1
        assert workers[0]["name"] == "summarizer"

    def test_list_workers_empty_dir(self, tmp_path):
        mgr = ConfigManager(str(tmp_path))
        assert mgr.list_workers() == []

    def test_get_worker(self, tmp_path):
        config = {"name": "summarizer", "system_prompt": "Summarize.", "description": "Test"}
        _write_worker(tmp_path, "summarizer", config)
        mgr = ConfigManager(str(tmp_path))

        result = mgr.get_worker("summarizer")
        assert result["name"] == "summarizer"
        assert result["system_prompt"] == "Summarize."

    def test_get_worker_not_found(self, tmp_path):
        mgr = ConfigManager(str(tmp_path))
        with pytest.raises(FileNotFoundError):
            mgr.get_worker("nonexistent")

    def test_get_worker_yaml(self, tmp_path):
        _write_worker(tmp_path, "summarizer")
        mgr = ConfigManager(str(tmp_path))
        raw = mgr.get_worker_yaml("summarizer")
        assert "summarizer" in raw

    def test_save_worker(self, tmp_path):
        mgr = ConfigManager(str(tmp_path))
        config = {"name": "new_worker", "system_prompt": "Do stuff."}

        errors = mgr.save_worker("new_worker", config)
        assert errors == []

        # Verify it was written
        loaded = mgr.get_worker("new_worker")
        assert loaded["name"] == "new_worker"

    def test_save_worker_invalid_config(self, tmp_path):
        mgr = ConfigManager(str(tmp_path))
        errors = mgr.save_worker("bad", {"name": "bad"})  # Missing system_prompt
        assert len(errors) > 0

    def test_save_worker_with_db_versioning(self, tmp_path):
        db = WorkshopDB(":memory:")
        mgr = ConfigManager(str(tmp_path), db=db)

        config = {"name": "versioned", "system_prompt": "V1."}
        mgr.save_worker("versioned", config)

        versions = db.get_worker_versions("versioned")
        assert len(versions) == 1
        db.close()

    def test_clone_worker(self, tmp_path):
        _write_worker(tmp_path, "original", {"name": "original", "system_prompt": "Original."})
        mgr = ConfigManager(str(tmp_path))

        errors = mgr.clone_worker("original", "clone")
        assert errors == []

        cloned = mgr.get_worker("clone")
        assert cloned["name"] == "clone"
        assert cloned["system_prompt"] == "Original."

    def test_delete_worker(self, tmp_path):
        _write_worker(tmp_path, "to_delete")
        mgr = ConfigManager(str(tmp_path))

        mgr.delete_worker("to_delete")
        with pytest.raises(FileNotFoundError):
            mgr.get_worker("to_delete")

    def test_delete_worker_not_found(self, tmp_path):
        mgr = ConfigManager(str(tmp_path))
        with pytest.raises(FileNotFoundError):
            mgr.delete_worker("nonexistent")


# ---------------------------------------------------------------------------
# Pipeline CRUD tests
# ---------------------------------------------------------------------------


class TestPipelineCRUD:
    def test_list_pipelines(self, tmp_path):
        _write_pipeline(tmp_path, "rag_pipeline")
        mgr = ConfigManager(str(tmp_path))

        pipelines = mgr.list_pipelines()
        assert len(pipelines) == 1
        assert pipelines[0]["name"] == "rag_pipeline"
        assert pipelines[0]["stage_count"] == 1

    def test_list_pipelines_empty(self, tmp_path):
        mgr = ConfigManager(str(tmp_path))
        assert mgr.list_pipelines() == []

    def test_get_pipeline(self, tmp_path):
        _write_pipeline(tmp_path, "test_pipe")
        mgr = ConfigManager(str(tmp_path))

        result = mgr.get_pipeline("test_pipe")
        assert result["name"] == "test_pipe"
        assert len(result["pipeline_stages"]) == 1

    def test_get_pipeline_not_found(self, tmp_path):
        mgr = ConfigManager(str(tmp_path))
        with pytest.raises(FileNotFoundError):
            mgr.get_pipeline("nonexistent")

    def test_save_pipeline(self, tmp_path):
        mgr = ConfigManager(str(tmp_path))
        config = {
            "name": "new_pipe",
            "pipeline_stages": [
                {"name": "s1", "worker_type": "w1"},
            ],
        }
        errors = mgr.save_pipeline("new_pipe", config)
        assert errors == []

        loaded = mgr.get_pipeline("new_pipe")
        assert loaded["name"] == "new_pipe"

    def test_save_pipeline_invalid(self, tmp_path):
        mgr = ConfigManager(str(tmp_path))
        errors = mgr.save_pipeline("bad", {"name": "bad"})  # Missing pipeline_stages
        assert len(errors) > 0


# ---------------------------------------------------------------------------
# Version history
# ---------------------------------------------------------------------------


class TestVersionHistory:
    def test_version_history_with_db(self, tmp_path):
        db = WorkshopDB(":memory:")
        mgr = ConfigManager(str(tmp_path), db=db)

        mgr.save_worker("w", {"name": "w", "system_prompt": "V1."})
        mgr.save_worker("w", {"name": "w", "system_prompt": "V2."})

        history = mgr.get_worker_version_history("w")
        assert len(history) == 2
        db.close()

    def test_version_history_without_db(self, tmp_path):
        mgr = ConfigManager(str(tmp_path))
        assert mgr.get_worker_version_history("w") == []


# ---------------------------------------------------------------------------
# Path-traversal security
#
# Names flow from URL paths and form fields straight into a filesystem
# path (``configs/workers/{name}.yaml``).  Without validation, the
# workshop's mutating routes accept payloads like
# ``"../../etc/cron.d/evil"`` and read or write outside ``configs_dir``.
# These tests pin the validation contract for every entry point, plus
# the parametrized table of unsafe names that any future entry point
# must also reject.
# ---------------------------------------------------------------------------


_UNSAFE_NAMES = [
    "../escape",
    "../../etc/passwd",
    "..",
    ".",
    ".gitignore",
    "subdir/file",
    "..\\windows",
    "/absolute",
    "with space",
    "with;semicolon",
    "with$dollar",
    "with\x00null",
    "",
    "name.with.dots",  # dots not allowed — could be confused with extensions
]


@pytest.mark.parametrize("bad_name", _UNSAFE_NAMES)
class TestConfigManagerNameValidation:
    """Every entry point that takes a user-supplied name must reject unsafe values.

    Parametrized over a table of attack payloads so a future entry
    point that forgets to validate fails the whole class.
    """

    def test_get_worker_rejects(self, tmp_path, bad_name):
        mgr = ConfigManager(str(tmp_path))
        with pytest.raises(ValueError, match="Invalid config name"):
            mgr.get_worker(bad_name)

    def test_get_worker_yaml_rejects(self, tmp_path, bad_name):
        mgr = ConfigManager(str(tmp_path))
        with pytest.raises(ValueError, match="Invalid config name"):
            mgr.get_worker_yaml(bad_name)

    def test_save_worker_rejects(self, tmp_path, bad_name):
        mgr = ConfigManager(str(tmp_path))
        with pytest.raises(ValueError, match="Invalid config name"):
            mgr.save_worker(bad_name, {"name": "x", "system_prompt": "x"})

    def test_delete_worker_rejects(self, tmp_path, bad_name):
        mgr = ConfigManager(str(tmp_path))
        with pytest.raises(ValueError, match="Invalid config name"):
            mgr.delete_worker(bad_name)

    def test_clone_worker_rejects_source(self, tmp_path, bad_name):
        mgr = ConfigManager(str(tmp_path))
        with pytest.raises(ValueError, match="Invalid config name"):
            mgr.clone_worker(bad_name, "ok")

    def test_clone_worker_rejects_target(self, tmp_path, bad_name):
        # Need a real source so the source check passes.
        _write_worker(tmp_path, "src")
        mgr = ConfigManager(str(tmp_path))
        with pytest.raises(ValueError, match="Invalid config name"):
            mgr.clone_worker("src", bad_name)

    def test_get_worker_version_history_rejects(self, tmp_path, bad_name):
        mgr = ConfigManager(str(tmp_path))
        with pytest.raises(ValueError, match="Invalid config name"):
            mgr.get_worker_version_history(bad_name)

    def test_get_pipeline_rejects(self, tmp_path, bad_name):
        mgr = ConfigManager(str(tmp_path))
        with pytest.raises(ValueError, match="Invalid config name"):
            mgr.get_pipeline(bad_name)

    def test_save_pipeline_rejects(self, tmp_path, bad_name):
        mgr = ConfigManager(str(tmp_path))
        with pytest.raises(ValueError, match="Invalid config name"):
            mgr.save_pipeline(
                bad_name,
                {"name": "x", "pipeline_stages": [{"name": "s", "worker_type": "w"}]},
            )


class TestConfigManagerNameValidationDoesNotEscapeConfigsDir:
    """The validation must actually prevent writes outside configs_dir.

    Belt-and-braces test: even if the regex check were bypassed somehow,
    no file should appear outside ``configs_dir`` after a traversal
    attempt.
    """

    def test_save_worker_traversal_does_not_write_outside(self, tmp_path):
        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()
        outside = tmp_path / "outside.yaml"

        mgr = ConfigManager(str(configs_dir))
        with pytest.raises(ValueError, match="Invalid config name"):
            mgr.save_worker(
                "../outside",
                {"name": "x", "system_prompt": "x"},
            )

        assert not outside.exists(), (
            "save_worker traversal must not produce a file outside configs_dir"
        )

    def test_save_pipeline_traversal_does_not_write_outside(self, tmp_path):
        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()
        outside = tmp_path / "outside.yaml"

        mgr = ConfigManager(str(configs_dir))
        with pytest.raises(ValueError, match="Invalid config name"):
            mgr.save_pipeline(
                "../outside",
                {
                    "name": "x",
                    "pipeline_stages": [{"name": "s", "worker_type": "w"}],
                },
            )

        assert not outside.exists()


class TestConfigManagerAcceptsLegitimateNames:
    """Sanity-check that validation does not reject names actually shipped."""

    @pytest.mark.parametrize(
        "good_name",
        [
            "summarizer",
            "qa",
            "rag_vectorstore_lance",
            "_subprocess_template",
            "worker-with-dashes",
            "MixedCase",
            "name123",
        ],
    )
    def test_accepts(self, tmp_path, good_name):
        mgr = ConfigManager(str(tmp_path))
        # save_worker with a valid config — this round-trips through the
        # validator and should succeed.
        errors = mgr.save_worker(good_name, {"name": good_name, "system_prompt": "x"})
        assert errors == []
        # And get_worker should find what we just saved.
        assert mgr.get_worker(good_name)["name"] == good_name
