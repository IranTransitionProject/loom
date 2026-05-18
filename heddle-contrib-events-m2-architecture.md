# Heddle `contrib.events` — M2 Architecture

**Status:** Architecture for review — not yet a Claude Code spec.
**Date:** 2026-05-15.
**Depends on:** Heddle PR #30 (`KeyValueStore` refactor) — merged.
**Delivers:** Event-sourcing patterns on top of Heddle's stateless-worker mesh, exercised end-to-end via an `OperatorSession` aggregate that backs ShopPulse clock-in/out.

---

## 1. What ships in M2

A new first-party contrib package, `heddle.contrib.events`, that lets Heddle applications model long-lived state as event-sourced aggregates without violating Heddle's framework invariants. The package ships:

- A new wire envelope (`EventEnvelope`) added to `schemas/v1/`.
- An `EventLog` abstraction with two implementations: `InMemoryEventLog` (zero-config) and `JetStreamEventLog` (production).
- An `Aggregate` base class that owns state replay and event recording.
- A `CommandHandler` base class that subclasses `TaskWorker` — commands ride on the existing `TaskMessage` envelope, with a deliberately-marked seam to split them out later.
- A `Projector` base class, also a `TaskWorker`, that subscribes to events and updates external state.
- One concrete aggregate (`OperatorSession`) wired end-to-end as the demonstration and the first real consumer.
- Snapshot storage via the freshly-generalized `KeyValueStore` (new `snapshot` domain registered).
- CLI commands for inspection (`heddle events list`, `heddle events replay`).
- A lightweight read-only Workshop "Events" tab.

ShopPulse M2's clock-in/out path is the first application: an operator scans their badge, ShopPulse emits a `BadgeIn` command, the `OperatorSessionHandler` validates it, an `OperatorBadgedIn` event lands in JetStream, the current `OperatorSession` snapshot updates in Valkey.

What ships in M2 does *not* include the ProfitFab write-back projector. That belongs to M3 alongside the `Job` aggregate, where PF natural keys and JOBTIME/EMPTIME write coordination need their own design pass.

---

## 2. Why event sourcing for ShopPulse M2

Two reasons that compound:

**Audit trail without trying.** Every state change on the shop floor — badge in, badge out, clock in, clock out, scrap event, completion — is naturally an event. A job's history reconstructs itself from the event log without anyone designing an audit schema. The 97% of historical ProfitFab `SCRAP` records with `FAULT='U'` is exactly the failure mode event sourcing prevents: there's no separate "log this for later" step that can be skipped under pressure.

**Replay as the debugging primitive.** The shop floor has weird race conditions (two operators clocking the same step, a nest closing while a constituent job is still active) that are nearly impossible to reproduce from a snapshot-only system. With an event log, you replay the events that led to the bad state and watch them happen.

The cost — operationally complex projection management, eventually-consistent reads, the steep learning curve of "current state is a derived view" — is real but bounded by Heddle's philosophy commitments. Solo/SMB first means we don't need Kafka or a 12-microservice projection mesh. JetStream on a single NATS host plus Valkey plus a Python projector process is the whole production substrate.

---

## 3. Architecture

### 3.1 Envelopes

**Events** get their own first-class envelope. Commands ride on `TaskMessage` for now, with a marked seam.

#### `EventEnvelope` (new in `schemas/v1/`)

```python
# In heddle/core/messages.py:

class EventEnvelope(BaseModel):
    """An event recorded against an aggregate.

    Aggregates produce events; the event log persists them; projectors
    react to them. The envelope is part of the heddle wire contract and
    is consumed by foreign-language SDKs.
    """

    event_id: str                                  # UUIDv7, sortable
    aggregate_type: str                            # e.g. "OperatorSession"
    aggregate_id: str                              # UUIDv7 or PF natural key
    aggregate_version: int = Field(..., ge=1)      # monotonic per aggregate
    event_type: str                                # e.g. "OperatorBadgedIn"
    event_version: int = 1                         # schema version of this event_type
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime                          # when the domain fact happened
    recorded_at: datetime                          # when the log accepted the event
```

