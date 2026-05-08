"""Tests for heddle.mcp.server — MCP server assembly and tool dispatch."""

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from heddle.bus.memory import InMemoryBus
from heddle.core.messages import TaskResult, TaskStatus
from heddle.mcp.bridge import BridgeError, BridgeTimeoutError, MCPBridge
from heddle.mcp.server import (
    MCPGateway,
    ToolEntry,
    _build_annotations,
    _dispatch_tool,
    _execute_query_direct,
    _safe_dispatch,
    create_server,
)
from heddle.mcp.workshop_bridge import WorkshopBridge, WorkshopBridgeError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(dir_path: str, filename: str, data: dict) -> str:
    path = os.path.join(dir_path, filename)
    with open(path, "w") as f:
        yaml.dump(data, f)
    return path


def _make_gateway_config(tmp_path, worker_cfgs=None, resources_dir=None):
    """Create a valid MCP gateway config with optional workers and resources."""
    workers_dir = tmp_path / "workers"
    workers_dir.mkdir(exist_ok=True)

    worker_entries = []
    if worker_cfgs:
        for name, cfg in worker_cfgs.items():
            path = _write_yaml(str(workers_dir), f"{name}.yaml", cfg)
            worker_entries.append({"config": path})

    config = {
        "name": "test-gateway",
        "nats_url": "nats://localhost:4222",
        "tools": {
            "workers": worker_entries,
            "pipelines": [],
            "queries": [],
        },
    }

    if resources_dir:
        config["resources"] = {
            "workspace_dir": str(resources_dir),
        }

    return _write_yaml(str(tmp_path), "gateway.yaml", config)


def _make_workshop_only_gateway_config(tmp_path, enable=None):
    """Create an MCP gateway config that exposes only workshop tools."""
    config = {
        "name": "workshop-gateway",
        "tools": {
            "workers": [],
            "pipelines": [],
            "queries": [],
            "workshop": {
                "configs_dir": str(tmp_path),
            },
        },
    }
    if enable is not None:
        config["tools"]["workshop"]["enable"] = enable
    return _write_yaml(str(tmp_path), "workshop-gateway.yaml", config)


def _single_worker_cfgs(name="test_worker"):
    """Return a minimal single-worker config dict."""
    return {
        name: {
            "name": name,
            "system_prompt": "Test.",
            "input_schema": {
                "type": "object",
                "properties": {},
            },
        },
    }


# ---------------------------------------------------------------------------
# create_server
# ---------------------------------------------------------------------------


class TestCreateServer:
    """Test that create_server returns (FastMCP, MCPGateway)."""

    def test_creates_server_and_gateway(self, tmp_path):
        worker_cfgs = {
            "summarizer": {
                "name": "summarizer",
                "system_prompt": "Summarize text.",
                "input_schema": {
                    "type": "object",
                    "required": ["text"],
                    "properties": {"text": {"type": "string"}},
                },
            },
        }
        config_path = _make_gateway_config(tmp_path, worker_cfgs=worker_cfgs)

        server, gateway = create_server(config_path)

        from fastmcp import FastMCP

        assert isinstance(server, FastMCP)
        assert gateway.config["name"] == "test-gateway"
        assert "summarizer" in gateway.tool_registry
        assert len(gateway.tool_defs) == 1

    def test_no_tools(self, tmp_path):
        config_path = _make_gateway_config(tmp_path)
        server, gateway = create_server(config_path)
        assert len(gateway.tool_registry) == 0

    def test_tools_registered_on_fastmcp(self, tmp_path):
        """Verify tools are actually registered on the FastMCP instance."""
        worker_cfgs = {
            "summarizer": {
                "name": "summarizer",
                "system_prompt": "Summarize.",
                "input_schema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                },
            },
        }
        config_path = _make_gateway_config(tmp_path, worker_cfgs=worker_cfgs)
        server, gateway = create_server(config_path)

        # FastMCP keeps tools in _tool_manager.
        # list_tools() returns MCP Tool objects.
        tools = asyncio.run(server.list_tools())
        names = [t.name for t in tools]
        assert "summarizer" in names

    def test_with_resources(self, tmp_path):
        resources_dir = tmp_path / "workspace"
        resources_dir.mkdir()
        (resources_dir / "test.txt").write_text("hello")

        config_path = _make_gateway_config(tmp_path, resources_dir=resources_dir)
        server, gateway = create_server(config_path)
        assert gateway.resources is not None

    def test_without_resources(self, tmp_path):
        config_path = _make_gateway_config(tmp_path)
        server, gateway = create_server(config_path)
        assert gateway.resources is None

    def test_workshop_only_gateway_does_not_require_bus(self, tmp_path):
        config_path = _make_workshop_only_gateway_config(tmp_path)
        _server, gateway = create_server(config_path)

        assert gateway.requires_bus is False
        assert gateway.workshop_bridge is not None
        # No live bus → no DeadLetterConsumer.  deadletter.* tools fail
        # with "DeadLetterConsumer not configured" rather than silently
        # returning empty results from a disconnected in-memory consumer.
        assert gateway.workshop_bridge.dead_letter is None
        assert "workshop.worker.list" in gateway.tool_registry


# ---------------------------------------------------------------------------
# ToolEntry
# ---------------------------------------------------------------------------


class TestToolEntry:
    def test_fields(self):
        entry = ToolEntry(
            name="my_tool",
            kind="worker",
            tool_def={
                "name": "my_tool",
                "description": "desc",
                "inputSchema": {},
            },
            heddle_meta={"kind": "worker", "worker_type": "my_worker"},
        )
        assert entry.name == "my_tool"
        assert entry.kind == "worker"


# ---------------------------------------------------------------------------
# _dispatch_tool
# ---------------------------------------------------------------------------


