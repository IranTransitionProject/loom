# ADR-009: Per-goal state isolation, lockless concurrency

**Status:** Accepted.
**Pairs with:** [Invariant 7](../DESIGN_INVARIANTS.md#7-per-goal-state-isolation-enables-concurrency-without-locks).

## Context

`OrchestratorActor` processes goals: it decomposes an
`OrchestratorGoal` into subtasks, dispatches them to workers,
collects results, and synthesises a final response. With
`max_concurrent_goals > 1` (default 1, configurable), multiple
goals may be in flight at the same time inside one actor process.

Each in-flight goal needs:

- A map of dispatched `task_id`s to `TaskMessage`s.
- A map of collected `task_id`s to `TaskResult`s.
- A monotonic start time for budget enforcement.
- Conversation-history entries for checkpoint decisions.
- A checkpoint-counter for chain versioning.

The decision was where this state lives and how concurrent goals
coordinate access to it.

## Decision

**Each goal owns its own `GoalState` dataclass (defined in
`src/heddle/orchestrator/runner.py:92-134`). There is no global
mutable state, no shared counters, no locks. Concurrent goals are as
independent as separate processes — they only share the
`OrchestratorActor` instance, which holds them in a per-goal dict
keyed by `goal_id`.**

When a goal arrives:

1. The actor creates a fresh `GoalState(goal=...)`.
2. The decomposer, dispatcher, collector, and synthesiser all
   operate on that one `GoalState` for the goal's lifetime.
3. When synthesis completes (or the goal times out), the state is
   discarded.

No method on `GoalState` is called from a different goal's task.
The only shared point is the actor-level dict that holds the
in-flight states, and lookups into that dict use the `goal_id` —
distinct goals have distinct keys.

## Alternatives considered

### Shared mutable state guarded by an `asyncio.Lock` (rejected)

Hold collected results, dispatched tasks, and conversation history
in actor-level attributes. Serialise access via an `asyncio.Lock`
when concurrent goals would otherwise race.

- **Rejected because** the lock would have to be acquired for
  every read and every write: decomposition writes
  `dispatched_tasks`, collection writes `collected_results`,
  budget checks read `start_time`. With `max_concurrent_goals >
  1`, the lock serialises every interaction inside the actor —
  the concurrency knob becomes nominal.
- Locks add deadlock risk in any code path that calls into
  itself (the synthesiser dispatches a follow-up subtask, the
  dispatcher logs a checkpoint that reads `conversation_history`,
  etc.). The lockless design has no such failure mode.

### Single-threaded execution, no concurrent goals (rejected)

Set `max_concurrent_goals = 1` permanently. Remove the knob.

- **Rejected because** the bottleneck for goal throughput is
  almost always I/O (worker LLM calls), not CPU. A serialised
  actor wastes the latency budget on a single slow worker call
  while other goals could be in their dispatch or collection
  phases.
- The framework's competing-consumers story (NATS queue groups
  load-balance tasks across workers) already extends naturally to
  competing goals — the actor can dispatch tasks for goal A while
  collecting results for goal B without any cross-goal
  coordination.

### One actor per goal (rejected)

Spawn a new `OrchestratorActor` instance per goal; let each die
when its goal completes.

- **Rejected because** spawning an actor means a new NATS
  subscription to `heddle.results.{goal_id}` and the wire round
  trip that comes with it. Per-goal spawn-and-die adds tens to
  hundreds of milliseconds of latency to every goal, which
  matters for short goals.
- A persistent actor amortises subscription cost across many
  goals. The per-goal cost is just dict insertion.

### Goal state stored in NATS (rejected)

Persist `GoalState` as a NATS KV bucket entry. Read and write the
bucket from any actor that picks up the goal.

- **Rejected because** the goal state is hot-path data — every
  result that arrives updates `collected_results`. Round-tripping
  through NATS KV adds latency proportional to result volume.
- Distributed state would enable goal migration between actor
  replicas, but `OrchestratorActor` is the singleton or
  short-list-of-replicas role; goals don't need to migrate
  during their lifetime. The complexity isn't justified.

## Consequences

**Enables:**

- Concurrent goals run with zero synchronisation overhead.
  Throughput scales linearly with `max_concurrent_goals` until
  the actor's event loop saturates (typically far past 10x).
- Reasoning about goal correctness is local: read
  `GoalState`'s methods, ignore everything else. Inter-goal
  interactions are physically impossible inside the
  state-management code.
- Per-goal failure isolation: a goal that exceptions out
  affects only its own `GoalState`; the other in-flight goals
  continue.

**Costs:**

- Goal state is per-process. If the actor crashes,
  all in-flight `GoalState`s are lost; the checkpoint manager's
  durable state (`heddle.orchestrator.checkpoint`) is the only
  recoverable layer.
- The `OrchestratorActor`-level dict that holds in-flight states
  is itself mutable, but only modified under
  `asyncio` single-threaded semantics (one event loop, no
  threads). Adding a worker thread or a sync callback would
  re-introduce the locking problem.
- A future change that "innocently" adds a shared counter or
  metric accumulator across goals (e.g. a total-tasks-dispatched
  counter for telemetry) re-creates the race. The invariant text
  flags this — the ADR exists to make the rejection durable.