Conventions:

- `event_id` is UUIDv7. Sortable. Used as JetStream message ID for dedup.
- `aggregate_version` is monotonic per `(aggregate_type, aggregate_id)` starting at 1. Used for optimistic concurrency on append.
- `event_type` is a bare name, NOT namespaced with the aggregate type. The pair `(aggregate_type, event_type)` is the global event identity. This keeps event types short in projector code while still being globally unambiguous.
- `event_version` is the schema version of *this event type's payload shape*. Starts at 1. Bumped when a payload field is added/renamed/removed; old events stay readable by versioned upcasters in the aggregate's `apply()`.
- `metadata` is free-form but conventions include: `command_id` (the TaskMessage.task_id that produced this event), `correlation_id`, `actor` (badge_id or system identifier).
- `occurred_at` vs `recorded_at` — when the event "really" happened in the domain vs. when the log accepted it. Equal except in late-arriving or backdated scenarios (e.g., a badge scan queued offline on a tablet).

The new schema lands in `schemas/v1/event_envelope.schema.json` via Heddle's existing schema-export tool, with the same CI drift gate as the other four envelopes.

#### Commands: `TaskMessage` today, with a clear seam

Commands are `TaskMessage` instances. The `worker_type` follows a convention:

```
worker_type = "commandhandler.{aggregate_type_lower}.{command_name_lower}"

# Examples:
"commandhandler.operatorsession.badgein"
"commandhandler.operatorsession.badgeout"
"commandhandler.job.clockin"
```

The `payload` carries the command fields. Routing uses Heddle's existing deterministic router rules — `router_rules.yaml` gets entries for each handler.

This means a command is just a task. No new wire schema, no new subject hierarchy, no new SDK surface. The cost: command-specific concerns (expected aggregate version, idempotency keys, command-vs-task metadata) ride in `payload` and `metadata` rather than being first-class envelope fields.

We accept that cost for M2 to keep the wire surface small. The seam is marked explicitly in `heddle/contrib/events/handler.py`:

```python
class CommandHandler(TaskWorker, Generic[AggregateT]):
    """Base class for event-sourcing command handlers.

    A CommandHandler is a TaskWorker whose payload is interpreted as a
    command. It loads the targeted aggregate (snapshot + event replay),
    dispatches to a per-command method, and persists any emitted events.

    Envelope choice — TaskMessage today, CommandMessage later
    ---------------------------------------------------------

    TODO(events-2.0): commands currently ride on the TaskMessage envelope.
    This is deliberate for M2: it lets event-sourcing aggregates participate
    in the existing bus and router without a parallel wire contract, and
    lets foreign-SDK consumers send commands using the same models they
    already use for tasks.

    If event-sourcing semantics outgrow TaskMessage — i.e., we accumulate
    enough command-specific fields in `payload`/`metadata` that they
    deserve to be first-class — introduce a `CommandMessage` envelope in
    schemas/v1/, add a `heddle.commands.*` subject hierarchy, and switch
    CommandHandler's superclass to a new TypedCommandWorker base. The
    domain logic (load aggregate, dispatch, append events) does not need
    to change; only the envelope and the subject conventions.

    Concrete trigger conditions to revisit this decision:
    1. Need for a native `expected_aggregate_version` field on every command
       (cleaner than nesting it in `metadata`).
    2. Need for command-specific deduplication keys distinct from
       TaskMessage's task_id.
    3. Foreign-SDK consumers asking for typed command stubs that don't
       inherit TaskMessage's task-routing fields.
    4. The router_rules.yaml entry list for `commandhandler.*` workers
       grows past ~20 — bus subject organization becomes worth splitting.
    """
```

That TODO is the most important sentence in the package: it makes the architectural debt visible, names the conditions for repaying it, and tells the future maintainer (human or LLM) what the split looks like before they have to design it from scratch.

### 3.2 Event log

The `EventLog` ABC is small. Three operations: append, read-by-aggregate, subscribe.

