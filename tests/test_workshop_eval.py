"""Tests for EvalRunner (workshop/eval_runner.py)."""

from __future__ import annotations

import json

import pytest

from heddle.worker.backends import LLMBackend
from heddle.workshop.db import WorkshopDB
from heddle.workshop.eval_runner import (
    EvalRunner,
    _score_exact_match,
    _score_field_match,
    _score_llm_judge,
)
from heddle.workshop.test_runner import WorkerTestRunner

# ---------------------------------------------------------------------------
# Mock backend
# ---------------------------------------------------------------------------


class MockEvalBackend(LLMBackend):
    """Returns output based on input payload content."""

    async def complete(
        self, system_prompt, user_message, max_tokens=2000, temperature=0.0, **kwargs
    ):
        # Parse the input to generate predictable output
        try:
            payload = json.loads(user_message)
            text = payload.get("text", "")
        except json.JSONDecodeError:
            text = ""
        return {
            "content": json.dumps({"summary": f"Summary of: {text}"}),
            "model": "mock-eval",
            "prompt_tokens": 50,
            "completion_tokens": 25,
            "tool_calls": None,
            "stop_reason": "end_turn",
        }


EVAL_CONFIG = {
    "name": "eval_worker",
    "system_prompt": "Summarize the text.",
    "input_schema": {
        "type": "object",
        "required": ["text"],
        "properties": {"text": {"type": "string"}},
    },
    "output_schema": {
        "type": "object",
        "required": ["summary"],
        "properties": {"summary": {"type": "string"}},
    },
    "default_model_tier": "local",
}


# ---------------------------------------------------------------------------
# Scoring function tests
# ---------------------------------------------------------------------------


class TestScoringFunctions:
    def test_exact_match_identical(self):
        score, details = _score_exact_match({"a": 1}, {"a": 1})
        assert score == 1.0

    def test_exact_match_different(self):
        score, details = _score_exact_match({"a": 1}, {"a": 2})
        assert score == 0.0

    def test_field_match_all_match(self):
        score, details = _score_field_match(
            {"name": "Alice", "age": 30},
            {"name": "Alice", "age": 30},
        )
        assert score == 1.0

    def test_field_match_partial(self):
        score, details = _score_field_match(
            {"name": "Alice", "age": 30},
            {"name": "Alice", "age": 25},
        )
        assert score == 0.5
        assert details["fields"]["name"] == 1.0
        assert details["fields"]["age"] == 0.0

    def test_field_match_case_insensitive_strings(self):
        score, _ = _score_field_match(
            {"name": "Alice"},
            {"name": "alice"},
        )
        assert score == 1.0

    def test_field_match_list_subset(self):
        score, details = _score_field_match(
            {"tags": ["a", "b"]},
            {"tags": ["a", "b", "c"]},
        )
        assert score == 1.0  # All expected items present

    def test_field_match_list_partial(self):
        score, details = _score_field_match(
            {"tags": ["a", "b"]},
            {"tags": ["a", "c"]},
        )
        assert score == 0.5  # Only "a" matches

    def test_field_match_empty_expected(self):
        score, _ = _score_field_match({}, {"any": "thing"})
        assert score == 1.0

    def test_field_match_missing_field(self):
        score, details = _score_field_match(
            {"name": "Alice"},
            {},
        )
        assert score == 0.0


# ---------------------------------------------------------------------------
# EvalRunner tests
# ---------------------------------------------------------------------------


