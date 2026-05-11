"""Tests for council config loading and validation."""

import tempfile

import pytest
import yaml
from pydantic import ValidationError

from heddle.contrib.council.config import (
    CouncilConfig,
    load_council_config,
    validate_council_config,
)


def _minimal_config(**overrides):
    """Build a minimal valid council config dict."""
    cfg = {
        "name": "test_council",
        "agents": [
            {"name": "a1", "worker_type": "summarizer"},
            {"name": "a2", "worker_type": "reviewer"},
        ],
    }
    cfg.update(overrides)
    return cfg


class TestCouncilConfig:
    def test_minimal_valid(self):
        cfg = CouncilConfig(**_minimal_config())
        assert cfg.name == "test_council"
        assert len(cfg.agents) == 2
        assert cfg.protocol == "round_robin"
        assert cfg.max_rounds == 4

    def test_duplicate_agent_names_rejected(self):
        with pytest.raises(ValidationError, match="Duplicate"):
            CouncilConfig(
                **_minimal_config(
                    agents=[
                        {"name": "dup", "worker_type": "w1"},
                        {"name": "dup", "worker_type": "w2"},
                    ]
                )
            )

    def test_fewer_than_2_agents_rejected(self):
        with pytest.raises(ValidationError):
            CouncilConfig(**_minimal_config(agents=[{"name": "only", "worker_type": "w1"}]))

    def test_invalid_sees_transcript_from(self):
        with pytest.raises(ValidationError, match="unknown agent"):
            CouncilConfig(
                **_minimal_config(
                    agents=[
                        {
                            "name": "a1",
                            "worker_type": "w1",
                            "sees_transcript_from": ["nonexistent"],
                        },
                        {"name": "a2", "worker_type": "w2"},
                    ]
                )
            )

    def test_valid_sees_transcript_from(self):
        cfg = CouncilConfig(
            **_minimal_config(
                agents=[
                    {
                        "name": "a1",
                        "worker_type": "w1",
                        "sees_transcript_from": ["a2"],
                    },
                    {"name": "a2", "worker_type": "w2"},
                ]
            )
        )
        assert cfg.agents[0].sees_transcript_from == ["a2"]


class TestCouncilTimeouts:
    """Timeout-related config invariants — synthesis budget + per-turn floor."""

    def test_synthesis_timeout_default(self):
        cfg = CouncilConfig(**_minimal_config())
        assert cfg.synthesis_timeout_seconds == 60

    def test_synthesis_timeout_must_be_positive(self):
        with pytest.raises(ValidationError):
            CouncilConfig(**_minimal_config(synthesis_timeout_seconds=0))

    def test_per_turn_timeout_excludes_synthesis_budget(self):
        """``per_turn_timeout`` subtracts synthesis_timeout_seconds from total.

        Pins the formula so a future contributor changing
        ``per_turn_timeout`` keeps the synthesis carve-out — without it,
        the K3 fix dissolves and per-turn budgets silently include
        synthesis again.
        """
        cfg = CouncilConfig(
            **_minimal_config(
                timeout_seconds=600,
                synthesis_timeout_seconds=60,
                max_rounds=2,
                # 2 agents per _minimal_config()
            )
        )
        # (600 - 60) / (2 * 2) = 135s per turn.
        assert cfg.per_turn_timeout() == 135.0

    def test_implied_per_turn_below_floor_rejected(self):
        """5s floor on per-turn budget — frontier first-token latency
        is routinely 1-3s on cold-start, so 5s is the minimum below
        which a council run cannot reasonably make progress.

        Configures a 60s total with a 50s synthesis budget across 2
        rounds * 2 agents = 4 turns: per_turn = 10/4 = 2.5s -> reject.
        """
        with pytest.raises(ValidationError, match=r"below the .*floor"):
            CouncilConfig(
                **_minimal_config(
                    timeout_seconds=60,
                    synthesis_timeout_seconds=50,
                    max_rounds=2,
                )
            )

    def test_floor_exactly_5s_accepted(self):
        """Exactly 5s/turn is at the floor and should pass."""
        cfg = CouncilConfig(
            **_minimal_config(
                timeout_seconds=80,
                synthesis_timeout_seconds=40,
                max_rounds=2,
                # 2 agents, 2 rounds → 4 turns → (80-40)/4 = 10s
            )
        )
        assert cfg.per_turn_timeout() == 10.0


class TestLoadCouncilConfig:
    def test_load_valid_yaml(self):
        raw = _minimal_config()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(raw, f)
            path = f.name

        cfg = load_council_config(path)
        assert cfg.name == "test_council"

    def test_load_missing_file(self):
        with pytest.raises(FileNotFoundError):
            load_council_config("/nonexistent/path.yaml")


class TestValidateCouncilConfig:
    def test_valid_returns_empty(self):
        assert validate_council_config(_minimal_config()) == []

    def test_missing_name(self):
        raw = _minimal_config()
        del raw["name"]
        errors = validate_council_config(raw)
        assert any("name" in e for e in errors)

    def test_missing_agents(self):
        errors = validate_council_config({"name": "x"})
        assert any("agents" in e for e in errors)

    def test_too_few_agents(self):
        errors = validate_council_config(
            {"name": "x", "agents": [{"name": "one", "worker_type": "w"}]}
        )
        assert any("2 agents" in e for e in errors)

    def test_invalid_protocol(self):
        raw = _minimal_config(protocol="nonexistent")
        errors = validate_council_config(raw)
        assert any("protocol" in e.lower() for e in errors)

    def test_invalid_convergence_method(self):
        raw = _minimal_config(convergence={"method": "magic"})
        errors = validate_council_config(raw)
        assert any("method" in e.lower() for e in errors)

    def test_not_dict(self):
        assert validate_council_config("not a dict") == ["Config must be a dict"]

    def test_validate_per_turn_floor(self):
        """``validate_council_config`` flags the same per-turn floor that
        ``CouncilConfig.model_validator`` raises on load.

        Both paths matter: the model validator catches eager loaders
        (e.g. ``load_council_config``); the dict-validator catches the
        smoke-test path that runs every example config through it.
        """
        errors = validate_council_config(
            _minimal_config(
                timeout_seconds=60,
                synthesis_timeout_seconds=50,
                max_rounds=2,
            )
        )
        assert any("below the" in e and "floor" in e for e in errors), errors

    def test_validate_synthesis_timeout_must_be_positive(self):
        errors = validate_council_config(_minimal_config(synthesis_timeout_seconds=0))
        assert any("synthesis_timeout_seconds" in e for e in errors), errors