```python
# heddle/contrib/events/log.py

class EventLog(ABC):
    @abstractmethod
    async def append(
        self,
        events: list[EventEnvelope],
        *,
        expected_version: int | None = None,
    ) -> None:
        """Append events atomically. If `expected_version` is given,
        the aggregate's current head version must match exactly, else
        ConcurrencyError is raised."""

    @abstractmethod
    def read_aggregate(
        self,
        aggregate_type: str,
        aggregate_id: str,
        *,
        from_version: int = 1,
    ) -> AsyncIterator[EventEnvelope]:
        """Yield events for a single aggregate in version order."""

    @abstractmethod
    def subscribe(
        self,
        aggregate_type: str | None = None,
        *,
        from_event_id: str | None = None,
    ) -> AsyncIterator[EventEnvelope]:
        """Subscribe to the live stream. If aggregate_type is None,
        all events. If from_event_id is given, replay from that point
        before going live."""
```

#### `InMemoryEventLog`

List-backed. Used in tests and for `heddle events workshop` zero-config runs. Append is atomic via an `asyncio.Lock`; subscriptions are `asyncio.Queue`-fed fanouts. Stays Pythonic, no networking.

#### `JetStreamEventLog`

JetStream is the production substrate. NATS already runs in Heddle deployments; turning on JetStream is a config flag plus a storage volume.

**Stream layout — one stream per aggregate type.**

```
HEDDLE_EVENTS_OPERATORSESSION    subjects: heddle.events.OperatorSession.*
HEDDLE_EVENTS_JOB                subjects: heddle.events.Job.*           (M3)
HEDDLE_EVENTS_NEST               subjects: heddle.events.Nest.*          (M3)
```

Why per-aggregate-type streams rather than one big `HEDDLE_EVENTS` stream with subject filtering:

- Independent retention policies per aggregate type. `OperatorSession` may rotate at 1y; `Job` is kept forever.
- Independent replica/storage choices per aggregate type if we ever scale out.
- Per-stream consumer offsets — projector A for `OperatorSession` doesn't see Job events at all, so its consumer cursor is naturally scoped.
- Simpler to reason about: each stream's growth is bounded by one domain.

The trade-off is more streams to manage. With ~10 aggregate types over the platform lifetime, that's fine.

**Subject conventions per stream:**

```
heddle.events.{aggregate_type}.{aggregate_id}.{event_type}
```

Subject components are stable: `aggregate_type` and `event_type` are CamelCase; `aggregate_id` is the natural key or UUIDv7. The `aggregate_id` segment lets consumers subscribe to a single aggregate's events cheaply (`heddle.events.OperatorSession.{specific-id}.>`).

**Stream config defaults:**

```yaml
storage: file
retention: limits
max_age: 0                    # 0 = unbounded; event sourcing never deletes
max_msgs: 0                   # 0 = unbounded
replicas: 1                   # single-host today; bumps when we cluster NATS
duplicate_window: 2m          # event_id-based dedup window
discard: new                  # if limits ever set, refuse new writes rather than drop old
```

`expected_version` on append maps to JetStream's `Nats-Expected-Last-Subject-Sequence` header. Atomicity for multi-event appends from a single command uses a JetStream batch publish.

**`event_id` is the JetStream message ID.** This makes dedup native — a command handler that retries an append doesn't double-write because UUIDv7 is content-stable for the same event.

### 3.3 Snapshots

Use the `KeyValueStore` we just generalized, with a new domain:

```python
# heddle/contrib/events/__init__.py (executed at package import):
from heddle.core.kvstore import register_domain
register_domain("snapshot", "heddle:snapshot:")
```

Key convention: `heddle:snapshot:{aggregate_type}:{aggregate_id}`. The aggregate's serialized state plus its `aggregate_version` go in the value; replay starts from `aggregate_version + 1`.

Snapshot policy is per-aggregate-class, configurable:

```python
class OperatorSession(Aggregate):
    aggregate_type = "OperatorSession"
    snapshot_every_n_events = 50      # default in base class is 100
```

