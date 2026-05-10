"""Tests for :mod:`heddle.core.paths`.

The boundary check is load-bearing — it's Design Invariant 13's
runtime enforcement.  Existing integrated tests (workspace,
mcp_resources, knowledge_silos) cover the call sites; these unit
tests pin the helper itself so a future change to the boundary
predicate trips here first.
"""

from __future__ import annotations

import pytest

from heddle.core.paths import resolve_within


class TestResolveWithin:
    def test_simple_file_resolves(self, tmp_path):
        result = resolve_within(tmp_path, "report.pdf")
        assert result == (tmp_path / "report.pdf").resolve()

    def test_nested_relative_path_resolves(self, tmp_path):
        result = resolve_within(tmp_path, "sub/dir/data.json")
        assert result == (tmp_path / "sub" / "dir" / "data.json").resolve()

    def test_parent_traversal_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="Path traversal detected"):
            resolve_within(tmp_path, "../escape.txt")

    def test_absolute_path_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="Path traversal detected"):
            resolve_within(tmp_path, "/etc/passwd")

    def test_sibling_prefix_collision_rejected(self, tmp_path):
        """The ``startswith`` shape this helper replaced was defeated by
        sibling-prefix collisions: ``workspace=/tmp/work`` plus
        ``"../work-evil/secret"`` resolves to ``/tmp/work-evil/secret``,
        whose string starts with ``/tmp/work``.  ``is_relative_to`` is
        path-component-aware and rejects it.
        """
        # Create a sibling directory with a prefix-overlapping name.
        sibling = tmp_path.parent / (tmp_path.name + "-evil")
        sibling.mkdir()
        try:
            with pytest.raises(ValueError, match="Path traversal detected"):
                resolve_within(tmp_path, f"../{sibling.name}/secret.txt")
        finally:
            sibling.rmdir()

    def test_symlink_escape_rejected(self, tmp_path):
        """A symlink inside the root that points outside is rejected
        because ``resolve()`` follows the symlink before the boundary
        check runs.
        """
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("secret")
        link = tmp_path / "link.txt"
        link.symlink_to(outside)
        try:
            with pytest.raises(ValueError, match="Path traversal detected"):
                resolve_within(tmp_path, "link.txt")
        finally:
            link.unlink()
            outside.unlink()

    def test_root_does_not_need_to_exist(self, tmp_path):
        """A fresh workspace dir may not exist yet; the helper still
        resolves under it so writes (which create the file) can pass
        the boundary check.
        """
        future_root = tmp_path / "not-yet-created"
        result = resolve_within(future_root, "new.json")
        # ``resolve()`` is strict=False by default — non-existent
        # paths get their parts computed component-wise.
        assert "new.json" in str(result)

    def test_accepts_path_candidate(self, tmp_path):
        from pathlib import Path

        result = resolve_within(tmp_path, Path("a") / "b")
        assert result == (tmp_path / "a" / "b").resolve()
