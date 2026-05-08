"""MCP server assembly — wires config, discovery, bridge, and resources.

Creates a fully configured ``FastMCP`` server from a HEDDLE MCP gateway
config YAML.  The server exposes HEDDLE workers, pipelines, query backends,
and Workshop operations as MCP tools, and workspace files as MCP resources.

Usage::

    from heddle.mcp.server import create_server, run_stdio

    mcp, gateway = create_server("configs/mcp/docman.yaml")
    run_stdio(mcp, gateway)

See Also:
    heddle.mcp.config              — config loading and validation
    heddle.mcp.discovery           — tool definition generation
    heddle.mcp.bridge              — NATS call dispatch
    heddle.mcp.resources           — workspace resource exposure
    heddle.mcp.workshop_discovery  — Workshop tool definitions
    heddle.mcp.workshop_bridge     — Workshop direct dispatch
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastmcp import FastMCP as FastMCPType

    from heddle.bus.base import MessageBus
    from heddle.router.dead_letter import DeadLetterConsumer

import structlog

from heddle.bus.nats_adapter import NATSBus
from heddle.mcp.bridge import BridgeError, MCPBridge
from heddle.mcp.config import load_mcp_config
from heddle.mcp.council_bridge import CouncilBridge
from heddle.mcp.council_discovery import discover_council_tools
from heddle.mcp.discovery import (
    discover_pipeline_tools,
    discover_query_tools,
    discover_worker_tools,
)
from heddle.mcp.resources import WorkspaceResources
from heddle.mcp.session_bridge import SessionBridge
from heddle.mcp.workshop_bridge import WorkshopBridge
from heddle.mcp.workshop_discovery import discover_session_tools, discover_workshop_tools

logger = structlog.get_logger()


@dataclass
class ToolEntry:
    """Registry entry linking an MCP tool name to its dispatch info."""

    name: str
    kind: str  # "worker", "pipeline", "query", "workshop"
    tool_def: dict[str, Any]  # MCP Tool shape
    heddle_meta: dict[str, Any]  # _loom metadata from discovery


@dataclass
class MCPGateway:
    """Holds all state for a running MCP gateway."""

    config: dict[str, Any]
    bridge: MCPBridge
    tool_registry: dict[str, ToolEntry] = field(default_factory=dict)
    tool_defs: list[dict[str, Any]] = field(default_factory=list)
    resources: WorkspaceResources | None = None
    workshop_bridge: WorkshopBridge | None = None
    session_bridge: SessionBridge | None = None
    council_bridge: CouncilBridge | None = None
    requires_bus: bool = True


def create_server(config_path: str) -> tuple[FastMCPType, MCPGateway]:
    """Create a FastMCP server and MCPGateway from a config file.

    Returns:
        Tuple of (FastMCP, MCPGateway).
        The gateway must be connected before the server can handle calls.
    """
    from fastmcp import FastMCP

    config = load_mcp_config(config_path)
    nats_url = config.get("nats_url", "nats://nats:4222")
    bus = NATSBus(nats_url)
    bridge = MCPBridge(bus)

    # --- Discover tools ---
    tools_config = config.get("tools", {})
    requires_bus = bool(tools_config.get("workers") or tools_config.get("pipelines"))

    all_tools: list[dict[str, Any]] = []
    all_tools.extend(discover_worker_tools(tools_config.get("workers", [])))
    all_tools.extend(discover_pipeline_tools(tools_config.get("pipelines", [])))
    all_tools.extend(discover_query_tools(tools_config.get("queries", [])))

    # Workshop tools (optional — only if tools.workshop is present).
    workshop_config = tools_config.get("workshop")
    workshop_bridge: WorkshopBridge | None = None
    if workshop_config is not None:
        all_tools.extend(discover_workshop_tools(workshop_config))
        workshop_bridge = _build_workshop_bridge(
            workshop_config,
            replay_bus=bridge.bus if requires_bus else None,
        )

    # Session tools (optional — only if tools.session is present).
    session_config = tools_config.get("session")
    session_bridge: SessionBridge | None = None
    if session_config is not None:
        all_tools.extend(discover_session_tools(session_config))
        session_bridge = SessionBridge(
            framework_dir=session_config.get(
                "framework_dir",
                os.environ.get("ITP_ROOT", ".") + "/framework",
            ),
            workspace_dir=session_config.get(
                "workspace_dir",
                os.environ.get("BAFT_WORKSPACE", "./itp-workspace"),
            ),
            baft_dir=session_config.get("baft_dir"),
            nats_url=session_config.get("nats_url", nats_url),
            ollama_url=session_config.get(
                "ollama_url",
                os.environ.get("OLLAMA_URL", "http://localhost:11434"),
            ),
            lm_studio_url=session_config.get(
                "lm_studio_url",
                os.environ.get("LM_STUDIO_URL"),
            ),
        )

    # Council tools (optional — only if tools.council is present).
    council_config = tools_config.get("council")
    council_bridge: CouncilBridge | None = None
    if council_config is not None:
        all_tools.extend(discover_council_tools(council_config))
        council_bridge = _build_council_bridge(council_config)

    # Build registry.
    registry: dict[str, ToolEntry] = {}
    mcp_tool_defs: list[dict[str, Any]] = []

    for tool in all_tools:
        heddle_meta = tool.pop("_heddle", {})
        entry = ToolEntry(
            name=tool["name"],
            kind=heddle_meta.get("kind", "unknown"),
            tool_def=tool,
            heddle_meta=heddle_meta,
        )
        registry[tool["name"]] = entry
        mcp_tool_defs.append(tool)

    logger.info(
        "mcp.server.tools_discovered",
        count=len(registry),
        tools=sorted(registry.keys()),
    )

    # --- Set up resources ---
    resources_config = config.get("resources")
    workspace_resources: WorkspaceResources | None = None
    if resources_config:
        workspace_resources = WorkspaceResources(
            workspace_dir=resources_config["workspace_dir"],
            patterns=resources_config.get("patterns"),
        )

    gateway = MCPGateway(
        config=config,
        bridge=bridge,
        tool_registry=registry,
        tool_defs=mcp_tool_defs,
        resources=workspace_resources,
        workshop_bridge=workshop_bridge,
        session_bridge=session_bridge,
        council_bridge=council_bridge,
        requires_bus=requires_bus,
    )

    # --- Build FastMCP Server ---
    mcp = FastMCP(
        name=config["name"],
        instructions=config.get("description"),
    )

    # Register each discovered tool dynamically.
    for entry in registry.values():
        _register_tool(mcp, gateway, entry)

    # Register workspace resources.
    if workspace_resources:
        _register_resources(mcp, gateway)

    # Health endpoint (available in HTTP transport).
    _register_health(mcp, gateway)

    return mcp, gateway


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def _register_tool(
    mcp: FastMCPType,
    gateway: MCPGateway,
    entry: ToolEntry,
) -> None:
    """Register a single discovered tool with the FastMCP server.

    Creates a ``FunctionTool`` wrapping the dispatch function with the
    JSON schema from discovery and annotations from heddle metadata.
    """
    from fastmcp.tools.function_tool import FunctionTool

    meta = entry.heddle_meta

    # Build the dispatch function for this specific entry.
    # Each tool gets its own closure capturing the gateway and entry.
    # Errors are caught and returned as JSON dicts (matching old behavior).
    async def tool_handler(**kwargs: Any) -> dict[str, Any]:
        return await _safe_dispatch(gateway, entry, kwargs)

    fn = tool_handler

    # Build annotations.
    annotations = _build_annotations(meta)

    tool = FunctionTool(
        fn=fn,
        name=entry.name,
        description=entry.tool_def.get("description", ""),
        parameters=entry.tool_def.get("inputSchema", {"type": "object"}),
        annotations=annotations,
    )
    mcp.add_tool(tool)


def _build_annotations(heddle_meta: dict[str, Any]) -> Any:
    """Build MCP ToolAnnotations from _loom metadata flags.

    Returns a ``ToolAnnotations`` instance if any flags are set,
    or ``None`` if no annotations are needed.
    """
    from mcp.types import ToolAnnotations

    destructive = heddle_meta.get("destructive", False)
    read_only = heddle_meta.get("read_only", False)
    long_running = heddle_meta.get("long_running", False)

    if not (destructive or read_only or long_running):
        return None

    kwargs: dict[str, Any] = {}
    if destructive:
        kwargs["destructiveHint"] = True
    if read_only:
        kwargs["readOnlyHint"] = True
    if long_running:
        # Eval runs create new DB entries — not idempotent, closed world.
        kwargs["idempotentHint"] = False
        kwargs["openWorldHint"] = False
    return ToolAnnotations(**kwargs)


# ---------------------------------------------------------------------------
# Resource registration
# ---------------------------------------------------------------------------


def _register_resources(mcp: FastMCPType, gateway: MCPGateway) -> None:
    """Register workspace files as FastMCP resources."""
    items = gateway.resources.list_resources()
    for item in items:
        uri = item["uri"]
        name = item["name"]
        mime = item.get("mimeType")

        # Each resource gets its own closure.
        def _make_reader(
            _uri: str = uri,
        ) -> Any:
            def read_resource() -> Any:
                content, _mime = gateway.resources.read_resource(_uri)
                return content

            return read_resource

        mcp.resource(uri, name=name, mime_type=mime)(_make_reader())


def _register_health(mcp: FastMCPType, gateway: MCPGateway) -> None:
    """Register a /health endpoint (used in HTTP transport)."""
    try:

        @mcp.custom_route("/health", methods=["GET"])
        async def health(_request: Any) -> Any:
            from starlette.responses import JSONResponse

            return JSONResponse({"status": "ok", "name": gateway.config["name"]})
    except Exception:
        # custom_route may not be available in all transports.
        pass


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


async def _safe_dispatch(
    gateway: MCPGateway,
    entry: ToolEntry,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch with error handling — returns error dicts instead of raising."""
    from heddle.mcp.bridge import BridgeTimeoutError
    from heddle.mcp.session_bridge import SessionBridgeError
    from heddle.mcp.workshop_bridge import WorkshopBridgeError

    try:
        return await _dispatch_tool(gateway, entry, arguments)
    except (WorkshopBridgeError, SessionBridgeError) as exc:
        return {"error": str(exc)}
    except BridgeTimeoutError as exc:
        return {"error": f"Timeout: {exc}"}
    except BridgeError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        logger.error("mcp.server.call_error", tool=entry.name, error=str(exc))
        return {"error": f"Internal error: {exc}"}


