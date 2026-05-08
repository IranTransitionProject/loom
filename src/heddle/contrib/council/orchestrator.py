"""CouncilOrchestrator — NATS-connected multi-round deliberation.

Subscribes to ``heddle.goals.incoming`` and runs multi-round council
discussions by dispatching :class:`TaskMessage` objects to existing
Heddle workers.  Each agent turn becomes a standard worker task, so
any worker reachable via the router can participate.

Unlike :class:`CouncilRunner` which calls LLM backends directly, this
orchestrator uses the full actor mesh (router → worker → result) and
produces a final :class:`TaskResult` on the goal's result subject.

Pattern follows :class:`heddle.orchestrator.pipeline.PipelineOrchestrator`
for NATS subscription, result waiting, and final result publishing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import UTC, datetime
from typing import Any

import structlog
import yaml

from heddle.contrib.council.config import CouncilConfig
from heddle.contrib.council.convergence import ConvergenceDetector
from heddle.contrib.council.protocol import get_protocol
from heddle.contrib.council.schemas import TranscriptEntry
from heddle.contrib.council.transcript import TranscriptStore
from heddle.core.actor import BaseActor
from heddle.core.messages import (
    OrchestratorGoal,
    TaskMessage,
    TaskResult,
    TaskStatus,
)
from heddle.tracing import get_tracer, inject_trace_context

logger = structlog.get_logger()
_tracer = get_tracer("heddle.council")


class CouncilOrchestrator(BaseActor):
    """Multi-round council orchestrator using the NATS actor mesh.

    Receives :class:`OrchestratorGoal` messages, runs a multi-round
    discussion by dispatching per-agent tasks to workers, checks
    convergence after each round, and publishes a final synthesized
    :class:`TaskResult`.

    The facilitator's convergence checks and final synthesis use
    ``self._backend`` directly (like the dynamic orchestrator uses
    its backend for decomposition/synthesis).

    Args:
        actor_id: Unique identifier for this actor instance.
        config_path: Path to the council YAML config.
        nats_url: NATS server URL.
        backend: LLM backend for facilitator synthesis and convergence.
        bus: Optional injectable message bus (for testing with InMemoryBus).
    """

    def __init__(
        self,
        actor_id: str,
        config_path: str,
        nats_url: str = "nats://nats:4222",
        *,
        backend: Any | None = None,
        bus: Any | None = None,
    ) -> None:
        self._config_path = config_path
        self.config = self._load_config(config_path)
        super().__init__(actor_id, nats_url, max_concurrent=1, bus=bus)
        self._backend = backend

    @staticmethod
    def _load_config(path: str) -> CouncilConfig:
        with open(path) as f:
            raw = yaml.safe_load(f)
        return CouncilConfig(**raw)

    async def on_reload(self) -> None:
        """Re-read the council config from disk."""
        self.config = self._load_config(self._config_path)
        logger.info("council.config_reloaded", config_path=self._config_path)

    async def handle_message(self, data: dict[str, Any]) -> None:  # noqa: PLR0915
        """Execute a council discussion for an incoming goal."""
        goal = OrchestratorGoal(**data)
        cfg = self.config
        topic = goal.instruction
        start = time.monotonic()

        log = logger.bind(
            goal_id=goal.goal_id,
            council=cfg.name,
            request_id=goal.request_id or goal.goal_id,
        )
        log.info("council.started", agents=len(cfg.agents), max_rounds=cfg.max_rounds)

        protocol = get_protocol(cfg.protocol)
        transcript = TranscriptStore()
        detector = ConvergenceDetector(cfg.convergence, backend=self._backend)

        total_tokens: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        converged = False
        convergence_score: float | None = None
        rounds_completed = 0
        per_turn_timeout = cfg.timeout_seconds / max(cfg.max_rounds * len(cfg.agents), 1)

        try:
            for round_num in range(1, cfg.max_rounds + 1):
                round_log = log.bind(round=round_num)

                with _tracer.start_as_current_span(
                    "council.round",
                    attributes={"council.round": round_num, "council.name": cfg.name},
                ):
                    round_log.info("council.round.start")
                    transcript.start_round(round_num)
                    turns = protocol.get_turn_order(round_num, cfg.agents, transcript)

                    for turn in turns:
                        agent = turn.agent

                        if agent.worker_type is None:
                            # Bridge agents not supported in NATS mode yet.
                            msg = "Bridge agents require CouncilRunner, not CouncilOrchestrator"
                            transcript.add_entry(
                                TranscriptEntry(
                                    round_num=round_num,
                                    agent_name=agent.name,
                                    role=agent.role,
                                    content=f"[{msg}]",
                                    timestamp=datetime.now(UTC),
                                )
                            )
                            continue

                        context = protocol.build_agent_context(agent, transcript, round_num, topic)

                        with _tracer.start_as_current_span(
                            "council.agent_turn",
                            attributes={
                                "council.agent": agent.name,
                                "council.round": round_num,
                            },
                        ):
                            entry = await self._dispatch_agent_turn(
                                agent_name=agent.name,
                                agent_role=agent.role,
                                worker_type=agent.worker_type,
                                tier=agent.tier.value,
                                context=context,
                                round_num=round_num,
                                goal=goal,
                                timeout=per_turn_timeout,
                                log=round_log,
                            )

                        transcript.add_entry(entry)
                        total_tokens["prompt_tokens"] += entry.token_count

                        round_log.info(
                            "council.agent_turn.done",
                            agent=agent.name,
                            model=entry.model_used,
                            tokens=entry.token_count,
                        )

                    # Check convergence.
                    with _tracer.start_as_current_span("council.convergence"):
                        conv_result = await detector.check(transcript, round_num, topic)

                    transcript.set_convergence_score(round_num, conv_result.score)
                    convergence_score = conv_result.score
                    rounds_completed = round_num

                    round_log.info(
                        "council.round.done",
                        convergence_score=conv_result.score,
                        converged=conv_result.converged,
                    )

                    if conv_result.converged:
                        converged = True
                        break

            # Facilitator synthesis.
            with _tracer.start_as_current_span("council.synthesis"):
                synthesis = await self._synthesize(cfg, transcript, topic, total_tokens)

            elapsed = int((time.monotonic() - start) * 1000)
            log.info(
                "council.done",
                rounds=rounds_completed,
                converged=converged,
                elapsed_ms=elapsed,
            )

            # Build output and publish final result.
            output = {
                "topic": topic,
                "rounds_completed": rounds_completed,
                "converged": converged,
                "convergence_score": convergence_score,
                "synthesis": synthesis,
                "agent_summaries": transcript.get_latest_positions(),
                "total_token_usage": total_tokens,
            }

            await self._publish_council_result(
                goal, TaskStatus.COMPLETED, output=output, elapsed=elapsed
            )

        except Exception as e:
            elapsed = int((time.monotonic() - start) * 1000)
            log.error("council.failed", error=str(e), exc_info=True)
            await self._publish_council_result(
                goal, TaskStatus.FAILED, error=str(e), elapsed=elapsed
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _dispatch_agent_turn(
        self,
        agent_name: str,
        agent_role: str,
        worker_type: str,
        tier: str,
        context: dict[str, Any],
        round_num: int,
        goal: OrchestratorGoal,
        timeout: float,
        log: Any,
    ) -> TranscriptEntry:
        """Dispatch a single agent turn via NATS and wait for the result."""
        task = TaskMessage(
            worker_type=worker_type,
            payload=context,
            model_tier=tier,
            parent_task_id=goal.goal_id,
            request_id=goal.request_id or goal.goal_id,
            metadata={
                "council": self.config.name,
                "round": round_num,
                "agent": agent_name,
            },
        )

        # Inject trace context for distributed tracing.
        msg = task.model_dump(mode="json")
        inject_trace_context(msg)

        log.debug(
            "council.task_dispatched",
            task_id=task.task_id,
            worker_type=worker_type,
            agent=agent_name,
        )

        # Subscribe BEFORE publishing.  NATS is at-most-once and a fast
        # worker can publish onto heddle.results.{goal_id} between the
        # publish and the subscription if this ordering is inverted.
        result = await self._dispatch_and_wait_for_result(
            task=task,
            task_data=msg,
            goal_id=goal.goal_id,
            timeout=timeout,
        )

        if result is None:
            return TranscriptEntry(
                round_num=round_num,
                agent_name=agent_name,
                role=agent_role,
                content=f"[Timeout: no response from {worker_type} within {timeout:.0f}s]",
                timestamp=datetime.now(UTC),
            )

        if result.status != TaskStatus.COMPLETED:
            return TranscriptEntry(
                round_num=round_num,
                agent_name=agent_name,
                role=agent_role,
                content=f"[Worker error: {result.error or 'unknown'}]",
                timestamp=datetime.now(UTC),
            )

        # Extract content from worker output.
        output = result.output or {}
        # Workers typically return JSON; extract a text field if available,
        # otherwise serialize the full output.
        content = (
            output.get("content")
            or output.get("response")
            or output.get("synthesis")
            or output.get("summary")
            or json.dumps(output, ensure_ascii=False)
        )

        prompt_tokens = result.token_usage.get("prompt_tokens", 0)
        completion_tokens = result.token_usage.get("completion_tokens", 0)

        return TranscriptEntry(
            round_num=round_num,
            agent_name=agent_name,
            role=agent_role,
            content=content,
            token_count=prompt_tokens + completion_tokens,
            model_used=result.model_used,
            timestamp=datetime.now(UTC),
        )

    async def _dispatch_and_wait_for_result(
        self,
        task: TaskMessage,
        task_data: dict[str, Any],
        goal_id: str,
        timeout: float,
    ) -> TaskResult | None:
        """Subscribe → publish → wait, in that order.

        See :meth:`PipelineOrchestrator._dispatch_and_wait_for_result`
        for the full rationale; this is the council-NATS-mode mirror
        of that helper.  Subscribe-before-publish is mandatory because
        NATS is at-most-once.
        """
        result_future: asyncio.Future[TaskResult] = asyncio.get_running_loop().create_future()
        subject = f"heddle.results.{goal_id}"

        sub = await self._bus.subscribe(subject)

        async def _consume() -> None:
            async for data in sub:
                if data.get("task_id") == task.task_id:
                    with contextlib.suppress(asyncio.InvalidStateError):
                        result_future.set_result(TaskResult(**data))
                    break

        consume_task = asyncio.create_task(_consume())

        try:
            await self.publish("heddle.tasks.incoming", task_data)
            return await asyncio.wait_for(result_future, timeout=timeout)
        except TimeoutError:
            return None
        finally:
            consume_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await consume_task
            await sub.unsubscribe()

    async def _synthesize(
        self,
        config: CouncilConfig,
        transcript: TranscriptStore,
        topic: str,
        total_tokens: dict[str, int],
    ) -> str:
        """Produce the facilitator's final synthesis via direct backend call."""
        if self._backend is None:
            return "[Synthesis unavailable: no backend configured]"

        entries = transcript.get_full_transcript_entries()
        formatted = TranscriptStore.format_for_payload(entries)

        user_message = (
            f"TOPIC: {topic}\n\n"
            f"FULL DISCUSSION TRANSCRIPT:\n\n{formatted}\n\n"
            f"Produce your synthesis now."
        )

        try:
            response = await self._backend.complete(
                system_prompt=config.facilitator.synthesis_prompt,
                user_message=user_message,
                max_tokens=4000,
                temperature=0.2,
            )
        except Exception as e:
            logger.error("council.synthesis.failed", error=str(e))
            return f"[Synthesis failed: {e}]"

        total_tokens["prompt_tokens"] += response.get("prompt_tokens", 0)
        total_tokens["completion_tokens"] += response.get("completion_tokens", 0)

        return response.get("content") or "[Empty synthesis]"

    async def _publish_council_result(
        self,
        goal: OrchestratorGoal,
        status: TaskStatus,
        output: dict | None = None,
        error: str | None = None,
        elapsed: int = 0,
    ) -> None:
        """Publish the final council result to the goal's result subject."""
        result = TaskResult(
            task_id=goal.goal_id,
            parent_task_id=None,
            worker_type=f"council:{self.config.name}",
            status=status,
            output=output,
            error=error,
            model_used=None,
            token_usage={},
            processing_time_ms=elapsed,
        )
        subject = f"heddle.results.{goal.goal_id}"
        await self.publish(subject, result.model_dump(mode="json"))
