# ADR-006: Tri-state synthesizer partition

**Status:** Accepted.
**Pairs with:** [Invariant 15](../DESIGN_INVARIANTS.md#15-resultstream-is-single-use-and-subscription-scoped)
(adjacent: pinned by the late-result-after-timeout drop test in
`tests/test_orchestrator.py:743+`).
**Source commit:** `15a9af4` (2026-05-10).

## Context

`Synthesizer._partition` is the helper that splits a list of
`TaskResult`s by their `TaskStatus` so the LLM synthesis prompt and
the merged-output dict can render each bucket separately. Its
original shape returned a 2-tuple `(succeeded, failed)` — anything
that wasn't `COMPLETED` was lumped into the second bucket.

Two `TaskStatus` values are not terminal: `PENDING` and
`PROCESSING`. Under the 2-tuple shape they collapsed into `failed`,
so the synthesizer's LLM prompt told the model "these workers
failed" when the workers were in fact still running — a different
epistemic state from "responded with an error."

(A third non-terminal status, `RETRY`, existed at the time this ADR
was written but was removed in ADR-012; the partition's `in_flight`
bucket is now defined as "every non-terminal status," which is the
load-bearing contract.)

The bug was not observable in the current caller: the dynamic
`OrchestratorActor` (commit `cc49783`) converts every pending task to
a synthetic `FAILED` placeholder before reaching the synthesizer, so
no caller passes non-terminal state today. The 2-tuple "worked"
because no live in-flight result ever arrived at `_partition`.

The decision was whether to leave the API shape latently incorrect
for a future caller, or pin the distinction at the contract level.

## Decision

**`_partition` returns `dict[str, list[TaskResult]]` with three keys:
`"succeeded"`, `"failed"`, `"in_flight"`. `merge()` output and the
LLM synthesis prompt both render the three buckets separately, with
the in-flight section explicitly labelled `STILL-IN-FLIGHT TASKS (no
output yet, treat as missing not failed)`.**

The three states map to `TaskStatus` as:

- `succeeded` — `TaskStatus.COMPLETED`.
- `failed` — `TaskStatus.FAILED`.
- `in_flight` — any non-terminal status (today `PENDING` or `PROCESSING`).

See `src/heddle/orchestrator/synthesizer.py:306-340` for the
partition; `:358-394` for the prompt-rendering split.

## Alternatives considered

### Keep the 2-tuple, fix at the call site (rejected)

Leave `_partition` returning `(succeeded, failed)` and require every
caller to pre-filter in-flight results into a synthetic `FAILED`
placeholder, as the dynamic orchestrator already does (`cc49783`).

- **Rejected because** "every caller must pre-filter" is exactly
  the kind of contract that exists only in the head of whoever
  wrote the helper. A future caller — Workshop, MCP bridge, a
  council variant — that calls `_partition` directly will hit the
  silent relabel.
- The reviewer's note "pin the API for future callers so the
  guarantee is contract-level, not coincidental" is the textbook
  ADR criterion: the current behaviour is correct by accident, not
  by design.

### Three-tuple `(succeeded, failed, in_flight)` (rejected)

Same information, positional unpacking. Smaller diff.

- **Rejected because** adding a fourth bucket later (e.g. `timed_out`
  if the late-result-drop semantics ever evolve into a separate
  status) would silently break every existing
  `succeeded, failed, in_flight = _partition(results)` unpack.
- A dict forces callers to name the keys they care about. New
  buckets are additive, not breaking.

### Boolean flag on the existing `(succeeded, failed)` tuple (rejected)

Pass an `include_in_flight: bool` flag to `_partition` that adds a
third element when set.

- **Rejected because** the flag would be set by every caller (the
  in-flight bucket is useful information; nobody wants the lossy
  2-tuple). An always-set flag is a code smell — collapse it back
  into the default shape.

## Consequences

**Enables:**

- The LLM synthesis prompt can distinguish "still running" from
  "responded with error" — the model no longer has to guess
  whether a missing worker output reflects an error or a
  timeout-with-late-completion.
- The merged-output dict exposes `in_flight` so callers (Workshop,
  MCP bridge) can render "N workers still processing" instead of
  collapsing them into the failure count.
- Future callers that legitimately pass non-terminal state (e.g.
  a streaming-synthesizer variant that emits partial results
  before all workers finish) inherit the right semantics without
  another contract change.

**Costs:**

- Callers must handle three buckets rather than two. Inside Heddle
  the only caller today is the synthesizer itself; the cost lands
  on any future external consumer of the merge output.
- The late-result-after-timeout drop test (`tests/test_orchestrator.py:743+`)
  is now load-bearing: in-flight results may complete after the
  outer timeout, and the orchestrator drops them rather than
  forwarding them to a closed result stream. Removing that test
  re-opens the result-stream-after-close failure mode that the
  tri-state partition is supposed to surface honestly.
