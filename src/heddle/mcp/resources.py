"""
Workspace resource exposure for MCP.

Exposes files in a HEDDLE workspace directory as MCP resources with
``workspace:///`` URIs.  Tracks file modification times to detect
changes after tool calls and emit MCP resource change notifications.

Also listens on the NATS ``heddle.resources.changed`` subject for
external change notifications from HEDDLE workers (future use).
"""

from __future__ import annotations

import fnmatch
import mimetypes
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

# URI scheme for workspace resources.
WORKSPACE_SCHEME = "workspace"


class WorkspaceResources:
    """Manages workspace files as MCP resources.

    Scans the workspace directory and exposes matching files as
    resources with ``workspace:///filename`` URIs.  Maintains an
    mtime cache to detect changes between tool calls.
    """

    def __init__(
        self,
        workspace_dir: str | Path,
        patterns: list[str] | None = None,
    ) -> None:
        self.workspace_dir = Path(workspace_dir)
        self.patterns = patterns  # None means all files.
        self._mtime_cache: dict[str, float] = {}  # relative_path -> mtime

    def list_resources(self) -> list[dict[str, Any]]:
        """Scan workspace and return MCP resource descriptors.

        Returns:
            List of dicts matching ``mcp.types.Resource`` shape:
            ``{uri, name, description, mimeType}``.
        """
        resources: list[dict[str, Any]] = []

        if not self.workspace_dir.is_dir():
            return resources

        for path in sorted(self.workspace_dir.iterdir()):
            if path.is_dir():
                continue
            rel = path.name
            if not self._matches(rel):
                continue

            mime, _ = mimetypes.guess_type(rel)
            resources.append(
                {
                    "uri": self._to_uri(rel),
                    "name": rel,
                    "description": f"Workspace file: {rel}",
                    "mimeType": mime or "application/octet-stream",
                }
            )

        return resources

    def read_resource(
        self, uri: str, *, max_bytes: int | None = None
    ) -> tuple[str | bytes, str | None]:
        """Read a workspace resource by URI.

        Args:
            uri: A ``workspace:///filename`` URI.
            max_bytes: Per-call override for the file-size cap.  ``None``
                uses :data:`heddle.core.limits.DEFAULT_FILE_READ_MAX_BYTES`
                (10 MiB).

        Returns:
            Tuple of (content, mimeType).  Text files return string content;
            binary files return raw bytes.

        Raises:
            ValueError: If the URI scheme is wrong or the file is outside
                the workspace (path traversal).
            FileNotFoundError: If the file does not exist.
            FileTooLargeError: If the file would exceed the configured
                read cap.  Caught here and surfaced rather than allowed
                to OOM the gateway on a multi-GB workspace file.
        """
        from heddle.core.limits import enforce_file_size

        rel_path = self._from_uri(uri)
        full_path = self.workspace_dir / rel_path

        # Path traversal check.
        try:
            full_path.resolve().relative_to(self.workspace_dir.resolve())
        except ValueError as exc:
            raise ValueError(f"Path traversal detected: {uri}") from exc

        if not full_path.is_file():
            raise FileNotFoundError(f"Resource not found: {uri}")

        # Cap check BEFORE the read so an oversize file never reaches memory.
        enforce_file_size(full_path, max_bytes=max_bytes)

        mime, _ = mimetypes.guess_type(rel_path)
        mime = mime or "application/octet-stream"

        if mime.startswith("text/") or mime in ("application/json", "application/xml"):
            return full_path.read_text(encoding="utf-8", errors="replace"), mime

        return full_path.read_bytes(), mime

    def detect_changes(self) -> list[str]:
        """Detect workspace file changes since the last check.

        Compares current mtimes against the cache.  Returns a list of
        ``workspace:///`` URIs for new or modified files.

        Call this after each tool invocation to determine which
        resources changed.
        """
        changed: list[str] = []

        if not self.workspace_dir.is_dir():
            return changed

        current: dict[str, float] = {}
        for path in self.workspace_dir.iterdir():
            if path.is_dir():
                continue
            rel = path.name
            if not self._matches(rel):
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            current[rel] = mtime

            old_mtime = self._mtime_cache.get(rel)
            if old_mtime is None or mtime > old_mtime:
                changed.append(self._to_uri(rel))

        self._mtime_cache = current
        return changed

    def snapshot(self) -> None:
        """Take a snapshot of current mtimes (no change detection)."""
        self.detect_changes()  # Side-effect: updates cache, ignore return.

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _matches(self, filename: str) -> bool:
        """Check if a filename matches the configured patterns."""
        if self.patterns is None:
            return True
        return any(fnmatch.fnmatch(filename, p) for p in self.patterns)

    @staticmethod
    def _to_uri(relative_path: str) -> str:
        return f"{WORKSPACE_SCHEME}:///{relative_path}"

    @staticmethod
    def _from_uri(uri: str) -> str:
        prefix = f"{WORKSPACE_SCHEME}:///"
        if not uri.startswith(prefix):
            raise ValueError(f"Invalid workspace URI: {uri} (expected {prefix}...)")
        return uri[len(prefix) :]
