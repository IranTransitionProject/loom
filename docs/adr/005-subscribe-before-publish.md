# ADR-005: Subscribe before publish for request-reply

**Status:** Accepted.
**Pairs with:** [Invariant 17](../DESIGN_INVARIANTS.md#17-subscribe-before-publish-for-orchestrator--worker-request-reply).

## Context

When an orchestrator dispatches a task to a worker and waits for
the matching result, it does two things:

1. Subscribe to `heddle.results.{goal_id}` (or the parent_task_id
   variant).
2. Publish the task to `heddle.tasks.incoming`.

The order matters. NATS core is at-most-once: a message published
when no subscription is live is dropped silently. If the worker is
fast (typical for `local` tier on a hot model), it can publish the
result before the orchestrator's subscription is established —
which means the result is dropped and the caller times out, even
though the worker logs a clean completion.

This was real bug. Multiple sites in the codebase implemented this
pattern, and one of them (`PipelineOrchestrator._dispatch_and_wait`,
since refactored) had subscribe-after-publish ordering, intermittent
failures, and a confusing "worker logs success but caller times out"
signature.

The decision to make: how do we ensure the ordering is correct
everywhere, not just in the places people remembered?

## Decision

**One shared helper, `dispatch_and_wait_for_result`, codifies the
subscribe → publish → wait sequence. Every orchestrator that does
request-reply routes through it. Adding a new orchestrator means
calling the helper, not re-implementing the dance.**

The helper lives at `heddle.orchestrator.dispatch.dispatch_and_wait_for_result`.
The matching multi-result counterpart is `ResultStream` in
`heddle.orchestrator.stream`, which enforces the same ordering via
an `async with` context that subscribes on `__aenter__`.

## Alternatives considered

### Use NATS request/reply with `nc.request()` (rejected)

`nats-py` exposes a built-in request/reply primitive: `nc.request()`
subscribes to an inbox subject, publishes with a `Reply-To` header,
and waits. The plumbing for ordering is internal to the library.

- **Rejected because** the result subject is not a generated
  inbox — it's a stable per-goal subject
  (`heddle.results.{goal_id}`) that multiple subscribers can
  attach to (e.g. Workshop tail, MCP bridge progress reporter,
  the orchestrator itself). `nc.request()` is point-to-point;
  it doesn't fit the fan-in pattern.
- The orchestrator collects multiple results (one per dispatched
  subtask) on the same subject. Request/reply assumes one
  request → one reply.
- Adopting `nc.request()` for the simple case would split the
  codebase into "uses request/reply" and "uses publish/subscribe"
  paths with different concurrency semantics. The cost is
  conceptual surface area; the win is small.

### Document the ordering and rely on reviewer vigilance (rejected)

Just write down "always subscribe before publish" in
DESIGN_INVARIANTS.md and trust contributors to follow it.

- **Rejected because** the original bug already happened under
  exactly this regime. The invariant was documented (informally,
  in comments) and a contributor still wrote subscribe-after-
  publish code. Documentation alone is necessary but not
  sufficient for a load-bearing constraint.
- A test for the ordering is hard to write at the unit level
  (the race is timing-dependent; passing the test once doesn't
  prove the invariant). A shared helper that *can't* be called
  out of order is structurally enforced.

### Per-orchestrator ordering tests (partially adopted)

Each orchestrator has a fast-worker simulation test that fails
loudly if the ordering reverses
(`tests/test_publish_before_subscribe.py`). The simulator is a
`MessageBus` shim that publishes the result the instant
`publish()` is called — so any code that publishes before
subscribing will fail to see the reply.

- **Adopted** as a regression guard. The simulator is cheap and
  catches the bug class deterministically.
- **Not adopted** as the primary defence — the helper is. Tests
  catch regressions at PR time; the helper prevents them from
  being written in the first place.

## Consequences

**Enables:**

- Request-reply ordering is correct by construction. A new
  orchestrator that uses the helper inherits the guarantee.
- The fast-worker simulator (`MalformedThenValidWorkerSimBus`,
  `FastWorkerSimBus` in the test file) make the guarantee
  testable without timing-dependent flake.
- The helper has one well-tested implementation; bugs found here
  benefit every caller. The parse-error resilience in
  `parse_task_result` ([ADR-004](004-skip-not-crash-on-malformed.md))
  was added to the helper once and reaches every orchestrator
  for free.

**Costs:**

- The helper is opinionated. Callers that need a different
  ordering (e.g. publish then subscribe for a deliberate fire-
  and-forget pattern) have to opt out by not using the helper —
  and a code review must catch that they're doing so on purpose.
- The helper's subscribe path adds one NATS round-trip before
  the publish. For ultra-low-latency producers, this is visible
  in profiling. Acceptable cost: the alternative is a race
  condition with caller-visible "looks like the worker is broken"
  symptoms.
- `ResultStream`'s `async with` pattern is mandatory — iterating
  without entering the context raises rather than lazy-
  subscribing. This is deliberate (lazy subscription is the
  original race) but surprises callers who haven't read the
  module docstring.
