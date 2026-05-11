"""
Tool discovery — introspect HEDDLE configs and generate MCP tool definitions.

Reads worker YAML configs, pipeline configs, and query backend classes to
produce MCP ``Tool`` objects with correct ``inputSchema`` definitions.
This is the core of the zero-code MCP exposure: HEDDLE configs already
contain names, descriptions, and JSON Schema contracts — this module
reshapes them into the MCP format.

Three discovery functions correspond to the three tool sources:

- ``discover_worker_tools``   — one tool per worker config
- ``discover_pipeline_tools`` — one tool per pipeline config
- ``discover_query_tools``    — one tool per query action
"""

from __future__ import annotations

import importlib
from typing import Any

import structlog

from heddle.core.config import load_config

logger = structlog.get_logger()

# Cap on the length of the description-fallback when an MCP tool has no
# explicit ``description`` and we're forced to use the first line of the
# worker's ``system_prompt``.  Without a cap, hundred-line prompts (and
# anything embedded near the top — prompt-injection guards, credentials,
# proprietary instructions) leaked to every MCP client that called
# ``list_tools``.
_DESCRIPTION_FALLBACK_MAX = 200


# ---------------------------------------------------------------------------
# Public types (plain dicts matching mcp.types.Tool shape)
# ---------------------------------------------------------------------------
# We produce dicts rather than importing mcp.types so that discovery works
# even when the ``mcp`` package is not installed (useful for testing and
# config validation).  The server module converts these to mcp.types.Tool.


def make_tool(
    name: str,
    description: str,
    input_schema: dict[str, Any],
) -> dict[str, Any]:
    """Build an MCP-compatible tool definition dict."""
    return {
        "name": name,
        "description": description,
        "inputSchema": input_schema,
    }


# ---------------------------------------------------------------------------
# Worker tools
# ---------------------------------------------------------------------------


