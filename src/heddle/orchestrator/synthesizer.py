"""
Result aggregation for orchestrators.

Responsible for combining results from multiple workers
into a coherent final output.

This module is used by OrchestratorActor (runner.py), NOT by
PipelineOrchestrator (which simply collects stage outputs into a dict).

Two modes of operation:

    1. **Simple merge** (no LLM backend required)
       Partitions results into succeeded/failed, aggregates outputs into a
       structured dict with metadata.  Fast, deterministic, zero cost.

    2. **LLM synthesis** (requires an LLM backend + a goal string)
       Sends the collected worker outputs to an LLM with instructions to
       produce a coherent narrative synthesis.  Use this when the orchestrator
       needs to present a unified answer to the user rather than a bag of
       sub-results.

Design decisions:
    - Partial failures are first-class: every output dict contains both
      ``succeeded`` and ``failed`` sections so callers never lose visibility
      into what went wrong.
    - The LLM synthesis prompt is kept internal to this module; callers only
      pass the goal string and the list of TaskResults.
    - Token-budget awareness: if the combined result text is very large, the
      synthesizer truncates individual outputs before sending them to the LLM
      to avoid blowing the context window.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import structlog

from heddle.core.messages import TaskResult, TaskStatus

if TYPE_CHECKING:
    from heddle.worker.backends import LLMBackend

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum character length for a single worker output when building the LLM
# prompt.  If an output serialises to more than this, it gets truncated with
# an ellipsis marker so the synthesis prompt stays within reasonable token
# budgets.  The value is deliberately conservative — 6 000 chars is roughly
# 1 500 tokens, and with 10 worker outputs that puts us at ~15 000 tokens
# for the user message alone, well within even small model context windows.
_MAX_OUTPUT_CHARS = 6_000

# System prompt used for LLM-based synthesis.
_SYNTHESIS_SYSTEM_PROMPT = """\
You are a result synthesizer.  You receive the outputs of several \
specialist workers that each processed a sub-task of a larger goal.

Your job:
1. Read every worker output carefully.
2. Identify agreements, contradictions, and gaps across the outputs.
3. Produce a single coherent answer that addresses the original goal.
4. If any workers failed, note what information is missing and how it \
   affects the overall answer.

Respond with valid JSON in this exact schema:
{
    "synthesis": "<your coherent combined answer>",
    "confidence": "<high | medium | low>",
    "conflicts": ["<description of any contradictions between outputs>"],
    "gaps": ["<description of missing information from failed tasks>"]
}