async def _dispatch_tool(  # noqa: PLR0911
    gateway: MCPGateway,
    entry: ToolEntry,
    arguments: dict[str, Any],
    progress_callback: Callable[[str, int, int], Any] | None = None,
) -> dict[str, Any]:
    """Dispatch an MCP tool call to the appropriate bridge method."""
    meta = entry.heddle_meta

    if entry.kind == "worker":
        return await gateway.bridge.call_worker(
            worker_type=meta["worker_type"],
            tier=meta.get("tier", "local"),
            payload=arguments,
            timeout=meta.get("timeout", 60),
        )

    if entry.kind == "pipeline":
        return await gateway.bridge.call_pipeline(
            goal_context=arguments,
            timeout=meta.get("timeout", 300),
            progress_callback=progress_callback,
        )

    if entry.kind == "query":
        if gateway.requires_bus:
            return await gateway.bridge.call_query(
                worker_type=meta["worker_type"],
                action=meta["action"],
                payload=arguments,
                timeout=meta.get("timeout", 30),
            )
        # Direct in-process query (no NATS).
        return await _execute_query_direct(meta, arguments)

    if entry.kind == "workshop":
        if gateway.workshop_bridge is None:
            raise BridgeError("Workshop tools are not configured")
        return await gateway.workshop_bridge.dispatch(
            action=meta["action"],
            arguments=arguments,
        )

    if entry.kind == "session":
        if gateway.session_bridge is None:
            raise BridgeError("Session tools are not configured")
        return await gateway.session_bridge.dispatch(
            action=meta["action"],
            arguments=arguments,
        )

    if entry.kind == "council":
        if gateway.council_bridge is None:
            raise BridgeError("Council tools are not configured")
        return await gateway.council_bridge.dispatch(
            action=meta["action"],
            arguments=arguments,
        )

    raise BridgeError(f"Unknown tool kind: {entry.kind}")


