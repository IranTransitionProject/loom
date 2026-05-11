# Foreign-Language Actors — The Wire Protocol

**Audience:** authors of Heddle actors written in languages other than
Python (.NET, Swift, Go, Rust, etc.).

Heddle's framework is Python, but the **wire protocol** between actors
is language-agnostic: NATS subjects + JSON messages. This document
specifies the contract a non-Python actor must implement to participate
in a Heddle bus.

## The envelope

Every message on the bus is a JSON object that carries two distinct
things:

- **Envelope fields** — routing, observability, lifecycle metadata.
  Stable across worker types. The router and orchestrator care about
  these.
- **Payload** — domain-specific data, shaped by the worker's
  `input_schema` / `output_schema`. Opaque to the framework.

`TaskMessage` is the request envelope. `TaskResult` is the response
envelope. Both are Pydantic models in `heddle.core.messages` (see
`src/heddle/core/messages.py` in the repository); the canonical JSON
Schemas are exported to `schemas/v1/` on every commit and gated by a
CI drift check — they are the authoritative wire contract.

### TaskMessage (request)

```text
heddle.tasks.incoming
       ▲
       │ (router subscribes; dispatches based on worker_type + tier)
       │
heddle.tasks.{worker_type}.{tier}
       ▲
       │ (workers subscribe; queue-group named after worker_type)
       │
       └── TaskMessage  ─── JSON object ─────────────────────┐
                                                              │
   task_id              str (UUID)                            │
   parent_task_id       str | null  (goal_id when from orch)  │
   worker_type          str                                   │ ENVELOPE
   model_tier           "local" | "standard" | "frontier"     │
   priority             "low" | "normal" | "high" | "critical"│
   max_retries          int  (≥ 0)                            │
   retry_count          int  (≥ 0)                            │
   created_at           str  (ISO 8601 UTC)                   │
   request_id           str | null                            │
   metadata             object                                │
   ─────────────────────────────────────────────────────────  ┘
   payload              object  ← validated against           ╮
                                  worker.input_schema         │ PAYLOAD
                                                              ╯
```

### TaskResult (response)

```text
heddle.results.{parent_task_id or "default"}
       ▲
       │ (orchestrator subscribed before publishing TaskMessage)
       │
       └── TaskResult  ──── JSON object ─────────────────────┐
                                                              │
   task_id              str  (matches TaskMessage.task_id)    │
   parent_task_id       str | null  (matches TaskMessage)     │
   worker_type          str                                   │ ENVELOPE
   status               "completed" | "failed" | "pending"    │
                        | "processing" | "retry"              │
   error                str | null                            │
   model_used           str | null                            │
   token_usage          object  ({"prompt_tokens": int, ...}) │
   metadata             object                                │
   processing_time_ms   int                                   │
   completed_at         str  (ISO 8601 UTC)                   │
   ─────────────────────────────────────────────────────────  ┘
   output               object | null  ← validated against    ╮
                                         worker.output_schema │ PAYLOAD
                                                              ╯
```

## Generating typed wrappers in your language

The Pydantic-exported JSON Schemas in `schemas/v1/` plug straight into
off-the-shelf code generators. No custom IDL or designer tool — the
wire contract IS the schema.

**Swift** (idiomatic `Codable` types):

```bash
quicktype \
    --src schemas/v1/task_message.schema.json \
    --src-lang schema \
    --lang swift \
    --top-level TaskMessage \
    -o Sources/HeddleActor/TaskMessage.swift
```

**.NET / C#** (with `System.Text.Json` attributes):

```bash
quicktype \
    --src schemas/v1/task_message.schema.json \
    --src-lang schema \
    --lang csharp \
    --framework SystemTextJson \
    --top-level TaskMessage \
    -o src/Heddle.Actor/TaskMessage.cs
```

**Other languages** — `quicktype` supports TypeScript, Kotlin, Go,
Rust, Java, Ruby, Python (dataclasses), and more. Use the same
invocation, varying `--lang`. For Python projects that want their own
typed view, `datamodel-code-generator` produces Pydantic models from
the same JSON Schemas.

Wire-format compatibility is guaranteed across language SDKs: every
SDK generates types from the same `schemas/v1/*.schema.json` files,
which are themselves generated from the canonical Pydantic models on
every commit (CI fails on drift).

## NATS subjects

| Subject | Direction | Notes |
| --- | --- | --- |
| `heddle.tasks.incoming` | client → router | Where orchestrators / clients publish `TaskMessage`s for routing. Foreign actors rarely subscribe here directly. |
| `heddle.tasks.{worker_type}.{tier}` | router → worker | Workers subscribe with **queue group name = `worker_type`** so multiple replicas of the same worker load-balance via NATS queue-group semantics. |
| `heddle.tasks.dead_letter` | router → operator | Unroutable or rate-limited messages land here. Foreign actors don't publish here; the router does. |
| `heddle.results.{parent_task_id}` | worker → orchestrator | Each goal opens a unique result subject; the worker echoes back to it. The orchestrator subscribes BEFORE publishing the task (Invariant 17). |
| `heddle.results.default` | worker → standalone caller | Used when a `TaskMessage` has no `parent_task_id`. |
| `heddle.control.reload` | operator → all actors | Optional config hot-reload broadcast. Foreign actors MAY subscribe to re-read their config. |

