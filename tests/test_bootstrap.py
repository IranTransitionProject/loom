"""The composition-root aggregator registers built-in payload types.

`heddle.bootstrap` is the single import a process makes so that
`get_payload_model` / `parse` can resolve `WireEnvelope` bodies — the home of
the Q2 "bootstrap gotcha" contract. This pins that importing it actually
registers the shipped body types.
"""

from __future__ import annotations

from heddle.core.envelope import get_payload_model
from heddle.core.messages import OrchestratorGoal


def test_bootstrap_registers_core_payload_types() -> None:
    import heddle.bootstrap  # noqa: F401 — import runs the registration side effect

    assert get_payload_model("core.OrchestratorGoal") is OrchestratorGoal
