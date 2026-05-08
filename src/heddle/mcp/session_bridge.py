"""Session bridge — in-process dispatch for session management MCP tools.

Provides session lifecycle operations (start, end, status, sync-check, sync)
as MCP tool actions.  Operations execute git commands and file checks via
subprocess — no NATS required.

Configuration is passed from the MCP gateway YAML ``tools.session`` section:

.. code-block:: yaml

    tools:
      session:
        framework_dir: /path/to/framework
        workspace_dir: /path/to/baft/itp-workspace
        baft_dir: /path/to/baft
        nats_url: nats://localhost:4222
        ollama_url: http://localhost:11434
        lm_studio_url: http://localhost:1234/v1
        enable: [start, end, status, sync_check, sync]
"""

from __future__ import annotations

import datetime as dt
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, ClassVar

import structlog

logger = structlog.get_logger()


class SessionBridgeError(Exception):
    """Raised when a session bridge operation fails."""


class SessionBridge:
    """Dispatch session management operations in-process."""

    def __init__(
        self,
        *,
        framework_dir: str | Path,
        workspace_dir: str | Path,
        baft_dir: str | Path | None = None,
        nats_url: str = "nats://localhost:4222",
        ollama_url: str = "http://localhost:11434",
        lm_studio_url: str | None = None,
    ) -> None:
        self.framework_dir = Path(framework_dir)
        self.workspace_dir = Path(workspace_dir)
        self.baft_dir = Path(baft_dir) if baft_dir else None
        self.nats_url = nats_url
        self.ollama_url = ollama_url
        self.lm_studio_url = lm_studio_url

    _HANDLERS: ClassVar[dict[str, str]] = {
        "start": "_session_start",
        "end": "_session_end",
        "status": "_session_status",
        "sync_check": "_session_sync_check",
        "sync": "_session_sync",
    }

    async def dispatch(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Route to the appropriate session handler."""
        handler_name = self._HANDLERS.get(action)
        if handler_name is None:
            raise SessionBridgeError(
                f"Unknown session action: {action}. Available: {sorted(self._HANDLERS)}"
            )
        handler = getattr(self, handler_name)
        return await handler(arguments)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _git(  # pragma: no cover — mocked in tests
        self, args: list[str], cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.framework_dir),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def _check_nats(self) -> tuple[bool, str]:  # pragma: no cover — mocked in tests
        host_port = self.nats_url.replace("nats://", "").split("/")[0]
        parts = host_port.split(":")
        host = parts[0]
        port = int(parts[1]) if len(parts) > 1 else 4222
        try:
            sock = socket.create_connection((host, port), timeout=3)
            sock.close()
            return True, f"NATS reachable ({host}:{port})"
        except (OSError, TimeoutError):
            return False, f"NATS unreachable at {host}:{port}"

    # TODO(local-runtime-registry): the parallel ``_check_ollama`` /
    # ``_check_lm_studio`` methods below should collapse into a single
    # loop over the planned ``LocalRuntime`` registry in
    # ``heddle.worker.backends``.  Each runtime entry would carry its
    # own probe; this class would just iterate and call probe(url).

    def _check_ollama(self) -> tuple[bool, str]:  # pragma: no cover — mocked in tests
        try:
            import httpx

            resp = httpx.get(f"{self.ollama_url}/api/tags", timeout=5)
            if resp.status_code == 200:
                models = [m["name"] for m in resp.json().get("models", [])]
                return True, f"Ollama reachable ({', '.join(models[:3])})"
            return True, f"Ollama returned {resp.status_code}"
        except Exception:
            return False, f"Ollama unreachable at {self.ollama_url}"

    def _check_lm_studio(self) -> tuple[bool, str]:  # pragma: no cover — mocked in tests
        if not self.lm_studio_url:
            return False, "LM Studio not configured"
        try:
            import httpx

            url = self.lm_studio_url.rstrip("/")
            if not url.endswith("/v1"):
                url = f"{url}/v1"
            resp = httpx.get(f"{url}/models", timeout=5)
            if resp.status_code == 200:
                models = [m["id"] for m in resp.json().get("data", [])]
                return True, f"LM Studio reachable ({', '.join(models[:3])})"
            return True, f"LM Studio returned {resp.status_code}"
        except Exception:
            return False, f"LM Studio unreachable at {self.lm_studio_url}"

    def _check_duckdb(self) -> tuple[bool, str]:  # pragma: no cover — mocked in tests
        db_path = self.workspace_dir / "itp.duckdb"
        if not db_path.exists():
            return False, f"DuckDB not found at {db_path}"
        stat = db_path.stat()
        size_mb = stat.st_size / (1024 * 1024)
        age_h = (time.time() - stat.st_mtime) / 3600
        return True, f"DuckDB exists ({size_mb:.1f} MB, {age_h:.0f}h old)"

    def _generate_session_id(self) -> str:
        return dt.datetime.now(tz=dt.UTC).strftime("session-%Y%m%d-%H%M%S")

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _session_start(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Pull framework, import DuckDB, check services, register."""
        from heddle.mcp.session_registry import (
            register_session,
        )

        sid = arguments.get("session_id") or self._generate_session_id()
        steps: list[dict[str, Any]] = []

        # 1. Pull framework
        fw = self.framework_dir
        if (fw / ".git").is_dir():
            pull = self._git(["pull", "--ff-only"])
            if pull.returncode != 0:
                return {
                    "error": f"Framework pull failed: {pull.stderr.strip()}",
                    "session_id": sid,
                    "steps": steps,
                }
            head = self._git(["rev-parse", "--short", "HEAD"])
            commit = head.stdout.strip() if head.returncode == 0 else "?"
            steps.append({"step": "pull", "ok": True, "commit": commit})
        else:
            steps.append({"step": "pull", "ok": False, "reason": "not a git repo"})

        # 2. Incremental DuckDB import
        if self.baft_dir:
            script = self.baft_dir / "pipeline" / "scripts" / "itp_import_to_duckdb.py"
            if script.exists():  # pragma: no cover — subprocess tested via mock
                result = subprocess.run(
                    ["uv", "run", "python", str(script), "--incremental"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    cwd=str(self.baft_dir),
                    check=False,
                )
                steps.append({"step": "duckdb_import", "ok": result.returncode == 0})
            else:
                steps.append(
                    {
                        "step": "duckdb_import",
                        "ok": False,
                        "reason": "script not found",
                    }
                )

        # 3. Service checks
        nats_ok, nats_msg = self._check_nats()
        steps.append({"step": "nats", "ok": nats_ok, "message": nats_msg})

        ollama_ok, ollama_msg = self._check_ollama()
        steps.append({"step": "ollama", "ok": ollama_ok, "message": ollama_msg})

        if not nats_ok:
            return {
                "error": "NATS is required but unreachable",
                "session_id": sid,
                "steps": steps,
            }

        # 4. Register session
        register_session(sid)
        steps.append({"step": "register", "ok": True})

        return {"session_id": sid, "status": "active", "steps": steps}

    async def _session_end(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Unregister session, commit framework, push."""
        from heddle.mcp.session_registry import (
            get_active_sessions,
            unregister_session,
        )

        sid = arguments.get("session_id")
        if not sid:
            active = get_active_sessions()
            if not active:
                return {"status": "no_active_sessions"}
            sid = active[0]["session_id"]

        message = arguments.get("message", "analytical session updates")

        # Unregister
        unregister_session(sid)

        # Commit framework
        fw = self.framework_dir
        committed = False
        push_ok = False
        if (fw / ".git").is_dir():
            status = self._git(["status", "--porcelain"])
            if status.stdout.strip():
                date_str = dt.datetime.now(tz=dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                commit_msg = f"Session {sid}: {message} — {date_str}"
                # Stage only data/ (analytical content) + tracked-file updates
                self._git(["add", "data/"])
                self._git(["add", "-u"])
                commit = self._git(["commit", "-m", commit_msg])
                committed = commit.returncode == 0
                if committed:
                    push = self._git(["push"])
                    push_ok = push.returncode == 0

        return {
            "session_id": sid,
            "status": "ended",
            "committed": committed,
            "pushed": push_ok,
        }

    async def _session_status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Show active sessions and system health."""
        from heddle.mcp.session_registry import get_active_sessions

        sessions = get_active_sessions()

        # Framework git
        fw = self.framework_dir
        git_clean = None
        dirty_count = 0
        if (fw / ".git").is_dir():
            status = self._git(["status", "--porcelain"])
            dirty_count = len(status.stdout.strip().split("\n")) if status.stdout.strip() else 0
            git_clean = dirty_count == 0

        # Services
        nats_ok, nats_msg = self._check_nats()
        ollama_ok, ollama_msg = self._check_ollama()
        db_ok, db_msg = self._check_duckdb()

        services: dict[str, Any] = {
            "nats": {"ok": nats_ok, "message": nats_msg},
            "ollama": {"ok": ollama_ok, "message": ollama_msg},
            "duckdb": {"ok": db_ok, "message": db_msg},
        }
        if self.lm_studio_url:
            lms_ok, lms_msg = self._check_lm_studio()
            services["lm_studio"] = {"ok": lms_ok, "message": lms_msg}

        return {
            "sessions": sessions,
            "framework": {
                "git_clean": git_clean,
                "dirty_files": dirty_count if not git_clean else 0,
            },
            "services": services,
        }

    async def _session_sync_check(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Check if framework remote has new commits."""
        fw = self.framework_dir
        if not (fw / ".git").is_dir():
            return {"error": "Framework is not a git repo"}

        fetch = self._git(["fetch", "origin", "--quiet"])
        if fetch.returncode != 0:
            return {"error": f"Fetch failed: {fetch.stderr.strip()}"}

        behind = self._git(["rev-list", "--count", "HEAD..origin/main"])
        ahead = self._git(["rev-list", "--count", "origin/main..HEAD"])
        behind_n = int(behind.stdout.strip()) if behind.returncode == 0 else 0
        ahead_n = int(ahead.stdout.strip()) if ahead.returncode == 0 else 0

        if behind_n > 0 and ahead_n > 0:
            status = "diverged"
        elif behind_n > 0:
            status = "behind"
        elif ahead_n > 0:
            status = "ahead"
        else:
            status = "current"

        return {
            "status": status,
            "behind": behind_n,
            "ahead": ahead_n,
        }

    async def _session_sync(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Pull framework and run incremental DuckDB import."""
        fw = self.framework_dir
        if not (fw / ".git").is_dir():
            return {"error": "Framework is not a git repo"}

        pull = self._git(["pull", "--ff-only"])
        if pull.returncode != 0:
            return {"error": f"Pull failed: {pull.stderr.strip()}"}

        head = self._git(["rev-parse", "--short", "HEAD"])
        commit = head.stdout.strip() if head.returncode == 0 else "?"

        import_ok = True
        if self.baft_dir:
            script = self.baft_dir / "pipeline" / "scripts" / "itp_import_to_duckdb.py"
            if script.exists():  # pragma: no cover — subprocess tested via mock
                result = subprocess.run(
                    ["uv", "run", "python", str(script), "--incremental"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    cwd=str(self.baft_dir),
                    check=False,
                )
                import_ok = result.returncode == 0

        return {
            "status": "synced",
            "commit": commit,
            "duckdb_imported": import_ok,
        }