The framework takes a snapshot inside the command-handler commit path whenever `aggregate.version % snapshot_every_n_events == 0` after applying new events. No background snapshotter. No timer. The handler's commit is the only place state changes, so it's the only place that needs to consider snapshotting.

Snapshots have no TTL by default — they're durable. The Valkey instance is configured with persistence (RDB or AOF), not as a cache.

### 3.4 Aggregate base class

```python
# heddle/contrib/events/aggregate.py

class Aggregate(ABC):
    """Base class for event-sourced aggregates.

    Subclasses define their state as instance attributes, override
    `apply()` to mutate state from each event type, and call `record()`
    from command-handling methods to stage events for commit.
    """

    aggregate_type: ClassVar[str]
    snapshot_every_n_events: ClassVar[int] = 100

    def __init__(self, aggregate_id: str) -> None:
        self.aggregate_id = aggregate_id
        self.version = 0
        self._pending: list[EventEnvelope] = []

    @abstractmethod
    def apply(self, event: EventEnvelope) -> None:
        """Apply a recorded event to current state. MUST be deterministic
        and side-effect-free. Called both during live recording and
        during replay."""

    def record(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        event_version: int = 1,
        metadata: dict[str, Any] | None = None,
    ) -> EventEnvelope:
        """Stage a new event. Calls apply() immediately so the aggregate
        reflects the new state; the event is also queued for commit."""
        next_version = self.version + len(self._pending) + 1
        now = datetime.now(timezone.utc)
        event = EventEnvelope(
            event_id=str(uuid7()),
            aggregate_type=self.aggregate_type,
            aggregate_id=self.aggregate_id,
            aggregate_version=next_version,
            event_type=event_type,
            event_version=event_version,
            payload=payload,
            metadata=metadata or {},
            occurred_at=now,
            recorded_at=now,
        )
        self.apply(event)
        self._pending.append(event)
        return event

    def snapshot(self) -> dict[str, Any]:
        """Serialize current state for snapshot storage. Override to
        exclude pending events or transient fields."""
        return {
            "aggregate_id": self.aggregate_id,
            "version": self.version,
            "state": self._serialize_state(),
        }

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> "Aggregate":
        """Reconstitute from a snapshot dict. Counterpart to snapshot()."""
        ...

    @abstractmethod
    def _serialize_state(self) -> dict[str, Any]: ...

    @classmethod
    @abstractmethod
    def _deserialize_state(cls, state: dict[str, Any]) -> "Aggregate": ...
```

`apply()` is the heart of the contract. It must be **deterministic** (same event → same state mutation always) and **side-effect-free** (no I/O, no logging, no clock reads). This is what makes replay sound.

`record()` is the only thing command-handling methods call. It runs `apply()` immediately so subsequent command-handler code sees the new state, then queues the event for commit by the handler.

`snapshot()` and `from_snapshot()` round-trip through JSON. The default implementations cover trivial dataclass-style aggregates; complex ones override.

### 3.5 CommandHandler base class