class TestDispatchTool:
    @pytest.fixture
    async def bus_and_bridge(self):
        bus = InMemoryBus()
        await bus.connect()
        bridge = MCPBridge(bus)
        yield bus, bridge
        await bus.close()

    async def test_dispatch_worker(self, bus_and_bridge):
        bus, bridge = bus_and_bridge
        gateway = MCPGateway(
            config={"name": "test"},
            bridge=bridge,
            tool_registry={},
            tool_defs=[],
        )

        entry = ToolEntry(
            name="summarizer",
            kind="worker",
            tool_def={},
            heddle_meta={
                "kind": "worker",
                "worker_type": "summarizer",
                "tier": "local",
                "timeout": 5,
            },
        )

        ready = asyncio.Event()

        async def mock_worker():
            sub = await bus.subscribe("heddle.tasks.incoming")
            ready.set()
            async for data in sub:
                result = TaskResult(
                    task_id=data["task_id"],
                    worker_type="summarizer",
                    status=TaskStatus.COMPLETED,
                    output={"summary": "done"},
                )
                await bus.publish(
                    f"heddle.results.{data['parent_task_id']}",
                    result.model_dump(mode="json"),
                )
                await sub.unsubscribe()
                break

        worker_task = asyncio.create_task(mock_worker())
        await ready.wait()

        result = await _dispatch_tool(gateway, entry, {"text": "hello"})
        assert result == {"summary": "done"}
        await worker_task

    async def test_dispatch_query(self, bus_and_bridge):
        bus, bridge = bus_and_bridge
        gateway = MCPGateway(
            config={"name": "test"},
            bridge=bridge,
            tool_registry={},
            tool_defs=[],
        )

        entry = ToolEntry(
            name="docs_search",
            kind="query",
            tool_def={},
            heddle_meta={
                "kind": "query",
                "worker_type": "docs_query",
                "action": "search",
                "timeout": 5,
            },
        )

        ready = asyncio.Event()

        async def mock_worker():
            sub = await bus.subscribe("heddle.tasks.incoming")
            ready.set()
            async for data in sub:
                assert data["payload"]["action"] == "search"
                result = TaskResult(
                    task_id=data["task_id"],
                    worker_type="docs_query",
                    status=TaskStatus.COMPLETED,
                    output={"results": [{"id": "1"}]},
                )
                await bus.publish(
                    f"heddle.results.{data['parent_task_id']}",
                    result.model_dump(mode="json"),
                )
                await sub.unsubscribe()
                break

        worker_task = asyncio.create_task(mock_worker())
        await ready.wait()

        result = await _dispatch_tool(gateway, entry, {"query": "test"})
        assert result == {"results": [{"id": "1"}]}
        await worker_task

    async def test_dispatch_unknown_kind_raises(self, bus_and_bridge):
        _, bridge = bus_and_bridge
        gateway = MCPGateway(
            config={"name": "test"},
            bridge=bridge,
            tool_registry={},
            tool_defs=[],
        )

        entry = ToolEntry(
            name="bad",
            kind="unknown",
            tool_def={},
            heddle_meta={"kind": "unknown"},
        )

        with pytest.raises(BridgeError, match="Unknown tool kind"):
            await _dispatch_tool(gateway, entry, {})

    async def test_dispatch_pipeline(self, bus_and_bridge):
        """_dispatch_tool for pipeline kind calls call_pipeline."""
        bus, bridge = bus_and_bridge
        gateway = MCPGateway(
            config={"name": "test"},
            bridge=bridge,
            tool_registry={},
            tool_defs=[],
        )

        entry = ToolEntry(
            name="ingest_doc",
            kind="pipeline",
            tool_def={},
            heddle_meta={
                "kind": "pipeline",
                "timeout": 5,
            },
        )

        ready = asyncio.Event()

        async def mock_pipeline():
            sub = await bus.subscribe("heddle.goals.incoming")
            ready.set()
            async for data in sub:
                goal_id = data.get("goal_id")
                result = TaskResult(
                    task_id=goal_id,
                    parent_task_id=None,
                    worker_type="pipeline",
                    status=TaskStatus.COMPLETED,
                    output={"processed": True},
                )
                await bus.publish(
                    f"heddle.results.{goal_id}",
                    result.model_dump(mode="json"),
                )
                await sub.unsubscribe()
                break

        worker_task = asyncio.create_task(mock_pipeline())
        await ready.wait()

        result = await _dispatch_tool(gateway, entry, {"file_ref": "test.pdf"})
        assert result == {"processed": True}
        await worker_task


# ---------------------------------------------------------------------------
# Progress callback wiring (MCP progress notifications)
# ---------------------------------------------------------------------------


