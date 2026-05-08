"""Tests for WorkspaceManager (file-ref resolution, path safety, JSON I/O)."""

import json

import pytest

from heddle.core.workspace import WorkspaceManager


@pytest.fixture
def workspace(tmp_path):
    """Provide an isolated temporary workspace directory."""
    return tmp_path


@pytest.fixture
def ws(workspace):
    """Create a WorkspaceManager pointing at the temporary workspace."""
    return WorkspaceManager(workspace)


# --- resolve() ---


class TestResolve:
    def test_resolve_valid_file(self, ws, workspace):
        """resolve() returns an absolute path for a valid file."""
        (workspace / "report.pdf").write_bytes(b"fake pdf")
        path = ws.resolve("report.pdf")
        assert path.is_absolute()
        assert path.name == "report.pdf"

    def test_resolve_subdirectory(self, ws, workspace):
        """resolve() works with files in subdirectories."""
        subdir = workspace / "subdir"
        subdir.mkdir()
        (subdir / "data.json").write_text("{}")
        path = ws.resolve("subdir/data.json")
        assert path.name == "data.json"

    def test_resolve_path_traversal_rejected(self, ws):
        """resolve() rejects path traversal attempts."""
        with pytest.raises(ValueError, match="Path traversal"):
            ws.resolve("../../etc/passwd")

    def test_resolve_sibling_prefix_traversal_rejected(self, tmp_path):
        """resolve() must reject paths that *string-prefix-match* the workspace
        but live in a sibling directory.

        Regression test for the original bug: when ``workspace_dir`` was
        ``/tmp/work``, a ``file_ref`` of ``../work-evil/secret`` resolved
        to ``/tmp/work-evil/secret`` whose string representation begins
        with ``/tmp/work``.  The previous ``str.startswith`` check passed
        and a file outside the workspace was readable.  ``is_relative_to``
        compares path components, not characters, and is immune.
        """
        inside = tmp_path / "work"
        sibling = tmp_path / "work-evil"
        inside.mkdir()
        sibling.mkdir()
        (sibling / "secret.txt").write_text("PWNED")

        ws = WorkspaceManager(inside)
        with pytest.raises(ValueError, match="Path traversal"):
            ws.resolve("../work-evil/secret.txt")

    def test_resolve_absolute_path_rejected(self, ws):
        """resolve() rejects absolute paths that target outside the workspace."""
        with pytest.raises(ValueError, match="Path traversal"):
            ws.resolve("/etc/passwd")

    def test_resolve_symlink_escaping_workspace_rejected(self, tmp_path):
        """resolve() rejects symlinks that point outside the workspace.

        ``Path.resolve()`` follows symlinks; ``is_relative_to`` checks
        the resolved path so an in-workspace symlink targeting an
        outside file does not bypass the boundary.
        """
        inside = tmp_path / "work"
        outside = tmp_path / "outside"
        inside.mkdir()
        outside.mkdir()
        (outside / "secret.txt").write_text("PWNED")
        (inside / "shortcut").symlink_to(outside / "secret.txt")

        ws = WorkspaceManager(inside)
        with pytest.raises(ValueError, match="Path traversal"):
            ws.resolve("shortcut")

    def test_resolve_file_not_found(self, ws):
        """resolve() raises FileNotFoundError for missing files."""
        with pytest.raises(FileNotFoundError, match="File not found"):
            ws.resolve("nonexistent.pdf")


# --- read_json() ---


class TestReadJson:
    def test_read_json(self, ws, workspace):
        """read_json() reads and parses a JSON file."""
        data = {"key": "value", "count": 42}
        (workspace / "data.json").write_text(json.dumps(data))
        result = ws.read_json("data.json")
        assert result == data

    def test_read_json_invalid(self, ws, workspace):
        """read_json() raises JSONDecodeError for invalid JSON."""
        (workspace / "bad.json").write_text("not json")
        with pytest.raises(json.JSONDecodeError):
            ws.read_json("bad.json")

    def test_read_json_missing_file(self, ws):
        """read_json() raises FileNotFoundError for missing files."""
        with pytest.raises(FileNotFoundError):
            ws.read_json("missing.json")


# --- read_text() ---


class TestReadText:
    def test_read_text(self, ws, workspace):
        """read_text() returns file contents as string."""
        (workspace / "note.txt").write_text("hello world")
        assert ws.read_text("note.txt") == "hello world"


# --- write_json() ---


class TestWriteJson:
    def test_write_json(self, ws, workspace):
        """write_json() writes JSON and returns the path."""
        data = {"result": "success"}
        path = ws.write_json("output.json", data)
        assert path == workspace / "output.json"
        assert json.loads(path.read_text()) == data

    def test_write_json_pretty_printed(self, ws, workspace):
        """write_json() writes indented JSON."""
        ws.write_json("formatted.json", {"a": 1})
        text = (workspace / "formatted.json").read_text()
        assert "  " in text  # indented

    def test_write_json_path_traversal_rejected(self, ws, tmp_path):
        """write_json() rejects filenames that escape the workspace.

        Regression test: previously ``write_json`` did NO traversal
        check and would happily write outside the workspace, e.g.
        ``write_json("../leak.json", ...)`` wrote to ``tmp_path/leak.json``
        rather than refusing.  The fix routes both reads and writes
        through ``_ensure_within_workspace`` so the boundary is
        enforced uniformly.
        """
        with pytest.raises(ValueError, match="Path traversal"):
            ws.write_json("../leak.json", {"leaked": True})
        # Confirm nothing was written outside the workspace.
        assert not (tmp_path / "leak.json").exists()

    def test_write_json_sibling_prefix_traversal_rejected(self, tmp_path):
        """write_json() rejects sibling-prefix collision attacks too."""
        inside = tmp_path / "work"
        inside.mkdir()
        ws = WorkspaceManager(inside)
        with pytest.raises(ValueError, match="Path traversal"):
            ws.write_json("../work-evil/leak.json", {"leaked": True})

    def test_write_json_creates_parent_directories(self, ws, workspace):
        """write_json() creates intermediate directories for nested writes."""
        path = ws.write_json("subdir/nested/output.json", {"ok": True})
        assert path.exists()
        assert path == workspace / "subdir" / "nested" / "output.json"
