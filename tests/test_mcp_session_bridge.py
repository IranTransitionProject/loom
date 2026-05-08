"""Tests for heddle.mcp.session_bridge — session management MCP tools."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from heddle.mcp.session_bridge import SessionBridge, SessionBridgeError


@pytest.fixture
def bridge(tmp_path):
    """Create a SessionBridge with temp dirs."""
    fw = tmp_path / "framework"
    fw.mkdir()
    (fw / ".git").mkdir()
    ws = tmp_path / "workspace"
    ws.mkdir()
    baft = tmp_path / "baft"
    baft.mkdir()
    return SessionBridge(
        framework_dir=fw,
        workspace_dir=ws,
        baft_dir=baft,
        nats_url="nats://127.0.0.1:19999",
        ollama_url="http://127.0.0.1:19999",
    )


class TestDispatch:
    """Test dispatch routing."""

    @pytest.mark.asyncio
    async def test_unknown_action_raises(self, bridge):
        with pytest.raises(SessionBridgeError, match="Unknown session action"):
            await bridge.dispatch("nonexistent", {})

    @pytest.mark.asyncio
    async def test_known_actions_dispatch(self, bridge):
        """All 5 actions are routable."""
        for action in ("start", "end", "status", "sync_check", "sync"):
            assert action in bridge._HANDLERS


class TestSessionStart:
    """Tests for session.start."""

    @pytest.mark.asyncio
    async def test_start_returns_session_id(self, bridge):
        with (
            patch.object(bridge, "_git") as mock_git,
            patch.object(bridge, "_check_nats", return_value=(True, "ok")),
            patch.object(bridge, "_check_ollama", return_value=(True, "ok")),
            patch("heddle.mcp.session_registry.register_session"),
        ):
            mock_git.return_value = MagicMock(returncode=0, stdout="abc1234\n", stderr="")
            result = await bridge.dispatch("start", {})

        assert "session_id" in result
        assert result["status"] == "active"

    @pytest.mark.asyncio
    async def test_start_with_explicit_id(self, bridge):
        with (
            patch.object(bridge, "_git") as mock_git,
            patch.object(bridge, "_check_nats", return_value=(True, "ok")),
            patch.object(bridge, "_check_ollama", return_value=(True, "ok")),
            patch("heddle.mcp.session_registry.register_session"),
        ):
            mock_git.return_value = MagicMock(returncode=0, stdout="abc1234\n", stderr="")
            result = await bridge.dispatch("start", {"session_id": "my-sess"})

        assert result["session_id"] == "my-sess"

    @pytest.mark.asyncio
    async def test_start_pull_failure_returns_error(self, bridge):
        with patch.object(bridge, "_git") as mock_git:
            mock_git.return_value = MagicMock(returncode=1, stdout="", stderr="conflict")
            result = await bridge.dispatch("start", {})

        assert "error" in result
        assert "pull failed" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_start_nats_unreachable_returns_error(self, bridge):
        with (
            patch.object(bridge, "_git") as mock_git,
            patch.object(bridge, "_check_nats", return_value=(False, "down")),
            patch.object(bridge, "_check_ollama", return_value=(True, "ok")),
        ):
            mock_git.return_value = MagicMock(returncode=0, stdout="abc\n", stderr="")
            result = await bridge.dispatch("start", {})

        assert "error" in result
        assert "NATS" in result["error"]


class TestSessionEnd:
    """Tests for session.end."""

    @pytest.mark.asyncio
    async def test_end_with_changes(self, bridge):
        def mock_git_fn(args, cwd=None):
            m = MagicMock(returncode=0, stderr="")
            if args[0] == "status":
                m.stdout = "M data/file.yaml\n"
            else:
                m.stdout = ""
            return m

        with (
            patch.object(bridge, "_git", side_effect=mock_git_fn),
            patch("heddle.mcp.session_registry.unregister_session"),
            patch(
                "heddle.mcp.session_registry.get_active_sessions",
                return_value=[{"session_id": "s1"}],
            ),
        ):
            result = await bridge.dispatch("end", {})

        assert result["status"] == "ended"
        assert result["committed"] is True

    @pytest.mark.asyncio
    async def test_end_no_active_sessions(self, bridge):
        with patch(
            "heddle.mcp.session_registry.get_active_sessions",
            return_value=[],
        ):
            result = await bridge.dispatch("end", {})

        assert result["status"] == "no_active_sessions"


class TestSessionStatus:
    """Tests for session.status."""

    @pytest.mark.asyncio
    async def test_status_returns_structure(self, bridge):
        with (
            patch(
                "heddle.mcp.session_registry.get_active_sessions",
                return_value=[{"session_id": "s1"}],
            ),
            patch.object(bridge, "_git") as mock_git,
            patch.object(bridge, "_check_nats", return_value=(True, "ok")),
            patch.object(bridge, "_check_ollama", return_value=(True, "ok")),
            patch.object(bridge, "_check_duckdb", return_value=(True, "ok")),
        ):
            mock_git.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = await bridge.dispatch("status", {})

        assert "sessions" in result
        assert "services" in result
        assert result["sessions"] == [{"session_id": "s1"}]

    @pytest.mark.asyncio
    async def test_status_on_non_git_framework(self, tmp_path):
        """Framework directory without .git must not raise NameError.

        Pins the contract that ``_session_status`` returns ``git_clean=None``
        and ``dirty_files=0`` when the framework is not a git repo, rather
        than raising ``NameError`` on ``dirty_count``.
        """
        fw = tmp_path / "framework"
        fw.mkdir()  # intentionally NO .git subdirectory
        ws = tmp_path / "workspace"
        ws.mkdir()
        bridge = SessionBridge(
            framework_dir=fw,
            workspace_dir=ws,
            nats_url="nats://127.0.0.1:19999",
            ollama_url="http://127.0.0.1:19999",
        )

        with (
            patch(
                "heddle.mcp.session_registry.get_active_sessions",
                return_value=[],
            ),
            patch.object(bridge, "_check_nats", return_value=(False, "down")),
            patch.object(bridge, "_check_ollama", return_value=(False, "down")),
            patch.object(bridge, "_check_duckdb", return_value=(False, "down")),
        ):
            result = await bridge.dispatch("status", {})

        assert result["framework"]["git_clean"] is None
        assert result["framework"]["dirty_files"] == 0
        assert result["sessions"] == []
        # Adjacent contract: services dict still populated even when git path is short-circuited.
        assert set(result["services"]) == {"nats", "ollama", "duckdb"}


class TestSyncCheck:
    """Tests for session.sync_check."""

    @pytest.mark.asyncio
    async def test_sync_check_current(self, bridge):
        def mock_git_fn(args, cwd=None):
            m = MagicMock(returncode=0, stderr="")
            if "rev-list" in args:
                m.stdout = "0\n"
            else:
                m.stdout = ""
            return m

        with patch.object(bridge, "_git", side_effect=mock_git_fn):
            result = await bridge.dispatch("sync_check", {})

        assert result["status"] == "current"

    @pytest.mark.asyncio
    async def test_sync_check_behind(self, bridge):
        def mock_git_fn(args, cwd=None):
            m = MagicMock(returncode=0, stderr="")
            if args == ["rev-list", "--count", "HEAD..origin/main"]:
                m.stdout = "5\n"
            elif args == ["rev-list", "--count", "origin/main..HEAD"]:
                m.stdout = "0\n"
            else:
                m.stdout = ""
            return m

        with patch.object(bridge, "_git", side_effect=mock_git_fn):
            result = await bridge.dispatch("sync_check", {})

        assert result["status"] == "behind"
        assert result["behind"] == 5


class TestSync:
    """Tests for session.sync."""

    @pytest.mark.asyncio
    async def test_sync_pulls_and_reports_commit(self, bridge):
        def mock_git_fn(args, cwd=None):
            m = MagicMock(returncode=0, stderr="")
            if args == ["rev-parse", "--short", "HEAD"]:
                m.stdout = "def5678\n"
            else:
                m.stdout = ""
            return m

        with patch.object(bridge, "_git", side_effect=mock_git_fn):
            result = await bridge.dispatch("sync", {})

        assert result["status"] == "synced"
        assert result["commit"] == "def5678"

    @pytest.mark.asyncio
    async def test_sync_pull_conflict_returns_error(self, bridge):
        with patch.object(bridge, "_git") as mock_git:
            mock_git.return_value = MagicMock(returncode=1, stdout="", stderr="conflict")
            result = await bridge.dispatch("sync", {})

        assert "error" in result


class TestSessionDiscovery:
    """Tests for discover_session_tools."""

    def test_default_enables_all_five(self):
        from heddle.mcp.workshop_discovery import discover_session_tools

        tools = discover_session_tools({})
        names = {t["name"] for t in tools}
        assert names == {
            "session.start",
            "session.end",
            "session.status",
            "session.sync_check",
            "session.sync",
        }

    def test_selective_enable(self):
        from heddle.mcp.workshop_discovery import discover_session_tools

        tools = discover_session_tools({"enable": ["start", "status"]})
        names = {t["name"] for t in tools}
        assert names == {"session.start", "session.status"}

    def test_heddle_metadata_present(self):
        from heddle.mcp.workshop_discovery import discover_session_tools

        tools = discover_session_tools({"enable": ["start"]})
        assert tools[0]["_heddle"]["kind"] == "session"
        assert tools[0]["_heddle"]["action"] == "start"

    def test_read_only_flags(self):
        from heddle.mcp.workshop_discovery import discover_session_tools

        tools = discover_session_tools({})
        by_name = {t["name"]: t for t in tools}
        assert by_name["session.status"]["_heddle"].get("read_only") is True
        assert by_name["session.sync_check"]["_heddle"].get("read_only") is True
        assert "read_only" not in by_name["session.start"]["_heddle"]
