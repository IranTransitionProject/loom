"""
Workspace manager for file-ref resolution and safe file I/O.

Many Heddle pipelines pass large data between stages via file references
rather than inlining content in NATS messages. This module provides a
centralized utility for resolving those references safely:

    - Path traversal protection (prevents ``../../../etc/passwd`` escapes)
    - File existence validation
    - JSON read/write helpers with structured error handling

Usage from a ProcessingBackend::

    from heddle.core.workspace import WorkspaceManager

    ws = WorkspaceManager("/tmp/my-workspace")
    path = ws.resolve("report.pdf")           # validated absolute path
    data = ws.read_json("report_extracted.json")  # parsed dict
    ws.write_json("output.json", {"key": "value"})

Usage from worker config YAML (for LLMWorker file-ref resolution)::

    workspace_dir: "/tmp/my-workspace"
    resolve_file_refs: ["file_ref"]   # payload fields to resolve

See Also:
    heddle.worker.runner.LLMWorker — resolves file_refs before building prompt
    heddle.worker.processor.SyncProcessingBackend — base class for sync backends
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class WorkspaceManager:
    """Manages file operations within a bounded workspace directory.

    All file access is restricted to the workspace boundary. Any attempt
    to reference a file outside the workspace (via ``../`` or symlinks)
    raises ``ValueError``.

    Attributes:
        workspace_dir: Absolute path to the workspace root directory.
    """

    def __init__(self, workspace_dir: str | Path) -> None:
        self.workspace_dir = Path(workspace_dir)

    def _ensure_within_workspace(self, file_ref: str) -> Path:
        """Resolve ``file_ref`` and confirm it lives inside the workspace.

        Returns the resolved absolute path on success, or raises
        ``ValueError`` if the path escapes the workspace boundary.
        Used by both read and write paths so the boundary is checked
        identically everywhere.

        Why ``Path.is_relative_to`` and not ``str.startswith`` (the
        previous shape):  string-prefix checks are defeated by
        sibling-prefix collisions.  With ``workspace_dir=/tmp/work``,
        a ``file_ref`` of ``"../work-evil/secret"`` resolves to
        ``/tmp/work-evil/secret`` whose string representation begins
        with ``/tmp/work`` — the prefix check passes and a file
        outside the workspace is read.  ``is_relative_to`` compares
        path components, not characters, and is immune.
        """
        workspace_root = self.workspace_dir.resolve()
        resolved = (self.workspace_dir / file_ref).resolve()
        if not resolved.is_relative_to(workspace_root):
            raise ValueError(f"Path traversal detected: {file_ref}")
        return resolved

    def resolve(self, file_ref: str) -> Path:
        """Resolve a file reference to a validated absolute path.

        Args:
            file_ref: Relative filename within the workspace
                (e.g., ``"report.pdf"`` or ``"subdir/data.json"``).

        Returns:
            Absolute resolved path guaranteed to be within the workspace.

        Raises:
            ValueError: If the resolved path escapes the workspace
                boundary (path traversal attack).
            FileNotFoundError: If the resolved file does not exist.
        """
        resolved = self._ensure_within_workspace(file_ref)
        if not resolved.exists():
            raise FileNotFoundError(f"File not found in workspace: {file_ref}")
        return resolved

    def read_json(self, file_ref: str) -> dict[str, Any]:
        """Read and parse a JSON file from the workspace.

        Args:
            file_ref: Relative filename of a JSON file in the workspace.

        Returns:
            Parsed JSON content as a dict.

        Raises:
            ValueError: If path traversal is detected.
            FileNotFoundError: If the file does not exist.
            json.JSONDecodeError: If the file is not valid JSON.
        """
        path = self.resolve(file_ref)
        return json.loads(path.read_text())

    def read_text(self, file_ref: str) -> str:
        """Read text content from a workspace file.

        Args:
            file_ref: Relative filename in the workspace.

        Returns:
            File contents as a string.

        Raises:
            ValueError: If path traversal is detected.
            FileNotFoundError: If the file does not exist.
        """
        path = self.resolve(file_ref)
        return path.read_text()

    def write_json(self, filename: str, data: dict[str, Any]) -> Path:
        """Write a dict as JSON to the workspace.

        Args:
            filename: Target filename within the workspace.
            data: Dict to serialize as JSON.

        Returns:
            Absolute path to the written file.

        Raises:
            ValueError: If ``filename`` would write outside the workspace
                (path traversal).
            OSError: If the write fails (disk full, permissions, etc.).
        """
        # Boundary check applies to writes too.  Without this, callers
        # could escape the workspace via ``"../outside.json"`` —
        # writing untrusted content anywhere on disk the process can
        # reach (Design Invariant 13).
        path = self._ensure_within_workspace(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
        return path
