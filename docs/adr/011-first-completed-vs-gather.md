# ADR-011: Pipeline parallel levels use FIRST_COMPLETED, not gather

**Status:** Accepted.
**Pairs with:** [Invariant 16](../DESIGN_INVARIANTS.md#16-pipeline-parallel-levels-use-first_completed-not-gather).

## Context

`PipelineOrchestrator` runs stages of a pipeline in topologically
ordered "levels": within one level, every stage's inputs are
already available, so all stages in that level can run
concurrently. Across levels, execution is sequential — level N+1
waits for level N.

The implementation question is how to await the concurrent stages
inside one level. Two `asyncio` primitives are candidates:

- `asyncio.gather(*tasks)` — runs all tasks concurrently; returns
  the full list of results when every task has completed (or
  raised).
- `asyncio.wait(tasks, return_when=FIRST_COMPLETED)` — runs all
  tasks concurrently; returns `(done, pending)` as soon as any
  single task completes. Caller iterates until `pending` is
  empty.

Functionally, both produce the same wall-clock latency for the
level — both end when the slowest stage in the level finishes. The
difference is in observability.

The baft audit pipeline has a parallel level with three auditor
stages (LA / PA / RT). Under `gather`, all three appeared to
complete simultaneously at the moment the slowest finished — the
Workshop UI and MCP `pipeline.run` consumers saw three
`stage.completed` events in a tight burst. The Workshop dashboard
became useless during long-running audits: a user could not tell
whether the level had any progress between minute 0 and minute 30.

The decision was whether to optimise for code simplicity (gather)
or for incremental observability (FIRST_COMPLETED).

## Decision

**`PipelineOrchestrator._run_parallel_level` (in
`src/heddle/orchestrator/pipeline.py:452-518`) uses
`asyncio.wait(..., return_when=FIRST_COMPLETED)` in a loop.** Each
stage's result is logged via `stage.completed` and stored in the
pipeline context as soon as it finishes, rather than waiting for
the entire level. The Workshop and MCP bridge see real-time
progress for every parallel level.

The general principle: **use `FIRST_COMPLETED` for any path that
streams results as they arrive. Use `gather` only where all
results are required before the next code can run, and incremental
visibility doesn't matter.**

Inside Heddle, "all results required before next code" is rare
because the result stream and the Workshop are both incremental
observers. The synthesizer is the one consumer that needs every
result (it builds a structured prompt from the bucketed
succeeded/failed/in_flight partitions per ADR-006), but it is fed
from the result-collection layer rather than awaiting a task list
directly, so it doesn't appear in this trade-off.

`asyncio.gather` does appear in the codebase — `pipeline.py:515`
uses it on the `pending` set during cleanup-on-cancel, where the
result of each pending task is `return_exceptions=True` and
explicitly ignored. That use is fine: it's a drain-and-discard, not
a result-streaming await.

## Alternatives considered

### `asyncio.gather` everywhere (rejected)

The simpler shape: `results = await asyncio.gather(*stages)`. One
line, no loop, no `done`/`pending` set bookkeeping.

- **Rejected because** the Workshop and MCP bridge see no
  progress during the parallel level. For a 30-minute audit
  level with three auditors, the operator sees no
  `stage.completed` events for 30 minutes — and three at the
  end. The framework's incremental observability story collapses
  inside parallel levels.
- The simplicity argument is genuine but the cost is paid every
  time someone watches a slow pipeline. Latency is the same,
  observability is strictly worse.

### `asyncio.as_completed(tasks)` (rejected)

Iterate over `asyncio.as_completed`, which yields tasks in
completion order. Roughly the same shape as `FIRST_COMPLETED` but
with a different iteration idiom.

- **Rejected because** `as_completed` doesn't expose the
  `pending` set directly — cancellation semantics on early exit
  (e.g. pipeline timeout firing mid-level) are harder to get
  right. The `FIRST_COMPLETED` loop holds the pending set
  explicitly, which makes the cancel-and-drain cleanup
  one-liner explicit.
- Stylistic; functionally close. `FIRST_COMPLETED` wins on
  cancellation clarity, not on streaming behaviour.

### Manual `asyncio.Queue` + `create_task` (rejected)

Each stage posts its result to a shared `asyncio.Queue`; the
parent loop drains the queue.

- **Rejected because** it adds a coordination primitive that
  `asyncio.wait` already provides. The queue's only advantage
  would be unifying with a hypothetical streaming consumer of
  pipeline results — but the result-stream layer already exists
  and lives outside `_run_parallel_level`.

## Consequences

**Enables:**

- Workshop dashboard shows per-stage progress in real time during
  parallel levels. The audit-pipeline UX justification is
  directly load-bearing.
- MCP `pipeline.run` clients see `stage.completed` events
  interleaved across the level, which lets them surface
  per-stage progress to their own consumers.
- Pipeline context is updated incrementally — a stage that
  completes early in the level can be observed by code inspecting
  the context (e.g. checkpoint manager) before the level ends.

**Costs:**

- The loop shape is genuinely more code than `gather`. The
  `done`/`pending` bookkeeping and the cancellation-on-error
  branch take ~30 lines that `gather` would compress into one.
- A future contributor who wants "simpler code" will naturally
  reach for `gather`. The invariant text plus this ADR exist so
  the rejection is durable; the rationale is observability, not
  correctness.
- Stages that complete out of order may write into the context
  in a different sequence than the pipeline's static
  declaration. Downstream code that depends on declaration
  order would break — but the existing
  dependency-inference (Invariant 6) already requires downstream
  stages to declare their inputs by name, so the declaration-
  order assumption is already disallowed.
