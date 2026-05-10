"""
Scoped knowledge/RAG loader for worker context injection.

Workers can have knowledge sources defined in their config YAML under
a `knowledge_sources` key. This module loads those files and formats them
for injection into the system prompt, giving workers domain-specific context.

Knowledge silos extend this with folder-based knowledge:
    knowledge_silos:
      - name: "classification_guides"
        type: "folder"
        path: "knowledge/classification/"
        permissions: "read"           # "read" or "read_write"

Folder silos load all text files from a directory into the system prompt.
Writable silos accept ``silo_updates`` from the LLM output to persist
learned patterns (add/modify/delete files within the silo folder).
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

import structlog
import yaml

from heddle.core.limits import FileTooLargeError, enforce_file_size

logger = structlog.get_logger()

# File extensions loaded from folder silos.
_TEXT_EXTENSIONS = {".md", ".txt", ".yaml", ".yml", ".json", ".csv", ".toml"}


def load_knowledge_sources(sources: list[dict[str, Any]]) -> str:
    """
    Load knowledge sources and format them for system prompt injection.

    Each source has:
    - path: file path to the knowledge file
    - inject_as: "reference" (append to prompt) or "few_shot" (format as examples)
    - max_bytes (optional): per-source read cap override (default: 10 MiB)

    Files larger than the cap are skipped with a warning rather than
    raising — knowledge loading is best-effort, not load-bearing, so
    one oversize file shouldn't fail the whole worker startup.
    """
    sections = []

    for source in sources:
        path = Path(source["path"])
        inject_as = source.get("inject_as", "reference")
        max_bytes = source.get("max_bytes")

        if not path.exists():
            logger.warning(
                "knowledge.source_not_found",
                path=str(path),
                inject_as=inject_as,
            )
            continue

        try:
            enforce_file_size(path, max_bytes=max_bytes)
        except FileTooLargeError as exc:
            logger.warning(
                "knowledge.source_too_large",
                path=str(path),
                size=exc.size,
                max_bytes=exc.max_bytes,
            )
            continue

        content = path.read_text()

        if inject_as == "reference":
            sections.append(f"\n--- Reference: {path.name} ---\n{content}")
        elif inject_as == "few_shot":
            sections.append(_format_few_shot(content, path.suffix))

    return "\n".join(sections)


def _format_few_shot(content: str, suffix: str) -> str:
    """Format content as few-shot examples."""
    if suffix in (".yaml", ".yml"):
        data = yaml.safe_load(content)
        if isinstance(data, list):
            examples = []
            for i, item in enumerate(data, 1):
                examples.append(f"\nExample {i}:")
                examples.append(f"Input: {item.get('input', '')}")
                examples.append(f"Output: {item.get('output', '')}")
            return "\n--- Few-Shot Examples ---" + "\n".join(examples)

    # For JSONL or plain text, return as-is with header
    return f"\n--- Few-Shot Examples ---\n{content}"


# ---------------------------------------------------------------------------
# Knowledge silos — folder loading and write-back
# ---------------------------------------------------------------------------


def load_knowledge_silos(silos: list[dict[str, Any]]) -> str:
    """Load folder-type silos and return formatted content for system prompt.

    Only processes silos with ``type="folder"``. Tool-type silos are handled
    separately by the runner's ``_load_tool_providers()``.

    Returns:
        Concatenated content from all folder silos, with section headers.
        Empty string if no folder silos or no content found.
    """
    sections: list[str] = []

    for silo in silos:
        if silo.get("type") != "folder":
            continue

        name = silo.get("name", "unnamed")
        path = Path(silo["path"])

        if not path.is_dir():
            logger.warning("knowledge.silo_folder_not_found", path=str(path), silo=name)
            continue

        content = _load_folder_contents(path)
        if content:
            sections.append(f"--- Knowledge Silo: {name} ---\n{content}")

    return "\n\n".join(sections)


def _load_folder_contents(folder: Path) -> str:
    """Read all text files from a folder, with file headers.

    Reads ``.md``, ``.txt``, ``.yaml``, ``.yml``, ``.json``, ``.csv``,
    ``.toml`` files. Skips files matching patterns in ``.siloignore``.

    Files are sorted by name for deterministic prompt ordering.
    """
    ignore_patterns = _load_siloignore(folder)
    parts: list[str] = []

    # Walk recursively, sorted for deterministic ordering
    for file_path in sorted(folder.rglob("*")):
        if not file_path.is_file():
            continue
        if file_path.suffix.lower() not in _TEXT_EXTENSIONS:
            continue
        if file_path.name == ".siloignore":
            continue

        rel_path = str(file_path.relative_to(folder))
        if _is_ignored(rel_path, ignore_patterns):
            continue

        try:
            enforce_file_size(file_path)
            text = file_path.read_text(encoding="utf-8")
            parts.append(f"## {rel_path}\n{text}")
        except FileTooLargeError as exc:
            # One oversized file in a silo folder must not break loading
            # of the others.  Skip with a warning so operators can see
            # that something was excluded from the prompt.
            logger.warning(
                "knowledge.silo_read_skipped",
                file=str(file_path),
                size=exc.size,
                max_bytes=exc.max_bytes,
            )
        except (OSError, UnicodeDecodeError) as e:
            logger.warning("knowledge.silo_read_error", file=str(file_path), error=str(e))

    return "\n\n".join(parts)


def _load_siloignore(folder: Path) -> list[str]:
    """Load ignore patterns from ``.siloignore`` if it exists.

    Format: one glob pattern per line. Lines starting with ``#`` are comments.
    """
    ignore_file = folder / ".siloignore"
    if not ignore_file.exists():
        return []

    patterns: list[str] = []
    for raw_line in ignore_file.read_text().splitlines():
        stripped = raw_line.strip()
        if stripped and not stripped.startswith("#"):
            patterns.append(stripped)
    return patterns


def _is_ignored(rel_path: str, patterns: list[str]) -> bool:
    """Check if a relative path matches any ignore pattern (fnmatch glob)."""
    return any(fnmatch.fnmatch(rel_path, pattern) for pattern in patterns)


def apply_silo_updates(
    updates: list[dict[str, Any]],
    silos: list[dict[str, Any]],
) -> None:
    """Apply LLM-requested file modifications to writable folder silos.

    Each update dict has:
        - ``silo``: Name of the target silo
        - ``action``: ``"add"`` | ``"modify"`` | ``"delete"``
        - ``filename``: Target filename within the silo folder
        - ``content``: File content (for add/modify actions)

    Validates:
        - Target silo exists and has ``permissions="read_write"``
        - Filename has no path traversal (``../``)
        - Action is one of the allowed values
    """
    # Build lookup of writable folder silos
    writable: dict[str, Path] = {}
    for silo in silos:
        if silo.get("type") == "folder" and silo.get("permissions") == "read_write":
            writable[silo["name"]] = Path(silo["path"])

    for update in updates:
        silo_name = update.get("silo", "")
        action = update.get("action", "")
        filename = update.get("filename", "")
        content = update.get("content", "")

        # Validate silo is writable
        if silo_name not in writable:
            logger.warning(
                "knowledge.silo_update_denied",
                silo=silo_name,
                reason="not writable or not found",
            )
            continue

        # Validate filename — no path traversal
        if ".." in filename or filename.startswith("/"):
            logger.warning(
                "knowledge.silo_update_denied",
                silo=silo_name,
                filename=filename,
                reason="path traversal",
            )
            continue

        folder = writable[silo_name]
        target = folder / filename

        # Ensure resolved path is within the silo folder
        try:
            target.resolve().relative_to(folder.resolve())
        except ValueError:
            logger.warning(
                "knowledge.silo_update_denied",
                silo=silo_name,
                filename=filename,
                reason="path escapes silo",
            )
            continue

        if action == "add":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            logger.info("knowledge.silo_file_added", silo=silo_name, file=filename)

        elif action == "modify":
            if not target.exists():
                logger.warning(
                    "knowledge.silo_update_skipped",
                    silo=silo_name,
                    filename=filename,
                    reason="file not found for modify",
                )
                continue
            target.write_text(content, encoding="utf-8")
            logger.info("knowledge.silo_file_modified", silo=silo_name, file=filename)

        elif action == "delete":
            if target.exists():
                target.unlink()
                logger.info("knowledge.silo_file_deleted", silo=silo_name, file=filename)

        else:
            logger.warning(
                "knowledge.silo_update_skipped",
                silo=silo_name,
                action=action,
                reason="unknown action",
            )