class TestProgressCallback:
    """Test that _dispatch_tool passes progress_callback to pipeline calls."""

    @pytest.fixture
    async def bus_and_bridge(self):
        bus = InMemoryBus()
        await bus.connect()
        bridge = MCPBridge(bus)
        yield bus, bridge
        await bus.close()

    async def test_pipeline_receives_progress_callback(self, bus_and_bridge):
        """_dispatch_tool passes progress_callback for pipelines."""
        bus, bridge = bus_and_bridge
        gateway = MCPGateway(
            config={"name": "test"},
            bridge=bridge,
            tool_registry={},
            tool_defs=[],
        )

        entry = ToolEntry(
            name="ingest_doc",
            kind="pipeline",
            tool_def={},
            heddle_meta={"kind": "pipeline", "timeout": 5},
        )

        progress_calls = []

        async def track_progress(stage_name: str, stage_idx: int, total: int) -> None:
            progress_calls.append((stage_name, stage_idx, total))

        ready = asyncio.Event()

        async def mock_pipeline():
            sub = await bus.subscribe("heddle.goals.incoming")
            ready.set()
            async for data in sub:
                goal_id = data.get("goal_id")
                # Emit an intermediate stage result first.
                stage_result = TaskResult(
                    task_id="stage-1-id",
                    parent_task_id=goal_id,
                    worker_type="extractor",
                    status=TaskStatus.COMPLETED,
                    output={"text": "extracted"},
                    processing_time_ms=10,
                )
                await bus.publish(
                    f"heddle.results.{goal_id}",
                    stage_result.model_dump(mode="json"),
                )
                # Small delay to let consumer process.
                await asyncio.sleep(0.01)
                # Then emit the final result.
                final = TaskResult(
                    task_id=goal_id,
                    parent_task_id=None,
                    worker_type="pipeline",
                    status=TaskStatus.COMPLETED,
                    output={"processed": True},
                )
                await bus.publish(
                    f"heddle.results.{goal_id}",
                    final.model_dump(mode="json"),
                )
                await sub.unsubscribe()
                break

        worker_task = asyncio.create_task(mock_pipeline())
        await ready.wait()

        result = await _dispatch_tool(
            gateway,
            entry,
            {"file_ref": "test.pdf"},
            progress_callback=track_progress,
        )
        assert result == {"processed": True}
        assert len(progress_calls) == 1
        assert progress_calls[0] == ("extractor", 1, 0)
        await worker_task

    async def test_worker_dispatch_ignores_progress_callback(self, bus_and_bridge):
        """_dispatch_tool for workers does not pass progress_callback."""
        bus, bridge = bus_and_bridge
        gateway = MCPGateway(
            config={"name": "test"},
            bridge=bridge,
            tool_registry={},
            tool_defs=[],
        )

        entry = ToolEntry(
            name="summarizer",
            kind="worker",
            tool_def={},
            heddle_meta={
                "kind": "worker",
                "worker_type": "summarizer",
                "tier": "local",
                "timeout": 5,
            },
        )

        ready = asyncio.Event()

        async def mock_worker():
            sub = await bus.subscribe("heddle.tasks.incoming")
            ready.set()
            async for data in sub:
                result = TaskResult(
                    task_id=data["task_id"],
                    worker_type="summarizer",
                    status=TaskStatus.COMPLETED,
                    output={"summary": "done"},
                )
                await bus.publish(
                    f"heddle.results.{data['parent_task_id']}",
                    result.model_dump(mode="json"),
                )
                await sub.unsubscribe()
                break

        worker_task = asyncio.create_task(mock_worker())
        await ready.wait()

        # Passing a callback shouldn't cause errors for non-pipelines.
        callback = AsyncMock()
        result = await _dispatch_tool(
            gateway,
            entry,
            {"text": "hi"},
            progress_callback=callback,
        )
        assert result == {"summary": "done"}
        callback.assert_not_called()
        await worker_task

    async def test_progress_callback_none_is_safe(self, bus_and_bridge):
        """_dispatch_tool with progress_callback=None works for pipelines."""
        bus, bridge = bus_and_bridge
        gateway = MCPGateway(
            config={"name": "test"},
            bridge=bridge,
            tool_registry={},
            tool_defs=[],
        )

        entry = ToolEntry(
            name="ingest_doc",
            kind="pipeline",
            tool_def={},
            heddle_meta={"kind": "pipeline", "timeout": 5},
        )

        ready = asyncio.Event()

        async def mock_pipeline():
            sub = await bus.subscribe("heddle.goals.incoming")
            ready.set()
            async for data in sub:
                goal_id = data.get("goal_id")
                final = TaskResult(
                    task_id=goal_id,
                    worker_type="pipeline",
                    status=TaskStatus.COMPLETED,
                    output={"ok": True},
                )
                await bus.publish(
                    f"heddle.results.{goal_id}",
                    final.model_dump(mode="json"),
                )
                await sub.unsubscribe()
                break

        worker_task = asyncio.create_task(mock_pipeline())
        await ready.wait()

        # Explicitly passing None (default).
        result = await _dispatch_tool(gateway, entry, {}, progress_callback=None)
        assert result == {"ok": True}
        await worker_task


# ---------------------------------------------------------------------------
# MCPGateway field tests
# ---------------------------------------------------------------------------


class TestMCPGateway:
    def test_gateway_defaults(self):
        bus = InMemoryBus()
        bridge = MCPBridge(bus)
        gw = MCPGateway(config={"name": "test"}, bridge=bridge)

        assert gw.tool_registry == {}
        assert gw.tool_defs == []
        assert gw.resources is None

    def test_gateway_with_registry(self):
        bus = InMemoryBus()
        bridge = MCPBridge(bus)
        entry = ToolEntry(
            name="tool1",
            kind="worker",
            tool_def={"name": "tool1", "inputSchema": {}},
            heddle_meta={"kind": "worker"},
        )
        gw = MCPGateway(
            config={"name": "test"},
            bridge=bridge,
            tool_registry={"tool1": entry},
            tool_defs=[{"name": "tool1", "inputSchema": {}}],
        )
        assert "tool1" in gw.tool_registry
        assert len(gw.tool_defs) == 1


# ---------------------------------------------------------------------------
# list_tools via FastMCP
# ---------------------------------------------------------------------------


class TestHandleListTools:
    """Test tool listing via FastMCP list_tools()."""

    @pytest.mark.asyncio
    async def test_list_tools_returns_tool_objects(self, tmp_path):
        """list_tools returns MCP Tool objects for registered tools."""
        worker_cfgs = {
            "summarizer": {
                "name": "summarizer",
                "system_prompt": "Summarize text.",
                "input_schema": {
                    "type": "object",
                    "required": ["text"],
                    "properties": {"text": {"type": "string"}},
                },
            },
        }
        config_path = _make_gateway_config(tmp_path, worker_cfgs=worker_cfgs)
        server, gateway = create_server(config_path)

        tools = await server.list_tools()

        assert len(tools) == 1
        tool = tools[0]
        assert tool.name == "summarizer"
        assert tool.parameters is not None

    @pytest.mark.asyncio
    async def test_list_tools_empty(self, tmp_path):
        """list_tools with no tools returns empty list."""
        config_path = _make_gateway_config(tmp_path)
        server, gateway = create_server(config_path)

        tools = await server.list_tools()

        assert tools == []


# ---------------------------------------------------------------------------
# call_tool via FastMCP (_safe_dispatch behavior)
# ---------------------------------------------------------------------------


