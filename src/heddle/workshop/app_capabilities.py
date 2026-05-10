"""
App-bundle capability extractor — surface powerful behaviors at deploy time.

Used by the ``/apps/deploy`` preview flow (D2 in
``REPOSITORY_REVIEW_2026-05-08.md`` §10) to give an operator a concrete
list of what a candidate app can do BEFORE the bundle is committed to
disk.  Validation in ``app_manager.py`` already rejects ZIPs that escape
the extraction directory, but a well-formed app config can still ask the
worker mesh to:

  - import and execute arbitrary Python (``processing_backend``,
    ``backend_class``)
  - read or write filesystem paths outside the app directory
    (``workspace_dir``, ``knowledge_silos`` of folder type,
    ``knowledge_sources`` paths, ``resolve_file_refs``)
  - bind network ports (MCP ``host`` / ``port``)
  - reference environment variables (``${VAR}`` interpolation)

The extractor reads referenced YAML configs straight from the in-memory
ZipFile (no disk side-effects) and returns a list of ``CapabilityFinding``
records grouped by category.  The preview template renders these with
warning styling so the operator can either confirm or cancel.

This file intentionally errs on the side of "show too much" rather than
"show too little" — every finding here is something the operator should
explicitly endorse, not silently allow.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog
import yaml

if TYPE_CHECKING:
    import zipfile

    from heddle.core.manifest import AppManifest

logger = structlog.get_logger()

# Categories used by the preview template's section ordering.  Each
# corresponds to a distinct trust boundary the operator should think
# about separately.
CATEGORY_CODE = "code_execution"
CATEGORY_FILESYSTEM = "filesystem"
CATEGORY_NETWORK = "network"
CATEGORY_ENV = "env_var"

_ENV_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}|\$([A-Z_][A-Z0-9_]*)")


@dataclass
class CapabilityFinding:
    """One concrete capability surfaced from an app config file."""

    config_file: str
    category: str
    summary: str
    detail: str | None = None


@dataclass
class CapabilitySummary:
    """Grouped list of findings + the original manifest for templating."""

    manifest: AppManifest
    findings: list[CapabilityFinding] = field(default_factory=list)

    def by_category(self) -> dict[str, list[CapabilityFinding]]:
        """Group findings into ``{category: [findings...]}`` for rendering."""
        out: dict[str, list[CapabilityFinding]] = {}
        for f in self.findings:
            out.setdefault(f.category, []).append(f)
        return out

    @property
    def has_findings(self) -> bool:
        """True if any capability was surfaced; drives template rendering."""
        return bool(self.findings)


def _collect_env_refs(value: Any, out: set[str]) -> None:
    """Walk ``value`` recursively, capturing every ``${VAR}`` / ``$VAR`` it sees."""
    if isinstance(value, str):
        for m in _ENV_VAR_RE.finditer(value):
            out.add(m.group(1) or m.group(2))
    elif isinstance(value, dict):
        for v in value.values():
            _collect_env_refs(v, out)
    elif isinstance(value, list):
        for v in value:
            _collect_env_refs(v, out)


def _scan_worker_config(cfg: dict[str, Any], path: str) -> list[CapabilityFinding]:
    """Surface code/filesystem capabilities from a worker config dict."""
    out: list[CapabilityFinding] = []

    backend = cfg.get("processing_backend")
    if isinstance(backend, str) and backend:
        out.append(
            CapabilityFinding(
                config_file=path,
                category=CATEGORY_CODE,
                summary=f"processing_backend imports and executes: {backend}",
                detail=(
                    "The Heddle runtime will import this dotted path and call its "
                    "process() method. A malicious value can run arbitrary Python."
                ),
            )
        )

    backend_cls = cfg.get("backend_class")
    if isinstance(backend_cls, str) and backend_cls:
        out.append(
            CapabilityFinding(
                config_file=path,
                category=CATEGORY_CODE,
                summary=f"backend_class imports: {backend_cls}",
            )
        )

    workspace = cfg.get("workspace_dir") or (cfg.get("backend_config") or {}).get("workspace_dir")
    if isinstance(workspace, str) and workspace:
        out.append(
            CapabilityFinding(
                config_file=path,
                category=CATEGORY_FILESYSTEM,
                summary=f"workspace_dir: {workspace}",
                detail="Worker reads/writes files under this directory.",
            )
        )

    out.extend(
        CapabilityFinding(
            config_file=path,
            category=CATEGORY_FILESYSTEM,
            summary=f"knowledge_source: {src['path']}",
        )
        for src in cfg.get("knowledge_sources", []) or []
        if isinstance(src, dict) and isinstance(src.get("path"), str)
    )

    for silo in cfg.get("knowledge_silos", []) or []:
        if not isinstance(silo, dict):
            continue
        silo_type = silo.get("type", "")
        silo_name = silo.get("name", "?")
        silo_path = silo.get("path") or silo.get("folder")
        if silo_type.startswith("folder_") and isinstance(silo_path, str):
            mode = "read+write" if "readwrite" in silo_type else "read-only"
            out.append(
                CapabilityFinding(
                    config_file=path,
                    category=CATEGORY_FILESYSTEM,
                    summary=f"knowledge_silo {silo_name!r} ({mode}): {silo_path}",
                )
            )
        elif silo_type == "tool":
            out.append(
                CapabilityFinding(
                    config_file=path,
                    category=CATEGORY_CODE,
                    summary=f"knowledge_silo tool: {silo_name}",
                    detail="Exposes a callable tool the LLM can invoke.",
                )
            )

    refs = cfg.get("resolve_file_refs")
    if isinstance(refs, list) and refs:
        out.append(
            CapabilityFinding(
                config_file=path,
                category=CATEGORY_FILESYSTEM,
                summary=f"resolve_file_refs reads files for fields: {sorted(str(r) for r in refs)}",
            )
        )

    return out


def _scan_mcp_config(cfg: dict[str, Any], path: str) -> list[CapabilityFinding]:
    """Surface network / tool exposure from an MCP gateway config."""
    out: list[CapabilityFinding] = []

    host = cfg.get("host")
    port = cfg.get("port")
    if host or port:
        bind = f"{host or 'localhost'}:{port or '?'}"
        out.append(
            CapabilityFinding(
                config_file=path,
                category=CATEGORY_NETWORK,
                summary=f"MCP gateway binds: {bind}",
                detail="Anyone reaching this address can invoke the exposed tools.",
            )
        )

    tools = cfg.get("tools")
    if isinstance(tools, list) and tools:
        names = [t.get("name", "?") if isinstance(t, dict) else str(t) for t in tools]
        out.append(
            CapabilityFinding(
                config_file=path,
                category=CATEGORY_CODE,
                summary=f"MCP exposes tools: {names}",
            )
        )

    workspace = cfg.get("workspace_dir")
    if isinstance(workspace, str) and workspace:
        out.append(
            CapabilityFinding(
                config_file=path,
                category=CATEGORY_FILESYSTEM,
                summary=f"MCP workspace_dir: {workspace}",
            )
        )

    return out


def _read_yaml_member(zf: zipfile.ZipFile, member: str) -> dict[str, Any] | None:
    """Read and YAML-parse a ZIP member; return None on any failure."""
    try:
        raw = zf.read(member)
    except KeyError:
        logger.warning("app_capabilities.member_missing", member=member)
        return None
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        logger.warning("app_capabilities.yaml_parse_failed", member=member, error=str(e))
        return None
    return parsed if isinstance(parsed, dict) else None


def summarize_capabilities(zf: zipfile.ZipFile, manifest: AppManifest) -> CapabilitySummary:
    """Read every config referenced by the manifest and surface powerful fields.

    The ZipFile is read but not extracted — call this before committing
    the bundle to disk.  Returns a ``CapabilitySummary`` whose
    ``findings`` list is safe to render; an empty list means no risky
    fields were detected (which the preview template should still render
    so the operator confirms intentionally rather than implicitly).
    """
    summary = CapabilitySummary(manifest=manifest)
    env_refs: set[str] = set()

    for ref in manifest.entry_configs.workers:
        cfg = _read_yaml_member(zf, ref.config)
        if cfg is None:
            continue
        summary.findings.extend(_scan_worker_config(cfg, ref.config))
        _collect_env_refs(cfg, env_refs)

    for ref in manifest.entry_configs.mcp:
        cfg = _read_yaml_member(zf, ref.config)
        if cfg is None:
            continue
        summary.findings.extend(_scan_mcp_config(cfg, ref.config))
        _collect_env_refs(cfg, env_refs)

    # Pipeline + scheduler configs: scan for env refs (their fields don't
    # otherwise carry direct execute/filesystem powers — those live in
    # the workers they orchestrate).
    for ref in (*manifest.entry_configs.pipelines, *manifest.entry_configs.schedulers):
        cfg = _read_yaml_member(zf, ref.config)
        if cfg is not None:
            _collect_env_refs(cfg, env_refs)

    if env_refs:
        summary.findings.append(
            CapabilityFinding(
                config_file="(any)",
                category=CATEGORY_ENV,
                summary=f"References {len(env_refs)} env var(s): {sorted(env_refs)}",
                detail=(
                    "These environment variables are read at runtime; their "
                    "values reach the configured backends."
                ),
            )
        )

    return summary
