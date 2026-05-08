"""LLM worker actor.

Processes a single task via an LLM backend and resets.
No state carries between tasks — this is enforced, not optional.

Supports tool-use: when knowledge_silos include tool-type entries, the worker
offers those tools to the LLM and executes a multi-turn loop until the LLM
produces a final text answer.
"""

from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING, Any

import structlog

from heddle.tracing import get_tracer
from heddle.worker.base import TaskWorker
from heddle.worker.tools import MAX_TOOL_ROUNDS, ToolProvider, load_tool_provider

if TYPE_CHECKING:
    from heddle.worker.backends import LLMBackend

logger = structlog.get_logger()
_tracer = get_tracer("heddle.worker")

# Regex to strip markdown code fences that LLMs commonly wrap output in.
# Matches ```json, ```yaml, or bare ``` blocks — anchored version for clean responses.
_FENCE_RE = re.compile(r"^```(?:json|ya?ml)?\s*\n?(.*?)\n?\s*```$", re.DOTALL)
# Unanchored version: finds the first code fence anywhere (handles preamble text).
_FENCE_SEARCH_RE = re.compile(r"```(?:json|ya?ml)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def _find_first_balanced_object(text: str) -> str | None:
    r"""Return the first brace-balanced JSON object substring in ``text``.

    Walks character-by-character tracking brace depth while honouring
    JSON string literals (so braces inside ``"..."`` don't count).  The
    naive ``\{.*\}`` regex used previously was greedy: with two JSON
    objects or prose containing stray braces it captured a single
    ill-formed superstring spanning from the first ``{`` to the LAST
    ``}``.  This walk returns just the first balanced object — the
    correct behaviour for "extract a JSON object from a noisy LLM
    response".

    Returns ``None`` if no balanced ``{...}`` block exists.
    """
    start = -1
    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if in_string:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth == 0:
                # Stray ``}`` before any ``{`` — keep scanning for a real opener.
                continue
            depth -= 1
            if depth == 0 and start >= 0:
                return text[start : i + 1]
    return None


def _extract_json(raw: str) -> dict:
    """Extract a structured dict from an LLM response (JSON or YAML).

    LLMs frequently wrap output in markdown code fences or add explanatory
    text.  This function tries parsers in order of preference:

    1. Direct JSON parse (ideal — model returned clean JSON)
    2. Strip markdown fences and parse as JSON
    3. Extract the first { ... } block via regex and parse as JSON
    4. Strip markdown fences and parse as YAML (handles yaml_only workers)
    5. Direct YAML parse of the full response

    Raises ValueError if no valid structured output can be extracted.
    """
    stripped = raw.strip()

    # --- JSON attempts ---
    # 1. Direct JSON parse
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # 2. Strip markdown fences and try JSON
    fence_match = _FENCE_RE.match(stripped) or _FENCE_SEARCH_RE.search(stripped)
    fence_content = fence_match.group(1).strip() if fence_match else None
    if fence_content:
        try:
            return json.loads(fence_content)
        except json.JSONDecodeError:
            pass

    # 3. Extract the first balanced { ... } JSON block from anywhere in the response.
    #    Uses a brace-aware scan instead of a greedy regex — see
    #    ``_find_first_balanced_object`` for the why.
    obj_text = _find_first_balanced_object(stripped)
    if obj_text:
        try:
            return json.loads(obj_text)
        except json.JSONDecodeError:
            pass

    # --- YAML fallback (for workers with output_constraints.format: yaml_only) ---
    try:
        import yaml
    except ImportError:
        raise ValueError(f"LLM returned non-JSON: {raw[:200]}") from None

    # 4. Parse fenced content as YAML
    if fence_content:
        try:
            parsed = yaml.safe_load(fence_content)
            if isinstance(parsed, dict):
                return parsed
        except yaml.YAMLError:
            pass

    # 5. Direct YAML parse
    try:
        parsed = yaml.safe_load(stripped)
        if isinstance(parsed, dict):
            return parsed
    except yaml.YAMLError:
        pass

    raise ValueError(f"LLM returned non-JSON/YAML: {raw[:200]}")


async def execute_with_tools(  # noqa: PLR0915
    backend: LLMBackend,
    system_prompt: str,
    user_message: str,
    tool_providers: dict[str, ToolProvider],
    tool_defs: list[dict[str, Any]] | None,
    max_tokens: int = 2000,
) -> dict[str, Any]:
    """Execute an LLM call with multi-round tool-use loop.

    This function encapsulates the core LLM interaction pattern used by both
    ``LLMWorker.process()`` and ``WorkerTestRunner.run()``.  It handles:

    - Initial LLM call with optional tool definitions
    - Multi-turn tool execution loop (up to ``MAX_TOOL_ROUNDS``)
    - Token count aggregation across rounds
    - Error handling for unknown tools and tool execution failures

    Args:
        backend: LLM backend to call (Anthropic, Ollama, OpenAI-compatible).
        system_prompt: Full system prompt (with knowledge silo content prepended).
        user_message: User message (typically JSON payload).
        tool_providers: Map of tool name → ToolProvider for execution.
        tool_defs: Tool definitions list for the LLM (or None for no tools).
        max_tokens: Maximum output tokens per LLM call.

    Returns:
        Dict with keys: content, model, prompt_tokens, completion_tokens,
        tool_calls, stop_reason.  Token counts are aggregated across all
        tool-use rounds.
    """
    with _tracer.start_as_current_span(
        "llm.call",
        attributes={"llm.max_tokens": max_tokens, "llm.has_tools": tool_defs is not None},
    ) as llm_span:
        result = await backend.complete(
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=max_tokens,
            tools=tool_defs,
        )
        # Legacy attributes (backward compat)
        llm_span.set_attribute("llm.model", result.get("model", "unknown"))
        llm_span.set_attribute("llm.prompt_tokens", result.get("prompt_tokens", 0))
        llm_span.set_attribute("llm.completion_tokens", result.get("completion_tokens", 0))

        # OTel GenAI semantic conventions
        # See: https://opentelemetry.io/docs/specs/semconv/gen-ai/
        llm_span.set_attribute("gen_ai.system", result.get("gen_ai_system", "unknown"))
        llm_span.set_attribute("gen_ai.request.model", result.get("gen_ai_request_model", ""))
        llm_span.set_attribute("gen_ai.response.model", result.get("gen_ai_response_model", ""))
        llm_span.set_attribute("gen_ai.usage.input_tokens", result.get("prompt_tokens", 0))
        llm_span.set_attribute("gen_ai.usage.output_tokens", result.get("completion_tokens", 0))
        if result.get("gen_ai_request_temperature") is not None:
            llm_span.set_attribute(
                "gen_ai.request.temperature", result["gen_ai_request_temperature"]
            )
        if result.get("gen_ai_request_max_tokens") is not None:
            llm_span.set_attribute("gen_ai.request.max_tokens", result["gen_ai_request_max_tokens"])

        # Optional content logging (opt-in via env var — may contain PII)
        if os.environ.get("HEDDLE_TRACE_CONTENT", "").lower() in ("1", "true"):
            llm_span.add_event(
                "gen_ai.content.prompt",
                {"gen_ai.prompt": user_message},
            )
            if result.get("content"):
                llm_span.add_event(
                    "gen_ai.content.completion",
                    {"gen_ai.completion": result["content"]},
                )

    total_prompt_tokens = result.get("prompt_tokens", 0)
    total_completion_tokens = result.get("completion_tokens", 0)
    messages: list[dict[str, Any]] | None = None
    rounds = 0

    while result.get("tool_calls") and rounds < MAX_TOOL_ROUNDS:
        rounds += 1
        logger.info("worker.tool_round", round=rounds, calls=len(result["tool_calls"]))

        # Build message history on first tool round
        if messages is None:
            messages = [{"role": "user", "content": user_message}]

        # Append assistant message with tool calls
        assistant_msg: dict[str, Any] = {"role": "assistant", "tool_calls": result["tool_calls"]}
        if result.get("content"):
            assistant_msg["content"] = result["content"]
        messages.append(assistant_msg)

        # Execute each tool call
        for call in result["tool_calls"]:
            tool_name = call["name"]
            provider = tool_providers.get(tool_name)
            if provider is None:
                tool_result = json.dumps({"error": f"Unknown tool: {tool_name}"})
                logger.warning("worker.unknown_tool", tool=tool_name)
            else:
                try:
                    tool_result = await provider.execute(call["arguments"])
                except Exception as e:
                    tool_result = json.dumps({"error": str(e)})
                    logger.error("worker.tool_execution_failed", tool=tool_name, error=str(e))

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": tool_result,
                }
            )

        # Call LLM again with updated message history
        with _tracer.start_as_current_span(
            "llm.tool_continuation",
            attributes={"llm.tool_round": rounds},
        ) as cont_span:
            result = await backend.complete(
                system_prompt=system_prompt,
                user_message=user_message,
                messages=messages,
                max_tokens=max_tokens,
                tools=tool_defs,
            )
            cont_span.set_attribute("llm.prompt_tokens", result.get("prompt_tokens", 0))
            cont_span.set_attribute("llm.completion_tokens", result.get("completion_tokens", 0))
        total_prompt_tokens += result.get("prompt_tokens", 0)
        total_completion_tokens += result.get("completion_tokens", 0)

    if rounds >= MAX_TOOL_ROUNDS:
        logger.warning("worker.max_tool_rounds_reached", rounds=rounds)

    # Return result with aggregated token counts
    result["prompt_tokens"] = total_prompt_tokens
    result["completion_tokens"] = total_completion_tokens
    return result


