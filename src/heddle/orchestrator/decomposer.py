"""
Task decomposition logic for orchestrators.

Responsible for breaking down complex goals into concrete subtasks
that can be routed to individual workers.

This module is used by OrchestratorActor (runner.py), NOT by
PipelineOrchestrator (which has its stages pre-defined in YAML).

The GoalDecomposer uses an LLM backend to analyze a high-level goal and
produce a list of concrete TaskMessages, each targeting a specific worker_type.
The LLM is given the goal instruction, domain context, and a manifest of
available workers (names, descriptions, and input schemas) so it can make
informed routing decisions and construct valid payloads.

The decomposition prompt asks the LLM to output structured JSON::

    [
        {"worker_type": "extractor", "payload": {...}, "model_tier": "local"},
        {"worker_type": "summarizer", "payload": {...}, "model_tier": "local"},
        ...
    ]

Each entry maps directly to a TaskMessage. The parent_task_id is set to the
caller-provided value so that worker results route back to the orchestrator
via heddle.results.{goal_id}.

See Also:
    heddle.core.messages.TaskMessage — the output message type
    heddle.core.messages.OrchestratorGoal — the input message type
    heddle.worker.backends.LLMBackend — the LLM interface used for decomposition
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from heddle.core.messages import ModelTier, TaskMessage, TaskPriority

if TYPE_CHECKING:
    from heddle.worker.backends import LLMBackend

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------

# Regex to strip markdown code fences that LLMs commonly wrap JSON in.
# Matches ```json ... ``` or ``` ... ``` with optional whitespace.
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?\s*```$", re.DOTALL)


def _extract_json_array(raw: str) -> list[dict[str, Any]]:  # noqa: PLR0912
    """Extract a JSON array from an LLM response, handling common quirks.

    LLMs frequently wrap valid JSON in markdown code fences
    or add explanatory text before/after the JSON body. This function
    handles those cases in order of preference:

    1. Direct parse (ideal -- model returned clean JSON)
    2. Strip markdown fences and parse
    3. Extract the first ``[...]`` block via regex (fallback for preamble/postamble)

    If the LLM returns a single JSON object instead of an array, it is
    wrapped in a list for convenience -- this handles the case where the
    goal decomposes into exactly one sub-task and the LLM omits the array.

    Raises:
        ValueError: If no valid JSON array or object can be extracted.
    """
    stripped = raw.strip()

    # 1. Try direct parse
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]
    except json.JSONDecodeError:
        pass

    # 2. Try stripping markdown fences
    fence_match = _FENCE_RE.match(stripped)
    if fence_match:
        try:
            parsed = json.loads(fence_match.group(1).strip())
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                return [parsed]
        except json.JSONDecodeError:
            pass

    # 3. Fallback: extract the first JSON array from anywhere in the response
    arr_match = re.search(r"\[.*\]", stripped, re.DOTALL)
    if arr_match:
        try:
            parsed = json.loads(arr_match.group(0))
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    # 4. Last resort: extract a single JSON object
    obj_match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if obj_match:
        try:
            parsed = json.loads(obj_match.group(0))
            if isinstance(parsed, dict):
                return [parsed]
        except json.JSONDecodeError:
            pass

    raise ValueError(f"LLM returned non-JSON for decomposition: {raw[:300]}")


# ---------------------------------------------------------------------------
# Worker type descriptor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkerDescriptor:
    """Metadata about an available worker type.

    Used to ground the LLM's decomposition in what the system can actually
    execute.  Typically constructed from a worker's YAML config file via the
    :meth:`GoalDecomposer.from_worker_configs` factory method.

    Attributes:
        name: The worker_type identifier (e.g. ``"summarizer"``, ``"extractor"``).
            Must match the ``name`` field in the worker's YAML config.
        description: One-line human-readable description of what the worker does.
        input_schema: JSON Schema dict for the worker's expected payload.
            Included in the LLM prompt so it can construct valid payloads.
        default_tier: The default ModelTier string for this worker
            (``"local"``, ``"standard"``, or ``"frontier"``).
    """

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    default_tier: str = "standard"

    def to_prompt_block(self) -> str:
        """Format this worker as a multi-line block for the LLM system prompt.

        Includes the name, description, expected payload schema, and default
        model tier so the LLM knows exactly how to construct valid sub-tasks.
        """
        lines = [
            f"  Worker: {self.name}",
            f"    Description: {self.description}",
            f"    Default tier: {self.default_tier}",
        ]
        if self.input_schema:
            schema_str = json.dumps(self.input_schema, indent=2)
            indented = _indent(schema_str, 6)
            lines.append(f"    Input payload schema:\n{indented}")
        return "\n".join(lines)


def _indent(text: str, spaces: int) -> str:
    """Indent every line of *text* by *spaces* spaces."""
    prefix = " " * spaces
    return "\n".join(prefix + line for line in text.splitlines())


# ---------------------------------------------------------------------------
# System prompt template
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_TEMPLATE = """\
You are a task decomposition engine in the Heddle orchestration framework.

