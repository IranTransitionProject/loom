"""
AppManager — deploy, list, and remove Heddle app bundles.

An app bundle is a ZIP archive containing a ``manifest.yaml`` and a set of
config files (workers, pipelines, schedulers, MCP configs) plus optional
scripts.  Apps are extracted to ``~/.heddle/apps/{app_name}/`` and their
configs become visible in the Workshop alongside the base configs.

After deployment, a reload message is published to ``heddle.control.reload``
so running actors pick up new or changed configs without restart.

Security:
    - ZIP entries with ``..`` segments, absolute paths, or symlinks are
      rejected before any files are written to disk.
    - Extraction uses an atomic deploy strategy: files go to a temporary
      directory first and are moved into place only on success.  A failed
      deployment leaves the previous version intact.
    - Config paths referenced in the manifest are validated against path
      traversal (must stay within the app directory).
"""

from __future__ import annotations

import re
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from heddle.core.manifest import AppManifest, load_manifest, validate_app_manifest

if TYPE_CHECKING:
    from heddle.bus.base import MessageBus

logger = structlog.get_logger()

DEFAULT_APPS_DIR = "~/.heddle/apps"

# Hidden subdirectory under ``apps_dir`` where the deploy-preview flow
# parks uploaded ZIPs between the upload step and the operator's
# confirm/cancel decision.  Hidden so it doesn't appear in the deployed
# apps list (``list_apps`` iterates ``apps_dir`` directly).
STAGING_SUBDIR = ".staging"

# Tokens are uuid4 hex (32 lowercase hex chars).  Validated on every
# confirm/cancel call so a request-derived token cannot escape the
# staging directory or target an unrelated path.
_TOKEN_RE = re.compile(r"^[a-f0-9]{32}$")


def _validate_config_path(config_path: str) -> None:
    """Validate that a config path stays within the app boundary.

    Rejects paths that would resolve outside the app directory via
    ``..`` segments, absolute paths, or other traversal tricks.

    Raises:
        AppDeployError: If the path escapes the app directory.
    """
    # Normalize the path and check for traversal.
    normalized = Path(config_path).as_posix()
    if normalized.startswith("/"):
        msg = f"Config path is absolute: {config_path}"
        raise AppDeployError(msg)

    # Resolve against a synthetic root to detect traversal.
    from pathlib import PurePosixPath

    root = PurePosixPath("/app-root")
    resolved = (root / normalized).as_posix()
    if not resolved.startswith("/app-root/"):
        msg = f"Config path escapes app directory: {config_path}"
        raise AppDeployError(msg)


class AppDeployError(Exception):
    """Raised when app deployment fails."""


