"""
WorkerTestRunner — execute a worker config directly against an LLM backend.

This module enables testing a worker config without NATS, without the actor
mesh, and without starting any long-lived processes.  It constructs the full
system prompt (including knowledge silo injection), calls the LLM backend
directly, runs the tool-use loop, parses the output, and validates it against
the worker's I/O contracts.

Usage::

    from heddle.worker.backends import build_backends_from_env
    from heddle.workshop.test_runner import WorkerTestRunner

    runner = WorkerTestRunner(build_backends_from_env())
    result = await runner.run(config_dict, {"text": "Hello world"}, tier="local")
    print(result.output, result.validation_errors, result.latency_ms)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from heddle.core.config import validate_worker_config
from heddle.core.contracts import validate_input, validate_output
from heddle.worker.runner import _extract_json, _load_tool_providers, execute_with_tools

if TYPE_CHECKING:
    from heddle.worker.backends import LLMBackend

logger = structlog.get_logger()


@dataclass
class WorkerTestResult:
    """Result of a single worker test execution."""

    output: dict[str, Any] | None = None
    raw_response: str | None = None
    validation_errors: list[str] = field(default_factory=list)
    input_validation_errors: list[str] = field(default_factory=list)
    token_usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0
    model_used: str | None = None
    error: str | None = None

    @property
    def success(self) -> bool:
        """True if the test produced valid output with no errors."""
        return (
            self.error is None
            and self.output is not None
            and not self.validation_errors
            and not self.input_validation_errors
        )


class WorkerTestRunner:
    """Execute a worker config against a payload without the actor mesh.

    This is the core of the Workshop test bench.  It replicates the
    ``LLMWorker.process()`` flow but without requiring NATS, bus subscriptions,
    or running actors.  The backend is called directly.

    Args:
        backends: Dict mapping tier name → LLMBackend instance.
            Build via ``build_backends_from_env()`` or pass mock backends.
    """

    def __init__(self, backends: dict[str, LLMBackend]) -> None:
        self.backends = backends

    async def aclose(self) -> None:
        """Close all owned LLM backends (releases httpx clients).

        Best-effort: a failure on one backend does not prevent the
        others from being closed.
        """
        for tier, backend in self.backends.items():
            try:
                await backend.aclose()
            except Exception as e:
                logger.warning(
                    "workshop.backend_close_failed",
                    tier=tier,
                    backend=type(backend).__name__,
                    error=str(e),
                )

    async def run(  # noqa: PLR0912, PLR0915
        self,
        config: dict[str, Any],
        payload: dict[str, Any],
        tier: str | None = None,
    ) -> WorkerTestResult:
        """Execute a worker config against a single payload.

        Args:
            config: Worker config dict (same format as YAML config).
            payload: Input payload matching the worker's input_schema.
            tier: Model tier to use.  Falls back to config's
                ``default_model_tier``, then ``"standard"``.

        Returns:
            TestResult with output, validation results, token usage, and timing.
        """
        result = WorkerTestResult()
        start = time.monotonic()

        try:
            # 1. Validate config
            config_errors = validate_worker_config(config)
            if config_errors:
                result.error = f"Invalid config: {'; '.join(config_errors)}"
                return result

            # 2. Validate input
            input_schema = config.get("input_schema", {})
            if input_schema:
                input_errors = validate_input(payload, input_schema)
                result.input_validation_errors = input_errors

            # 3. Build system prompt
            system_prompt = config["system_prompt"]

            # 3a. Knowledge silo injection
            silos = config.get("knowledge_silos", [])
            if silos:
                try:
                    from heddle.worker.knowledge import load_knowledge_silos

                    silo_text = load_knowledge_silos(silos)
                    if silo_text:
                        system_prompt = silo_text + "\n\n" + system_prompt
                except Exception as e:
                    logger.warning("workshop.silo_load_failed", error=str(e))

            # 3b. Legacy knowledge injection
            knowledge_sources = config.get("knowledge_sources", [])
            if knowledge_sources:
                try:
                    from heddle.worker.knowledge import load_knowledge_sources

                    knowledge_text = load_knowledge_sources(knowledge_sources)
                    if knowledge_text:
                        system_prompt = knowledge_text + "\n\n" + system_prompt
                except Exception as e:
                    logger.warning("workshop.knowledge_load_failed", error=str(e))

            # 3c. File-ref resolution
            workspace_dir = config.get("workspace_dir")
            file_ref_fields = config.get("resolve_file_refs", [])
            if workspace_dir and file_ref_fields:
                from heddle.core.workspace import WorkspaceManager

                ws = WorkspaceManager(workspace_dir)
                for f in file_ref_fields:
                    if f in payload:
                        try:
                            content = ws.read_json(payload[f])
                            payload[f"{f}_content"] = content
                        except Exception as e:
                            logger.warning("workshop.file_ref_failed", field=f, error=str(e))

            user_message = json.dumps(payload, indent=2)

            # 4. Load tool providers
            tool_providers = _load_tool_providers(silos)
            tool_defs = [p.get_definition() for p in tool_providers.values()] or None

            # 5. Resolve tier and backend
            resolved_tier = tier or config.get(
                "default_model_tier", config.get("default_tier", "standard")
            )
            backend = self.backends.get(resolved_tier)
            if not backend:
                available = list(self.backends.keys())
                result.error = (
                    f"No backend for tier '{resolved_tier}'. Available tiers: {available}"
                )
                return result

            # 6. Execute LLM call with tool-use loop
            from heddle.worker.tools import DEFAULT_TOOL_TIMEOUT_SECONDS

            max_tokens = config.get("max_output_tokens", 2000)
            tool_timeout = float(config.get("tool_timeout_seconds", DEFAULT_TOOL_TIMEOUT_SECONDS))
            llm_result = await execute_with_tools(
                backend=backend,
                system_prompt=system_prompt,
                user_message=user_message,
                tool_providers=tool_providers,
                tool_defs=tool_defs,
                max_tokens=max_tokens,
                tool_timeout_seconds=tool_timeout,
            )

            result.model_used = llm_result.get("model")
            result.token_usage = {
                "prompt_tokens": llm_result.get("prompt_tokens", 0),
                "completion_tokens": llm_result.get("completion_tokens", 0),
            }
            result.raw_response = llm_result.get("content")

            # 7. Parse output
            if result.raw_response is None:
                result.error = "LLM did not produce a text response"
                return result

            output = _extract_json(result.raw_response)

            # Remove silo_updates from output (same as LLMWorker)
            output.pop("silo_updates", None)
            result.output = output

            # 8. Validate output
            output_schema = config.get("output_schema", {})
            if output_schema:
                result.validation_errors = validate_output(output, output_schema)

        except Exception as e:
            result.error = str(e)
            logger.error("workshop.test_failed", error=str(e), exc_info=True)
        finally:
            result.latency_ms = int((time.monotonic() - start) * 1000)

        return result
