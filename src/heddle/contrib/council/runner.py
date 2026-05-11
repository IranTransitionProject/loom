"""CouncilRunner — NATS-free council execution.

Runs a multi-round deliberation directly against LLM backends without
requiring NATS, actors, or running infrastructure.  This is the council
equivalent of :class:`heddle.workshop.test_runner.WorkerTestRunner`.

Usage::

    from heddle.worker.backends import build_backends_from_env
    from heddle.contrib.council.config import load_council_config
    from heddle.contrib.council.runner import CouncilRunner

    config = load_council_config("configs/councils/example.yaml")
    runner = CouncilRunner(build_backends_from_env())
    result = await runner.run("Should we adopt microservices?", config=config)
"""

from __future__ import annotations

import importlib
import json
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from heddle.contrib.council._budget import CouncilTimeoutError, call_with_budget
from heddle.contrib.council.convergence import ConvergenceDetector
from heddle.contrib.council.protocol import get_protocol
from heddle.contrib.council.schemas import (
    CouncilResult,
    TranscriptEntry,
)
from heddle.contrib.council.transcript import TranscriptStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from heddle.contrib.chatbridge.base import ChatBridge
    from heddle.contrib.council.config import CouncilConfig
    from heddle.contrib.council.schemas import AgentConfig
    from heddle.worker.backends import LLMBackend

logger = structlog.get_logger()


