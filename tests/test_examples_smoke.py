"""Smoke tests for the ``examples/`` directory.

The examples directory contains five reference apps (blind-taste-test,
debate-arena, town-hall, document-intake, research-review).  They are
the project's public "this is how you actually use Heddle" surface —
which means a silent config drift in any of them is a slow-burn bug
that only an operator hitting the example first notices.

What these smoke tests cover:

- Every YAML config under ``examples/*/phase-*/{workers,orchestrators}/``
  passes the appropriate framework validator.
- Every council config referenced by an example's ``run.py``
  (``configs/councils/{name}.yaml``) passes ``validate_council_config``.
- Every example's ``run.py`` is importable as a module (catches
  dependency drift, syntax errors, missing modules) without actually
  invoking ``main()`` — examples are meant to be run with live LLM
  backends and that is not a unit-test concern.

What they intentionally do NOT cover:

- The behaviour of running an example end-to-end.  That requires
  live LLM backends, real NATS, and real config of API keys —
  belongs in a manual or integration suite (``@pytest.mark.integration``)
  not here.
- Whether ``--help`` works on each ``run.py``.  ``click``'s CLI
  surface is exercised by the import test indirectly; a regression
  in ``click`` integration would surface there.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

from heddle.contrib.council.config import validate_council_config
from heddle.core.config import (
    validate_orchestrator_config,
    validate_pipeline_config,
    validate_worker_config,
)

REPO_ROOT = Path(__file__).parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples"
COUNCILS_DIR = REPO_ROOT / "configs" / "councils"


def _load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Worker configs under examples/*/phase-*/workers/*.yaml
# ---------------------------------------------------------------------------

_EXAMPLE_WORKER_CONFIGS = sorted(EXAMPLES_DIR.glob("*/phase-*/workers/*.yaml"))


@pytest.mark.parametrize(
    "config_path",
    _EXAMPLE_WORKER_CONFIGS,
    ids=[str(p.relative_to(EXAMPLES_DIR)) for p in _EXAMPLE_WORKER_CONFIGS],
)
def test_example_worker_config_valid(config_path: Path):
    """Every worker config shipped under examples/ must validate."""
    cfg = _load_yaml(config_path)
    errors = validate_worker_config(cfg, config_path)
    assert errors == [], (
        f"Validation errors in {config_path.relative_to(EXAMPLES_DIR)}:\n"
        + "\n".join(f"  - {e}" for e in errors)
    )


# ---------------------------------------------------------------------------
# Orchestrator / pipeline configs under examples/*/phase-*/orchestrators/
# ---------------------------------------------------------------------------


def _is_pipeline_config(path: Path) -> bool:
    try:
        cfg = _load_yaml(path)
        return isinstance(cfg, dict) and "pipeline_stages" in cfg
    except Exception:
        return False


_EXAMPLE_ORCHESTRATOR_CONFIGS = sorted(EXAMPLES_DIR.glob("*/phase-*/orchestrators/*.yaml"))
_EXAMPLE_PIPELINE_CONFIGS = [p for p in _EXAMPLE_ORCHESTRATOR_CONFIGS if _is_pipeline_config(p)]
_EXAMPLE_NONPIPELINE_ORCHESTRATORS = [
    p for p in _EXAMPLE_ORCHESTRATOR_CONFIGS if p not in _EXAMPLE_PIPELINE_CONFIGS
]


@pytest.mark.parametrize(
    "config_path",
    _EXAMPLE_PIPELINE_CONFIGS,
    ids=[str(p.relative_to(EXAMPLES_DIR)) for p in _EXAMPLE_PIPELINE_CONFIGS],
)
def test_example_pipeline_config_valid(config_path: Path):
    """Every pipeline orchestrator config under examples/ must validate."""
    cfg = _load_yaml(config_path)
    errors = validate_pipeline_config(cfg, config_path)
    assert errors == [], (
        f"Validation errors in {config_path.relative_to(EXAMPLES_DIR)}:\n"
        + "\n".join(f"  - {e}" for e in errors)
    )


# ``examples/`` currently ships only pipeline-shaped orchestrators
# (research-review, document-intake) — the non-pipeline category is
# empty.  Keeping the test stub here so a future non-pipeline
# orchestrator example automatically gets the same validation.
if _EXAMPLE_NONPIPELINE_ORCHESTRATORS:

    @pytest.mark.parametrize(
        "config_path",
        _EXAMPLE_NONPIPELINE_ORCHESTRATORS,
        ids=[str(p.relative_to(EXAMPLES_DIR)) for p in _EXAMPLE_NONPIPELINE_ORCHESTRATORS],
    )
    def test_example_orchestrator_config_valid(config_path: Path):
        """Every non-pipeline orchestrator config under examples/ must validate."""
        cfg = _load_yaml(config_path)
        errors = validate_orchestrator_config(cfg, config_path)
        assert errors == [], (
            f"Validation errors in {config_path.relative_to(EXAMPLES_DIR)}:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )


# ---------------------------------------------------------------------------
# Council configs referenced by example run.py scripts
# ---------------------------------------------------------------------------

# Each council-example's run.py loads exactly one council config from
# configs/councils/.  We don't try to discover this dynamically (the
# pattern would brittle); the explicit mapping is the contract and the
# test asserts it.
_COUNCIL_EXAMPLES: dict[str, str] = {
    "blind-taste-test": "blind_taste_test.yaml",
    "debate-arena": "debate_arena.yaml",
    "town-hall": "town_hall_debate.yaml",
}


@pytest.mark.parametrize(
    ("example_name", "council_file"),
    list(_COUNCIL_EXAMPLES.items()),
    ids=list(_COUNCIL_EXAMPLES.keys()),
)
def test_example_council_config_valid(example_name: str, council_file: str):
    """The council config each example's run.py references must validate.

    If an example renames or moves its council config, this fails loud
    — preferable to a runtime ``ConfigError`` for the operator who
    just ran ``examples/blind-taste-test/run.py``.
    """
    config_path = COUNCILS_DIR / council_file
    assert config_path.exists(), (
        f"Example {example_name} references missing council config: {council_file}"
    )
    cfg = _load_yaml(config_path)
    errors = validate_council_config(cfg)
    assert errors == [], f"Validation errors in {council_file}:\n" + "\n".join(
        f"  - {e}" for e in errors
    )


# ---------------------------------------------------------------------------
# Importability of every example's run.py
# ---------------------------------------------------------------------------

_EXAMPLE_RUN_SCRIPTS = sorted(EXAMPLES_DIR.glob("*/run.py"))


@pytest.mark.parametrize(
    "script_path",
    _EXAMPLE_RUN_SCRIPTS,
    ids=[str(p.parent.name) for p in _EXAMPLE_RUN_SCRIPTS],
)
def test_example_run_script_importable(script_path: Path):
    """Each example's run.py loads without error.

    This catches dependency drift (a renamed module under
    ``heddle.contrib``, a removed function in
    ``heddle.contrib.council``), syntax errors, and any import-time
    side effect that would explode under a fresh checkout.

    We do not invoke ``main()`` — running an example end-to-end is
    not a unit-test concern.  The import is sufficient because every
    example uses click decorators at module level, so the click
    plumbing is exercised as a side effect.
    """
    import sys

    module_name = f"_example_smoke_{script_path.parent.name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass field-type lookup (which
    # does ``sys.modules.get(cls.__module__).__dict__``) succeeds.
    # Without this, examples that declare dataclasses at module
    # scope fail with AttributeError on the synthetic module.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
