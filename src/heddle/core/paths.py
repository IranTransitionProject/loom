"""Path-safety helper for workspace-bound file references.

Several modules accept a caller-supplied filename and resolve it
against a trusted root directory:

- :class:`heddle.core.workspace.WorkspaceManager` (pipeline file refs)
- :class:`heddle.mcp.resources.WorkspaceResourceProvider`
  (``workspace:///filename`` URIs)
- :func:`heddle.worker.knowledge.apply_silo_updates`
  (LLM-requested writes to ``read_write`` folder silos)

Each previously implemented the boundary check inline.  The check is
load-bearing — it is Design Invariant 13's runtime enforcement — and
inline copies drift.  This module provides one definition:

    resolve_within(root, candidate) -> Path

The two important details the helper preserves:

1. **``Path.is_relative_to`` and not ``str.startswith``.**  Prefix
   string comparison is defeated by sibling-prefix collisions:
   ``workspace="/tmp/work"`` + ``candidate="../work-evil/secret"``
   resolves to ``/tmp/work-evil/secret`` whose string representation
   begins with ``/tmp/work`` — and a file outside the workspace is
   read.  ``is_relative_to`` compares path components, not characters,
   and is immune.

2. **Both sides are ``.resolve()``-d.**  ``resolve()`` follows
   symlinks, so a malicious symlink inside the root that points
   outside is caught: ``(root / "link").resolve()`` lands outside
   ``root.resolve()`` and fails the ``is_relative_to`` check.

The helper does NOT check existence.  Callers that need existence
checks layer them on top — that's deliberate, because writes need
boundary safety but the target path doesn't exist yet.

What this helper does NOT replace:

- :func:`heddle.workshop.config_manager._validate_config_name` —
  filename allowlist (``^[A-Za-z0-9_-]+$``), no path resolution.
  Different problem, different threat model.
- :func:`heddle.workshop.app_manager._validate_config_path` — checks
  ZIP entry strings against a synthetic ``/app-root`` before any file
  is on disk.  No filesystem to resolve against.
"""

from __future__ import annotations

from pathlib import Path


def resolve_within(root: Path, candidate: str | Path) -> Path:
    """Resolve ``candidate`` against ``root`` and raise if it escapes.

    Resolves both ``root`` and ``(root / candidate)`` (so symlinks are
    followed) and verifies the resolved candidate is a descendant of
    the resolved root.  An absolute ``candidate`` is rejected the same
    way — it resolves outside ``root`` and trips the boundary check.

    Args:
        root: Trusted root directory.  Need not exist yet (a fresh
            workspace dir is valid), but if it does exist, symlinks on
            the path to it are followed.
        candidate: A relative path inside ``root``, supplied by an
            untrusted caller (URL, message payload, LLM output, ...).

    Returns:
        The resolved absolute :class:`Path` (guaranteed under ``root``).

    Raises:
        ValueError: If the resolved candidate is not relative to the
            resolved root — i.e. the candidate would escape the
            boundary via ``..``, an absolute path, or a symlink.
    """
    root_resolved = Path(root).resolve()
    candidate_resolved = (Path(root) / candidate).resolve()
    if not candidate_resolved.is_relative_to(root_resolved):
        raise ValueError(f"Path traversal detected: {candidate}")
    return candidate_resolved
