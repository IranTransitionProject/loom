"""Round-robin debate tournament scheduling and aggregation.

A tournament runs the same debate across many model pairings, scores
each one with a :class:`JudgePanelScorer`, and aggregates the results
into a leaderboard plus a head-to-head matchup matrix.

The pattern mirrors Lech Mazur's debate benchmark: every model debates
every other model, both sides of each topic (with sides swapped), and
a panel of out-of-family judges decides each round.
"""

from __future__ import annotations

import asyncio
import copy
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from heddle.contrib.council.config import CouncilConfig
    from heddle.contrib.council.runner import CouncilRunner
    from heddle.contrib.council.schemas import AgentConfig
    from heddle.contrib.council.scorer import Scorer, ScoringResult

logger = structlog.get_logger()


@dataclass
class Matchup:
    """One pairing in a debate tournament."""

    model_a: str
    model_b: str
    topic: str
    pro_model: str  # one of model_a / model_b — argues PRO this round
    matchup_id: str = ""

    def __post_init__(self) -> None:
        if self.pro_model not in (self.model_a, self.model_b):
            msg = (
                f"pro_model '{self.pro_model}' must be one of "
                f"model_a='{self.model_a}' or model_b='{self.model_b}'"
            )
            raise ValueError(msg)
        if not self.matchup_id:
            topic_hash = hash(self.topic) & 0xFFFF
            self.matchup_id = (
                f"{self.model_a}_vs_{self.model_b}_{self.pro_model}_pro_{topic_hash:04x}"
            )

    @property
    def con_model(self) -> str:
        """The model arguing CON in this matchup."""
        return self.model_b if self.pro_model == self.model_a else self.model_a


@dataclass
class MatchupResult:
    """Outcome of a single matchup."""

    matchup: Matchup
    scoring: ScoringResult | None = None
    elapsed_ms: int = 0
    error: str | None = None

    def winner_model(self, pro_agent_name: str, con_agent_name: str) -> str | None:
        """Resolve the model that won, given the agent role names.

        Returns ``None`` for draws, errors, or unrecognized winners.
        """
        if self.error or self.scoring is None or self.scoring.winner is None:
            return None
        if self.scoring.winner == pro_agent_name:
            return self.matchup.pro_model
        if self.scoring.winner == con_agent_name:
            return self.matchup.con_model
        return None


class TournamentResult(BaseModel):
    """Aggregated outcome of a debate tournament."""

    models: list[str]
    topics: list[str]
    total_matchups: int = 0
    completed_matchups: int = 0
    failed_matchups: int = 0
    leaderboard: list[dict[str, Any]] = Field(default_factory=list)
    matchup_matrix: dict[str, dict[str, dict[str, int]]] = Field(default_factory=dict)
    results: list[dict[str, Any]] = Field(default_factory=list)
    elapsed_ms: int = 0


