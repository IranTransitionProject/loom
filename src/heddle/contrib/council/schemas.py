"""Pydantic models for the council deliberation framework.

These models form the typed contract for multi-round agent discussions.
They are used by :class:`CouncilRunner` (NATS-free execution),
:class:`CouncilOrchestrator` (NATS-connected), and the MCP council bridge.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from heddle.core.messages import ModelTier


class AgentConfig(BaseModel):
    """Configuration for a single council agent.

    Each agent is backed by either an existing Heddle worker (via ``worker_type``)
    or an external chat bridge (via ``bridge``).  Exactly one must be set.
    """

    name: str
    worker_type: str | None = None
    bridge: str | None = None
    bridge_config: dict[str, Any] = Field(default_factory=dict)
    tier: ModelTier = ModelTier.STANDARD
    role: str = ""
    sees_transcript_from: list[str] = Field(default_factory=lambda: ["all"])
    max_tokens_per_turn: int = 2000

    @model_validator(mode="after")
    def _exactly_one_backend(self) -> AgentConfig:
        has_worker = self.worker_type is not None
        has_bridge = self.bridge is not None
        if has_worker == has_bridge:
            msg = "Exactly one of 'worker_type' or 'bridge' must be set"
            raise ValueError(msg)
        return self


class FacilitatorConfig(BaseModel):
    """Facilitator settings — the LLM that synthesizes and checks convergence."""

    tier: ModelTier = ModelTier.STANDARD
    synthesis_prompt: str = (
        "You are the facilitator of a multi-agent discussion.\n"
        "Synthesize the team's contributions into a coherent set of "
        "recommendations.  Highlight areas of agreement, unresolved "
        "tensions, and concrete action items."
    )
    convergence_prompt: str = (
        "Rate the level of agreement among the participants from 0.0 to 1.0.\n"
        'Respond with JSON only: {"score": 0.X, "reason": "..."}'
    )


class ConvergenceConfig(BaseModel):
    """Settings for convergence detection."""

    method: str = "none"  # "llm_judge", "position_stability", "none"
    threshold: float = 0.8
    backend_tier: ModelTier = ModelTier.STANDARD

    @model_validator(mode="after")
    def _valid_method(self) -> ConvergenceConfig:
        allowed = {"llm_judge", "position_stability", "none"}
        if self.method not in allowed:
            msg = f"convergence.method must be one of {sorted(allowed)}, got '{self.method}'"
            raise ValueError(msg)
        return self


class TranscriptEntry(BaseModel):
    """A single contribution within a discussion round.

    ``entry_type`` distinguishes panelist turns from audience
    interjections.  Agents may choose to engage with interjections
    or ignore them — the protocol presents them separately.
    """

    round_num: int
    agent_name: str
    role: str = ""
    content: str
    entry_type: str = "turn"  # "turn" | "interjection"
    token_count: int = 0  # prompt + completion (kept for back-compat)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model_used: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RoundEntry(BaseModel):
    """All contributions from one discussion round."""

    round_num: int
    entries: list[TranscriptEntry] = Field(default_factory=list)
    convergence_score: float | None = None


class AgentTurn(BaseModel):
    """Instruction for one agent's turn in the current round."""

    agent: AgentConfig
    round_num: int
    context: dict[str, Any] = Field(default_factory=dict)


class ConvergenceResult(BaseModel):
    """Result of a convergence check after a round."""

    converged: bool
    score: float
    reason: str = ""
    round_num: int


class CouncilResult(BaseModel):
    """Final output of a council deliberation."""

    topic: str
    rounds_completed: int
    converged: bool
    convergence_score: float | None = None
    synthesis: str
    transcript: list[RoundEntry] = Field(default_factory=list)
    agent_summaries: dict[str, str] = Field(default_factory=dict)
    total_token_usage: dict[str, int] = Field(default_factory=dict)
    elapsed_ms: int = 0
