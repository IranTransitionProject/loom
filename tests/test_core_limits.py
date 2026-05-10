"""Tests for the shared file-read size cap (core/limits.py).

These tests pin both the central helper (``enforce_file_size``) and
each call site (MCP resources, WorkspaceManager, knowledge loaders) to
the same contract: a file larger than the cap is rejected before the
read runs, so an oversized file can never reach memory.

Mutation note for reviewers: each call-site test sets a tiny ``max_bytes``
override (or monkeypatches the module default) and writes a slightly
larger file.  Removing the ``enforce_file_size`` call at the read site
will make the test fail because the read will succeed when it should
have raised.
"""

from __future__ import annotations

import json

import pytest

from heddle.core.limits import (
    DEFAULT_FILE_READ_MAX_BYTES,
    FileTooLargeError,
    enforce_file_size,
)


class TestEnforceFileSize:
    def test_under_cap_returns_size(self, tmp_path):
        f = tmp_path / "small.txt"
        f.write_text("hi")
        assert enforce_file_size(f, max_bytes=100) == 2

    def test_over_cap_raises_with_metadata(self, tmp_path):
        f = tmp_path / "big.txt"
        f.write_text("x" * 50)
        with pytest.raises(FileTooLargeError) as exc_info:
            enforce_file_size(f, max_bytes=10)
        # Exception carries enough context to write a useful log line
        # without re-stat'ing the file.
        assert exc_info.value.size == 50
        assert exc_info.value.max_bytes == 10
        assert exc_info.value.path == str(f)
        assert "50 bytes" in str(exc_info.value)
        assert "10-byte" in str(exc_info.value)

    def test_default_cap_used_when_max_bytes_is_none(self, tmp_path, monkeypatch):
        """``max_bytes=None`` reads the module-level default at call time so
        ``monkeypatch.setattr`` works without threading overrides through
        every caller."""
        monkeypatch.setattr("heddle.core.limits.DEFAULT_FILE_READ_MAX_BYTES", 5)
        f = tmp_path / "x.txt"
        f.write_text("123456")  # 6 bytes
        with pytest.raises(FileTooLargeError):
            enforce_file_size(f)  # uses (patched) default = 5

    def test_default_is_10_mib(self):
        """The shipped default is documented as 10 MiB across docs and
        tests; pinning it here so a silent change to the constant
        breaks the build instead of the operator's expectations."""
        assert DEFAULT_FILE_READ_MAX_BYTES == 10 * 1024 * 1024

    def test_missing_file_propagates_filenotfounderror(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            enforce_file_size(tmp_path / "does_not_exist.txt")


class TestWorkspaceManagerCap:
    """``WorkspaceManager.read_text`` / ``read_json`` must apply the cap."""

    def _ws(self, tmp_path):
        from heddle.core.workspace import WorkspaceManager

        return WorkspaceManager(tmp_path)

    def test_read_text_rejects_oversized_file(self, tmp_path):
        big = tmp_path / "big.txt"
        big.write_text("x" * 200)
        with pytest.raises(FileTooLargeError):
            self._ws(tmp_path).read_text("big.txt", max_bytes=50)

    def test_read_text_under_cap_returns_content(self, tmp_path):
        small = tmp_path / "small.txt"
        small.write_text("ok")
        assert self._ws(tmp_path).read_text("small.txt", max_bytes=50) == "ok"

    def test_read_json_rejects_oversized_file(self, tmp_path):
        big = tmp_path / "big.json"
        big.write_text(json.dumps({"k": "x" * 500}))
        with pytest.raises(FileTooLargeError):
            self._ws(tmp_path).read_json("big.json", max_bytes=50)

    def test_read_json_under_cap_parses(self, tmp_path):
        small = tmp_path / "small.json"
        small.write_text(json.dumps({"a": 1}))
        assert self._ws(tmp_path).read_json("small.json", max_bytes=200) == {"a": 1}


class TestMcpResourcesCap:
    """``WorkspaceResources.read_resource`` must apply the cap."""

    def _resources(self, tmp_path):
        from heddle.mcp.resources import WorkspaceResources

        return WorkspaceResources(tmp_path)

    def test_read_resource_rejects_oversized_file(self, tmp_path):
        big = tmp_path / "big.md"
        big.write_text("x" * 200)
        with pytest.raises(FileTooLargeError):
            self._resources(tmp_path).read_resource("workspace:///big.md", max_bytes=50)

    def test_read_resource_under_cap_returns_text(self, tmp_path):
        small = tmp_path / "small.md"
        small.write_text("# hi")
        content, mime = self._resources(tmp_path).read_resource(
            "workspace:///small.md", max_bytes=200
        )
        assert content == "# hi"
        assert mime is not None
        assert mime.startswith("text/")

    def test_traversal_check_runs_before_size_check(self, tmp_path):
        """Path traversal must still be the first failure mode; an attacker
        who points at /etc/passwd should not learn its size from the
        cap error message."""
        with pytest.raises(ValueError, match="Path traversal"):
            # A real workspace dir; the URI escapes it.
            self._resources(tmp_path).read_resource(
                "workspace:///../../../etc/passwd", max_bytes=10
            )


class TestKnowledgeSourceCap:
    """Knowledge sources skip oversized files with a warning, not raise."""

    def test_oversized_source_is_skipped_not_raised(self, tmp_path):
        """Knowledge loading is best-effort; one big file must not fail
        the whole worker startup, and its content must not leak into
        the formatted prompt sections."""
        from heddle.worker.knowledge import load_knowledge_sources

        small = tmp_path / "small.txt"
        small.write_text("good content")
        big = tmp_path / "big.txt"
        big.write_text("x" * 5000)

        sources = [
            {"path": str(small), "inject_as": "reference", "max_bytes": 10000},
            {"path": str(big), "inject_as": "reference", "max_bytes": 100},
        ]

        result = load_knowledge_sources(sources)

        # Small file made it in; big file did not (content presence is
        # the load-bearing assertion — the warning log is best-effort
        # observability).
        assert "good content" in result
        assert "x" * 5000 not in result


class TestKnowledgeSiloCap:
    """Knowledge silo folder reads skip oversized files individually."""

    def test_oversized_silo_file_is_skipped(self, tmp_path, caplog):
        import logging

        from heddle.worker.knowledge import load_knowledge_silos

        silo_dir = tmp_path / "silo"
        silo_dir.mkdir()
        (silo_dir / "ok.md").write_text("# good")
        (silo_dir / "huge.md").write_text("x" * (DEFAULT_FILE_READ_MAX_BYTES + 1))

        with caplog.at_level(logging.WARNING):
            result = load_knowledge_silos(
                [{"name": "test", "type": "folder", "path": str(silo_dir)}]
            )

        assert "# good" in result
        # Confirms the cap fired and the read did NOT silently succeed.
        assert "x" * 100 not in result