Your job: given a high-level goal, break it into a list of concrete sub-tasks
that can each be executed independently by a stateless worker.

CRITICAL RULES:
- Workers are STATELESS. Each sub-task must be completely self-contained.
  Include all necessary text, data, and parameters in the payload.
  A worker cannot reference results from another worker's task.
- Only use the workers listed below. Do not invent worker types.
- Each sub-task's payload MUST conform to that worker's input schema.
- Keep the number of sub-tasks minimal. Do not create unnecessary tasks.
- If the goal cannot be meaningfully decomposed with the available workers,
  return an empty array: []

AVAILABLE WORKERS:
{worker_blocks}

MODEL TIERS:
- "local": Fast, cheap. Good for straightforward tasks (summarize, classify).
- "standard": More capable. Use for tasks requiring nuance or complex reasoning.
- "frontier": Top-tier. Use only when deep analysis or synthesis is needed.

OUTPUT FORMAT:
Respond with ONLY a JSON array. No preamble, no explanation, no markdown fences.
Each element is an object with these fields:

[
  {{
    "worker_type": "<worker name from the list above>",
    "payload": {{ <valid JSON matching that worker's input schema> }},
    "model_tier": "<local|standard|frontier>",
    "priority": "<low|normal|high|critical>",
    "rationale": "<one sentence explaining why this sub-task is needed>"
  }}
]

FIELD NOTES:
- "model_tier" is optional. Omit it to use the worker's default tier.
  Prefer "local" unless the task genuinely requires more capability.
- "priority" is optional. Defaults to "normal".
- "rationale" is for logging/debugging only and will not be sent to the worker.
"""


def _build_system_prompt(workers: list[WorkerDescriptor]) -> str:
    """Construct the full system prompt with descriptions of all available workers."""
    if workers:
        worker_blocks = "\n\n".join(w.to_prompt_block() for w in workers)
    else:
        worker_blocks = "  (none configured)"
    return _SYSTEM_PROMPT_TEMPLATE.format(worker_blocks=worker_blocks)


def _build_user_message(goal: str, context: dict[str, Any] | None) -> str:
    """Construct the user message from the goal string and optional context.

    The context dict carries domain-specific data (e.g. file references,
    category lists, full text content) that the LLM needs to construct
    appropriate payloads for each sub-task.
    """
    parts = [f"GOAL:\n{goal}"]
    if context:
        # Use default=str to handle datetime and other non-serializable types
        parts.append(f"\nCONTEXT:\n{json.dumps(context, indent=2, default=str)}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# GoalDecomposer
# ---------------------------------------------------------------------------


class GoalDecomposer:
    """LLM-based goal decomposition.

    Turns a high-level goal string into a list of TaskMessage objects ready
    for dispatch through the router.

    The decomposer asks an LLM to plan which workers to invoke and how to
    parameterize each one. It then parses the structured JSON response into
    validated TaskMessage objects.

    All parsing and validation failures are handled gracefully -- invalid
    sub-tasks are logged and skipped rather than crashing the orchestrator.
    If the entire LLM response is unparseable, an empty list is returned.

    Args:
        backend: An LLM backend instance (OllamaBackend, AnthropicBackend, etc.)
            used to generate the decomposition plan.
        workers: List of WorkerDescriptor objects describing the available
            worker types. These are injected into the system prompt so the LLM
            knows what tools it can plan around.
        max_tokens: Maximum tokens for the LLM response. Should be large enough
            to accommodate the full JSON plan. Defaults to 2000.
        temperature: Sampling temperature. Low values (0.0--0.2) produce more
            deterministic plans. Defaults to 0.0 for reproducibility.

    Example::

        workers = [
            WorkerDescriptor(
                name="summarizer",
                description="Compresses text to structured summary",
                input_schema={"type": "object", "required": ["text"], ...},
                default_tier="local",
            ),
            WorkerDescriptor(
                name="extractor",
                description="Extracts structured fields from text",
                input_schema={"type": "object", "required": ["text", "fields"], ...},
                default_tier="standard",
            ),
        ]
        decomposer = GoalDecomposer(backend=ollama_backend, workers=workers)
        tasks = await decomposer.decompose(
            goal="Summarize this report and extract the key dates",
            context={"text": "...report content..."},
        )
        # tasks is a list[TaskMessage] ready for dispatch
    """

    def __init__(
        self,
        backend: LLMBackend,
        workers: list[WorkerDescriptor],
        *,
        max_tokens: int = 2000,
        temperature: float = 0.0,
    ) -> None:
        self._backend = backend
        self._workers = workers
        self._worker_names = {w.name for w in workers}
        self._system_prompt = _build_system_prompt(workers)
        self._max_tokens = max_tokens
        self._temperature = temperature

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    # TODO(Strategy E): Add decomposition caching. Goals with structurally
    #   similar instructions (same intent, same worker set) often produce
    #   identical decomposition plans. A cache keyed by a goal fingerprint
    #   (e.g. instruction template + available worker types) would skip the
    #   LLM call entirely for repeated patterns.

    async def decompose(
        self,
        goal: str,
        context: dict[str, Any] | None = None,
        *,
        parent_task_id: str | None = None,
        priority: TaskPriority = TaskPriority.NORMAL,
    ) -> list[TaskMessage]:
        """Decompose a high-level goal into a list of TaskMessage objects.

        Sends the goal and context to the LLM along with descriptions of all
        available workers. The LLM returns a JSON plan which is parsed and
        validated into TaskMessage instances.

        This method never raises on LLM or parsing failures -- it logs the
        error and returns an empty list. The orchestrator can then decide
        whether to retry with different parameters or report failure upstream.

        Args:
            goal: Natural-language description of what needs to be accomplished.
            context: Optional domain-specific data dict (e.g. file references,
                category lists, full text content). Included verbatim in the
                LLM prompt so it can construct appropriate payloads.
            parent_task_id: If this decomposition is part of a larger goal,
                all generated TaskMessages will carry this as their
                ``parent_task_id`` for result correlation. Typically set to
                ``OrchestratorGoal.goal_id``.
            priority: Default priority for generated tasks. Individual tasks
                may override this if the LLM specifies a different priority.

        Returns:
            A list of TaskMessage objects ready for dispatch to the router.
            Returns an empty list if:

            - The LLM backend call fails (network error, timeout, etc.)
            - The LLM response cannot be parsed as JSON
            - The LLM returns an empty plan
            - All sub-tasks fail validation (unknown worker types, etc.)
        """
        log = logger.bind(
            goal_preview=goal[:120],
            parent_task_id=parent_task_id,
        )
        log.info("decomposer.starting", num_workers=len(self._workers))

        user_message = _build_user_message(goal, context)

        # -- Call the LLM backend --
        try:
            response = await self._backend.complete(
                system_prompt=self._system_prompt,
                user_message=user_message,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            )
        except Exception:
            log.exception("decomposer.llm_call_failed")
            return []

        raw_content = response["content"]
        model_used = response.get("model", "unknown")
        log.debug(
            "decomposer.llm_response",
            model=model_used,
            prompt_tokens=response.get("prompt_tokens"),
            completion_tokens=response.get("completion_tokens"),
            response_length=len(raw_content),
        )

        # -- Parse the JSON response into raw subtask dicts --
        try:
            raw_tasks = _extract_json_array(raw_content)
        except ValueError:
            log.error(
                "decomposer.json_parse_failed",
                raw_preview=raw_content[:300],
            )
            return []

        if not raw_tasks:
            log.info("decomposer.empty_plan", goal_preview=goal[:120])
            return []

        # -- Validate and convert each raw dict into a TaskMessage --
        tasks: list[TaskMessage] = []
        for i, raw_task in enumerate(raw_tasks):
            task = self._parse_subtask(
                raw_task,
                index=i,
                parent_task_id=parent_task_id,
                default_priority=priority,
            )
            if task is not None:
                tasks.append(task)

        log.info(
            "decomposer.completed",
            total_planned=len(raw_tasks),
            total_valid=len(tasks),
            worker_types=[t.worker_type for t in tasks],
        )
        return tasks

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_subtask(
        self,
        raw: dict[str, Any],
        *,
        index: int,
        parent_task_id: str | None,
        default_priority: TaskPriority,
    ) -> TaskMessage | None:
        """Parse and validate a single raw sub-task dict into a TaskMessage.

        Validates that the worker_type is known, resolves the model tier
        and priority, and constructs a fully populated TaskMessage.

        Returns None (and logs a warning) if the sub-task is invalid, rather
        than raising -- this keeps the decompose() loop resilient against
        partially malformed LLM output.

        Args:
            raw: Dict from the LLM's JSON output representing one sub-task.
            index: Positional index of this sub-task in the plan (for logging).
            parent_task_id: Correlator linking this sub-task to the parent goal.
            default_priority: Fallback priority if the LLM didn't specify one.

        Returns:
            A validated TaskMessage, or None if validation failed.
        """
        log = logger.bind(subtask_index=index)

        # -- worker_type (required) --
        worker_type = raw.get("worker_type")
        if not worker_type:
            log.warning("decomposer.subtask_missing_worker_type", raw_keys=list(raw.keys()))
            return None

        if worker_type not in self._worker_names:
            log.warning(
                "decomposer.subtask_unknown_worker",
                worker_type=worker_type,
                available=sorted(self._worker_names),
            )
            return None

        # -- payload (required, must be dict) --
        payload = raw.get("payload")
        if payload is None:
            log.warning("decomposer.subtask_missing_payload", worker_type=worker_type)
            return None

        if not isinstance(payload, dict):
            log.warning(
                "decomposer.subtask_invalid_payload_type",
                worker_type=worker_type,
                payload_type=type(payload).__name__,
            )
            return None

        # -- model_tier (optional, with fallback chain) --
        model_tier = self._resolve_tier(raw.get("model_tier"), worker_type)

        # -- priority (optional, with fallback) --
        priority = self._resolve_priority(raw.get("priority"), default_priority)

        # -- metadata: store LLM rationale for debugging/audit --
        metadata: dict[str, Any] = {
            "subtask_index": index,
        }
        rationale = raw.get("rationale")
        if rationale:
            metadata["decomposer_rationale"] = rationale
        if parent_task_id:
            metadata["goal_id"] = parent_task_id

        return TaskMessage(
            task_id=str(uuid.uuid4()),
            parent_task_id=parent_task_id,
            worker_type=worker_type,
            input=payload,
            model_tier=model_tier,
            priority=priority,
            metadata=metadata,
        )

    def _resolve_tier(
        self,
        raw_tier: str | None,
        worker_type: str,
    ) -> ModelTier:
        """Resolve the model tier for a sub-task.

        Priority order:
            1. Explicit tier from the LLM's plan (if valid)
            2. The worker's default tier from its WorkerDescriptor
            3. ``ModelTier.STANDARD`` as the global fallback
        """
        # 1. Try the LLM-specified tier
        if raw_tier:
            try:
                return ModelTier(raw_tier)
            except ValueError:
                logger.warning(
                    "decomposer.invalid_tier_value",
                    raw_tier=raw_tier,
                    worker_type=worker_type,
                    fallback="worker default",
                )

        # 2. Fall back to the worker's configured default tier
        for w in self._workers:
            if w.name == worker_type:
                try:
                    return ModelTier(w.default_tier)
                except ValueError:
                    break

        # 3. Global fallback
        return ModelTier.STANDARD

    @staticmethod
    def _resolve_priority(
        raw_priority: str | None,
        default: TaskPriority,
    ) -> TaskPriority:
        """Resolve the priority for a sub-task.

        Uses the LLM-specified priority if valid, otherwise falls back to
        the provided default (typically inherited from the parent goal).
        """
        if raw_priority:
            try:
                return TaskPriority(raw_priority)
            except ValueError:
                logger.warning(
                    "decomposer.invalid_priority_value",
                    raw_priority=raw_priority,
                    fallback=default.value,
                )
        return default

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @classmethod
    def from_worker_configs(
        cls,
        backend: LLMBackend,
        configs: list[dict[str, Any]],
        **kwargs: Any,
    ) -> GoalDecomposer:
        """Build WorkerDescriptors from raw worker config dicts.

        This avoids the caller having to manually construct WorkerDescriptor
        objects when the data is already available as parsed YAML configs.

        Args:
            backend: The LLM backend to use for decomposition.
            configs: List of worker config dicts, each containing at minimum
                ``name`` and ``description`` keys. Typically loaded from the
                worker YAML files in ``configs/workers/``.
            **kwargs: Additional keyword arguments forwarded to the
                GoalDecomposer constructor (e.g. ``max_tokens``, ``temperature``).

        Returns:
            A configured GoalDecomposer instance.

        Example::

            import yaml

            with open("configs/workers/summarizer.yaml") as f:
                summarizer_cfg = yaml.safe_load(f)
            with open("configs/workers/classifier.yaml") as f:
                classifier_cfg = yaml.safe_load(f)

            decomposer = GoalDecomposer.from_worker_configs(
                backend=ollama_backend,
                configs=[summarizer_cfg, classifier_cfg],
            )
        """
        workers = [
            WorkerDescriptor(
                name=cfg["name"],
                description=cfg.get("description", "No description provided."),
                input_schema=cfg.get("input_schema", {}),
                default_tier=cfg.get("default_model_tier", "standard"),
            )
            for cfg in configs
        ]
        return cls(backend=backend, workers=workers, **kwargs)