```python
# heddle/contrib/events/handler.py

class CommandHandler(TaskWorker, Generic[AggregateT]):
    """Base class for event-sourcing command handlers.

    [Big TODO(events-2.0) docstring here — see Section 3.1.]
    """

    aggregate_class: ClassVar[type[Aggregate]]

    def __init__(
        self,
        *args: Any,
        event_log: EventLog,
        snapshot_store: KeyValueStore,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._event_log = event_log
        self._snapshot_store = scoped(snapshot_store, "snapshot")

    async def process(self, message: TaskMessage) -> TaskResult:
        """TaskWorker entry point. Dispatches based on command name."""
        command_name = self._extract_command_name(message.worker_type)
        aggregate_id = message.payload["aggregate_id"]

        # Load
        aggregate = await self._load_aggregate(aggregate_id)

        # Dispatch — convention: handle_{command_name_lower}(aggregate, payload, message)
        handler = getattr(self, f"handle_{command_name.lower()}")
        await handler(aggregate, message.payload, message)

        # Commit
        if aggregate._pending:
            expected = aggregate.version - len(aggregate._pending)
            await self._event_log.append(
                aggregate._pending,
                expected_version=expected,
            )
            await self._maybe_snapshot(aggregate)
            aggregate._pending.clear()

        return TaskResult(
            task_id=message.task_id,
            status=TaskStatus.OK,
            payload={"aggregate_version": aggregate.version},
        )

    async def _load_aggregate(self, aggregate_id: str) -> AggregateT:
        """Load aggregate: snapshot first, then replay events from there."""
        snapshot_key = f"{self.aggregate_class.aggregate_type}:{aggregate_id}"
        raw = await self._snapshot_store.get(snapshot_key)

        if raw is None:
            agg = self.aggregate_class(aggregate_id)
        else:
            data = json.loads(raw)
            agg = self.aggregate_class.from_snapshot(data)

        # Replay any events past the snapshot's version
        async for event in self._event_log.read_aggregate(
            self.aggregate_class.aggregate_type,
            aggregate_id,
            from_version=agg.version + 1,
        ):
            agg.apply(event)
            agg.version = event.aggregate_version

        return agg

    async def _maybe_snapshot(self, aggregate: AggregateT) -> None:
        if aggregate.version % aggregate.snapshot_every_n_events == 0:
            key = f"{aggregate.aggregate_type}:{aggregate.aggregate_id}"
            await self._snapshot_store.set(
                key, json.dumps(aggregate.snapshot())
            )

    @staticmethod
    def _extract_command_name(worker_type: str) -> str:
        # "commandhandler.operatorsession.badgein" -> "BadgeIn"
        ...
```

The base class is intentionally simple. Optimistic concurrency is the only retry case — if `append()` raises `ConcurrencyError`, the handler should re-load and re-dispatch. For M2 the policy is "let NATS redeliver via queue group"; a future refinement can add bounded in-process retry.

`CommandHandler` does NOT carry state between tasks. Heddle's stateless-worker invariant (Invariant 1) is preserved: each task loads the aggregate fresh from snapshot + replay, processes, commits, discards. The "stateful" part of event sourcing is in the event log and snapshot store, not in the handler.

### 3.6 Projector base class

```python
# heddle/contrib/events/projector.py

class Projector(TaskWorker, ABC):
    """Subscribes to events and updates external state.

    Projectors run as TaskWorkers in a NATS queue group. The events
    arrive as TaskMessages whose payload is an EventEnvelope dict.
    The framework (an EventDispatcher utility, see below) is responsible
    for the JetStream → TaskMessage adapter.
    """

    aggregate_types: ClassVar[list[str]]  # which aggregates this projects

    @abstractmethod
    async def apply_event(self, event: EventEnvelope) -> None:
        """Apply a single event. MUST be idempotent — projectors may
        receive the same event more than once on restart or NATS
        redelivery."""

    async def process(self, message: TaskMessage) -> TaskResult:
        event = EventEnvelope.model_validate(message.payload)
        await self.apply_event(event)
        return TaskResult(task_id=message.task_id, status=TaskStatus.OK)
```

**Idempotency is a hard requirement on projectors.** JetStream redelivers on consumer ack failure; projector restarts replay from the last ack'd event. The framework can't enforce idempotency; the projector author guarantees it. Typical patterns: upsert-by-event_id, conditional update gated on a version field, or stateless re-derivation.

An `EventDispatcher` utility bridges JetStream consumers to `heddle.tasks.*` subjects so projectors look like regular workers from the router's perspective:

```python
# heddle/contrib/events/dispatcher.py
class EventDispatcher:
    """Consumes JetStream events and re-publishes as TaskMessages for
    projector workers, so projectors fit Heddle's existing
    worker→router→queue-group lifecycle."""
```

This is what keeps Heddle's framework invariants intact: projectors are stateless workers, the bus stays pub/sub at-most-once, and JetStream sits *above* the existing bus rather than replacing it.

### 3.7 NATS subjects summary

