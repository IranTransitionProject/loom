"""Discussion protocols — pluggable turn-ordering and context-building.

Each protocol defines *who speaks when* and *what they see*.  The
:class:`CouncilRunner` and :class:`CouncilOrchestrator` delegate to
the active protocol every round.

Built-in protocols:
    - ``round_robin`` — all agents speak every round in config order
    - ``structured_debate`` — positions -> rebuttals -> closing (Phase 5)
    - ``delphi`` — anonymized positions with convergence feedback (Phase 5)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from heddle.contrib.council.schemas import AgentConfig, AgentTurn

if TYPE_CHECKING:
    from heddle.contrib.council.transcript import TranscriptStore


class DiscussionProtocol(ABC):
    """Abstract base for discussion protocols."""

    @abstractmethod
    def get_turn_order(
        self,
        round_num: int,
        agents: list[AgentConfig],
        transcript: TranscriptStore,
    ) -> list[AgentTurn]:
        """Determine who speaks this round and in what order."""

    @abstractmethod
    def build_agent_context(
        self,
        agent: AgentConfig,
        transcript: TranscriptStore,
        round_num: int,
        topic: str,
    ) -> dict[str, Any]:
        """Build the payload context for one agent's turn."""

    def set_agents(self, agents: list[AgentConfig]) -> None:
        """Hand the full agent roster to the protocol before round 1.

        Default is a no-op — only protocols that need a stable
        identity-to-label mapping across agents (e.g. Delphi
        anonymization) override this.  Runner + orchestrator must
        call this immediately after :func:`get_protocol` so the
        first round has the roster.
        """
        _ = agents


class RoundRobinProtocol(DiscussionProtocol):
    """All agents speak every round, in config order.

    Round 1: each agent states their initial position (no prior transcript).
    Round 2+: each agent sees the (filtered) transcript and responds.
    """

    def get_turn_order(  # noqa: D102  # noqa: D102
        self,
        round_num: int,
        agents: list[AgentConfig],
        transcript: TranscriptStore,
    ) -> list[AgentTurn]:
        return [AgentTurn(agent=a, round_num=round_num) for a in agents]

    def build_agent_context(  # noqa: D102  # noqa: D102
        self,
        agent: AgentConfig,
        transcript: TranscriptStore,
        round_num: int,
        topic: str,
    ) -> dict[str, Any]:
        # Get entries visible to this agent, up to previous round.
        prior_round = round_num - 1 if round_num > 1 else None
        visible = transcript.get_visible_turns(agent, up_to_round=prior_round)
        round_context = transcript.format_for_payload(visible, max_chars=transcript._max_chars)

        # Collect audience interjections from the prior round.
        interjections = transcript.get_interjections(since_round=prior_round)
        audience_block = _format_interjections(interjections)

        if round_num == 1:
            instructions = (
                "This is Round 1 of a team discussion.  State your initial "
                "position on the topic.  Be specific and substantive."
            )
        else:
            instructions = (
                f"This is Round {round_num}.  Review the discussion so far "
                f"and refine your position.  Address points raised by other "
                f"participants.  Note agreements and remaining disagreements."
            )

        ctx: dict[str, Any] = {
            "topic": topic,
            "round_num": round_num,
            "role": agent.role,
            "round_context": round_context,
            "instructions": instructions,
        }
        if audience_block:
            ctx["audience_reactions"] = audience_block
        return ctx


class StructuredDebateProtocol(DiscussionProtocol):
    """Three-phase structured debate: positions -> rebuttals -> closing.

    Round 1: Initial positions (no transcript).
    Round 2..N-1: Rebuttals — agents respond to specific points.
    Round N (final): Closing statements summarizing final position.
    """

    def get_turn_order(  # noqa: D102
        self,
        round_num: int,
        agents: list[AgentConfig],
        transcript: TranscriptStore,
    ) -> list[AgentTurn]:
        return [AgentTurn(agent=a, round_num=round_num) for a in agents]

    def build_agent_context(  # noqa: D102
        self,
        agent: AgentConfig,
        transcript: TranscriptStore,
        round_num: int,
        topic: str,
    ) -> dict[str, Any]:
        visible = transcript.get_visible_turns(
            agent, up_to_round=round_num - 1 if round_num > 1 else None
        )
        round_context = transcript.format_for_payload(visible, max_chars=transcript._max_chars)

        interjections = transcript.get_interjections(
            since_round=round_num - 1 if round_num > 1 else None
        )
        audience_block = _format_interjections(interjections)

        if round_num == 1:
            instructions = (
                "OPENING STATEMENT: Present your initial position on the "
                "topic.  Be clear, specific, and well-reasoned.  This is "
                "the foundation others will respond to."
            )
        else:
            instructions = (
                f"REBUTTAL (Round {round_num}): Review the discussion so "
                f"far.  Directly address specific points raised by other "
                f"participants.  Concede where appropriate, challenge where "
                f"you disagree, and propose synthesis where possible."
            )

        ctx: dict[str, Any] = {
            "topic": topic,
            "round_num": round_num,
            "role": agent.role,
            "round_context": round_context,
            "instructions": instructions,
        }
        if audience_block:
            ctx["audience_reactions"] = audience_block
        return ctx


