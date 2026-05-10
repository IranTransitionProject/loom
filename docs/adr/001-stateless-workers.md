# ADR-001: Stateless workers, no process-local cache

**Status:** Accepted.
**Pairs with:** [Invariant 1](../DESIGN_INVARIANTS.md#1-worker-statelessness-is-enforced-not-optional).

## Context

Workers in Heddle are deployed as competing consumers via NATS
queue groups. The router publishes to `heddle.tasks.{worker_type}.{tier}`,
and any number of worker replicas subscribed to that subject can
pick up a task — NATS hands the message to exactly one of them per
queue group.

This means two consecutive tasks of the same `worker_type` can land
on different replicas. There is no client-side affinity, no
session, and no way for a worker to know which task came before its
current one without external coordination.

The decision to make: should workers carry state between tasks?

## Decision

**Workers are stateless. `reset()` runs unconditionally after every
task — even if the task raised an exception. There is no mechanism
to carry state between tasks within a worker process.**

The base `TaskWorker` invokes `reset()` in a `finally` block
(commit `d28192b`), so a task that fails mid-processing cannot leave
behind partial state for the next task.

## Alternatives considered

### Process-local cache (rejected)

Workers could maintain an in-memory cache (LRU, TTL-bounded) keyed
by some salient input — e.g. a chat history keyed by user_id, a
parsed-document cache keyed by file hash.

- **Rejected because** the cache only helps when the next request
  for the same key lands on the same replica. Under queue-group
  delivery, that's random. Effective hit rate scales as 1/N for N
  replicas, while the cache memory cost scales as N (each replica
  holds its own).
- The failure mode is silent: a single-replica test passes, the
  production multi-replica deployment quietly returns stale or
  inconsistent results when the cache state diverges across
  replicas.

### Sticky routing by worker_id (rejected)

Have the router pin specific request keys to specific worker
replicas, defeating queue-group randomness.

- **Rejected because** it requires the router to know which
  replicas exist (it doesn't — NATS abstracts the topology) and
  collapses horizontal scaling. A sticky-routed worker pool with
  N replicas behaves like N independent single-replica workers.
- Recovering from a replica failure becomes a directory-update
  problem rather than a "just spin up another replica" problem.

### Shared external cache (acceptable, but not via worker state)

Workers can read from and write to a shared cache (Valkey, DuckDB,
a knowledge silo). This is fine — it's just not worker state. The
cache is external infrastructure, identical to every other
replica's view, and is what `OrchestratorActor`'s checkpoint manager
uses.

The invariant forbids **instance variables that persist across
tasks**, not external persistence in general.

## Consequences

**Enables:**

- Trivial horizontal scaling — add replicas, NATS load-balances,
  done.
- Replica failures are recoverable without state migration.
- Testing is straightforward: a single-replica unit test
  faithfully represents production behaviour.
- Worker hot-reload (config change → restart) loses no in-flight
  state, because there is no in-flight state.

**Costs:**

- Workers cannot maintain conversation history without an external
  store. Heddle solves this at the orchestrator layer (via
  `CheckpointManager`), not the worker layer.
- Repeated identical inputs trigger repeated identical work. For
  expensive operations (embedding generation, large doc parsing),
  the caller layers a content-addressed cache on top — see
  `heddle.contrib.rag` for the pattern.
- The `reset()` discipline is mechanical and easy to forget.
  Mitigated by enforcing it in the base class's `finally` block.
