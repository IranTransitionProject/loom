"""Tests for discussion protocols."""

import pytest

from heddle.contrib.council.protocol import (
    DelphiProtocol,
    DiscussionProtocol,
    RoundRobinProtocol,
    StructuredDebateProtocol,
    get_protocol,
)
from heddle.contrib.council.schemas import AgentConfig, TranscriptEntry
from heddle.contrib.council.transcript import TranscriptStore


def _agents(n=3):
    return [AgentConfig(name=f"agent_{i}", worker_type="w", role=f"Role {i}") for i in range(n)]


def _populated_transcript():
    """Build a transcript with one round of entries."""
    store = TranscriptStore()
    store.start_round(1)
    store.add_entry(TranscriptEntry(round_num=1, agent_name="agent_0", content="Position A"))
    store.add_entry(TranscriptEntry(round_num=1, agent_name="agent_1", content="Position B"))
    return store


class TestGetProtocol:
    def test_valid_names(self):
        for name in ("round_robin", "structured_debate", "delphi"):
            p = get_protocol(name)
            assert isinstance(p, DiscussionProtocol)

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown protocol"):
            get_protocol("nonexistent")


class TestRoundRobinProtocol:
    def test_all_agents_every_round(self):
        p = RoundRobinProtocol()
        agents = _agents(3)
        store = TranscriptStore()

        turns = p.get_turn_order(1, agents, store)
        assert len(turns) == 3
        assert [t.agent.name for t in turns] == ["agent_0", "agent_1", "agent_2"]

    def test_round1_context_has_no_prior_transcript(self):
        p = RoundRobinProtocol()
        agents = _agents(2)
        store = TranscriptStore()

        ctx = p.build_agent_context(agents[0], store, round_num=1, topic="test")
        assert ctx["topic"] == "test"
        assert ctx["round_num"] == 1
        assert ctx["round_context"] == ""
        assert "Round 1" in ctx["instructions"]

    def test_round2_context_includes_prior_transcript(self):
        p = RoundRobinProtocol()
        agents = _agents(2)
        store = _populated_transcript()

        ctx = p.build_agent_context(agents[0], store, round_num=2, topic="test")
        assert "Position A" in ctx["round_context"]
        assert "Position B" in ctx["round_context"]
        assert "Round 2" in ctx["instructions"]

    def test_visibility_filtering(self):
        p = RoundRobinProtocol()
        restricted = AgentConfig(
            name="restricted",
            worker_type="w",
            sees_transcript_from=["agent_0"],
        )
        store = _populated_transcript()

        ctx = p.build_agent_context(restricted, store, round_num=2, topic="test")
        assert "Position A" in ctx["round_context"]
        assert "Position B" not in ctx["round_context"]


class TestStructuredDebateProtocol:
    def test_all_agents_speak_each_round(self):
        p = StructuredDebateProtocol()
        agents = _agents(2)
        store = TranscriptStore()

        turns = p.get_turn_order(1, agents, store)
        assert len(turns) == 2

    def test_round1_is_opening(self):
        p = StructuredDebateProtocol()
        agent = _agents(1)[0]
        store = TranscriptStore()

        ctx = p.build_agent_context(agent, store, round_num=1, topic="test")
        assert "OPENING" in ctx["instructions"]

    def test_round2_is_rebuttal(self):
        p = StructuredDebateProtocol()
        agent = _agents(1)[0]
        store = _populated_transcript()

        ctx = p.build_agent_context(agent, store, round_num=2, topic="test")
        assert "REBUTTAL" in ctx["instructions"]


class TestDelphiProtocol:
    def test_anonymizes_other_agents(self):
        p = DelphiProtocol()
        p.set_agents(_agents(3))
        agent = AgentConfig(name="agent_0", worker_type="w")
        store = _populated_transcript()

        ctx = p.build_agent_context(agent, store, round_num=2, topic="test")
        # Own entries show "You"; agent_1 keeps its config-order
        # alias ("Participant B" because agent_0 is "A" in the
        # roster — the alias is tied to config position, not to
        # whether the self agent's turn is present in the view).
        assert "You" in ctx["round_context"]
        assert "Participant B" in ctx["round_context"]
        # Real names should NOT appear.
        assert "agent_1" not in ctx["round_context"]

    def test_convergence_feedback_in_round2(self):
        p = DelphiProtocol()
        p.set_agents(_agents(3))
        agent = _agents(1)[0]
        store = _populated_transcript()
        store.set_convergence_score(1, 0.6)

        ctx = p.build_agent_context(agent, store, round_num=2, topic="test")
        assert "0.60" in ctx["instructions"]

    def test_round1_instructions(self):
        p = DelphiProtocol()
        p.set_agents(_agents(1))
        agent = _agents(1)[0]
        store = TranscriptStore()

        ctx = p.build_agent_context(agent, store, round_num=1, topic="test")
        assert "Delphi" in ctx["instructions"]

    def test_alias_map_stable_across_filtered_views(self):
        """Pin the bug B3 closed.

        Earlier ``_anonymize_transcript`` rebuilt labels from
        iteration order on every call.  When agent X had visibility
        only into a subset of agents (or a transcript where the
        first turn was filtered out), the labels reassigned and the
        same real agent could be "Participant A" in one view and
        "Participant B" in another.  Now the map is built from the
        council config's agent order once via ``set_agents`` and
        stays stable across all views.
        """
        p = DelphiProtocol()
        agents = _agents(3)
        p.set_agents(agents)

        # Full view: agent_2 (a non-self) reads everyone else.
        store_full = TranscriptStore()
        store_full.start_round(1)
        store_full.add_entry(TranscriptEntry(round_num=1, agent_name="agent_0", content="A0"))
        store_full.add_entry(TranscriptEntry(round_num=1, agent_name="agent_1", content="A1"))

        # Filtered view: same agent's view, but agent_0's turn has
        # been dropped (e.g. via ``sees_transcript_from`` visibility
        # rules).  Without B3, agent_1 would re-label as
        # "Participant A" here because iteration order changed.
        store_filtered = TranscriptStore()
        store_filtered.start_round(1)
        store_filtered.add_entry(TranscriptEntry(round_num=1, agent_name="agent_1", content="A1"))

        ctx_full = p.build_agent_context(agents[2], store_full, round_num=2, topic="t")
        ctx_filtered = p.build_agent_context(agents[2], store_filtered, round_num=2, topic="t")

        # agent_1's label stays "Participant B" in both views.
        assert "Participant B" in ctx_full["round_context"]
        assert "Participant B" in ctx_filtered["round_context"]
        # agent_0's label is consistent too — appears as "Participant A"
        # in the full view (and absent from the filtered view).
        assert "Participant A" in ctx_full["round_context"]

    def test_raises_when_more_than_26_agents(self):
        """A..Z aliases overflow into ASCII junk past 26 agents.

        Raise at ``set_agents`` so the misconfiguration surfaces
        before the first turn rather than producing labels like
        ``Participant [`` mid-discussion.
        """
        p = DelphiProtocol()
        many = [AgentConfig(name=f"a{i}", worker_type="w") for i in range(27)]
        with pytest.raises(ValueError, match="at most 26 agents"):
            p.set_agents(many)
