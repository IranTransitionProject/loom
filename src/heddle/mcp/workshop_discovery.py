"""
Workshop tool discovery — generate MCP tool definitions for Workshop operations.

Exposes Workshop capabilities (worker CRUD, test bench, eval, impact analysis,
dead-letter inspection) as MCP tools under the ``workshop.*`` namespace.

Unlike worker/pipeline/query tools which dispatch through NATS, workshop tools
call Workshop components directly — no NATS required.
"""

from __future__ import annotations

from typing import Any

from heddle.mcp.discovery import make_tool


def discover_workshop_tools(
    workshop_config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Generate MCP tool definitions for Workshop operations.

    Args:
        workshop_config: The ``tools.workshop`` dict from the MCP gateway config.
            Supported keys: ``configs_dir``, ``apps_dir``, ``enable`` (list of
            tool groups to enable, default all).

    Returns:
        List of tool definition dicts with ``_loom`` metadata.
    """
    # Dead-letter tools are excluded by default — opt in via
    # ``enable: [..., "deadletter"]``.  When enabled, the gateway subscribes
    # the consumer to ``heddle.tasks.dead_letter`` on the live bus.
    # Workshop-only (no-bus) deployments cannot list or replay; those tool
    # calls fail with "DeadLetterConsumer not configured".
    enabled = set(
        workshop_config.get(
            "enable",
            [
                "worker",
                "test",
                "eval",
                "impact",
            ],
        )
    )

    tools: list[dict[str, Any]] = []

    if "worker" in enabled:
        tools.extend(_worker_tools())
    if "test" in enabled:
        tools.extend(_test_tools())
    if "eval" in enabled:
        tools.extend(_eval_tools())
    if "impact" in enabled:
        tools.extend(_impact_tools())
    if "deadletter" in enabled:
        tools.extend(_deadletter_tools())

    return tools


# ---------------------------------------------------------------------------
# Worker config tools
# ---------------------------------------------------------------------------


def _worker_tools() -> list[dict[str, Any]]:
    tools = []

    tool = make_tool(
        "workshop.worker.list",
        "List all worker configs with name, tier, and app source.",
        {"type": "object", "properties": {}},
    )
    tool["_heddle"] = {"kind": "workshop", "action": "worker.list", "read_only": True}
    tools.append(tool)

    tool = make_tool(
        "workshop.worker.get",
        "Get a worker config by name, including full YAML and version history.",
        {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string", "description": "Worker config name"},
            },
        },
    )
    tool["_heddle"] = {"kind": "workshop", "action": "worker.get", "read_only": True}
    tools.append(tool)

    tool = make_tool(
        "workshop.worker.update",
        "Create or update a worker config. Validates before saving.",
        {
            "type": "object",
            "required": ["name", "config_yaml"],
            "properties": {
                "name": {"type": "string", "description": "Worker config name"},
                "config_yaml": {
                    "type": "string",
                    "description": "Full YAML content for the worker config",
                },
                "description": {
                    "type": "string",
                    "description": "Version description (for change tracking)",
                },
            },
        },
    )
    tool["_heddle"] = {"kind": "workshop", "action": "worker.update"}
    tools.append(tool)

    return tools


# ---------------------------------------------------------------------------
# Test bench tools
# ---------------------------------------------------------------------------


def _test_tools() -> list[dict[str, Any]]:
    tool = make_tool(
        "workshop.worker.test",
        "Run a worker against a test payload. Returns structured output, "
        "validation results, token usage, and latency.",
        {
            "type": "object",
            "required": ["name", "payload"],
            "properties": {
                "name": {"type": "string", "description": "Worker config name"},
                "payload": {
                    "type": "object",
                    "description": "Input payload matching the worker's input_schema",
                },
                "tier": {
                    "type": "string",
                    "description": "Model tier override (local, standard, frontier)",
                },
            },
        },
    )
    tool["_heddle"] = {"kind": "workshop", "action": "worker.test", "read_only": True}
    return [tool]


# ---------------------------------------------------------------------------
# Eval tools
# ---------------------------------------------------------------------------


def _eval_tools() -> list[dict[str, Any]]:
    tools = []

    tool = make_tool(
        "workshop.eval.run",
        "Run an eval suite against a worker. Returns run_id and summary scores.",
        {
            "type": "object",
            "required": ["name", "test_suite"],
            "properties": {
                "name": {"type": "string", "description": "Worker config name"},
                "test_suite": {
                    "type": "array",
                    "description": (
                        "List of test cases, each with 'name', 'input', and 'expected_output'"
                    ),
                    "items": {
                        "type": "object",
                        "required": ["name", "input", "expected_output"],
                        "properties": {
                            "name": {"type": "string"},
                            "input": {"type": "object"},
                            "expected_output": {"type": "object"},
                        },
                    },
                },
                "scoring": {
                    "type": "string",
                    "description": (
                        "Scoring method: field_match, exact_match, "
                        "or llm_judge (default: field_match)"
                    ),
                },
                "tier": {
                    "type": "string",
                    "description": "Model tier override (local, standard, frontier)",
                },
            },
        },
    )
    tool["_heddle"] = {
        "kind": "workshop",
        "action": "eval.run",
        "long_running": True,
    }
    tools.append(tool)

    tool = make_tool(
        "workshop.eval.compare",
        "Compare an eval run against the golden baseline for a worker. "
        "Returns per-case regression/improvement analysis.",
        {
            "type": "object",
            "required": ["name", "run_id"],
            "properties": {
                "name": {"type": "string", "description": "Worker config name"},
                "run_id": {"type": "string", "description": "Eval run ID to compare"},
            },
        },
    )
    tool["_heddle"] = {"kind": "workshop", "action": "eval.compare", "read_only": True}
    tools.append(tool)

    return tools


# ---------------------------------------------------------------------------
# Impact analysis tools
# ---------------------------------------------------------------------------


def _impact_tools() -> list[dict[str, Any]]:
    tool = make_tool(
        "workshop.impact.analyze",
        "Analyze which pipelines and downstream stages are affected by "
        "changing a worker config. Returns risk assessment.",
        {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Worker config name to analyze impact for",
                },
            },
        },
    )
    tool["_heddle"] = {"kind": "workshop", "action": "impact.analyze", "read_only": True}
    return [tool]


# ---------------------------------------------------------------------------
# Dead-letter tools
# ---------------------------------------------------------------------------


def _deadletter_tools() -> list[dict[str, Any]]:
    tools = []

    tool = make_tool(
        "workshop.deadletter.list",
        "List dead-letter entries (failed/unroutable tasks) with reason and timestamps.",
        {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max entries to return (default 50)",
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination offset (default 0)",
                },
            },
        },
    )
    tool["_heddle"] = {"kind": "workshop", "action": "deadletter.list", "read_only": True}
    tools.append(tool)

    tool = make_tool(
        "workshop.deadletter.replay",
        "Re-publish a dead-letter task to the router for retry. "
        "This is a destructive operation — the task will be re-dispatched.",
        {
            "type": "object",
            "required": ["entry_id"],
            "properties": {
                "entry_id": {
                    "type": "string",
                    "description": "Dead-letter entry ID to replay",
                },
            },
        },
    )
    tool["_heddle"] = {
        "kind": "workshop",
        "action": "deadletter.replay",
        "destructive": True,
    }
    tools.append(tool)

    return tools


# ---------------------------------------------------------------------------
# Session management tools
# ---------------------------------------------------------------------------


def discover_session_tools(
    session_config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Generate MCP tool definitions for session management operations.

    Args:
        session_config: The ``tools.session`` dict from the MCP gateway config.
            Supported keys: ``framework_dir``, ``workspace_dir``, ``baft_dir``,
            ``nats_url``, ``ollama_url``, ``enable`` (list of actions).

    Returns:
        List of tool definition dicts with ``_loom`` metadata.
    """
    enabled = set(
        session_config.get(
            "enable",
            ["start", "end", "status", "sync_check", "sync"],
        )
    )

    tools: list[dict[str, Any]] = []

    if "start" in enabled:
        tool = make_tool(
            "session.start",
            "Start an analytical session: pull framework, import DuckDB, "
            "check services, register session.",
            {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID (auto-generated if omitted)",
                    },
                },
            },
        )
        tool["_heddle"] = {"kind": "session", "action": "start"}
        tools.append(tool)

    if "end" in enabled:
        tool = make_tool(
            "session.end",
            "End an analytical session: commit framework changes, push, unregister session.",
            {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session to end (most recent if omitted)",
                    },
                    "message": {
                        "type": "string",
                        "description": "Commit message describing session work",
                    },
                },
            },
        )
        tool["_heddle"] = {"kind": "session", "action": "end"}
        tools.append(tool)

    if "status" in enabled:
        tool = make_tool(
            "session.status",
            "Show active sessions, framework git status, and service health.",
            {"type": "object", "properties": {}},
        )
        tool["_heddle"] = {
            "kind": "session",
            "action": "status",
            "read_only": True,
        }
        tools.append(tool)

    if "sync_check" in enabled:
        tool = make_tool(
            "session.sync_check",
            "Check if the framework remote has new commits. Reports "
            "ahead/behind/diverged/current status.",
            {"type": "object", "properties": {}},
        )
        tool["_heddle"] = {
            "kind": "session",
            "action": "sync_check",
            "read_only": True,
        }
        tools.append(tool)

    if "sync" in enabled:
        tool = make_tool(
            "session.sync",
            "Pull framework updates (fast-forward) and run incremental DuckDB import.",
            {"type": "object", "properties": {}},
        )
        tool["_heddle"] = {"kind": "session", "action": "sync"}
        tools.append(tool)

    return tools
