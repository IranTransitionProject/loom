"""Council configuration loading and validation.

Loads council YAML configs into typed :class:`CouncilConfig` models.
Follows the same pattern as :func:`heddle.core.config.load_config`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from heddle.contrib.council.schemas import (
    AgentConfig,
    ConvergenceConfig,
    FacilitatorConfig,
)

# Absolute floor on per-turn timeout — a frontier-tier first-token
# latency is routinely 1-3s on cold-start, so 5s is the minimum below
# which a council run cannot reasonably make progress.  Configs that
# imply a smaller per-turn budget are rejected at load time so the
# operator finds out before the first task dispatches, not at the
# first turn's silent timeout.
_PER_TURN_TIMEOUT_FLOOR_SECONDS: int = 5


class CouncilConfig(BaseModel):
    """Top-level council configuration, loaded from YAML.

    Two timeouts compose the overall budget:

    - ``timeout_seconds`` — total wall time for the council run.
    - ``synthesis_timeout_seconds`` — the budget reserved for the
      facilitator's synthesis call at the end.  Subtracted from the
      total to compute the per-turn budget.

    Why a separate synthesis budget: the original shape divided
    ``timeout_seconds`` evenly across ``max_rounds * len(agents)``
    turns, leaving synthesis unbounded — which masked spurious
    timeouts (the synthesis could wedge indefinitely on a frontier
    model) and made per-turn budgeting fragile.  Carving synthesis
    out makes both halves explicit and bounded.
    """

    name: str
    protocol: str = "round_robin"
    max_rounds: int = 4
    timeout_seconds: int = 600
    synthesis_timeout_seconds: int = Field(default=60, ge=1)
    convergence: ConvergenceConfig = Field(default_factory=ConvergenceConfig)
    agents: list[AgentConfig] = Field(min_length=2)
    facilitator: FacilitatorConfig = Field(default_factory=FacilitatorConfig)

    @model_validator(mode="after")
    def _validate_agent_refs(self) -> CouncilConfig:
        names = {a.name for a in self.agents}

        # Check for duplicate names.
        if len(names) != len(self.agents):
            seen: set[str] = set()
            dupes: list[str] = []
            for a in self.agents:
                if a.name in seen:
                    dupes.append(a.name)
                seen.add(a.name)
            msg = f"Duplicate agent names: {dupes}"
            raise ValueError(msg)

        # Validate sees_transcript_from references.
        for a in self.agents:
            for ref in a.sees_transcript_from:
                if ref != "all" and ref not in names:
                    msg = (
                        f"Agent '{a.name}' references unknown agent '{ref}' in sees_transcript_from"
                    )
                    raise ValueError(msg)

        # Synthesis must leave a usable per-turn budget.
        per_turn_total = self.timeout_seconds - self.synthesis_timeout_seconds
        turn_count = max(self.max_rounds * len(self.agents), 1)
        per_turn = per_turn_total / turn_count
        if per_turn < _PER_TURN_TIMEOUT_FLOOR_SECONDS:
            msg = (
                f"Implied per-turn timeout is "
                f"{per_turn:.1f}s "
                f"(({self.timeout_seconds} - {self.synthesis_timeout_seconds}) / "
                f"({self.max_rounds} rounds * {len(self.agents)} agents)), "
                f"below the {_PER_TURN_TIMEOUT_FLOOR_SECONDS}s floor.  "
                "Raise timeout_seconds, lower synthesis_timeout_seconds, "
                "or reduce max_rounds * agents."
            )
            raise ValueError(msg)

        return self

    def per_turn_timeout(self) -> float:
        """Compute the per-turn timeout in seconds.

        Centralised so the orchestrator and any future caller agree on
        the formula.  Subtracts ``synthesis_timeout_seconds`` from the
        total budget and divides across rounds * agents.
        """
        return (self.timeout_seconds - self.synthesis_timeout_seconds) / max(
            self.max_rounds * len(self.agents), 1
        )


def load_council_config(path: str | Path) -> CouncilConfig:
    """Load a council YAML config and return a validated model.

    Raises:
        FileNotFoundError: If *path* does not exist.
        pydantic.ValidationError: If the YAML content is invalid.
    """
    path = Path(path)
    with path.open() as f:
        raw = yaml.safe_load(f)
    return CouncilConfig(**raw)


def validate_council_config(raw: dict[str, Any]) -> list[str]:
    """Validate a raw dict as a council config, returning error strings.

    Returns an empty list if valid.  This mirrors the pattern of
    :func:`heddle.core.config.validate_worker_config`.
    """
    errors: list[str] = []

    if not isinstance(raw, dict):
        return ["Config must be a dict"]

    if "name" not in raw:
        errors.append("Missing required key: 'name'")

    agents = raw.get("agents")
    if agents is None:
        errors.append("Missing required key: 'agents'")
    elif not isinstance(agents, list):
        errors.append("'agents' must be a list")
    elif len(agents) < 2:
        errors.append("At least 2 agents are required")

    protocol = raw.get("protocol", "round_robin")
    from heddle.contrib.council.protocol import PROTOCOL_REGISTRY

    if protocol not in PROTOCOL_REGISTRY:
        errors.append(f"Unknown protocol '{protocol}'. Available: {sorted(PROTOCOL_REGISTRY)}")

    convergence = raw.get("convergence", {})
    if isinstance(convergence, dict):
        method = convergence.get("method", "none")
        if method not in {"llm_judge", "position_stability", "none"}:
            errors.append(
                f"convergence.method must be one of "
                f"'llm_judge', 'position_stability', 'none'; got '{method}'"
            )

    # Timeout sanity: synthesis must leave a usable per-turn budget.
    # Re-check here so callers using ``validate_council_config`` (e.g.
    # the smoke test in ``tests/test_examples_smoke.py``) catch the
    # same gotcha that ``CouncilConfig.model_validator`` raises on load.
    timeout_seconds = raw.get("timeout_seconds", 600)
    synthesis_timeout_seconds = raw.get("synthesis_timeout_seconds", 60)
    max_rounds = raw.get("max_rounds", 4)
    if (
        isinstance(timeout_seconds, int)
        and isinstance(synthesis_timeout_seconds, int)
        and isinstance(max_rounds, int)
        and isinstance(agents, list)
        and len(agents) >= 1
    ):
        if synthesis_timeout_seconds < 1:
            errors.append("synthesis_timeout_seconds must be >= 1")
        else:
            per_turn_total = timeout_seconds - synthesis_timeout_seconds
            turn_count = max(max_rounds * len(agents), 1)
            per_turn = per_turn_total / turn_count
            if per_turn < _PER_TURN_TIMEOUT_FLOOR_SECONDS:
                errors.append(
                    f"Implied per-turn timeout is {per_turn:.1f}s, "
                    f"below the {_PER_TURN_TIMEOUT_FLOOR_SECONDS}s floor"
                )

    return errors
