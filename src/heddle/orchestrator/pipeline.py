"""
Pipeline orchestrator for multi-stage processing with automatic parallelism.

Executes a defined sequence of stages, passing results from each stage
as input to later stages. Each stage maps to a worker_type. Stages can be
LLM workers, processor workers, or any other actor — the pipeline
doesn't care about the implementation, only the message contract.

Stage dependencies are **automatically inferred** from ``input_mapping``
paths: if stage B references ``"A.output.field"``, then B depends on A.
Stages with no inter-stage dependencies (only ``goal.*`` paths) are
independent and execute in parallel. Alternatively, explicit
``depends_on`` lists in the YAML config override automatic inference.

Execution proceeds in *levels* — each level contains stages whose
dependencies are all satisfied by earlier levels. Stages within a level
run concurrently via ``asyncio.wait(FIRST_COMPLETED)`` for incremental
progress reporting.

Pipeline definition comes from YAML config with stages, input mappings,
and optional conditions.

Data flow through the pipeline::

    OrchestratorGoal arrives at handle_message()
        ↓
    context = { "goal": { "instruction": ..., "context": { ... } } }
        ↓
    Build execution levels from stage dependencies (Kahn's algorithm)
        ↓
    For each level:
        For each stage in level (concurrently if >1):
            1. Evaluate condition (skip if false)
            2. Build payload via input_mapping (dot-notation paths into context)
            3. Publish TaskMessage to heddle.tasks.incoming
            4. Wait for TaskResult on heddle.results.{goal_id}
            5. Store result: context[stage_name] = { "output": ..., ... }
        ↓
    Publish final TaskResult with all stage outputs

Input mapping example (from doc_pipeline.yaml)::

    input_mapping:
        text_preview: "extract.output.text_preview"
        metadata: "extract.output.metadata"

This resolves to::

    payload["text_preview"] = context["extract"]["output"]["text_preview"]
    payload["metadata"] = context["extract"]["output"]["metadata"]

See Also:
    heddle.orchestrator.runner — dynamic LLM-based orchestrator
    heddle.core.messages.OrchestratorGoal — the input message type
    configs/orchestrators/ — pipeline config YAML files
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime
from typing import Any

import structlog
import yaml

from heddle.core.actor import BaseActor
from heddle.core.contracts import validate_input, validate_output
from heddle.core.mapping import is_mapping_literal, resolve_mapping_value
from heddle.core.messages import (
    ModelTier,
    OrchestratorGoal,
    TaskMessage,
    TaskResult,
    TaskStatus,
)
from heddle.orchestrator.dispatch import dispatch_and_wait_for_result
from heddle.tracing import get_tracer, inject_trace_context

logger = structlog.get_logger()
_tracer = get_tracer("heddle.pipeline")

HEDDLE_TRACE = bool(os.environ.get("HEDDLE_TRACE"))


def _summarize(data: Any, *, full: bool = HEDDLE_TRACE) -> str:
    """Return a repr of *data*, truncated to 200 chars unless *full* is set."""
    text = repr(data)
    if full:
        return text
    return text[:200]


class PipelineStageError(Exception):
    """Raised when a pipeline stage fails or times out."""

    def __init__(self, stage_name: str, message: str) -> None:
        self.stage_name = stage_name
        super().__init__(message)


class PipelineTimeoutError(PipelineStageError):
    """Raised when a pipeline stage times out waiting for a result."""


class PipelineValidationError(PipelineStageError):
    """Raised when input or output schema validation fails for a stage."""


class PipelineWorkerError(PipelineStageError):
    """Raised when a worker returns FAILED status for a stage."""


class PipelineMappingError(PipelineStageError):
    """Raised when input_mapping resolution fails for a stage."""


class PipelineOrchestrator(BaseActor):
    """
    Pipeline orchestrator with automatic stage parallelism.

    Processes an OrchestratorGoal by running it through a series of stages
    organized into execution levels based on their dependencies. Stages
    within the same level run concurrently; levels execute sequentially.
    Stage outputs are accumulated in a context dict and can be referenced
    by subsequent stages via input_mapping.
    """

    def __init__(
        self,
        actor_id: str,
        config_path: str,
        nats_url: str = "nats://nats:4222",
        *,
        bus: Any | None = None,
    ) -> None:
        self._config_path = config_path
        self.config = self._load_config(config_path)
        max_goals = self.config.get("max_concurrent_goals", 1)
        super().__init__(actor_id, nats_url, max_concurrent=max_goals, bus=bus)

    def _load_config(self, path: str) -> dict:
        from heddle.core.config import resolve_schema_refs

        with open(path) as f:
            config = yaml.safe_load(f)
        return resolve_schema_refs(config)

    async def on_reload(self) -> None:
        """Re-read the pipeline config from disk on reload signal."""
        self.config = self._load_config(self._config_path)
        logger.info("pipeline.config_reloaded", config_path=self._config_path)

    # ------------------------------------------------------------------
    # Dependency inference and execution level construction
    # ------------------------------------------------------------------

    @staticmethod
    def _infer_dependencies(
        stages: list[dict[str, Any]],
    ) -> dict[str, set[str]]:
        """Infer stage dependencies from input_mapping paths.

        For each stage, parse the first segment of every ``input_mapping``
        source path.  If that segment matches another stage's name (and
        is not ``"goal"``), record it as a dependency.

        If a stage has an explicit ``depends_on`` list in its config, that
        takes precedence over automatic inference.

        Returns a dict mapping stage name → set of stage names it depends on.
        """
        stage_names = {s["name"] for s in stages}
        deps: dict[str, set[str]] = {}

        for stage in stages:
            name = stage["name"]

            if "depends_on" in stage:
                # Explicit override — use as-is (filtered to known stages).
                deps[name] = {d for d in stage["depends_on"] if d in stage_names}
                continue

            # Automatic inference from input_mapping paths.  Single-
            # quoted literals are not stage references, so skip them
            # explicitly via ``is_mapping_literal``.  Without the
            # predicate this still worked, but only because a literal
            # like ``"'store'"`` doesn't dot-split into a known stage
            # name — safe by coincidence, not by design.
            mapping = stage.get("input_mapping", {})
            inferred: set[str] = set()
            for source_path in mapping.values():
                if is_mapping_literal(source_path):
                    continue
                first_segment = source_path.split(".")[0]
                if first_segment != "goal" and first_segment in stage_names:
                    inferred.add(first_segment)
            deps[name] = inferred

        return deps

    @staticmethod
    def _build_execution_levels(
        stages: list[dict[str, Any]],
        deps: dict[str, set[str]],
    ) -> list[list[dict[str, Any]]]:
        """Group stages into execution levels using Kahn's algorithm.

        Stages with all dependencies satisfied by earlier levels are placed
        in the same level and can run concurrently.  Within each level,
        stages are sorted alphabetically for deterministic ordering.

        Raises ``ValueError`` if the dependency graph contains a cycle.
        """
        # Build adjacency and in-degree maps.
        stage_by_name = {s["name"]: s for s in stages}
        in_degree: dict[str, int] = {s["name"]: 0 for s in stages}
        dependents: dict[str, list[str]] = {s["name"]: [] for s in stages}

        for name, dep_set in deps.items():
            in_degree[name] = len(dep_set)
            for dep in dep_set:
                dependents[dep].append(name)

        levels: list[list[dict[str, Any]]] = []
        remaining = set(in_degree.keys())

        while remaining:
            # Collect all nodes with in-degree 0 (no unresolved deps).
            ready = sorted(n for n in remaining if in_degree[n] == 0)
            if not ready:
                raise ValueError(f"Circular dependency detected among stages: {sorted(remaining)}")

            level = [stage_by_name[n] for n in ready]
            levels.append(level)

            for n in ready:
                remaining.discard(n)
                for dep in dependents[n]:
                    in_degree[dep] -= 1

        return levels

    # ------------------------------------------------------------------
    # Single-stage execution
    # ------------------------------------------------------------------

    async def _execute_stage(
        self,
        stage: dict[str, Any],
        context: dict[str, Any],
        goal: OrchestratorGoal,
        timeout: float,
        log: Any,
    ) -> tuple[str, dict[str, Any]]:
        """Execute a single pipeline stage: build payload, dispatch, wait.

        Returns ``(stage_name, result_dict)`` on success where result_dict
        has keys ``output``, ``model_used``, ``processing_time_ms``.

        Raises typed ``PipelineStageError`` subclasses on failure:
        ``PipelineMappingError``, ``PipelineValidationError``,
        ``PipelineTimeoutError``, ``PipelineWorkerError``.

        Retries on ``PipelineWorkerError`` and ``PipelineTimeoutError``
        up to ``max_retries`` times (from stage config, default 0).
        """
        stage_name = stage["name"]
        stage_log = log.bind(stage=stage_name)

        with _tracer.start_as_current_span(
            f"pipeline.stage.{stage_name}",
            attributes={
                "pipeline.stage": stage_name,
                "pipeline.worker_type": stage.get("worker_type", ""),
                "pipeline.goal_id": goal.goal_id,
            },
        ) as stage_span:
            return await self._execute_stage_inner(
                stage,
                stage_name,
                context,
                goal,
                timeout,
                stage_log,
                stage_span,
            )

    async def _execute_stage_inner(
        self,
        stage: dict[str, Any],
        stage_name: str,
        context: dict[str, Any],
        goal: OrchestratorGoal,
        timeout: float,
        stage_log: Any,
        stage_span: Any,
    ) -> tuple[str, dict[str, Any]]:
        """Inner stage execution (wrapped by _execute_stage span)."""
        # Check condition (if present) — skipped stages return empty output.
        condition = stage.get("condition")
        if condition and not self._evaluate_condition(condition, context):
            stage_log.info("pipeline.stage_skipped", reason="condition_false")
            stage_span.set_attribute("pipeline.stage_skipped", True)
            return stage_name, {
                "output": None,
                "model_used": None,
                "processing_time_ms": 0,
                "_skipped": True,
            }

        # Build task payload from input_mapping.
        try:
            payload = self._build_stage_payload(stage, context)
        except (KeyError, ValueError) as e:
            msg = f"Stage '{stage_name}' mapping error: {e}"
            raise PipelineMappingError(stage_name, msg) from e

        # Validate payload against stage's input_schema (if declared).
        stage_input_schema = stage.get("input_schema")
        if stage_input_schema:
            errors = validate_input(payload, stage_input_schema)
            if errors:
                raise PipelineValidationError(
                    stage_name,
                    f"Stage '{stage_name}' input validation failed: {errors}",
                )

        # Log stage input summary for tracing.
        stage_log.info("pipeline.stage_input", summary=_summarize(payload))

        # Record stage start time for execution timeline.
        stage_started_at = datetime.now(UTC).isoformat()
        stage_start_mono = time.monotonic()

        # Dispatch and wait, with optional retry for transient errors.
        max_retries = stage.get("max_retries", 0)
        last_error: PipelineStageError | None = None

        for attempt in range(max_retries + 1):
            if attempt > 0:
                stage_log.warning(
                    "pipeline.stage_retry",
                    attempt=attempt,
                    max_retries=max_retries,
                )

            task = TaskMessage(
                worker_type=stage["worker_type"],
                payload=payload,
                model_tier=ModelTier(stage.get("tier", "local")),
                parent_task_id=goal.goal_id,
                request_id=goal.goal_id,
                metadata={
                    "stage_name": stage_name,
                    "model_tier": stage.get("tier", "local"),
                },
            )

            stage_log.info("pipeline.stage_dispatching", worker_type=stage["worker_type"])
            task_data = task.model_dump(mode="json")
            inject_trace_context(task_data)
            stage_timeout = stage.get("timeout_seconds", timeout)
            # Subscribe BEFORE publishing.  NATS is at-most-once — if a
            # fast worker publishes its result before we have an active
            # subscription on heddle.results.{goal_id}, the result is
            # dropped and the stage will time out for no good reason.
            result = await dispatch_and_wait_for_result(
                bus=self._bus,
                task=task,
                task_data=task_data,
                goal_id=goal.goal_id,
                timeout=stage_timeout,
            )

            if result is None:
                last_error = PipelineTimeoutError(
                    stage_name,
                    f"Stage '{stage_name}' timed out after {stage_timeout}s",
                )
                continue

            if result.status == TaskStatus.FAILED:
                last_error = PipelineWorkerError(
                    stage_name,
                    f"Stage '{stage_name}' failed: {result.error}",
                )
                continue

            # Validate result against stage's output_schema (if declared).
            stage_output_schema = stage.get("output_schema")
            if stage_output_schema and result.output is not None:
                output_errors = validate_output(result.output, stage_output_schema)
                if output_errors:
                    raise PipelineValidationError(
                        stage_name,
                        f"Stage '{stage_name}' output validation failed: {output_errors}",
                    )

            stage_log.info("pipeline.stage_output", summary=_summarize(result.output))
            stage_ended_at = datetime.now(UTC).isoformat()
            stage_elapsed_ms = int((time.monotonic() - stage_start_mono) * 1000)
            stage_log.info("pipeline.stage_completed", ms=result.processing_time_ms)
            stage_span.set_attribute("pipeline.wall_time_ms", stage_elapsed_ms)
            stage_span.set_attribute("pipeline.model_used", result.model_used or "")
            stage_span.set_attribute("pipeline.processing_time_ms", result.processing_time_ms)
            return stage_name, {
                "output": result.output,
                "model_used": result.model_used,
                "processing_time_ms": result.processing_time_ms,
                "started_at": stage_started_at,
                "ended_at": stage_ended_at,
                "wall_time_ms": stage_elapsed_ms,
            }

        # All retries exhausted — raise the last error.
        raise last_error  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Core message handler
    # ------------------------------------------------------------------

    async def _execute_parallel_level(
        self,
        level: list[dict[str, Any]],
        context: dict[str, Any],
        goal: OrchestratorGoal,
        timeout: float,
        level_log: Any,
        completed_stage_count: int,
        total_stage_count: int,
    ) -> int:
        """Execute a parallel level: launch stages concurrently, collect incrementally.

        Uses ``asyncio.wait(FIRST_COMPLETED)`` to report progress as each
        stage finishes, rather than waiting for the entire level via
        ``asyncio.gather``.  Context is updated incrementally as stages
        complete, enabling earlier observability.

        Returns the updated ``completed_stage_count``.

        Raises ``PipelineStageError`` (or subclass) if any stage fails.
        """
        level_log.info(
            "pipeline.level_parallel",
            stages=[s["name"] for s in level],
        )

        # Launch all stage coroutines as tasks.
        task_to_stage: dict[asyncio.Task, dict[str, Any]] = {}
        for stage in level:
            coro = self._execute_stage(
                stage,
                context,
                goal,
                timeout,
                level_log,
            )
            task = asyncio.create_task(coro)
            task_to_stage[task] = stage

        pending = set(task_to_stage.keys())
        # task.exception() returns BaseException; in practice workers raise
        # Exception subclasses, but the declared type must match the source.
        first_error: BaseException | None = None

        # Collect results incrementally via FIRST_COMPLETED.
        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                exc = task.exception()
                if exc is not None:
                    if first_error is None:
                        first_error = exc
                    continue

                name, result_dict = task.result()
                if not result_dict.get("_skipped"):
                    context[name] = result_dict
                completed_stage_count += 1
                level_log.info(
                    "pipeline.stage_progress",
                    stage=name,
                    completed=completed_stage_count,
                    total=total_stage_count,
                )

            # On first error, cancel any still-running peers in this level
            # and drain their cancellations.  Without this, a failed level
            # blocked on the slowest sibling — wasting time and tokens on
            # work whose results would be discarded anyway.
            if first_error is not None and pending:
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                pending = set()

        # After all tasks in the level are done, propagate the first error.
        if first_error is not None:
            if isinstance(first_error, PipelineStageError):
                raise first_error
            raise first_error

        return completed_stage_count

    async def handle_message(self, data: dict[str, Any]) -> None:
        """Execute the pipeline for an incoming orchestrator goal."""
        goal = OrchestratorGoal(**data)
        stages = self.config["pipeline_stages"]
        timeout = self.config.get("timeout_seconds", 300)

        log = logger.bind(
            goal_id=goal.goal_id,
            pipeline=self.config["name"],
            request_id=goal.request_id or goal.goal_id,
        )

        # Build execution levels from dependency graph.
        deps = self._infer_dependencies(stages)
        levels = self._build_execution_levels(stages, deps)

        # Log execution plan.
        level_summary = [[s["name"] for s in level] for level in levels]
        log.info(
            "pipeline.started",
            stages=len(stages),
            levels=len(levels),
            execution_plan=level_summary,
        )

        # Accumulated context: goal info + results from each completed stage.
        context: dict[str, Any] = {
            "goal": {
                "instruction": goal.instruction,
                "context": goal.context,
            },
        }

        start = time.monotonic()
        completed_stage_count = 0
        total_stage_count = len(stages)

        try:
            for level_idx, level in enumerate(levels):
                level_log = log.bind(level=level_idx)

                if len(level) == 1:
                    # Single stage — no concurrency overhead.
                    stage = level[0]
                    name, result_dict = await self._execute_stage(
                        stage,
                        context,
                        goal,
                        timeout,
                        level_log,
                    )
                    if not result_dict.get("_skipped"):
                        context[name] = result_dict
                    completed_stage_count += 1
                    level_log.info(
                        "pipeline.stage_progress",
                        completed=completed_stage_count,
                        total=total_stage_count,
                    )
                else:
                    completed_stage_count = await self._execute_parallel_level(
                        level,
                        context,
                        goal,
                        timeout,
                        level_log,
                        completed_stage_count,
                        total_stage_count,
                    )

        except PipelineStageError as e:
            # Build a brief input summary for diagnostics.
            stage_context = context.get("goal", {}).get("context")
            input_summary = repr(stage_context)[:200] if stage_context else ""
            log.error(
                "pipeline.stage_failed",
                stage=e.stage_name,
                error_type=type(e).__name__,
                error=str(e),
                input_summary=input_summary,
            )
            elapsed = int((time.monotonic() - start) * 1000)
            await self._publish_pipeline_result(
                goal,
                TaskStatus.FAILED,
                error=str(e),
                elapsed=elapsed,
            )
            return
        except Exception as e:
            log.error(
                "pipeline.unexpected_error",
                error=str(e),
                error_type=type(e).__name__,
            )
            elapsed = int((time.monotonic() - start) * 1000)
            await self._publish_pipeline_result(
                goal,
                TaskStatus.FAILED,
                error=f"Pipeline error ({type(e).__name__}): {e}",
                elapsed=elapsed,
            )
            return

        # All stages complete.
        elapsed = int((time.monotonic() - start) * 1000)
        log.info("pipeline.completed", ms=elapsed, stages_run=len(context) - 1)

        # Build final output from all stage results.
        final_output = {
            name: data["output"]
            for name, data in context.items()
            if name != "goal" and isinstance(data, dict) and "output" in data
        }

        # Build execution timeline for observability.
        timeline = [
            {
                "stage": name,
                "started_at": data.get("started_at"),
                "ended_at": data.get("ended_at"),
                "wall_time_ms": data.get("wall_time_ms"),
                "processing_time_ms": data.get("processing_time_ms"),
            }
            for name, data in context.items()
            if name != "goal" and isinstance(data, dict) and "started_at" in data
        ]

        await self._publish_pipeline_result(
            goal,
            TaskStatus.COMPLETED,
            output=final_output,
            elapsed=elapsed,
            timeline=timeline,
        )

    # ------------------------------------------------------------------
    # Payload building and path resolution
    # ------------------------------------------------------------------

    def _build_stage_payload(
        self,
        stage: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Build a stage's task payload by resolving input_mapping against context.

        Mapping values are dot-separated paths into the context dict, OR
        single-quoted strings that pass through as literal values.

        Examples:
            "goal.context.file_ref" → context["goal"]["context"]["file_ref"]
            "extract.output.page_count" → context["extract"]["output"]["page_count"]
            "'store'" → "store"  (literal — used by the shipped RAG
                                  pipeline's ``vectorize`` stage to pass
                                  ``action="store"`` without a context key)
        """
        mapping = stage.get("input_mapping", {})
        payload: dict[str, Any] = {}
        for target_field, source_path in mapping.items():
            # ``resolve_mapping_value`` handles both single-quoted
            # literals and dot-paths, sharing one predicate with the
            # validator and dependency inference (see
            # ``heddle.core.mapping``).  Drifting the literal predicate
            # between this site and the validator was the original
            # source of the runtime KeyError fixed in 9f297ca.
            payload[target_field] = resolve_mapping_value(source_path, context)
        return payload

    @staticmethod
    def _resolve_path(path: str, context: dict[str, Any]) -> Any:
        """Resolve a dot-separated path against the context dict."""
        parts = path.split(".")
        current: Any = context
        for part in parts:
            if isinstance(current, dict):
                if part not in current:
                    raise KeyError(f"Path '{path}': key '{part}' not found in context")
                current = current[part]
            else:
                raise ValueError(
                    f"Path '{path}': cannot traverse into {type(current).__name__} at '{part}'"
                )
        return current

    @staticmethod
    def _evaluate_condition(condition: str, context: dict[str, Any]) -> bool:
        """Evaluate a simple condition string against context.

        Supports: "path.to.value == true", "path.to.value == false",
                  "path.to.value != null"

        Note: This is a minimal condition evaluator supporting == and != against
        bool, null, and string literals. If more complex conditions are needed
        (AND/OR, numeric comparisons, regex), consider a safe expression evaluator.
        """
        parts = condition.split()
        if len(parts) != 3:
            logger.warning("pipeline.invalid_condition", condition=condition)
            return True  # Default to running the stage

        path, op, expected = parts
        try:
            value = PipelineOrchestrator._resolve_path(path, context)
        except (KeyError, ValueError):
            logger.warning(
                "pipeline.condition_missing_path",
                condition=condition,
                path=path,
                hint="Path not found in context; condition evaluates to false (stage skipped)",
            )
            return False

        # Normalize expected value
        expected_lower = expected.lower()
        if expected_lower == "true":
            expected_val = True
        elif expected_lower == "false":
            expected_val = False
        elif expected_lower in {"null", "none"}:
            expected_val = None
        else:
            expected_val = expected

        if op == "==":
            return value == expected_val
        if op == "!=":
            return value != expected_val
        logger.warning("pipeline.unsupported_operator", op=op)
        return True

    # ``_dispatch_and_wait_for_result`` was extracted to
    # :func:`heddle.orchestrator.dispatch.dispatch_and_wait_for_result`
    # — see that module for the subscribe-before-publish rationale.

    async def _publish_pipeline_result(
        self,
        goal: OrchestratorGoal,
        status: TaskStatus,
        output: dict | None = None,
        error: str | None = None,
        elapsed: int = 0,
        timeline: list[dict[str, Any]] | None = None,
    ) -> None:
        """Publish the final pipeline result back to the goal's result subject.

        When *timeline* is provided, it is included in the output under the
        ``_timeline`` key so callers (Workshop, MCP bridge) can inspect
        per-stage timing without changing the result schema.
        """
        # Embed timeline in output (if present) for downstream consumers.
        if timeline and output is not None:
            output = {**output, "_timeline": timeline}

        result = TaskResult(
            task_id=goal.goal_id,
            parent_task_id=None,
            worker_type=self.config["name"],
            status=status,
            output=output,
            error=error,
            model_used=None,
            token_usage={},
            processing_time_ms=elapsed,
        )
        subject = f"heddle.results.{goal.goal_id}"
        await self.publish(subject, result.model_dump(mode="json"))
