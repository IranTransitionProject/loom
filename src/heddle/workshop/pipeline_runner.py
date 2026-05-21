"""
PipelineTestRunner — execute a pipeline config end-to-end without NATS.

Spins up an :class:`~heddle.bus.memory.InMemoryBus`, a
:class:`~heddle.router.router.TaskRouter`, and one worker actor per unique
``(worker_type, tier)`` pair the pipeline references; then runs a real
:class:`~heddle.orchestrator.pipeline.PipelineOrchestrator` over them and
collects per-stage results into a structured :class:`PipelineTestResult`.

This is the multi-stage analog of
:class:`~heddle.workshop.test_runner.WorkerTestRunner`: same constructor
shape, same ``aclose()`` discipline, but exercises the real orchestrator
code path — input mappings, conditional stages, dependency inference,
parallelism, retry. Mocks plug in at the same seam they do in worker
tests (the backend dict).

Usage::

    from heddle.worker.backends import build_backends_from_env
    from heddle.workshop.pipeline_runner import PipelineTestRunner

    runner = PipelineTestRunner(backends=build_backends_from_env())
    try:
        result = await runner.run(
            "configs/orchestrators/my_pipeline.yaml",
            context={"file_ref": "input.pdf"},
        )
        for stage in result.stage_results:
            print(f"  {stage.stage_name}: {stage.status} ({stage.latency_ms}ms)")
    finally:
        await runner.aclose()
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
import yaml

from heddle.bus.memory import InMemoryBus
from heddle.core.config import load_config, validate_pipeline_config
from heddle.core.envelope import wrap
from heddle.core.messages import OrchestratorGoal, TaskStatus
from heddle.orchestrator.pipeline import PipelineOrchestrator
from heddle.router.router import TaskRouter
from heddle.worker.runner import LLMWorker

if TYPE_CHECKING:
    from datetime import datetime

    from heddle.worker.backends import LLMBackend
    from heddle.worker.base import TaskWorker

logger = structlog.get_logger()

_STAGE_NAME_IN_ERROR = re.compile(r"Stage '([^']+)'")


@dataclass
class StageResult:
    """Per-stage execution result inside a :class:`PipelineTestResult`."""

    stage_name: str
    worker_type: str
    status: str  # "completed" | "failed" | "skipped" | "timeout" | "upstream-failure"
    output: dict[str, Any] | None = None
    error: str | None = None
    latency_ms: int = 0
    token_usage: dict[str, int] = field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass
class PipelineTestResult:
    """Result of running a pipeline config end-to-end against a context."""

    goal_id: str
    success: bool
    final_output: dict[str, Any] | None = None
    stage_results: list[StageResult] = field(default_factory=list)
    total_latency_ms: int = 0
    total_token_usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    @property
    def succeeded_stages(self) -> int:
        """Number of stages whose final status is ``completed``."""
        return sum(1 for s in self.stage_results if s.status == "completed")


def _import_worker_class(dotted: str) -> type[TaskWorker]:
    """Resolve a ``module.path:ClassName`` or ``module.path.ClassName`` reference."""
    if ":" in dotted:
        module_name, class_name = dotted.split(":", 1)
    else:
        module_name, _, class_name = dotted.rpartition(".")
    if not module_name or not class_name:
        raise ValueError(
            f"Worker config 'class' must be a dotted path like 'pkg.module:Class', got: {dotted!r}"
        )
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


class PipelineTestRunner:
    """Execute a pipeline config end-to-end without NATS.

    Owns an :class:`InMemoryBus` across :meth:`run` calls so callers can
    drive the same runner against multiple pipelines without rebuilding
    the bus and backends.  Call :meth:`aclose` to release backends, close
    the bus, and remove any temp files.

    Args:
        backends: Dict mapping tier name → :class:`LLMBackend`. Mirrors
            :class:`WorkerTestRunner`: pass mocks for tests, real
            backends from ``build_backends_from_env()`` for actual use.
        workers_dir: Directory to find worker configs in. Pipeline
            configs reference workers by ``worker_type``; the runner
            resolves them to ``<workers_dir>/<worker_type>.yaml``.
        worker_config_overrides: Optional dict mapping ``worker_type`` →
            already-loaded config dict, bypassing ``workers_dir`` lookup
            for that worker. Useful for tests with synthetic configs.
        default_timeout_seconds: Per-stage timeout. The full-pipeline
            upper bound is this multiplied by ``len(stages)``.
    """

    def __init__(
        self,
        backends: dict[str, LLMBackend],
        *,
        workers_dir: str | Path = "configs/workers",
        worker_config_overrides: dict[str, dict[str, Any]] | None = None,
        default_timeout_seconds: float = 30.0,
    ) -> None:
        self.backends = backends
        self.workers_dir = Path(workers_dir)
        self.worker_config_overrides = worker_config_overrides or {}
        self.default_timeout_seconds = default_timeout_seconds
        self._bus: InMemoryBus | None = None
        self._closed = False
        self._tmp_paths: list[Path] = []

    async def _ensure_bus(self) -> InMemoryBus:
        if self._bus is None:
            self._bus = InMemoryBus()
            await self._bus.connect()
        return self._bus

    def _write_temp_config(self, config: dict[str, Any]) -> Path:
        fd, raw = tempfile.mkstemp(suffix=".yaml", prefix="heddle-pipeline-test-")
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(config, f)
        path = Path(raw)
        self._tmp_paths.append(path)
        return path

    def _resolve_worker_config(self, worker_type: str) -> tuple[dict[str, Any], Path]:
        """Return ``(config_dict, file_path)`` for a worker_type.

        Honours :attr:`worker_config_overrides` first; otherwise reads
        ``<workers_dir>/<worker_type>.yaml``.  Always returns a real
        on-disk path because :class:`TaskWorker` requires one to load
        its config on reload.
        """
        if worker_type in self.worker_config_overrides:
            cfg = self.worker_config_overrides[worker_type]
            path = self._write_temp_config(cfg)
            return cfg, path
        path = self.workers_dir / f"{worker_type}.yaml"
        if not path.exists():
            raise FileNotFoundError(
                f"Worker config for '{worker_type}' not found at {path} "
                f"and not in worker_config_overrides"
            )
        return load_config(str(path)), path

    def _make_worker(
        self,
        worker_type: str,
        tier: str,
        config: dict[str, Any],
        path: Path,
        bus: InMemoryBus,
    ) -> TaskWorker:
        """Instantiate the appropriate :class:`TaskWorker` subclass.

        If the worker config declares a ``class`` field (dotted ref),
        import and instantiate that class.  Otherwise default to
        :class:`LLMWorker`.  The freshly-constructed actor's bus is
        swapped in-place: :class:`LLMWorker` does not thread ``bus=``
        through to :class:`BaseActor`, but the rest of the actor only
        touches ``self._bus`` from ``publish()`` and inside
        ``handle_message``, neither of which we exercise until after
        this swap completes.
        """
        actor_id = f"test-{worker_type}-{tier}"
        cls_ref = config.get("class")
        worker: TaskWorker
        if cls_ref:
            cls = _import_worker_class(cls_ref)
            worker = cls(actor_id=actor_id, config_path=str(path))
        else:
            worker = LLMWorker(
                actor_id=actor_id,
                config_path=str(path),
                backends=self.backends,
            )
        worker._bus = bus  # type: ignore[attr-defined]
        return worker

    async def run(  # noqa: PLR0912, PLR0915 — linear orchestration sequence; splitting hides flow
        self,
        pipeline_config_path: str | Path,
        context: dict[str, Any],
        *,
        instruction: str = "Pipeline test run",
    ) -> PipelineTestResult:
        """Run the pipeline at ``pipeline_config_path`` against ``context``.

        Returns a :class:`PipelineTestResult` even on early failure
        (config errors, unknown worker_type); the ``error`` field
        explains. ``goal_id`` is set to the empty string when failure
        occurs before goal creation.
        """
        if self._closed:
            raise RuntimeError("PipelineTestRunner is closed")

        result = PipelineTestResult(goal_id="", success=False)
        start_mono = time.monotonic()

        # 1. Load + validate pipeline config.
        try:
            pipeline_cfg = load_config(str(pipeline_config_path))
        except FileNotFoundError as e:
            result.error = f"Pipeline config not found: {e}"
            return result
        errors = validate_pipeline_config(pipeline_cfg, str(pipeline_config_path))
        if errors:
            result.error = f"Invalid pipeline config: {'; '.join(errors)}"
            return result

        stages = pipeline_cfg.get("pipeline_stages", [])

        # 2. Collect unique (worker_type, tier) pairs.
        specs: set[tuple[str, str]] = set()
        for stage in stages:
            specs.add((stage["worker_type"], stage.get("tier", "local")))

        # 3. Resolve worker configs up front — fail fast before touching the bus.
        worker_configs: dict[str, tuple[dict[str, Any], Path]] = {}
        for worker_type, _ in specs:
            if worker_type in worker_configs:
                continue
            try:
                worker_configs[worker_type] = self._resolve_worker_config(worker_type)
            except FileNotFoundError as e:
                result.error = str(e)
                return result

        # 4. Bus + router.
        bus = await self._ensure_bus()
        rules_path = self._write_temp_config({"tier_overrides": {}, "rate_limits": {}})
        router = TaskRouter(str(rules_path), bus)
        await router.run()
        router_task = asyncio.create_task(router.process_messages())

        # 5. Wiretap on heddle.tasks.incoming to record task_id → stage_name.
        # The pipeline orchestrator sets metadata.stage_name on every dispatched
        # TaskMessage; workers do not echo it back on the TaskResult, so we have
        # to remember the mapping on the way out to attribute per-stage results
        # on the way back.
        task_to_stage: dict[str, str] = {}
        incoming_tap = await bus.subscribe("heddle.tasks.incoming")

        async def _drain_incoming() -> None:
            async for data in incoming_tap:
                # Task fields live on the body (envelope payload), not the frame.
                body = data.get("payload") or {}
                tid = body.get("task_id")
                meta = body.get("metadata") or {}
                sname = meta.get("stage_name")
                if tid and sname:
                    task_to_stage[tid] = sname

        tap_task = asyncio.create_task(_drain_incoming())

        # 6. Spawn one drive loop per (worker_type, tier).
        worker_tasks: list[asyncio.Task[None]] = []
        for worker_type, tier in specs:
            cfg, path = worker_configs[worker_type]
            worker = self._make_worker(worker_type, tier, cfg, path, bus)
            subject = f"heddle.tasks.{worker_type}.{tier}"
            sub = await bus.subscribe(subject)

            async def _drive(actor: TaskWorker = worker, subscription: Any = sub) -> None:
                async for data in subscription:
                    try:
                        await actor.handle_message(data)
                    except Exception as e:
                        # TaskWorker.handle_message already publishes
                        # FAILED on its own exceptions; this catch is
                        # belt-and-suspenders for bugs above that layer.
                        logger.error(
                            "pipeline_runner.worker_drive_exception",
                            worker_type=actor.config.get("name", "?"),
                            error=str(e),
                        )

            worker_tasks.append(asyncio.create_task(_drive()))

        # 7. Build goal, subscribe to its result subject.
        goal = OrchestratorGoal(instruction=instruction, context=context)
        result.goal_id = goal.goal_id
        result_sub = await bus.subscribe(f"heddle.results.{goal.goal_id}")
        collected: list[dict[str, Any]] = []

        async def _drain_results() -> None:
            async for data in result_sub:
                # Unwrap the WireEnvelope once here so downstream partitioning
                # reads body fields (task_id/status/output/...) directly.
                body = data.get("payload") or data
                collected.append(body)
                if body.get("task_id") == goal.goal_id:
                    return

        drain_task = asyncio.create_task(_drain_results())

        # 8. Drive the orchestrator.
        pipeline = PipelineOrchestrator(
            actor_id="test-pipeline",
            config_path=str(pipeline_config_path),
            bus=bus,
        )
        upper = self.default_timeout_seconds * max(len(stages), 1)
        goal_data = wrap("core.OrchestratorGoal", goal).model_dump(mode="json")
        try:
            await asyncio.wait_for(pipeline.handle_message(goal_data), timeout=upper)
        except TimeoutError:
            result.error = f"Pipeline exceeded upper-bound timeout {upper}s"

        # 9. Let the result-drain task pick up the final result.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(drain_task, timeout=2.0)
        if not drain_task.done():
            drain_task.cancel()
            # gather(..., return_exceptions=True) joins the cancelled
            # task and discards the CancelledError without the static-
            # analyser flagging the bare ``await`` as effect-free.
            await asyncio.gather(drain_task, return_exceptions=True)

        # 10. Tear down workers / router / tap.
        teardown_tasks: tuple[asyncio.Task[Any], ...] = (
            *worker_tasks,
            router_task,
            tap_task,
        )
        for t in teardown_tasks:
            t.cancel()
        await asyncio.gather(*teardown_tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await result_sub.unsubscribe()
        with contextlib.suppress(Exception):
            await incoming_tap.unsubscribe()

        # 11. Partition collected results.
        final_msg: dict[str, Any] | None = None
        per_stage_msgs: dict[str, list[dict[str, Any]]] = {}
        for msg in collected:
            tid = msg.get("task_id")
            if tid == goal.goal_id:
                final_msg = msg
                continue
            if not tid:
                continue
            sname = task_to_stage.get(tid)
            if sname is None:
                continue
            per_stage_msgs.setdefault(sname, []).append(msg)

        # 12. Build per-stage results in declared order.
        top_status = (final_msg or {}).get("status")
        top_error = (final_msg or {}).get("error") or ""
        failing_stage = self._extract_stage_from_error(top_error)
        pipeline_failed = top_status == TaskStatus.FAILED.value or final_msg is None
        failure_seen = False

        for stage in stages:
            sname = stage["name"]
            msgs = per_stage_msgs.get(sname, [])
            sresult = self._pick_representative(msgs)

            if sresult is None:
                if not pipeline_failed:
                    status = "skipped"
                elif failing_stage == sname or (failing_stage is None and not failure_seen):
                    # Stage failed without publishing a TaskResult — typical
                    # when input validation, mapping resolution, or output
                    # validation raised inside the orchestrator before / after
                    # the worker round-trip.
                    status = "failed"
                    failure_seen = True
                else:
                    status = "upstream-failure"
                err = top_error if status == "failed" else None
                if status == "upstream-failure" and not err:
                    err = "upstream-failure: a prior stage in the pipeline failed"
                result.stage_results.append(
                    StageResult(
                        stage_name=sname,
                        worker_type=stage["worker_type"],
                        status=status,
                        error=err,
                    )
                )
                continue

            raw_status = sresult.get("status")
            if raw_status == TaskStatus.COMPLETED.value:
                api_status = "completed"
            else:
                api_status = "failed"
                failure_seen = True

            tokens = sresult.get("token_usage") or {}
            result.stage_results.append(
                StageResult(
                    stage_name=sname,
                    worker_type=stage["worker_type"],
                    status=api_status,
                    output=sresult.get("output"),
                    error=sresult.get("error"),
                    latency_ms=sresult.get("processing_time_ms", 0) or 0,
                    token_usage={
                        "prompt_tokens": int(tokens.get("prompt_tokens", 0) or 0),
                        "completion_tokens": int(tokens.get("completion_tokens", 0) or 0),
                    },
                )
            )

        # 13. Aggregate top-level fields.
        if final_msg is not None:
            result.final_output = final_msg.get("output")
            if top_status == TaskStatus.FAILED.value and not result.error:
                result.error = top_error or "Pipeline failed"
            result.success = top_status == TaskStatus.COMPLETED.value and all(
                s.status in ("completed", "skipped") for s in result.stage_results
            )
        else:
            result.success = False
            if not result.error:
                result.error = "No final pipeline result received"

        result.total_latency_ms = int((time.monotonic() - start_mono) * 1000)
        for s in result.stage_results:
            for k, v in s.token_usage.items():
                result.total_token_usage[k] = result.total_token_usage.get(k, 0) + v

        return result

    @staticmethod
    def _extract_stage_from_error(error: str) -> str | None:
        """Pull the failing stage name out of a PipelineStageError message.

        PipelineStageError formats its messages as ``Stage '<name>' ...``;
        this gives us the failing stage even when the orchestrator never
        published a TaskResult for it (validation / mapping failures).
        """
        m = _STAGE_NAME_IN_ERROR.search(error or "")
        return m.group(1) if m else None

    @staticmethod
    def _pick_representative(msgs: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Pick the result that best represents a stage's outcome.

        Retries produce multiple TaskResults per stage (each with its
        own ``task_id`` but the same ``stage_name`` in the dispatch
        metadata). The successful one — if any — is the canonical
        outcome; otherwise the last failure stands in.
        """
        if not msgs:
            return None
        for m in msgs:
            if m.get("status") == TaskStatus.COMPLETED.value:
                return m
        return msgs[-1]

    async def aclose(self) -> None:
        """Close the bus, close owned backends, remove temp files.

        Idempotent; safe to call multiple times. A failure on one
        resource does not prevent the others from being released.
        """
        self._closed = True
        if self._bus is not None:
            try:
                await self._bus.close()
            except Exception as e:
                logger.warning("pipeline_runner.bus_close_failed", error=str(e))
            self._bus = None
        for tier, backend in list(self.backends.items()):
            try:
                await backend.aclose()
            except Exception as e:
                logger.warning(
                    "pipeline_runner.backend_close_failed",
                    tier=tier,
                    backend=type(backend).__name__,
                    error=str(e),
                )
        for p in self._tmp_paths:
            with contextlib.suppress(Exception):
                p.unlink(missing_ok=True)
        self._tmp_paths.clear()