class TestHandleCallTool:
    """Test tool calling via FastMCP call_tool().

    Tools are registered with _safe_dispatch which catches errors and
    returns error dicts instead of raising.
    """

    @pytest.mark.asyncio
    async def test_unknown_tool_raises(self, tmp_path):
        """Calling an unknown tool raises an error from FastMCP."""
        config_path = _make_gateway_config(tmp_path)
        server, _gw = create_server(config_path)

        from fastmcp.exceptions import NotFoundError

        with pytest.raises(NotFoundError, match="Unknown tool"):
            await server.call_tool("nonexistent_tool", {"x": 1})

    @pytest.mark.asyncio
    async def test_bridge_timeout_error_returns_error_dict(self, tmp_path):
        """BridgeTimeoutError is caught by _safe_dispatch."""
        config_path = _make_gateway_config(tmp_path, worker_cfgs=_single_worker_cfgs("slow"))
        server, _gw = create_server(config_path)

        with patch(
            "heddle.mcp.server._dispatch_tool",
            new_callable=AsyncMock,
        ) as m:
            m.side_effect = BridgeTimeoutError("Timed out after 5s")
            result = await server.call_tool("slow", {})
            data = json.loads(result.content[0].text)
            assert "Timeout" in data["error"]

    @pytest.mark.asyncio
    async def test_bridge_error_returns_error_dict(self, tmp_path):
        """BridgeError is caught by _safe_dispatch."""
        config_path = _make_gateway_config(tmp_path, worker_cfgs=_single_worker_cfgs("bad"))
        server, _gw = create_server(config_path)

        with patch(
            "heddle.mcp.server._dispatch_tool",
            new_callable=AsyncMock,
        ) as m:
            m.side_effect = BridgeError("Connection lost")
            result = await server.call_tool("bad", {})
            data = json.loads(result.content[0].text)
            assert data["error"] == "Connection lost"

    @pytest.mark.asyncio
    async def test_generic_exception_returns_internal_error(self, tmp_path):
        """Generic Exception is caught by _safe_dispatch."""
        config_path = _make_gateway_config(tmp_path, worker_cfgs=_single_worker_cfgs("crash"))
        server, _gw = create_server(config_path)

        with patch(
            "heddle.mcp.server._dispatch_tool",
            new_callable=AsyncMock,
        ) as m:
            m.side_effect = RuntimeError("kaboom")
            result = await server.call_tool("crash", {})
            data = json.loads(result.content[0].text)
            assert "Internal error" in data["error"]
            assert "kaboom" in data["error"]

    @pytest.mark.asyncio
    async def test_successful_call_returns_result(self, tmp_path):
        """Successful dispatch returns serialized result."""
        config_path = _make_gateway_config(tmp_path, worker_cfgs=_single_worker_cfgs("good"))
        server, _gw = create_server(config_path)

        with patch(
            "heddle.mcp.server._dispatch_tool",
            new_callable=AsyncMock,
        ) as m:
            m.return_value = {"summary": "test", "score": 0.9}
            result = await server.call_tool("good", {"text": "hello"})
            data = json.loads(result.content[0].text)
            assert data == {"summary": "test", "score": 0.9}


# ---------------------------------------------------------------------------
# _safe_dispatch unit tests
# ---------------------------------------------------------------------------


class TestSafeDispatch:
    """Test _safe_dispatch error handling directly."""

    @pytest.mark.asyncio
    async def test_workshop_bridge_error_caught(self):
        """WorkshopBridgeError returns error dict."""
        gateway = MagicMock()
        entry = ToolEntry(
            name="ws_tool",
            kind="workshop",
            tool_def={},
            heddle_meta={"kind": "workshop", "action": "worker.list"},
        )

        with patch(
            "heddle.mcp.server._dispatch_tool",
            new_callable=AsyncMock,
        ) as m:
            m.side_effect = WorkshopBridgeError("ConfigManager not configured")
            result = await _safe_dispatch(gateway, entry, {})
            assert "ConfigManager not configured" in result["error"]

    @pytest.mark.asyncio
    async def test_bridge_timeout_caught(self):
        """BridgeTimeoutError returns timeout error dict."""
        gateway = MagicMock()
        entry = ToolEntry(
            name="slow",
            kind="worker",
            tool_def={},
            heddle_meta={"kind": "worker"},
        )

        with patch(
            "heddle.mcp.server._dispatch_tool",
            new_callable=AsyncMock,
        ) as m:
            m.side_effect = BridgeTimeoutError("5s")
            result = await _safe_dispatch(gateway, entry, {})
            assert "Timeout" in result["error"]

    @pytest.mark.asyncio
    async def test_bridge_error_caught(self):
        """BridgeError returns error dict."""
        gateway = MagicMock()
        entry = ToolEntry(
            name="bad",
            kind="worker",
            tool_def={},
            heddle_meta={"kind": "worker"},
        )

        with patch(
            "heddle.mcp.server._dispatch_tool",
            new_callable=AsyncMock,
        ) as m:
            m.side_effect = BridgeError("Connection lost")
            result = await _safe_dispatch(gateway, entry, {})
            assert result["error"] == "Connection lost"

    @pytest.mark.asyncio
    async def test_generic_exception_caught(self):
        """Generic Exception returns internal error dict."""
        gateway = MagicMock()
        entry = ToolEntry(
            name="crash",
            kind="worker",
            tool_def={},
            heddle_meta={"kind": "worker"},
        )

        with patch(
            "heddle.mcp.server._dispatch_tool",
            new_callable=AsyncMock,
        ) as m:
            m.side_effect = RuntimeError("kaboom")
            result = await _safe_dispatch(gateway, entry, {})
            assert "Internal error" in result["error"]
            assert "kaboom" in result["error"]


# ---------------------------------------------------------------------------
# Resource handlers via FastMCP
# ---------------------------------------------------------------------------