## Required behaviours

A foreign actor MUST observe these to interoperate cleanly with the
Python framework:

1. **Queue-group subscription.** Subscribe to
   `heddle.tasks.{worker_type}.{tier}` with `queue_group=worker_type`.
   Without the queue group, every replica receives every task.
2. **Reset between tasks** (Invariant 1). No state may carry from one
   task to the next inside one actor process. The Python framework
   enforces this via a `reset()` call in a `finally` block; foreign
   actors must achieve the same effect (clear per-task locals before
   the next `recv()` call).
3. **Skip-and-log on malformed input** (Invariant 8). If the bytes
   don't parse as JSON, or don't conform to `TaskMessage`, log a
   warning and continue the subscription loop. Do not crash the
   process; do not retry the bad message — the publisher needs to
   fix its output.
4. **Subscribe-before-publish for the result subject** is *the
   orchestrator's* responsibility, not the worker's. Workers just
   publish to `heddle.results.{parent_task_id}` when done.
5. **JSON Schema validation** is intentionally shallow (Invariant 5):
   required-field presence + top-level type checks. Foreign SDKs
   SHOULD implement the same shallow validation against
   `input_schema` / `output_schema` from the worker's config.
   Implementing full JSON Schema validation is allowed but produces
   stricter behaviour than the Python actors; document the divergence
   if you do.
6. **OTel trace propagation.** If `TaskMessage.metadata.traceparent`
   is present, propagate it through any downstream calls and into
   `TaskResult.metadata.traceparent` on response. The format is W3C
   Trace Context. If your language ecosystem lacks OTel maturity,
   passing the value through verbatim is acceptable.

## Scope — what foreign actors are (and aren't)

Foreign actors are positioned as **`processor` workers**: they run
compute, ML inference, transformations, native-library calls, etc.
The framework supports them as first-class participants on the bus.

What they **are not** (today, by design):

- **LLM workers.** Heddle's LLM backends (Anthropic, OpenAI, Ollama,
  LM Studio) are Python-native and offer features (retry policy,
  reasoning-content rescue, thinking-block surface) that haven't
  been replicated in other languages. A foreign actor that needs LLM
  inference should call a sibling Python LLM worker via the bus.
- **Knowledge-silo consumers.** `docproc`, `rag`, `lancedb`, etc. are
  Python-side abstractions over file formats and embedding stores.
  Foreign actors that need these capabilities call sibling Python
  workers; they don't reimplement silo access.
- **Tool providers.** Same shape as silos.

This scope keeps the foreign-actor wire surface small and well-defined.
If you find yourself wanting to reimplement an LLM backend or a silo
adapter in .NET / Swift, the cleaner shape is a Python proxy worker
that exposes the capability over the bus.

## Configuration

A foreign actor's worker config YAML lives alongside Python worker
configs in `configs/workers/<name>.yaml`. The fields the router and
orchestrator care about are unchanged:

```yaml
name: image_classifier              # worker_type
worker_kind: processor              # foreign actors are always 'processor'
default_model_tier: local
input_schema:
  type: object
  required: [image_url]
  properties:
    image_url: {type: string}
output_schema:
  type: object
  required: [label, confidence]
  properties:
    label: {type: string}
    confidence: {type: number}
implementation:                     # OPTIONAL — interpreted by Workshop
  runtime: dotnet                   #   tooling so it can offer the
  entry: ./bin/Heddle.ImageClassifier  #   right lifecycle affordances
                                       #   (test-run, eval-runs, etc.)
                                       #   without trying to import a
                                       #   Python module.
```

The `implementation` block is informational — the framework doesn't
launch foreign workers itself. Run them under systemd, Docker,
launchd, or whatever your deploy target supports; they connect to
NATS the same way Python workers do.

## Reference SDKs

When SDKs land for specific languages, they will be linked here.
Until then, the wire spec above plus `schemas/v1/` is sufficient to
build one — the protocol surface is intentionally small.

## See also

- `schemas/v1/` (in the repository) — authoritative JSON Schemas, CI-checked.
- [Gateway Actors](gateway-actors.md) — for non-NATS protocols (MQTT, HTTP, IoT).
- [Design Invariants](DESIGN_INVARIANTS.md) — framework-safety contracts (1, 5, 8, 17 in particular apply to foreign actors).
- [ADR-001](adr/001-stateless-workers.md) — why the reset-between-tasks invariant exists.
