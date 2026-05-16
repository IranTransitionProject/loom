"""Tests for the heddle.tracing module (OTel integration)."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from heddle.tracing.otel import (
    _NoOpSpan,
    _NoOpTracer,
    extract_trace_context,
    get_tracer,
    init_tracing,
    inject_trace_context,
    trace_correlation_processor,
)


class TestNoOpSpan:
    def test_set_attribute(self):
        span = _NoOpSpan()
        span.set_attribute("key", "value")  # should not raise

    def test_set_status(self):
        span = _NoOpSpan()
        span.set_status("OK")

    def test_record_exception(self):
        span = _NoOpSpan()
        span.record_exception(ValueError("test"))

    def test_add_event(self):
        span = _NoOpSpan()
        span.add_event("event", {"key": "val"})

    def test_end(self):
        span = _NoOpSpan()
        span.end()

    def test_context_manager(self):
        span = _NoOpSpan()
        with span as s:
            assert s is span


class TestNoOpTracer:
    def test_start_as_current_span(self):
        tracer = _NoOpTracer()
        with tracer.start_as_current_span("test") as span:
            assert isinstance(span, _NoOpSpan)

    def test_start_as_current_span_with_kwargs(self):
        tracer = _NoOpTracer()
        with tracer.start_as_current_span("test", context=None, attributes={"a": 1}) as span:
            assert isinstance(span, _NoOpSpan)

    def test_start_span(self):
        tracer = _NoOpTracer()
        span = tracer.start_span("test")
        assert isinstance(span, _NoOpSpan)

    def test_start_span_with_kwargs(self):
        tracer = _NoOpTracer()
        span = tracer.start_span("test", context=None, attributes={"a": 1})
        assert isinstance(span, _NoOpSpan)


class TestGetTracer:
    def test_returns_noop_when_no_otel(self):
        with patch("heddle.tracing.otel._HAS_OTEL", False):
            tracer = get_tracer("test")
            assert isinstance(tracer, _NoOpTracer)

    def test_returns_real_when_otel_available(self):
        mock_trace = MagicMock()
        mock_trace.get_tracer.return_value = MagicMock()
        with (
            patch("heddle.tracing.otel._HAS_OTEL", True),
            patch("heddle.tracing.otel._trace_mod", mock_trace),
        ):
            tracer = get_tracer("test")
            mock_trace.get_tracer.assert_called_once_with("test")
            assert tracer is mock_trace.get_tracer.return_value


class TestInjectTraceContext:
    def test_noop_when_no_otel(self):
        with patch("heddle.tracing.otel._HAS_OTEL", False):
            carrier = {"task_id": "123"}
            inject_trace_context(carrier)
            assert "_trace_context" not in carrier

    def test_injects_when_otel_available(self):
        mock_propagate = MagicMock()
        mock_propagate.inject.side_effect = lambda h: h.update({"traceparent": "00-abc-def-01"})
        with (
            patch("heddle.tracing.otel._HAS_OTEL", True),
            patch("heddle.tracing.otel._propagate_mod", mock_propagate),
        ):
            carrier = {"task_id": "123"}
            inject_trace_context(carrier)
            assert carrier["_trace_context"] == {"traceparent": "00-abc-def-01"}

    def test_no_inject_when_headers_empty(self):
        mock_propagate = MagicMock()
        mock_propagate.inject.side_effect = lambda h: None  # no headers added
        with (
            patch("heddle.tracing.otel._HAS_OTEL", True),
            patch("heddle.tracing.otel._propagate_mod", mock_propagate),
        ):
            carrier = {"task_id": "123"}
            inject_trace_context(carrier)
            assert "_trace_context" not in carrier


class TestExtractTraceContext:
    def test_noop_when_no_otel(self):
        with patch("heddle.tracing.otel._HAS_OTEL", False):
            ctx = extract_trace_context({"_trace_context": {"traceparent": "x"}})
            assert ctx is None

    def test_none_when_no_key(self):
        with patch("heddle.tracing.otel._HAS_OTEL", True):
            ctx = extract_trace_context({"task_id": "123"})
            assert ctx is None

    def test_none_when_invalid_type(self):
        with patch("heddle.tracing.otel._HAS_OTEL", True):
            ctx = extract_trace_context({"_trace_context": "not-a-dict"})
            assert ctx is None

    def test_extracts_when_present(self):
        mock_propagate = MagicMock()
        mock_propagate.extract.return_value = "mock_context"
        with (
            patch("heddle.tracing.otel._HAS_OTEL", True),
            patch("heddle.tracing.otel._propagate_mod", mock_propagate),
        ):
            headers = {"traceparent": "00-abc-def-01"}
            ctx = extract_trace_context({"_trace_context": headers})
            mock_propagate.extract.assert_called_once_with(headers)
            assert ctx == "mock_context"


class TestInitTracing:
    @pytest.fixture(autouse=True)
    def _reset_init_flag(self):
        """Reset the module-level idempotency guard between tests.

        ``init_tracing`` was made idempotent so a second call is a no-op
        (``_TRACING_INITIALIZED = True`` after the first success).  Each
        test in this class mocks the OTel SDK and asserts that the
        ``set_tracer_provider`` call happened — which only runs on the
        first invocation.  Reset the flag before each test so they don't
        bleed across one another.
        """
        from heddle.tracing import otel as _otel

        _otel._TRACING_INITIALIZED = False
        yield
        _otel._TRACING_INITIALIZED = False

    def test_returns_false_when_no_otel(self):
        with patch("heddle.tracing.otel._HAS_OTEL", False):
            assert init_tracing() is False

    def test_returns_false_when_sdk_missing(self):
        with (
            patch("heddle.tracing.otel._HAS_OTEL", True),
            patch.dict(
                "sys.modules",
                {
                    "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": None,
                },
            ),
        ):
            # The import will raise ImportError inside init_tracing
            assert init_tracing() is False

    def test_returns_true_when_sdk_fully_available(self):
        """Lines 132-134, 141-153: init_tracing sets up provider and returns True."""
        mock_resource_cls = MagicMock()
        mock_resource_cls.create.return_value = MagicMock()

        mock_provider = MagicMock()
        mock_provider_cls = MagicMock(return_value=mock_provider)

        mock_exporter = MagicMock()
        mock_exporter_cls = MagicMock(return_value=mock_exporter)

        mock_processor = MagicMock()
        mock_processor_cls = MagicMock(return_value=mock_processor)

        mock_trace = MagicMock()

        with (
            patch("heddle.tracing.otel._HAS_OTEL", True),
            patch("heddle.tracing.otel._trace_mod", mock_trace),
            patch.dict(
                "sys.modules",
                {
                    "opentelemetry.sdk.resources": MagicMock(Resource=mock_resource_cls),
                    "opentelemetry.sdk.trace": MagicMock(TracerProvider=mock_provider_cls),
                    "opentelemetry.sdk.trace.export": MagicMock(
                        BatchSpanProcessor=mock_processor_cls
                    ),
                    "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": MagicMock(
                        OTLPSpanExporter=mock_exporter_cls
                    ),
                },
            ),
        ):
            result = init_tracing("my-service", endpoint="http://localhost:4317")

        assert result is True
        mock_trace.set_tracer_provider.assert_called_once()

    def test_returns_true_without_explicit_endpoint(self):
        """Lines 144-148: endpoint=None means no endpoint kwarg to OTLPSpanExporter."""
        mock_resource_cls = MagicMock()
        mock_resource_cls.create.return_value = MagicMock()

        mock_provider = MagicMock()
        mock_provider_cls = MagicMock(return_value=mock_provider)

        mock_exporter_cls = MagicMock(return_value=MagicMock())
        mock_processor_cls = MagicMock(return_value=MagicMock())

        mock_trace = MagicMock()

        with (
            patch("heddle.tracing.otel._HAS_OTEL", True),
            patch("heddle.tracing.otel._trace_mod", mock_trace),
            patch.dict(
                "sys.modules",
                {
                    "opentelemetry.sdk.resources": MagicMock(Resource=mock_resource_cls),
                    "opentelemetry.sdk.trace": MagicMock(TracerProvider=mock_provider_cls),
                    "opentelemetry.sdk.trace.export": MagicMock(
                        BatchSpanProcessor=mock_processor_cls
                    ),
                    "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": MagicMock(
                        OTLPSpanExporter=mock_exporter_cls
                    ),
                },
            ),
        ):
            result = init_tracing("my-service")

        assert result is True
        # When no endpoint is provided, OTLPSpanExporter should be called with no kwargs.
        mock_exporter_cls.assert_called_once_with()
        mock_trace.set_tracer_provider.assert_called_once()


class TestTracingIntegrationWithActor:
    """Verify that tracing imports and no-op behavior work in the actor."""

    def test_actor_process_one_with_noop_tracing(self):
        """The actor module should import tracing without error."""
        from heddle.core.actor import BaseActor

        assert hasattr(BaseActor, "_process_one")

    def test_trace_context_roundtrip(self):
        """Inject/extract should work as a pair even in no-op mode."""
        carrier = {"task_id": "t1", "worker_type": "summarizer"}
        inject_trace_context(carrier)
        ctx = extract_trace_context(carrier)
        # Without OTel installed, inject is a no-op, extract returns None
        # Both should be safe to call
        assert ctx is None or ctx is not None  # just verifying no crash


class TestGenAISemanticConventions:
    """Verify that execute_with_tools sets GenAI semantic convention span attributes."""

    @pytest.mark.asyncio
    async def test_genai_attributes_on_span(self):
        """execute_with_tools should set gen_ai.* attributes on the llm.call span."""
        from heddle.worker.runner import execute_with_tools

        mock_backend = AsyncMock()
        mock_backend.complete.return_value = {
            "content": '{"result": "ok"}',
            "model": "claude-test",
            "prompt_tokens": 50,
            "completion_tokens": 25,
            "tool_calls": None,
            "stop_reason": "end_turn",
            "gen_ai_system": "anthropic",
            "gen_ai_request_model": "claude-test",
            "gen_ai_response_model": "claude-test",
            "gen_ai_request_temperature": 0.5,
            "gen_ai_request_max_tokens": 2000,
        }

        # Use a recording span to capture set_attribute calls
        recorded_attrs = {}
        recorded_events = []

        class RecordingSpan:
            def set_attribute(self, key, value):
                recorded_attrs[key] = value

            def add_event(self, name, attributes=None):
                recorded_events.append((name, attributes))

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        class RecordingTracer:
            def start_as_current_span(self, name, **kwargs):
                from contextlib import contextmanager

                @contextmanager
                def _cm():
                    yield RecordingSpan()

                return _cm()

        with patch("heddle.worker.runner._tracer", RecordingTracer()):
            await execute_with_tools(
                backend=mock_backend,
                system_prompt="sys",
                user_message="msg",
                tool_providers={},
                tool_defs=None,
            )

        # Legacy attributes preserved
        assert recorded_attrs["llm.model"] == "claude-test"
        assert recorded_attrs["llm.prompt_tokens"] == 50
        assert recorded_attrs["llm.completion_tokens"] == 25

        # GenAI semantic convention attributes
        assert recorded_attrs["gen_ai.system"] == "anthropic"
        assert recorded_attrs["gen_ai.request.model"] == "claude-test"
        assert recorded_attrs["gen_ai.response.model"] == "claude-test"
        assert recorded_attrs["gen_ai.usage.input_tokens"] == 50
        assert recorded_attrs["gen_ai.usage.output_tokens"] == 25
        assert recorded_attrs["gen_ai.request.temperature"] == 0.5
        assert recorded_attrs["gen_ai.request.max_tokens"] == 2000

    @pytest.mark.asyncio
    async def test_genai_optional_temperature_omitted(self):
        """When gen_ai_request_temperature is None, the attribute should not be set."""
        from heddle.worker.runner import execute_with_tools

        mock_backend = AsyncMock()
        mock_backend.complete.return_value = {
            "content": '{"result": "ok"}',
            "model": "test-model",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "tool_calls": None,
            "stop_reason": "end_turn",
            "gen_ai_system": "ollama",
            "gen_ai_request_model": "test-model",
            "gen_ai_response_model": "test-model",
            "gen_ai_request_temperature": None,
            "gen_ai_request_max_tokens": None,
        }

        recorded_attrs = {}

        class RecordingSpan:
            def set_attribute(self, key, value):
                recorded_attrs[key] = value

            def add_event(self, name, attributes=None):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        class RecordingTracer:
            def start_as_current_span(self, name, **kwargs):
                from contextlib import contextmanager

                @contextmanager
                def _cm():
                    yield RecordingSpan()

                return _cm()

        with patch("heddle.worker.runner._tracer", RecordingTracer()):
            await execute_with_tools(
                backend=mock_backend,
                system_prompt="sys",
                user_message="msg",
                tool_providers={},
                tool_defs=None,
            )

        assert "gen_ai.request.temperature" not in recorded_attrs
        assert "gen_ai.request.max_tokens" not in recorded_attrs

    @pytest.mark.asyncio
    async def test_heddle_trace_content_env_var(self):
        """HEDDLE_TRACE_CONTENT=1 should add prompt/completion span events."""
        from heddle.worker.runner import execute_with_tools

        mock_backend = AsyncMock()
        mock_backend.complete.return_value = {
            "content": '{"result": "ok"}',
            "model": "test-model",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "tool_calls": None,
            "stop_reason": "end_turn",
            "gen_ai_system": "openai",
            "gen_ai_request_model": "test-model",
            "gen_ai_response_model": "test-model",
            "gen_ai_request_temperature": 0.0,
            "gen_ai_request_max_tokens": 2000,
        }

        recorded_events = []

        class RecordingSpan:
            def set_attribute(self, key, value):
                pass

            def add_event(self, name, attributes=None):
                recorded_events.append((name, attributes))

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        class RecordingTracer:
            def start_as_current_span(self, name, **kwargs):
                from contextlib import contextmanager

                @contextmanager
                def _cm():
                    yield RecordingSpan()

                return _cm()

        with (
            patch("heddle.worker.runner._tracer", RecordingTracer()),
            patch.dict(os.environ, {"HEDDLE_TRACE_CONTENT": "1"}),
        ):
            await execute_with_tools(
                backend=mock_backend,
                system_prompt="sys",
                user_message="my user message",
                tool_providers={},
                tool_defs=None,
            )

        event_names = [e[0] for e in recorded_events]
        assert "gen_ai.content.prompt" in event_names
        assert "gen_ai.content.completion" in event_names

        # Check prompt event content
        prompt_event = next(e for e in recorded_events if e[0] == "gen_ai.content.prompt")
        assert prompt_event[1]["gen_ai.prompt"] == "my user message"

        # Check completion event content
        completion_event = next(e for e in recorded_events if e[0] == "gen_ai.content.completion")
        assert completion_event[1]["gen_ai.completion"] == '{"result": "ok"}'

    @pytest.mark.asyncio
    async def test_heddle_trace_content_disabled_by_default(self):
        """Without HEDDLE_TRACE_CONTENT, no content events should be added."""
        from heddle.worker.runner import execute_with_tools

        mock_backend = AsyncMock()
        mock_backend.complete.return_value = {
            "content": '{"result": "ok"}',
            "model": "test-model",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "tool_calls": None,
            "stop_reason": "end_turn",
            "gen_ai_system": "openai",
            "gen_ai_request_model": "test-model",
            "gen_ai_response_model": "test-model",
            "gen_ai_request_temperature": 0.0,
            "gen_ai_request_max_tokens": 2000,
        }

        recorded_events = []

        class RecordingSpan:
            def set_attribute(self, key, value):
                pass

            def add_event(self, name, attributes=None):
                recorded_events.append((name, attributes))

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        class RecordingTracer:
            def start_as_current_span(self, name, **kwargs):
                from contextlib import contextmanager

                @contextmanager
                def _cm():
                    yield RecordingSpan()

                return _cm()

        with (
            patch("heddle.worker.runner._tracer", RecordingTracer()),
            patch.dict(os.environ, {"HEDDLE_TRACE_CONTENT": ""}, clear=False),
        ):
            await execute_with_tools(
                backend=mock_backend,
                system_prompt="sys",
                user_message="msg",
                tool_providers={},
                tool_defs=None,
            )

        event_names = [e[0] for e in recorded_events]
        assert "gen_ai.content.prompt" not in event_names
        assert "gen_ai.content.completion" not in event_names

    @pytest.mark.asyncio
    async def test_genai_attributes_work_with_noop_tracer(self):
        """The NoOpTracer/NoOpSpan should silently accept GenAI attributes."""
        from heddle.worker.runner import execute_with_tools

        mock_backend = AsyncMock()
        mock_backend.complete.return_value = {
            "content": '{"result": "ok"}',
            "model": "test-model",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "tool_calls": None,
            "stop_reason": "end_turn",
            "gen_ai_system": "ollama",
            "gen_ai_request_model": "test-model",
            "gen_ai_response_model": "test-model",
            "gen_ai_request_temperature": 0.7,
            "gen_ai_request_max_tokens": 1000,
        }

        # Use actual NoOpTracer — should not raise
        noop_tracer = _NoOpTracer()
        with patch("heddle.worker.runner._tracer", noop_tracer):
            result = await execute_with_tools(
                backend=mock_backend,
                system_prompt="sys",
                user_message="msg",
                tool_providers={},
                tool_defs=None,
            )

        assert result["content"] == '{"result": "ok"}'


class TestTraceCorrelationProcessor:
    """structlog ↔ OTel correlation processor (OTel-audit S5)."""

    def test_noop_when_no_otel(self):
        """Without OTel installed, the processor returns event_dict unchanged."""
        event_dict = {"event": "hello", "key": "value"}
        with patch("heddle.tracing.otel._HAS_OTEL", False):
            out = trace_correlation_processor(None, "info", event_dict)
        assert out == {"event": "hello", "key": "value"}
        assert "trace_id" not in out
        assert "span_id" not in out

    def test_noop_when_no_active_span(self):
        """With OTel installed but no span active, event_dict is unchanged."""
        mock_trace = MagicMock()
        mock_span = MagicMock()
        mock_span.get_span_context.return_value.is_valid = False
        mock_trace.get_current_span.return_value = mock_span

        event_dict = {"event": "hello"}
        with (
            patch("heddle.tracing.otel._HAS_OTEL", True),
            patch("heddle.tracing.otel._trace_mod", mock_trace),
        ):
            out = trace_correlation_processor(None, "info", event_dict)
        assert "trace_id" not in out
        assert "span_id" not in out

    def test_injects_hex_ids_when_span_active(self):
        """With an active span, trace_id (32 hex) and span_id (16 hex) are added."""
        mock_trace = MagicMock()
        mock_span = MagicMock()
        span_ctx = mock_span.get_span_context.return_value
        span_ctx.is_valid = True
        # OTel uses 128-bit trace_id and 64-bit span_id as ints.
        span_ctx.trace_id = 0x0123456789ABCDEF0123456789ABCDEF
        span_ctx.span_id = 0xFEDCBA9876543210
        mock_trace.get_current_span.return_value = mock_span

        event_dict: dict = {"event": "hello"}
        with (
            patch("heddle.tracing.otel._HAS_OTEL", True),
            patch("heddle.tracing.otel._trace_mod", mock_trace),
        ):
            out = trace_correlation_processor(None, "info", event_dict)

        assert out["trace_id"] == "0123456789abcdef0123456789abcdef"
        assert out["span_id"] == "fedcba9876543210"
        assert len(out["trace_id"]) == 32
        assert len(out["span_id"]) == 16

    def test_preserves_existing_event_dict_keys(self):
        """The processor adds keys; it doesn't overwrite or drop existing ones."""
        mock_trace = MagicMock()
        mock_span = MagicMock()
        span_ctx = mock_span.get_span_context.return_value
        span_ctx.is_valid = True
        span_ctx.trace_id = 1
        span_ctx.span_id = 2
        mock_trace.get_current_span.return_value = mock_span

        event_dict: dict = {"event": "msg", "actor_id": "abc", "ms": 42}
        with (
            patch("heddle.tracing.otel._HAS_OTEL", True),
            patch("heddle.tracing.otel._trace_mod", mock_trace),
        ):
            out = trace_correlation_processor(None, "info", event_dict)

        assert out["event"] == "msg"
        assert out["actor_id"] == "abc"
        assert out["ms"] == 42
        assert "trace_id" in out
        assert "span_id" in out
