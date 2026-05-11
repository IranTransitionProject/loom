"""Tests for ConvergenceDetector."""

from unittest.mock import AsyncMock

import pytest

from heddle.contrib.council.convergence import ConvergenceDetector, _parse_json
from heddle.contrib.council.schemas import ConvergenceConfig, TranscriptEntry
from heddle.contrib.council.transcript import TranscriptStore


def _store_with_rounds(*round_data):
    """Build a TranscriptStore with given rounds.

    Each element in round_data is a list of (agent_name, content) tuples.
    """
    store = TranscriptStore()
    for round_num, entries in enumerate(round_data, 1):
        store.start_round(round_num)
        for agent_name, content in entries:
            store.add_entry(
                TranscriptEntry(
                    round_num=round_num,
                    agent_name=agent_name,
                    content=content,
                )
            )
    return store


class TestCheckNone:
    async def test_always_not_converged(self):
        detector = ConvergenceDetector(ConvergenceConfig(method="none"))
        store = _store_with_rounds([("a", "hello")])
        result = await detector.check(store, 1, "topic")
        assert result.converged is False
        assert result.score == 0.0


class TestPositionStability:
    async def test_round1_not_converged(self):
        detector = ConvergenceDetector(
            ConvergenceConfig(method="position_stability", threshold=0.8)
        )
        store = _store_with_rounds([("a", "position")])
        result = await detector.check(store, 1, "topic")
        assert result.converged is False
        assert "2 rounds" in result.reason

    async def test_identical_positions_converge(self):
        detector = ConvergenceDetector(
            ConvergenceConfig(method="position_stability", threshold=0.9)
        )
        store = _store_with_rounds(
            [("a", "I agree with X"), ("b", "Y is good")],
            [("a", "I agree with X"), ("b", "Y is good")],
        )
        result = await detector.check(store, 2, "topic")
        assert result.converged is True
        assert result.score == 1.0

    async def test_divergent_positions_not_converged(self):
        detector = ConvergenceDetector(
            ConvergenceConfig(method="position_stability", threshold=0.9)
        )
        store = _store_with_rounds(
            [("a", "I think X"), ("b", "Y is the way")],
            [("a", "Actually Z"), ("b", "No, W is better")],
        )
        result = await detector.check(store, 2, "topic")
        assert result.converged is False
        assert result.score < 0.9

    async def test_no_common_agents(self):
        detector = ConvergenceDetector(
            ConvergenceConfig(method="position_stability", threshold=0.5)
        )
        store = _store_with_rounds(
            [("a", "hello")],
            [("b", "world")],
        )
        result = await detector.check(store, 2, "topic")
        assert result.converged is False


class TestLLMJudge:
    async def test_converged_high_score(self):
        mock_backend = AsyncMock()
        mock_backend.complete.return_value = {
            "content": '{"score": 0.95, "reason": "Strong consensus"}',
            "model": "mock",
            "prompt_tokens": 100,
            "completion_tokens": 20,
        }
        detector = ConvergenceDetector(
            ConvergenceConfig(method="llm_judge", threshold=0.8),
            backend=mock_backend,
        )
        store = _store_with_rounds(
            [("a", "I agree"), ("b", "Me too")],
        )
        result = await detector.check(store, 1, "topic")
        assert result.converged is True
        assert result.score == 0.95

    async def test_not_converged_low_score(self):
        mock_backend = AsyncMock()
        mock_backend.complete.return_value = {
            "content": '{"score": 0.3, "reason": "Major disagreements"}',
            "model": "mock",
            "prompt_tokens": 100,
            "completion_tokens": 20,
        }
        detector = ConvergenceDetector(
            ConvergenceConfig(method="llm_judge", threshold=0.8),
            backend=mock_backend,
        )
        store = _store_with_rounds([("a", "X"), ("b", "Y")])
        result = await detector.check(store, 1, "topic")
        assert result.converged is False
        assert result.score == 0.3

    async def test_no_backend_returns_not_converged(self):
        detector = ConvergenceDetector(
            ConvergenceConfig(method="llm_judge"),
            backend=None,
        )
        store = _store_with_rounds([("a", "X")])
        result = await detector.check(store, 1, "topic")
        assert result.converged is False
        assert "No LLM backend" in result.reason

    async def test_backend_failure_returns_not_converged(self):
        mock_backend = AsyncMock()
        mock_backend.complete.side_effect = RuntimeError("API down")
        detector = ConvergenceDetector(
            ConvergenceConfig(method="llm_judge", threshold=0.5),
            backend=mock_backend,
        )
        store = _store_with_rounds([("a", "X")])
        result = await detector.check(store, 1, "topic")
        assert result.converged is False

    async def test_unparseable_json_returns_not_converged(self):
        mock_backend = AsyncMock()
        mock_backend.complete.return_value = {
            "content": "I can't rate this properly",
            "model": "mock",
            "prompt_tokens": 50,
            "completion_tokens": 10,
        }
        detector = ConvergenceDetector(
            ConvergenceConfig(method="llm_judge", threshold=0.8),
            backend=mock_backend,
        )
        store = _store_with_rounds([("a", "X")])
        result = await detector.check(store, 1, "topic")
        assert result.converged is False

    @pytest.mark.parametrize(
        "score_value",
        [
            '"high"',  # ValueError on float()
            "null",  # TypeError on float()
            '"0.7; DROP TABLE x"',  # ValueError on float()
            "[]",  # TypeError on float()
        ],
    )
    async def test_non_numeric_score_returns_parse_failure(self, score_value):
        """Earlier ``float(parsed.get("score", 0.0))`` raised ValueError /
        TypeError on non-numeric LLM judge responses, tearing down the
        whole council.  ADR-004 (skip vs crash) calls for skip-and-log;
        the typed parse-failure ConvergenceResult is the existing shape.
        """
        mock_backend = AsyncMock()
        mock_backend.complete.return_value = {
            "content": f'{{"score": {score_value}, "reason": "junk"}}',
            "model": "mock",
            "prompt_tokens": 50,
            "completion_tokens": 10,
        }
        detector = ConvergenceDetector(
            ConvergenceConfig(method="llm_judge", threshold=0.8),
            backend=mock_backend,
        )
        store = _store_with_rounds([("a", "X")])
        result = await detector.check(store, 1, "topic")
        # Must NOT propagate; must return a structured failure with
        # score=0.0 and a descriptive reason.
        assert result.converged is False
        assert result.score == 0.0
        assert "non-numeric score" in result.reason


class TestParseJson:
    def test_plain_json(self):
        assert _parse_json('{"score": 0.8}') == {"score": 0.8}

    def test_markdown_fences(self):
        raw = '```json\n{"score": 0.5}\n```'
        assert _parse_json(raw) == {"score": 0.5}

    def test_preamble(self):
        raw = 'Here is my rating:\n{"score": 0.7, "reason": "mostly agree"}'
        parsed = _parse_json(raw)
        assert parsed is not None
        assert parsed["score"] == 0.7

    def test_invalid_returns_none(self):
        assert _parse_json("not json at all") is None

    def test_empty_string(self):
        assert _parse_json("") is None