class TestResourceHandlers:
    """Test list_resources and read_resource via FastMCP API."""

    @pytest.mark.asyncio
    async def test_list_resources(self, tmp_path):
        """list_resources returns registered workspace resources."""
        resources_dir = tmp_path / "workspace"
        resources_dir.mkdir()
        (resources_dir / "doc.txt").write_text("hello")
        (resources_dir / "data.json").write_text('{"a": 1}')

        config_path = _make_gateway_config(tmp_path, resources_dir=resources_dir)
        server, gateway = create_server(config_path)

        resources = await server.list_resources()

        assert len(resources) >= 1
        names = [r.name for r in resources]
        assert "doc.txt" in names or "data.json" in names

    @pytest.mark.asyncio
    async def test_read_resource_text(self, tmp_path):
        """read_resource returns text content for text files."""
        resources_dir = tmp_path / "workspace"
        resources_dir.mkdir()
        (resources_dir / "readme.txt").write_text("hello workspace")

        config_path = _make_gateway_config(tmp_path, resources_dir=resources_dir)
        server, gateway = create_server(config_path)

        content = await server.read_resource("workspace:///readme.txt")
        # FastMCP returns the content directly (str or bytes).
        assert "hello workspace" in str(content)

    @pytest.mark.asyncio
    async def test_read_resource_json(self, tmp_path):
        """read_resource returns content for JSON files."""
        resources_dir = tmp_path / "workspace"
        resources_dir.mkdir()
        (resources_dir / "data.json").write_text('{"key": "value"}')

        config_path = _make_gateway_config(tmp_path, resources_dir=resources_dir)
        server, gateway = create_server(config_path)

        content = await server.read_resource("workspace:///data.json")
        assert "key" in str(content)


# ---------------------------------------------------------------------------
# run_stdio
# ---------------------------------------------------------------------------


class TestRunStdio:
    """Test run_stdio transport runner."""

    def test_run_stdio_connects_and_closes_bridge(self, tmp_path):
        """run_stdio connects bridge, runs server, closes bridge."""
        config_path = _make_gateway_config(
            tmp_path,
            worker_cfgs=_single_worker_cfgs("summarizer"),
        )
        server, gateway = create_server(config_path)

        gateway.bridge = MagicMock()
        gateway.bridge.connect = AsyncMock()
        gateway.bridge.close = AsyncMock()

        with patch.object(server, "run_async", new_callable=AsyncMock) as mock_run:
            from heddle.mcp.server import run_stdio

            run_stdio(server, gateway)

        gateway.bridge.connect.assert_awaited_once()
        mock_run.assert_awaited_once_with(transport="stdio")
        gateway.bridge.close.assert_awaited_once()

    def test_run_stdio_with_resources_snapshots(self, tmp_path):
        """run_stdio snapshots resources if present."""
        resources_dir = tmp_path / "workspace"
        resources_dir.mkdir()
        config_path = _make_gateway_config(tmp_path, resources_dir=resources_dir)
        server, gateway = create_server(config_path)

        gateway.bridge = MagicMock()
        gateway.bridge.connect = AsyncMock()
        gateway.bridge.close = AsyncMock()

        mock_resources = MagicMock()
        mock_resources.snapshot = MagicMock()
        gateway.resources = mock_resources

        with patch.object(server, "run_async", new_callable=AsyncMock):
            from heddle.mcp.server import run_stdio

            run_stdio(server, gateway)

        mock_resources.snapshot.assert_called_once()

    def test_run_stdio_closes_bridge_on_error(self, tmp_path):
        """bridge.close() is called even if server.run_async raises."""
        config_path = _make_gateway_config(
            tmp_path,
            worker_cfgs=_single_worker_cfgs("summarizer"),
        )
        server, gateway = create_server(config_path)

        gateway.bridge = MagicMock()
        gateway.bridge.connect = AsyncMock()
        gateway.bridge.close = AsyncMock()

        from heddle.mcp.server import run_stdio

        server.run_async = AsyncMock(side_effect=RuntimeError("server crash"))
        with pytest.raises(RuntimeError, match="server crash"):
            run_stdio(server, gateway)

        gateway.bridge.close.assert_awaited_once()

    def test_run_stdio_skips_bridge_for_workshop_only(self, tmp_path):
        """Workshop-only gateway skips bridge connect/close."""
        config_path = _make_workshop_only_gateway_config(tmp_path)
        server, gateway = create_server(config_path)

        gateway.bridge = MagicMock()
        gateway.bridge.connect = AsyncMock()
        gateway.bridge.close = AsyncMock()

        with patch.object(server, "run_async", new_callable=AsyncMock) as mock_run:
            from heddle.mcp.server import run_stdio

            run_stdio(server, gateway)

        gateway.bridge.connect.assert_not_awaited()
        gateway.bridge.close.assert_not_awaited()
        mock_run.assert_awaited_once_with(transport="stdio")


# ---------------------------------------------------------------------------
# run_streamable_http
# ---------------------------------------------------------------------------


