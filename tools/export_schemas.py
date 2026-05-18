"""Export Heddle's wire-format Pydantic models to JSON Schema.

The canonical message envelopes — ``TaskMessage``, ``TaskResult``,
``OrchestratorGoal``, ``CheckpointState`` — live as Pydantic models in
``heddle.core.messages``.  Foreign-language SDKs (.NET, Swift, etc.) and
external integrations don't speak Pydantic; they need JSON Schema in a
stable on-disk location to feed into off-the-shelf code generators
(``quicktype``, ``NJsonSchema``, ``datamodel-code-generator``).

This script exports each model's ``.model_json_schema()`` to
``schemas/v1/<model>.schema.json`` with a stable sort order and trailing
newline so the output is git-diffable.  CI runs the script in ``--check``
mode and fails when the committed schemas drift from what the live
Pydantic models would produce — the same drift-prevention pattern that
``ruff format --check`` uses.

Usage:

    uv run python tools/export_schemas.py            # rewrite schemas/v1/
    uv run python tools/export_schemas.py --check    # CI: exit 1 on drift
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from heddle.core.messages import (
    CheckpointState,
    CommandMessage,
    EventEnvelope,
    OrchestratorGoal,
    TaskMessage,
    TaskResult,
)

# Each entry: (filename stem, Pydantic model class).  ``filename`` is the
# basename inside ``schemas/v1/``; ``schema_version`` is currently 1 across
# the board because the envelope shape hasn't broken backwards-
# compatibility.  Bump to ``schemas/v2/`` when a model gains a breaking
# field change; never overwrite a published v1 schema in place.
_EXPORTS: list[tuple[str, type]] = [
    ("task_message", TaskMessage),
    ("task_result", TaskResult),
    ("orchestrator_goal", OrchestratorGoal),
    ("checkpoint_state", CheckpointState),
    ("event_envelope", EventEnvelope),
    ("command_message", CommandMessage),
]

_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "schemas" / "v1"


def _render(model: type) -> str:
    """Render a model's JSON Schema with stable sort + trailing newline."""
    schema: dict[str, Any] = model.model_json_schema()
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def _write_all() -> list[Path]:
    """Write every export to disk.  Returns the list of paths written."""
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for stem, model in _EXPORTS:
        path = _OUTPUT_DIR / f"{stem}.schema.json"
        path.write_text(_render(model), encoding="utf-8")
        written.append(path)
    return written


def _check_all() -> int:
    """Compare on-disk schemas against the live render.  Return shell exit code."""
    drift: list[str] = []
    for stem, model in _EXPORTS:
        path = _OUTPUT_DIR / f"{stem}.schema.json"
        expected = _render(model)
        actual = path.read_text(encoding="utf-8") if path.exists() else ""
        if expected != actual:
            drift.append(stem)
    if drift:
        sys.stderr.write(
            "Schema drift detected for: "
            + ", ".join(drift)
            + "\nRun ``uv run python tools/export_schemas.py`` and commit the result.\n"
        )
        return 1
    return 0


def main() -> int:
    """CLI entry point — exports schemas, or in ``--check`` mode reports drift."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the on-disk schemas don't match the live Pydantic output.",
    )
    args = parser.parse_args()
    if args.check:
        return _check_all()
    written = _write_all()
    for path in written:
        sys.stdout.write(f"wrote {path.relative_to(Path.cwd())}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