# ---------------------------------------------------------------------------
# Workshop bridge factory
# ---------------------------------------------------------------------------


def _build_workshop_bridge(
    workshop_config: dict[str, Any],
    *,
    replay_bus: Any | None = None,
) -> WorkshopBridge:
    """Construct a WorkshopBridge from the MCP gateway workshop config.

    Instantiates ConfigManager, DeadLetterConsumer, and optionally
    WorkerTestRunner, EvalRunner, and WorkshopDB based on available
    dependencies.
    """
    from pathlib import Path

    from heddle.router.dead_letter import DeadLetterConsumer
    from heddle.workshop.config_manager import ConfigManager

    configs_dir = workshop_config.get("configs_dir", "configs/")

    # Build extra config dirs from apps_dir (deployed apps).
    extra_config_dirs: list[Path] = []
    apps_dir = workshop_config.get("apps_dir")
    if apps_dir:
        apps_path = Path(apps_dir)
        if apps_path.is_dir():
            for app_dir in sorted(apps_path.iterdir()):
                if app_dir.is_dir():
                    configs_subdir = app_dir / "configs"
                    if configs_subdir.is_dir():
                        extra_config_dirs.append(configs_subdir)

    # Try to set up WorkshopDB.
    db = None
    try:
        from heddle.workshop.db import WorkshopDB

        db = WorkshopDB()
    except Exception as exc:
        logger.debug("workshop_bridge.db_init_skipped", reason=str(exc))

    config_manager = ConfigManager(
        configs_dir=configs_dir,
        db=db,
        extra_config_dirs=extra_config_dirs,
    )

    # Try to set up test runner (needs LLM backends).
    test_runner = None
    try:
        from heddle.worker.backends import build_backends_from_env
        from heddle.workshop.test_runner import WorkerTestRunner

        backends = build_backends_from_env()
        if backends:
            test_runner = WorkerTestRunner(backends)
    except Exception as exc:
        logger.debug("workshop_bridge.test_runner_skipped", reason=str(exc))

    # Set up eval runner if we have both test runner and DB.
    eval_runner = None
    if test_runner and db:
        from heddle.workshop.eval_runner import EvalRunner

        eval_runner = EvalRunner(test_runner, db)

    # Only construct a DeadLetterConsumer when a live bus is available.
    # In no-bus (workshop-only) mode the consumer would have no source of
    # dead-letter messages, so the deadletter.* tools fail explicitly with
    # "DeadLetterConsumer not configured" instead of silently returning empty.
    # Only construct a DeadLetterConsumer when a live bus is available.
    # In no-bus (workshop-only) mode the consumer would have no source of
    # dead-letter messages, so the deadletter.* tools fail explicitly with
    # "DeadLetterConsumer not configured" instead of silently returning empty.
    dead_letter = DeadLetterConsumer(bus=replay_bus) if replay_bus is not None else None

    return WorkshopBridge(
        config_manager=config_manager,
        test_runner=test_runner,
        eval_runner=eval_runner,
        db=db,
        dead_letter=dead_letter,
        replay_bus=replay_bus,
    )