def discover_worker_tools(
    worker_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Generate MCP tool definitions from worker config entries.

    Each entry in *worker_entries* is a dict from the MCP gateway config::

        { "config": "path/to/worker.yaml", "name": "override", ... }

    The worker YAML provides ``name``, ``input_schema``, and ``system_prompt``.
    MCP config entries can override ``name`` and ``description``.

    Returns:
        List of tool definition dicts (one per worker entry).
    """
    tools: list[dict[str, Any]] = []

    for entry in worker_entries:
        config_path = entry["config"]
        try:
            cfg = load_config(config_path)
        except Exception as exc:
            logger.warning("mcp.discovery.worker_load_failed", path=config_path, error=str(exc))
            continue

        tool_name = entry.get("name", cfg.get("name", "unknown_worker"))
        description = entry.get("description") or cfg.get("description")
        if not description:
            # No explicit description configured.  Fall back to the first
            # line of the system_prompt — but capped, and only after warning
            # the operator.  The prior unconditional reveal could leak
            # prompt content (occasionally including credentials, prompt-
            # injection guards, or proprietary instructions) to every MCP
            # client that listed tools.
            description = _first_line(cfg.get("system_prompt", ""))
            if len(description) > _DESCRIPTION_FALLBACK_MAX:
                description = description[: _DESCRIPTION_FALLBACK_MAX - 1].rstrip() + "…"
            logger.warning(
                "mcp.discovery.description_fallback_used",
                tool_name=tool_name,
                hint="set 'description' in the worker config or MCP gateway entry",
            )
        input_schema = cfg.get("input_schema", {"type": "object"})

        # Ensure the schema has the required top-level structure.
        if "type" not in input_schema:
            input_schema = {"type": "object", "properties": input_schema}

        # Stash metadata for the bridge to use when dispatching.
        tool = make_tool(tool_name, description, input_schema)
        tool["_heddle"] = {
            "kind": "worker",
            "worker_type": cfg.get("name", tool_name),
            "tier": entry.get("tier", cfg.get("default_model_tier", "local")),
            "timeout": cfg.get("timeout_seconds", 60),
        }
        tools.append(tool)

    return tools


# ---------------------------------------------------------------------------
# Pipeline tools
# ---------------------------------------------------------------------------


def discover_pipeline_tools(
    pipeline_entries: list[dict[str, Any]],
    gateway_configs_dir: str | None = None,
) -> list[dict[str, Any]]:
    """Generate MCP tool definitions from pipeline config entries.

    The entry input schema is computed from the first stage's
    ``input_mapping``: keys whose source path starts with
    ``goal.context.`` become the tool's input properties.

    For example, ``input_mapping: {file_ref: "goal.context.file_ref"}``
    produces ``inputSchema: {type: object, required: [file_ref],
    properties: {file_ref: {type: string}}}``.

    Args:
        pipeline_entries: Pipeline config entries from the gateway YAML.
        gateway_configs_dir: Optional path to the directory containing the
            gateway's ``workers/`` directory.  When provided, worker
            configs referenced by pipeline stages are resolved against
            this directory.  When ``None`` (the legacy default), worker
            config lookup falls back to deriving the path from each
            pipeline's own config path — which used to also include a
            CWD-relative ``configs/workers/...`` fallback that resolved
            to ``~/configs/workers/...`` under Claude Desktop / Cursor
            and silently degraded every property type to ``string``.
    """
    tools: list[dict[str, Any]] = []

    for entry in pipeline_entries:
        config_path = entry["config"]
        try:
            cfg = load_config(config_path)
        except Exception as exc:
            logger.warning("mcp.discovery.pipeline_load_failed", path=config_path, error=str(exc))
            continue

        tool_name = entry["name"]  # required by validation
        description = entry.get("description", f"Pipeline: {cfg.get('name', tool_name)}")

        # Derive input schema from first stage's input_mapping.
        stages = cfg.get("pipeline_stages", [])
        input_schema = _pipeline_entry_schema(stages, entry, gateway_configs_dir)

        tool = make_tool(tool_name, description, input_schema)
        tool["_heddle"] = {
            "kind": "pipeline",
            "pipeline_name": cfg.get("name", tool_name),
            "timeout": cfg.get("timeout_seconds", 300),
        }
        tools.append(tool)

    return tools


def _pipeline_entry_schema(
    stages: list[dict[str, Any]],
    entry: dict[str, Any],
    gateway_configs_dir: str | None = None,
) -> dict[str, Any]:
    """Compute an MCP input schema from pipeline stage input mappings.

    Extracts ``goal.context.*`` references from the first stage's
    ``input_mapping`` and optionally enriches types from the first
    stage's worker config ``input_schema``.
    """
    if not stages:
        return {"type": "object"}

    # Collect all goal.context.* fields across ALL stages (not just first).
    # Some pipelines feed goal.context fields into later stages directly.
    context_fields: dict[str, str] = {}  # field_name -> source_path
    for stage in stages:
        mapping = stage.get("input_mapping", {})
        for source_path in mapping.values():
            if isinstance(source_path, str) and source_path.startswith("goal.context."):
                context_key = source_path.split(".", 2)[-1]
                context_fields[context_key] = source_path

    if not context_fields:
        return {"type": "object"}

    # Try to get property types from the first stage's worker config.
    first_stage_types = _load_stage_property_types(stages[0], entry, gateway_configs_dir)

    properties: dict[str, Any] = {}
    for field_name in context_fields:
        prop: dict[str, Any] = first_stage_types.get(field_name, {"type": "string"})
        properties[field_name] = prop

    return {
        "type": "object",
        "required": list(context_fields.keys()),
        "properties": properties,
    }


def _load_stage_property_types(
    stage: dict[str, Any],
    entry: dict[str, Any],
    gateway_configs_dir: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Try to load property type info from a stage's worker config.

    Returns a mapping of field_name -> JSON Schema property dict.
    Returns empty dict on failure (graceful degradation).
    """
    # The worker config path is not directly in the pipeline YAML.
    # Convention: look for configs/workers/{worker_type}.yaml relative
    # to the pipeline config's directory.
    # This is best-effort; if we can't find it, all types default to string.
    worker_type = stage.get("worker_type", "")
    if not worker_type:
        return {}

    # Resolve worker config paths.  Candidates, in priority order:
    #
    # 1. Pipeline-config-anchored ``../workers/{worker_type}.yaml``.
    #    Standard layout (``configs/orchestrators/foo.yaml`` →
    #    ``configs/workers/bar.yaml``); resolves correctly even when
    #    the MCP server runs with an unexpected CWD.
    # 2. Gateway-anchored ``{gateway_configs_dir}/workers/{worker_type}.yaml``
    #    when ``gateway_configs_dir`` was threaded in.  Caller passes
    #    the directory that contains ``workers/`` (typically the same
    #    directory the gateway YAML lives in).
    #
    # The earlier code also fell back to an unanchored
    # ``configs/workers/{worker_type}.yaml`` resolved against the CWD.
    # Under Claude Desktop / Cursor (CWD ≈ ``~``) that became
    # ``~/configs/workers/...`` — never existed, every property type
    # silently degraded to ``string``.  Dropped.
    import os

    config_path = entry.get("config", "")
    config_dir = os.path.dirname(config_path)
    base_dir = os.path.dirname(config_dir)  # up from orchestrators/ to project root

    candidates = [os.path.join(base_dir, "workers", f"{worker_type}.yaml")]
    if gateway_configs_dir:
        candidates.append(os.path.join(gateway_configs_dir, "workers", f"{worker_type}.yaml"))

    for candidate in candidates:
        try:
            worker_cfg = load_config(candidate)
            schema = worker_cfg.get("input_schema", {})
            return schema.get("properties", {})
        except Exception:
            continue

    # Nothing found — log once at DEBUG so deployments with non-
    # standard layouts can see that their schema degraded to
    # ``{type: string}`` for every property.
    logger.debug(
        "mcp.discovery.worker_schema_unresolved",
        worker_type=worker_type,
        tried=candidates,
        hint="Pass gateway_configs_dir to discover_pipeline_tools for non-standard layouts",
    )
    return {}


# ---------------------------------------------------------------------------
# Query tools
# ---------------------------------------------------------------------------


def discover_query_tools(
    query_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Generate MCP tool definitions from query backend entries.

    Each query backend is instantiated and its ``_get_handlers()`` method
    is called to discover available actions.  Per-action input schemas
    are generated from the backend's configuration (filter_fields,
    stats_groups, id_column, etc.).
    """
    tools: list[dict[str, Any]] = []

    for entry in query_entries:
        backend_path = entry["backend"]
        backend_config = entry.get("backend_config", {})
        requested_actions = entry["actions"]
        name_prefix = entry["name_prefix"]

        backend = _instantiate_backend(backend_path, backend_config)
        if backend is None:
            continue

        # Discover available actions.
        try:
            handlers = backend._get_handlers()
        except AttributeError:
            logger.warning(
                "mcp.discovery.query_no_handlers",
                backend=backend_path,
                hint="Backend must have _get_handlers() method",
            )
            continue

        available = set(handlers.keys())
        for action in requested_actions:
            if action not in available:
                logger.warning(
                    "mcp.discovery.query_unknown_action",
                    backend=backend_path,
                    action=action,
                    available=sorted(available),
                )
                continue

            tool_name = f"{name_prefix}_{action}"
            description = _query_action_description(action, backend)
            input_schema = _query_action_schema(action, backend)

            tool = make_tool(tool_name, description, input_schema)
            tool["_heddle"] = {
                "kind": "query",
                "worker_type": entry.get("worker_type", f"{name_prefix}_query"),
                "action": action,
                "backend_path": backend_path,
                "backend_config": backend_config,
                "timeout": entry.get("timeout", 30),
            }
            tools.append(tool)

    return tools


def _instantiate_backend(class_path: str, config: dict[str, Any]) -> Any:
    """Import and instantiate a backend class by fully qualified path."""
    if "." not in class_path:
        logger.warning("mcp.discovery.invalid_backend_path", path=class_path)
        return None

    module_path, class_name = class_path.rsplit(".", 1)
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        logger.warning("mcp.discovery.backend_import_failed", path=module_path, error=str(exc))
        return None

    backend_class = getattr(module, class_name, None)
    if backend_class is None:
        logger.warning("mcp.discovery.backend_class_not_found", module=module_path, cls=class_name)
        return None

    try:
        return backend_class(**config)
    except Exception as exc:
        logger.warning("mcp.discovery.backend_init_failed", path=class_path, error=str(exc))
        return None


def _query_action_description(action: str, backend: Any) -> str:
    """Generate a human-readable description for a query action."""
    table = getattr(backend, "table_name", "records")
    descriptions = {
        "search": f"Full-text search across {table}",
        "filter": f"Filter {table} by attributes",
        "stats": f"Aggregate statistics for {table}",
        "get": f"Get a single record from {table} by ID",
        "vector_search": f"Semantic similarity search across {table}",
    }
    return descriptions.get(action, f"Query action '{action}' on {table}")


def _query_action_schema(action: str, backend: Any) -> dict[str, Any]:
    """Generate an MCP input schema for a query action.

    Derives the schema from the backend's configuration rather than
    requiring manual schema definitions.
    """
    if action == "search":
        return {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "Search query text"},
                "limit": {"type": "integer", "description": "Max results (default 20, max 100)"},
            },
        }

    if action == "filter":
        props: dict[str, Any] = {}
        filter_fields = getattr(backend, "filter_fields", {})
        for field_name in filter_fields:
            # Infer type from the SQL template.
            sql = filter_fields[field_name]
            if "BOOLEAN" in sql.upper() or field_name.startswith("has_"):
                props[field_name] = {"type": "boolean"}
            elif ">=" in sql or "<=" in sql:
                props[field_name] = {"type": "integer"}
            else:
                props[field_name] = {"type": "string"}
        props["limit"] = {"type": "integer", "description": "Max results (default 20, max 100)"}
        return {"type": "object", "properties": props}

    if action == "stats":
        groups = getattr(backend, "stats_groups", set())
        schema: dict[str, Any] = {"type": "object", "properties": {}}
        if groups:
            schema["properties"]["group_by"] = {
                "type": "string",
                "description": f"Column to group by. Allowed: {', '.join(sorted(groups))}",
            }
        return schema

    if action == "get":
        id_col = getattr(backend, "id_column", "id")
        return {
            "type": "object",
            "required": [id_col],
            "properties": {
                id_col: {"type": "string", "description": f"Record ID ({id_col})"},
            },
        }

    if action == "vector_search":
        return {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "Semantic search query"},
                "limit": {"type": "integer", "description": "Max results (default 5, max 100)"},
            },
        }

    # Unknown action — accept anything.
    return {"type": "object"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _first_line(text: str) -> str:
    """Extract the first non-empty line from a string."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""