class AppManager:
    """Manages deployed Heddle app bundles.

    Args:
        apps_dir: Root directory for deployed apps (``~`` is expanded).
        bus: Optional message bus for publishing reload notifications.
    """

    def __init__(
        self,
        apps_dir: str = DEFAULT_APPS_DIR,
        bus: MessageBus | None = None,
    ) -> None:
        self.apps_dir = Path(apps_dir).expanduser()
        self.apps_dir.mkdir(parents=True, exist_ok=True)
        self._bus = bus

    def list_apps(self) -> list[AppManifest]:
        """List all deployed apps by reading their manifests.

        Skips dot-prefixed directories so the deploy-preview staging area
        (``.staging``) and any other hidden state does not appear in the
        deployed-apps list.
        """
        apps: list[AppManifest] = []
        if not self.apps_dir.exists():
            return apps
        for app_dir in sorted(self.apps_dir.iterdir()):
            if app_dir.name.startswith("."):
                continue
            manifest_path = app_dir / "manifest.yaml"
            if app_dir.is_dir() and manifest_path.exists():
                try:
                    apps.append(load_manifest(manifest_path))
                except Exception as e:
                    logger.warning(
                        "app_manager.manifest_load_failed",
                        app_dir=str(app_dir),
                        error=str(e),
                    )
        return apps

    def get_app(self, app_name: str) -> AppManifest:
        """Load a deployed app's manifest.

        Raises:
            FileNotFoundError: If the app is not deployed.
        """
        manifest_path = self.apps_dir / app_name / "manifest.yaml"
        return load_manifest(manifest_path)

    def get_app_configs_dir(self, app_name: str) -> Path:
        """Return the configs directory for a deployed app."""
        return self.apps_dir / app_name / "configs"

    @staticmethod
    def _validate_zip(zf: zipfile.ZipFile) -> AppManifest:
        """Validate ZIP structure, manifest, security, and return manifest.

        Raises ``AppDeployError`` on any validation failure.
        """
        import yaml

        names = zf.namelist()
        if "manifest.yaml" not in names:
            msg = "ZIP must contain manifest.yaml at the root"
            raise AppDeployError(msg)

        # Parse and validate manifest.
        manifest_data = yaml.safe_load(zf.read("manifest.yaml"))
        if not isinstance(manifest_data, dict):
            msg = "manifest.yaml must be a YAML mapping"
            raise AppDeployError(msg)

        errors = validate_app_manifest(manifest_data)
        if errors:
            msg = f"Invalid manifest: {'; '.join(errors)}"
            raise AppDeployError(msg)

        manifest = AppManifest(**manifest_data)

        # Security: reject paths that escape the extraction directory.
        for name in names:
            if name.startswith("/") or ".." in name:
                msg = f"ZIP contains unsafe path: {name}"
                raise AppDeployError(msg)

        # Security: reject symlinks in ZIP entries.
        for info in zf.infolist():
            # ZIP external_attr: upper 16 bits are Unix mode.
            # Symlinks have S_IFLNK (0o120000) in the mode bits.
            unix_mode = info.external_attr >> 16
            if unix_mode & 0o170000 == 0o120000:
                msg = f"ZIP contains symlink: {info.filename}"
                raise AppDeployError(msg)

        # Verify referenced config files exist and paths are safe.
        for ref_list in [
            manifest.entry_configs.workers,
            manifest.entry_configs.pipelines,
            manifest.entry_configs.schedulers,
            manifest.entry_configs.mcp,
        ]:
            for ref in ref_list:
                if ref.config not in names:
                    msg = f"Manifest references missing file: {ref.config}"
                    raise AppDeployError(msg)
                _validate_config_path(ref.config)

        return manifest

    def peek_manifest(self, zip_path: Path) -> AppManifest:
        """Validate a ZIP and return its parsed manifest without extracting.

        Used by the ``/apps/deploy`` route to read the manifest before
        committing to extraction — needed for the collision check (see
        ``deploy_app``) and the capability-preview flow (D2).  Same
        validation as ``deploy_app`` runs internally; calling
        ``deploy_app`` after ``peek_manifest`` will re-validate (cheap)
        rather than rely on the caller to remember.

        Raises:
            AppDeployError: If the ZIP is invalid or fails validation.
        """
        if not zip_path.exists():
            msg = f"ZIP file not found: {zip_path}"
            raise AppDeployError(msg)
        if not zipfile.is_zipfile(zip_path):
            msg = f"Not a valid ZIP file: {zip_path}"
            raise AppDeployError(msg)
        with zipfile.ZipFile(zip_path, "r") as zf:
            return self._validate_zip(zf)

    @staticmethod
    def _config_stems_from_manifest(manifest: AppManifest) -> dict[str, set[str]]:
        """Extract worker/pipeline filename stems referenced by a manifest.

        ``configs/workers/foo.yaml`` → ``foo``.  Stems are the lookup key
        on workshop URLs (``/workers/foo``), so collision detection
        operates on stems rather than full paths or YAML-internal names.
        """
        from pathlib import PurePosixPath

        return {
            "workers": {PurePosixPath(r.config).stem for r in manifest.entry_configs.workers},
            "pipelines": {PurePosixPath(r.config).stem for r in manifest.entry_configs.pipelines},
        }

    def deploy_app(
        self,
        zip_path: Path,
        *,
        forbidden_config_names: dict[str, set[str]] | None = None,
    ) -> AppManifest:
        """Deploy an app from a ZIP archive.

        Steps:
        1. Validate the ZIP structure, manifest, and security constraints
        2. Reject if any worker/pipeline filename collides with an existing
           config (passed in via ``forbidden_config_names``)
        3. Atomic extract: write to temp dir, move into place on success
        4. Return the parsed manifest

        If extraction fails, the previous deployment (if any) is preserved.

        Args:
            zip_path: Path to the ZIP file.
            forbidden_config_names: ``{"workers": {...}, "pipelines": {...}}``
                of filename stems already visible to the workshop from
                base configs and other deployed apps (the route layer
                should exclude this app's own existing stems so a
                redeploy doesn't collide with itself).  ``None`` skips
                the collision check entirely — only callers that own
                the namespace (tests, CLI tooling that knows what it is
                doing) should pass ``None``.

        Returns:
            The parsed AppManifest.

        Raises:
            AppDeployError: If the ZIP is invalid, names collide, or
                deployment fails.
        """
        if not zip_path.exists():
            msg = f"ZIP file not found: {zip_path}"
            raise AppDeployError(msg)

        if not zipfile.is_zipfile(zip_path):
            msg = f"Not a valid ZIP file: {zip_path}"
            raise AppDeployError(msg)

        with zipfile.ZipFile(zip_path, "r") as zf:
            manifest = self._validate_zip(zf)

            if forbidden_config_names is not None:
                new_stems = self._config_stems_from_manifest(manifest)
                worker_collisions = sorted(
                    new_stems["workers"] & forbidden_config_names.get("workers", set())
                )
                pipeline_collisions = sorted(
                    new_stems["pipelines"] & forbidden_config_names.get("pipelines", set())
                )
                if worker_collisions or pipeline_collisions:
                    parts = []
                    if worker_collisions:
                        parts.append(f"workers: {worker_collisions}")
                    if pipeline_collisions:
                        parts.append(f"pipelines: {pipeline_collisions}")
                    msg = (
                        f"App {manifest.name!r} cannot be deployed: name "
                        f"collisions with existing configs ({'; '.join(parts)}). "
                        "Rename one side before redeploying."
                    )
                    raise AppDeployError(msg)

            # Atomic deployment: extract to temp dir, then move into place.
            app_dir = self.apps_dir / manifest.name
            tmp_dir = None
            try:
                tmp_dir = Path(
                    tempfile.mkdtemp(
                        prefix=f".deploy-{manifest.name}-",
                        dir=self.apps_dir,
                    )
                )
                zf.extractall(tmp_dir)

                # Post-extraction: verify no symlinks on disk (defense-in-depth).
                for extracted in tmp_dir.rglob("*"):
                    if extracted.is_symlink():
                        msg = f"Extracted file is a symlink: {extracted.name}"
                        raise AppDeployError(msg)

                # Swap: remove old, move new into place.
                if app_dir.exists():
                    shutil.rmtree(app_dir)
                tmp_dir.rename(app_dir)
                tmp_dir = None  # Prevent cleanup in finally block.
            finally:
                if tmp_dir is not None and tmp_dir.exists():
                    shutil.rmtree(tmp_dir, ignore_errors=True)

        # Warn about Python packages that need manual install.
        if manifest.python_package:
            pkg = manifest.python_package
            install_dir = app_dir / pkg.install_path
            logger.warning(
                "app_manager.python_package_detected",
                app=manifest.name,
                package=pkg.name,
                hint=(
                    f"This app includes Python package '{pkg.name}'. "
                    f"Install it manually: pip install -e {install_dir}"
                ),
            )

        logger.info(
            "app_manager.deployed",
            app=manifest.name,
            version=manifest.version,
            app_dir=str(app_dir),
        )

        return manifest

    def remove_app(self, app_name: str) -> None:
        """Remove a deployed app.

        Raises:
            FileNotFoundError: If the app is not deployed.
        """
        app_dir = self.apps_dir / app_name
        if not app_dir.exists():
            raise FileNotFoundError(f"App not found: {app_name}")
        shutil.rmtree(app_dir)
        logger.info("app_manager.removed", app=app_name)

    # ------------------------------------------------------------------
    # Deploy-preview staging
    # ------------------------------------------------------------------

    def _staging_root(self) -> Path:
        """Return ``apps_dir/.staging``, creating it on first use."""
        root = self.apps_dir / STAGING_SUBDIR
        root.mkdir(parents=True, exist_ok=True)
        return root

    def stage_zip(self, zip_path: Path) -> str:
        """Move a verified ZIP into the staging area; return the lookup token.

        Used by the two-step deploy flow: the upload route saves the
        upload to a temp file, calls ``peek_manifest`` + collision check,
        then calls ``stage_zip`` to park the bytes between the preview
        render and the operator's confirm POST.  The temp file at
        ``zip_path`` is *moved*, not copied — caller should not re-use
        the path afterwards.

        Returns:
            Hex token identifying the staged bundle.  Pass this back to
            ``consume_staged`` or ``discard_staged``.
        """
        token = uuid.uuid4().hex
        staging_dir = self._staging_root() / token
        staging_dir.mkdir(parents=True)
        staged_zip = staging_dir / "app.zip"
        shutil.move(str(zip_path), staged_zip)
        return token

    def _staged_dir(self, token: str) -> Path:
        """Resolve ``token`` to its staging directory after format validation.

        Raises:
            AppDeployError: If the token format is invalid (path-traversal
                guard) or the staged bundle does not exist.
        """
        if not _TOKEN_RE.fullmatch(token):
            msg = f"Invalid staging token: {token!r}"
            raise AppDeployError(msg)
        staged = self._staging_root() / token
        if not staged.is_dir():
            msg = f"Staged bundle not found (already confirmed or expired): {token}"
            raise AppDeployError(msg)
        return staged

    def staged_zip_path(self, token: str) -> Path:
        """Return the on-disk path of a staged ZIP for further inspection."""
        return self._staged_dir(token) / "app.zip"

    def discard_staged(self, token: str) -> None:
        """Remove a staged bundle and its directory.  Idempotent on bad tokens."""
        try:
            staged = self._staged_dir(token)
        except AppDeployError:
            return
        shutil.rmtree(staged, ignore_errors=True)

    async def notify_reload(self) -> None:
        """Publish a reload control message to notify running actors.

        This is a best-effort notification — if no NATS bus is connected
        or no actors are running, the message is silently dropped.
        """
        if self._bus is None:
            logger.info("app_manager.reload_skipped", reason="no bus configured")
            return
        try:
            await self._bus.publish("heddle.control.reload", {"action": "reload"})
            logger.info("app_manager.reload_published")
        except Exception as e:
            logger.warning("app_manager.reload_failed", error=str(e))