class TestEvalRunner:
    @pytest.fixture
    def runner_and_db(self):
        db = WorkshopDB(":memory:")
        backend = MockEvalBackend()
        test_runner = WorkerTestRunner({"local": backend})
        eval_runner = EvalRunner(test_runner, db)
        yield eval_runner, db
        db.close()

    @pytest.mark.asyncio
    async def test_run_suite_basic(self, runner_and_db):
        eval_runner, db = runner_and_db

        suite = [
            {
                "name": "case_1",
                "input": {"text": "hello"},
                "expected_output": {"summary": "Summary of: hello"},
            },
            {
                "name": "case_2",
                "input": {"text": "world"},
                "expected_output": {"summary": "Summary of: world"},
            },
        ]

        run_id = await eval_runner.run_suite(EVAL_CONFIG, suite)

        runs = db.get_eval_runs("eval_worker")
        assert len(runs) == 1
        assert runs[0]["status"] == "completed"
        assert runs[0]["total_cases"] == 2

        results = db.get_eval_results(run_id)
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_run_suite_records_scores(self, runner_and_db):
        eval_runner, db = runner_and_db

        suite = [
            {
                "name": "match",
                "input": {"text": "test"},
                "expected_output": {"summary": "Summary of: test"},
            },
            {
                "name": "mismatch",
                "input": {"text": "test"},
                "expected_output": {"summary": "wrong"},
            },
        ]

        run_id = await eval_runner.run_suite(EVAL_CONFIG, suite, scoring="field_match")
        results = db.get_eval_results(run_id)

        by_name = {r["case_name"]: r for r in results}
        assert by_name["match"]["score"] == 1.0
        assert by_name["mismatch"]["score"] < 1.0

    @pytest.mark.asyncio
    async def test_run_suite_saves_worker_version(self, runner_and_db):
        eval_runner, db = runner_and_db

        suite = [{"name": "c1", "input": {"text": "x"}}]
        await eval_runner.run_suite(EVAL_CONFIG, suite)

        versions = db.get_worker_versions("eval_worker")
        assert len(versions) == 1

    @pytest.mark.asyncio
    async def test_run_suite_without_expected_output(self, runner_and_db):
        eval_runner, db = runner_and_db

        suite = [
            {"name": "no_expected", "input": {"text": "test"}},
        ]
        run_id = await eval_runner.run_suite(EVAL_CONFIG, suite)

        results = db.get_eval_results(run_id)
        assert len(results) == 1
        # Should still pass since output is valid
        assert results[0]["passed"] is True

    @pytest.mark.asyncio
    async def test_run_suite_exact_match_scoring(self, runner_and_db):
        eval_runner, db = runner_and_db

        suite = [
            {
                "name": "exact",
                "input": {"text": "test"},
                "expected_output": {"summary": "Summary of: test"},
            },
        ]
        run_id = await eval_runner.run_suite(EVAL_CONFIG, suite, scoring="exact_match")

        results = db.get_eval_results(run_id)
        assert results[0]["score"] == 1.0

    @pytest.mark.asyncio
    async def test_run_summary_aggregates(self, runner_and_db):
        eval_runner, db = runner_and_db

        suite = [{"name": f"case_{i}", "input": {"text": f"text_{i}"}} for i in range(5)]
        await eval_runner.run_suite(EVAL_CONFIG, suite)

        runs = db.get_eval_runs("eval_worker")
        run = runs[0]
        assert run["total_cases"] == 5
        assert run["passed_cases"] + run["failed_cases"] == 5
        assert run["avg_latency_ms"] is not None

    @pytest.mark.asyncio
    async def test_run_suite_marks_failed_on_unexpected_error(self, runner_and_db):
        """An exception outside the per-case path must not leave the run pinned
        at ``status='running'``.  Patches the post-aggregation update so the
        first call (the success-path summary write) raises, then verifies the
        recovery write set ``status='failed'`` with the error captured.
        """
        eval_runner, db = runner_and_db

        real_update = db.update_eval_run
        call_count = {"n": 0}

        def patched_update(run_id, updates):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("synthetic db failure")
            return real_update(run_id, updates)

        db.update_eval_run = patched_update

        suite = [{"name": "c1", "input": {"text": "x"}}]
        with pytest.raises(RuntimeError, match="synthetic db failure"):
            await eval_runner.run_suite(EVAL_CONFIG, suite)

        runs = db.get_eval_runs("eval_worker")
        assert len(runs) == 1
        assert runs[0]["status"] == "failed"
        assert runs[0]["error"] is not None
        assert "synthetic db failure" in runs[0]["error"]

    @pytest.mark.asyncio
    async def test_run_suite_isolates_per_case_exception(self, runner_and_db):
        """``return_exceptions=True`` keeps one bad case from cancelling its
        siblings.  An exception that escapes ``test_runner.run`` (mocked here
        via a stubbed runner) is captured and persisted as a failed
        ``eval_results`` row; the suite still completes.
        """
        eval_runner, db = runner_and_db

        real_run = eval_runner.test_runner.run

        async def flaky_run(config, payload, tier=None):
            if payload.get("text") == "boom":
                raise RuntimeError("backend exploded")
            return await real_run(config, payload, tier=tier)

        eval_runner.test_runner.run = flaky_run

        suite = [
            {"name": "ok", "input": {"text": "hello"}},
            {"name": "boom", "input": {"text": "boom"}},
        ]
        run_id = await eval_runner.run_suite(EVAL_CONFIG, suite)

        runs = db.get_eval_runs("eval_worker")
        assert runs[0]["status"] == "completed"  # not "failed" — case isolated
        assert runs[0]["error"] is None

        results = db.get_eval_results(run_id)
        by_name = {r["case_name"]: r for r in results}
        assert by_name["boom"]["passed"] is False
        assert by_name["boom"]["error"] is not None
        assert "backend exploded" in by_name["boom"]["error"]


# ---------------------------------------------------------------------------
# LLM-as-judge scoring tests
# ---------------------------------------------------------------------------


class MockJudgeBackend(LLMBackend):
    """Returns a judge evaluation JSON."""

    def __init__(self, score: float = 0.85):
        self.score = score

    async def complete(
        self, system_prompt, user_message, max_tokens=2000, temperature=0.0, **kwargs
    ):
        return {
            "content": json.dumps(
                {
                    "score": self.score,
                    "reasoning": "Good output quality.",
                    "criteria": {
                        "correctness": self.score,
                        "completeness": self.score,
                        "format_compliance": 1.0,
                    },
                }
            ),
            "model": "mock-judge",
            "prompt_tokens": 200,
            "completion_tokens": 50,
            "tool_calls": None,
            "stop_reason": "end_turn",
        }


