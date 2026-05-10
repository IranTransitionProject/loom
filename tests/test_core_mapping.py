"""Tests for :mod:`heddle.core.mapping`.

Pin the helper itself rather than re-pinning every caller — the
existing pipeline + config-validation tests already cover the
integrated behaviour (``test_pipeline.test_literal_mapping_value``,
the shipped-RAG smoke tests, the validator extended tests).  These
tests target edge cases of the predicate and resolver directly so a
future change to the literal syntax breaks the shared rule first.
"""

from __future__ import annotations

import pytest

from heddle.core.mapping import is_mapping_literal, resolve_mapping_value


class TestIsMappingLiteral:
    def test_plain_single_quoted_string_is_literal(self):
        assert is_mapping_literal("'store'") is True

    def test_dotted_path_is_not_literal(self):
        assert is_mapping_literal("goal.context.file_ref") is False

    def test_bare_identifier_is_not_literal(self):
        assert is_mapping_literal("goal") is False

    def test_empty_quotes_are_literal(self):
        # Degenerate but well-defined: the resolver returns "".
        assert is_mapping_literal("''") is True

    def test_only_leading_quote_is_not_literal(self):
        assert is_mapping_literal("'unterminated") is False

    def test_only_trailing_quote_is_not_literal(self):
        assert is_mapping_literal("unterminated'") is False


class TestResolveMappingValue:
    def test_literal_strips_surrounding_quotes(self):
        assert resolve_mapping_value("'store'", {}) == "store"

    def test_empty_literal_yields_empty_string(self):
        assert resolve_mapping_value("''", {}) == ""

    def test_single_segment_path(self):
        assert resolve_mapping_value("a", {"a": 1}) == 1

    def test_nested_path(self):
        ctx = {"goal": {"context": {"file_ref": "doc.pdf"}}}
        assert resolve_mapping_value("goal.context.file_ref", ctx) == "doc.pdf"

    def test_missing_key_raises_keyerror(self):
        with pytest.raises(KeyError, match=r"key 'missing' not found"):
            resolve_mapping_value("a.missing", {"a": {}})

    def test_traversing_into_non_dict_raises_valueerror(self):
        with pytest.raises(ValueError, match=r"cannot traverse into str"):
            resolve_mapping_value("a.b", {"a": "scalar"})

    def test_literal_short_circuits_before_path_walk(self):
        # If the predicate misfired and the value went through the
        # path walker, "'store'".split(".") yields ["'store'"] and the
        # walker would raise KeyError.  A clean return proves the
        # short-circuit.
        assert resolve_mapping_value("'store'", {}) == "store"
