"""Pipeline ``input_mapping`` source resolution.

Pipeline stages map worker payload fields from one of two source shapes:

- a dot-separated path into the run context
  (``"goal.context.file_ref"``, ``"extract.output.page_count"``); or
- a single-quoted string literal
  (``"'store'"`` → the string ``"store"``), used by the shipped RAG
  pipeline's ``vectorize`` stage to pass ``action="store"`` without
  routing the value through a context key.

Three sites need to recognise that same predicate:

- :func:`heddle.core.config.validate_pipeline_config` — skips the
  inter-stage reference check for literals (a literal isn't a stage
  name).
- :meth:`heddle.orchestrator.pipeline.PipelineOrchestrator._build_stage_payload`
  — strips the surrounding quotes and emits the literal value into the
  payload.
- :meth:`heddle.orchestrator.pipeline.PipelineOrchestrator._infer_dependencies`
  — must NOT treat ``"'store'"`` as a reference to a stage named
  ``"'store'"``.  Historically this was "safe by accident" because the
  first dot-split segment of ``"'store'"`` doesn't match any real stage
  name, but routing it through the predicate makes the safety explicit
  rather than incidental.

The two functions exported here exist so all three sites share one
definition of "is this a literal?"  If the literal syntax ever grows
richer (escape sequences, multi-line literals, etc.), there's exactly
one place to change.
"""

from __future__ import annotations

from typing import Any


def is_mapping_literal(source_path: str) -> bool:
    """Return True if ``source_path`` is a single-quoted literal.

    The predicate is intentionally minimal — ``startswith("'") and
    endswith("'")``.  Degenerate inputs like a bare ``"'"`` resolve to
    a literal empty string, which matches the prior inline behaviour.
    """
    return source_path.startswith("'") and source_path.endswith("'")


def resolve_mapping_value(source_path: str, context: dict[str, Any]) -> Any:
    """Resolve an ``input_mapping`` source against the run context.

    - Single-quoted strings are returned with the quotes stripped.
    - Anything else is treated as a dot-separated path into ``context``
      and resolved segment by segment.

    Raises:
        KeyError: If a dict segment is missing.
        ValueError: If a segment tries to traverse into a non-dict.
    """
    if is_mapping_literal(source_path):
        return source_path[1:-1]

    parts = source_path.split(".")
    current: Any = context
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(f"Path '{source_path}': key '{part}' not found in context")
            current = current[part]
        else:
            raise ValueError(
                f"Path '{source_path}': cannot traverse into {type(current).__name__} at '{part}'"
            )
    return current
