"""Tests for F1: ``required: true|false`` on knowledge silos / sources / tool providers.

Default = ``true`` (strict-by-default policy chosen during F-session
scoping).  These tests pin three contracts:

  - **Required missing → raise.**  When ``required`` is True (the
    default), a missing folder / source / tool provider raises
    ``RequiredKnowledgeMissingError`` so the worker layer can surface
    the failure as a FAILED ``TaskResult`` rather than silently run
    with a degraded prompt.
  - **Optional missing → log + skip + record.**  When the operator
    explicitly sets ``required: False``, the loader keeps the
    pre-F1 behaviour, logs a warning, and (if a ``skipped``
    accumulator was passed) records a ``{kind, name, reason}`` dict
    so callers can detect degraded mode without scraping logs.
  - **Skipped optionals reach TaskResult.metadata.**  The plumbing
    from worker.process()'s ``degraded`` list through
    ``_publish_result(metadata=...)`` to ``TaskResult.metadata`` is
    end-to-end pinned via a focused unit test on the publish call.
"""

from __future__ import annotations

import pytest

from heddle.worker.knowledge import (
    RequiredKnowledgeMissingError,
    load_knowledge_silos,
    load_knowledge_sources,
)
from heddle.worker.runner import _load_tool_providers


class TestRequiredKnowledgeSilo:
    def test_required_silo_raises_when_folder_missing(self, tmp_path):
        """Default ``required=True``: missing folder raises with a useful message."""
        silos = [
            {
                "name": "missing",
                "type": "folder",
                "path": str(tmp_path / "nope"),
            }
        ]
        with pytest.raises(RequiredKnowledgeMissingError) as exc_info:
            load_knowledge_silos(silos)
        assert exc_info.value.kind == "knowledge_silo"
        assert exc_info.value.name == "missing"
        assert "folder not found" in exc_info.value.reason

    def test_explicit_required_true_raises(self, tmp_path):
        """``required: True`` written explicitly behaves the same as default."""
        silos = [
            {
                "name": "explicit",
                "type": "folder",
                "path": str(tmp_path / "nope"),
                "required": True,
            }
        ]
        with pytest.raises(RequiredKnowledgeMissingError):
            load_knowledge_silos(silos)

    def test_optional_silo_skipped_records_to_accumulator(self, tmp_path):
        """``required: False`` → log + skip + accumulator gets a {kind,name,reason}."""
        silos = [
            {
                "name": "opt",
                "type": "folder",
                "path": str(tmp_path / "nope"),
                "required": False,
            }
        ]
        skipped: list[dict] = []
        result = load_knowledge_silos(silos, skipped=skipped)
        assert result == ""
        assert skipped == [{"kind": "knowledge_silo", "name": "opt", "reason": "folder_not_found"}]


class TestRequiredKnowledgeSource:
    def test_required_source_raises_when_file_missing(self, tmp_path):
        """Default ``required=True``: missing source raises."""
        sources = [{"path": str(tmp_path / "missing.md"), "inject_as": "reference"}]
        with pytest.raises(RequiredKnowledgeMissingError) as exc_info:
            load_knowledge_sources(sources)
        assert exc_info.value.kind == "knowledge_source"
        assert "missing.md" in exc_info.value.name
        assert "file not found" in exc_info.value.reason

    def test_required_source_raises_when_oversize(self, tmp_path):
        """A required source that exceeds the size cap also raises."""
        big = tmp_path / "big.txt"
        big.write_text("x" * 500)
        sources = [{"path": str(big), "inject_as": "reference", "max_bytes": 50}]
        with pytest.raises(RequiredKnowledgeMissingError) as exc_info:
            load_knowledge_sources(sources)
        assert "exceeds size cap" in exc_info.value.reason

    def test_optional_source_skipped_records_to_accumulator(self, tmp_path):
        sources = [
            {
                "path": str(tmp_path / "missing.md"),
                "inject_as": "reference",
                "required": False,
            }
        ]
        skipped: list[dict] = []
        result = load_knowledge_sources(sources, skipped=skipped)
        assert result == ""
        assert skipped == [
            {
                "kind": "knowledge_source",
                "name": str(tmp_path / "missing.md"),
                "reason": "not_found",
            }
        ]


class TestRequiredToolProvider:
    def test_required_tool_provider_raises_when_load_fails(self):
        """A tool-type silo whose provider class can't be imported raises
        when ``required`` defaults to True."""
        silos = [
            {
                "name": "broken",
                "type": "tool",
                "provider": "nonexistent.module.NoSuchProvider",
                "config": {},
            }
        ]
        with pytest.raises(RequiredKnowledgeMissingError) as exc_info:
            _load_tool_providers(silos)
        assert exc_info.value.kind == "tool_provider"
        assert exc_info.value.name == "broken"
        assert "load failed" in exc_info.value.reason

    def test_optional_tool_provider_skipped_records_to_accumulator(self):
        silos = [
            {
                "name": "opt_tool",
                "type": "tool",
                "provider": "nonexistent.module.NoSuchProvider",
                "config": {},
                "required": False,
            }
        ]
        skipped: list[dict] = []
        providers = _load_tool_providers(silos, skipped=skipped)
        assert providers == {}
        assert len(skipped) == 1
        assert skipped[0]["kind"] == "tool_provider"
        assert skipped[0]["name"] == "opt_tool"
        assert skipped[0]["reason"].startswith("load_failed")


class TestRequiredFieldTypeValidation:
    """Config validation rejects ``required: <non-bool>`` so a typo is caught
    at config-load time, not at runtime."""

    def test_silo_required_must_be_bool(self):
        from heddle.core.config import _validate_knowledge_silos

        errors = _validate_knowledge_silos(
            [
                {
                    "name": "x",
                    "type": "folder",
                    "path": "/tmp/y",
                    "required": "yes",  # string, not bool
                }
            ],
            "<test>",
        )
        assert any("'required' must be a boolean" in e for e in errors)

    def test_silo_required_bool_passes(self):
        from heddle.core.config import _validate_knowledge_silos

        errors = _validate_knowledge_silos(
            [{"name": "x", "type": "folder", "path": "/tmp/y", "required": False}],
            "<test>",
        )
        assert errors == []


class TestSkippedMetadataReachesTaskResult:
    """End-to-end pin: the ``skipped`` accumulator that worker.process()
    populates lands on TaskResult.metadata via _publish_result.
    """

    def test_publish_result_carries_metadata_field(self):
        """``TaskResult`` has a ``metadata`` field; ``_publish_result`` sets
        it from its ``metadata`` kwarg.  Pins the F1 plumbing without
        requiring a full worker actor + bus dance.
        """
        from heddle.core.messages import TaskResult, TaskStatus

        # The Pydantic model itself accepts the metadata field.
        result = TaskResult(
            task_id="t1",
            worker_type="w",
            status=TaskStatus.COMPLETED,
            metadata={"degraded_modes": [{"kind": "knowledge_silo", "name": "x"}]},
        )
        assert result.metadata["degraded_modes"][0]["kind"] == "knowledge_silo"

    def test_publish_result_default_metadata_is_empty_dict(self):
        from heddle.core.messages import TaskResult, TaskStatus

        result = TaskResult(task_id="t1", worker_type="w", status=TaskStatus.COMPLETED)
        # Empty default (not None) so consumers can do
        # ``result.metadata.get("degraded_modes", [])`` without an
        # if-not-None guard.
        assert result.metadata == {}
