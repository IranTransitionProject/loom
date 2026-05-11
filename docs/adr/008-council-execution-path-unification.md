# ADR-008: Council execution paths share one budget helper

**Status:** Accepted.
**Pairs with:** [ADR-007](007-council-budget-and-per-turn-floor.md)
(the per-turn and synthesis budget this helper enforces).
**Source commit:** `dcfb0df` (2026-05-11, B1).

## Context

The council framework has two execution paths:

- `CouncilOrchestrator` — runs over NATS via the
  `heddle.goals.incoming` actor. The path the framework's own
  examples use.
- `CouncilRunner` — runs in-process. The path used by the CLI
  (`heddle council run`), the MCP `council.run` tool
  (`src/heddle/mcp/council_bridge.py`), and tournament harnesses
  in `contrib/council/tournament.py`.

Commit `436ab2a` (ADR-007) enforced `synthesis_timeout_seconds` and
the 5s per-turn floor in `CouncilOrchestrator` only.
`CouncilRunner` had no `asyncio.wait_for` on either
`backend.complete` or `_synthesize`. A wedged local backend wedged
the CLI, MCP, and tournament paths indefinitely — every consumer
except the NATS one.

The review (`REPOSITORY_REVIEW_2026-05-10.md` §3.2, §8.2) flagged
this as a high-impact correctness gap and recommended unifying the
two paths rather than duplicating the wrapping logic.

The decision was how to share the timeout-enforcement code without
having one path quietly drift again.

## Decision

**Both execution paths route their `backend.complete`,
`bridge.send_turn`, and `_synthesize` calls through a shared helper
in `src/heddle/contrib/council/_budget.py`:**

- `CouncilTimeoutError(TimeoutError)` carries `label` (`"agent:X"`,
  `"synthesis"`, etc.) and `timeout_seconds` attributes for
  attribution. Subclassing the builtin `TimeoutError` means
  existing `except TimeoutError:` blocks still catch it without
  callsite changes.
- `call_with_budget(coro, *, timeout_seconds, label)` wraps the
  coroutine in `asyncio.wait_for` and raises `CouncilTimeoutError`
  on expiry.

Both `CouncilRunner._execute_agent_turn` /
`_execute_via_bridge` and `CouncilOrchestrator.handle_message`
import the helper and pass `cfg.per_turn_timeout()` (deliberation)
or `cfg.synthesis_timeout_seconds` (synthesis). A timed-out turn
records `[Timeout: <agent> did not respond within Ns]` in the
transcript; a timed-out synthesis records `[Synthesis timed out
after Ns]`. The shapes are deliberately identical across both
paths.

See:

- `src/heddle/contrib/council/_budget.py` (the helper).
- `src/heddle/contrib/council/runner.py:204,288,373` (runner sites).
- `src/heddle/contrib/council/orchestrator.py:165,210` (orchestrator
  sites).

## Alternatives considered

### Duplicate `asyncio.wait_for` inline in both paths (rejected)

Copy the orchestrator's `asyncio.wait_for(...)` wrappers into
`CouncilRunner` without extracting a helper.

- **Rejected because** the timeout-attribution shape — building the
  transcript entry, structured-log key, and exception type
  consistently — wants to be one piece of code. Two copies diverge
  on the first cosmetic edit and the second consumer of the
  budget logic forgets the new shape.
- Reviewer's note: "two paths, one budget" is the design goal; two
  paths and two budgets would just create the second drift
  opportunity.

### One concrete class with both code paths as methods (rejected)

Collapse `CouncilOrchestrator` and `CouncilRunner` into a single
class with two entry points (a `run_over_nats` and a `run_in_process`
method on the same class).

- **Rejected because** the two paths have genuinely different
  lifecycles: the orchestrator is a NATS subscriber actor with
  `BaseActor`'s `_wait_next_message` loop; the runner is a
  one-shot async function with no subscription. Merging them
  conflates the message-loop responsibility with the
  deliberation-loop responsibility.
- The shared piece is the budget enforcement, not the loop shape.
  Extract only what's shared; leave the rest separated.

### Decorator-based timeout (rejected)

Wrap `_execute_agent_turn` and `_synthesize` with a decorator that
reads `timeout_seconds` from `self.config`.

- **Rejected because** the timeout label varies per call
  (`"agent:critic"`, `"agent:proposer"`, `"synthesis"`) and a
  decorator that closes over `self` can't see it without
  introspection. The helper takes the label explicitly, which keeps
  the call site honest about what's being budgeted.

## Consequences

**Enables:**

- A wedged provider in any of CLI, MCP, tournament, or NATS paths
  surfaces as a `CouncilTimeoutError` with attribution rather
  than a hung process.
- The two paths can evolve budget semantics together: a future
  change to retry-on-timeout or partial-transcript-recovery lands
  in `_budget.py` once, not in two places.
- Subclassing `TimeoutError` means downstream code that catches
  `TimeoutError` (the MCP bridge's error reporting, tournament
  harnesses' result aggregation) continues to work without import
  changes.

**Costs:**

- `_execute_agent_turn` and `_execute_via_bridge` now require a
  `timeout_seconds` argument. External callers — if any future
  consumer subclasses `CouncilRunner` and overrides these — must
  pass it. No callers outside the module today; the cost is
  hypothetical but real.
- The label string is informal — `"agent:<name>"` and
  `"synthesis"` are conventions, not enum values. A future
  consumer that wants structured attribution would need to parse
  the string or accept the convention.
- One more module to read when investigating a council timeout
  (`_budget.py`). Mitigated by the cross-references from both
  call sites.
