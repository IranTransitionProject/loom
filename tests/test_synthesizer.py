"""
Unit tests for ResultSynthesizer (orchestrator/synthesizer.py).

Tests cover:
- merge(): deterministic merge with succeeded/failed/empty results
- synthesize(): mode selection (merge vs. LLM)
- _llm_synthesize(): full synthesis flow with mock backend
- _parse_llm_json(): all parsing paths (clean, fenced, brace extraction)
- _partition(): succeeded vs. failed grouping
- _build_user_message(): prompt construction and truncation
"""

from __future__ import annotations

import json

import pytest

from heddle.core.messages import TaskResult, TaskStatus
from heddle.orchestrator.synthesizer import ResultSynthesizer
from heddle.worker.backends import LLMBackend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockSynthesisBackend(LLMBackend):
    def __init__(self, content: str):
        self._content = content

    async def complete(self, system_prompt, user_message, max_tokens, temperature, **kw):
        return {
            "content": self._content,
            "model": "mock-synth",
            "prompt_tokens": 200,
            "completion_tokens": 100,
        }


class FailingSynthesisBackend(LLMBackend):
    async def complete(self, *args, **kwargs):
        raise RuntimeError("synthesis failed")


def _make_result(
    *,
    status: TaskStatus = TaskStatus.COMPLETED,
    worker_type: str = "summarizer",
    output: dict | None = None,
    error: str | None = None,
    model_used: str = "mock-model",
    processing_time_ms: int = 100,
    token_usage: dict | None = None,
) -> TaskResult:
    return TaskResult(
        task_id="task-001",
        worker_type=worker_type,
        status=status,
        output=output or {"summary": "test result"},
        error=error,
        model_used=model_used,
        processing_time_ms=processing_time_ms,
        token_usage=token_usage or {"prompt_tokens": 50, "completion_tokens": 30},
    )


# ---------------------------------------------------------------------------
# merge() tests
# ---------------------------------------------------------------------------


class TestMerge:
    def test_merge_all_succeeded(self):
        results = [
            _make_result(worker_type="summarizer"),
            _make_result(worker_type="classifier"),
        ]
        synth = ResultSynthesizer()
        merged = synth.merge(results)

        assert len(merged["succeeded"]) == 2
        assert len(merged["failed"]) == 0
        assert merged["metadata"]["total"] == 2
        assert merged["metadata"]["succeeded"] == 2
        assert merged["metadata"]["failed"] == 0
        assert "mock-model" in merged["metadata"]["models_used"]

    def test_merge_with_failures(self):
        results = [
            _make_result(worker_type="summarizer"),
            _make_result(
                worker_type="extractor",
                status=TaskStatus.FAILED,
                error="timeout",
            ),
        ]
        synth = ResultSynthesizer()
        merged = synth.merge(results)

        assert merged["metadata"]["succeeded"] == 1
        assert merged["metadata"]["failed"] == 1
        assert merged["failed"][0]["error"] == "timeout"

    def test_merge_empty_results(self):
        synth = ResultSynthesizer()
        merged = synth.merge([])

        assert merged["succeeded"] == []
        assert merged["failed"] == []
        assert merged["metadata"]["total"] == 0

    def test_merge_aggregates_tokens(self):
        results = [
            _make_result(token_usage={"prompt_tokens": 100, "completion_tokens": 50}),
            _make_result(token_usage={"prompt_tokens": 200, "completion_tokens": 80}),
        ]
        synth = ResultSynthesizer()
        merged = synth.merge(results)

        assert merged["metadata"]["total_tokens"]["prompt_tokens"] == 300
        assert merged["metadata"]["total_tokens"]["completion_tokens"] == 130

    def test_merge_aggregates_processing_time(self):
        results = [
            _make_result(processing_time_ms=100),
            _make_result(processing_time_ms=250),
        ]
        synth = ResultSynthesizer()
        merged = synth.merge(results)
        assert merged["metadata"]["total_processing_time_ms"] == 350

    def test_merge_surfaces_in_flight_bucket(self):
        """An in-flight result lands in its own bucket and metadata count.

        Pins the contract for a future caller passing live state.  The
        current dynamic orchestrator never reaches this branch (cc49783
        pre-converts pending tasks to FAILED), but the synthesizer's
        API has to be honest about what it sees.
        """
        results = [
            _make_result(worker_type="summarizer"),  # COMPLETED
            _make_result(
                worker_type="extractor",
                status=TaskStatus.PROCESSING,
            ),
        ]
        synth = ResultSynthesizer()
        merged = synth.merge(results)

        assert len(merged["succeeded"]) == 1
        assert merged["failed"] == []
        assert len(merged["in_flight"]) == 1
        assert merged["in_flight"][0]["worker_type"] == "extractor"
        assert merged["in_flight"][0]["status"] == "processing"
        assert merged["metadata"]["in_flight"] == 1


