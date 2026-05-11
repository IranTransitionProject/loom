"""Test I/O contract validation."""

from heddle.core.contracts import validate_input, validate_output


def test_missing_required_field():
    schema = {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}}
    errors = validate_input({}, schema)
    assert any("missing required" in e for e in errors)


def test_valid_input():
    schema = {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}}
    errors = validate_input({"text": "hello"}, schema)
    assert errors == []


def test_wrong_type():
    schema = {"type": "object", "properties": {"count": {"type": "integer"}}}
    errors = validate_input({"count": "not_a_number"}, schema)
    assert any("expected integer" in e for e in errors)


def test_empty_schema():
    errors = validate_input({"anything": "goes"}, {})
    assert errors == []


def test_multiple_required_fields():
    schema = {
        "type": "object",
        "required": ["text", "categories"],
        "properties": {
            "text": {"type": "string"},
            "categories": {"type": "array"},
        },
    }
    errors = validate_input({}, schema)
    assert len(errors) == 2


def test_valid_output():
    schema = {
        "type": "object",
        "required": ["summary", "key_points"],
        "properties": {
            "summary": {"type": "string"},
            "key_points": {"type": "array"},
        },
    }
    errors = validate_output({"summary": "test", "key_points": ["a"]}, schema)
    assert errors == []


def test_not_an_object():
    schema = {"type": "object"}
    errors = validate_input("not a dict", schema)
    assert any("expected object" in e for e in errors)


def test_boolean_type_check():
    schema = {"type": "object", "properties": {"flag": {"type": "boolean"}}}
    errors = validate_input({"flag": "true"}, schema)
    assert any("expected boolean" in e for e in errors)

    errors = validate_input({"flag": True}, schema)
    assert errors == []


def test_number_type_check():
    schema = {"type": "object", "properties": {"score": {"type": "number"}}}
    errors = validate_input({"score": 0.95}, schema)
    assert errors == []

    errors = validate_input({"score": 5}, schema)
    assert errors == []

    errors = validate_input({"score": "high"}, schema)
    assert any("expected number" in e for e in errors)


def test_uncovered_keyword_warns_once(capsys):
    """G8: schemas using oneOf/allOf/anyOf/$ref without ``type`` warn.

    Heddle's shallow validator does not interpret those keywords;
    a schema relying on them without a top-level ``type`` silently
    accepts every payload — operators expecting strict enforcement
    get an open-world contract.  The warning fires once per
    (schema, keyword) pair so logs don't flood.
    """
    from heddle.core.contracts import _warned_schemas

    _warned_schemas.clear()

    schema = {"oneOf": [{"type": "object"}, {"type": "array"}]}
    # First call → warn (structlog renders to stdout).
    validate_input({"anything": 1}, schema)
    out = capsys.readouterr().out
    assert "schema.uncovered_keyword" in out
    assert "oneOf" in out
    # Second call with the same schema → no new warning.
    validate_input({"anything": 2}, schema)
    out = capsys.readouterr().out
    assert "schema.uncovered_keyword" not in out


def test_uncovered_keyword_silent_when_type_present():
    """If the schema already has ``type``, the keyword is consulted as-is."""
    from heddle.core.contracts import _warned_schemas

    _warned_schemas.clear()
    schema = {"type": "object", "oneOf": [{"required": ["a"]}]}
    # Just confirm no AttributeError / KeyError; no warning expected.
    validate_input({"a": 1}, schema)
