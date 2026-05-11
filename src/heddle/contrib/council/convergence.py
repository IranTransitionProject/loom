"""Convergence detection for council discussions.

Determines when a multi-round discussion has reached sufficient
agreement to stop.  Three methods:

    - ``none`` — run all rounds, never stop early
    - ``position_stability`` — compare positions across rounds via
      sequence similarity
    - ``llm_judge`` — ask an LLM to rate agreement 0-1

The LLM judge reuses the JSON extraction pattern from
:meth:`heddle.orchestrator.synthesizer.ResultSynthesizer._parse_llm_json`.
"""

from __future__ import annotations

import difflib
import json
from typing import TYPE_CHECKING, Any

import structlog

from heddle.contrib.council.schemas import ConvergenceConfig, ConvergenceResult

if TYPE_CHECKING:
    from heddle.contrib.council.transcript import TranscriptStore
    from heddle.worker.backends import LLMBackend

logger = structlog.get_logger()


class ConvergenceDetector:
    """Checks whether discussion participants have reached consensus."""

    def __init__(
        self,
        config: ConvergenceConfig,
        backend: LLMBackend | None = None,
    ) -> None:
        self._config = config
        self._backend = backend

    async def check(
        self,
        transcript: TranscriptStore,
        round_num: int,
        topic: str,
    ) -> ConvergenceResult:
        """Run the configured convergence check.

        Returns a :class:`ConvergenceResult` indicating whether the
        discussion has converged and the current agreement score.
        """
        method = self._config.method
        if method == "none":
            return self._check_none(round_num)
        if method == "position_stability":
            return self._check_position_stability(transcript, round_num)
        if method == "llm_judge":
            return await self._check_llm_judge(transcript, round_num, topic)
        # Shouldn't reach here due to config validation, but be safe.
        return self._check_none(round_num)

    # ------------------------------------------------------------------
    # Methods
    # ------------------------------------------------------------------

    @staticmethod
    def _check_none(round_num: int) -> ConvergenceResult:
        return ConvergenceResult(
            converged=False,
            score=0.0,
            reason="Convergence checking disabled",
            round_num=round_num,
        )

    def _check_position_stability(
        self,
        transcript: TranscriptStore,
        round_num: int,
    ) -> ConvergenceResult:
        """Compare each agent's latest position with their prior round.

        Uses :func:`difflib.SequenceMatcher.ratio` — a ratio of 1.0
        means the text is identical.  If the average ratio across all
        agents exceeds the threshold, the discussion is converged.
        """
        if round_num < 2:
            return ConvergenceResult(
                converged=False,
                score=0.0,
                reason="Need at least 2 rounds to compare positions",
                round_num=round_num,
            )

        rounds = transcript.rounds

        # Collect per-agent positions for the last two rounds.
        current: dict[str, str] = {}
        previous: dict[str, str] = {}
        for r in rounds:
            if r.round_num == round_num:
                for e in r.entries:
                    current[e.agent_name] = e.content
            elif r.round_num == round_num - 1:
                for e in r.entries:
                    previous[e.agent_name] = e.content

        if not current or not previous:
            return ConvergenceResult(
                converged=False,
                score=0.0,
                reason="Incomplete round data",
                round_num=round_num,
            )

        # Compute average similarity across agents present in both rounds.
        common_agents = set(current) & set(previous)
        if not common_agents:
            return ConvergenceResult(
                converged=False,
                score=0.0,
                reason="No common agents between rounds",
                round_num=round_num,
            )

        ratios = [
            difflib.SequenceMatcher(None, previous[name], current[name]).ratio()
            for name in common_agents
        ]
        avg_ratio = sum(ratios) / len(ratios)
        converged = avg_ratio >= self._config.threshold

        return ConvergenceResult(
            converged=converged,
            score=avg_ratio,
            reason=(f"Position stability: {avg_ratio:.3f} (threshold: {self._config.threshold})"),
            round_num=round_num,
        )

    async def _check_llm_judge(
        self,
        transcript: TranscriptStore,
        round_num: int,
        topic: str,
    ) -> ConvergenceResult:
        """Ask an LLM to rate the level of agreement."""
        if self._backend is None:
            logger.warning("convergence.llm_judge.no_backend")
            return ConvergenceResult(
                converged=False,
                score=0.0,
                reason="No LLM backend configured for convergence checking",
                round_num=round_num,
            )

        # Build the prompt with latest positions.
        positions = transcript.get_latest_positions()
        if not positions:
            return ConvergenceResult(
                converged=False,
                score=0.0,
                reason="No positions to evaluate",
                round_num=round_num,
            )

        positions_text = "\n\n".join(
            f"**{name}**:\n{content}" for name, content in positions.items()
        )
        user_message = (
            f"TOPIC: {topic}\n\n"
            f"PARTICIPANT POSITIONS (Round {round_num}):\n\n"
            f"{positions_text}\n\n"
            f"Rate the overall agreement among participants."
        )

        try:
            response = await self._backend.complete(
                system_prompt=self._config.convergence_prompt
                if hasattr(self._config, "convergence_prompt")
                else (
                    "Rate the level of agreement from 0.0 to 1.0.\n"
                    'Respond with JSON only: {"score": 0.X, "reason": "..."}'
                ),
                user_message=user_message,
                max_tokens=500,
                temperature=0.1,
            )
        except Exception:
            logger.exception("convergence.llm_judge.call_failed")
            return ConvergenceResult(
                converged=False,
                score=0.0,
                reason="LLM judge call failed",
                round_num=round_num,
            )

        raw_content: str = response.get("content", "")
        parsed = _parse_json(raw_content)
        if parsed is None:
            logger.warning(
                "convergence.llm_judge.parse_failed",
                raw_length=len(raw_content),
            )
            return ConvergenceResult(
                converged=False,
                score=0.0,
                reason=f"Failed to parse LLM response: {raw_content[:200]}",
                round_num=round_num,
            )

        # ``float()`` raises on ``{"score": "high"}`` or
        # ``{"score": null}``.  Treat any non-numeric score the same
        # as a JSON parse failure — log + return a parse-failed
        # ``ConvergenceResult`` rather than propagating, matching
        # ADR-004 (skip vs crash on malformed messages).  Earlier
        # the unhandled ValueError/TypeError tore down the entire
        # council.
        raw_score = parsed.get("score", 0.0)
        try:
            score = float(raw_score)
        except (ValueError, TypeError):
            logger.warning(
                "convergence.llm_judge.parse_failed",
                reason="score not numeric",
                raw_score_excerpt=str(raw_score)[:200],
            )
            return ConvergenceResult(
                converged=False,
                score=0.0,
                reason=f"LLM judge returned non-numeric score: {str(raw_score)[:200]}",
                round_num=round_num,
            )
        reason = parsed.get("reason", "")
        converged = score >= self._config.threshold

        logger.info(
            "convergence.llm_judge.result",
            score=score,
            converged=converged,
            round_num=round_num,
        )

        return ConvergenceResult(
            converged=converged,
            score=score,
            reason=reason,
            round_num=round_num,
        )


# -- JSON extraction (from ResultSynthesizer pattern) -------------------


def _parse_json(raw: str) -> dict[str, Any] | None:
    """Extract a JSON object from LLM output, tolerating common wrappers."""
    text = raw.strip()

    # Strip markdown code fences.
    if text.startswith("```"):
        first_nl = text.index("\n") if "\n" in text else len(text)
        text = text[first_nl + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Last resort: find outermost braces.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    return None
