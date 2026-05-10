# ADR-004: Skip-and-log on malformed messages, not crash

**Status:** Accepted.
**Pairs with:** [Invariant 8](../DESIGN_INVARIANTS.md#8-malformed-nats-messages-are-skipped-not-crashed).

## Context

Heddle has two layers at which a message can be malformed:

1. **Bus deserialization** — the NATS payload isn't valid JSON,
   or the UTF-8 decoding fails. `NATSBus._handle_message` catches
   `json.JSONDecodeError` and `UnicodeDecodeError`, logs, and
   keeps the subscription alive.
2. **Pydantic parsing** — the dict is valid JSON but doesn't fit
   the `TaskMessage` / `TaskResult` schema. The orchestrator's
   `dispatch_and_wait_for_result` and `ResultStream` both route
   the parse attempt through `parse_task_result`
   (`heddle.core.messages`), which logs and returns `None` on
   failure.

The decision to make: when a malformed message arrives, should the
consumer crash and force a restart, or skip the message and
continue?

## Decision

**Malformed messages are skip-and-logged at every layer. The
subscription stays live, the consumer task keeps running, and the
outer timeout is the only way a malformed message causes
caller-visible failure.**

Specifically:

- `NATSBus` catches deserialization errors and logs
  `nats.malformed_message`. Subscription loop continues.
- `parse_task_result` catches Pydantic `ValidationError`, logs
  via the caller-supplied `log_event` (one of
  `dispatch.parse_error` / `result_stream.parse_error`), and
  returns `None`. Caller-supplied skip-and-continue logic.

## Alternatives considered

### Crash and let the supervisor restart (rejected)

Treat a malformed message as fatal. The consumer crashes, the
process manager (systemd, Kubernetes) restarts the actor, and
processing resumes from the new subscription.

- **Rejected because** the bad message stays in NATS (NATS is
  at-most-once; once delivered it's gone, but a corrupted
  payload from a buggy publisher may keep arriving). The worker
  crashes, restarts, subscribes, receives the same bad message,
  crashes again — a crash loop that masks valid messages behind
  it.
- Restart latency is much longer than message latency. A worker
  processing a high-volume stream spends 10s rebooting per bad
  message, during which valid messages pile up and time out at
  the caller.
- A single buggy publisher upstream can take down the entire
  fleet of consumers via this mechanism. The blast radius is
  out of proportion to the local fault.

### Dead-letter malformed messages (partially adopted)

The router already dead-letters tasks that fail `TaskMessage`
parsing (`reason="invalid_task_message: ..."` —
[Invariant 8 covers the bus layer; the router covers the
TaskMessage layer specifically](../runbooks/interpret-dead-letters.md)).

- **Adopted** for the routing layer — the router is the natural
  triage point and the dead-letter queue gives operators
  visibility into upstream sender bugs.
- **Not adopted** for the result-collection layer — a malformed
  *result* shouldn't be moved to a dead-letter queue, because
  the goal is still in progress and the caller is still
  waiting. The skip-and-log path lets the next (valid) matching
  result complete the wait. b453298 fixed the bug where the
  result-collection layer was treating ValidationError as
  fatal; the current shape is the alternative we chose.

### Pause subscription, drain, then resume (rejected)

When a bad message arrives, stop the subscription, drain
in-flight handlers, restart the subscription pointer past the bad
message, and continue.

- **Rejected because** NATS doesn't expose a per-message
  acknowledgement pointer for non-JetStream subjects (Heddle
  runs on core NATS by default for low latency). The drain-
  and-resume primitive doesn't exist at the protocol level.
- Even if it did, the complexity isn't justified. The state
  machine for "currently draining due to bad message at offset
  N" has more failure modes than the simple skip-and-log path.

## Consequences

**Enables:**

- Single buggy producer cannot take down a consumer fleet.
- High-volume streams remain processable through transient
  corruption events.
- Operator triage is straightforward: grep `*.parse_error` to
  find the bad messages, fix the upstream cause, no consumer
  restart needed.
- The two parse-error log keys (`dispatch.parse_error`,
  `result_stream.parse_error`) are parallel so an operator can
  grep both modules with one query — the centralization in
  `parse_task_result` makes them inseparable from the helper
  itself.

**Costs:**

- A consistently malformed message produces a steady stream of
  log warnings. Operators must monitor the warning rate
  themselves; the framework doesn't escalate to fatal.
- Silent bug class: a publisher emits valid JSON that almost
  matches the schema (missing one required field). The consumer
  skips every one of its messages. The publisher author sees no
  feedback at the publish call. Mitigated by the dead-letter
  queue at the router layer for `TaskMessage`s, but result-
  layer parse errors only surface via log greps.
- The bad message stays in NATS for as long as NATS retains it.
  Operators expecting "drain it from the queue" semantics need
  to know that's not how core NATS works.
