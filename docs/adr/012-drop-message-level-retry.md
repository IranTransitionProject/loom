# ADR-012: Drop message-level retry fields

**Status:** Accepted.
**Supersedes:** the documented-but-unimplemented retry contract on
`TaskMessage` and the `RETRY` value on `TaskStatus`.
**Pairs with:** [Invariant 1](../DESIGN_INVARIANTS.md#1-worker-statelessness-is-enforced-not-optional)
(stateless workers reset between tasks — unchanged by this decision).

## Context

`TaskMessage` carried two fields and `TaskStatus` carried one value
that together described a worker-side retry contract:

- `TaskMessage.max_retries: int = 2` — "Max retry attempts before
  permanent failure" (docstring).
- `TaskMessage.retry_count: int = 0` — "Incremented on each retry by
  TaskWorker" (docstring).
- `TaskStatus.RETRY` with the state-transition note
  `FAILED -> RETRY -> PROCESSING (handled by TaskWorker)`.

The 2026-05-10 repository review (§8.8) flagged the entire contract
as dead: no code path reads `task.max_retries`, no code path
increments `task.retry_count`, and no code path emits or consumes
the `RETRY` status. The fields and the enum value were aspirational
shims for a worker-side retry feature that was never built.

The 2026-05-11 wire-protocol externalisation (`schemas/v1/` published
in `bf13827`) made the dead surface load-bearing for the first time:
foreign-language SDKs would generate types for `retry_count`,
`max_retries`, and `RETRY` from the canonical JSON Schemas, treat
them as a stable contract, and bake them into their wire-handling
code. Once that happens, the fields can't be removed without a
breaking `schemas/v2/` migration.

The decision was forced by the schema publication: either implement
the retry contract before downstream SDKs lock to it, or remove it
while the schema is still `v1` and uncoupled from external consumers.

## Decision

**Drop the dead surface. Specifically:**

- Remove `max_retries` from `TaskMessage`.
- Remove `retry_count` from `TaskMessage`.
- Remove `RETRY` from `TaskStatus`.
- Regenerate `schemas/v1/task_message.schema.json` and
  `schemas/v1/task_result.schema.json` to reflect the trimmed
  shape. CI's schema-drift gate enforces synchronisation.

Retry semantics that DO exist in Heddle remain unchanged, and the
documentation now points to them explicitly:

- **Stage-level retry** lives in `PipelineOrchestrator` via the
  per-stage `max_retries: int` YAML field on each pipeline stage.
  Validated at point-of-use ([ADR-007 / G7](007-council-budget-and-per-turn-floor.md)
  is unrelated; the validation lives in
  `src/heddle/orchestrator/pipeline.py:340-348`).
- **Bus-level redelivery** is handled by NATS queue-group semantics:
  when a worker disconnects mid-task without acknowledging, NATS
  re-routes the message to another consumer in the same queue
  group. This is the framework's transparent retry path for
  worker-process failures.

Neither of these requires fields on `TaskMessage`. The first lives in
the pipeline's YAML config; the second is invisible to the
application layer.

## Alternatives considered

### Implement worker-side retry (rejected)

Wire the dead fields up: have `TaskWorker.handle_message` on
failure increment `task.retry_count` and republish the task if
`retry_count < max_retries`. This was the "make the docstring true"
option.

- **Rejected because** it would evolve [Invariant 1](../DESIGN_INVARIANTS.md#1-worker-statelessness-is-enforced-not-optional)
  from "stateless workers reset between tasks" to "stateless workers
  reset between tasks; retry handled at the message level." That's a
  meaningful change to the framework's safety contract — the
  worker's `reset()` would have to coordinate with whatever cached
  the in-flight task pending republish, and the queue-group dispatch
  guarantee ("one consumer per task") would gain an exception ("...
  unless the task is being retried, in which case the same consumer
  re-publishes").
- The pipeline-level `max_retries` already covers the documented
  use case ("the LLM failed transiently; try again"). The worker-
  level retry would only add value for the narrower case "the LLM
  succeeded but the network round-trip to NATS failed during
  publish," which the existing F1 publish-failure regression test
  pins as a fail-the-task path — the orchestrator timeout +
  per-stage retry recovers cleanly from there.
- No concrete workload has asked for worker-side retry. Adding
  Invariant-changing complexity for a hypothetical user violates
  the project's minimum-code discipline.

### Keep the fields, document them as reserved (rejected)

Leave the fields in `TaskMessage` and the value in `TaskStatus`,
but document them as "reserved for future use."

- **Rejected because** "reserved" fields on a wire contract still
  appear in generated SDK types, still occupy schema-versioning
  semantics, and still confuse readers who expect them to do
  something. A future implementation that "fills in" the reserved
  fields with different semantics than the original docstring
  implied would be a behavioural change masquerading as a
  feature-add.
- The cost of removal now is one commit; the cost of removal
  later (after the foreign-language SDKs are generated) is a
  `schemas/v2/` break.

### Defer the decision; ship the release with the dead fields (rejected)

Mark `schemas/v1/` as "experimental — wire shape may change before
v1.0" and decide K2 separately.

- **Rejected because** "experimental" is doc cover for "we haven't
  decided yet," and the decision is cheap. Deferral has a real cost
  (the dead surface keeps confusing every reader) and no real
  benefit (no concrete user is asking for worker-side retry that
  the deferral preserves the option for).

## Consequences

**Enables:**

- The wire contract documents only behaviours the framework
  actually implements. A foreign-language SDK author reading
  `schemas/v1/task_message.schema.json` sees the truth.
- Invariant 1 keeps its tight shape. The "stateless" claim is no
  longer caveated by docstrings that describe a feature that
  doesn't exist.
- Future contributors who want retries find the
  pipeline-stage path documented at the right level (stage YAML,
  not message envelope) and don't get steered toward the
  abandoned worker-side design.

**Costs:**

- This is a breaking change to `TaskMessage` and `TaskStatus`
  Python types. External code that constructed `TaskMessage`
  with `max_retries=` or `retry_count=`, or pattern-matched on
  `TaskStatus.RETRY`, will need to be updated.
  - Mitigation: no in-tree consumer outside test code did any of
    these things (verified by grep). The dead surface was
    genuinely dead.
- The `schemas/v1/*.schema.json` files change shape. CI's drift
  gate enforces the regeneration. Foreign-language SDKs that
  were already generated against the previous v1 shape (none
  today) would need to regenerate.
- If a concrete worker-side retry workload appears later, the
  reintroduction will need its own ADR + invariant evolution.
  That's the right time to design it — when the actual use case
  is concrete and the constraints can be honest, rather than
  pre-emptively shimmed.