class DelphiProtocol(DiscussionProtocol):
    """Delphi method — anonymized positions with convergence feedback.

    Agent identities are hidden in the transcript shown to participants.
    Each round includes the previous round's convergence score as feedback.

    The alias map (``agent_name -> "Participant A/B/C"``) is built
    from the council config's agent order via :meth:`set_agents` so
    every agent sees a consistent label for every other agent.
    Earlier the map was rebuilt from iteration order on each call,
    so a filtered transcript (visibility differences between agents)
    produced inconsistent labels — Agent X was "Participant A" in
    one agent's view and "Participant B" in another's.
    """

    _ALIAS_LIMIT: int = 26  # A..Z; raise rather than overflow into ASCII junk.

    def __init__(self) -> None:
        self._alias_map: dict[str, str] = {}

    def set_agents(self, agents: list[AgentConfig]) -> None:  # noqa: D102
        if len(agents) > self._ALIAS_LIMIT:
            msg = (
                f"DelphiProtocol supports at most {self._ALIAS_LIMIT} agents "
                f"(A..Z aliases); got {len(agents)}.  Use a different "
                "protocol or extend ``_build_alias_label`` to AA/AB/AC."
            )
            raise ValueError(msg)
        self._alias_map = {a.name: f"Participant {chr(65 + i)}" for i, a in enumerate(agents)}

    def get_turn_order(  # noqa: D102
        self,
        round_num: int,
        agents: list[AgentConfig],
        transcript: TranscriptStore,
    ) -> list[AgentTurn]:
        return [AgentTurn(agent=a, round_num=round_num) for a in agents]

    def build_agent_context(  # noqa: D102
        self,
        agent: AgentConfig,
        transcript: TranscriptStore,
        round_num: int,
        topic: str,
    ) -> dict[str, Any]:
        # Build anonymized transcript.
        visible = transcript.get_visible_turns(
            agent, up_to_round=round_num - 1 if round_num > 1 else None
        )
        anon_context = self._anonymize_transcript(visible, agent.name)

        # Audience interjections (not anonymized — they are public).
        interjections = transcript.get_interjections(
            since_round=round_num - 1 if round_num > 1 else None
        )
        audience_block = _format_interjections(interjections)

        # Include convergence feedback from prior round.
        convergence_feedback = ""
        if round_num > 1:
            rounds = transcript.rounds
            for r in rounds:
                if r.round_num == round_num - 1 and r.convergence_score is not None:
                    convergence_feedback = (
                        f"\nPrior round agreement score: "
                        f"{r.convergence_score:.2f} (0=total disagreement, "
                        f"1=full consensus)"
                    )
                    break

        if round_num == 1:
            instructions = (
                "This is a Delphi-method discussion.  State your position "
                "independently.  Other participants' identities are hidden "
                "to reduce anchoring bias."
            )
        else:
            instructions = (
                f"Round {round_num} of Delphi discussion.  Review the "
                f"anonymized positions below and revise yours if persuaded.  "
                f"Focus on the arguments, not who made them."
                f"{convergence_feedback}"
            )

        ctx: dict[str, Any] = {
            "topic": topic,
            "round_num": round_num,
            "role": agent.role,
            "round_context": anon_context,
            "instructions": instructions,
        }
        if audience_block:
            ctx["audience_reactions"] = audience_block
        return ctx

    def _anonymize_transcript(
        self,
        entries: list,
        self_name: str,
    ) -> str:
        """Replace agent names with Participant A/B/C labels.

        Uses ``self._alias_map`` built once from the council's agent
        roster.  Agents not in the map (audience interjections, e.g.)
        fall back to a deterministic late-binding label that doesn't
        collide with the config-order aliases (prefixed ``Unknown``).
        """
        blocks: list[str] = []
        # Late-binding aliases for any entry whose agent_name is
        # outside the configured roster.  Stable within one call and
        # never collides with the A..Z aliases above.
        unknown_map: dict[str, str] = {}
        for e in entries:
            if e.agent_name == self_name:
                label = "You"
            elif e.agent_name in self._alias_map:
                label = self._alias_map[e.agent_name]
            else:
                if e.agent_name not in unknown_map:
                    unknown_map[e.agent_name] = f"Unknown Participant {len(unknown_map) + 1}"
                label = unknown_map[e.agent_name]

            blocks.append(f"[Round {e.round_num}] {label}:\n{e.content}")

        return "\n\n".join(blocks)


# -- Interjection formatting -----------------------------------------


def _format_interjections(entries: list) -> str:
    """Format audience interjections as a clearly delineated block.

    The framing explicitly tells agents they may engage or ignore
    audience input — this is what makes it different from a panelist turn.
    """
    if not entries:
        return ""

    lines = []
    for e in entries:
        label = e.agent_name
        if e.role and e.role != "audience":
            label += f" ({e.role})"
        lines.append(f"- {label}: {e.content}")

    return (
        "[AUDIENCE REACTIONS]\n"
        + "\n".join(lines)
        + "\n\nYou may address audience points if relevant, or continue "
        "the main discussion."
    )


# -- Protocol registry -------------------------------------------------

PROTOCOL_REGISTRY: dict[str, type[DiscussionProtocol]] = {
    "round_robin": RoundRobinProtocol,
    "structured_debate": StructuredDebateProtocol,
    "delphi": DelphiProtocol,
}


def get_protocol(name: str) -> DiscussionProtocol:
    """Instantiate a protocol by name.

    Raises :class:`ValueError` if *name* is not in the registry.
    """
    cls = PROTOCOL_REGISTRY.get(name)
    if cls is None:
        msg = f"Unknown protocol: '{name}'. Available: {sorted(PROTOCOL_REGISTRY)}"
        raise ValueError(msg)
    return cls()
