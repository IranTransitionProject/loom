# ADR-002: Deterministic router, no LLM in routing path

**Status:** Accepted.
**Pairs with:** [Invariant 2](../DESIGN_INVARIANTS.md#2-the-router-is-deterministic--no-llm-in-the-routing-path).

## Context

The router sits between `heddle.tasks.incoming` and the per-worker
subjects (`heddle.tasks.{worker_type}.{tier}`). Every task in the
system flows through it. Its job:

1. Resolve the model tier (apply `router_rules.yaml` overrides).
2. Enforce rate limits per tier.
3. Publish to the resolved subject — or to
   `heddle.tasks.dead_letter` if unroutable.

The decision to make: should the router make routing decisions via
LLM inference, or via static rules?

## Decision

**The router uses deterministic rule lookups. No LLM call is made
in the routing path. Routing is by `worker_type` and resolved
`model_tier`, both keys that come from the incoming `TaskMessage`
itself.**

The router code in `heddle.router.router` is plain Python: enum
lookups, dict accesses, a token-bucket rate limiter. No backend
call, no inference, no async LLM round-trip.

## Alternatives considered

### LLM-based intent classification (rejected)

The router could call a small fast LLM to classify each incoming
task and pick a `worker_type` based on the inferred intent —
useful for accepting free-form goal strings without requiring the
sender to know the worker taxonomy.

- **Rejected because** routing latency would dominate end-to-end
  latency for fast tasks (a 200ms classifier call on top of a
  300ms worker call doubles the user-visible latency).
- LLM classifiers fail in distribution-dependent ways. A new
  worker type or a worker rename means re-training or prompt-
  engineering the classifier. A static rule means editing a
  YAML.
- Every task pays the classifier cost, including tasks where the
  sender already knows the right `worker_type`. The vast majority
  of in-pipeline tasks fall in that category — the orchestrator
  decomposed them, so it already named them correctly.
- Failure modes are opaque. A misclassification produces a
  silently wrong route; debugging it requires re-running the
  classifier on the failed payload.

### Pluggable router with LLM as one option (rejected)

A pluggable router base class with multiple implementations
(deterministic, LLM-based, ML-based) selectable via config.

- **Rejected because** the routing layer is on every task's hot
  path. Indirection through a base class with multiple
  implementations makes the determinism guarantee harder to
  audit. If even one implementation is non-deterministic, the
  guarantee becomes "it depends" — which is what the invariant
  exists to prevent.
- Plugin points have a way of accreting. Today's "LLM router as
  a plugin" becomes tomorrow's "we ship two routers and nobody
  remembers which is in production."

### Push the LLM-intent step upstream into the orchestrator (accepted)

The decomposition step in `OrchestratorActor` already uses an LLM
to convert a free-form goal into structured subtasks (each with a
named `worker_type`). This is the right place for natural-language
→ structured-task translation — it runs once per goal, not once
per task, and its output is structured enough that the router can
treat it deterministically.

The router is "downstream" of intent. By the time a task hits the
router, intent has already been resolved into a concrete worker
type and tier. The router's job is plumbing, not interpretation.

## Consequences

**Enables:**

- Predictable router latency (microseconds, not hundreds of
  milliseconds).
- Routing decisions are auditable from logs and config alone — no
  need to replay an LLM call to understand why a task went where
  it went.
- The router is testable without LLM mocks. The test suite covers
  routing semantics exhaustively (`test_router.py`) at sub-second
  cost.
- Rate limiting is per-tier and per-process — no need to
  coordinate token state across an LLM proxy layer.
- The router can be horizontally scaled (multiple router replicas
  in a queue group on `heddle.tasks.incoming`) without
  coordination, because every replica looks at the same static
  rules.

**Costs:**

- The sender must know the right `worker_type`. For programmatic
  callers this is trivial; for free-form CLI users we rely on the
  orchestrator's decomposition step to do the translation.
- Adding a new tier or worker type requires editing
  `router_rules.yaml`. The hot-reload mechanism
  (`heddle.control.reload` subject) keeps that cheap, but the
  edit is explicit.
- If the orchestrator's decomposition step produces a
  `worker_type` that's not in `router_rules.yaml`, the task is
  dead-lettered with `reason="unknown_tier: ..."` — see
  [Interpret dead letters](../runbooks/interpret-dead-letters.md).
