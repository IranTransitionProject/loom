"""SQL safety helpers shared by DuckDB and LanceDB contrib backends.

Both DuckDB and LanceDB take identifier-shaped values (table names,
column names, view names) from worker YAML configs and interpolate
them into queries via ``f""`` strings.  Today the values flow only
from constructor arguments populated at config-load time, so a
crafted YAML is the only way to introduce a SQL/filter-syntax
injection — but a single config typo is enough to silently break or
hijack a query.

This module provides two small pure helpers used by every site that
currently does string interpolation into SQL or LanceDB filter
expressions:

- :func:`validate_sql_identifier` — strict allowlist regex on a
  single identifier (table, column, view, etc).  Raises
  :class:`ValueError` on anything outside ``^[A-Za-z_][A-Za-z0-9_]*$``.
- :func:`escape_sql_string_literal` — escape a single-quoted string
  literal for inclusion in a LanceDB ``where`` predicate.

Why we don't try to validate full SQL fragments (e.g. the
``stats_aggregates`` list, which carries strings like
``"COUNT(*) AS record_count"``): identifier validation is the safe,
unambiguous case; full-SQL parsing is out of scope and the existing
free-form fields are intentionally that way.
"""

from __future__ import annotations

import re

# A SQL identifier per the SQL-92 unquoted form: starts with a letter
# or underscore, followed by letters, digits, or underscores.  This
# matches every shipped DuckDB config (``documents``, ``id``,
# ``full_text``, ``embedding``, ``rowid``, …) and rejects anything
# that could break out of the identifier slot — quotes, semicolons,
# whitespace, parentheses, dots, dashes, comment markers, null bytes.
_VALID_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_sql_identifier(name: str, *, field: str = "identifier") -> str:
    """Validate ``name`` as a safe SQL identifier and return it.

    Args:
        name: The identifier to validate.
        field: Human-readable label for the validation error (e.g.
            ``"table_name"``) so misconfigured YAML reports which
            field is wrong.

    Returns:
        The same string on success — convenient for use inside an
        f-string, e.g. ``f"DESCRIBE {validate_sql_identifier(name)}"``.

    Raises:
        ValueError: If ``name`` does not match the strict identifier
            pattern.  The error message names ``field`` so config
            authors can locate the problem.
    """
    if not isinstance(name, str) or not _VALID_IDENTIFIER_RE.fullmatch(name):
        raise ValueError(
            f"Invalid SQL identifier for {field}: {name!r}.  "
            "Identifiers must match ^[A-Za-z_][A-Za-z0-9_]*$ "
            "(no quotes, dots, dashes, whitespace, or punctuation)."
        )
    return name


def validate_sql_identifier_list(value: str, *, field: str = "identifier list") -> list[str]:
    """Validate a comma-separated list of identifiers (e.g. ``"a,b,c"``).

    Returns the parsed list of identifiers on success.  Empty entries
    are rejected.  Used for ``fts_fields`` and similar config keys.
    """
    if not isinstance(value, str):
        raise ValueError(
            f"Invalid {field}: expected comma-separated string, got {type(value).__name__}"
        )
    parts = [p.strip() for p in value.split(",")]
    if not parts or any(p == "" for p in parts):
        raise ValueError(
            f"Invalid {field}: must be a non-empty comma-separated list, got {value!r}"
        )
    for part in parts:
        validate_sql_identifier(part, field=f"{field} item")
    return parts


def escape_sql_string_literal(value: str) -> str:
    """Escape ``value`` for use inside a single-quoted SQL/LanceDB string literal.

    The standard SQL escape: ``'`` → ``''``.  Also rejects null
    bytes, which truncate strings on most backends and could be used
    to smuggle injection past a naive escape.

    The returned string DOES NOT include the surrounding quotes —
    callers wrap it themselves so the call site is self-documenting:

        f"chunk_id = '{escape_sql_string_literal(chunk_id)}'"

    Raises:
        ValueError: If ``value`` is not a string or contains a null
            byte.
    """
    if not isinstance(value, str):
        raise ValueError(
            f"Invalid string literal: expected str, got {type(value).__name__}"
        )
    if "\x00" in value:
        raise ValueError("Invalid string literal: contains null byte")
    return value.replace("'", "''")