class LLMWorker(TaskWorker):
    """
    LLM-backed stateless worker.

    Extends TaskWorker with LLM-specific logic:
    - Builds prompt from system_prompt + JSON payload
    - Loads knowledge silos (folder content → system prompt, tools → function-calling)
    - Calls the appropriate LLM backend by model tier
    - Executes multi-turn tool-use loop when tools are available
    - Parses JSON output from the LLM response (with fence-stripping fallback)
    - Applies silo_updates for writable folder silos
    """

    def __init__(
        self,
        actor_id: str,
        config_path: str,
        backends: dict[str, LLMBackend],
        nats_url: str = "nats://nats:4222",
    ) -> None:
        super().__init__(actor_id, config_path, nats_url)
        self.backends = backends

    async def disconnect(self) -> None:
        """Disconnect from the bus and close all owned LLM backends.

        Backends are closed best-effort: a failure on one does not
        prevent the others from being closed, and bus disconnection
        runs first so an exception here cannot leak the NATS connection.
        """
        try:
            await super().disconnect()
        finally:
            for tier, backend in self.backends.items():
                try:
                    await backend.aclose()
                except Exception as e:
                    logger.warning(
                        "worker.backend_close_failed",
                        tier=tier,
                        backend=type(backend).__name__,
                        error=str(e),
                    )

    async def process(self, payload: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
        """Build prompt, call LLM with tool-use loop, and parse structured output."""
        # 1. Build prompt
        system_prompt = self.config["system_prompt"]

        # 1a. Knowledge silo injection — load folder silos into system prompt
        silos = self.config.get("knowledge_silos", [])
        if silos:
            from heddle.worker.knowledge import load_knowledge_silos

            silo_text = load_knowledge_silos(silos)
            if silo_text:
                system_prompt = silo_text + "\n\n" + system_prompt

        # 1b. Legacy knowledge injection — prepend loaded knowledge to system prompt
        knowledge_sources = self.config.get("knowledge_sources", [])
        if knowledge_sources:
            from heddle.worker.knowledge import load_knowledge_sources

            knowledge_text = load_knowledge_sources(knowledge_sources)
            if knowledge_text:
                system_prompt = knowledge_text + "\n\n" + system_prompt

        # 1c. File-ref resolution — read workspace files and inject content.
        #     Failures here MUST fail the task: silently continuing produced
        #     LLM output for inputs the worker never actually saw, which the
        #     orchestrator then trusted as "successful processing of file X".
        #     Re-raising lets ``TaskWorker.handle_message`` publish a FAILED
        #     ``TaskResult`` with the offending file path in ``error``.
        workspace_dir = self.config.get("workspace_dir")
        file_ref_fields = self.config.get("resolve_file_refs", [])
        if workspace_dir and file_ref_fields:
            from heddle.core.workspace import WorkspaceManager

            ws = WorkspaceManager(workspace_dir)
            for field in file_ref_fields:
                if field in payload:
                    try:
                        content = ws.read_json(payload[field])
                        payload[f"{field}_content"] = content
                    except (ValueError, FileNotFoundError, json.JSONDecodeError) as e:
                        logger.error(
                            "worker.file_ref_resolution_failed",
                            field=field,
                            file_ref=payload[field],
                            error=str(e),
                        )
                        raise ValueError(
                            f"Failed to resolve file_ref '{payload[field]}' "
                            f"for field '{field}': {e}"
                        ) from e

        user_message = json.dumps(payload, indent=2)

        # 2. Load tool providers from knowledge_silos
        tool_providers = _load_tool_providers(silos)
        tool_defs = [p.get_definition() for p in tool_providers.values()] or None

        # 3. Resolve backend from task metadata or config default
        tier = metadata.get(
            "model_tier",
            self.config.get("default_model_tier", self.config.get("default_tier", "standard")),
        )
        backend = self.backends.get(tier)
        if not backend:
            raise RuntimeError(f"No backend for tier: {tier}")

        # 4. Call LLM with tool-use loop
        logger.info("worker.calling_llm", tier=tier, tools=len(tool_providers))
        max_tokens = self.config.get("max_output_tokens", 2000)
        result = await execute_with_tools(
            backend=backend,
            system_prompt=system_prompt,
            user_message=user_message,
            tool_providers=tool_providers,
            tool_defs=tool_defs,
            max_tokens=max_tokens,
        )
        total_prompt_tokens = result["prompt_tokens"]
        total_completion_tokens = result["completion_tokens"]

        # 4b. Log token usage for cost tracking.
        logger.info(
            "worker.llm_usage",
            worker_type=self.config.get("name", "unknown"),
            model_used=result.get("model", "unknown"),
            input_tokens=total_prompt_tokens,
            output_tokens=total_completion_tokens,
        )

        # 5. Parse JSON output — handles markdown fences and preamble text
        if result.get("content") is None:
            raise ValueError("LLM did not produce a text response after tool-use loop")

        output = _extract_json(result["content"])

        # 7. Process silo_updates — apply write-back to writable folder silos
        silo_updates = output.pop("silo_updates", None)
        if silo_updates:
            from heddle.worker.knowledge import apply_silo_updates

            apply_silo_updates(silo_updates, silos)

        return {
            "output": output,
            "model_used": result["model"],
            "token_usage": {
                "prompt_tokens": total_prompt_tokens,
                "completion_tokens": total_completion_tokens,
            },
        }


def _load_tool_providers(silos: list[dict[str, Any]]) -> dict[str, ToolProvider]:
    """Load tool providers from tool-type knowledge silos.

    Returns a dict mapping tool name → ToolProvider instance.
    """
    providers: dict[str, ToolProvider] = {}
    for silo in silos:
        if silo.get("type") != "tool":
            continue
        class_path = silo.get("provider", "")
        config = silo.get("config", {})
        try:
            provider = load_tool_provider(class_path, config)
            definition = provider.get_definition()
            name = definition["name"]
            providers[name] = provider
            logger.info("worker.tool_loaded", tool=name, provider=class_path)
        except Exception as e:
            logger.error("worker.tool_load_failed", provider=class_path, error=str(e))
    return providers
