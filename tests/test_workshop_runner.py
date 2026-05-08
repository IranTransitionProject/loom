"""Tests for WorkerTestRunner (workshop/test_runner.py)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from heddle.worker.backends import LLMBackend
from heddle.workshop.test_runner import WorkerTestResult, WorkerTestRunner

# ---------------------------------------------------------------------------
# Mock backends
# ---------------------------------------------------------------------------


class MockBackend(LLMBackend):
    """Returns configurable JSON responses."""

    def __init__(self, output: dict | None = None, content: str | None = None):
        self._output = output or {"summary": "test", "key_points": ["a"]}
        self._content = content  # override raw content if needed

    async def complete(
        self, system_prompt, user_message, max_tokens=2000, temperature=0.0, **kwargs
    ):
        return {
            "content": self._content or json.dumps(self._output),
            "model": "mock-model",
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "tool_calls": None,
            "stop_reason": "end_turn",
        }


class ErrorBackend(LLMBackend):
    """Raises on complete()."""

    async def complete(
        self, system_prompt, user_message, max_tokens=2000, temperature=0.0, **kwargs
    ):
        raise RuntimeError("Backend unavailable")


class NoContentBackend(LLMBackend):
    """Returns None content."""

    async def complete(
        self, system_prompt, user_message, max_tokens=2000, temperature=0.0, **kwargs
    ):
        return {
            "content": None,
            "model": "mock",
            "prompt_tokens": 10,
            "completion_tokens": 0,
            "tool_calls": None,
            "stop_reason": "end_turn",
        }


# ---------------------------------------------------------------------------
# Minimal worker config
# ---------------------------------------------------------------------------

BASIC_CONFIG = {
    "name": "test_worker",
    "system_prompt": "You are a test worker. Return JSON.",
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
# Tests
# ---------------------------------------------------------------------------


class TestWorkerTestRunner:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        """Valid config + valid payload → successful TestResult."""
        backend = MockBackend({"summary": "done"})
        runner = WorkerTestRunner({"local": backend})

        result = await runner.run(BASIC_CONFIG, {"text": "Hello world"})

        assert result.success
        assert result.output == {"summary": "done"}
        assert result.model_used == "mock-model"
        assert result.token_usage["prompt_tokens"] == 100
        assert result.latency_ms >= 0
        assert result.error is None
        assert result.validation_errors == []

    @pytest.mark.asyncio
    async def test_input_validation_failure(self):
        """Missing required input field → input_validation_errors populated."""
        backend = MockBackend({"summary": "done"})
        runner = WorkerTestRunner({"local": backend})

        result = await runner.run(BASIC_CONFIG, {"wrong_field": "value"})

        assert len(result.input_validation_errors) > 0
        assert any("text" in e for e in result.input_validation_errors)
        # Output may still be produced (input validation is advisory)
        assert result.output is not None

    @pytest.mark.asyncio
    async def test_output_validation_failure(self):
        """LLM returns output that doesn't match output_schema."""
        backend = MockBackend({"wrong_key": "value"})
        runner = WorkerTestRunner({"local": backend})

        result = await runner.run(BASIC_CONFIG, {"text": "test"})

        assert not result.success
        assert result.output == {"wrong_key": "value"}
        assert len(result.validation_errors) > 0
        assert any("summary" in e for e in result.validation_errors)

    @pytest.mark.asyncio
    async def test_invalid_config(self):
        """Invalid config → error without calling backend."""
        backend = MockBackend()
        runner = WorkerTestRunner({"local": backend})

        bad_config = {"name": "test"}  # Missing system_prompt
        result = await runner.run(bad_config, {"text": "test"})

        assert not result.success
        assert "Invalid config" in result.error

    @pytest.mark.asyncio
    async def test_missing_backend_for_tier(self):
        """No backend for requested tier → clear error message."""
        backend = MockBackend()
        runner = WorkerTestRunner({"local": backend})

        result = await runner.run(BASIC_CONFIG, {"text": "test"}, tier="frontier")

        assert not result.success
        assert "No backend for tier" in result.error
        assert "frontier" in result.error

    @pytest.mark.asyncio
    async def test_tier_resolution_from_config(self):
        """Tier falls back to config's default_model_tier."""
        backend = MockBackend({"summary": "ok"})
        runner = WorkerTestRunner({"local": backend})

        # No tier arg → uses config's default_model_tier="local"
        result = await runner.run(BASIC_CONFIG, {"text": "test"})
        assert result.success

    @pytest.mark.asyncio
    async def test_explicit_tier_overrides_config(self):
        """Explicit tier parameter overrides config default."""
        backend = MockBackend({"summary": "ok"})
        runner = WorkerTestRunner({"standard": backend})

        result = await runner.run(BASIC_CONFIG, {"text": "test"}, tier="standard")
        assert result.success

    @pytest.mark.asyncio
    async def test_backend_error(self):
        """Backend exception → error captured in TestResult."""
        runner = WorkerTestRunner({"local": ErrorBackend()})

        result = await runner.run(BASIC_CONFIG, {"text": "test"})

        assert not result.success
        assert "Backend unavailable" in result.error

    @pytest.mark.asyncio
    async def test_no_content_response(self):
        """LLM returns None content → error message."""
        runner = WorkerTestRunner({"local": NoContentBackend()})

        result = await runner.run(BASIC_CONFIG, {"text": "test"})

        assert not result.success
        assert "text response" in result.error.lower()

    @pytest.mark.asyncio
    async def test_json_extraction_with_fences(self):
        """LLM wraps output in markdown fences → still extracted."""
        backend = MockBackend(content='```json\n{"summary": "fenced"}\n```')
        runner = WorkerTestRunner({"local": backend})

        result = await runner.run(BASIC_CONFIG, {"text": "test"})

        assert result.success
        assert result.output == {"summary": "fenced"}

    @pytest.mark.asyncio
    async def test_no_input_schema(self):
        """Config without input_schema → no input validation."""
        config = {
            "name": "flexible",
            "system_prompt": "Return JSON.",
            "default_model_tier": "local",
        }
        backend = MockBackend({"result": "ok"})
        runner = WorkerTestRunner({"local": backend})

        result = await runner.run(config, {"anything": "goes"})

        assert result.input_validation_errors == []
        assert result.output == {"result": "ok"}

    @pytest.mark.asyncio
    async def test_no_output_schema(self):
        """Config without output_schema → no output validation."""
        config = {
            "name": "flexible",
            "system_prompt": "Return JSON.",
            "default_model_tier": "local",
        }
        backend = MockBackend({"any": "output"})
        runner = WorkerTestRunner({"local": backend})

        result = await runner.run(config, {"text": "test"})

        assert result.success
        assert result.validation_errors == []

    @pytest.mark.asyncio
    async def test_raw_response_captured(self):
        """raw_response contains the LLM's original text."""
        backend = MockBackend({"summary": "test"})
        runner = WorkerTestRunner({"local": backend})

        result = await runner.run(BASIC_CONFIG, {"text": "test"})

        assert result.raw_response == '{"summary": "test"}'

    @pytest.mark.asyncio
    async def test_latency_is_positive(self):
        """latency_ms is always set, even on error."""
        runner = WorkerTestRunner({"local": ErrorBackend()})
        result = await runner.run(BASIC_CONFIG, {"text": "test"})
        assert result.latency_ms >= 0