class CouncilRunner:
    """Execute a council discussion directly against LLM backends.

    This replicates the multi-round deliberation loop without NATS.
    Each agent turn calls ``backend.complete()`` directly, builds a
    transcript entry, and feeds it into the next round.

    Args:
        backends: Dict mapping tier name (``"local"``, ``"standard"``,
            ``"frontier"``) to :class:`LLMBackend` instances.
        config: Optional default :class:`CouncilConfig`.  Can be
            overridden per-call in :meth:`run`.
    """

    def __init__(
        self,
        backends: dict[str, LLMBackend] | None = None,
        config: CouncilConfig | None = None,
    ) -> None:
        self.backends = backends or {}
        self._default_config = config
        self._active_transcript: TranscriptStore | None = None
        # Cache of ChatBridge instances keyed by agent name.  Each
        # agent with ``bridge`` set gets a dedicated bridge so per-
        # session conversation history is preserved across rounds.
        self._bridges: dict[str, ChatBridge] = {}

    def inject(
        self,
        agent_name: str,
        content: str,
        role: str = "audience",
    ) -> None:
        """Inject a spectator interjection into the active discussion.

        Safe to call from another thread or coroutine while :meth:`run`
        is executing.  The interjection will appear in the next agent's
        context as an audience reaction.

        Raises :class:`RuntimeError` if no discussion is active.
        """
        if self._active_transcript is None:
            msg = "No active council discussion — call run() first"
            raise RuntimeError(msg)
        self._active_transcript.inject_interjection(agent_name, content, role)

    async def run(  # noqa: PLR0915
        self,
        topic: str,
        config: CouncilConfig | None = None,
        on_turn: Callable | None = None,
    ) -> CouncilResult:
        """Run a full council deliberation.

        Args:
            topic: The discussion topic / question.
            config: Council config (overrides the constructor default).
            on_turn: Optional callback invoked after each agent's turn
                with the :class:`TranscriptEntry`.  May be sync or async.

        Returns:
            :class:`CouncilResult` with the full transcript, synthesis,
            convergence info, and token usage.
        """
        cfg = config or self._default_config
        if cfg is None:
            msg = "No council config provided"
            raise ValueError(msg)

        start = time.monotonic()
        log = logger.bind(council=cfg.name, topic=topic[:80])
        log.info("council.start", agents=len(cfg.agents), max_rounds=cfg.max_rounds)

        protocol = get_protocol(cfg.protocol)
        protocol.set_agents(cfg.agents)
        transcript = TranscriptStore()
        self._active_transcript = transcript
        convergence_backend = self.backends.get(cfg.convergence.backend_tier.value)
        detector = ConvergenceDetector(cfg.convergence, backend=convergence_backend)

        total_tokens: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        converged = False
        convergence_score: float | None = None
        rounds_completed = 0
        # Per-turn budget — same formula the orchestrator uses, validated
        # at config-load time against the 5s floor.  A wedged backend
        # would otherwise starve the runner indefinitely (the bug B1
        # closed: the orchestrator path enforced a budget via
        # ``dispatch_and_wait_for_result``, the runner path did not).
        per_turn_timeout = cfg.per_turn_timeout()

        for round_num in range(1, cfg.max_rounds + 1):
            round_log = log.bind(round=round_num)
            round_log.info("council.round.start")

            transcript.start_round(round_num)
            turns = protocol.get_turn_order(round_num, cfg.agents, transcript)

            for turn in turns:
                agent = turn.agent
                context = protocol.build_agent_context(agent, transcript, round_num, topic)

                entry = await self._execute_agent_turn(
                    agent=agent,
                    context=context,
                    round_num=round_num,
                    topic=topic,
                    config=cfg,
                    timeout_seconds=per_turn_timeout,
                )

                transcript.add_entry(entry)

                # Accumulate tokens.
                total_tokens["prompt_tokens"] += entry.token_count
                # token_count tracks prompt tokens; we don't have a
                # separate completion count from backend.complete()
                # in this simplified path.

                if on_turn is not None:
                    result = on_turn(entry)
                    if hasattr(result, "__await__"):
                        await result

                round_log.info(
                    "council.agent_turn.done",
                    agent=agent.name,
                    model=entry.model_used,
                    tokens=entry.token_count,
                )

            # Check convergence.
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
                log.info("council.converged", round=round_num, score=conv_result.score)
                break

        # Facilitator synthesis — budgeted so a wedged backend cannot
        # leave the runner without ever publishing a final result.
        # Mirrors the orchestrator's synthesis timeout contract.
        try:
            synthesis = await call_with_budget(
                self._synthesize(cfg, transcript, topic, total_tokens),
                timeout_seconds=cfg.synthesis_timeout_seconds,
                label="synthesis",
            )
        except CouncilTimeoutError as exc:
            log.warning(
                "council.synthesis.timeout",
                timeout_seconds=exc.timeout_seconds,
            )
            synthesis = f"[Synthesis timed out after {exc.timeout_seconds:.0f}s]"

        # Build agent summaries (latest position per agent).
        agent_summaries = transcript.get_latest_positions()

        elapsed_ms = int((time.monotonic() - start) * 1000)
        log.info(
            "council.done",
            rounds=rounds_completed,
            converged=converged,
            elapsed_ms=elapsed_ms,
        )

        self._active_transcript = None

        return CouncilResult(
            topic=topic,
            rounds_completed=rounds_completed,
            converged=converged,
            convergence_score=convergence_score,
            synthesis=synthesis,
            transcript=transcript.rounds,
            agent_summaries=agent_summaries,
            total_token_usage=total_tokens,
            elapsed_ms=elapsed_ms,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _execute_agent_turn(
        self,
        agent: AgentConfig,
        context: dict[str, Any],
        round_num: int,
        topic: str,
        config: CouncilConfig,
        timeout_seconds: float,
    ) -> TranscriptEntry:
        """Execute a single agent's turn.

        When ``agent.bridge`` is set, the turn is dispatched through
        the configured :class:`ChatBridge` (each agent gets its own
        bridge instance so multi-turn conversations work across
        rounds).  Otherwise the runner falls back to the tier-based
        :class:`LLMBackend` path.

        Both paths are bounded by ``timeout_seconds`` so a wedged
        backend produces a ``[Timeout: ...]`` transcript entry rather
        than hanging the entire deliberation.
        """
        if agent.bridge:
            return await self._execute_via_bridge(
                agent, context, round_num, topic, config, timeout_seconds
            )

        tier = agent.tier.value
        backend = self.backends.get(tier)

        if backend is None:
            available = list(self.backends.keys())
            return TranscriptEntry(
                round_num=round_num,
                agent_name=agent.name,
                role=agent.role,
                content=f"[ERROR: No backend for tier '{tier}'. Available: {available}]",
                timestamp=datetime.now(UTC),
            )

        system_prompt = self._build_agent_prompt(agent, config)
        user_message = json.dumps(context, ensure_ascii=False, indent=2)

        try:
            response = await call_with_budget(
                backend.complete(
                    system_prompt=system_prompt,
                    user_message=user_message,
                    max_tokens=agent.max_tokens_per_turn,
                    temperature=0.3,
                ),
                timeout_seconds=timeout_seconds,
                label=f"agent_turn:{agent.name}",
            )
        except CouncilTimeoutError as exc:
            logger.warning(
                "council.agent_turn.timeout",
                agent=agent.name,
                timeout_seconds=exc.timeout_seconds,
            )
            return TranscriptEntry(
                round_num=round_num,
                agent_name=agent.name,
                role=agent.role,
                content=(
                    f"[Timeout: {agent.name} did not respond within {exc.timeout_seconds:.0f}s]"
                ),
                timestamp=datetime.now(UTC),
            )
        except Exception as e:
            logger.error(
                "council.agent_turn.failed",
                agent=agent.name,
                error=str(e),
            )
            return TranscriptEntry(
                round_num=round_num,
                agent_name=agent.name,
                role=agent.role,
                content=f"[ERROR: {e}]",
                timestamp=datetime.now(UTC),
            )

        content = response.get("content") or ""
        prompt_tokens = response.get("prompt_tokens", 0)
        completion_tokens = response.get("completion_tokens", 0)

        return TranscriptEntry(
            round_num=round_num,
            agent_name=agent.name,
            role=agent.role,
            content=content,
            token_count=prompt_tokens + completion_tokens,
            model_used=response.get("model"),
            timestamp=datetime.now(UTC),
        )

    async def _execute_via_bridge(
        self,
        agent: AgentConfig,
        context: dict[str, Any],
        round_num: int,
        topic: str,
        config: CouncilConfig,
        timeout_seconds: float,
    ) -> TranscriptEntry:
        """Execute a single agent's turn through its :class:`ChatBridge`."""
        try:
            bridge = self._get_or_create_bridge(agent, config)
        except Exception as e:
            logger.error(
                "council.bridge.instantiation_failed",
                agent=agent.name,
                bridge=agent.bridge,
                error=str(e),
            )
            return TranscriptEntry(
                round_num=round_num,
                agent_name=agent.name,
                role=agent.role,
                content=f"[ERROR: failed to instantiate bridge '{agent.bridge}': {e}]",
                timestamp=datetime.now(UTC),
            )

        user_message = json.dumps(context, ensure_ascii=False, indent=2)

        try:
            response = await call_with_budget(
                bridge.send_turn(
                    message=user_message,
                    context={"round_num": round_num, "topic": topic},
                    session_id=agent.name,
                ),
                timeout_seconds=timeout_seconds,
                label=f"agent_turn:{agent.name}",
            )
        except CouncilTimeoutError as exc:
            logger.warning(
                "council.agent_turn.timeout",
                agent=agent.name,
                timeout_seconds=exc.timeout_seconds,
            )
            return TranscriptEntry(
                round_num=round_num,
                agent_name=agent.name,
                role=agent.role,
                content=(
                    f"[Timeout: {agent.name} did not respond within {exc.timeout_seconds:.0f}s]"
                ),
                timestamp=datetime.now(UTC),
            )
        except Exception as e:
            logger.error(
                "council.agent_turn.failed",
                agent=agent.name,
                error=str(e),
            )
            return TranscriptEntry(
                round_num=round_num,
                agent_name=agent.name,
                role=agent.role,
                content=f"[ERROR: {e}]",
                timestamp=datetime.now(UTC),
            )

        usage = response.token_usage or {}
        return TranscriptEntry(
            round_num=round_num,
            agent_name=agent.name,
            role=agent.role,
            content=response.content or "",
            token_count=usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0),
            model_used=response.model,
            timestamp=datetime.now(UTC),
        )

    def _get_or_create_bridge(
        self,
        agent: AgentConfig,
        config: CouncilConfig,
    ) -> ChatBridge:
        """Lazily instantiate (and cache) the :class:`ChatBridge` for one agent.

        ``agent.bridge`` is a dotted path to the bridge class
        (e.g. ``heddle.contrib.chatbridge.openai.OpenAIChatBridge``).
        ``agent.bridge_config`` is forwarded as keyword arguments.
        The agent's role is woven into the bridge's system prompt
        unless ``bridge_config`` already supplies one.
        """
        if agent.name in self._bridges:
            return self._bridges[agent.name]

        if not agent.bridge:
            msg = f"Agent '{agent.name}' has no bridge configured"
            raise ValueError(msg)

        module_path, _, class_name = agent.bridge.rpartition(".")
        if not module_path:
            msg = (
                f"Agent '{agent.name}' bridge '{agent.bridge}' is not a "
                "fully-qualified dotted path (expected e.g. "
                "'heddle.contrib.chatbridge.openai.OpenAIChatBridge')"
            )
            raise ValueError(msg)
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)

        bridge_kwargs = dict(agent.bridge_config)
        bridge_kwargs.setdefault("system_prompt", self._build_agent_prompt(agent, config))
        # Bridges accept ``max_tokens`` as a constructor arg (with
        # their own default).  Honour the agent's per-turn budget
        # unless ``bridge_config`` explicitly overrides it — without
        # this, thinking models can blow well past the agent's
        # declared cap because the bridge's own default wins.
        bridge_kwargs.setdefault("max_tokens", agent.max_tokens_per_turn)

        bridge = cls(**bridge_kwargs)
        self._bridges[agent.name] = bridge
        return bridge

    async def aclose(self) -> None:
        """Close any cached :class:`ChatBridge` sessions.

        ChatBridges may hold open ``httpx.AsyncClient`` connections.
        Call this when you are done with the runner to release them
        cleanly.  Safe to call multiple times.
        """
        for bridge in self._bridges.values():
            close = getattr(bridge, "aclose", None) or getattr(bridge, "close", None)
            if close is None:
                continue
            try:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            except Exception as e:  # pragma: no cover — best-effort cleanup
                logger.warning(
                    "council.bridge.close_failed",
                    bridge=type(bridge).__name__,
                    error=str(e),
                )
        self._bridges.clear()

    @staticmethod
    def _build_agent_prompt(
        agent: AgentConfig,
        config: CouncilConfig,
    ) -> str:
        """Build the system prompt for an agent's turn."""
        parts: list[str] = []

        parts.append(
            f"You are participating in a structured team discussion (council: {config.name})."
        )

        if agent.role:
            parts.append(f"Your role: {agent.role}")

        parts.append(
            "Respond substantively to the topic and context provided.  "
            "Be specific, cite evidence where applicable, and clearly "
            "state your position.  Do NOT wrap your response in JSON — "
            "respond in natural prose."
        )

        return "\n\n".join(parts)

    async def _synthesize(
        self,
        config: CouncilConfig,
        transcript: TranscriptStore,
        topic: str,
        total_tokens: dict[str, int],
    ) -> str:
        """Produce the facilitator's final synthesis."""
        tier = config.facilitator.tier.value
        backend = self.backends.get(tier)

        if backend is None:
            return "[Synthesis unavailable: no backend for facilitator tier]"

        entries = transcript.get_full_transcript_entries()
        formatted = TranscriptStore.format_for_payload(entries)

        user_message = (
            f"TOPIC: {topic}\n\n"
            f"FULL DISCUSSION TRANSCRIPT:\n\n{formatted}\n\n"
            f"Produce your synthesis now."
        )

        try:
            response = await backend.complete(
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