class TournamentRunner:
    """Schedule, execute, and aggregate a round-robin debate tournament.

    Args:
        runner: A :class:`CouncilRunner` configured with backends.
        scorer: A :class:`Scorer` used to judge each finished debate.
        config_template: A :class:`CouncilConfig` whose first two
            agents are the PRO/CON slots.  Their ``name`` and ``role``
            fields are preserved across matchups; the ``tier`` and
            backend (``worker_type`` or ``bridge``) are replaced for
            each matchup by ``agent_factory``.  Any agents beyond the
            first two are kept as-is (e.g., a moderator).
        agent_factory: Callable ``(model_key, role, topic) ->
            AgentConfig`` that builds one debater config per call.
            The runner overrides ``name`` to match the template slot.
    """

    def __init__(
        self,
        runner: CouncilRunner,
        scorer: Scorer,
        config_template: CouncilConfig,
        agent_factory: Callable[[str, str, str], AgentConfig],
    ) -> None:
        if len(config_template.agents) < 2:
            msg = "config_template must have at least 2 agents (pro and con)"
            raise ValueError(msg)
        self.runner = runner
        self.scorer = scorer
        self.config_template = config_template
        self.agent_factory = agent_factory
        self._pro_name = config_template.agents[0].name
        self._con_name = config_template.agents[1].name

    @staticmethod
    def generate_matchups(
        models: list[str],
        topics: list[str],
        both_sides: bool = True,
    ) -> list[Matchup]:
        """Round-robin all model pairs across all topics.

        With ``both_sides=True``, each pair debates each topic twice
        with sides swapped.  Three models on two topics yields 12
        matchups (3 pairs * 2 topics * 2 sides).  With
        ``both_sides=False``, six matchups (3 pairs * 2 topics).
        """
        if len(models) < 2:
            msg = "Need at least 2 models for a tournament"
            raise ValueError(msg)
        if not topics:
            msg = "Need at least 1 topic for a tournament"
            raise ValueError(msg)

        matchups: list[Matchup] = []
        for i, model_a in enumerate(models):
            for model_b in models[i + 1 :]:
                for topic in topics:
                    matchups.append(
                        Matchup(model_a=model_a, model_b=model_b, topic=topic, pro_model=model_a)
                    )
                    if both_sides:
                        matchups.append(
                            Matchup(
                                model_a=model_a,
                                model_b=model_b,
                                topic=topic,
                                pro_model=model_b,
                            )
                        )
        return matchups

    async def run(
        self,
        matchups: list[Matchup],
        on_matchup_done: Callable[[MatchupResult], Awaitable[None] | None] | None = None,
        concurrency: int = 1,
    ) -> TournamentResult:
        """Run all matchups, optionally in parallel, and aggregate."""
        start = time.monotonic()
        sem = asyncio.Semaphore(max(1, concurrency))

        async def _bounded(m: Matchup) -> MatchupResult:
            async with sem:
                result = await self._run_matchup(m)
            if on_matchup_done is not None:
                cb = on_matchup_done(result)
                if hasattr(cb, "__await__"):
                    await cb
            return result

        results = await asyncio.gather(*[_bounded(m) for m in matchups])
        elapsed_ms = int((time.monotonic() - start) * 1000)

        models = sorted({m.model_a for m in matchups} | {m.model_b for m in matchups})
        topics = list(dict.fromkeys(m.topic for m in matchups))  # preserve order, dedupe
        return self._aggregate(models, topics, results, elapsed_ms)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _new_runner_for_matchup(self) -> CouncilRunner:
        """Build a fresh :class:`CouncilRunner` for one matchup.

        ``CouncilRunner`` carries per-discussion state (the active
        transcript pointer and the cache of :class:`ChatBridge`
        instances).  When ``concurrency > 1`` the tournament would
        previously share one runner across overlapping matchups,
        clobbering both attributes between coroutines.  Each
        matchup now gets its own runner sharing the prototype's
        backends — backends are stateless / thread-safe, runners
        are not.  Cheap: a runner is a thin object holding two
        references and two empty dicts.
        """
        # Local import avoids the TYPE_CHECKING-only top-level import
        # bleeding into runtime (CouncilRunner pulls the whole runner
        # module + transitive deps; tournament's module-load shouldn't
        # pay that cost when only the type is referenced).
        from heddle.contrib.council.runner import CouncilRunner

        return CouncilRunner(backends=self.runner.backends)

    async def _run_matchup(self, matchup: Matchup) -> MatchupResult:
        """Run one debate, then score it."""
        log = logger.bind(matchup=matchup.matchup_id)
        start = time.monotonic()

        try:
            config = self._build_matchup_config(matchup)
            runner = self._new_runner_for_matchup()
            council_result = await runner.run(matchup.topic, config=config)
            scoring = await self.scorer.score(council_result)
        except Exception as e:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            log.warning("tournament.matchup.failed", error=str(e))
            return MatchupResult(matchup=matchup, elapsed_ms=elapsed_ms, error=str(e))

        elapsed_ms = int((time.monotonic() - start) * 1000)
        log.info(
            "tournament.matchup.done",
            winner=scoring.winner,
            margin=scoring.win_margin,
            elapsed_ms=elapsed_ms,
        )
        return MatchupResult(matchup=matchup, scoring=scoring, elapsed_ms=elapsed_ms)

    def _build_matchup_config(self, matchup: Matchup) -> CouncilConfig:
        """Clone the template, swap in pro/con debaters via ``agent_factory``."""
        config = copy.deepcopy(self.config_template)
        pro_role = config.agents[0].role
        con_role = config.agents[1].role

        pro_agent = self.agent_factory(matchup.pro_model, pro_role, matchup.topic)
        con_agent = self.agent_factory(matchup.con_model, con_role, matchup.topic)

        # Preserve template names so the scorer's winner field is stable.
        pro_agent.name = self._pro_name
        con_agent.name = self._con_name

        config.agents[0] = pro_agent
        config.agents[1] = con_agent
        return config

    def _aggregate(
        self,
        models: list[str],
        topics: list[str],
        results: list[MatchupResult],
        elapsed_ms: int,
    ) -> TournamentResult:
        """Build leaderboard and matchup matrix from raw results."""
        stats: dict[str, dict[str, Any]] = {
            m: {"wins": 0, "losses": 0, "draws": 0, "total": 0, "margins": []} for m in models
        }
        matrix: dict[str, dict[str, dict[str, int]]] = {
            m: {n: {"wins": 0, "losses": 0, "draws": 0} for n in models if n != m} for m in models
        }

        completed = 0
        failed = 0

        for r in results:
            if r.error is not None or r.scoring is None:
                failed += 1
                continue
            completed += 1

            a = r.matchup.model_a
            b = r.matchup.model_b
            stats[a]["total"] += 1
            stats[b]["total"] += 1

            winner = r.winner_model(self._pro_name, self._con_name)
            if winner is None:
                stats[a]["draws"] += 1
                stats[b]["draws"] += 1
                matrix[a][b]["draws"] += 1
                matrix[b][a]["draws"] += 1
            else:
                loser = b if winner == a else a
                stats[winner]["wins"] += 1
                stats[loser]["losses"] += 1
                stats[winner]["margins"].append(r.scoring.win_margin)
                matrix[winner][loser]["wins"] += 1
                matrix[loser][winner]["losses"] += 1

        leaderboard = []
        for model in models:
            s = stats[model]
            margins = s["margins"]
            avg_margin = sum(margins) / len(margins) if margins else 0.0
            win_rate = s["wins"] / s["total"] if s["total"] else 0.0
            leaderboard.append(
                {
                    "model": model,
                    "wins": s["wins"],
                    "losses": s["losses"],
                    "draws": s["draws"],
                    "total": s["total"],
                    "win_rate": round(win_rate, 3),
                    "avg_margin": round(avg_margin, 3),
                }
            )
        leaderboard.sort(key=lambda row: (row["win_rate"], row["avg_margin"]), reverse=True)

        serialized = [
            {
                "matchup_id": r.matchup.matchup_id,
                "model_a": r.matchup.model_a,
                "model_b": r.matchup.model_b,
                "pro_model": r.matchup.pro_model,
                "topic": r.matchup.topic,
                "elapsed_ms": r.elapsed_ms,
                "error": r.error,
                "winner": (
                    r.winner_model(self._pro_name, self._con_name)
                    if r.scoring is not None
                    else None
                ),
                "scoring": r.scoring.model_dump() if r.scoring is not None else None,
            }
            for r in results
        ]

        return TournamentResult(
            models=models,
            topics=topics,
            total_matchups=len(results),
            completed_matchups=completed,
            failed_matchups=failed,
            leaderboard=leaderboard,
            matchup_matrix=matrix,
            results=serialized,
            elapsed_ms=elapsed_ms,
        )