# ---------------------------------------------------------------------------
# _partition() tests
# ---------------------------------------------------------------------------


class TestPartition:
    def test_partition_splits_three_ways(self):
        """``_partition`` returns three buckets: succeeded / failed / in_flight.

        Constructs ``TaskResult`` objects with each non-terminal status
        directly so the test pins the API contract independent of any
        upstream caller.  No current caller produces non-terminal
        states (the dynamic orchestrator synthesises FAILED placeholders
        for pending tasks before reaching the synthesizer, per cc49783)
        — this contract guards against a *future* caller that does.
        """
        results = [
            _make_result(status=TaskStatus.COMPLETED),
            _make_result(status=TaskStatus.FAILED),
            _make_result(status=TaskStatus.PROCESSING),
            _make_result(status=TaskStatus.PENDING),
            _make_result(status=TaskStatus.COMPLETED),
        ]
        parts = ResultSynthesizer._partition(results)
        assert len(parts["succeeded"]) == 2
        assert len(parts["failed"]) == 1
        assert len(parts["in_flight"]) == 2
        # Spot-check: every non-terminal status lands in in_flight.
        flight_statuses = {r.status for r in parts["in_flight"]}
        assert flight_statuses == {
            TaskStatus.PENDING,
            TaskStatus.PROCESSING,
        }

    def test_partition_empty(self):
        parts = ResultSynthesizer._partition([])
        assert parts == {"succeeded": [], "failed": [], "in_flight": []}

    def test_partition_does_not_relabel_in_flight_as_failed(self):
        """Regression guard for the prior ``(succeeded, failed)`` shape.

        The old API silently dumped PENDING/PROCESSING/RETRY into the
        ``failed`` bucket, which then surfaced to the LLM as "these
        tasks failed."  This test confirms the new shape keeps them
        distinct.
        """
        results = [_make_result(status=TaskStatus.PROCESSING)]
        parts = ResultSynthesizer._partition(results)
        assert parts["failed"] == []
        assert len(parts["in_flight"]) == 1


# ---------------------------------------------------------------------------
# _parse_llm_json() tests
# ---------------------------------------------------------------------------


class TestParseLlmJson:
    def test_clean_json(self):
        raw = '{"synthesis": "Combined answer", "confidence": "high", "conflicts": [], "gaps": []}'
        result = ResultSynthesizer._parse_llm_json(raw)
        assert result["synthesis"] == "Combined answer"

    def test_fenced_json(self):
        raw = '```json\n{"synthesis": "Answer", "confidence": "medium"}\n```'
        result = ResultSynthesizer._parse_llm_json(raw)
        assert result["synthesis"] == "Answer"

    def test_json_with_preamble(self):
        raw = 'Here is my analysis:\n{"synthesis": "Good", "confidence": "low"}\nDone.'
        result = ResultSynthesizer._parse_llm_json(raw)
        assert result["synthesis"] == "Good"

    def test_non_json_returns_none(self):
        result = ResultSynthesizer._parse_llm_json("This is not JSON")
        assert result is None

    def test_array_returns_none(self):
        """Only dict results are accepted, not arrays."""
        result = ResultSynthesizer._parse_llm_json("[1, 2, 3]")
        assert result is None