class TestWorkerTestRunnerAclose:
    """WorkerTestRunner.aclose() must close every owned backend."""

    @pytest.mark.asyncio
    async def test_aclose_calls_aclose_on_all_backends(self):
        class _Spy(LLMBackend):
            def __init__(self):
                self.aclose_called = 0

            async def complete(self, *a, **kw):  # pragma: no cover
                return {
                    "content": "",
                    "model": "x",
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                }

            async def aclose(self):
                self.aclose_called += 1

        local, standard = _Spy(), _Spy()
        runner = WorkerTestRunner({"local": local, "standard": standard})

        await runner.aclose()

        assert local.aclose_called == 1
        assert standard.aclose_called == 1

    @pytest.mark.asyncio
    async def test_aclose_continues_when_one_backend_raises(self):
        """A failure on one backend must not prevent the others from closing.

        This matters during workshop shutdown: a stuck httpx client on
        one provider should not strand the others.
        """

        class _Spy(LLMBackend):
            def __init__(self, fail=False):
                self.aclose_called = 0
                self._fail = fail

            async def complete(self, *a, **kw):  # pragma: no cover
                return {
                    "content": "",
                    "model": "x",
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                }

            async def aclose(self):
                self.aclose_called += 1
                if self._fail:
                    raise RuntimeError("flaky aclose")

        flaky = _Spy(fail=True)
        healthy = _Spy()
        runner = WorkerTestRunner({"local": flaky, "standard": healthy})

        # Must not raise.
        await runner.aclose()

        assert flaky.aclose_called == 1
        assert healthy.aclose_called == 1


