"""
Heddle message schemas — the canonical wire format.

All inter-actor communication is typed through these Pydantic models.
Actors ONLY communicate through these message types; raw dicts or
ad-hoc JSON are forbidden. This enforces a contract-driven architecture
where every message is validatable at compile time.

Message flow:
    Client/CLI  ──OrchestratorGoal──>  Orchestrator
    Orchestrator  ──TaskMessage──>  Router  ──TaskMessage──>  Worker
    Worker  ──TaskResult──>  Orchestrator

This module also hosts the event-sourcing wire contract used by
``heddle.contrib.events`` — ``EventEnvelope`` / ``EventMetadata`` and
``CommandMessage`` / ``CommandMetadata``. Those envelopes are distinct
from the router-dispatched worker envelopes above: they target
aggregates by natural identity and CAS rather than worker classes.

See Also:
    heddle.core.contracts — JSON Schema validation for payload/output dicts
    heddle.core.subjects — NATS subject helpers for the events wire contract
    heddle.bus.nats_adapter — NATS subject conventions for message routing
    heddle.contrib.events.issuer_conventions — reserved ``issued_by`` prefixes
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger()


class TaskPriority(StrEnum):
    """Priority levels for task scheduling (not yet enforced by router)."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class TaskStatus(StrEnum):
    """Lifecycle states for a task.

    State transitions::

        PENDING -> PROCESSING -> COMPLETED
                               -> FAILED

    Note on retries: Heddle does not implement worker-side retry by
    design (see ADR-012).  Stage-level retry lives in
    ``PipelineOrchestrator`` via the per-stage ``max_retries`` YAML
    field, and transient bus-level redelivery is handled by NATS
    queue-group semantics when an actor disconnects mid-task — both
    are external to the task's own lifecycle.  ``COMPLETED`` and
    ``FAILED`` are the only terminal states.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ModelTier(StrEnum):
    """Which model tier should handle this task.

    Tiers map to backend instances configured at startup:
        LOCAL    -> OllamaBackend (e.g., llama3.2:3b)
        STANDARD -> AnthropicBackend (e.g., Claude Sonnet)
        FRONTIER -> AnthropicBackend (e.g., Claude Opus)

    The router may override the tier via tier_overrides in router_rules.yaml.
    """

    LOCAL = "local"  # Small local model (Ollama, llama.cpp)
    STANDARD = "standard"  # Mid-tier API model
    FRONTIER = "frontier"  # Top-tier model (Claude Opus, GPT-4, etc.)


class TaskMessage(BaseModel):
    """Message sent TO a worker actor.

    The payload dict must conform to the worker's input_schema (JSON Schema).
    Contract validation happens in TaskWorker.handle_message(), not here.

    Retry semantics live OUTSIDE the message envelope by design (see
    ADR-012): stage-level ``max_retries`` is in the pipeline YAML, and
    bus-level redelivery on actor disconnect is handled by NATS queue
    groups.  No retry fields appear on TaskMessage.
    """

    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    parent_task_id: str | None = None  # Links subtask to orchestrator's goal
    worker_type: str  # Which worker config to use (e.g., "summarizer", "doc_extractor")
    payload: dict[str, Any]  # Structured input — must match worker's input_schema
    model_tier: ModelTier = ModelTier.STANDARD
    priority: TaskPriority = TaskPriority.NORMAL
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    request_id: str | None = None  # Correlates all tasks from the same goal (set by pipeline)
    metadata: dict[str, Any] = Field(default_factory=dict)  # Routing hints, pipeline context


class TaskResult(BaseModel):
    """Message sent FROM a worker actor after processing.

    Published to: heddle.results.{parent_task_id or 'default'}
    The output dict must conform to the worker's output_schema (JSON Schema).
    """

    task_id: str
    parent_task_id: str | None = None
    worker_type: str
    status: TaskStatus
    output: dict[str, Any] | None = None  # Structured output — must match worker's output_schema
    error: str | None = None  # Human-readable error message on failure
    model_used: str | None = None  # Actual model that processed this (e.g., "llama3.2:3b")
    token_usage: dict[str, int] = Field(
        default_factory=dict
    )  # {"prompt_tokens": N, "completion_tokens": N}
    # Worker-side observability that is NOT part of the worker's output
    # schema.  Currently carries ``degraded_modes: [{kind, name, reason}]``
    # for optional knowledge silos / sources / tool providers that were
    # skipped at load time (F1).  Empty by default; orchestrators can
    # check it to detect "ran without resource X" without scraping logs.
    metadata: dict[str, Any] = Field(default_factory=dict)
    processing_time_ms: int = 0
    completed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def parse_task_result(
    data: dict[str, Any],
    *,
    log_event: str,
    task_id: str | None = None,
    **extra_log_fields: Any,
) -> TaskResult | None:
    """Parse a :class:`TaskResult` payload, logging + returning None on error.

    Two call sites parse :class:`TaskResult` off raw bus data:

    - :func:`heddle.orchestrator.dispatch.dispatch_and_wait_for_result`
      (single-result wait), logging ``dispatch.parse_error``;
    - :class:`heddle.orchestrator.stream.ResultStream` (multi-result
      collection), logging ``result_stream.parse_error``.

    Both must treat a Pydantic ``ValidationError`` on one matching
    payload as a skip rather than a fatal — the consumer keeps reading
    until the next valid result or the outer timeout.  Drift between
    the two was the regression fixed in b453298.

    ``log_event`` keeps the two existing module-specific log keys
    intact so operator queries / alerting that grep for either key
    continue to work.  ``task_id`` is logged when provided so the
    skip is correlatable with the dispatched task.  ``extra_log_fields``
    forwards caller-specific context (e.g.
    :class:`~heddle.orchestrator.stream.ResultStream` includes the
    subject and expected count it had bound on its logger).
    """
    try:
        return TaskResult(**data)
    except Exception as e:
        logger.warning(
            log_event,
            task_id=task_id,
            error=str(e),
            **extra_log_fields,
        )
        return None


class OrchestratorGoal(BaseModel):
    """Top-level goal submitted to an orchestrator.

    Published to: heddle.goals.incoming
    The orchestrator (PipelineOrchestrator or OrchestratorActor) picks this up,
    decomposes it into TaskMessages, and synthesizes results.

    The context dict carries domain-specific data (e.g., file_ref for doc processing).
    """

    goal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    instruction: str  # Natural language goal
    context: dict[str, Any] = Field(
        default_factory=dict
    )  # Domain data (file_ref, categories, etc.)
    request_id: str | None = None  # Optional correlation ID for tracing goal→task chains
    priority: TaskPriority = TaskPriority.NORMAL
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CheckpointState(BaseModel):
    """Compressed orchestrator state for self-summarization.

    When the orchestrator's conversation history exceeds a token threshold,
    CheckpointManager compresses it into this structure and persists to Valkey.
    The orchestrator can then "reboot" with a fresh context containing only
    the checkpoint + a small recent-interactions window.

    See: heddle.orchestrator.checkpoint.CheckpointManager
    """

    goal_id: str
    original_instruction: str
    executive_summary: str  # High-level status (always short)
    completed_tasks: list[dict[str, Any]]  # Key outcomes only, not full results
    pending_tasks: list[dict[str, Any]]  # What remains
    open_issues: list[str]  # Conflicts, blockers, uncertainties
    decisions_made: list[str]  # Important choices and rationale
    context_token_count: int  # Tokens at time of checkpoint
    checkpoint_number: int
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Event-sourcing wire contract (Sprint 1, heddle.contrib.events)
# ---------------------------------------------------------------------------
#
# These envelopes are the canonical wire format for the heddle.contrib.events
# layer (see heddle-contrib-events-m2-architecture-v7.md §4.1).
#
# Distinct from TaskMessage/TaskResult:
#   - TaskMessage/TaskResult carry router-dispatched WORKER tasks and results.
#   - EventEnvelope/CommandMessage carry AGGREGATE state-change events and
#     the commands that produce them.
#
# Issuer convention: every event and command carries `metadata.issued_by`
# with a reserved prefix. See heddle.contrib.events.issuer_conventions.


def _uuid7() -> str:
    # Lazy import so bare `pip install heddle` (without the [events] extra)
    # still imports this module. Only callers that actually construct an
    # envelope without supplying an id pay the dependency cost.
    import uuid_utils

    return str(uuid_utils.uuid7())


class EventMetadata(BaseModel):
    """Provenance and correlation for an event.

    `issued_by` is a semi-structured identifier with one of six reserved
    prefixes — see ``heddle.contrib.events.issuer_conventions``. Framework
    enforces the prefix at ``apply()`` for sensitive event types (e.g.
    ``InternalFinalized`` requires ``framework:*``).
    """

    command_id: str | None = None
    correlation_id: str | None = None
    issued_by: str = Field(
        ...,
        description=(
            "Semi-structured issuer identifier. Reserved prefixes: framework:, "
            "observer:{name}, projector:{name}, user:badge:{id}, "
            "user:system:{component}, bridge:{worker_type}. See "
            "heddle.contrib.events.issuer_conventions."
        ),
    )
    extra: dict[str, Any] = Field(default_factory=dict)


class EventEnvelope(BaseModel):
    """Canonical event envelope for heddle.contrib.events.

    Persisted in JetStream ``HEDDLE_EVENTS_{TYPE}`` streams. Replayed by
    aggregates to reconstruct state. See architecture v7 §4.1.

    Ordering authority:
      - ``aggregate_version``: authoritative for in-aggregate ordering;
        CAS field on ``EventLog.append()``.
      - ``recorded_at``: authoritative for cross-aggregate log ordering.
      - ``occurred_at``: for domain queries only — never for ordering
        computation.
    """

    event_id: str = Field(
        default_factory=_uuid7,
        description="UUIDv7. Used as JetStream Nats-Msg-Id for opportunistic dedup.",
    )
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int = Field(..., ge=1)
    event_type: str = Field(
        ...,
        description=(
            "CamelCase event type, e.g. 'JobClockedIn'. "
            "(aggregate_type, event_type) is global identity."
        ),
    )
    event_version: int = 1
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: EventMetadata
    occurred_at: datetime
    recorded_at: datetime


class CommandMetadata(BaseModel):
    """Provenance and correlation for a command.

    Same ``issued_by`` reserved-prefix conventions as :class:`EventMetadata`.
    The ``issued_by_legacy`` slot is reserved for future use and is NOT part
    of the Sprint 1 surface — included in the schema so adding it later
    doesn't break wire-compat.
    """

    correlation_id: str | None = None
    issued_by: str = Field(
        ...,
        description="Same reserved prefix conventions as EventMetadata.issued_by.",
    )
    issued_by_legacy: str | None = None  # reserved; not used in M2
    extra: dict[str, Any] = Field(default_factory=dict)


class CommandMessage(BaseModel):
    """Canonical command envelope for heddle.contrib.events.

    Published to JetStream ``HEDDLE_COMMANDS_{TYPE}`` streams. Consumed by
    ``CommandHandler`` (Sprint 2/3). Distinct from :class:`TaskMessage`:
    ``CommandMessage`` targets an aggregate by natural identity and CAS;
    ``TaskMessage`` targets a worker class via router rules.

    ``expected_aggregate_version`` is the optimistic concurrency token.
    ``None`` means "no version check" (typical for create-from-PF observers
    that have no prior state).
    """

    command_id: str = Field(
        default_factory=_uuid7,
        description="UUIDv7. Used as JetStream Nats-Msg-Id.",
    )
    aggregate_type: str
    aggregate_id: str
    command_type: str = Field(
        ...,
        description="CamelCase command type, e.g. 'JobClockIn'.",
    )
    command_version: int = 1
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: CommandMetadata
    issued_at: datetime
    expected_aggregate_version: int | None = None