# ---------------------------------------------------------------------------
# synthesize() tests
# ---------------------------------------------------------------------------


class TestSynthesize:
    @pytest.mark.asyncio
    async def test_synthesize_without_backend_uses_merge(self):
        """No backend → always merge mode."""
        results = [_make_result()]
        synth = ResultSynthesizer(backend=None)
        output = await synth.synthesize(results, goal="Test goal")

        # Should have merge keys, not LLM keys
        assert "succeeded" in output
        assert "synthesis" not in output

    @pytest.mark.asyncio
    async def test_synthesize_without_goal_uses_merge(self):
        """Backend present but no goal → merge mode."""
        results = [_make_result()]
        synth = ResultSynthesizer(backend=MockSynthesisBackend("{}"))
        output = await synth.synthesize(results, goal=None)
        assert "synthesis" not in output

    @pytest.mark.asyncio
    async def test_synthesize_empty_results(self):
        synth = ResultSynthesizer(backend=MockSynthesisBackend("{}"))
        output = await synth.synthesize([], goal="Test")
        assert output["metadata"]["total"] == 0

    @pytest.mark.asyncio
    async def test_synthesize_with_llm(self):
        llm_output = json.dumps(
            {
                "synthesis": "Combined answer from workers",
                "confidence": "high",
                "conflicts": [],
                "gaps": [],
            }
        )
        results = [_make_result(), _make_result(worker_type="classifier")]
        synth = ResultSynthesizer(backend=MockSynthesisBackend(llm_output))
        output = await synth.synthesize(results, goal="Summarize and classify")

        assert output["synthesis"] == "Combined answer from workers"
        assert output["confidence"] == "high"
        assert output["conflicts"] == []
        assert output["gaps"] == []
        # Merge data is also present
        assert len(output["succeeded"]) == 2
        assert output["llm_metadata"]["model"] == "mock-synth"

    @pytest.mark.asyncio
    async def test_synthesize_llm_failure_falls_back_to_merge(self):
        results = [_make_result()]
        synth = ResultSynthesizer(backend=FailingSynthesisBackend())
        output = await synth.synthesize(results, goal="Test")

        # Should fall back to merge and add an error key
        assert "llm_error" in output
        assert "succeeded" in output

    @pytest.mark.asyncio
    async def test_synthesize_unparseable_llm_uses_raw_text(self):
        """If LLM returns non-JSON, raw text becomes the synthesis."""
        results = [_make_result()]
        synth = ResultSynthesizer(backend=MockSynthesisBackend("Just plain text"))
        output = await synth.synthesize(results, goal="Test")

        assert output["synthesis"] == "Just plain text"
        assert output["confidence"] == "low"


# ---------------------------------------------------------------------------
# _build_user_message() tests
# ---------------------------------------------------------------------------


class TestBuildUserMessage:
    def test_includes_goal_and_results(self):
        results = [_make_result()]
        synth = ResultSynthesizer()
        msg = synth._build_user_message(results, "Analyze the document")

        assert "GOAL: Analyze the document" in msg
        assert "WORKER RESULTS" in msg
        assert "summarizer" in msg

    def test_includes_failed_section(self):
        results = [
            _make_result(status=TaskStatus.FAILED, error="crashed"),
        ]
        synth = ResultSynthesizer()
        msg = synth._build_user_message(results, "Test")

        assert "FAILED TASKS:" in msg
        assert "crashed" in msg

    def test_truncates_long_output(self):
        long_output = {"data": "x" * 10000}
        results = [_make_result(output=long_output)]
        synth = ResultSynthesizer(max_output_chars=100)
        msg = synth._build_user_message(results, "Test")

        assert "[truncated]" in msg
