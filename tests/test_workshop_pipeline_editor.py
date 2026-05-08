"""Tests for PipelineEditor (workshop/pipeline_editor.py)."""

from __future__ import annotations

import pytest

from heddle.workshop.pipeline_editor import PipelineEditor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pipeline_config(stages=None):
    """Build a minimal pipeline config."""
    return {
        "name": "test-pipe",
        "pipeline_stages": stages
        or [
            {
                "name": "extract",
                "worker_type": "extractor",
                "tier": "local",
                "input_mapping": {"file_ref": "goal.context.file_ref"},
            },
            {
                "name": "classify",
                "worker_type": "classifier",
                "tier": "local",
                "input_mapping": {"text": "extract.output.text"},
            },
        ],
    }


# ---------------------------------------------------------------------------
# get_dependency_graph
# ---------------------------------------------------------------------------


class TestDependencyGraph:
    def test_sequential_pipeline(self):
        config = _pipeline_config()
        graph = PipelineEditor.get_dependency_graph(config)

        assert graph["stage_count"] == 2
        assert graph["dependencies"]["extract"] == []
        assert graph["dependencies"]["classify"] == ["extract"]
        assert len(graph["levels"]) == 2
        assert graph["levels"][0] == ["extract"]
        assert graph["levels"][1] == ["classify"]

    def test_parallel_stages(self):
        config = _pipeline_config(
            [
                {"name": "a", "worker_type": "w1", "input_mapping": {"f": "goal.context.f"}},
                {"name": "b", "worker_type": "w2", "input_mapping": {"f": "goal.context.f"}},
            ]
        )
        graph = PipelineEditor.get_dependency_graph(config)

        assert graph["stage_count"] == 2
        assert graph["dependencies"]["a"] == []
        assert graph["dependencies"]["b"] == []
        # Both should be in the same level
        assert len(graph["levels"]) == 1
        assert sorted(graph["levels"][0]) == ["a", "b"]

    def test_empty_pipeline(self):
        config = {"name": "empty", "pipeline_stages": []}
        graph = PipelineEditor.get_dependency_graph(config)

        assert graph["stage_count"] == 0
        assert graph["levels"] == []

    def test_diamond_dependency(self):
        """A → B, A → C, B+C → D"""
        config = _pipeline_config(
            [
                {"name": "a", "worker_type": "w", "input_mapping": {"f": "goal.context.f"}},
                {"name": "b", "worker_type": "w", "input_mapping": {"x": "a.output.x"}},
                {"name": "c", "worker_type": "w", "input_mapping": {"x": "a.output.x"}},
                {
                    "name": "d",
                    "worker_type": "w",
                    "input_mapping": {"x": "b.output.x", "y": "c.output.y"},
                },
            ]
        )
        graph = PipelineEditor.get_dependency_graph(config)

        assert graph["levels"][0] == ["a"]
        assert sorted(graph["levels"][1]) == ["b", "c"]
        assert graph["levels"][2] == ["d"]


# ---------------------------------------------------------------------------
# insert_stage
# ---------------------------------------------------------------------------


class TestInsertStage:
    def test_append_at_end(self):
        config = _pipeline_config()
        new_stage = {
            "name": "summarize",
            "worker_type": "summarizer",
            "input_mapping": {"text": "classify.output.category"},
        }

        result = PipelineEditor.insert_stage(config, new_stage)
        names = [s["name"] for s in result["pipeline_stages"]]
        assert names == ["extract", "classify", "summarize"]

    def test_insert_after_specific_stage(self):
        config = _pipeline_config()
        new_stage = {
            "name": "validate",
            "worker_type": "validator",
            "input_mapping": {"text": "extract.output.text"},
        }

        result = PipelineEditor.insert_stage(config, new_stage, after_stage="extract")
        names = [s["name"] for s in result["pipeline_stages"]]
        assert names == ["extract", "validate", "classify"]

    def test_insert_after_nonexistent_stage_raises(self):
        config = _pipeline_config()
        with pytest.raises(ValueError, match="not found"):
            PipelineEditor.insert_stage(config, {"name": "x"}, after_stage="nonexistent")

    def test_insert_does_not_mutate_original(self):
        config = _pipeline_config()
        original_count = len(config["pipeline_stages"])
        PipelineEditor.insert_stage(config, {"name": "new"})
        assert len(config["pipeline_stages"]) == original_count


