"""
EvalRunner — systematic test suite execution with scoring.

Runs a list of test cases against a worker config, scores each result,
and persists everything to WorkshopDB.

Scoring methods:
- ``exact_match``: Binary 1.0/0.0 based on output equality.
- ``field_match``: Fractional score based on per-field comparison.
- ``llm_judge``: Uses a separate LLM call to evaluate output quality
  on a 0-to-1 scale with reasoning.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from heddle.worker.backends import LLMBackend
    from heddle.workshop.db import WorkshopDB
    from heddle.workshop.test_runner import WorkerTestRunner

logger = structlog.get_logger()

# Default system prompt for the LLM judge.  Can be overridden per-run via
# ``judge_prompt`` on ``run_suite()``.
DEFAULT_JUDGE_PROMPT = """\
You are an evaluation judge. You will be given:
- A worker's system prompt (what the worker was asked to do)
- The input payload sent to the worker
- The expected output (if provided)
- The actual output produced by the worker

Evaluate the actual output on these criteria:
1. **Correctness**: Does the output match the expected output semantically?
2. **Completeness**: Does the output contain all required information?
3. **Format compliance**: Does the output follow the expected structure?

Respond with ONLY a JSON object (no markdown fences):
{
    "score": <float 0.0 to 1.0>,
    "reasoning": "<brief explanation of the score>",
    "criteria": {
        "correctness": <float 0.0 to 1.0>,
        "completeness": <float 0.0 to 1.0>,
        "format_compliance": <float 0.0 to 1.0>
    }
}
"""


def _score_exact_match(expected: dict, actual: dict) -> tuple[float, dict]:
    """Score 1.0 if outputs are identical, 0.0 otherwise."""
    match = expected == actual
    return (1.0 if match else 0.0, {"method": "exact_match", "match": match})


def _score_field_match(expected: dict, actual: dict) -> tuple[float, dict]:
    """Score = fraction of expected fields that match in actual output."""
    if not expected:
        return (1.0, {"method": "field_match", "fields": {}})

    field_scores = {}
    for key, expected_val in expected.items():
        actual_val = actual.get(key)
        if actual_val == expected_val:
            field_scores[key] = 1.0
        elif isinstance(expected_val, str) and isinstance(actual_val, str):
            # Normalized string comparison (case-insensitive, strip whitespace)
            field_scores[key] = (
                1.0 if expected_val.strip().lower() == actual_val.strip().lower() else 0.0
            )
        elif isinstance(expected_val, list) and isinstance(actual_val, list):
            # Check if expected items are a subset of actual
            expected_set = {str(v) for v in expected_val}
            actual_set = {str(v) for v in actual_val}
            if expected_set:
                overlap = len(expected_set & actual_set) / len(expected_set)
                field_scores[key] = overlap
            else:
                field_scores[key] = 1.0
        else:
            field_scores[key] = 0.0

    avg_score = sum(field_scores.values()) / len(field_scores)
    return (avg_score, {"method": "field_match", "fields": field_scores})


async def _score_llm_judge(
    expected: dict | None,
    actual: dict,
    *,
    backend: LLMBackend,
    worker_system_prompt: str,
    input_payload: dict,
    judge_prompt: str = DEFAULT_JUDGE_PROMPT,
) -> tuple[float, dict]:
    """Use an LLM to evaluate output quality.

    The judge receives the worker's system prompt, input, expected output,
    and actual output, then returns a 0-to-1 score with reasoning.

    Args:
        expected: Expected output (may be None).
        actual: Actual output from the worker.
        backend: LLM backend to use for the judge call.
        worker_system_prompt: The system prompt of the worker being evaluated.
        input_payload: The input that was sent to the worker.
        judge_prompt: System prompt for the judge LLM.

    Returns:
        Tuple of (score, details_dict).
    """
    user_message = json.dumps(
        {
            "worker_system_prompt": worker_system_prompt,
            "input": input_payload,
            "expected_output": expected,
            "actual_output": actual,
        },
        indent=2,
    )

    try:
        result = await backend.complete(
            system_prompt=judge_prompt,
            user_message=user_message,
            max_tokens=500,
            temperature=0.0,
        )

        content = result.get("content", "")
        # Strip markdown fences if present
        if "```" in content:
            lines = content.split("\n")
            lines = [line for line in lines if not line.strip().startswith("```")]
            content = "\n".join(lines)

        parsed = json.loads(content)
        score = float(parsed.get("score", 0.0))
        score = max(0.0, min(1.0, score))  # Clamp to [0, 1]

        return (
            score,
            {
                "method": "llm_judge",
                "reasoning": parsed.get("reasoning", ""),
                "criteria": parsed.get("criteria", {}),
                "model": result.get("model"),
                "judge_tokens": {
                    "prompt": result.get("prompt_tokens", 0),
                    "completion": result.get("completion_tokens", 0),
                },
            },
        )
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning("eval.llm_judge_parse_error", error=str(e))
        return (0.0, {"method": "llm_judge", "error": f"Judge parse error: {e}"})


class EvalRunner:
    """Run a test suite against a worker config and store results.

    Args:
        test_runner: WorkerTestRunner instance for executing individual tests.
        db: WorkshopDB for persisting results.
    """

    def __init__(self, test_runner: WorkerTestRunner, db: WorkshopDB) -> None:
        self.test_runner = test_runner
        self.db = db

    async def run_suite(  # noqa: PLR0915
        self,
        config: dict[str, Any],
        test_suite: list[dict[str, Any]],
        tier: str | None = None,
        scoring: str = "field_match",
        max_concurrency: int = 3,
        judge_backend: LLMBackend | None = None,
        judge_prompt: str | None = None,
    ) -> str:
        """Run all test cases and store results.

        Args:
            config: Worker config dict.
            test_suite: List of ``{"name": str, "input": dict, "expected_output": dict}``.
            tier: Model tier override.
            scoring: Scoring method — ``"exact_match"``, ``"field_match"``,
                or ``"llm_judge"``.
            max_concurrency: Max concurrent test case executions.
            judge_backend: LLM backend for ``llm_judge`` scoring.  Required
                when ``scoring="llm_judge"``.
            judge_prompt: Custom system prompt for the judge LLM.  Uses
                ``DEFAULT_JUDGE_PROMPT`` if not provided.

        Returns:
            The eval run ID.
        """
        import yaml

        if scoring == "llm_judge" and judge_backend is None:
            msg = "judge_backend is required when scoring='llm_judge'"
            raise ValueError(msg)

        worker_name = config.get("name", "unknown")
        worker_system_prompt = config.get("system_prompt", "")

        # Save worker version
        config_yaml = yaml.dump(config, default_flow_style=False, sort_keys=False)
        version_id = self.db.save_worker_version(worker_name, config_yaml)

        resolved_tier = tier or config.get("default_model_tier", "standard")

        # Create eval run with scoring method in metadata
        run_id = self.db.save_eval_run(
            worker_name=worker_name,
            tier=resolved_tier,
            total_cases=len(test_suite),
            worker_version_id=version_id,
            metadata={"scoring_method": scoring},
        )

        logger.info(
            "eval.suite_started",
            run_id=run_id,
            worker=worker_name,
            cases=len(test_suite),
            scoring=scoring,
        )

        # Select scoring function
        if scoring == "llm_judge":
            score_fn = None  # Handled specially (async)
        elif scoring == "field_match":
            score_fn = _score_field_match
        else:
            score_fn = _score_exact_match

        # Run test cases with bounded concurrency
        semaphore = asyncio.Semaphore(max_concurrency)
        passed = 0
        failed = 0
        total_latency = 0
        total_prompt = 0
        total_completion = 0

        async def run_one(case: dict) -> None:
            nonlocal passed, failed, total_latency, total_prompt, total_completion
            async with semaphore:
                case_name = case.get("name", "unnamed")
                input_payload = case.get("input", {})
                expected = case.get("expected_output")

                result = await self.test_runner.run(config, input_payload, tier=resolved_tier)

                # Score
                score = 0.0
                score_details: dict[str, Any] = {}
                case_passed = result.success

                if result.output and expected:
                    if scoring == "llm_judge":
                        assert judge_backend is not None  # Guaranteed by check above
                        score, score_details = await _score_llm_judge(
                            expected,
                            result.output,
                            backend=judge_backend,
                            worker_system_prompt=worker_system_prompt,
                            input_payload=input_payload,
                            judge_prompt=judge_prompt or DEFAULT_JUDGE_PROMPT,
                        )
                    else:
                        assert score_fn is not None
                        score, score_details = score_fn(expected, result.output)
                    case_passed = case_passed and score >= 0.5
                elif result.output and not expected:
                    if scoring == "llm_judge":
                        assert judge_backend is not None
                        score, score_details = await _score_llm_judge(
                            None,
                            result.output,
                            backend=judge_backend,
                            worker_system_prompt=worker_system_prompt,
                            input_payload=input_payload,
                            judge_prompt=judge_prompt or DEFAULT_JUDGE_PROMPT,
                        )
                        case_passed = case_passed and score >= 0.5
                    else:
                        score = 1.0 if result.success else 0.0
                        score_details = {"method": scoring, "note": "no expected output"}

                if case_passed:
                    passed += 1
                else:
                    failed += 1

                total_latency += result.latency_ms
                total_prompt += result.token_usage.get("prompt_tokens", 0)
                total_completion += result.token_usage.get("completion_tokens", 0)

                self.db.save_eval_result(
                    run_id=run_id,
                    case_name=case_name,
                    input_payload=input_payload,
                    passed=case_passed,
                    expected_output=expected,
                    actual_output=result.output,
                    raw_response=result.raw_response,
                    validation_errors=result.validation_errors or None,
                    score=score,
                    score_details=score_details,
                    latency_ms=result.latency_ms,
                    prompt_tokens=result.token_usage.get("prompt_tokens"),
                    completion_tokens=result.token_usage.get("completion_tokens"),
                    model_used=result.model_used,
                    error=result.error,
                )

        # ``return_exceptions=True`` keeps one bad case from cancelling its
        # siblings.  Per-case exceptions are persisted as failed
        # ``eval_results`` rows below so the run summary stays consistent.
        try:
            case_results = await asyncio.gather(
                *(run_one(case) for case in test_suite),
                return_exceptions=True,
            )

            for case, res in zip(test_suite, case_results, strict=True):
                if isinstance(res, Exception):
                    failed += 1
                    case_name = case.get("name", "unnamed")
                    err = f"{type(res).__name__}: {res}"
                    logger.error("eval.case_failed", run_id=run_id, case=case_name, error=err)
                    try:
                        self.db.save_eval_result(
                            run_id=run_id,
                            case_name=case_name,
                            input_payload=case.get("input", {}),
                            passed=False,
                            error=err,
                        )
                    except Exception:
                        logger.exception("eval.case_save_failed", run_id=run_id, case=case_name)

            # Update run summary
            n = len(test_suite)
            self.db.update_eval_run(
                run_id,
                {
                    "status": "completed",
                    "completed_at": datetime.now(UTC),
                    "passed_cases": passed,
                    "failed_cases": failed,
                    "avg_latency_ms": total_latency / n if n else 0,
                    "avg_prompt_tokens": total_prompt / n if n else 0,
                    "avg_completion_tokens": total_completion / n if n else 0,
                },
            )

            logger.info(
                "eval.suite_completed",
                run_id=run_id,
                passed=passed,
                failed=failed,
                total=n,
            )
        except Exception as exc:
            # Anything that escapes the per-case isolation (DB summary write
            # failure, unexpected error in the gather scaffolding, etc.) would
            # otherwise leave the eval_runs row pinned at ``status='running'``
            # forever.  Mark it failed with the error so operators see it.
            err = f"{type(exc).__name__}: {exc}"
            logger.exception("eval.suite_failed", run_id=run_id)
            try:
                self.db.update_eval_run(
                    run_id,
                    {
                        "status": "failed",
                        "completed_at": datetime.now(UTC),
                        "error": err,
                    },
                )
            except Exception:
                logger.exception("eval.suite_status_write_failed", run_id=run_id)
            raise

        return run_id