class TestRunStreamableHTTP:
    """Test run_streamable_http transport runner."""

    def test_run_streamable_http_connects_and_closes(self, tmp_path):
        """Streamable HTTP connects bridge, runs server, closes bridge."""
        config_path = _make_gateway_config(
            tmp_path,
            worker_cfgs=_single_worker_cfgs("summarizer"),
        )
        server, gateway = create_server(config_path)

        gateway.bridge = MagicMock()
        gateway.bridge.connect = AsyncMock()
        gateway.bridge.close = AsyncMock()

        with patch.object(server, "run_async", new_callable=AsyncMock) as mock_run:
            from heddle.mcp.server import run_streamable_http

            run_streamable_http(server, gateway, host="127.0.0.1", port=9999)

        gateway.bridge.connect.assert_awaited_once()
        mock_run.assert_awaited_once_with(transport="http", host="127.0.0.1", port=9999)
        gateway.bridge.close.assert_awaited_once()

    def test_run_streamable_http_with_resources(self, tmp_path):
        """Streamable HTTP snapshots resources if present."""
        resources_dir = tmp_path / "workspace"
        resources_dir.mkdir()
        config_path = _make_gateway_config(tmp_path, resources_dir=resources_dir)
        server, gateway = create_server(config_path)

        gateway.bridge = MagicMock()
        gateway.bridge.connect = AsyncMock()
        gateway.bridge.close = AsyncMock()

        mock_resources = MagicMock()
        mock_resources.snapshot = MagicMock()
        gateway.resources = mock_resources

        with patch.object(server, "run_async", new_callable=AsyncMock):
            from heddle.mcp.server import run_streamable_http

            run_streamable_http(server, gateway)

        mock_resources.snapshot.assert_called_once()

    def test_run_streamable_http_closes_on_error(self, tmp_path):
        """bridge.close() called even if run_async raises."""
        config_path = _make_gateway_config(
            tmp_path,
            worker_cfgs=_single_worker_cfgs("summarizer"),
        )
        server, gateway = create_server(config_path)

        gateway.bridge = MagicMock()
        gateway.bridge.connect = AsyncMock()
        gateway.bridge.close = AsyncMock()

        from heddle.mcp.server import run_streamable_http

        server.run_async = AsyncMock(side_effect=RuntimeError("port in use"))
        with pytest.raises(RuntimeError, match="port in use"):
            run_streamable_http(server, gateway)

        gateway.bridge.close.assert_awaited_once()

    def test_run_streamable_http_skips_bridge_for_workshop_only(self, tmp_path):
        """Workshop-only gateway skips bridge connect/close."""
        config_path = _make_workshop_only_gateway_config(tmp_path)
        server, gateway = create_server(config_path)

        gateway.bridge = MagicMock()
        gateway.bridge.connect = AsyncMock()
        gateway.bridge.close = AsyncMock()

        with patch.object(server, "run_async", new_callable=AsyncMock) as mock_run:
            from heddle.mcp.server import run_streamable_http

            run_streamable_http(server, gateway)

        gateway.bridge.connect.assert_not_awaited()
        gateway.bridge.close.assert_not_awaited()
        mock_run.assert_awaited_once()


# ---------------------------------------------------------------------------
# _build_annotations
# ---------------------------------------------------------------------------


class TestBuildAnnotations:
    """Test the _build_annotations helper."""

    def test_no_flags_returns_none(self):
        assert _build_annotations({}) is None
        assert _build_annotations({"kind": "worker"}) is None

    def test_destructive_flag(self):
        ann = _build_annotations({"destructive": True})
        assert ann is not None
        assert ann.destructiveHint is True

    def test_read_only_flag(self):
        ann = _build_annotations({"read_only": True})
        assert ann is not None
        assert ann.readOnlyHint is True

    def test_long_running_flag(self):
        ann = _build_annotations({"long_running": True})
        assert ann is not None
        assert ann.idempotentHint is False
        assert ann.openWorldHint is False

    def test_multiple_flags(self):
        ann = _build_annotations({"destructive": True, "read_only": True})
        assert ann.destructiveHint is True
        assert ann.readOnlyHint is True

    def test_false_flags_return_none(self):
        """Explicitly False flags should not produce annotations."""
        assert _build_annotations({"destructive": False, "read_only": False}) is None


# ---------------------------------------------------------------------------
# Workshop dispatch integration
# ---------------------------------------------------------------------------


class TestDispatchToolWorkshop:
    """Test _dispatch_tool for workshop kind."""

    @pytest.fixture
    async def bus_and_bridge(self):
        bus = InMemoryBus()
        await bus.connect()
        bridge = MCPBridge(bus)
        yield bus, bridge
        await bus.close()

    async def test_dispatch_workshop_tool(self, bus_and_bridge):
        """Workshop tools dispatch through WorkshopBridge."""
        _, bridge = bus_and_bridge
        mock_wb = AsyncMock(spec=WorkshopBridge)
        mock_wb.dispatch.return_value = {"workers": [], "count": 0}

        gateway = MCPGateway(
            config={"name": "test"},
            bridge=bridge,
            tool_registry={},
            tool_defs=[],
            workshop_bridge=mock_wb,
        )

        entry = ToolEntry(
            name="workshop.worker.list",
            kind="workshop",
            tool_def={},
            heddle_meta={
                "kind": "workshop",
                "action": "worker.list",
            },
        )

        result = await _dispatch_tool(gateway, entry, {})
        assert result == {"workers": [], "count": 0}
        mock_wb.dispatch.assert_awaited_once_with(
            action="worker.list",
            arguments={},
        )

    async def test_dispatch_workshop_no_bridge_raises(self, bus_and_bridge):
        """Workshop dispatch with no workshop_bridge raises BridgeError."""
        _, bridge = bus_and_bridge
        gateway = MCPGateway(
            config={"name": "test"},
            bridge=bridge,
            tool_registry={},
            tool_defs=[],
            workshop_bridge=None,
        )

        entry = ToolEntry(
            name="workshop.worker.list",
            kind="workshop",
            tool_def={},
            heddle_meta={
                "kind": "workshop",
                "action": "worker.list",
            },
        )

        with pytest.raises(BridgeError, match="Workshop tools are not configured"):
            await _dispatch_tool(gateway, entry, {})


# ---------------------------------------------------------------------------
# Direct query execution (no NATS)
# ---------------------------------------------------------------------------