# ---------------------------------------------------------------------------
# remove_stage
# ---------------------------------------------------------------------------


class TestRemoveStage:
    def test_remove_leaf_stage(self):
        config = _pipeline_config()
        result = PipelineEditor.remove_stage(config, "classify")
        names = [s["name"] for s in result["pipeline_stages"]]
        assert names == ["extract"]

    def test_remove_dependency_raises(self):
        config = _pipeline_config()
        with pytest.raises(ValueError, match="depends on it"):
            PipelineEditor.remove_stage(config, "extract")

    def test_remove_nonexistent_raises(self):
        config = _pipeline_config()
        with pytest.raises(ValueError, match="not found"):
            PipelineEditor.remove_stage(config, "nonexistent")

    def test_remove_does_not_mutate_original(self):
        config = _pipeline_config()
        PipelineEditor.remove_stage(config, "classify")
        assert len(config["pipeline_stages"]) == 2

    def test_remove_with_explicit_depends_on(self):
        config = _pipeline_config(
            [
                {"name": "a", "worker_type": "w", "input_mapping": {"f": "goal.context.f"}},
                {
                    "name": "b",
                    "worker_type": "w",
                    "depends_on": ["a"],
                    "input_mapping": {"f": "goal.context.f"},
                },
            ]
        )
        with pytest.raises(ValueError, match="depends_on"):
            PipelineEditor.remove_stage(config, "a")


# ---------------------------------------------------------------------------
# swap_worker
# ---------------------------------------------------------------------------


class TestSwapWorker:
    def test_swap_worker_type(self):
        config = _pipeline_config()
        result = PipelineEditor.swap_worker(config, "extract", "new_extractor")
        stage = next(s for s in result["pipeline_stages"] if s["name"] == "extract")
        assert stage["worker_type"] == "new_extractor"

    def test_swap_with_tier(self):
        """swap_worker must write ``tier`` — the field PipelineOrchestrator reads.

        Writing ``model_tier`` here would be silently ignored at runtime
        (pipeline.py reads ``stage.get("tier", "local")``), making the
        Workshop's tier swap a no-op.
        """
        config = _pipeline_config()
        result = PipelineEditor.swap_worker(config, "extract", "new_ext", new_tier="standard")
        stage = next(s for s in result["pipeline_stages"] if s["name"] == "extract")
        assert stage["worker_type"] == "new_ext"
        assert stage["tier"] == "standard"
        assert "model_tier" not in stage

    def test_swap_nonexistent_raises(self):
        config = _pipeline_config()
        with pytest.raises(ValueError, match="not found"):
            PipelineEditor.swap_worker(config, "nonexistent", "w")

    def test_swap_does_not_mutate_original(self):
        config = _pipeline_config()
        PipelineEditor.swap_worker(config, "extract", "new")
        assert config["pipeline_stages"][0]["worker_type"] == "extractor"


# ---------------------------------------------------------------------------
# add_parallel_branch
# ---------------------------------------------------------------------------


class TestAddParallelBranch:
    def test_add_independent_stage(self):
        config = _pipeline_config()
        branch = {
            "name": "images",
            "worker_type": "image_extractor",
            "input_mapping": {"file_ref": "goal.context.file_ref"},
        }

        result = PipelineEditor.add_parallel_branch(config, branch)
        assert len(result["pipeline_stages"]) == 3

        graph = PipelineEditor.get_dependency_graph(result)
        # "images" should be in level 0 alongside "extract"
        assert "images" in graph["levels"][0]

    def test_add_dependent_branch_raises(self):
        config = _pipeline_config()
        branch = {
            "name": "dependent",
            "worker_type": "w",
            "input_mapping": {"text": "extract.output.text"},
        }  # Depends on extract

        with pytest.raises(ValueError, match="cannot depend"):
            PipelineEditor.add_parallel_branch(config, branch)


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


class TestValidate:
    def test_valid_pipeline(self):
        config = _pipeline_config()
        assert PipelineEditor.validate(config) == []

    def test_invalid_pipeline_missing_stages(self):
        config = {"name": "bad"}
        errors = PipelineEditor.validate(config)
        assert len(errors) > 0

    def test_valid_empty_stages(self):
        config = {"name": "empty", "pipeline_stages": []}
        assert PipelineEditor.validate(config) == []