| Subject | Purpose | New / existing |
|---|---|---|
| `heddle.tasks.commandhandler.{aggregate_type}.{command}.{tier}` | Command dispatch via existing router | existing pattern |
| `heddle.tasks.projector.{projector_name}.{tier}` | Projector dispatch via existing router | existing pattern |
| `heddle.events.{aggregate_type}.{aggregate_id}.{event_type}` | JetStream event subjects | **new — JetStream** |
| `heddle.results.{parent_task_id}` | Command result / projector ack | existing |

No new subjects in heddle proper; all new subjects live under JetStream streams. Heddle's bus contract is undisturbed.

---

## 4. First aggregate: `OperatorSession`

The motivating use case is shop-floor badge in/out. Today ProfitFab models this in `EMPTIME` rows with `Type='C'` (clock-in). ShopPulse M2 owns the live state; the eventual PF write-back projector (M3) syncs it to `EMPTIME` for ProfitFab's reports.

### State

```python
@dataclass
class OperatorSessionState:
    session_id: str                # UUIDv7, also the aggregate_id
    badge_id: str                  # e.g. "206" for BECKER, JAMES
    station_num: int | None        # current station, None when off-station
    badged_in_at: datetime
    badged_out_at: datetime | None # None while session is active
    active_dronums: list[int]      # operator's active ops preferences
```

### Commands

| Command | Payload | Preconditions | Emits |
|---|---|---|---|
| `BadgeIn` | `{badge_id, station_num}` | Aggregate is new OR last session is closed | `OperatorBadgedIn` |
| `BadgeOut` | `{}` | Aggregate is active (no `badged_out_at`) | `OperatorBadgedOut` |
| `SetActiveOps` | `{active_dronums: list[int]}` | Aggregate is active | `OperatorActiveOpsSet` |

### Events

| Event type | Payload |
|---|---|
| `OperatorBadgedIn` | `{session_id, badge_id, station_num, badged_in_at}` |
| `OperatorBadgedOut` | `{session_id, badged_out_at}` |
| `OperatorActiveOpsSet` | `{session_id, active_dronums}` |

### Lifecycle

A session starts on `BadgeIn` and ends on `BadgeOut`. Multiple sessions for the same badge are separate aggregates — each `BadgeIn` creates a new `session_id`. The `aggregate_id` IS the `session_id`. This means the M1 in-memory "operator active ops preferences" layer becomes a property of the current session rather than a global keyed by badge.

A read model (projector, M3) will maintain `latest_active_session_by_badge_id` in Valkey so the API can answer "what session is operator 206 currently in?" without scanning the event log.

### Why this aggregate first

- No PF natural key collision — sessions are system-generated UUIDs.
- No write-back required for M2 — the PF write-back projector is M3 work.
- Exercises all framework layers: command handling, event recording, snapshot, replay.
- Maps directly to ShopPulse M1's existing in-memory preferences code — refactor target is concrete and small.
- Operationally low-risk: a buggy `OperatorSession` doesn't corrupt ProfitFab data.

---

## 5. Sprint sequence

M2 splits into five sprints, each landing as its own Claude Code spec. Estimated 6–8 weeks total, with stop-and-ask gates between sprints.

### Sprint 1 — Wire contract: `EventEnvelope`

Add `EventEnvelope` Pydantic model to `heddle.core.messages`. Export to `schemas/v1/event_envelope.schema.json` via the existing schema-export tool. Vendor downstream into `heddle-sdk`. Add tests. CHANGELOG entry. No new functionality, no behavioral changes — just the type and its schema.

**Files:** 5 modified, 1 new, 1 CHANGELOG. ~250 LOC including tests.
**Verification:** existing schemas keep their drift-gate green; new schema is generated and matches the model; heddle-sdk sync passes.
**Done criterion:** `EventEnvelope` is importable from `heddle.core.messages` and `schemas/v1/event_envelope.schema.json` is checked in.

### Sprint 2 — `heddle.contrib.events` foundation

