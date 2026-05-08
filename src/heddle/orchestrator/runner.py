"""
Orchestrator actor lifecycle -- the "thinking" layer above workers.

The orchestrator is a longer-lived LLM actor that:
- Receives high-level goals (OrchestratorGoal messages)
- Decomposes them into subtasks for workers (via decomposer.py)
- Dispatches subtasks through the router and collects results
- Synthesizes worker outputs into a coherent final answer (via synthesizer.py)
- Performs periodic self-summarization checkpoints (via checkpoint.py)

This differs from PipelineOrchestrator in that it uses an LLM to dynamically
decide which workers to invoke, rather than following a fixed stage sequence.

Message flow::

    heddle.goals.incoming --> OrchestratorActor.handle_message()
        --> GoalDecomposer breaks goal into subtasks
        --> Publishes TaskMessages to heddle.tasks.incoming (one per subtask)
        --> Subscribes to heddle.results.{goal_id} for worker responses
        --> ResultSynthesizer combines results into a coherent answer
        --> Publishes final TaskResult to heddle.results.{goal_id}

Concurrency model:
    The ``max_concurrent_goals`` config setting (default 1) controls how many
    goals a single OrchestratorActor instance can process simultaneously.
    With the default of 1, goals are queued and processed one at a time
    (strict ordering).  Higher values enable concurrent goal processing
    within a single instance.  For horizontal scaling, run multiple
    OrchestratorActor instances with a NATS queue group.

    All mutable state (conversation_history, checkpoint_counter) is per-goal
    inside ``GoalState``, so concurrent goals are fully isolated — no shared
    mutable data, no locks required.

    Within a single goal, subtasks are dispatched concurrently (all published
    to heddle.tasks.incoming at once) and results are collected as they arrive.

State tracking:
    The orchestrator is the ONLY stateful component in Heddle.  It maintains:
    - ``_active_goals``: maps goal_id -> GoalState for in-flight goals

    Each GoalState carries its own ``conversation_history`` and
    ``checkpoint_counter`` so that concurrent goals never interfere.

    Workers and the router are stateless by design.

See Also:
    heddle.orchestrator.pipeline -- PipelineOrchestrator (fixed stage sequence)
    heddle.orchestrator.decomposer -- GoalDecomposer (LLM-based task breakdown)
    heddle.orchestrator.synthesizer -- ResultSynthesizer (result combination)
    heddle.orchestrator.checkpoint -- CheckpointManager (context compression)
    heddle.core.messages -- all message schemas
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog
import yaml

from heddle.core.actor import BaseActor
from heddle.core.messages import (
    OrchestratorGoal,
    TaskMessage,
    TaskResult,
    TaskStatus,
)
from heddle.orchestrator.checkpoint import CheckpointManager
from heddle.orchestrator.decomposer import GoalDecomposer
from heddle.orchestrator.stream import ResultStream
from heddle.orchestrator.synthesizer import ResultSynthesizer
from heddle.tracing import get_tracer

if TYPE_CHECKING:
    from heddle.orchestrator.store import CheckpointStore
    from heddle.worker.backends import LLMBackend

logger = structlog.get_logger()
_tracer = get_tracer("heddle.orchestrator")


# ---------------------------------------------------------------------------
# Internal state container
# ---------------------------------------------------------------------------


@dataclass
class GoalState:
    """Tracks the lifecycle of a single goal through decomposition and collection.

    One ``GoalState`` exists per in-flight goal.  It is created when a goal
    arrives, populated during decomposition, updated as results trickle in,
    and discarded after synthesis completes.

    Conversation history and checkpoint state are per-goal so that concurrent
    goals (``max_concurrent_goals > 1``) maintain fully isolated state — no
    shared mutable data, no locks required.

    Attributes:
        goal: The original ``OrchestratorGoal`` message.
        dispatched_tasks: Maps ``task_id`` -> ``TaskMessage`` for every subtask
            that was published to ``heddle.tasks.incoming``.
        collected_results: Maps ``task_id`` -> ``TaskResult`` for every result
            received on ``heddle.results.{goal_id}``.
        start_time: Monotonic timestamp when processing began.
        conversation_history: Accumulated context entries for checkpoint
            decisions.  Each entry is a compact summary of a completed goal.
        checkpoint_counter: Monotonically increasing checkpoint version
            number for this goal's checkpoint chain.
    """

    goal: OrchestratorGoal
    dispatched_tasks: dict[str, TaskMessage] = field(default_factory=dict)
    collected_results: dict[str, TaskResult] = field(default_factory=dict)
    start_time: float = field(default_factory=time.monotonic)
    conversation_history: list[dict[str, Any]] = field(default_factory=list)
    checkpoint_counter: int = 0

    @property
    def all_collected(self) -> bool:
        """True when every dispatched task has a corresponding result."""
        return len(self.dispatched_tasks) > 0 and len(self.collected_results) >= len(
            self.dispatched_tasks
        )

    @property
    def pending_count(self) -> int:
        """Number of dispatched tasks still awaiting results."""
        return len(self.dispatched_tasks) - len(self.collected_results)


# ---------------------------------------------------------------------------
# Orchestrator actor
# ---------------------------------------------------------------------------


class OrchestratorActor(BaseActor):
    """Dynamic orchestrator actor -- LLM-driven goal decomposition and synthesis.

    Unlike :class:`PipelineOrchestrator` which follows a fixed stage sequence,
    this actor uses an LLM to dynamically reason about which workers to invoke
    and how to combine their results.

    Lifecycle per goal:

    1. **Receive** -- parse the incoming dict as an ``OrchestratorGoal``.
    2. **Decompose** -- call :class:`GoalDecomposer` to break the goal into
       a list of ``TaskMessage`` subtasks.
    3. **Dispatch** -- publish each subtask to ``heddle.tasks.incoming`` so the
       router can forward them to the appropriate workers.
    4. **Collect** -- subscribe to ``heddle.results.{goal_id}`` and gather
       ``TaskResult`` messages until all subtasks have responded or the
       timeout expires.
    5. **Synthesize** -- call :class:`ResultSynthesizer` to combine all
       collected results into a coherent final answer.
    6. **Publish** -- send the synthesized ``TaskResult`` to
       ``heddle.results.{goal_id}`` for the original caller.
    7. **Checkpoint** (optional) -- if the accumulated conversation history
       exceeds the token threshold, compress it via :class:`CheckpointManager`.

    Parameters
    ----------
    actor_id : str
        Unique identifier for this actor instance.
    config_path : str
        Path to the orchestrator YAML config file (e.g.
        ``configs/orchestrators/default.yaml``).
    backend : LLMBackend
        LLM backend used for both decomposition and synthesis.  Typically
        the same backend instance, but could be different tiers.
    nats_url : str
        NATS server URL.
    checkpoint_store : CheckpointStore | None
        Checkpoint persistence backend.  Pass None to disable checkpointing.

    Example:
    -------
    ::

        from heddle.worker.backends import OllamaBackend
        from heddle.contrib.redis.store import RedisCheckpointStore

        backend = OllamaBackend(model="command-r7b:latest")
        store = RedisCheckpointStore("redis://localhost:6379")
        actor = OrchestratorActor(
            actor_id="orchestrator-1",
            config_path="configs/orchestrators/default.yaml",
            backend=backend,
            nats_url="nats://localhost:4222",
            checkpoint_store=store,
        )
        await actor.run("heddle.goals.incoming")
    """

    def __init__(
        self,
        actor_id: str,
        config_path: str,
        backend: LLMBackend,
        nats_url: str = "nats://nats:4222",
        checkpoint_store: CheckpointStore | None = None,
        *,
        bus: Any | None = None,
    ) -> None:
        # Load config first so we can read max_concurrent_goals before
        # passing it to BaseActor.
        self._config_path = config_path
        self.config = self._load_config(config_path)
        max_goals = self.config.get("max_concurrent_goals", 1)
        super().__init__(actor_id, nats_url, max_concurrent=max_goals, bus=bus)
        self.backend = backend

        # Build the decomposer from config-defined available workers.
        # Each entry needs at least "name" and "description".
        available_workers = self.config.get("available_workers", [])
        if not available_workers:
            # Fallback: infer from the system_prompt if no explicit list.
            # The default.yaml lists workers in the system prompt text; callers
            # should provide an explicit list for production use.
            logger.warning(
                "orchestrator.no_available_workers",
                hint="Add 'available_workers' list to orchestrator config",
            )
        self.decomposer = GoalDecomposer.from_worker_configs(
            backend=backend,
            configs=available_workers,
        )

        # Synthesizer uses the same backend for LLM-based synthesis.
        self.synthesizer = ResultSynthesizer(backend=backend)

        # Checkpoint manager -- only initialized if a checkpoint store is provided.
        checkpoint_config = self.config.get("checkpoint", {})
        self._checkpoint_manager: CheckpointManager | None = None
        if checkpoint_store is not None:
            self._checkpoint_manager = CheckpointManager(
                store=checkpoint_store,
                token_threshold=checkpoint_config.get("token_threshold", 50_000),
                recent_window_size=checkpoint_config.get("recent_window", 5),
            )

        # Configurable timeouts and concurrency limits from YAML.
        self._task_timeout: float = float(self.config.get("timeout_seconds", 300))
        self._max_concurrent_tasks: int = self.config.get("max_concurrent_tasks", 5)

        # ---------- Mutable state ----------
        # Active goals being processed.  Keyed by goal_id.
        # With max_concurrent_goals > 1, multiple goals can be in-flight
        # simultaneously.  Each goal's mutable state (conversation_history,
        # checkpoint_counter) is isolated inside its GoalState — no shared
        # mutable data between goals, no locks required.
        self._active_goals: dict[str, GoalState] = {}

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    @staticmethod
    def _load_config(path: str) -> dict[str, Any]:
        """Load orchestrator YAML configuration."""
        from heddle.core.config import resolve_schema_refs

        with open(path) as f:
            config = yaml.safe_load(f)
        return resolve_schema_refs(config)

    async def on_reload(self) -> None:
        """Re-read the orchestrator config from disk on reload signal.

        Updates config-derived settings (timeouts, concurrency limits).
        Does not rebuild the decomposer or synthesizer — those are
        constructed from the backend, which doesn't change at runtime.
        """
        self.config = self._load_config(self._config_path)
        self._task_timeout = float(self.config.get("timeout_seconds", 300))
        self._max_concurrent_tasks = self.config.get("max_concurrent_tasks", 5)
        logger.info("orchestrator.config_reloaded", config_path=self._config_path)

    # ------------------------------------------------------------------
    # Core message handler
    # ------------------------------------------------------------------

    async def handle_message(self, data: dict[str, Any]) -> None:
        """Handle an incoming OrchestratorGoal.

        This is the main entry point called by :meth:`BaseActor._process_one`
        for every message received on ``heddle.goals.incoming``.

        The method orchestrates the full goal lifecycle: parse, decompose,
        dispatch, collect, synthesize, publish.  Errors at any stage result
        in a ``FAILED`` TaskResult published to the goal's result subject.

        Parameters
        ----------
        data : dict[str, Any]
            Raw message dict, expected to conform to
            :class:`OrchestratorGoal` schema.
        """
        # -- 1. Parse --
        try:
            goal = OrchestratorGoal(**data)
        except Exception as e:
            logger.error(
                "orchestrator.parse_error",
                error=str(e),
                data_keys=list(data.keys()),
            )
            return

        log = logger.bind(goal_id=goal.goal_id)
        log.info("orchestrator.goal_received", instruction=goal.instruction[:120])

        goal_state = GoalState(goal=goal)
        self._active_goals[goal.goal_id] = goal_state

        try:
            # -- 2. Decompose --
            with _tracer.start_as_current_span(
                "orchestrator.decompose",
                attributes={"orchestrator.goal_id": goal.goal_id},
            ) as decompose_span:
                subtasks = await self._decompose_goal(goal, log)
                decompose_span.set_attribute(
                    "orchestrator.subtask_count",
                    len(subtasks) if subtasks else 0,
                )

            if not subtasks:
                log.warning("orchestrator.no_subtasks")
                await self._publish_final_result(
                    goal,
                    TaskStatus.FAILED,
                    error="Decomposition produced no subtasks for this goal.",
                )
                return

            # Enforce max concurrent tasks limit.
            #
            # Earlier shape silently truncated the list and dispatched
            # the first N subtasks, then published ``COMPLETED`` —
            # producing a partial result the caller had no way to
            # detect.  We now fail the goal with an explicit error so
            # the caller learns the goal was too large and can either
            # raise the limit or split the goal at submission time.
            #
            # Future enhancement: chunk the dispatch into N-at-a-time
            # batches.  Out of scope for the current fix; the failure
            # is the safe default until that is implemented.
            if len(subtasks) > self._max_concurrent_tasks:
                log.error(
                    "orchestrator.subtask_limit_exceeded",
                    requested=len(subtasks),
                    limit=self._max_concurrent_tasks,
                )
                await self._publish_final_result(
                    goal,
                    TaskStatus.FAILED,
                    error=(
                        f"Decomposition produced {len(subtasks)} subtasks but "
                        f"max_concurrent_tasks={self._max_concurrent_tasks}.  "
                        "Raise the limit or split the goal."
                    ),
                )
                return

            # -- 3. Subscribe → dispatch → collect.
            # Subscription is opened BEFORE dispatch.  NATS is at-most-once,
            # so a fast worker can publish its result onto
            # ``heddle.results.{goal_id}`` between dispatch and the moment
            # we subscribe — losing that result and forcing the goal to
            # time out.  The ``async with`` block makes the ordering
            # explicit and is enforced by ResultStream (iterating without
            # entering the context raises).
            expected_ids = {t.task_id for t in subtasks}
            stream = ResultStream(
                bus=self._bus,
                subject=f"heddle.results.{goal.goal_id}",
                expected_task_ids=expected_ids,
                timeout=self._task_timeout,
            )
            async with stream:
                with _tracer.start_as_current_span(
                    "orchestrator.dispatch",
                    attributes={
                        "orchestrator.goal_id": goal.goal_id,
                        "orchestrator.subtask_count": len(subtasks),
                    },
                ):
                    await self._dispatch_subtasks(goal_state, subtasks, log)

                with _tracer.start_as_current_span(
                    "orchestrator.collect",
                    attributes={
                        "orchestrator.goal_id": goal.goal_id,
                        "orchestrator.expected_count": len(goal_state.dispatched_tasks),
                        "orchestrator.timeout_seconds": self._task_timeout,
                    },
                ) as collect_span:
                    results = await self._collect_results(stream, goal_state, log)
                    collect_span.set_attribute("orchestrator.collected_count", len(results))
                    collect_span.set_attribute(
                        "orchestrator.success_count",
                        sum(1 for r in results if r.status == TaskStatus.COMPLETED),
                    )

            # -- 5. Synthesize --
            with _tracer.start_as_current_span(
                "orchestrator.synthesize",
                attributes={
                    "orchestrator.goal_id": goal.goal_id,
                    "orchestrator.result_count": len(results),
                },
            ) as synth_span:
                synthesis = await self._synthesize_results(goal, results, log)
                synth_span.set_attribute(
                    "orchestrator.confidence",
                    synthesis.get("confidence", "unknown"),
                )

            # -- 6. Publish final result --
            elapsed = int((time.monotonic() - goal_state.start_time) * 1000)
            await self._publish_final_result(
                goal,
                TaskStatus.COMPLETED,
                output=synthesis,
                elapsed=elapsed,
            )
            log.info("orchestrator.goal_completed", ms=elapsed)

            # -- 7. Record in conversation history and check for checkpoint --
            await self._record_in_history(goal_state, results, synthesis)
            await self._maybe_checkpoint(goal_state, log)

        except Exception as e:
            log.error("orchestrator.goal_failed", error=str(e), exc_info=True)
            elapsed = int((time.monotonic() - goal_state.start_time) * 1000)
            await self._publish_final_result(
                goal,
                TaskStatus.FAILED,
                error=f"Orchestrator error: {e}",
                elapsed=elapsed,
            )
        finally:
            # Clean up goal state regardless of outcome.
            self._active_goals.pop(goal.goal_id, None)

    # ------------------------------------------------------------------
    # Step 2: Decomposition
    # ------------------------------------------------------------------

    async def _decompose_goal(
        self,
        goal: OrchestratorGoal,
        log: Any,
    ) -> list[TaskMessage]:
        """Use the GoalDecomposer to break a goal into subtasks.

        Returns:
        -------
        list[TaskMessage]
            Ready-to-dispatch task messages.  May be empty if the LLM
            determines the goal cannot be decomposed.
        """
        log.info("orchestrator.decomposing")
        try:
            subtasks = await self.decomposer.decompose(
                goal=goal.instruction,
                context=goal.context,
                parent_task_id=goal.goal_id,
                priority=goal.priority,
            )
            log.info("orchestrator.decomposed", subtask_count=len(subtasks))
            return subtasks
        except (ValueError, RuntimeError) as e:
            log.error("orchestrator.decomposition_failed", error=str(e))
            raise

    # ------------------------------------------------------------------
    # Step 3: Dispatch
    # ------------------------------------------------------------------

    async def _dispatch_subtasks(
        self,
        goal_state: GoalState,
        subtasks: list[TaskMessage],
        log: Any,
    ) -> None:
        """Publish all subtasks to ``heddle.tasks.incoming`` for the router.

        Each subtask is registered in ``goal_state.dispatched_tasks`` so we
        know which results to expect during collection.

        All subtasks are published concurrently -- the router and workers
        handle parallelism.  There is no dependency ordering here; the
        dynamic orchestrator treats all decomposed subtasks as independent.
        (For sequential dependencies, use PipelineOrchestrator instead.)
        """
        for task in subtasks:
            goal_state.dispatched_tasks[task.task_id] = task
            await self.publish(
                "heddle.tasks.incoming",
                task.model_dump(mode="json"),
            )
            log.info(
                "orchestrator.dispatched",
                task_id=task.task_id,
                worker_type=task.worker_type,
                model_tier=task.model_tier.value,
            )

        log.info(
            "orchestrator.all_dispatched",
            total=len(subtasks),
        )

    # ------------------------------------------------------------------
    # Step 4: Result collection (Strategy A — streaming)
    # ------------------------------------------------------------------

    async def _collect_results(
        self,
        stream: ResultStream,
        goal_state: GoalState,
        log: Any,
    ) -> list[TaskResult]:
        """Iterate an already-started :class:`ResultStream`, recording results.

        The stream MUST already be subscribed (i.e. inside an
        ``async with stream:`` block opened by the caller before any
        subtasks were published).  This split is intentional — it
        prevents the publish-before-subscribe race by construction:
        a caller that forgets ``async with`` will hit a ``RuntimeError``
        on the first iteration rather than silently losing results.

        Collection completes when:

        - All expected results have arrived, OR
        - The stream's configured timeout expires, OR
        - The stream's ``on_result`` callback signals early exit.

        On timeout or early exit, whatever results have been collected
        so far are returned.  The synthesizer handles partial results
        gracefully.

        Parameters
        ----------
        stream : ResultStream
            A stream constructed by the caller and already entered
            (``await stream.start()`` or ``async with stream:``).
        goal_state : GoalState
            The active goal's state container.  Updated in place as
            results arrive.
        log : Any
            Bound structlog logger.

        Returns:
        -------
        list[TaskResult]
            Collected results (may be fewer than expected on timeout
            or early exit).
        """
        log.info(
            "orchestrator.collecting",
            expected=stream.expected_count,
            timeout_seconds=self._task_timeout,
        )

        async for result in stream:
            goal_state.collected_results[result.task_id] = result
            log.info(
                "orchestrator.result_received",
                task_id=result.task_id,
                worker_type=result.worker_type,
                status=result.status.value,
                collected=stream.collected_count,
                expected=stream.expected_count,
            )

        if stream.all_collected:
            log.info("orchestrator.all_results_collected")
        elif stream.timed_out:
            log.warning(
                "orchestrator.collection_timeout",
                collected=stream.collected_count,
                expected=stream.expected_count,
                timeout_seconds=self._task_timeout,
            )
        elif stream.early_exited:
            log.info(
                "orchestrator.collection_early_exit",
                collected=stream.collected_count,
                expected=stream.expected_count,
            )

        return list(stream.collected.values())

    # ------------------------------------------------------------------
    # Step 5: Synthesis
    # ------------------------------------------------------------------

    async def _synthesize_results(
        self,
        goal: OrchestratorGoal,
        results: list[TaskResult],
        log: Any,
    ) -> dict[str, Any]:
        """Combine collected results into a final answer using the synthesizer.

        Uses :meth:`ResultSynthesizer.synthesize` with the goal instruction
        to produce an LLM-driven coherent narrative.  If no LLM backend is
        available, falls back to deterministic merge.

        Returns:
        -------
        dict[str, Any]
            The synthesized output dict, ready for inclusion in the final
            TaskResult.
        """
        log.info(
            "orchestrator.synthesizing",
            result_count=len(results),
            successful=sum(1 for r in results if r.status == TaskStatus.COMPLETED),
            failed=sum(1 for r in results if r.status == TaskStatus.FAILED),
        )

        synthesis = await self.synthesizer.synthesize(
            results,
            goal=goal.instruction,
        )

        log.info(
            "orchestrator.synthesized",
            confidence=synthesis.get("confidence"),
        )
        return synthesis

    # ------------------------------------------------------------------
    # Step 6: Final result publication
    # ------------------------------------------------------------------

    async def _publish_final_result(
        self,
        goal: OrchestratorGoal,
        status: TaskStatus,
        output: dict[str, Any] | None = None,
        error: str | None = None,
        elapsed: int = 0,
    ) -> None:
        """Publish the final orchestration result to ``heddle.results.{goal_id}``.

        This result is consumed by whoever submitted the original goal --
        typically the CLI ``heddle submit`` command or an external system.

        The ``task_id`` is set to the ``goal_id`` so the caller can correlate
        the result with the original goal submission.
        """
        result = TaskResult(
            task_id=goal.goal_id,
            parent_task_id=None,
            worker_type=self.config.get("name", "orchestrator"),
            status=status,
            output=output,
            error=error,
            model_used=None,
            token_usage={},
            processing_time_ms=elapsed,
        )
        subject = f"heddle.results.{goal.goal_id}"
        await self.publish(subject, result.model_dump(mode="json"))

    # ------------------------------------------------------------------
    # Step 7: Conversation history and checkpointing
    # ------------------------------------------------------------------

    async def _record_in_history(
        self,
        goal_state: GoalState,
        results: list[TaskResult],
        synthesis: dict[str, Any],
    ) -> None:
        """Append a goal's lifecycle to its per-goal conversation history.

        Each ``GoalState`` carries its own ``conversation_history`` so that
        concurrent goals maintain fully isolated state.  When the history
        exceeds the token threshold, the CheckpointManager compresses it.

        Each history entry is a compact summary -- not the full result data.
        """
        goal = goal_state.goal
        result_summaries = []
        for r in results:
            summary: dict[str, Any] = {
                "task_id": r.task_id,
                "worker_type": r.worker_type,
                "status": r.status.value,
            }
            if r.status == TaskStatus.COMPLETED and r.output:
                # Store a truncated version of the output to limit history size.
                output_str = json.dumps(r.output, default=str)
                summary["output_preview"] = output_str[:500]
            elif r.status == TaskStatus.FAILED:
                summary["error"] = r.error
            result_summaries.append(summary)

        entry = {
            "goal_id": goal.goal_id,
            "instruction": goal.instruction,
            "subtask_count": len(results),
            "results": result_summaries,
            "synthesis_confidence": synthesis.get("confidence"),
            "timestamp": time.time(),
        }
        goal_state.conversation_history.append(entry)

    async def _maybe_checkpoint(
        self,
        goal_state: GoalState,
        log: Any,
    ) -> None:
        """Check if the goal's conversation history needs compression.

        If a CheckpointManager is configured and the history exceeds the
        token threshold, creates a checkpoint and resets the history to
        only the most recent entries (the "recent window").

        All mutable state (``conversation_history``, ``checkpoint_counter``)
        lives inside the ``GoalState``, so concurrent goals are fully
        isolated — no locks required.
        """
        if self._checkpoint_manager is None:
            return

        goal = goal_state.goal

        if not self._checkpoint_manager.should_checkpoint(goal_state.conversation_history):
            return

        log.info("orchestrator.checkpoint_triggered")
        goal_state.checkpoint_counter += 1

        # Build completed/pending task summaries for the checkpoint.
        completed_tasks = [
            {
                "task_id": r.get("task_id"),
                "worker_type": r.get("worker_type"),
                "status": r.get("status"),
                "summary": r.get("output_preview", r.get("error", "")),
            }
            for entry in goal_state.conversation_history
            for r in entry.get("results", [])
        ]

        try:
            checkpoint = await self._checkpoint_manager.create_checkpoint(
                goal_id=goal.goal_id,
                original_instruction=goal.instruction,
                completed_tasks=completed_tasks,
                pending_tasks=[],  # No pending tasks at checkpoint time
                open_issues=[],
                decisions_made=[],
                checkpoint_number=goal_state.checkpoint_counter,
            )

            # Reset conversation history, keeping only the recent window.
            window = self._checkpoint_manager.recent_window_size
            goal_state.conversation_history = goal_state.conversation_history[-window:]

            log.info(
                "orchestrator.checkpoint_created",
                checkpoint_number=checkpoint.checkpoint_number,
                token_count=checkpoint.context_token_count,
                history_entries_kept=len(goal_state.conversation_history),
            )
        except Exception as e:
            # Checkpoint failure is non-fatal -- the orchestrator continues
            # with a growing history.  The next goal will try again.
            log.error("orchestrator.checkpoint_failed", error=str(e))
