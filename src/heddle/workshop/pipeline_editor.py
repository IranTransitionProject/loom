"""
PipelineEditor — stateless pipeline config manipulation.

All methods operate on config dicts and return modified copies.
No filesystem I/O — use ConfigManager to persist changes.
"""

from __future__ import annotations

import copy
from typing import Any

from heddle.core.config import validate_pipeline_config
from heddle.orchestrator.pipeline import PipelineOrchestrator


class PipelineEditor:
    """Stateless operations for manipulating pipeline stage configs."""

    @staticmethod
    def get_dependency_graph(config: dict[str, Any]) -> dict[str, Any]:
        """Compute the dependency graph and execution levels.

        Returns:
            Dict with:
            - ``dependencies``: {stage_name: [dependency_names]}
            - ``levels``: [[stage_names at level 0], [level 1], ...]
            - ``stage_count``: total number of stages
        """
        stages = config.get("pipeline_stages", [])
        if not stages:
            return {"dependencies": {}, "levels": [], "stage_count": 0}

        deps = PipelineOrchestrator._infer_dependencies(stages)
        levels = PipelineOrchestrator._build_execution_levels(stages, deps)

        return {
            "dependencies": {k: sorted(v) for k, v in deps.items()},
            "levels": [[s["name"] for s in level] for level in levels],
            "stage_count": len(stages),
        }

    @staticmethod
    def insert_stage(
        config: dict[str, Any],
        stage_def: dict[str, Any],
        after_stage: str | None = None,
    ) -> dict[str, Any]:
        """Insert a new stage into the pipeline.

        Args:
            config: Pipeline config dict.
            stage_def: New stage definition (name, worker_type, input_mapping, etc).
            after_stage: Insert after this stage name. None = append at end.

        Returns:
            Modified config copy.
        """
        config = copy.deepcopy(config)
        stages = config.get("pipeline_stages", [])

        if after_stage is None:
            stages.append(stage_def)
        else:
            idx = next(
                (i for i, s in enumerate(stages) if s["name"] == after_stage),
                None,
            )
            if idx is None:
                raise ValueError(f"Stage '{after_stage}' not found in pipeline")
            stages.insert(idx + 1, stage_def)

        config["pipeline_stages"] = stages
        return config

    @staticmethod
    def remove_stage(config: dict[str, Any], stage_name: str) -> dict[str, Any]:
        """Remove a stage from the pipeline.

        Validates that no remaining stages depend on the removed stage.

        Raises:
            ValueError: If other stages depend on the removed stage.
        """
        config = copy.deepcopy(config)
        stages = config.get("pipeline_stages", [])

        # Check if any remaining stage depends on this one
        remaining = [s for s in stages if s["name"] != stage_name]
        if len(remaining) == len(stages):
            raise ValueError(f"Stage '{stage_name}' not found in pipeline")

        # Check dependencies
        for stage in remaining:
            mapping = stage.get("input_mapping", {})
            for source_path in mapping.values():
                first_segment = source_path.split(".")[0]
                if first_segment == stage_name:
                    raise ValueError(
                        f"Cannot remove '{stage_name}': stage '{stage['name']}' "
                        f"depends on it via input_mapping"
                    )
            # Also check explicit depends_on
            depends_on = stage.get("depends_on", [])
            if stage_name in depends_on:
                raise ValueError(
                    f"Cannot remove '{stage_name}': stage '{stage['name']}' "
                    f"has explicit depends_on reference"
                )

        config["pipeline_stages"] = remaining
        return config

    @staticmethod
    def swap_worker(
        config: dict[str, Any],
        stage_name: str,
        new_worker_type: str,
        new_tier: str | None = None,
    ) -> dict[str, Any]:
        """Swap the worker_type (and optionally tier) of a pipeline stage.

        Returns:
            Modified config copy.
        """
        config = copy.deepcopy(config)
        stages = config.get("pipeline_stages", [])

        for stage in stages:
            if stage["name"] == stage_name:
                stage["worker_type"] = new_worker_type
                if new_tier is not None:
                    # Field name must match what PipelineOrchestrator reads
                    # at dispatch time: ``stage.get("tier", "local")``.
                    # Writing ``model_tier`` here was a silent contract drift —
                    # the Workshop appeared to swap tier but the runtime kept
                    # defaulting to ``local``.
                    stage["tier"] = new_tier
                config["pipeline_stages"] = stages
                return config

        raise ValueError(f"Stage '{stage_name}' not found in pipeline")

    @staticmethod
    def add_parallel_branch(
        config: dict[str, Any],
        stage_def: dict[str, Any],
    ) -> dict[str, Any]:
        """Add a stage that runs in parallel (no inter-stage dependencies).

        The stage's input_mapping should reference only ``goal.*`` paths,
        ensuring it has no dependencies on other stages.

        Returns:
            Modified config copy.
        """
        # Validate that the stage only references goal.* paths
        mapping = stage_def.get("input_mapping", {})
        for key, source_path in mapping.items():
            first_segment = source_path.split(".")[0]
            stage_names = {s["name"] for s in config.get("pipeline_stages", [])}
            if first_segment != "goal" and first_segment in stage_names:
                raise ValueError(
                    f"Parallel branch stage cannot depend on other stages. "
                    f"input_mapping key '{key}' references '{first_segment}'"
                )

        config = copy.deepcopy(config)
        config.setdefault("pipeline_stages", []).append(stage_def)
        return config

    @staticmethod
    def validate(config: dict[str, Any]) -> list[str]:
        """Validate a pipeline config including cycle detection.

        Returns list of error strings (empty = valid).
        """
        errors = validate_pipeline_config(config)
        if errors:
            return errors

        # Check for dependency cycles via build_execution_levels
        stages = config.get("pipeline_stages", [])
        if stages:
            deps = PipelineOrchestrator._infer_dependencies(stages)
            try:
                PipelineOrchestrator._build_execution_levels(stages, deps)
            except ValueError as e:
                errors.append(f"Dependency cycle detected: {e}")

        return errors
