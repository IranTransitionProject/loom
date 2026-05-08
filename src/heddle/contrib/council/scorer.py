"""Post-council evaluation framework.

Scorers consume a :class:`CouncilResult` and produce a
:class:`ScoringResult` with per-agent scores, judge verdicts, and an
aggregate winner.  This is the post-debate analogue of the in-loop
:class:`heddle.contrib.council.convergence.ConvergenceDetector` —
convergence detection happens during the discussion, scoring happens
after it ends.

Two scorers ship with Heddle:

- :class:`JudgePanelScorer` — adversarial scoring (debate, two-side
  comparison).  Each judge picks a winner and grades them on a
  rubric.  The panel aggregates by majority vote (ties → draws).
  Mirrors Lech Mazur's debate benchmark methodology.

- :class:`RubricScorer` — independent per-participant scoring (blind
  taste test, Q&A panels).  Judges grade *every* participant on every
  rubric dimension — they do not pick a single winner.  The scorer
  anonymizes the transcript at evaluation time (real names →
  ``Participant A/B/C``) so judges grade content, not branding; the
  caller maps aliases back to model identities for the reveal.
"""

from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from collections import Counter
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from heddle.contrib.chatbridge.base import ChatBridge
    from heddle.contrib.council.schemas import CouncilResult, RoundEntry

logger = structlog.get_logger()


DEFAULT_RUBRIC_FIELDS = (
    "argument_quality",
    "rebuttal_strength",
    "evidence_use",
    "rhetorical_skill",
    "responsiveness",
)


