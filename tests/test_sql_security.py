"""Tests for the shared SQL safety helpers used by DuckDB + LanceDB contrib.

The helpers themselves live in ``heddle.contrib._sql_security``;
they're applied at the constructors of :class:`DuckDBQueryBackend`,
:class:`DuckDBViewTool`, :class:`DuckDBVectorTool`, and at the
filter-expression call sites in :class:`LanceDBVectorStore`.

Two flavours of test:

- Unit tests on the pure helpers (regex behaviour, escape rules).
- Integration tests confirming the constructors actually call the
  validator — a misconfigured YAML must raise loudly at startup
  rather than silently corrupt a query later.
"""

from __future__ import annotations

import pytest

from heddle.contrib._sql_security import (
    escape_sql_string_literal,
    validate_sql_identifier,
    validate_sql_identifier_list,
)

# ---------------------------------------------------------------------------
# validate_sql_identifier
# ---------------------------------------------------------------------------


_VALID_IDENTIFIERS = [
    "documents",
    "id",
    "full_text",
    "embedding",
    "rowid",
    "_internal",
    "T1",
    "snake_case_99",
    "MixedCase",
]


_INVALID_IDENTIFIERS = [
    # Quotes / SQL string-literal escape attempts.
    "x' OR 1=1 --",
    "x';DROP TABLE users;--",
    "x\";",
    "x` ",
    # Path-like / dot notation (no schema-qualified support today).
    "schema.table",
    # Whitespace (could break out of the identifier slot).
    "with space",
    "leading_tab\t",
    # Punctuation that has SQL meaning.
    "x;",
    "x,y",
    "x(",
    "x)",
    "x-y",
    "x*",
    # Comment markers.
    "x--",
    "x/*",
    # Leading digit (SQL identifier rule).
    "1table",
    # Empty / null byte.
    "",
    "x\x00",
    # Non-string types reject cleanly.
]


@pytest.mark.parametrize("name", _VALID_IDENTIFIERS)
def test_validate_sql_identifier_accepts(name):
    """Every shipped DuckDB config name validates."""
    assert validate_sql_identifier(name) == name


@pytest.mark.parametrize("name", _INVALID_IDENTIFIERS)
def test_validate_sql_identifier_rejects(name):
    """Names that could break out of the identifier slot are rejected.

    Parametrised so a future entry point that adds a new validation
    site has the same attack table available.  Adding to this list
    is the cheapest way to extend coverage if a new attack shape
    surfaces.
    """
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        validate_sql_identifier(name)


def test_validate_sql_identifier_includes_field_in_error():
    """The error message must name the configured field so misconfigured
    YAML reports which key is wrong (not just "identifier").
    """
    with pytest.raises(ValueError, match=r"for table_name"):
        validate_sql_identifier("a-b", field="table_name")