Do NOT wrap the JSON in markdown fences.  Output the raw JSON object only.\
"""


class ResultSynthesizer:
    """Combines multiple worker :class:`TaskResult` objects into a final output.

    The synthesizer operates in one of two modes depending on how it is
    constructed and invoked:

    **Simple merge** (default, no LLM):
        Call :meth:`merge` or call :meth:`synthesize` without a ``goal``.
        Returns a structured dict with ``succeeded`` and ``failed`` sections
        plus aggregate metadata.

    **LLM synthesis** (requires ``backend`` and a ``goal``):
        Call :meth:`synthesize` with a ``goal`` string.  The LLM receives the
        original goal, all worker outputs, and instructions to produce a
        unified answer.

    Parameters
    ----------
    backend : LLMBackend | None
        An optional LLM backend (e.g. :class:`OllamaBackend`,
        :class:`AnthropicBackend`).  When provided *and* a ``goal`` is passed
        to :meth:`synthesize`, the synthesizer will use the LLM to produce a
        coherent narrative.  When ``None``, only deterministic merge is
        available.
    max_output_chars : int
        Per-result character budget when building the LLM prompt.  Outputs
        longer than this are truncated to avoid exceeding the model's context
        window.  Defaults to :data:`_MAX_OUTPUT_CHARS`.

    Example:
    -------
    ::

        # Simple merge (no LLM)
        synth = ResultSynthesizer()
        merged = synth.merge(results)

        # LLM synthesis
        synth = ResultSynthesizer(backend=my_ollama_backend)
        combined = await synth.synthesize(results, goal="Summarise the document")
    """

    def __init__(
        self,
        backend: LLMBackend | None = None,
        max_output_chars: int = _MAX_OUTPUT_CHARS,
    ) -> None:
        self._backend = backend
        self._max_output_chars = max_output_chars

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def merge(self, results: list[TaskResult]) -> dict[str, Any]:
        """Deterministic merge of task results — no LLM involved.

        Partitions *results* into succeeded and failed groups, extracts their
        outputs (or errors), and returns a structured dict with aggregate
        metadata.

        Parameters
        ----------
        results : list[TaskResult]
            Worker results to merge.  May be empty.

        Returns:
        -------
        dict[str, Any]
            A dict with the following top-level keys:

            - ``succeeded`` — list of dicts, each containing ``task_id``,
              ``worker_type``, ``output``, ``model_used``, and
              ``processing_time_ms`` for every completed result.
            - ``failed`` — list of dicts, each containing ``task_id``,
              ``worker_type``, ``error``, and ``processing_time_ms`` for every
              failed result.
            - ``in_flight`` — list of dicts, each containing ``task_id``,
              ``worker_type``, ``status``, and ``processing_time_ms`` for any
              task still in a non-terminal state.  Empty under the current
              dynamic-orchestrator caller (cc49783 converts pending tasks
              to synthetic ``FAILED`` placeholders before synthesis).
            - ``metadata`` — aggregate statistics: ``total``, ``succeeded``,
              ``failed``, ``in_flight``, ``total_processing_time_ms``,
              ``models_used``, and ``total_tokens``.
        """
        parts = self._partition(results)
        succeeded = parts["succeeded"]
        failed = parts["failed"]
        in_flight = parts["in_flight"]

        succeeded_entries = [
            {
                "task_id": r.task_id,
                "worker_type": r.worker_type,
                "output": r.output,
                "model_used": r.model_used,
                "processing_time_ms": r.processing_time_ms,
            }
            for r in succeeded
        ]

        failed_entries = [
            {
                "task_id": r.task_id,
                "worker_type": r.worker_type,
                "error": r.error,
                "processing_time_ms": r.processing_time_ms,
            }
            for r in failed
        ]

        in_flight_entries = [
            {
                "task_id": r.task_id,
                "worker_type": r.worker_type,
                "status": r.status.value,
                "processing_time_ms": r.processing_time_ms,
            }
            for r in in_flight
        ]

        # Aggregate token usage across all results (succeeded or not).
        total_tokens: dict[str, int] = {}
        for r in results:
            for key, value in r.token_usage.items():
                total_tokens[key] = total_tokens.get(key, 0) + value

        # Collect distinct model identifiers (filter out None).
        models_used = sorted({r.model_used for r in results if r.model_used is not None})

        metadata = {
            "total": len(results),
            "succeeded": len(succeeded),
            "failed": len(failed),
            "in_flight": len(in_flight),
            "total_processing_time_ms": sum(r.processing_time_ms for r in results),
            "models_used": models_used,
            "total_tokens": total_tokens,
        }

        if in_flight:
            # Distinct event from merge_partial_failure: in-flight tasks
            # are *not* errors and a future caller passing live state
            # should be observable as a separate operator signal.
            logger.warning(
                "synthesizer.merge_with_in_flight",
                total=len(results),
                in_flight=len(in_flight),
                in_flight_workers=[r.worker_type for r in in_flight],
            )
        if failed:
            logger.warning(
                "synthesizer.merge_partial_failure",
                total=len(results),
                failed=len(failed),
                failed_workers=[r.worker_type for r in failed],
            )
        if not failed and not in_flight:
            logger.info(
                "synthesizer.merge_complete",
                total=len(results),
            )

        return {
            "succeeded": succeeded_entries,
            "failed": failed_entries,
            "in_flight": in_flight_entries,
            "metadata": metadata,
        }

    async def synthesize(
        self,
        results: list[TaskResult],
        goal: str | None = None,
    ) -> dict[str, Any]:
        """Combine worker results into a final coherent output.

        If an LLM backend was provided at construction time **and** a *goal*
        string is supplied, the method delegates to :meth:`_llm_synthesize`
        which asks the LLM to produce a unified narrative.  Otherwise it falls
        back to :meth:`merge`.

        Parameters
        ----------
        results : list[TaskResult]
            Worker results to synthesize.  May be empty (in which case the
            output will indicate that no results were available).
        goal : str | None
            The original high-level goal that spawned these tasks.  Required
            for LLM synthesis mode; ignored in merge mode.

        Returns:
        -------
        dict[str, Any]
            In **merge mode** the return value is identical to :meth:`merge`.

            In **LLM mode** the dict contains:

            - ``synthesis`` — the LLM's coherent combined answer (str).
            - ``confidence`` — ``"high"``, ``"medium"``, or ``"low"`` (str).
            - ``conflicts`` — list of contradictions the LLM identified.
            - ``gaps`` — list of missing information from failed tasks.
            - ``succeeded`` / ``failed`` / ``metadata`` — same as merge mode.
            - ``llm_metadata`` — model used and token counts for the synthesis
              call itself.
        """
        # Fast path: no results at all.
        if not results:
            logger.warning("synthesizer.no_results")
            return self.merge(results)

        # Decide mode.
        use_llm = self._backend is not None and goal is not None
        if not use_llm:
            return self.merge(results)

        return await self._llm_synthesize(results, goal)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _partition(
        results: list[TaskResult],
    ) -> dict[str, list[TaskResult]]:
        """Split results into ``succeeded``, ``failed``, and ``in_flight`` buckets.

        - ``succeeded`` — ``TaskStatus.COMPLETED``.
        - ``failed`` — ``TaskStatus.FAILED``.
        - ``in_flight`` — ``PENDING`` / ``PROCESSING`` (any non-terminal state).
          A non-terminal result should never reach the synthesizer
          today (the dynamic orchestrator converts pending tasks to
          synthetic ``FAILED`` placeholders via cc49783 before
          calling).  The bucket exists to keep the API honest if a
          future caller starts passing live state — the synthesizer
          MUST NOT silently relabel "still running" as "failed", which
          is what the prior ``(succeeded, failed)`` shape did.

        Returns a dict so callers ask for the keys they care about
        rather than positional unpack — adding ``in_flight`` to a
        tuple would have been a silent breakage at every callsite.
        """
        buckets: dict[str, list[TaskResult]] = {
            "succeeded": [],
            "failed": [],
            "in_flight": [],
        }
        for r in results:
            if r.status == TaskStatus.COMPLETED:
                buckets["succeeded"].append(r)
            elif r.status == TaskStatus.FAILED:
                buckets["failed"].append(r)
            else:
                # PENDING / PROCESSING (any non-terminal status) — still in flight.
                buckets["in_flight"].append(r)
        return buckets

    def _build_user_message(
        self,
        results: list[TaskResult],
        goal: str,
    ) -> str:
        """Build the user-facing prompt for the LLM synthesis call.

        The prompt contains the original goal followed by a numbered list of
        worker outputs.  Failed and in-flight tasks are listed separately
        so the LLM can reason about missing information without conflating
        the two categories — "did not respond yet" is a different epistemic
        state from "responded with an error."

        Individual outputs that exceed :attr:`_max_output_chars` are truncated
        to keep the overall prompt within token budget.
        """
        parts = self._partition(results)
        succeeded = parts["succeeded"]
        failed = parts["failed"]
        in_flight = parts["in_flight"]

        header = f"WORKER RESULTS ({len(succeeded)} succeeded, {len(failed)} failed"
        if in_flight:
            header += f", {len(in_flight)} still in flight"
        header += "):"
        sections: list[str] = [f"GOAL: {goal}", "", header, ""]

        # Succeeded outputs.
        for i, r in enumerate(succeeded, 1):
            output_str = json.dumps(r.output, ensure_ascii=False, default=str)
            if len(output_str) > self._max_output_chars:
                output_str = output_str[: self._max_output_chars] + "... [truncated]"
            sections.append(
                f"--- Worker #{i}: {r.worker_type} (task {r.task_id}) ---\n{output_str}"
            )
            sections.append("")

        # Failed tasks.
        if failed:
            sections.append("FAILED TASKS:")
            sections.extend(
                f"  - {r.worker_type} (task {r.task_id}): {r.error or 'unknown error'}"
                for r in failed
            )
            sections.append("")

        # In-flight tasks — listed under their own header so the LLM
        # knows these workers have not yet produced output (not failed).
        if in_flight:
            sections.append("STILL-IN-FLIGHT TASKS (no output yet, treat as missing not failed):")
            sections.extend(
                f"  - {r.worker_type} (task {r.task_id}): status={r.status.value}"
                for r in in_flight
            )
            sections.append("")

        return "\n".join(sections)

    async def _llm_synthesize(
        self,
        results: list[TaskResult],
        goal: str,
    ) -> dict[str, Any]:
        """Perform LLM-based synthesis of worker results.

        Sends the goal and all worker outputs to the configured LLM backend,
        parses the structured JSON response, and merges it with the
        deterministic merge output so callers always have access to both the
        raw data and the LLM narrative.

        If the LLM call or JSON parsing fails, the method falls back to a
        plain merge with an error annotation rather than raising.
        """
        assert self._backend is not None  # Guarded by caller.

        log = logger.bind(
            goal=goal[:80],
            result_count=len(results),
        )

        user_message = self._build_user_message(results, goal)
        log.info("synthesizer.llm_requesting", prompt_chars=len(user_message))

        # Get the deterministic merge first — we always include it.
        merged = self.merge(results)

        try:
            llm_response = await self._backend.complete(
                system_prompt=_SYNTHESIS_SYSTEM_PROMPT,
                user_message=user_message,
                # Allow a generous output for synthesis — the LLM may need
                # room to describe conflicts and gaps.
                max_tokens=4000,
                temperature=0.1,
            )
        except Exception:
            log.exception("synthesizer.llm_call_failed")
            merged["llm_error"] = "LLM synthesis call failed; falling back to merge."
            return merged

        raw_content: str = llm_response["content"]

        # Parse the LLM's structured JSON response.
        synthesis_data = self._parse_llm_json(raw_content)
        if synthesis_data is None:
            log.warning(
                "synthesizer.llm_parse_failed",
                raw_length=len(raw_content),
            )
            # Fall back: use the raw text as the synthesis string.
            synthesis_data = {
                "synthesis": raw_content,
                "confidence": "low",
                "conflicts": [],
                "gaps": [],
            }

        log.info(
            "synthesizer.llm_complete",
            confidence=synthesis_data.get("confidence"),
            conflicts=len(synthesis_data.get("conflicts", [])),
            gaps=len(synthesis_data.get("gaps", [])),
            prompt_tokens=llm_response.get("prompt_tokens", 0),
            completion_tokens=llm_response.get("completion_tokens", 0),
        )

        # Combine the LLM synthesis with the deterministic merge.
        return {
            # LLM-produced fields.
            "synthesis": synthesis_data.get("synthesis", ""),
            "confidence": synthesis_data.get("confidence", "low"),
            "conflicts": synthesis_data.get("conflicts", []),
            "gaps": synthesis_data.get("gaps", []),
            # Deterministic merge fields (always present).
            "succeeded": merged["succeeded"],
            "failed": merged["failed"],
            "metadata": merged["metadata"],
            # Metadata about the synthesis LLM call itself.
            "llm_metadata": {
                "model": llm_response.get("model"),
                "prompt_tokens": llm_response.get("prompt_tokens", 0),
                "completion_tokens": llm_response.get("completion_tokens", 0),
            },
        }

    @staticmethod
    def _parse_llm_json(raw: str) -> dict[str, Any] | None:
        """Attempt to parse the LLM output as JSON.

        LLMs sometimes wrap their output in markdown fences (```json ... ```)
        or add a preamble/postamble.  This method strips common wrappers
        before parsing.

        Returns ``None`` if parsing fails after all recovery attempts.
        """
        text = raw.strip()

        # Strip markdown code fences if present.
        if text.startswith("```"):
            # Remove opening fence (with optional language tag).
            first_newline = text.index("\n") if "\n" in text else len(text)
            text = text[first_newline + 1 :]
            # Remove closing fence.
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        # Try direct parse first.
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        # Last resort: find the first { ... } block in the string.
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start != -1 and brace_end > brace_start:
            try:
                parsed = json.loads(text[brace_start : brace_end + 1])
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        return None
