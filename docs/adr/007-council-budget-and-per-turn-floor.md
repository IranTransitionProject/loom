# ADR-007: Council synthesis budget and 5s per-turn floor

**Status:** Accepted.
**Pairs with:** [council-howto](../council-howto.md) — operator-facing
formula and tuning guidance.
**Source commits:** `436ab2a` (2026-05-10, K3 — synthesis budget +
floor); `dcfb0df` (2026-05-11, B1 — apply the same budget to
`CouncilRunner`, see [ADR-008](008-council-execution-path-unification.md)).

## Context

The council orchestrator runs a multi-agent deliberation:
`max_rounds * len(agents)` per-turn LLM calls, followed by a
facilitator synthesis call that produces the final position. The
original shape had two failure modes:

1. **Unbounded synthesis.** Synthesis ran with no `asyncio.wait_for`
   wrapper. A wedged frontier-tier provider — token streaming
   stalled, TCP reset never reaching the client, rate-limit
   backoff misbehaving — would hang the entire council indefinitely.
   Operators saw a stuck goal that the
   `recover-stuck-orchestrator-goal` runbook had to clear by hand.
2. **Silently-shrinking per-turn budget.** `cfg.timeout_seconds` was
   divided evenly across `max_rounds * len(agents)` per-turn slots.
   At 60s / 4 rounds / 3 agents the implied per-turn was 5s — and at
   90s / 6 rounds / 4 agents it dropped to 3.75s, below the
   first-token cold-start latency of frontier providers. Configs
   accepted at load time produced silent per-turn timeouts at runtime.

Two related decisions: how much of the total council budget should
be carved out for synthesis, and what minimum per-turn budget should
the framework refuse to accept?

## Decision

**`CouncilConfig.synthesis_timeout_seconds` carves a dedicated
synthesis budget out of `timeout_seconds`. The remainder is divided
across per-turn slots via the shared `CouncilConfig.per_turn_timeout()`
helper. The framework rejects any config whose implied per-turn
budget falls below 5 seconds at load time.**

Numerics:

- `synthesis_timeout_seconds: int = Field(default=60, ge=1)`.
- `per_turn = (timeout_seconds - synthesis_timeout_seconds) / max(max_rounds * len(agents), 1)`.
- `_PER_TURN_TIMEOUT_FLOOR_SECONDS = 5` (defined as a private
  module-level constant in `contrib/council/config.py:27`).

A config that violates the floor raises `ValueError` from the
Pydantic `model_validator` with a message that names every input to
the formula:

```text
Implied per-turn timeout is 3.75s
(( 90 - 60 ) / ( 6 rounds * 4 agents )),
below the 5s floor. Raise timeout_seconds, lower
synthesis_timeout_seconds, or reduce max_rounds * agents.
```

See `src/heddle/contrib/council/config.py:81-108` for the validator
and the helper.

## Alternatives considered

### No synthesis-specific budget (rejected)

Apply the per-turn budget to synthesis as well; remove
`synthesis_timeout_seconds` entirely.

- **Rejected because** synthesis and per-turn deliberation have
  different shapes: synthesis is one long completion against the
  full transcript, deliberation is many short turns. Forcing them
  to share a single budget either under-budgets synthesis (the
  long-context call gets the same 5-20s as a one-line turn) or
  over-budgets deliberation (the long completion's budget bloats
  every turn slot).
- A wedged synthesis with no dedicated timeout is the original
  failure mode — the rejection rationale and the bug are the same.

### Higher floor (10s, rejected)

Set the floor at 10s to give frontier providers a comfortable
generation budget on top of first-token latency.

- **Rejected because** legitimate local-tier rapid-fire configs
  (LM Studio with `qwen3:0.6b` for adversarial-challenge agents
  that emit single-line objections) finish per-turn well under
  10s. A 10s floor would reject configs that work fine in
  production for the local tier — the framework would prefer
  safety over operator autonomy in a way that doesn't match the
  cost/risk profile.
- A frontier-tier user can raise `timeout_seconds` to clear the
  floor; a local-tier user can't lower it.

### No floor at all (rejected)

Trust the operator. Accept any positive per-turn budget. Let
runtime timeouts surface misconfigurations.

- **Rejected because** the silent-shrinkage failure mode is hard
  to recognise — the operator sees "council never produced a
  final result" and chases the wrong cause (provider outage,
  synthesizer bug). Surfacing the misconfiguration at config-load
  time turns a runtime mystery into a one-line error.
- Frontier-tier first-token latency is empirically 1-3s on
  cold-start; anything below 5s rejects valid configs while
  leaving genuinely small budgets to fail silently. 5s is the
  smallest floor that excludes "must be wrong" without excluding
  "small but legitimate."

### Validate per-agent rather than uniformly (rejected)

Different agents have different model tiers. A council with one
frontier agent and three local agents arguably shouldn't apply a
uniform 5s floor.

- **Rejected because** the per-turn timeout is enforced at the
  framework level, where `asyncio.wait_for` doesn't know which
  agent owns the current turn. Per-agent timeouts would require
  threading the agent context into the budget helper, which the
  current single-budget shape avoids.
- A future refactor could thread `AgentConfig` through
  `call_with_budget` and apply tier-specific floors. Out of scope
  for now; the uniform floor is the simpler invariant.

## Consequences

**Enables:**

- Wedged providers cannot hang a council indefinitely — every
  agent turn and the synthesis are wrapped in
  `call_with_budget` (see [ADR-008](008-council-execution-path-unification.md)
  for the shared helper).
- Misconfigured per-turn budgets surface as a `ValueError` from
  the Pydantic validator before the first task dispatches.
  Operators learn about a bad config at `heddle council validate`,
  not at "the council appears to have stopped responding."
- The formula is exposed via `CouncilConfig.per_turn_timeout()` so
  the orchestrator, the runner, and any future caller agree on
  the inputs and the arithmetic.

**Costs:**

- Configs that pre-date this validation may be rejected at load
  time without code changes. The error message lists every input
  so the fix is mechanical, but operators upgrading from older
  versions may need to raise `timeout_seconds` or lower
  `max_rounds * agents`.
- Synthesis with `synthesis_timeout_seconds=60` rejects on a
  cold-start frontier provider that takes longer than a minute
  to assemble the synthesis. Operators with very long transcripts
  may need to raise the synthesis budget separately from the
  total — the two fields are independent on purpose, but that
  independence is a tuning surface to learn.
- The floor is a magic number. Future contributors who try to
  lower it for "rejecting valid configs" need to find this ADR
  and the empirical claim about first-token latency rather than
  edit the constant blind.