def test_validate_sql_identifier_rejects_non_string():
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        validate_sql_identifier(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        validate_sql_identifier(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# validate_sql_identifier_list
# ---------------------------------------------------------------------------


class TestValidateSqlIdentifierList:
    def test_accepts_single(self):
        assert validate_sql_identifier_list("full_text") == ["full_text"]

    def test_accepts_multiple_with_whitespace(self):
        assert validate_sql_identifier_list("full_text, summary, body") == [
            "full_text",
            "summary",
            "body",
        ]

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError, match="non-empty"):
            validate_sql_identifier_list("")

    def test_rejects_empty_segment(self):
        """``"a,,b"`` produces an empty middle entry — reject."""
        with pytest.raises(ValueError, match="non-empty"):
            validate_sql_identifier_list("a,,b")

    def test_rejects_invalid_segment(self):
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            validate_sql_identifier_list("a,'b'--, c")

    def test_rejects_non_string(self):
        with pytest.raises(ValueError, match="expected comma-separated"):
            validate_sql_identifier_list(["a", "b"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# escape_sql_string_literal
# ---------------------------------------------------------------------------


class TestEscapeSqlStringLiteral:
    def test_no_quotes_unchanged(self):
        assert escape_sql_string_literal("hello") == "hello"

    def test_single_quote_doubled(self):
        """SQL standard escape: ``'`` → ``''``."""
        assert escape_sql_string_literal("O'Brien") == "O''Brien"

    def test_classic_injection_payload_neutralised(self):
        """The classic ``' OR 1=1 --`` becomes a benign string literal.

        Once doubled, it is no longer a quote escape — it's just
        characters inside the surrounding quotes.
        """
        payload = "' OR 1=1 --"
        escaped = escape_sql_string_literal(payload)
        # The escaped result, wrapped in single quotes, should produce
        # a string that contains the literal payload as-is — no quote
        # break-out.
        wrapped = f"'{escaped}'"
        # The wrapped string has a balanced number of single quotes
        # (start, end, plus the two doubled internal quotes).
        assert wrapped.count("'") % 2 == 0
        # And the user-visible content survives a round-trip through
        # SQL-style un-escape.
        assert wrapped[1:-1].replace("''", "'") == payload

    def test_rejects_null_byte(self):
        """Null bytes truncate strings on most backends — reject."""
        with pytest.raises(ValueError, match="null byte"):
            escape_sql_string_literal("safe\x00malicious")

    def test_rejects_non_string(self):
        with pytest.raises(ValueError, match="expected str"):
            escape_sql_string_literal(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Constructor integration — DuckDB tools validate at __init__ time
# ---------------------------------------------------------------------------


class TestDuckDBQueryBackendConstructorValidation:
    def _kwargs(self, **overrides):
        base = {
            "db_path": "/tmp/test.duckdb",
            "table_name": "documents",
            "id_column": "id",
        }
        base.update(overrides)
        return base

    def test_accepts_default_config(self, tmp_path):
        from heddle.contrib.duckdb.query_backend import DuckDBQueryBackend

        # Default constructor must round-trip without raising.
        DuckDBQueryBackend(db_path=str(tmp_path / "x.duckdb"))

    def test_rejects_bad_table_name(self, tmp_path):
        from heddle.contrib.duckdb.query_backend import DuckDBQueryBackend

        with pytest.raises(ValueError, match="for table_name"):
            DuckDBQueryBackend(
                **self._kwargs(
                    db_path=str(tmp_path / "x.duckdb"),
                    table_name="docs; DROP TABLE users; --",
                )
            )

    def test_rejects_bad_id_column(self, tmp_path):
        from heddle.contrib.duckdb.query_backend import DuckDBQueryBackend

        with pytest.raises(ValueError, match="for id_column"):
            DuckDBQueryBackend(
                **self._kwargs(
                    db_path=str(tmp_path / "x.duckdb"),
                    id_column="a' OR '1'='1",
                )
            )

    def test_rejects_bad_fts_field(self, tmp_path):
        from heddle.contrib.duckdb.query_backend import DuckDBQueryBackend

        with pytest.raises(ValueError, match="fts_fields"):
            DuckDBQueryBackend(
                **self._kwargs(
                    db_path=str(tmp_path / "x.duckdb"),
                    fts_fields="full_text,'; DROP --",
                )
            )

    def test_rejects_bad_embedding_column(self, tmp_path):
        from heddle.contrib.duckdb.query_backend import DuckDBQueryBackend

        with pytest.raises(ValueError, match="for embedding_column"):
            DuckDBQueryBackend(
                **self._kwargs(
                    db_path=str(tmp_path / "x.duckdb"),
                    embedding_column="vec)) --",
                )
            )

    def test_canonicalises_fts_fields_whitespace(self, tmp_path):
        """``"full_text,  summary "`` is stored as ``"full_text,summary"``.

        Pinning the canonical form means the value interpolated into
        SQL never carries surrounding whitespace that could surprise
        a downstream parser.
        """
        from heddle.contrib.duckdb.query_backend import DuckDBQueryBackend

        backend = DuckDBQueryBackend(
            **self._kwargs(
                db_path=str(tmp_path / "x.duckdb"),
                fts_fields="full_text,  summary ",
            )
        )
        assert backend.fts_fields == "full_text,summary"


class TestDuckDBViewToolConstructorValidation:
    def test_accepts_safe_view_name(self, tmp_path):
        from heddle.contrib.duckdb.view_tool import DuckDBViewTool

        # Construction must not raise even if the view doesn't exist —
        # introspection is best-effort and logs a warning.
        DuckDBViewTool(db_path=str(tmp_path / "x.duckdb"), view_name="summaries")

    def test_rejects_bad_view_name(self, tmp_path):
        from heddle.contrib.duckdb.view_tool import DuckDBViewTool

        with pytest.raises(ValueError, match="for view_name"):
            DuckDBViewTool(
                db_path=str(tmp_path / "x.duckdb"),
                view_name="v); DROP VIEW summaries; --",
            )


class TestDuckDBVectorToolConstructorValidation:
    def test_accepts_default(self, tmp_path):
        from heddle.contrib.duckdb.vector_tool import DuckDBVectorTool

        DuckDBVectorTool(db_path=str(tmp_path / "x.duckdb"))

    def test_rejects_bad_table_name(self, tmp_path):
        from heddle.contrib.duckdb.vector_tool import DuckDBVectorTool

        with pytest.raises(ValueError, match="for table_name"):
            DuckDBVectorTool(
                db_path=str(tmp_path / "x.duckdb"),
                table_name="t' --",
            )

    def test_rejects_bad_embedding_column(self, tmp_path):
        from heddle.contrib.duckdb.vector_tool import DuckDBVectorTool

        with pytest.raises(ValueError, match="for embedding_column"):
            DuckDBVectorTool(
                db_path=str(tmp_path / "x.duckdb"),
                embedding_column="v)) --",
            )
