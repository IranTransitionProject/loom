"""Smoke test for the shipped ``configs/orchestrators/rag_pipeline.yaml``.

The 9f297ca commit (review #1) fixed
``PipelineOrchestrator._build_stage_payload`` to honour
single-quoted literals so ``rag_pipeline.yaml``'s ``vectorize`` stage
(``action: "'store'"``) runs at all.  That commit deferred a full
end-to-end smoke for the shipped pipeline because it benefited from a
shared mapping resolver that wasn't shipped.

This test fills the gap: it walks every stage in the shipped config,
builds a representative context from the previous stages' outputs,
and asserts ``_build_stage_payload`` resolves cleanly with the
literal-vs-path distinction landing on the right side.

Pre-fix, ``"'store'"`` would have raised ``KeyError`` because the
resolver tried to traverse it as a dot-path.  Post-fix the literal
unwraps to the string ``"store"``.  This test pins both behaviours.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from heddle.orchestrator.pipeline import PipelineOrchestrator

SHIPPED_RAG_PIPELINE = (
    Path(__file__).parent.parent / "configs" / "orchestrators" / "rag_pipeline.yaml"
)


def _load_shipped_pipeline() -> dict[str, Any]:
    with open(SHIPPED_RAG_PIPELINE) as f:
        return yaml.safe_load(f)


# Synthetic per-stage outputs covering every field the next stage
# references via its ``input_mapping``.  Adding a new stage to the
# shipped pipeline that pulls a new field from a prior stage will
# break this test until the field is added here — that's the point.
_SYNTHETIC_OUTPUTS: dict[str, dict[str, Any]] = {
    "ingest": {
        "posts": [{"id": 1, "text": "hello world"}],
        "channel_id": "channel-1",
        "channel_name": "Test Channel",
        "post_count": 1,
    },
    "chunk": {
        "chunks": [{"text": "hello", "post_id": 1}],
        "chunk_count": 1,
    },
    "vectorize": {
        "stored_count": 1,
    },
}

_GOAL_CONTEXT: dict[str, Any] = {
    "source_path": "/data/result-1.json",
    "source_paths": ["/data/result-1.json", "/data/result-2.json"],
}


def test_shipped_rag_pipeline_payload_mapping_smoke(tmp_path):
    """Every stage in ``rag_pipeline.yaml`` must build a payload cleanly.

    Walks the shipped pipeline in order, accumulating synthetic
    stage outputs into a context dict, and calls
    ``_build_stage_payload`` for each stage.  Asserts:

    - No ``KeyError`` from missing context paths.
    - The literal value ``"'store'"`` in the ``vectorize`` stage
      unwraps to the bare string ``"store"`` (the regression from
      9f297ca).
    - Every mapped field name reaches the payload.
    """
    config = _load_shipped_pipeline()
    stages = config["pipeline_stages"]
    assert stages, "rag_pipeline.yaml must define pipeline_stages"

    # The orchestrator constructor needs a config file path; point it
    # at the actual shipped config so we exercise the real load_config
    # + resolve_schema_refs path.
    orch = PipelineOrchestrator("rag-pipeline-smoke", str(SHIPPED_RAG_PIPELINE))

    context: dict[str, Any] = {"goal": {"context": _GOAL_CONTEXT}}

    for stage in stages:
        stage_name = stage["name"]
        payload = orch._build_stage_payload(stage, context)

        # Every input_mapping target field reaches the payload.
        mapping = stage.get("input_mapping", {})
        assert set(payload.keys()) == set(mapping.keys()), (
            f"Stage {stage_name!r}: payload keys do not match input_mapping keys"
        )

        # Add this stage's synthetic output to context for the next stage.
        if stage_name in _SYNTHETIC_OUTPUTS:
            context[stage_name] = {"output": _SYNTHETIC_OUTPUTS[stage_name]}

    # Specific pin for the 9f297ca regression: the vectorize stage's
    # literal ``"'store'"`` must resolve to the bare string ``"store"``,
    # not raise KeyError trying to traverse it as a path.
    vectorize_stage = next(s for s in stages if s["name"] == "vectorize")
    vectorize_payload = orch._build_stage_payload(
        vectorize_stage,
        {**context, "chunk": {"output": _SYNTHETIC_OUTPUTS["chunk"]}},
    )
    assert vectorize_payload["action"] == "store", (
        "Single-quoted literal in input_mapping must unwrap to the bare "
        "string; this is the contract 9f297ca pinned for the shipped "
        "rag_pipeline.yaml."
    )
    # And the path-resolved value ends up as a real list, not the
    # literal path string.
    assert vectorize_payload["chunks"] == _SYNTHETIC_OUTPUTS["chunk"]["chunks"]


def test_shipped_rag_pipeline_dependency_inference_matches_payload_paths():
    """Auto-inferred dependencies must match the stages whose outputs
    each input_mapping references.  Pins the contract that ``ingest``
    has no deps, ``chunk`` depends on ``ingest``, ``vectorize`` depends
    on ``chunk``.
    """
    config = _load_shipped_pipeline()
    stages = config["pipeline_stages"]

    deps = PipelineOrchestrator._infer_dependencies(stages)

    assert deps == {
        "ingest": set(),
        "chunk": {"ingest"},
        "vectorize": {"chunk"},
    }, (
        "Dependency inference for the shipped rag_pipeline must produce "
        "ingest -> chunk -> vectorize.  Adding a stage to the YAML that "
        "doesn't show up here means inference broke or the test needs an "
        "update; the YAML is the source of truth."
    )