class TestExecuteQueryDirect:
    """Test _execute_query_direct for in-process query execution."""

    async def test_direct_query_search(self):
        """Direct query dispatches to backend handler in-process."""
        mock_handler = MagicMock(
            return_value={"results": [{"id": "1", "title": "Test"}]},
        )
        mock_backend = MagicMock()
        mock_backend._get_handlers.return_value = {"search": mock_handler}

        with patch("importlib.import_module") as mock_import:
            mock_module = MagicMock()
            mock_module.FakeBackend = MagicMock(return_value=mock_backend)
            mock_import.return_value = mock_module

            result = await _execute_query_direct(
                meta={
                    "backend_path": "some.module.FakeBackend",
                    "backend_config": {"db_path": "/tmp/test.db"},
                    "action": "search",
                },
                arguments={"query": "test"},
            )

        assert result == {"results": [{"id": "1", "title": "Test"}]}
        mock_handler.assert_called_once_with(
            {"action": "search", "query": "test"},
        )

    async def test_direct_query_unknown_action(self):
        """Unknown action returns error dict."""
        mock_backend = MagicMock()
        mock_backend._get_handlers.return_value = {"search": MagicMock()}

        with patch("importlib.import_module") as mock_import:
            mock_module = MagicMock()
            mock_module.FakeBackend = MagicMock(return_value=mock_backend)
            mock_import.return_value = mock_module

            result = await _execute_query_direct(
                meta={
                    "backend_path": "some.module.FakeBackend",
                    "backend_config": {},
                    "action": "nonexistent",
                },
                arguments={},
            )

        assert "error" in result
        assert "nonexistent" in result["error"]

    async def test_dispatch_query_direct_when_no_bus(self):
        """_dispatch_tool routes queries directly when requires_bus is False."""
        mock_handler = MagicMock(
            return_value={"results": [{"id": "2"}]},
        )
        mock_backend = MagicMock()
        mock_backend._get_handlers.return_value = {"filter": mock_handler}

        bus = InMemoryBus()
        await bus.connect()
        bridge = MCPBridge(bus)

        gateway = MCPGateway(
            config={"name": "test"},
            bridge=bridge,
            tool_registry={},
            tool_defs=[],
            requires_bus=False,
        )

        entry = ToolEntry(
            name="docs_filter",
            kind="query",
            tool_def={},
            heddle_meta={
                "kind": "query",
                "worker_type": "docs_query",
                "action": "filter",
                "backend_path": "some.module.FakeBackend",
                "backend_config": {},
            },
        )

        with patch("importlib.import_module") as mock_import:
            mock_module = MagicMock()
            mock_module.FakeBackend = MagicMock(return_value=mock_backend)
            mock_import.return_value = mock_module

            result = await _dispatch_tool(gateway, entry, {"type": "person"})

        assert result == {"results": [{"id": "2"}]}
        mock_handler.assert_called_once()
        await bus.close()


# ---------------------------------------------------------------------------
# Workshop error handling via _safe_dispatch
# ---------------------------------------------------------------------------


class TestHandleCallToolWorkshopError:
    """Test that WorkshopBridgeError is caught by _safe_dispatch."""

    @pytest.mark.asyncio
    async def test_workshop_bridge_error_returns_json(self, tmp_path):
        """WorkshopBridgeError is caught and returned as error dict."""
        config_path = _make_gateway_config(
            tmp_path,
            worker_cfgs=_single_worker_cfgs("ws_tool"),
        )
        server, gateway = create_server(config_path)

        with patch(
            "heddle.mcp.server._dispatch_tool",
            new_callable=AsyncMock,
        ) as m:
            m.side_effect = WorkshopBridgeError("ConfigManager not configured")
            result = await server.call_tool("ws_tool", {})
            data = json.loads(result.content[0].text)
            assert "ConfigManager not configured" in data["error"]


# ---------------------------------------------------------------------------
# list_tools annotations via FastMCP
# ---------------------------------------------------------------------------


class TestHandleListToolsAnnotations:
    """Test that list_tools returns annotations from registry metadata."""

    @pytest.mark.asyncio
    async def test_annotations_attached_to_tools(self, tmp_path):
        """Tools with annotation flags have annotations attached."""
        config_path = _make_gateway_config(
            tmp_path,
            worker_cfgs=_single_worker_cfgs("annotated"),
        )
        server, gateway = create_server(config_path)

        # Inject read_only flag into the registry entry.
        entry = gateway.tool_registry["annotated"]
        entry.heddle_meta["read_only"] = True

        # Re-register the tool with updated annotations.
        from heddle.mcp.server import _register_tool

        # Remove old tool registration and re-register.
        if hasattr(server, "_tool_manager"):
            server._tool_manager._tools.pop("annotated", None)
        _register_tool(server, gateway, entry)

        tools = await server.list_tools()

        tool = next(t for t in tools if t.name == "annotated")
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True

    @pytest.mark.asyncio
    async def test_no_annotations_when_no_flags(self, tmp_path):
        """Tools without annotation flags have no annotations."""
        config_path = _make_gateway_config(
            tmp_path,
            worker_cfgs=_single_worker_cfgs("plain"),
        )
        server, gateway = create_server(config_path)

        tools = await server.list_tools()

        tool = next(t for t in tools if t.name == "plain")
        assert tool.annotations is None


# ---------------------------------------------------------------------------
# _build_workshop_bridge apps_dir edge cases
# ---------------------------------------------------------------------------


class TestBuildWorkshopBridgeAppsDir:
    """Test _build_workshop_bridge with apps_dir configuration."""

    def test_apps_dir_nonexistent_is_skipped(self, tmp_path):
        """Non-existent apps_dir produces empty extra_config_dirs."""
        from heddle.mcp.server import _build_workshop_bridge

        workshop_config = {
            "configs_dir": str(tmp_path),
            "apps_dir": str(tmp_path / "nonexistent_apps"),
        }
        bridge = _build_workshop_bridge(workshop_config)
        # Should succeed without error.
        assert bridge is not None

    def test_apps_dir_with_app_configs_subdir(self, tmp_path):
        """App subdirs with configs/ are added to extra_config_dirs."""
        from heddle.mcp.server import _build_workshop_bridge

        # Set up a fake apps directory with one deployed app.
        apps_dir = tmp_path / "apps"
        apps_dir.mkdir()
        app_dir = apps_dir / "myapp"
        app_dir.mkdir()
        (app_dir / "configs").mkdir()
        # Create a dummy worker config.
        (app_dir / "configs" / "worker.yaml").write_text(
            "name: myapp_worker\nsystem_prompt: test\n"
        )

        workshop_config = {
            "configs_dir": str(tmp_path),
            "apps_dir": str(apps_dir),
        }
        bridge = _build_workshop_bridge(workshop_config)
        assert bridge is not None

    def test_apps_dir_app_without_configs_subdir_skipped(self, tmp_path):
        """App subdirs without configs/ are not added."""
        from heddle.mcp.server import _build_workshop_bridge

        apps_dir = tmp_path / "apps"
        apps_dir.mkdir()
        # App directory without a configs/ subdirectory.
        (apps_dir / "bareapp").mkdir()

        workshop_config = {
            "configs_dir": str(tmp_path),
            "apps_dir": str(apps_dir),
        }
        bridge = _build_workshop_bridge(workshop_config)
        assert bridge is not None