def _extract_json_object(content: str) -> dict[str, Any] | None:
    """Best-effort JSON-object extraction from a (possibly noisy) LLM reply.

    Tries, in order:

    1. Strip a markdown ```json``` fence.
    2. Direct ``json.loads`` (clean responses).
    3. Greedy ``{...}`` extraction (response has surrounding prose
       before/after the JSON object — common with verbose judges).

    Returns the parsed dict or ``None`` on any parse failure or
    non-object payload.  Shared by :class:`JudgePanelScorer` and
    :class:`RubricScorer`.
    """
    if not content:
        return None

    text = content.strip()
    fence = re.search(r"```(?:json)?\s*\n(.+?)\n```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    # Try direct parse first — handles clean responses with no extra prose.
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # Fall back to greedy extraction for responses like
    # ``Here is my verdict: {...}\nThanks!``.  The greedy ``.*``
    # captures from the first ``{`` to the LAST ``}``, so a single
    # well-formed JSON object survives surrounding prose.
    obj = re.search(r"\{.*\}", text, re.DOTALL)
    if obj is None:
        return None
    try:
        data = json.loads(obj.group(0))
    except (json.JSONDecodeError, ValueError):
        return None

    return data if isinstance(data, dict) else None


def _coerce_rubric_scores(
    raw: dict[str, Any],
    valid_aliases: set[str],
) -> dict[str, dict[str, float]]:
    """Filter and float-coerce a ``{alias: {dim: score}}`` dict.

    Drops aliases not in ``valid_aliases``, drops dimensions whose
    values are not numeric.  Used by :class:`RubricScorer` to keep
    ``_parse_verdict`` focused on its own control flow.
    """
    clean: dict[str, dict[str, float]] = {}
    for alias, dims in raw.items():
        if alias not in valid_aliases or not isinstance(dims, dict):
            continue
        inner: dict[str, float] = {}
        for dim, val in dims.items():
            try:
                inner[str(dim)] = float(val)
            except (TypeError, ValueError):
                continue
        if inner:
            clean[str(alias)] = inner
    return clean


class AgentScore(BaseModel):
    """Aggregate score for one agent across the judge panel."""

    agent_name: str
    score: float = 0.0  # win rate across judges, 0..1
    rubric: dict[str, float] = Field(default_factory=dict)
    notes: str = ""


class JudgeVerdict(BaseModel):
    """One judge's verdict on a single debate."""

    judge_model: str
    winner: str  # agent name
    margin: float = 0.0  # 0..1, how decisive
    rubric: dict[str, float] = Field(default_factory=dict)
    reasoning: str = ""


class ScoringResult(BaseModel):
    """Aggregated scoring across all judges."""

    council_topic: str
    agent_scores: list[AgentScore] = Field(default_factory=list)
    verdicts: list[JudgeVerdict] = Field(default_factory=list)
    winner: str | None = None  # None means draw
    win_margin: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class Scorer(ABC):
    """Abstract base for council post-hoc scorers."""

    @abstractmethod
    async def score(self, result: CouncilResult) -> ScoringResult:
        """Score a finished council deliberation."""


class JudgePanelScorer(Scorer):
    """Send a transcript to N judges and aggregate verdicts by majority vote.

    Each judge is a :class:`ChatBridge` — typically from a different
    model family than the debaters, to reduce family-bias in scoring.
    Judges return JSON with ``winner`` / ``margin`` / ``rubric`` /
    ``reasoning``.  The panel aggregates by majority vote; ties (equal
    top-vote count, or 1-1-1 splits) are recorded as draws.

    Args:
        judges: List of :class:`ChatBridge` instances (one per judge).
        rubric_fields: Rubric dimensions for the judge to score.
            Defaults to ``DEFAULT_RUBRIC_FIELDS``.
        scoring_prompt: Override the default judge system prompt.
            Must contain ``{transcript}``, ``{topic}``, ``{agents}``,
            and ``{rubric_fields}`` placeholders.
    """

    def __init__(
        self,
        judges: list[ChatBridge],
        rubric_fields: list[str] | None = None,
        scoring_prompt: str | None = None,
    ) -> None:
        if not judges:
            msg = "JudgePanelScorer requires at least one judge"
            raise ValueError(msg)
        self.judges = judges
        self.rubric_fields = list(rubric_fields) if rubric_fields else list(DEFAULT_RUBRIC_FIELDS)
        self.scoring_prompt = scoring_prompt or self._default_prompt()

    async def score(self, result: CouncilResult) -> ScoringResult:
        """Score a finished council via the judge panel."""
        agent_names = sorted({e.agent_name for r in result.transcript for e in r.entries})

        transcript_text = self._format_transcript(result.transcript)
        prompt = self.scoring_prompt.format(
            transcript=transcript_text,
            topic=result.topic,
            agents=", ".join(agent_names),
            rubric_fields=", ".join(self.rubric_fields),
        )

        verdicts = await self._collect_verdicts(prompt)
        winner, win_margin = self._aggregate_verdicts(verdicts)
        agent_scores = self._compute_agent_scores(verdicts, agent_names)

        return ScoringResult(
            council_topic=result.topic,
            agent_scores=agent_scores,
            verdicts=verdicts,
            winner=winner,
            win_margin=win_margin,
            metadata={
                "judge_count": len(self.judges),
                "verdict_count": len(verdicts),
                "rubric_fields": list(self.rubric_fields),
            },
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _collect_verdicts(self, prompt: str) -> list[JudgeVerdict]:
        """Send the scoring prompt to all judges concurrently."""

        async def _one(judge: ChatBridge, idx: int) -> JudgeVerdict | None:
            try:
                response = await judge.send_turn(
                    message=prompt,
                    context={},
                    session_id=f"score-{idx}",
                )
            except Exception as e:
                logger.warning("scorer.judge.failed", judge_index=idx, error=str(e))
                return None
            return self._parse_verdict(response.content, response.model or f"judge-{idx}")

        results = await asyncio.gather(*[_one(j, i) for i, j in enumerate(self.judges)])
        return [v for v in results if v is not None]

    @staticmethod
    def _format_transcript(rounds: list[RoundEntry]) -> str:
        """Convert :class:`RoundEntry` list into readable text.

        Distinguishes panelist turns from audience interjections.
        """
        lines: list[str] = []
        for round_entry in rounds:
            lines.append(f"--- Round {round_entry.round_num} ---")
            for entry in round_entry.entries:
                if entry.entry_type == "interjection":
                    label = f"[AUDIENCE: {entry.agent_name}]"
                else:
                    label = f"[{entry.agent_name.upper()}]"
                lines.append(f"{label}\n{entry.content.strip()}\n")
        return "\n".join(lines)

    @staticmethod
    def _parse_verdict(content: str, judge_model: str) -> JudgeVerdict | None:
        """Extract JSON verdict from a judge response.

        Handles bare JSON and markdown-wrapped (```json ... ```) JSON.
        Returns ``None`` if parsing fails or required fields are missing.
        """
        data = _extract_json_object(content)
        if data is None:
            logger.warning("scorer.verdict.parse_failed", preview=content[:200])
            return None

        if "winner" not in data:
            return None

        # Coerce rubric values one at a time so a single non-numeric entry
        # doesn't void the whole verdict.  Pre-fix: ``{k: float(v) for ...}``
        # ran inside the same try/except as the whole ``JudgeVerdict``
        # construction, so one bad rubric value made ``_parse_verdict``
        # return ``None`` — discarding winner, margin, and reasoning that
        # had parsed correctly.
        rubric_raw = data.get("rubric") or {}
        rubric: dict[str, float] = {}
        for k, v in rubric_raw.items():
            try:
                rubric[k] = float(v)
            except (TypeError, ValueError):
                logger.warning(
                    "scorer.verdict.rubric_value_skipped",
                    key=str(k),
                    value=str(v),
                )

        try:
            return JudgeVerdict(
                judge_model=judge_model,
                winner=str(data["winner"]),
                margin=float(data.get("margin", 0.0)),
                rubric=rubric,
                reasoning=str(data.get("reasoning", "")),
            )
        except (TypeError, ValueError):
            logger.warning("scorer.verdict.coerce_failed", preview=content[:200])
            return None

    @staticmethod
    def _aggregate_verdicts(verdicts: list[JudgeVerdict]) -> tuple[str | None, float]:
        """Majority vote on winner; ties are draws.

        Returns ``(winner_name, avg_margin)``.  ``winner_name`` is
        ``None`` for an empty verdict list, an even split, or a
        three-way (or worse) tie at the top.  ``avg_margin`` averages
        the margins of verdicts that voted for the winner; ``0.0`` for
        draws.
        """
        if not verdicts:
            return None, 0.0

        counts = Counter(v.winner for v in verdicts)
        ranking = counts.most_common()
        top_count = ranking[0][1]
        tied = [name for name, c in ranking if c == top_count]
        if len(tied) > 1:
            return None, 0.0

        winner = ranking[0][0]
        winning_margins = [v.margin for v in verdicts if v.winner == winner]
        avg_margin = sum(winning_margins) / len(winning_margins) if winning_margins else 0.0
        return winner, avg_margin

    @staticmethod
    def _compute_agent_scores(
        verdicts: list[JudgeVerdict],
        agent_names: list[str],
    ) -> list[AgentScore]:
        """Per-agent win rate across judges.

        Each agent's ``score`` is the fraction of judges that picked
        them as winner.  ``rubric`` is the average of rubric scores
        from verdicts where the agent won (empty if the agent never
        won, since judges report rubric for the winner).
        """
        if not verdicts:
            return [AgentScore(agent_name=n) for n in agent_names]

        n_judges = len(verdicts)
        scores: list[AgentScore] = []
        for name in agent_names:
            winning = [v for v in verdicts if v.winner == name]
            wins = len(winning)
            avg_rubric: dict[str, float] = {}
            if winning:
                fields: set[str] = set()
                for v in winning:
                    fields.update(v.rubric)
                for field in fields:
                    vals = [v.rubric.get(field, 0.0) for v in winning]
                    avg_rubric[field] = sum(vals) / len(vals)
            scores.append(
                AgentScore(
                    agent_name=name,
                    score=wins / n_judges,
                    rubric=avg_rubric,
                )
            )
        return scores

    def _default_prompt(self) -> str:
        return (
            "You are a fair, substantive judge of a structured debate.\n"
            "Judge on argument quality, not rhetorical style.  Reward "
            "concrete evidence, sharp rebuttals, and direct engagement "
            "with the opponent.  Penalize evasion, ad hominem, and "
            "unsupported claims.\n\n"
            "TOPIC: {topic}\n"
            "DEBATERS: {agents}\n\n"
            "TRANSCRIPT:\n{transcript}\n\n"
            "Score each rubric dimension from 0.0 to 1.0 for the WINNER:\n"
            "{rubric_fields}\n\n"
            "Return JSON ONLY (no prose, no markdown fence) with this shape:\n"
            "{{\n"
            '  "winner": "<exact agent name>",\n'
            '  "margin": <0.0..1.0 — how decisive>,\n'
            '  "rubric": {{"argument_quality": 0.0, ...}},\n'
            '  "reasoning": "<one or two sentences>"\n'
            "}}"
        )


# ---------------------------------------------------------------------------
# Per-participant rubric scoring (blind taste test)
# ---------------------------------------------------------------------------


class RubricVerdict(BaseModel):
    """One judge's per-participant rubric scores.

    Unlike :class:`JudgeVerdict` (which picks a single winner),
    :class:`RubricVerdict` carries the judge's score for *every*
    participant on *every* rubric dimension.  The keys of ``scores``
    are anonymized labels (``Participant A`` / ``B`` / ...), not real
    agent names — the scorer assigns the aliases before the judge
    sees the transcript so judges grade blind.
    """

    judge_model: str
    # alias -> dimension -> 0.0..1.0
    scores: dict[str, dict[str, float]] = Field(default_factory=dict)
    best_response: str = ""  # alias of the participant the judge thinks is best
    reasoning: str = ""


class RubricScorer(Scorer):
    """Score every participant on every rubric dimension, blind.

    Designed for blind-taste-test style evaluations where multiple
    LLMs answer the same prompt independently and must be graded
    without the judge knowing which model produced which response.

    The scorer:

    1. Builds an alias map ``agent_name -> "Participant A/B/C"`` from
       the agents present in the transcript (sorted by name for
       stability).
    2. Anonymizes the transcript using that map and sends it to each
       judge :class:`ChatBridge`.
    3. Parses per-participant per-dimension scores out of each
       judge's JSON response and averages them across the panel.
    4. Returns a :class:`ScoringResult` whose ``agent_scores`` carry
       per-dimension averages (``rubric``) and an overall mean
       (``score``), plus ``alias_map`` and the raw verdicts in
       ``metadata``.  ``winner`` is the agent with the highest
       overall score; ``win_margin`` is the gap to the runner-up.

    The alias map is stable across all judges within one ``score()``
    call but is *not* preserved across calls — callers that need a
    consistent agent→alias mapping across multiple prompts (e.g. the
    blind-taste-test reveal) build their own from agent order.

    Args:
        judges: List of :class:`ChatBridge` instances.  Use models
            from a different family than the participants when
            possible to reduce evaluator bias.
        rubric_fields: Dimensions for the judge to score every
            participant on.  Defaults to
            ``("accuracy", "depth", "clarity", "creativity",
            "conciseness")``.
        scoring_prompt: Optional override of the judge prompt.  Must
            include ``{transcript}``, ``{topic}``, ``{participants}``,
            and ``{rubric_fields}`` placeholders.
    """

    DEFAULT_RUBRIC_FIELDS: tuple[str, ...] = (
        "accuracy",
        "depth",
        "clarity",
        "creativity",
        "conciseness",
    )

    def __init__(
        self,
        judges: list[ChatBridge],
        rubric_fields: list[str] | None = None,
        scoring_prompt: str | None = None,
    ) -> None:
        if not judges:
            msg = "RubricScorer requires at least one judge"
            raise ValueError(msg)
        self.judges = judges
        self.rubric_fields = (
            list(rubric_fields) if rubric_fields else list(self.DEFAULT_RUBRIC_FIELDS)
        )
        self.scoring_prompt = scoring_prompt or self._default_prompt()

    async def score(self, result: CouncilResult) -> ScoringResult:
        """Run blind per-participant scoring across the judge panel."""
        agent_names = sorted({e.agent_name for r in result.transcript for e in r.entries})
        alias_map = self.build_alias_map(agent_names)
        reverse = {alias: name for name, alias in alias_map.items()}

        transcript_text = self._format_anonymized(result.transcript, alias_map)
        prompt = self.scoring_prompt.format(
            transcript=transcript_text,
            topic=result.topic,
            participants=", ".join(alias_map.values()),
            rubric_fields=", ".join(self.rubric_fields),
        )

        verdicts = await self._collect_verdicts(prompt, set(alias_map.values()))
        agent_scores = self._aggregate(verdicts, agent_names, alias_map)
        winner, win_margin = self._pick_winner(agent_scores)

        # Surface the best-response votes per judge as a sanity signal.
        best_votes: dict[str, int] = {}
        for v in verdicts:
            agent = reverse.get(v.best_response)
            if agent:
                best_votes[agent] = best_votes.get(agent, 0) + 1

        return ScoringResult(
            council_topic=result.topic,
            agent_scores=agent_scores,
            verdicts=[],  # JudgeVerdict shape doesn't fit; raw rubric verdicts in metadata
            winner=winner,
            win_margin=win_margin,
            metadata={
                "scoring_mode": "per_participant_rubric",
                "alias_map": alias_map,
                "judge_count": len(self.judges),
                "verdict_count": len(verdicts),
                "rubric_fields": list(self.rubric_fields),
                "best_response_votes": best_votes,
                "rubric_verdicts": [v.model_dump() for v in verdicts],
            },
        )

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @staticmethod
    def build_alias_map(agent_names: list[str]) -> dict[str, str]:
        """Build a stable ``agent_name -> "Participant A/B/..."`` map.

        Agents are sorted by name for determinism within a single
        :meth:`score` call.  The blind-taste-test example uses its
        own per-prompt mapping (built from agent order in the
        council config) when it needs the same alias to track a
        model across multiple prompts.
        """
        return {name: f"Participant {chr(65 + i)}" for i, name in enumerate(sorted(agent_names))}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _collect_verdicts(
        self,
        prompt: str,
        valid_aliases: set[str],
    ) -> list[RubricVerdict]:
        """Send the anonymized prompt to all judges concurrently."""

        async def _one(judge: ChatBridge, idx: int) -> RubricVerdict | None:
            try:
                response = await judge.send_turn(
                    message=prompt,
                    context={},
                    session_id=f"rubric-score-{idx}",
                )
            except Exception as e:
                logger.warning("scorer.rubric.judge.failed", judge_index=idx, error=str(e))
                return None
            return self._parse_verdict(
                response.content,
                response.model or f"judge-{idx}",
                valid_aliases,
            )

        results = await asyncio.gather(*[_one(j, i) for i, j in enumerate(self.judges)])
        return [v for v in results if v is not None]

    def _format_anonymized(
        self,
        rounds: list[RoundEntry],
        alias_map: dict[str, str],
    ) -> str:
        """Render the transcript with real names replaced by aliases."""
        lines: list[str] = []
        for round_entry in rounds:
            lines.append(f"--- Round {round_entry.round_num} ---")
            for entry in round_entry.entries:
                if entry.entry_type == "interjection":
                    label = f"[AUDIENCE: {entry.agent_name}]"
                else:
                    label = f"[{alias_map.get(entry.agent_name, entry.agent_name).upper()}]"
                lines.append(f"{label}\n{entry.content.strip()}\n")
        return "\n".join(lines)

    def _parse_verdict(
        self,
        content: str,
        judge_model: str,
        valid_aliases: set[str],
    ) -> RubricVerdict | None:
        """Extract per-participant scores from one judge's JSON response.

        Rejects the verdict outright if no valid participant scores
        come through (otherwise the average would silently drift
        toward zero for missing aliases).
        """
        data = _extract_json_object(content)
        if data is None:
            logger.warning("scorer.rubric.parse_failed", preview=content[:200])
            return None

        raw_scores = data.get("scores")
        if not isinstance(raw_scores, dict):
            return None

        clean = _coerce_rubric_scores(raw_scores, valid_aliases)
        if not clean:
            logger.warning(
                "scorer.rubric.no_valid_scores",
                preview=content[:200],
                valid_aliases=sorted(valid_aliases),
            )
            return None

        return RubricVerdict(
            judge_model=judge_model,
            scores=clean,
            best_response=str(data.get("best_response", "")),
            reasoning=str(data.get("reasoning", "")),
        )

    def _aggregate(
        self,
        verdicts: list[RubricVerdict],
        agent_names: list[str],
        alias_map: dict[str, str],
    ) -> list[AgentScore]:
        """Average rubric scores per (agent, dimension) across judges.

        Each :class:`AgentScore` holds:
          - ``rubric``: per-dimension means across judges that scored
            the corresponding alias.
          - ``score``: overall mean (mean of dimension means), 0.0
            if the agent received no scores.
          - ``notes``: the alias used during blind judging.
        """
        scores: list[AgentScore] = []
        for name in agent_names:
            alias = alias_map.get(name, name)
            per_dim: dict[str, list[float]] = {}
            for v in verdicts:
                dims = v.scores.get(alias)
                if not dims:
                    continue
                for dim, val in dims.items():
                    per_dim.setdefault(dim, []).append(val)

            rubric = {dim: sum(vals) / len(vals) for dim, vals in per_dim.items() if vals}
            overall = sum(rubric.values()) / len(rubric) if rubric else 0.0
            scores.append(
                AgentScore(
                    agent_name=name,
                    score=overall,
                    rubric=rubric,
                    notes=alias,
                )
            )
        return scores

    @staticmethod
    def _pick_winner(agent_scores: list[AgentScore]) -> tuple[str | None, float]:
        """Return ``(top_agent_name, gap_to_second)`` or ``(None, 0.0)``.

        Ties at the top are recorded as ``(None, 0.0)`` — the
        consumer should treat that as a draw rather than picking
        arbitrarily.
        """
        if not agent_scores:
            return None, 0.0
        ranked = sorted(agent_scores, key=lambda a: a.score, reverse=True)
        top = ranked[0]
        if top.score == 0.0 and all(a.score == 0.0 for a in ranked):
            return None, 0.0
        if len(ranked) >= 2 and ranked[1].score == top.score:
            return None, 0.0
        gap = top.score - ranked[1].score if len(ranked) >= 2 else top.score
        return top.agent_name, gap

    def _default_prompt(self) -> str:
        return (
            "You are blindly evaluating anonymous responses to a question.\n"
            "You do NOT know which AI model produced each response.\n"
            "Judge on quality of the answer, not on style or branding.\n\n"
            "QUESTION: {topic}\n\n"
            "PARTICIPANTS: {participants}\n\n"
            "RESPONSES:\n{transcript}\n\n"
            "Score EVERY participant on EVERY rubric dimension (0.0 to 1.0):\n"
            "{rubric_fields}\n\n"
            "Return JSON ONLY (no prose, no markdown fence) with this shape:\n"
            "{{\n"
            '  "scores": {{\n'
            '    "Participant A": {{"accuracy": 0.X, "depth": 0.X, ...}},\n'
            '    "Participant B": {{"accuracy": 0.X, "depth": 0.X, ...}},\n'
            "    ...\n"
            "  }},\n"
            '  "best_response": "Participant X",\n'
            '   "reasoning": "<one or two sentences>"\n'
            "}}"
        )
