#!/usr/bin/env python3
"""Enforce the underscore-prefix middleware-lane wire convention.

See ``heddle-agent-toolkit/anchors/CONTRACT_MAP.md`` "Reserved
middleware lane" for the convention. This script is its
machine-checkable counterpart and runs in CI.

Failures here mean someone crossed the line between the application
contract (Pydantic models in ``heddle.core.messages``, declared in
``schemas/v1/``) and the middleware lane (underscore-prefixed wire
keys, owned by tagged middleware modules).

Run locally::

    uv run python tools/check_envelope_convention.py

Exit code 0 on clean, 1 on any violation.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MESSAGES_PATH = REPO_ROOT / "src/heddle/core/messages.py"
SCHEMAS_DIR = REPO_ROOT / "schemas/v1"

# Modules permitted to read/write underscore-prefixed wire keys.
# Each entry is a path relative to REPO_ROOT. Adding to this list is a
# deliberate convention-extension: the new middleware module's purpose
# and reserved key(s) should also be added to CONTRACT_MAP.md.
MIDDLEWARE_MODULES = {
    "src/heddle/tracing/otel.py",
}


# ---------------------------------------------------------------------------
# Rule 1 — Pydantic envelope fields must not use the underscore prefix.
# ---------------------------------------------------------------------------


def check_pydantic_fields_no_underscore_prefix() -> list[str]:
    """No field in ``heddle.core.messages`` Pydantic models may start with ``_``.

    The middleware lane is reserved at the wire level; application
    fields with leading underscores would clash with the convention
    and confuse readers.
    """
    errors: list[str] = []
    tree = ast.parse(MESSAGES_PATH.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        # Heuristic: any class with a base named "BaseModel" (bare or attr).
        # Heddle's models all subclass pydantic.BaseModel directly.
        is_pydantic = any(
            (isinstance(b, ast.Name) and b.id == "BaseModel")
            or (isinstance(b, ast.Attribute) and b.attr == "BaseModel")
            for b in node.bases
        )
        if not is_pydantic:
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                field = stmt.target.id
                if field.startswith("_"):
                    errors.append(
                        f"{MESSAGES_PATH.relative_to(REPO_ROOT)}: "
                        f"class {node.name}: field `{field}` starts with `_` "
                        f"— underscore-prefix is reserved for the middleware "
                        f"lane, not application fields. See "
                        f"CONTRACT_MAP.md 'Reserved middleware lane'."
                    )
    return errors


# ---------------------------------------------------------------------------
# Rule 2 — middleware modules touch only underscore-prefixed wire keys.
# ---------------------------------------------------------------------------


def _extract_carrier_key_op(node: ast.AST) -> tuple[str | None, str | None]:
    """Return (key, target_name) if ``node`` is a dict-key op on a Name.

    Handles ``target["key"]`` (Subscript) and
    ``target.get("key")`` / ``setdefault`` / ``pop`` (Call).
    Returns ``(None, None)`` otherwise.
    """
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    ):
        return node.slice.value, node.value.id
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"get", "setdefault", "pop"}
        and isinstance(node.func.value, ast.Name)
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value, node.func.value.id
    return None, None


def check_middleware_modules_underscore_only() -> list[str]:
    """Verify tagged middleware functions touch only ``_``-prefixed wire keys.

    ``inject_*`` / ``extract_*`` functions in allowlisted modules must
    only read/write underscore-prefixed keys on their carrier (the
    function's parameters — typically a wire-envelope dict).

    The check is parameter-scoped: dict-key operations on *local*
    variables (e.g. an OTel-internal ``headers`` dict the propagator
    fills) are not flagged; only operations on parameter names are.
    """
    errors: list[str] = []
    for rel in sorted(MIDDLEWARE_MODULES):
        path = REPO_ROOT / rel
        if not path.exists():
            errors.append(
                f"{rel}: middleware module listed in MIDDLEWARE_MODULES but file not found"
            )
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if not (node.name.startswith("inject_") or node.name.startswith("extract_")):
                continue
            params = {arg.arg for arg in node.args.args}
            for sub in ast.walk(node):
                key, target = _extract_carrier_key_op(sub)
                if key is None or target not in params:
                    continue
                if not key.startswith("_"):
                    errors.append(
                        f"{path.relative_to(REPO_ROOT)}: function "
                        f"`{node.name}` operates on non-underscore key "
                        f"`{key}` of carrier parameter `{target}` — "
                        f"middleware functions must only read/write "
                        f"`_`-prefixed keys on the wire carrier."
                    )
    return errors


# ---------------------------------------------------------------------------
# Rule 3 — JSON schemas must not declare underscore-prefixed properties.
# ---------------------------------------------------------------------------


def check_schemas_no_underscore_properties() -> list[str]:
    """Forbid schema-declared underscore-prefixed properties.

    Schemas describe the application contract; underscore-prefixed
    fields belong to the middleware lane and must not be declared.
    """
    errors: list[str] = []
    for schema_path in sorted(SCHEMAS_DIR.glob("*.schema.json")):
        schema = json.loads(schema_path.read_text())
        props = schema.get("properties", {})
        errors.extend(
            f"{schema_path.relative_to(REPO_ROOT)}: property "
            f"`{name}` starts with `_` — middleware-lane fields "
            f"are intentionally undeclared to preserve forward "
            f"compatibility. See CONTRACT_MAP.md."
            for name in props
            if name.startswith("_")
        )
    return errors


# ---------------------------------------------------------------------------
# Rule 4 — schemas may not strictly forbid extras without carving out `_*`.
# ---------------------------------------------------------------------------


def check_schemas_carve_out_underscore_when_strict() -> list[str]:
    """Require the ``^_`` patternProperties carve-out when schemas are strict.

    If a top-level envelope schema sets ``additionalProperties: false``,
    it must also include ``patternProperties: { "^_": {} }`` so the
    middleware lane survives strict validation.
    """
    errors: list[str] = []
    for schema_path in sorted(SCHEMAS_DIR.glob("*.schema.json")):
        schema = json.loads(schema_path.read_text())
        if schema.get("additionalProperties") is False:
            patterns = schema.get("patternProperties", {})
            if not any(p.startswith("^_") for p in patterns):
                errors.append(
                    f"{schema_path.relative_to(REPO_ROOT)}: "
                    f"top-level `additionalProperties: false` without "
                    f'`patternProperties: {{"^_": {{}}}}` — strict '
                    f"validation would reject middleware-lane fields. "
                    f"Add the carve-out or drop the strict setting."
                )
    return errors


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main() -> int:
    """Run all envelope-convention checks; return 0 on clean, 1 on any violation."""
    all_errors: list[str] = []
    all_errors.extend(check_pydantic_fields_no_underscore_prefix())
    all_errors.extend(check_middleware_modules_underscore_only())
    all_errors.extend(check_schemas_no_underscore_properties())
    all_errors.extend(check_schemas_carve_out_underscore_when_strict())

    if all_errors:
        print(  # noqa: T201 — CLI tool output
            f"envelope-convention check FAILED ({len(all_errors)} violation(s)):",
            file=sys.stderr,
        )
        for err in all_errors:
            print(f"  - {err}", file=sys.stderr)  # noqa: T201 — CLI tool output
        print(  # noqa: T201 — CLI tool output
            "\nSee heddle-agent-toolkit/anchors/CONTRACT_MAP.md "
            "'Reserved middleware lane' for the convention.",
            file=sys.stderr,
        )
        return 1

    print(  # noqa: T201 — CLI tool output
        f"envelope-convention check OK "
        f"({len(MIDDLEWARE_MODULES)} middleware module(s) inspected, "
        f"{sum(1 for _ in SCHEMAS_DIR.glob('*.schema.json'))} schema(s) checked)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