# ---------------------------------------------------------------------------
# Dead-letter wiring (A1 regression — workshop deadletter.list was always empty)
# ---------------------------------------------------------------------------


class TestDeadLetterWiring:
    """Pin the wiring that connects Workshop's DeadLetterConsumer to the live bus.

    Before the fix: ``_build_workshop_bridge`` constructed
    ``DeadLetterConsumer(bus=InMemoryBus())`` unconditionally and never
    subscribed it, so ``workshop.deadletter.list`` over the MCP gateway
    was permanently empty when the gateway ran against real NATS.
    """

    def test_consumer_uses_live_bus_when_provided(self):
        """The DLC's bus must be the gateway's live bus (not a fresh in-memory)."""
        from heddle.mcp.server import _build_workshop_bridge

        live_bus = InMemoryBus()
        wb = _build_workshop_bridge({"configs_dir": "."}, replay_bus=live_bus)

        assert wb.dead_letter is not None
        # _bus is the underlying bus injected into BaseActor.__init__.
        assert wb.dead_letter._bus is live_bus
        assert wb.replay_bus is live_bus

    def test_no_consumer_in_no_bus_mode(self):
        """No live bus → no DLC (avoid silent-empty trap)."""
        from heddle.mcp.server import _build_workshop_bridge

        wb = _build_workshop_bridge({"configs_dir": "."}, replay_bus=None)
        assert wb.dead_letter is None
        assert wb.replay_bus is None

    @pytest.mark.asyncio
    async def test_drain_helper_records_published_messages(self):
        """``_drain_dead_letter`` subscribes and feeds messages to the consumer."""
        import contextlib as _contextlib

        from heddle.mcp.server import _drain_dead_letter
        from heddle.router.dead_letter import DeadLetterConsumer

        bus = InMemoryBus()
        await bus.connect()
        consumer = DeadLetterConsumer(bus=bus)

        task = asyncio.create_task(_drain_dead_letter(consumer, bus))
        # Yield so the subscribe inside the drain runs before publish.
        for _ in range(5):
            if "heddle.tasks.dead_letter" in bus._subscribers:
                break
            await asyncio.sleep(0)

        await bus.publish(
            "heddle.tasks.dead_letter",
            {
                "reason": "no_route",
                "task_id": "t-1",
                "worker_type": "missing",
                "original_task": {"foo": "bar"},
            },
        )

        for _ in range(50):
            if consumer.count() >= 1:
                break
            await asyncio.sleep(0.01)

        assert consumer.count() == 1
        entries = consumer.list_entries()
        assert entries[0]["reason"] == "no_route"
        assert entries[0]["task_id"] == "t-1"

        task.cancel()
        with _contextlib.suppress(asyncio.CancelledError):
            await task
        await bus.close()

    def test_run_stdio_subscribes_consumer_when_workshop_present(self):
        """Lifecycle: run_stdio subscribes to ``heddle.tasks.dead_letter``.

        Pins both the subscribe (assertion on bus subscribers) and that
        published dead-letter envelopes flow through the drain into the
        consumer's stored entries — the contract the Workshop UI relies on.
        """
        from fastmcp import FastMCP

        from heddle.mcp.server import run_stdio
        from heddle.mcp.workshop_bridge import WorkshopBridge
        from heddle.router.dead_letter import DeadLetterConsumer

        bus = InMemoryBus()
        bridge = MCPBridge(bus)
        consumer = DeadLetterConsumer(bus=bus)
        workshop_bridge = WorkshopBridge(dead_letter=consumer, replay_bus=bus)
        gateway = MCPGateway(
            config={"name": "test"},
            bridge=bridge,
            tool_registry={},
            tool_defs=[],
            workshop_bridge=workshop_bridge,
            requires_bus=True,
        )
        server = FastMCP(name="test")

        observed: dict[str, object] = {}

        async def fake_run_async(transport: str) -> None:
            # By now, the drain task has been spawned and subscribed.
            for _ in range(20):
                if "heddle.tasks.dead_letter" in bus._subscribers:
                    break
                await asyncio.sleep(0)
            observed["subjects"] = list(bus._subscribers.keys())
            await bus.publish(
                "heddle.tasks.dead_letter",
                {"reason": "rate_limited", "task_id": "rl-1"},
            )
            for _ in range(50):
                if consumer.count() >= 1:
                    break
                await asyncio.sleep(0.01)

        with patch.object(server, "run_async", side_effect=fake_run_async):
            run_stdio(server, gateway)

        assert "heddle.tasks.dead_letter" in observed["subjects"]
        assert consumer.count() == 1
        assert consumer.list_entries()[0]["reason"] == "rate_limited"

    def test_run_stdio_no_drain_when_no_workshop(self, tmp_path):
        """Adjacent contract: no workshop bridge → no drain task, no crash."""
        config_path = _make_gateway_config(
            tmp_path,
            worker_cfgs=_single_worker_cfgs("solo"),
        )
        server, gateway = create_server(config_path)

        gateway.bridge = MagicMock()
        gateway.bridge.connect = AsyncMock()
        gateway.bridge.close = AsyncMock()

        # No workshop bridge means no drain — runner must complete cleanly.
        assert gateway.workshop_bridge is None

        with patch.object(server, "run_async", new_callable=AsyncMock):
            from heddle.mcp.server import run_stdio

            run_stdio(server, gateway)

        gateway.bridge.connect.assert_awaited_once()
        gateway.bridge.close.assert_awaited_once()