class TestWorkerTestResult:
    def test_success_when_all_good(self):
        r = WorkerTestResult(output={"k": "v"}, error=None)
        assert r.success

    def test_failure_when_error(self):
        r = WorkerTestResult(output={"k": "v"}, error="boom")
        assert not r.success

    def test_failure_when_no_output(self):
        r = WorkerTestResult(output=None)
        assert not r.success

    def test_failure_when_validation_errors(self):
        r = WorkerTestResult(output={"k": "v"}, validation_errors=["missing field"])
        assert not r.success

    def test_failure_when_input_validation_errors(self):
        r = WorkerTestResult(output={"k": "v"}, input_validation_errors=["bad input"])
        assert not r.success


# ---------------------------------------------------------------------------
# Knowledge silo / knowledge sources injection (lines 117-136)
# ---------------------------------------------------------------------------


class TestKnowledgeInjection:
    @pytest.mark.asyncio
    async def test_knowledge_silo_injection(self):
        """Knowledge silos are loaded and prepended to system_prompt."""
        config = {
            **BASIC_CONFIG,
            "knowledge_silos": [{"name": "test_silo", "path": "/tmp/silo", "type": "folder"}],
        }
        backend = MockBackend({"summary": "silo_test"})
        runner = WorkerTestRunner({"local": backend})

        with patch(
            "heddle.worker.knowledge.load_knowledge_silos",
            return_value="SILO CONTEXT",
        ):
            result = await runner.run(config, {"text": "test"})

        assert result.success
        assert result.output == {"summary": "silo_test"}

    @pytest.mark.asyncio
    async def test_knowledge_silo_empty_text_not_prepended(self):
        """When load_knowledge_silos returns empty string, prompt is unchanged."""
        config = {
            **BASIC_CONFIG,
            "knowledge_silos": [{"name": "test_silo", "path": "/tmp/silo", "type": "folder"}],
        }
        backend = MockBackend({"summary": "ok"})
        runner = WorkerTestRunner({"local": backend})

        with patch(
            "heddle.worker.knowledge.load_knowledge_silos",
            return_value="",
        ):
            result = await runner.run(config, {"text": "test"})

        assert result.success

    @pytest.mark.asyncio
    async def test_knowledge_silo_load_failure_continues(self):
        """Silo load failure is warned but execution continues."""
        config = {
            **BASIC_CONFIG,
            "knowledge_silos": [{"name": "test_silo", "path": "/tmp/bad_silo", "type": "folder"}],
        }
        backend = MockBackend({"summary": "fallback"})
        runner = WorkerTestRunner({"local": backend})

        with patch(
            "heddle.worker.knowledge.load_knowledge_silos",
            side_effect=Exception("silo read error"),
        ):
            result = await runner.run(config, {"text": "test"})

        assert result.success
        assert result.output == {"summary": "fallback"}

    @pytest.mark.asyncio
    async def test_legacy_knowledge_sources_injection(self):
        """Legacy knowledge_sources are loaded and prepended to system_prompt."""
        config = {
            **BASIC_CONFIG,
            "knowledge_sources": [{"path": "/tmp/knowledge.txt"}],
        }
        backend = MockBackend({"summary": "knowledge_test"})
        runner = WorkerTestRunner({"local": backend})

        with patch(
            "heddle.worker.knowledge.load_knowledge_sources",
            return_value="KNOWLEDGE CONTEXT",
        ):
            result = await runner.run(config, {"text": "test"})

        assert result.success

    @pytest.mark.asyncio
    async def test_legacy_knowledge_sources_empty_not_prepended(self):
        """When load_knowledge_sources returns empty string, prompt is unchanged."""
        config = {
            **BASIC_CONFIG,
            "knowledge_sources": [{"path": "/tmp/knowledge.txt"}],
        }
        backend = MockBackend({"summary": "ok"})
        runner = WorkerTestRunner({"local": backend})

        with patch(
            "heddle.worker.knowledge.load_knowledge_sources",
            return_value="",
        ):
            result = await runner.run(config, {"text": "test"})

        assert result.success

    @pytest.mark.asyncio
    async def test_legacy_knowledge_sources_failure_continues(self):
        """Knowledge sources load failure is warned but execution continues."""
        config = {
            **BASIC_CONFIG,
            "knowledge_sources": [{"path": "/tmp/bad.txt"}],
        }
        backend = MockBackend({"summary": "fallback"})
        runner = WorkerTestRunner({"local": backend})

        with patch(
            "heddle.worker.knowledge.load_knowledge_sources",
            side_effect=Exception("file not found"),
        ):
            result = await runner.run(config, {"text": "test"})

        assert result.success
        assert result.output == {"summary": "fallback"}


