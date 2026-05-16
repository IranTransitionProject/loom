"""
TaskWorker base class.

Extracts the reusable worker lifecycle from the LLM-specific worker:
message parsing, I/O contract validation, result publishing, timing,
and error handling. Subclasses implement process() to do the actual work.
"""

from __future__ import annotations

import time
from abc import abstractmethod
from typing import Any

import structlog
import yaml

from heddle.core.actor import BaseActor
from heddle.core.config import resolve_schema_refs
from heddle.core.contracts import validate_input, validate_output
from heddle.core.messages import TaskMessage, TaskResult, TaskStatus
from heddle.tracing.metrics import record_task_completed
from heddle.tracing.otel import inject_trace_context

logger = structlog.get_logger()


class TaskWorker(BaseActor):
    """
    Generic stateless worker base.

    Lifecycle per message:
    1. Receive TaskMessage
    2. Validate input against worker contract
    3. Delegate to process() (subclass implements)
    4. Validate output against worker contract
    5. Publish TaskResult
    6. Reset (no state retained)
    """

    def __init__(
        self,
        actor_id: str,
        config_path: str,
        nats_url: str = "nats://nats:4222",
    ) -> None:
        super().__init__(actor_id, nats_url)
        self._config_path = config_path
        self.config = self._load_config(config_path)

    def _load_config(self, path: str) -> dict[str, Any]:
        with open(path) as f:
            config = yaml.safe_load(f)
        return resolve_schema_refs(config)

    async def on_reload(self) -> None:
        """Re-read the worker config from disk on reload signal."""
        self.config = self._load_config(self._config_path)
        logger.info("worker.config_reloaded", config_path=self._config_path)

    async def handle_message(self, data: dict[str, Any]) -> None:
        """Handle an incoming task message through the full worker lifecycle."""
        task = TaskMessage(**data)
        start = time.monotonic()

        log = logger.bind(
            task_id=task.task_id,
            worker_type=task.worker_type,
            model_tier=task.model_tier.value,
        )

        try:
            # 1. Validate input
            errors = validate_input(task.payload, self.config.get("input_schema", {}))
            if errors:
                await self._publish_result(
                    task, TaskStatus.FAILED, error=f"Input validation: {errors}"
                )
                return

            # 2. Delegate to subclass — inject model_tier into metadata
            #    so process() can resolve the correct LLM backend.
            enriched_metadata = {**task.metadata, "model_tier": task.model_tier.value}
            result = await self.process(task.payload, enriched_metadata)

            # 3. Validate output
            output = result["output"]
            output_errors = validate_output(output, self.config.get("output_schema", {}))
            if output_errors:
                await self._publish_result(
                    task,
                    TaskStatus.FAILED,
                    error=f"Output validation: {output_errors}",
                    model_used=result.get("model_used"),
                    tokens=result.get("token_usage"),
                )
                return

            # 4. Publish success
            elapsed = int((time.monotonic() - start) * 1000)
            await self._publish_result(
                task,
                TaskStatus.COMPLETED,
                output=output,
                model_used=result.get("model_used"),
                tokens=result.get("token_usage"),
                metadata=result.get("metadata"),
                elapsed=elapsed,
            )
            log.info("worker.completed", ms=elapsed)

        except Exception as e:
            log.error("worker.exception", error=str(e))
            await self._publish_result(task, TaskStatus.FAILED, error=str(e))
        finally:
            # Reset — worker holds NO state from this task.
            # This is a design invariant, not a suggestion. Any instance
            # variables set during process() must not affect subsequent
            # invocations.  ``finally`` ensures reset runs even when the
            # try-block exits early via ``return`` on input/output
            # validation failure, or when ``_publish_result`` raises.
            await self.reset()

    @abstractmethod
    async def process(self, payload: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
        """Process a task payload. Subclasses implement this.

        Args:
            payload: Validated input dict (matches input_schema).
            metadata: Task metadata dict (routing hints, pipeline context, etc.).

        Returns:
            A dict with the following structure::

                {
                    "output": dict,              # Must match output_schema
                    "model_used": str | None,    # Identifier for what processed this
                    "token_usage": dict | None,  # {"prompt_tokens": int, ...} or empty
                    "metadata": dict | None,     # Optional worker-side observability
                                                 #   (e.g. {"degraded_modes": [...]})
                                                 #   surfaced on TaskResult.metadata
                }
        """
        ...

    async def reset(self) -> None:
        """Post-task cleanup hook for subclasses.

        Called after every task (success or failure) to release temporary
        resources (file handles, caches, scratch buffers). The default
        implementation is a no-op. Override in subclasses that allocate
        per-task resources in process().

        This is the enforcement point for the statelessness invariant:
        any state set during process() must be cleared here.
        """

    async def _publish_result(
        self,
        task: TaskMessage,
        status: TaskStatus,
        output: dict[str, Any] | None = None,
        error: str | None = None,
        model_used: str | None = None,
        tokens: dict[str, int] | None = None,
        metadata: dict[str, Any] | None = None,
        elapsed: int = 0,
    ) -> None:
        result = TaskResult(
            task_id=task.task_id,
            parent_task_id=task.parent_task_id,
            worker_type=task.worker_type,
            status=status,
            output=output,
            error=error,
            model_used=model_used,
            token_usage={
                "prompt_tokens": tokens.get("prompt_tokens", 0) if tokens else 0,
                "completion_tokens": tokens.get("completion_tokens", 0) if tokens else 0,
            },
            metadata=metadata or {},
            processing_time_ms=elapsed,
        )
        # Record the terminal-status metric before publishing. The
        # `status` here is the resolved TaskStatus enum; convert to its
        # string value so the attribute is stable wire-side
        # (a Prometheus/Grafana query against `status="completed"` /
        # `status="failed"` must match across heddle versions).
        record_task_completed(
            worker_type=task.worker_type,
            model_tier=task.model_tier.value,
            status=status.value,
        )
        # Results route back to the orchestrator that dispatched this task.
        # If parent_task_id is None (no orchestrator), results go to "heddle.results.default".
        subject = f"heddle.results.{task.parent_task_id or 'default'}"
        result_dict = result.model_dump(mode="json")
        # Inject _trace_context for return-path propagation symmetry with
        # the outbound TaskMessage. No consumer reads this today; it
        # exists so any future consumer-side span (in dispatch.py or
        # elsewhere) can parent under the worker span correctly.
        inject_trace_context(result_dict)
        await self.publish(subject, result_dict)