class MockJudgeBackendBadJSON(LLMBackend):
    """Returns invalid JSON from judge."""

    async def complete(
        self, system_prompt, user_message, max_tokens=2000, temperature=0.0, **kwargs
    ):
        return {
            "content": "This is not JSON at all",
            "model": "mock-judge-bad",
            "prompt_tokens": 200,
            "completion_tokens": 50,
            "tool_calls": None,
            "stop_reason": "end_turn",
        }


class TestLLMJudgeScoring:
    @pytest.mark.asyncio
    async def test_llm_judge_basic(self):
        backend = MockJudgeBackend(score=0.9)
        score, details = await _score_llm_judge(
            {"summary": "expected"},
            {"summary": "actual"},
            backend=backend,
            worker_system_prompt="Summarize.",
            input_payload={"text": "hello"},
        )
        assert score == 0.9
        assert details["method"] == "llm_judge"
        assert details["reasoning"] == "Good output quality."
        assert "criteria" in details
        assert details["model"] == "mock-judge"

    @pytest.mark.asyncio
    async def test_llm_judge_no_expected_output(self):
        backend = MockJudgeBackend(score=0.7)
        score, details = await _score_llm_judge(
            None,
            {"summary": "actual"},
            backend=backend,
            worker_system_prompt="Summarize.",
            input_payload={"text": "hello"},
        )
        assert score == 0.7

    @pytest.mark.asyncio
    async def test_llm_judge_bad_json_returns_zero(self):
        backend = MockJudgeBackendBadJSON()
        score, details = await _score_llm_judge(
            {"summary": "expected"},
            {"summary": "actual"},
            backend=backend,
            worker_system_prompt="Summarize.",
            input_payload={"text": "hello"},
        )
        assert score == 0.0
        assert "error" in details

    @pytest.mark.asyncio
    async def test_llm_judge_score_clamped(self):
        """Scores outside [0, 1] are clamped."""

        class HighScoreBackend(LLMBackend):
            async def complete(self, system_prompt, user_message, **kwargs):
                return {
                    "content": json.dumps({"score": 5.0, "reasoning": "over"}),
                    "model": "mock",
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "tool_calls": None,
                    "stop_reason": "end_turn",
                }

        score, _ = await _score_llm_judge(
            {"a": 1},
            {"a": 1},
            backend=HighScoreBackend(),
            worker_system_prompt="test",
            input_payload={},
        )
        assert score == 1.0

    @pytest.mark.asyncio
    async def test_llm_judge_markdown_fences_stripped(self):
        """Judge output wrapped in markdown fences is handled."""

        class FencedBackend(LLMBackend):
            async def complete(self, system_prompt, user_message, **kwargs):
                return {
                    "content": '```json\n{"score": 0.75, "reasoning": "ok"}\n```',
                    "model": "mock",
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "tool_calls": None,
                    "stop_reason": "end_turn",
                }

        score, details = await _score_llm_judge(
            {"a": 1},
            {"a": 1},
            backend=FencedBackend(),
            worker_system_prompt="test",
            input_payload={},
        )
        assert score == 0.75


class TestEvalRunnerWithLLMJudge:
    @pytest.fixture
    def runner_and_db(self):
        db = WorkshopDB(":memory:")
        backend = MockEvalBackend()
        test_runner = WorkerTestRunner({"local": backend})
        eval_runner = EvalRunner(test_runner, db)
        yield eval_runner, db
        db.close()

    @pytest.mark.asyncio
    async def test_run_suite_llm_judge(self, runner_and_db):
        eval_runner, db = runner_and_db
        judge = MockJudgeBackend(score=0.85)

        suite = [
            {
                "name": "case_1",
                "input": {"text": "hello"},
                "expected_output": {"summary": "Summary of: hello"},
            },
        ]

        run_id = await eval_runner.run_suite(
            EVAL_CONFIG, suite, scoring="llm_judge", judge_backend=judge
        )

        results = db.get_eval_results(run_id)
        assert len(results) == 1
        assert results[0]["score"] == 0.85
        score_details = json.loads(results[0]["score_details"])
        assert score_details["method"] == "llm_judge"

    @pytest.mark.asyncio
    async def test_run_suite_llm_judge_requires_backend(self, runner_and_db):
        eval_runner, _ = runner_and_db

        suite = [{"name": "c1", "input": {"text": "x"}}]
        with pytest.raises(ValueError, match="judge_backend is required"):
            await eval_runner.run_suite(EVAL_CONFIG, suite, scoring="llm_judge")

    @pytest.mark.asyncio
    async def test_run_suite_llm_judge_no_expected(self, runner_and_db):
        eval_runner, db = runner_and_db
        judge = MockJudgeBackend(score=0.6)

        suite = [{"name": "no_expected", "input": {"text": "test"}}]
        run_id = await eval_runner.run_suite(
            EVAL_CONFIG, suite, scoring="llm_judge", judge_backend=judge
        )

        results = db.get_eval_results(run_id)
        assert len(results) == 1
        assert results[0]["score"] == 0.6