Create the package. Land `EventLog` ABC, `InMemoryEventLog`, `Aggregate` base class, `CommandHandler` base class (with the seam TODO), `Projector` base class, `EventDispatcher` stub (in-memory implementation). Register the `snapshot` domain. Tests for everything against `InMemoryEventLog` + `InMemoryKeyValueStore`. CHANGELOG entry.

**Files:** ~8 new files. ~800 LOC including tests.
**Verification:** new tests green, full suite green, coverage held, pyright strict clean.
**Done criterion:** an in-test `Counter` aggregate (defined in the test file, not shipped) can be incremented via a command, persist events, replay from scratch, take a snapshot, replay from snapshot.

### Sprint 3 — JetStream event log

Land `JetStreamEventLog`. Add JetStream config helpers (`create_stream_for_aggregate_type`). Update docker-compose to enable JetStream with a persistent volume. Add integration tests behind a NATS marker (skipped if no NATS available). Update Heddle's CLI to surface JetStream stream status (`heddle events streams`). CHANGELOG entry.

**Files:** ~5 new, ~3 modified. ~500 LOC.
**Verification:** integration tests pass with a local NATS+JetStream container; unit tests still pass without NATS; existing CI doesn't need JetStream.
**Done criterion:** `JetStreamEventLog` round-trips a 1000-event batch with optimistic-concurrency-conflict detection.

### Sprint 4 — `OperatorSession` aggregate

Land the concrete `OperatorSession` aggregate, its three commands, three events, and one command handler. Tests for each command and each invariant (badge-in-while-active, badge-out-while-inactive, snapshot at version 50, replay from scratch, replay from snapshot, version conflict on concurrent commands). Wire into ShopPulse's existing M1 codebase at a clearly-marked seam — but don't replace the existing in-memory preferences yet. CHANGELOG entry.

**Files:** ~4 new in `heddle.contrib.events.aggregates.operator_session`, ~2 new in shoppulse repo. ~500 LOC.
**Verification:** unit tests green; manual end-to-end run: send a BadgeIn command via `heddle events apply`, observe event in JetStream, observe snapshot in Valkey.
**Done criterion:** a complete `OperatorSession` lifecycle runs end-to-end against the real NATS+Valkey infrastructure.

### Sprint 5 — CLI and Workshop browser

CLI commands: `heddle events list` (list known aggregate types and their stream stats), `heddle events show {aggregate_type} {aggregate_id}` (dump events for an aggregate), `heddle events replay {aggregate_type} {aggregate_id}` (replay and print final state — no side effects). Workshop "Events" tab: read-only browser listing aggregates of each type, click-through to event timeline. No replay-from-UI, no command-emission-from-UI.

**Files:** ~5 new in CLI, ~5 new in Workshop. ~700 LOC.
**Verification:** CLI tests, Workshop snapshot tests, manual demo.
**Done criterion:** Hooman can demo "browse to `OperatorSession`, pick a session, see the timeline" from Workshop.

---

## 6. Open questions to resolve before Sprint 1

Five things worth pinning before any code is written:

**Q1: `EventEnvelope.metadata` typing.** `dict[str, Any]` is what we'd write today, but Heddle's invariants prefer typed contracts. Three options:

- (a) `dict[str, Any]` — matches TaskMessage's metadata, max flexibility, weakest contract.
- (b) Typed `EventMetadata` model with optional fields (`command_id`, `correlation_id`, `actor`, etc.) plus a free-form `extra: dict[str, Any]` escape hatch.
- (c) `dict[str, str]` — primitives only, no nested structures, forces serialization decisions upfront.

Default: (b) — earns its complexity, matches how typed-worker payloads work elsewhere.

**Q2: UUIDv7 dependency.** Python stdlib UUIDv7 lands in 3.14 (PEP 727); Heddle requires 3.11+. Either bump the floor to 3.14 (large change), or pull `uuid7` from a small library (e.g., `uuid6` or `uuid-utils`).

Default: pull `uuid_utils` as a contrib-events-only dependency. Floor stays at 3.11 for Heddle core. UUIDv7 is internal to event-id generation; downstream consumers see the resulting string.

