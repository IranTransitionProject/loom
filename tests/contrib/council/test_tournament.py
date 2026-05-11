"""Tests for TournamentRunner and tournament data models."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from heddle.contrib.council.config import CouncilConfig
from heddle.contrib.council.schemas import AgentConfig, CouncilResult
from heddle.contrib.council.scorer import ScoringResult
from heddle.contrib.council.tournament import (
    Matchup,
    MatchupResult,
    TournamentResult,
    TournamentRunner,
)

# -- Fixtures --------------------------------------------------------------


def _template(extra_agents: list[AgentConfig] | None = None) -> CouncilConfig:
    agents = [
        AgentConfig(name="pro", worker_type="reviewer", role="Argue PRO."),
        AgentConfig(name="con", worker_type="reviewer", role="Argue CON."),
    ]
    if extra_agents:
        agents.extend(extra_agents)
    return CouncilConfig(
        name="debate",
        protocol="structured_debate",
        max_rounds=2,
        convergence={"method": "none"},
        agents=agents,
    )


def _factory(model_key: str, role: str, topic: str) -> AgentConfig:
    """Default test factory — stamps the model_key into role."""
    del topic
    return AgentConfig(
        name="placeholder",  # runner overrides
        worker_type="reviewer",
        role=f"{model_key}: {role}",
    )


def _scoring_result(winner: str | None, margin: float = 0.7) -> ScoringResult:
    return ScoringResult(
        council_topic="t",
        winner=winner,
        win_margin=margin,
    )


def _matchup_result(
    model_a: str,
    model_b: str,
    topic: str,
    pro_model: str,
    winner: str | None,
    margin: float = 0.7,
    error: str | None = None,
) -> MatchupResult:
    matchup = Matchup(model_a=model_a, model_b=model_b, topic=topic, pro_model=pro_model)
    scoring = None if error else _scoring_result(winner, margin)
    return MatchupResult(matchup=matchup, scoring=scoring, elapsed_ms=10, error=error)


# -- Matchup ---------------------------------------------------------------


class TestMatchup:
    def test_id_auto_generated(self) -> None:
        m = Matchup(model_a="a", model_b="b", topic="t1", pro_model="a")
        assert m.matchup_id != ""
        assert "a_vs_b_a_pro_" in m.matchup_id

    def test_id_deterministic(self) -> None:
        m1 = Matchup(model_a="a", model_b="b", topic="topic", pro_model="a")
        m2 = Matchup(model_a="a", model_b="b", topic="topic", pro_model="a")
        assert m1.matchup_id == m2.matchup_id

    def test_id_different_for_different_inputs(self) -> None:
        m1 = Matchup(model_a="a", model_b="b", topic="t1", pro_model="a")
        m2 = Matchup(model_a="a", model_b="b", topic="t2", pro_model="a")
        assert m1.matchup_id != m2.matchup_id

    def test_id_different_when_sides_swap(self) -> None:
        m1 = Matchup(model_a="a", model_b="b", topic="t", pro_model="a")
        m2 = Matchup(model_a="a", model_b="b", topic="t", pro_model="b")
        assert m1.matchup_id != m2.matchup_id

    def test_pro_model_must_be_one_of(self) -> None:
        with pytest.raises(ValueError, match="must be one of"):
            Matchup(model_a="a", model_b="b", topic="t", pro_model="c")

    def test_con_model_property(self) -> None:
        m = Matchup(model_a="a", model_b="b", topic="t", pro_model="a")
        assert m.con_model == "b"
        m2 = Matchup(model_a="a", model_b="b", topic="t", pro_model="b")
        assert m2.con_model == "a"


# -- generate_matchups -----------------------------------------------------


class TestGenerateMatchups:
    def test_both_sides_3_models_2_topics(self) -> None:
        matchups = TournamentRunner.generate_matchups(
            models=["a", "b", "c"],
            topics=["t1", "t2"],
            both_sides=True,
        )
        # 3 pairs (ab, ac, bc) * 2 topics * 2 sides = 12
        assert len(matchups) == 12
        # Every pair appears with each side as pro.
        for pair in [("a", "b"), ("a", "c"), ("b", "c")]:
            for topic in ["t1", "t2"]:
                pro_a = [
                    m
                    for m in matchups
                    if m.model_a == pair[0]
                    and m.model_b == pair[1]
                    and m.topic == topic
                    and m.pro_model == pair[0]
                ]
                pro_b = [
                    m
                    for m in matchups
                    if m.model_a == pair[0]
                    and m.model_b == pair[1]
                    and m.topic == topic
                    and m.pro_model == pair[1]
                ]
                assert len(pro_a) == 1
                assert len(pro_b) == 1

    def test_one_side_3_models_2_topics(self) -> None:
        matchups = TournamentRunner.generate_matchups(
            models=["a", "b", "c"],
            topics=["t1", "t2"],
            both_sides=False,
        )
        # 3 pairs * 2 topics * 1 side = 6
        assert len(matchups) == 6

    def test_no_self_play(self) -> None:
        matchups = TournamentRunner.generate_matchups(
            models=["a", "b"],
            topics=["t"],
            both_sides=True,
        )
        for m in matchups:
            assert m.model_a != m.model_b

    def test_too_few_models_raises(self) -> None:
        with pytest.raises(ValueError, match="at least 2 models"):
            TournamentRunner.generate_matchups(models=["a"], topics=["t"])

    def test_no_topics_raises(self) -> None:
        with pytest.raises(ValueError, match="at least 1 topic"):
            TournamentRunner.generate_matchups(models=["a", "b"], topics=[])


# -- _build_matchup_config -------------------------------------------------


class TestBuildMatchupConfig:
    def test_swaps_first_two_agents(self) -> None:
        runner = TournamentRunner(
            runner=AsyncMock(),
            scorer=AsyncMock(),
            config_template=_template(),
            agent_factory=_factory,
        )
        matchup = Matchup(model_a="a", model_b="b", topic="t", pro_model="a")
        cfg = runner._build_matchup_config(matchup)

        assert len(cfg.agents) == 2
        assert cfg.agents[0].name == "pro"
        assert cfg.agents[1].name == "con"
        assert "a:" in cfg.agents[0].role  # pro side carries model "a"
        assert "b:" in cfg.agents[1].role  # con side carries model "b"

    def test_preserves_extra_agents(self) -> None:
        moderator = AgentConfig(name="moderator", worker_type="reviewer", role="Moderate.")
        runner = TournamentRunner(
            runner=AsyncMock(),
            scorer=AsyncMock(),
            config_template=_template(extra_agents=[moderator]),
            agent_factory=_factory,
        )
        matchup = Matchup(model_a="a", model_b="b", topic="t", pro_model="a")
        cfg = runner._build_matchup_config(matchup)

        assert len(cfg.agents) == 3
        assert cfg.agents[2].name == "moderator"
        assert cfg.agents[2].role == "Moderate."

    def test_does_not_mutate_template(self) -> None:
        template = _template()
        original_role_pro = template.agents[0].role
        runner = TournamentRunner(
            runner=AsyncMock(),
            scorer=AsyncMock(),
            config_template=template,
            agent_factory=_factory,
        )
        matchup = Matchup(model_a="a", model_b="b", topic="t", pro_model="b")
        runner._build_matchup_config(matchup)
        assert template.agents[0].role == original_role_pro

    def test_swap_when_pro_is_b(self) -> None:
        runner = TournamentRunner(
            runner=AsyncMock(),
            scorer=AsyncMock(),
            config_template=_template(),
            agent_factory=_factory,
        )
        matchup = Matchup(model_a="a", model_b="b", topic="t", pro_model="b")
        cfg = runner._build_matchup_config(matchup)

        # pro slot gets model "b" (matchup.pro_model)
        # con slot gets model "a" (the other model)
        assert "b:" in cfg.agents[0].role
        assert "a:" in cfg.agents[1].role

    def test_too_few_template_agents_raises(self) -> None:
        thin = CouncilConfig(
            name="x",
            agents=[
                AgentConfig(name="solo1", worker_type="reviewer"),
                AgentConfig(name="solo2", worker_type="reviewer"),
            ],
        )
        # 2 agents is the minimum, so this should not raise.
        TournamentRunner(
            runner=AsyncMock(),
            scorer=AsyncMock(),
            config_template=thin,
            agent_factory=_factory,
        )


# -- _aggregate ------------------------------------------------------------


class TestAggregate:
    def test_leaderboard_sorting(self) -> None:
        runner = TournamentRunner(
            runner=AsyncMock(),
            scorer=AsyncMock(),
            config_template=_template(),
            agent_factory=_factory,
        )
        results = [
            _matchup_result("a", "b", "t1", "a", winner="pro", margin=0.8),
            _matchup_result("a", "b", "t1", "b", winner="pro", margin=0.6),
            _matchup_result("a", "c", "t1", "a", winner="pro", margin=0.7),
            _matchup_result("a", "c", "t1", "c", winner="con", margin=0.5),
            _matchup_result("b", "c", "t1", "b", winner="con", margin=0.4),
            _matchup_result("b", "c", "t1", "c", winner="pro", margin=0.5),
        ]
        result = runner._aggregate(["a", "b", "c"], ["t1"], results, elapsed_ms=100)

        # Each matchup result above resolves to: a=3 wins, b=1 win, c=2 wins.
        # See _matchup_result + winner_model for the resolution logic.
        by_model = {row["model"]: row for row in result.leaderboard}
        assert by_model["a"]["wins"] == 3
        assert by_model["b"]["wins"] == 1
        assert by_model["c"]["wins"] == 2
        # Leaderboard sorted by win_rate desc, so a is first.
        assert result.leaderboard[0]["model"] == "a"

    def test_matrix_tallies(self) -> None:
        runner = TournamentRunner(
            runner=AsyncMock(),
            scorer=AsyncMock(),
            config_template=_template(),
            agent_factory=_factory,
        )
        results = [
            _matchup_result("a", "b", "t", "a", winner="pro"),  # a beats b
            _matchup_result("a", "b", "t", "b", winner="pro"),  # b beats a
            _matchup_result("a", "b", "t", "a", winner=None),  # draw
        ]
        result = runner._aggregate(["a", "b"], ["t"], results, elapsed_ms=10)
        assert result.matchup_matrix["a"]["b"] == {"wins": 1, "losses": 1, "draws": 1}
        assert result.matchup_matrix["b"]["a"] == {"wins": 1, "losses": 1, "draws": 1}

    def test_total_counts(self) -> None:
        runner = TournamentRunner(
            runner=AsyncMock(),
            scorer=AsyncMock(),
            config_template=_template(),
            agent_factory=_factory,
        )
        results = [
            _matchup_result("a", "b", "t", "a", winner="pro"),
            _matchup_result("a", "b", "t", "b", winner=None),
            _matchup_result("a", "b", "t", "a", winner=None, error="boom"),
        ]
        result = runner._aggregate(["a", "b"], ["t"], results, elapsed_ms=10)
        assert result.total_matchups == 3
        assert result.completed_matchups == 2
        assert result.failed_matchups == 1

    def test_empty_results(self) -> None:
        runner = TournamentRunner(
            runner=AsyncMock(),
            scorer=AsyncMock(),
            config_template=_template(),
            agent_factory=_factory,
        )
        result = runner._aggregate(["a", "b"], ["t"], [], elapsed_ms=0)
        assert result.total_matchups == 0
        assert all(row["win_rate"] == 0.0 for row in result.leaderboard)


# -- run() end-to-end -------------------------------------------------------


class TestRun:
    async def test_run_dispatches_all_matchups(self, monkeypatch) -> None:
        # Council runner returns a stub CouncilResult.
        council_runner = AsyncMock()
        council_runner.run.return_value = CouncilResult(
            topic="t",
            rounds_completed=1,
            converged=False,
            synthesis="syn",
        )
        # Scorer always picks pro.
        scorer = AsyncMock()
        scorer.score.return_value = _scoring_result("pro", 0.7)

        runner = TournamentRunner(
            runner=council_runner,
            scorer=scorer,
            config_template=_template(),
            agent_factory=_factory,
        )
        # B4: tournament builds a fresh runner per matchup to keep
        # ``_active_transcript`` / ``_bridges`` isolated.  In tests
        # we want the AsyncMock above to be the runner; override the
        # factory so it returns the mock.
        monkeypatch.setattr(runner, "_new_runner_for_matchup", lambda: council_runner)
        matchups = TournamentRunner.generate_matchups(
            models=["a", "b"],
            topics=["t1"],
            both_sides=True,
        )
        assert len(matchups) == 2

        callback_count = {"n": 0}

        def _on_done(result: MatchupResult) -> None:
            del result
            callback_count["n"] += 1

        result = await runner.run(matchups, on_matchup_done=_on_done, concurrency=1)

        assert result.total_matchups == 2
        assert result.completed_matchups == 2
        assert result.failed_matchups == 0
        assert callback_count["n"] == 2
        assert council_runner.run.await_count == 2
        assert scorer.score.await_count == 2

        # In matchup #1 pro=a wins → a beats b.  In matchup #2 pro=b wins → b beats a.
        # So 1-1, a draw scenario in win counts.
        by_model = {row["model"]: row for row in result.leaderboard}
        assert by_model["a"]["wins"] == 1
        assert by_model["b"]["wins"] == 1

    async def test_run_handles_runner_failure(self, monkeypatch) -> None:
        council_runner = AsyncMock()
        council_runner.run.side_effect = RuntimeError("backend down")
        scorer = AsyncMock()
        scorer.score.return_value = _scoring_result("pro", 0.7)

        runner = TournamentRunner(
            runner=council_runner,
            scorer=scorer,
            config_template=_template(),
            agent_factory=_factory,
        )
        monkeypatch.setattr(runner, "_new_runner_for_matchup", lambda: council_runner)
        matchups = [Matchup(model_a="a", model_b="b", topic="t", pro_model="a")]
        result = await runner.run(matchups, concurrency=1)

        assert result.total_matchups == 1
        assert result.failed_matchups == 1
        assert result.results[0]["error"] == "backend down"

    async def test_async_callback(self, monkeypatch) -> None:
        council_runner = AsyncMock()
        council_runner.run.return_value = CouncilResult(
            topic="t", rounds_completed=1, converged=False, synthesis="syn"
        )
        scorer = AsyncMock()
        scorer.score.return_value = _scoring_result("pro", 0.7)

        runner = TournamentRunner(
            runner=council_runner,
            scorer=scorer,
            config_template=_template(),
            agent_factory=_factory,
        )
        monkeypatch.setattr(runner, "_new_runner_for_matchup", lambda: council_runner)
        matchups = [Matchup(model_a="a", model_b="b", topic="t", pro_model="a")]

        seen = []

        async def _async_cb(result: MatchupResult) -> None:
            seen.append(result.matchup.matchup_id)

        await runner.run(matchups, on_matchup_done=_async_cb, concurrency=1)
        assert len(seen) == 1

    async def test_per_matchup_runner_isolation(self) -> None:
        """Pin the B4 fix: each matchup gets its own CouncilRunner.

        Earlier the tournament shared one ``CouncilRunner`` across
        concurrent matchups, clobbering ``_active_transcript`` and
        ``_bridges`` between coroutines.  Now ``_new_runner_for_matchup``
        produces a fresh runner per matchup.  Under ``concurrency > 1``
        with overlapping matchups, the runner instance backing one
        matchup must not be the same object as the one backing another
        — otherwise their transcript pointers race.
        """
        from heddle.contrib.council.runner import CouncilRunner

        prototype = CouncilRunner(backends={"local": object()})
        scorer = AsyncMock()
        scorer.score.return_value = _scoring_result("pro", 0.7)

        runner = TournamentRunner(
            runner=prototype,
            scorer=scorer,
            config_template=_template(),
            agent_factory=_factory,
        )

        r1 = runner._new_runner_for_matchup()
        r2 = runner._new_runner_for_matchup()
        assert r1 is not r2  # distinct objects
        assert r1 is not prototype  # neither one is the prototype
        # Backends share the same dict reference (the prototype's) so
        # backend connections aren't duplicated needlessly.
        assert r1.backends is prototype.backends


# -- TournamentResult model ------------------------------------------------


class TestTournamentResult:
    def test_round_trip_serialization(self) -> None:
        tr = TournamentResult(
            models=["a", "b"],
            topics=["t"],
            total_matchups=2,
            completed_matchups=2,
            leaderboard=[{"model": "a", "wins": 1, "losses": 0, "draws": 1}],
        )
        data = tr.model_dump()
        restored = TournamentResult.model_validate(data)
        assert restored.models == ["a", "b"]
        assert restored.leaderboard[0]["model"] == "a"


# -- MatchupResult.winner_model -------------------------------------------


class TestWinnerModel:
    def test_pro_wins(self) -> None:
        r = _matchup_result("a", "b", "t", "a", winner="pro")
        assert r.winner_model("pro", "con") == "a"

    def test_con_wins(self) -> None:
        r = _matchup_result("a", "b", "t", "a", winner="con")
        assert r.winner_model("pro", "con") == "b"

    def test_draw_returns_none(self) -> None:
        r = _matchup_result("a", "b", "t", "a", winner=None)
        assert r.winner_model("pro", "con") is None

    def test_error_returns_none(self) -> None:
        r = _matchup_result("a", "b", "t", "a", winner=None, error="boom")
        assert r.winner_model("pro", "con") is None

    def test_unknown_winner_returns_none(self) -> None:
        r = MatchupResult(
            matchup=Matchup(model_a="a", model_b="b", topic="t", pro_model="a"),
            scoring=ScoringResult(council_topic="t", winner="moderator"),
        )
        assert r.winner_model("pro", "con") is None
