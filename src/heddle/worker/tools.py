"""
Tool provider abstraction for LLM function-calling.

Workers can offer tools to LLMs via their config's ``knowledge_silos`` key.
Each tool-type silo specifies a ``provider`` class path (fully qualified,
like ``processing_backend``) and a ``config`` dict passed to the constructor.

Tool providers define what the LLM can call (via ``get_definition()``) and
execute the call when the LLM invokes it (via ``execute()``).

Example config::

    knowledge_silos:
      - name: "document_catalog"
        type: "tool"
        provider: "docman.tools.duckdb_view.DuckDBViewTool"
        config:
          db_path: "/tmp/docman-workspace/docman.duckdb"
          view_name: "document_summaries"
"""

from __future__ import annotations

import asyncio
import importlib
from abc import ABC, abstractmethod
from typing import Any

import structlog

logger = structlog.get_logger()


class ToolProvider(ABC):
    """A tool that can be offered to an LLM for function-calling.

    Subclasses define the tool's JSON Schema definition and implement
    the execution logic. The LLMWorker manages the multi-turn loop:
    it passes tool definitions to the backend, receives tool_calls,
    dispatches to the appropriate provider, and feeds results back.
    """

    @abstractmethod
    def get_definition(self) -> dict[str, Any]:
        """Return the tool definition in standard JSON Schema format.

        The returned dict must contain:
            - ``name``: Tool name (alphanumeric + underscores)
            - ``description``: What the tool does (shown to LLM)
            - ``parameters``: JSON Schema object for the tool's arguments

        Example::

            {
                "name": "search_documents",
                "description": "Full-text search over document catalog",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 10},
                    },
                    "required": ["query"],
                },
            }
        """
        ...

    @abstractmethod
    async def execute(self, arguments: dict[str, Any]) -> str:
        """Execute the tool with LLM-provided arguments.

        Args:
            arguments: Parsed arguments matching the tool's parameters schema.

        Returns:
            Result as a string (typically JSON). This is sent back to the LLM
            as the tool result in the next turn.
        """
        ...


class SyncToolProvider(ToolProvider):
    """Convenience base for synchronous tool implementations.

    Subclasses implement ``execute_sync()`` which is automatically offloaded
    to a thread pool. Use this for tools that wrap synchronous libraries
    (e.g., DuckDB queries, file I/O).
    """

    @abstractmethod
    def execute_sync(self, arguments: dict[str, Any]) -> str:
        """Execute the tool synchronously. Runs in a thread pool."""
        ...

    async def execute(self, arguments: dict[str, Any]) -> str:
        """Offload execute_sync() to a thread pool and return the result."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.execute_sync, arguments)


# Maximum number of tool-use rounds before forcing a final answer.
MAX_TOOL_ROUNDS = 10

# Default per-tool timeout in seconds.  Tools include DB queries
# that can legitimately run a few seconds; 30s gives slow tools
# real headroom while bounding a hung process.  Worker config can
# override via ``tool_timeout_seconds``.  ``0`` disables the bound
# (the worker logs a WARN at startup so the operator notices).
DEFAULT_TOOL_TIMEOUT_SECONDS: float = 30.0


def load_tool_provider(class_path: str, config: dict[str, Any]) -> ToolProvider:
    """Import and instantiate a ToolProvider by fully qualified class path.

    Follows the same dynamic-import pattern as ``_load_processing_backend``
    in ``cli/main.py``.

    Args:
        class_path: Dotted path like ``docman.tools.duckdb_view.DuckDBViewTool``.
        config: Dict of keyword arguments passed to the constructor.

    Returns:
        An instantiated ToolProvider.

    Raises:
        ImportError: If the module cannot be imported.
        AttributeError: If the class is not found in the module.
        TypeError: If the class is not a ToolProvider subclass.
    """
    if "." not in class_path:
        raise ImportError(
            f"Tool provider '{class_path}' must be a fully qualified class path "
            f"(e.g., 'docman.tools.duckdb_view.DuckDBViewTool')"
        )

    module_path, class_name = class_path.rsplit(".", 1)
    module = importlib.import_module(module_path)

    tool_class = getattr(module, class_name, None)
    if tool_class is None:
        raise AttributeError(f"Tool class '{class_name}' not found in '{module_path}'")

    if not (isinstance(tool_class, type) and issubclass(tool_class, ToolProvider)):
        raise TypeError(f"'{class_path}' is not a ToolProvider subclass")

    return tool_class(**config)