**Q3: `event_id` collision policy in `JetStreamEventLog`.** UUIDv7 collisions are vanishingly rare but theoretically possible. JetStream's `Nats-Msg-Id` dedup window is 2 minutes by default. If two events with the same `event_id` are submitted within 2 min, JetStream silently dedups — the second one is "successful" from the publisher's perspective but no message is stored.

- (a) Accept the silent dedup — UUIDv7 collisions are noise, dedup is a feature.
- (b) Set duplicate window to 0 (no dedup) and treat re-publishing the same event_id as a bug.
- (c) Detect dedups via `Pub.Ack.Duplicate` and surface as a soft warning.

Default: (a) for M2; revisit if it bites.

**Q4: Snapshot serialization format.** JSON is the obvious choice (round-trips through Valkey, human-readable, debuggable). Alternative is msgpack (smaller). For OperatorSession-shaped state (few hundred bytes), JSON is fine.

Default: JSON. Revisit if snapshots get large.

**Q5: Event `payload` schema enforcement.** Today, Heddle's worker I/O schemas live on the worker config YAML. Event payloads don't have an equivalent. Three options:

- (a) Mirror the worker pattern — each aggregate class declares per-event payload schemas, validated on `record()` and on `apply()`.
- (b) Validate via Pydantic models defined as nested classes on the Aggregate subclass.
- (c) No validation. Trust the developer; rely on the typed `apply()` method receiving the dict and crashing on missing keys.

Default: (b) — Pydantic models for event payloads. Pairs naturally with `event_version` upcasters (each version has its own model). Validation is automatic via Pydantic's existing infrastructure.

---

## 7. Out of scope for M2 (explicit deferrals)

- **ProfitFab write-back.** No projector that touches JOBTIME/EMPTIME yet. M3.
- **The `Job` aggregate.** Has PF natural keys and write-back coupling; needs its own design pass. M3.
- **The `Nest` aggregate.** Multi-aggregate command pattern (a single nest clock-in affects N jobs). Complex enough to wait. M3.
- **Event versioning / upcasters.** `event_version` field is defined and reserved, but no upcaster infrastructure ships in M2 because no v2 events exist yet. First upcaster lands when the first event-type schema breaks.
- **Replay-from-UI in Workshop.** Read-only browser only. Replay is CLI-only in M2.
- **Multi-NATS clustering.** Single-host JetStream is what we ship. Clustering is an operational concern when Naimor outgrows one Hyper-V host (i.e., never, for the foreseeable future).
- **Projection rebuild tooling.** A "rewind projector to event_id X and replay forward" tool is conceptually clean but earns its complexity when we have a projector worth rebuilding. M3 or later.
- **Backpressure between event log and projectors.** JetStream's pull-based consumers handle this for free at our scale. Revisit if a projector falls catastrophically behind.

---

## 8. Resolved earlier decisions

These are listed in one place for the spec authors who follow:

| Decision | Choice | Where decided |
|---|---|---|
| Packaging | `heddle.contrib.events` (not sibling repo) | This session, post-PHILOSOPHY review |
| Event envelope | New `EventEnvelope` in `schemas/v1/` | This session |
| Command envelope | `TaskMessage` today, seam to `CommandMessage` later | This session |
| Storage abstraction | Reuse `KeyValueStore` via scoped view | This session, post-kvstore refactor |
| Snapshot domain prefix | `heddle:snapshot:` | This session |
| First aggregate | `OperatorSession` | This session |
| Workshop scope for M2 | Read-only browser (Q4=c, "both partial") | This session |
| Stream layout | One stream per aggregate type | This document |
| Subject convention | `heddle.events.{aggregate_type}.{aggregate_id}.{event_type}` | This document |
| Optimistic concurrency | Per-aggregate `aggregate_version`, JetStream `Nats-Expected-Last-Subject-Sequence` | This document |
| Snapshot policy | Take every N events at commit time; no background snapshotter | This document |
