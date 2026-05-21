"""
MCP-to-NATS call bridge.

Converts MCP tool invocations into HEDDLE messages (``TaskMessage`` or
``OrchestratorGoal``), publishes them to the NATS bus, waits for the
corresponding ``TaskResult``, and returns the output.

The bridge is transport-agnostic — it accepts any ``MessageBus``
implementation (NATSBus for production, InMemoryBus for testing).

Three call patterns:

- ``call_worker``   — direct worker dispatch via ``heddle.tasks.incoming``
- ``call_pipeline`` — pipeline goal via ``heddle.goals.incoming``
- ``call_query``    — worker dispatch with ``action`` field in payload
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import TYPE_CHECKING, Any, cast

import structlog
from pydantic import ValidationError

from heddle.core.envelope import parse, wrap
from heddle.core.messages import (
    ModelTier,
    OrchestratorGoal,
    TaskMessage,
    TaskResult,
    TaskStatus,
)
from heddle.tracing import get_tracer, inject_trace_context

if TYPE_CHECKING:
    from collections.abc import Callable

    from heddle.bus.base import MessageBus

logger = structlog.get_logger()
_tracer = get_tracer("heddle.mcp.bridge")


class BridgeError(Exception):
    """Raised when a bridge call fails."""


class BridgeTimeoutError(BridgeError):
    """Raised when a bridge call times out."""


class MCPBridge:
    """Bridges MCP tool calls to the HEDDLE actor mesh via NATS.

    Holds a ``MessageBus`` connection and provides three dispatch methods
    corresponding to the three MCP tool kinds: worker, pipeline, query.
    """

    def __init__(self, bus: MessageBus) -> None:
        self.bus = bus

    async def connect(self) -> None:
        """Connect the underlying message bus."""
        await self.bus.connect()

    async def close(self) -> None:
        """Close the underlying message bus."""
        await self.bus.close()

    # ------------------------------------------------------------------
    # Worker dispatch
    # ------------------------------------------------------------------

    async def call_worker(
        self,
        worker_type: str,
        tier: str,
        payload: dict[str, Any],
        timeout: float = 60,
    ) -> dict[str, Any]:
        """Dispatch a task to a HEDDLE worker and wait for the result.

        Publishes a ``TaskMessage`` to ``heddle.tasks.incoming`` (where the
        router picks it up) and subscribes to the result subject.

        Args:
            worker_type: Worker name (matches worker config ``name``).
            tier: Model tier (local, standard, frontier).
            payload: Tool arguments, validated against worker input_schema.
            timeout: Seconds to wait for result.

        Returns:
            The worker's output dict.

        Raises:
            BridgeError: If the worker returns a failed result.
            BridgeTimeoutError: If no result arrives within timeout.
        """
        call_id = _new_id()

        task = TaskMessage(
            worker_type=worker_type,
            input=payload,
            model_tier=ModelTier(tier),
            parent_task_id=call_id,
        )

        result = await self._dispatch_and_wait(
            publish_subject="heddle.tasks.incoming",
            message=wrap("core.TaskMessage", task).model_dump(mode="json"),
            result_subject=f"heddle.results.{call_id}",
            match_task_id=task.task_id,
            timeout=timeout,
        )

        return self._unwrap_result(result)

    # ------------------------------------------------------------------
    # Pipeline dispatch
    # ------------------------------------------------------------------

    async def call_pipeline(
        self,
        goal_context: dict[str, Any],
        timeout: float = 300,
        progress_callback: Callable[[str, int, int], Any] | None = None,
    ) -> dict[str, Any]:
        """Submit an OrchestratorGoal and wait for the pipeline result.

        Args:
            goal_context: Dict of context fields (e.g. ``{file_ref: "doc.pdf"}``).
            timeout: Seconds to wait for the full pipeline.
            progress_callback: Optional ``async/sync (stage_name, stage_idx, total)``
                called as intermediate stage results arrive.

        Returns:
            The pipeline's final output dict (all stage outputs).
        """
        goal = OrchestratorGoal(
            instruction="MCP tool call",
            context=goal_context,
        )

        result_subject = f"heddle.results.{goal.goal_id}"
        sub = await self.bus.subscribe(result_subject)

        await self.bus.publish(
            "heddle.goals.incoming",
            wrap("core.OrchestratorGoal", goal).model_dump(mode="json"),
        )

        logger.info("bridge.pipeline_dispatched", goal_id=goal.goal_id)

        try:
            # The pipeline publishes its final result with task_id == goal_id.
            # Intermediate stage results also arrive on this subject (from
            # individual workers) but with different task_ids.
            final_result = await self._collect_pipeline_results(
                sub,
                goal.goal_id,
                timeout,
                progress_callback,
            )
        finally:
            await sub.unsubscribe()

        return self._unwrap_result(final_result)

    async def _collect_pipeline_results(
        self,
        sub: Any,
        goal_id: str,
        timeout: float,
        progress_callback: Callable | None,
    ) -> TaskResult:
        """Collect results from a pipeline subscription.

        Intermediate stage results (worker_type != pipeline_name, task_id != goal_id)
        trigger progress_callback.  The final result (task_id == goal_id) is returned.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        stage_count = 0

        async def _consume() -> TaskResult:
            nonlocal stage_count
            async for data in sub:
                task_id = (data.get("payload") or {}).get("task_id", "")

                if task_id == goal_id:
                    # This is the final pipeline result.
                    try:
                        _envelope, body = parse(data)
                        return cast("TaskResult", body)
                    except (ValidationError, ValueError, KeyError) as exc:
                        raise BridgeError(
                            f"Malformed pipeline result for goal {goal_id}: {exc}"
                        ) from exc

                # Intermediate stage result — report progress. worker_type is
                # on the body (envelope payload), not the frame.
                worker_type = (data.get("payload") or {}).get("worker_type", "unknown")
                stage_count += 1
                logger.debug(
                    "bridge.intermediate_result",
                    task_id=task_id,
                    worker_type=worker_type,
                    goal_id=goal_id,
                    stage_count=stage_count,
                )
                if progress_callback is not None:
                    stage_name = (data.get("payload") or {}).get(
                        "worker_type", f"stage_{stage_count}"
                    )
                    try:
                        cb_result = progress_callback(stage_name, stage_count, 0)
                        if asyncio.iscoroutine(cb_result):
                            await cb_result
                    except Exception as exc:
                        logger.warning(
                            "bridge.progress_callback_error",
                            stage_name=stage_name,
                            error=str(exc),
                        )

            raise BridgeError("Subscription closed before pipeline completed")

        remaining = deadline - asyncio.get_event_loop().time()
        try:
            return await asyncio.wait_for(_consume(), timeout=max(remaining, 0))
        except TimeoutError as exc:
            raise BridgeTimeoutError(
                f"Pipeline {goal_id} timed out after {timeout}s ({stage_count} stages completed)"
            ) from exc

    # ------------------------------------------------------------------
    # Query dispatch
    # ------------------------------------------------------------------

    async def call_query(
        self,
        worker_type: str,
        action: str,
        payload: dict[str, Any],
        timeout: float = 30,
    ) -> dict[str, Any]:
        """Dispatch a query action to a HEDDLE query worker.

        Wraps the payload with an ``action`` field and dispatches as a
        regular worker task.
        """
        full_payload = {"action": action, **payload}
        return await self.call_worker(
            worker_type=worker_type,
            tier="local",  # Query backends are always local (non-LLM).
            payload=full_payload,
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _dispatch_and_wait(
        self,
        publish_subject: str,
        message: dict[str, Any],
        result_subject: str,
        match_task_id: str,
        timeout: float,
    ) -> TaskResult:
        """Publish a message and wait for a matching TaskResult."""
        result_future: asyncio.Future[TaskResult] = asyncio.get_running_loop().create_future()
        sub = await self.bus.subscribe(result_subject)

        async def _consume() -> None:
            async for data in sub:
                if (data.get("payload") or {}).get("task_id") == match_task_id:
                    try:
                        _envelope, body = parse(data)
                        result = cast("TaskResult", body)
                    except (ValidationError, ValueError, KeyError) as exc:
                        with contextlib.suppress(asyncio.InvalidStateError):
                            result_future.set_exception(
                                BridgeError(f"Malformed result for task {match_task_id}: {exc}")
                            )
                        break
                    with contextlib.suppress(asyncio.InvalidStateError):
                        result_future.set_result(result)
                    break

        consume_task = asyncio.create_task(_consume())

        # Publish after subscribing to avoid missing the response.
        inject_trace_context(message)
        await self.bus.publish(publish_subject, message)
        logger.info("bridge.dispatched", subject=publish_subject, task_id=match_task_id)

        try:
            return await asyncio.wait_for(result_future, timeout=timeout)
        except TimeoutError as exc:
            raise BridgeTimeoutError(
                f"No result for task {match_task_id} within {timeout}s"
            ) from exc
        finally:
            consume_task.cancel()
            await sub.unsubscribe()

    @staticmethod
    def _unwrap_result(result: TaskResult) -> dict[str, Any]:
        """Extract output from a TaskResult, raising on failure."""
        if result.status == TaskStatus.FAILED:
            raise BridgeError(result.error or "Task failed with no error message")
        return result.output or {}


def _new_id() -> str:
    """Generate a short unique ID for call tracking."""
    return f"mcp-{uuid.uuid4().hex[:12]}"
