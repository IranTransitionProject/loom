"""
Shared resource caps for the Heddle file-read paths.

A single source of truth for the maximum size of any file the runtime
will load into memory.  Used by:

  - :class:`heddle.mcp.resources.WorkspaceResources` (MCP resource reads)
  - :class:`heddle.core.workspace.WorkspaceManager` (worker workspace reads)
  - :mod:`heddle.worker.knowledge` (knowledge silo + knowledge-source reads)

Why a central cap:  before this module each read site happily slurped
``path.read_text()`` on whatever it found.  A single oversized file in
a workspace or silo could OOM the worker (and through MCP, the
gateway's caller) — same shape as the upload-cap bug fixed earlier in
``workshop/security.py`` but on the read side.

The cap is applied via :func:`enforce_file_size` at the read site
*before* the file is read, so the OOM never happens.  Each call site
accepts a ``max_bytes`` override for the rare case where a known-large
file should be allowed (e.g., a knowledge silo containing a large
transcript that the operator has explicitly opted into).

Default = 10 MiB:  enough for text + small JSON + every shipped
knowledge file, well below the smallest reasonable container memory
limit, and aggressive enough to reject the obvious DoS payloads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


DEFAULT_FILE_READ_MAX_BYTES: int = 10 * 1024 * 1024  # 10 MiB


class FileTooLargeError(Exception):
    """Raised when a file's size exceeds the configured read cap.

    The exception carries the path, observed size, and the cap that was
    in effect, so callers can produce a meaningful error message
    (workshop UI, MCP read response, log line) without re-stat'ing the
    file.
    """

    def __init__(self, path: str, size: int, max_bytes: int) -> None:
        super().__init__(f"File {path!r} is {size} bytes; exceeds the {max_bytes}-byte read cap.")
        self.path = path
        self.size = size
        self.max_bytes = max_bytes


def enforce_file_size(path: Path, *, max_bytes: int | None = None) -> int:
    """``stat`` the file and raise if it would breach the cap.

    Returns the file's size on success so the caller can log it without
    a second stat.  Pass ``max_bytes=None`` (the default) to use the
    module-level :data:`DEFAULT_FILE_READ_MAX_BYTES`; tests can patch
    that constant via ``monkeypatch.setattr`` rather than thread an
    override through every call site.

    Raises:
        FileTooLargeError: If ``path.stat().st_size > max_bytes``.
        FileNotFoundError: If ``path`` does not exist (propagated from ``stat``).
    """
    if max_bytes is None:
        max_bytes = DEFAULT_FILE_READ_MAX_BYTES
    size = path.stat().st_size
    if size > max_bytes:
        raise FileTooLargeError(str(path), size, max_bytes)
    return size