# ---------------------------------------------------------------------------
# File-ref resolution (lines 142-151)
# ---------------------------------------------------------------------------


class TestFileRefResolution:
    @pytest.mark.asyncio
    async def test_file_ref_resolved_and_added_to_payload(self):
        """File ref fields are resolved via WorkspaceManager and added to payload."""
        config = {
            **BASIC_CONFIG,
            "workspace_dir": "/tmp/workspace",
            "resolve_file_refs": ["doc_ref"],
        }
        backend = MockBackend({"summary": "resolved"})
        runner = WorkerTestRunner({"local": backend})

        mock_ws = MagicMock()
        mock_ws.read_json.return_value = {"key": "value"}

        with patch(
            "heddle.core.workspace.WorkspaceManager",
            return_value=mock_ws,
        ):
            result = await runner.run(config, {"text": "test", "doc_ref": "file.json"})

        assert result.success
        mock_ws.read_json.assert_called_once_with("file.json")

    @pytest.mark.asyncio
    async def test_file_ref_missing_field_skipped(self):
        """File ref fields not present in payload are silently skipped."""
        config = {
            **BASIC_CONFIG,
            "workspace_dir": "/tmp/workspace",
            "resolve_file_refs": ["doc_ref"],
        }
        backend = MockBackend({"summary": "ok"})
        runner = WorkerTestRunner({"local": backend})

        mock_ws = MagicMock()

        with patch(
            "heddle.core.workspace.WorkspaceManager",
            return_value=mock_ws,
        ):
            # payload does NOT contain "doc_ref"
            result = await runner.run(config, {"text": "test"})

        assert result.success
        mock_ws.read_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_file_ref_read_failure_continues(self):
        """File ref resolution failure is warned but execution continues."""
        config = {
            **BASIC_CONFIG,
            "workspace_dir": "/tmp/workspace",
            "resolve_file_refs": ["doc_ref"],
        }
        backend = MockBackend({"summary": "fallback"})
        runner = WorkerTestRunner({"local": backend})

        mock_ws = MagicMock()
        mock_ws.read_json.side_effect = Exception("file not found")

        with patch(
            "heddle.core.workspace.WorkspaceManager",
            return_value=mock_ws,
        ):
            result = await runner.run(config, {"text": "test", "doc_ref": "missing.json"})

        assert result.success
        assert result.output == {"summary": "fallback"}