def _build_council_bridge(
    council_config: dict[str, Any],
) -> CouncilBridge:
    """Construct a CouncilBridge from the MCP gateway council config.

    Instantiates a :class:`CouncilRunner` with available LLM backends
    and wraps it in a :class:`CouncilBridge`.
    """
    from heddle.contrib.council.runner import CouncilRunner

    configs_dir = council_config.get("configs_dir", "configs/councils")

    # Build LLM backends from environment.
    backends: dict = {}
    try:
        from heddle.worker.backends import build_backends_from_env

        backends = build_backends_from_env()
    except Exception as exc:
        logger.warning("council_bridge.backends_init_failed", reason=str(exc))

    runner = CouncilRunner(backends=backends)
    return CouncilBridge(runner=runner, configs_dir=configs_dir)


# ---------------------------------------------------------------------------
# Direct query execution (no NATS)
# ---------------------------------------------------------------------------


async def _execute_query_direct(
    meta: dict[str, Any],
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Execute a query action directly against an in-process backend.

    Used when the gateway has no NATS bus (local mode).  Instantiates
    the query backend from the ``_loom`` metadata and calls its handler.
    """
    import importlib

    backend_path = meta["backend_path"]
    backend_config = meta.get("backend_config", {})
    action = meta["action"]

    module_path, class_name = backend_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    backend_class = getattr(module, class_name)
    backend = backend_class(**backend_config)

    handlers = backend._get_handlers()
    handler = handlers.get(action)
    if handler is None:
        return {"error": f"Unknown query action: {action}"}

    payload = {"action": action, **arguments}

    # Query backends are synchronous — run in a thread.
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, handler, payload)


# ---------------------------------------------------------------------------
# Transport runners
# ---------------------------------------------------------------------------


async def _drain_dead_letter(consumer: DeadLetterConsumer, bus: MessageBus) -> None:
    """Subscribe to ``heddle.tasks.dead_letter`` and feed the consumer.

    Runs as a background task during the lifetime of the MCP server so
    Workshop's ``deadletter.list`` / ``deadletter.replay`` tools see live
    NATS dead-letter traffic.  Cancelled by the runner on shutdown.
    """
    sub = await bus.subscribe("heddle.tasks.dead_letter")
    logger.info("mcp.gateway.dead_letter_subscribed")
    try:
        async for data in sub:
            await consumer.handle_message(data)
    except asyncio.CancelledError:
        raise
    finally:
        with contextlib.suppress(Exception):
            await sub.unsubscribe()


def _maybe_start_dead_letter_drain(gateway: MCPGateway) -> asyncio.Task[None] | None:
    """Spawn the dead-letter drain task if the gateway's workshop bridge has one.

    Returns the task (so the runner can cancel it on shutdown), or None if
    no consumer is configured (workshop-only / no-bus mode).
    """
    wb = gateway.workshop_bridge
    if wb is None or wb.dead_letter is None or not gateway.requires_bus:
        return None
    return asyncio.create_task(_drain_dead_letter(wb.dead_letter, gateway.bridge.bus))


async def _stop_dead_letter_drain(task: asyncio.Task[None] | None) -> None:
    """Cancel and await the dead-letter drain task, suppressing CancelledError."""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def run_stdio(server: FastMCPType, gateway: MCPGateway) -> None:
    """Run the MCP server on stdio transport (blocking)."""

    async def _run() -> None:
        bridge_connected = False
        drain_task: asyncio.Task[None] | None = None
        if gateway.requires_bus:
            await gateway.bridge.connect()
            bridge_connected = True
            logger.info(
                "mcp.gateway.connected",
                nats_url=gateway.config.get("nats_url"),
            )
            drain_task = _maybe_start_dead_letter_drain(gateway)

        if gateway.resources:
            gateway.resources.snapshot()

        try:
            await server.run_async(transport="stdio")
        finally:
            await _stop_dead_letter_drain(drain_task)
            if bridge_connected:
                await gateway.bridge.close()

    asyncio.run(_run())


def run_streamable_http(
    server: FastMCPType,
    gateway: MCPGateway,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    """Run the MCP server on streamable HTTP transport (blocking).

    FastMCP handles all Starlette/uvicorn setup internally.
    """

    async def _run() -> None:
        bridge_connected = False
        drain_task: asyncio.Task[None] | None = None
        if gateway.requires_bus:
            await gateway.bridge.connect()
            bridge_connected = True
            logger.info(
                "mcp.gateway.connected",
                nats_url=gateway.config.get("nats_url"),
            )
            drain_task = _maybe_start_dead_letter_drain(gateway)

        if gateway.resources:
            gateway.resources.snapshot()

        try:
            await server.run_async(
                transport="http",
                host=host,
                port=port,
            )
        finally:
            await _stop_dead_letter_drain(drain_task)
            if bridge_connected:
                await gateway.bridge.close()

    asyncio.run(_run())
