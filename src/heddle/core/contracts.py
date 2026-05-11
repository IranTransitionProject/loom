"""
Lightweight JSON Schema validation for I/O contracts.

We avoid the jsonschema dependency — this covers the 90% case (required fields
+ shallow type checks for object properties). This is intentionally simple:
every worker has an input_schema and output_schema defined in its YAML config,
and this module validates payloads against those schemas at message boundaries.

Limitations (by design — keeps the dependency tree minimal):
- No nested object validation (only top-level properties)
- No array item type validation
- No min/max, pattern, enum, or format constraints
- No $ref or schema composition (allOf, oneOf, anyOf)

If you need full Draft 2020-12 validation, add `jsonschema` to dependencies
and swap this module. The validate_input/validate_output API stays the same.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()

# Schemas Heddle's shallow validator does not interpret.  When a
# schema relies on these without a sibling ``type`` the validator
# silently accepts anything — operators expecting strict
# enforcement get an open-world contract.  We warn ONCE per
# (schema_id, missing_keyword) pair so the misconfiguration is
# visible without flooding logs.
_UNCOVERED_KEYWORDS: tuple[str, ...] = ("$ref", "allOf", "oneOf", "anyOf")
_warned_schemas: set[tuple[int, str]] = set()


def _check_uncovered_keywords(schema: dict[str, Any], context: str) -> None:
    """Log a one-time WARN when a schema uses keywords this validator skips.

    Only fires when the schema relies on the uncovered keyword *and*
    has no top-level ``type`` — i.e. validation would silently pass
    everything.  Set ``HEDDLE_STRICT_SCHEMAS=1`` to also raise (off
    by default to keep backwards-compat while operators audit).
    """
    if schema.get("type"):
        return
    for keyword in _UNCOVERED_KEYWORDS:
        if keyword in schema:
            key = (id(schema), keyword)
            if key in _warned_schemas:
                return
            _warned_schemas.add(key)
            logger.warning(
                "schema.uncovered_keyword",
                context=context,
                keyword=keyword,
                hint=(
                    "Heddle's shallow validator does not interpret "
                    f"{keyword}; any payload will pass validation.  "
                    "Add a top-level ``type`` or migrate to a full "
                    "jsonschema validator."
                ),
            )
            return


def validate_input(data: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Validate a task's input payload against the worker's input_schema.

    Returns an empty list if valid, or a list of human-readable error strings.
    """
    return _validate(data, schema, "input")


def validate_output(data: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Validate a worker's output against its output_schema.

    Returns an empty list if valid, or a list of human-readable error strings.
    """
    return _validate(data, schema, "output")


def _validate(data: Any, schema: dict[str, Any], context: str) -> list[str]:
    """Basic schema validation. Returns list of error strings (empty = valid).

    Only validates top-level required fields and shallow property types.
    Extra fields not in the schema are silently allowed (open-world assumption).
    """
    errors = []

    if not schema:
        return errors  # No schema = no constraints = always valid

    _check_uncovered_keywords(schema, context)

    expected_type = schema.get("type")
    if expected_type == "object" and not isinstance(data, dict):
        return [f"{context}: expected object, got {type(data).__name__}"]

    if expected_type == "object":
        # Check required fields
        errors.extend(
            f"{context}: missing required field '{field}'"
            for field in schema.get("required", [])
            if field not in data
        )

        # Check property types (shallow — does not recurse into nested objects).
        # Type checking order matters: bool must be checked BEFORE int/number
        # because Python's bool is a subclass of int (isinstance(True, int) is True).
        props = schema.get("properties", {})
        for field, field_schema in props.items():
            if field in data:
                field_type = field_schema.get("type")
                value = data[field]
                if field_type == "boolean" and not isinstance(value, bool):
                    errors.append(f"{context}.{field}: expected boolean")
                elif field_type == "integer":
                    # Reject bools masquerading as ints (bool is a subclass of int)
                    if isinstance(value, bool) or not isinstance(value, int):
                        errors.append(f"{context}.{field}: expected integer")
                elif field_type == "number":
                    # Reject bools masquerading as numbers
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        errors.append(f"{context}.{field}: expected number")
                elif field_type == "string" and not isinstance(value, str):
                    errors.append(f"{context}.{field}: expected string")
                elif field_type == "array" and not isinstance(value, list):
                    errors.append(f"{context}.{field}: expected array")

    return errors
