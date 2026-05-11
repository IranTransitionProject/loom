"""
Deterministic task router. NOT an LLM -- pure logic.

Reads router_rules.yaml and routes tasks to appropriate NATS subjects
based on worker_type and model_tier.

The router is the single entry point for all task dispatch:
    Producers (orchestrators, CLI) publish to:  heddle.tasks.incoming
    Router resolves the tier and re-publishes to: heddle.tasks.{worker_type}.{tier}

This indirection means producers never need to know which tier or backend
handles a given worker_type -- that's all controlled via router_rules.yaml.

Routing pipeline (in order):
    1. Deserialize incoming dict into a TaskMessage (Pydantic validation).
    2. Resolve the model tier:
       a. If router_rules.yaml has a tier_override for this worker_type, use it.
       b. Otherwise, use the tier from the TaskMessage itself.
    3. Check the per-tier token-bucket rate limiter:
       a. If the tier has capacity, allow the task through.
       b. If the tier is exhausted, publish to the dead-letter subject.
    4. Validate the resolved tier is a known ModelTier enum value.
    5. Publish to heddle.tasks.{worker_type}.{tier}.

Dead-letter handling:
    Tasks that cannot be routed are published to heddle.tasks.dead_letter with
    an attached reason. This covers:
    - Malformed messages that fail Pydantic validation
    - Unknown tier values after override resolution
    - Rate-limited tasks that exceed the tier's max_concurrent budget

Rate limiting:
    Per-tier token-bucket rate limiter based on max_concurrent from
    router_rules.yaml. Each tier gets a bucket with capacity equal to
    max_concurrent. A token is consumed when a task is dispatched and
    refilled when the refill interval elapses (tokens_per_minute /
    max_concurrent gives the refill rate). This is a simple dispatch-side
    throttle -- it does not track actual worker completion.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

import structlog
import yaml

from heddle.core.messages import ModelTier, TaskMessage
from heddle.tracing import extract_trace_context, get_tracer, inject_trace_context

if TYPE_CHECKING:
    from heddle.bus.base import MessageBus

logger = structlog.get_logger()
_tracer = get_tracer("heddle.router")

# NATS subject where unroutable or rate-limited tasks are sent for observability.
DEAD_LETTER_SUBJECT = "heddle.tasks.dead_letter"


class TokenBucketRateLimiter:
    """Per-tier token-bucket rate limiter.

    Each tier (local, standard, frontier) gets its own bucket. The bucket
    capacity equals max_concurrent from router_rules.yaml. Tokens refill
    at a steady rate derived from tokens_per_minute:

        refill_interval = 60.0 / max_concurrent  (seconds between refills)

    This means a tier with max_concurrent=4 refills one token every 15 seconds,
    allowing bursts up to 4 but averaging 4 dispatches per minute.

    Note: This is a dispatch-side throttle only. It does not track whether
    workers have finished processing. For true backpressure, workers would
    need to report completion events that refill tokens.
    """

    def __init__(self, rate_limits: dict[str, dict[str, Any]]) -> None:
        # _buckets maps tier name -> {tokens, capacity, refill_interval, last_refill}
        self._buckets: dict[str, dict[str, Any]] = {}
        # ``try_acquire`` does a read-modify-write on the bucket
        # dict.  Under ``max_concurrent=1`` and cooperative asyncio
        # this is effectively atomic, but the router is also reachable
        # from thread-pool callers (Workshop test_runner) and future
        # multi-loop deployments.  Guard with a threading.Lock so the
        # read-then-decrement sequence is properly serialised.
        self._lock = threading.Lock()

        for tier_name, limits in rate_limits.items():
            max_concurrent = limits.get("max_concurrent", 10)
            # refill_interval: seconds between adding one token back.
            # Derived so that over 60 seconds, max_concurrent tokens are refilled.
            refill_interval = 60.0 / max_concurrent if max_concurrent > 0 else 1.0

            self._buckets[tier_name] = {
                "tokens": float(max_concurrent),
                "capacity": float(max_concurrent),
                "refill_interval": refill_interval,
                "last_refill": time.monotonic(),
            }

        logger.info(
            "rate_limiter.initialized",
            tiers={name: int(b["capacity"]) for name, b in self._buckets.items()},
        )

    def try_acquire(self, tier: str) -> bool:
        """Attempt to consume one token for the given tier.

        Returns True if a token was available (task may proceed).
        Returns False if the bucket is empty (task should be dead-lettered).

        If the tier has no configured rate limit, the task is always allowed.
        """
        bucket = self._buckets.get(tier)
        if bucket is None:
            # No rate limit configured for this tier -- allow unconditionally.
            return True

        with self._lock:
            self._refill(bucket)

            if bucket["tokens"] >= 1.0:
                bucket["tokens"] -= 1.0
                return True
            return False

    def _refill(self, bucket: dict[str, Any]) -> None:
        """Add tokens back based on elapsed time since last refill."""
        now = time.monotonic()
        elapsed = now - bucket["last_refill"]
        # Calculate how many tokens to add based on time elapsed.
        # Each refill_interval seconds, one token is added.
        tokens_to_add = elapsed / bucket["refill_interval"]
        if tokens_to_add > 0:
            bucket["tokens"] = min(
                bucket["capacity"],
                bucket["tokens"] + tokens_to_add,
            )
            bucket["last_refill"] = now


class TaskRouter:
    """Deterministic router that dispatches TaskMessages to worker queues.

    This is NOT an LLM component. It contains zero inference logic. It reads
    routing rules from a YAML config and applies them mechanically.

    .. note::

       **Strategy D -- Worker-side batching.** A batching layer between the
       router and workers could accumulate similar tasks (same worker_type + tier)
       and dispatch them as a single batch to reduce LLM API call overhead.
       This would sit here in the routing pipeline, before the publish step.

    Routing pipeline::

        incoming task --> resolve tier --> check rate limit --> publish to worker queue
                                      |                   |
                                      |                   +--> dead-letter (rate limited)
                                      +--> dead-letter (bad tier / validation failure)

    Subscribes to: ``heddle.tasks.incoming``

    Publishes to:  ``heddle.tasks.{worker_type}.{tier}``  (normal route)
                   ``heddle.tasks.dead_letter``            (unroutable / rate-limited)

    The router runs as a long-lived async process. After subscribing, it
    processes tasks via NATS async callbacks. The caller (cli/main.py) is
    responsible for keeping the event loop alive after run() returns.
    """

    def __init__(self, config_path: str, bus: MessageBus) -> None:
        self.bus = bus
        self.rules = self._load_rules(config_path)

        # Build the rate limiter from config. If no rate_limits key exists,
        # the limiter is created with no buckets (everything passes).
        rate_limits = self.rules.get("rate_limits", {})
        self._rate_limiter = TokenBucketRateLimiter(rate_limits)

        # Cache the set of valid tier values for fast validation.
        self._valid_tiers = {t.value for t in ModelTier}

        logger.info(
            "router.initialized",
            tier_overrides=self.rules.get("tier_overrides", {}),
            rate_limit_tiers=list(rate_limits.keys()),
        )

    def _load_rules(self, path: str) -> dict[str, Any]:
        """Load router_rules.yaml.

        Expected top-level keys:
            tier_overrides: dict mapping worker_type -> tier string
            rate_limits:    dict mapping tier -> {max_concurrent, tokens_per_minute}

        Raises FileNotFoundError if the config file does not exist.
        Raises yaml.YAMLError if the file is malformed.
        """
        with open(path) as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ValueError(f"router_rules.yaml must be a YAML mapping, got {type(data).__name__}")
        return data

    def resolve_tier(self, task: TaskMessage) -> ModelTier:
        """Determine the model tier for a task.

        Resolution priority:
            1. Worker-specific override in router_rules.yaml (tier_overrides section).
               This lets operators force all tasks of a given worker_type to a
               specific tier without changing producer code.
            2. The tier specified in the TaskMessage itself (set by the producer).
            3. TaskMessage defaults to ModelTier.STANDARD if not specified.

        Raises ValueError if the resolved tier string is not a valid ModelTier.
        """
        overrides = self.rules.get("tier_overrides", {})
        if task.worker_type in overrides:
            tier_str = overrides[task.worker_type]
            # Validate that the override is a known tier. This catches typos
            # in router_rules.yaml early rather than silently publishing to
            # a subject no worker will ever subscribe to.
            return ModelTier(tier_str)
        return task.model_tier

    async def _dead_letter(
        self,
        data: dict[str, Any],
        reason: str,
        *,
        task_id: str | None = None,
        worker_type: str | None = None,
    ) -> None:
        """Publish an unroutable task to the dead-letter subject.

        The dead-letter message wraps the original task data with metadata
        explaining why routing failed. Downstream consumers (monitoring,
        alerting, retry systems) can subscribe to heddle.tasks.dead_letter
        to handle these.

        Args:
            data: The original message dict as received from NATS.
            reason: Human-readable explanation of why routing failed.
            task_id: Task ID if available (may be None for malformed messages).
            worker_type: Worker type if available.
        """
        dead_letter_msg = {
            "reason": reason,
            "original_task": data,
            "task_id": task_id,
            "worker_type": worker_type,
        }
        logger.warning(
            "router.dead_letter",
            reason=reason,
            task_id=task_id,
            worker_type=worker_type,
        )
        await self.bus.publish(DEAD_LETTER_SUBJECT, dead_letter_msg)

    async def route(self, data: dict[str, Any]) -> None:
        """Route a single task from heddle.tasks.incoming to the correct worker queue.

        This is the main routing pipeline, called once per incoming message:

            1. Parse the raw dict into a TaskMessage (validates schema).
            2. Resolve the tier via tier_overrides or TaskMessage default.
            3. Check the per-tier rate limiter.
            4. Publish to heddle.tasks.{worker_type}.{tier}.

        Any failure at steps 1-3 sends the task to the dead-letter subject
        instead of silently dropping it.
        """
        ctx = extract_trace_context(data)
        with _tracer.start_as_current_span("router.route", context=ctx) as span:
            # Step 1: Deserialize and validate the incoming message.
            try:
                task = TaskMessage(**data)
            except Exception as exc:
                span.record_exception(exc)
                await self._dead_letter(
                    data,
                    reason=f"invalid_task_message: {exc}",
                    task_id=data.get("task_id"),
                    worker_type=data.get("worker_type"),
                )
                return

            span.set_attribute("task.id", task.task_id)
            span.set_attribute("task.worker_type", task.worker_type)

            # Step 2: Resolve the model tier.
            try:
                tier = self.resolve_tier(task)
            except ValueError as exc:
                span.record_exception(exc)
                await self._dead_letter(
                    data,
                    reason=f"unknown_tier: {exc}",
                    task_id=task.task_id,
                    worker_type=task.worker_type,
                )
                return

            span.set_attribute("task.tier", tier.value)

            # Step 3: Check rate limit for the resolved tier.
            if not self._rate_limiter.try_acquire(tier.value):
                await self._dead_letter(
                    data,
                    reason=f"rate_limited: tier '{tier.value}' has no available capacity",
                    task_id=task.task_id,
                    worker_type=task.worker_type,
                )
                return

            # Step 4: Publish to the resolved worker subject.
            subject = f"heddle.tasks.{task.worker_type}.{tier.value}"
            route_log = logger.bind(task_id=task.task_id, worker_type=task.worker_type)
            route_log.info(
                "router.task_routed",
                tier=tier.value,
                subject=subject,
            )
            outgoing = task.model_dump(mode="json")
            inject_trace_context(outgoing)
            await self.bus.publish(subject, outgoing)

    # Queue group name shared by all router replicas.  NATS delivers
    # each ``heddle.tasks.incoming`` message to exactly one member of
    # the group, so multiple router pods form a competing-consumer
    # pool rather than producing N-fold duplicate dispatch.
    QUEUE_GROUP = "router"

    async def run(self) -> None:
        """Connect to NATS and subscribe to the incoming task subject.

        Subscribes with the ``router`` queue group so that running more
        than one router replica produces a competing-consumer pool —
        each task is delivered to exactly one replica, which then
        re-publishes once to the worker subject.  Without the queue
        group, every replica receives every task and dispatches
        duplicates (and consumes N times the rate-limit budget).

        After this method returns, the router is connected and
        subscribed; the caller must call :meth:`process_messages` to
        enter the consumption loop (see ``cli/main.py``).
        """
        await self.bus.connect()
        self._sub = await self.bus.subscribe(
            "heddle.tasks.incoming",
            queue_group=self.QUEUE_GROUP,
        )
        logger.info(
            "router.running",
            dead_letter_subject=DEAD_LETTER_SUBJECT,
            queue_group=self.QUEUE_GROUP,
        )

    async def process_messages(self) -> None:
        """Process messages from the subscription until cancelled.

        Call run() first to connect and subscribe, then call this to
        enter the message processing loop.
        """
        async for data in self._sub:
            await self.route(data)
