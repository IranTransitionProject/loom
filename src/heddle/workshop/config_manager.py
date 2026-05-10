"""
ConfigManager — CRUD for worker and pipeline YAML configs.

Manages the filesystem-based worker and pipeline configs, with optional
version tracking via WorkshopDB.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
import yaml

from heddle.core.config import load_config, validate_pipeline_config, validate_worker_config

if TYPE_CHECKING:
    from heddle.workshop.db import WorkshopDB

logger = structlog.get_logger()


# Worker / pipeline names flow from URL paths and form fields straight
# into a filesystem path (``configs/workers/{name}.yaml``).  Without
# validation a value like ``"../../etc/cron.d/evil"`` would write
# outside ``configs_dir``.  The allowed set is alphanumerics plus
# underscore and hyphen, no dots, slashes, or backslashes — which
# matches every shipped worker config name (``summarizer``, ``qa``,
# ``rag_vectorstore_lance``, ``_subprocess_template``) and rejects
# every traversal payload.
_VALID_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_config_name(name: str) -> None:
    r"""Reject worker/pipeline names that could escape ``configs_dir``.

    Names flow from URL paths and form fields directly into filesystem
    paths.  This rejects empty strings, path separators (``/``, ``\``),
    parent traversal (``..``), null bytes, leading dots, and anything
    not in ``[A-Za-z0-9_-]``.

    Why a strict allowlist and not just a "no slash" check: ``Path``
    happily resolves ``"a/b"`` and ``"..\.."`` into useful traversals
    on different platforms.  An allowlist is the only check that holds
    on every OS without having to enumerate every blocked sequence.
    """
    if not isinstance(name, str) or not _VALID_NAME_RE.fullmatch(name):
        raise ValueError(
            f"Invalid config name: {name!r}. "
            "Names must match ^[A-Za-z0-9_-]+$ "
            "(no path separators, no '..', no leading dots)."
        )


class ConfigManager:
    """CRUD operations for worker and pipeline YAML configs.

    Args:
        configs_dir: Root directory containing ``workers/`` and ``orchestrators/``.
        db: Optional WorkshopDB for version tracking.
    """

    def __init__(
        self,
        configs_dir: str = "configs/",
        db: WorkshopDB | None = None,
        extra_config_dirs: list[Path] | None = None,
    ) -> None:
        self.configs_dir = Path(configs_dir)
        self.db = db
        self.extra_config_dirs: list[Path] = extra_config_dirs or []

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------

    def list_workers(self) -> list[dict[str, Any]]:
        """List all worker configs (excluding _template.yaml).

        Scans the main ``configs_dir/workers/`` and all extra config dirs
        from deployed apps. Each result includes an ``app_name`` field
        (empty string for base configs, app name for deployed apps).
        """
        results = []
        # Scan main configs dir
        results.extend(self._scan_workers_dir(self.configs_dir / "workers"))
        # Scan deployed app config dirs
        for extra_dir in self.extra_config_dirs:
            app_name = extra_dir.parent.name if extra_dir.name == "configs" else extra_dir.name
            results.extend(self._scan_workers_dir(extra_dir / "workers", app_name=app_name))
        return results

    def _scan_workers_dir(self, workers_dir: Path, app_name: str = "") -> list[dict[str, Any]]:
        """Scan a workers directory for YAML configs."""
        if not workers_dir.exists():
            return []
        results = []
        for path in sorted(workers_dir.glob("*.yaml")):
            if path.name.startswith("_"):
                continue
            try:
                cfg = load_config(str(path))
                results.append(
                    {
                        "name": cfg.get("name", path.stem),
                        "description": cfg.get("description", ""),
                        "path": str(path),
                        "default_model_tier": cfg.get("default_model_tier", ""),
                        "worker_kind": cfg.get("worker_kind", "llm"),
                        "app_name": app_name,
                    }
                )
            except Exception as e:
                logger.warning("config.load_failed", path=str(path), error=str(e))
        return results

    def _resolve_worker_path(self, name: str) -> Path:
        """Find the worker YAML for ``name`` across base + extra config dirs.

        Search order: base ``configs_dir/workers/`` first, then each
        ``extra_config_dirs`` entry's ``workers/`` subdirectory in the
        order they were registered.  First match wins.  Deploy-time
        collision rejection (see ``AppManager.deploy_app``) means a
        well-formed environment never has two paths to choose from, but
        the deterministic base-first order is the documented fallback.

        Raises:
            FileNotFoundError: If the config exists in none of the dirs.
        """
        candidates = [self.configs_dir / "workers" / f"{name}.yaml"]
        candidates.extend(extra / "workers" / f"{name}.yaml" for extra in self.extra_config_dirs)
        for path in candidates:
            if path.exists():
                return path
        raise FileNotFoundError(
            f"Worker config not found: {name!r} (searched {[str(c) for c in candidates]})"
        )

    def get_worker(self, name: str) -> dict[str, Any]:
        """Load a worker config by name.

        Searches the base ``configs_dir`` first, then each registered
        deployed-app config dir.  This matches ``list_workers`` so that
        configs the operator sees in the list view are also openable in
        detail / test / eval views.

        Raises:
            ValueError: If ``name`` is not a safe filename (path traversal).
            FileNotFoundError: If the config doesn't exist in any source.
        """
        _validate_config_name(name)
        return load_config(str(self._resolve_worker_path(name)))

    def get_worker_yaml(self, name: str) -> str:
        """Get raw YAML content for a worker config.

        Searches base + extra config dirs (see ``get_worker``).

        Raises:
            ValueError: If ``name`` is not a safe filename (path traversal).
            FileNotFoundError: If the config doesn't exist in any source.
        """
        _validate_config_name(name)
        return self._resolve_worker_path(name).read_text()

    def save_worker(
        self,
        name: str,
        config: dict[str, Any],
        description: str | None = None,
    ) -> list[str]:
        """Validate and save a worker config.

        Returns list of validation errors (empty = success).
        Also saves a version to DB if available.

        Raises:
            ValueError: If ``name`` is not a safe filename (path traversal).
        """
        _validate_config_name(name)
        errors = validate_worker_config(config)
        if errors:
            return errors

        workers_dir = self.configs_dir / "workers"
        workers_dir.mkdir(parents=True, exist_ok=True)

        config_yaml = yaml.dump(config, default_flow_style=False, sort_keys=False)
        path = workers_dir / f"{name}.yaml"
        path.write_text(config_yaml)

        if self.db:
            self.db.save_worker_version(name, config_yaml, description)

        logger.info("config.worker_saved", name=name, path=str(path))
        return []

    def clone_worker(self, source_name: str, new_name: str) -> list[str]:
        """Clone a worker config with a new name.

        Both ``source_name`` and ``new_name`` are validated; the inner
        calls re-validate so this is defence-in-depth.
        """
        _validate_config_name(source_name)
        _validate_config_name(new_name)
        config = self.get_worker(source_name)
        config["name"] = new_name
        return self.save_worker(new_name, config, description=f"Cloned from {source_name}")

    def delete_worker(self, name: str) -> None:
        """Delete a worker config file.

        Raises:
            ValueError: If ``name`` is not a safe filename (path traversal).
            FileNotFoundError: If the config doesn't exist.
        """
        _validate_config_name(name)
        path = self.configs_dir / "workers" / f"{name}.yaml"
        if path.exists():
            path.unlink()
            logger.info("config.worker_deleted", name=name)
        else:
            raise FileNotFoundError(f"Worker config not found: {path}")

    def get_worker_version_history(self, name: str) -> list[dict[str, Any]]:
        """Get version history from DB (empty if no DB).

        Raises:
            ValueError: If ``name`` is not a safe filename (path traversal).
        """
        _validate_config_name(name)
        if self.db:
            return self.db.get_worker_versions(name)
        return []

    # ------------------------------------------------------------------
    # Pipelines
    # ------------------------------------------------------------------

    def list_pipelines(self) -> list[dict[str, Any]]:
        """List all pipeline configs.

        Scans the main ``configs_dir/orchestrators/`` and all extra config dirs
        from deployed apps.
        """
        results = []
        results.extend(self._scan_pipelines_dir(self.configs_dir / "orchestrators"))
        for extra_dir in self.extra_config_dirs:
            app_name = extra_dir.parent.name if extra_dir.name == "configs" else extra_dir.name
            results.extend(self._scan_pipelines_dir(extra_dir / "orchestrators", app_name=app_name))
        return results

    def _scan_pipelines_dir(self, orch_dir: Path, app_name: str = "") -> list[dict[str, Any]]:
        """Scan an orchestrators directory for pipeline configs."""
        if not orch_dir.exists():
            return []
        results = []
        for path in sorted(orch_dir.glob("*.yaml")):
            try:
                cfg = load_config(str(path))
                stage_count = len(cfg.get("pipeline_stages", []))
                results.append(
                    {
                        "name": cfg.get("name", path.stem),
                        "path": str(path),
                        "stage_count": stage_count,
                        "has_pipeline_stages": stage_count > 0,
                        "app_name": app_name,
                    }
                )
            except Exception as e:
                logger.warning("config.load_failed", path=str(path), error=str(e))
        return results

    def _resolve_pipeline_path(self, name: str) -> Path:
        """Find the pipeline YAML for ``name`` across base + extra config dirs.

        Same source-aware lookup as ``_resolve_worker_path``: base wins,
        deployed apps fall back in registration order.

        Raises:
            FileNotFoundError: If the config exists in none of the dirs.
        """
        candidates = [self.configs_dir / "orchestrators" / f"{name}.yaml"]
        candidates.extend(
            extra / "orchestrators" / f"{name}.yaml" for extra in self.extra_config_dirs
        )
        for path in candidates:
            if path.exists():
                return path
        raise FileNotFoundError(
            f"Pipeline config not found: {name!r} (searched {[str(c) for c in candidates]})"
        )

    def get_pipeline(self, name: str) -> dict[str, Any]:
        """Load a pipeline config by name.

        Searches the base ``configs_dir`` first, then each registered
        deployed-app config dir.  Symmetric with ``list_pipelines`` so
        that pipelines the operator sees in the list view are also
        openable in the editor / graph views.

        Raises:
            ValueError: If ``name`` is not a safe filename (path traversal).
            FileNotFoundError: If the config doesn't exist in any source.
        """
        _validate_config_name(name)
        return load_config(str(self._resolve_pipeline_path(name)))

    def existing_config_stems(self) -> dict[str, set[str]]:
        """Return every visible config filename stem, grouped by kind.

        Result shape: ``{"workers": {...}, "pipelines": {...}}`` covering
        the base ``configs_dir`` plus every registered extra dir.  Used
        by the ``/apps/deploy`` route to compute the set of names that
        ``AppManager.deploy_app`` should reject as collisions.  Returns
        the file stem (without the ``.yaml`` suffix) because that is the
        lookup key on every workshop URL (e.g. ``/workers/foo``).
        """
        workers: set[str] = set()
        pipelines: set[str] = set()
        for base in [self.configs_dir, *self.extra_config_dirs]:
            workers_dir = base / "workers"
            if workers_dir.exists():
                workers.update(
                    p.stem for p in workers_dir.glob("*.yaml") if not p.name.startswith("_")
                )
            orch_dir = base / "orchestrators"
            if orch_dir.exists():
                pipelines.update(p.stem for p in orch_dir.glob("*.yaml"))
        return {"workers": workers, "pipelines": pipelines}

    def save_pipeline(self, name: str, config: dict[str, Any]) -> list[str]:
        """Validate and save a pipeline config.

        Returns list of validation errors (empty = success).

        Raises:
            ValueError: If ``name`` is not a safe filename (path traversal).
        """
        _validate_config_name(name)
        errors = validate_pipeline_config(config)
        if errors:
            return errors

        orch_dir = self.configs_dir / "orchestrators"
        orch_dir.mkdir(parents=True, exist_ok=True)

        config_yaml = yaml.dump(config, default_flow_style=False, sort_keys=False)
        path = orch_dir / f"{name}.yaml"
        path.write_text(config_yaml)

        logger.info("config.pipeline_saved", name=name, path=str(path))
        return []
