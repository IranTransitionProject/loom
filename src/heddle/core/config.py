"""
Configuration loading and validation utilities.

All Heddle configs are YAML files. This module validates them at load time so
misconfigurations fail fast at startup — not at first-message time.

Validation functions return a list of error strings (empty = valid).  They
do NOT raise; callers decide how to handle errors (log, abort, collect).

Four config families are validated here:

- **Worker configs** — system prompt, I/O schemas, tier, timeout
- **Pipeline configs** — stage names, worker_types, input_mapping, conditions
- **Orchestrator configs** — name, system_prompt, checkpoint settings
- **Router rules** — tier_overrides, rate_limits

Scheduler and MCP configs have dedicated validators in their own modules.

See Also:
    configs/workers/_template.yaml — canonical worker config reference
    heddle.scheduler.config — scheduler config validation
    heddle.mcp.config — MCP gateway config validation
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

import structlog
import yaml

from heddle.core.mapping import is_mapping_literal

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Valid enum values used across configs
# ---------------------------------------------------------------------------

VALID_MODEL_TIERS = {"local", "standard", "frontier"}
VALID_PRIORITIES = {"low", "normal", "high", "critical"}
VALID_SCHEMA_TYPES = {"object", "string", "integer", "number", "boolean", "array"}


class ConfigValidationError(Exception):
    """Raised when a config file fails structural validation."""


def load_config(path: str | Path, *, resolve_refs: bool = True) -> dict[str, Any]:
    """Load a YAML config file and return as a dict.

    When *resolve_refs* is True (the default), ``input_schema_ref`` and
    ``output_schema_ref`` fields are resolved to JSON Schema via Pydantic
    model imports.  Pass ``resolve_refs=False`` when the referenced modules
    may not be importable (e.g., during config-only validation).

    Raises:
        FileNotFoundError: If the config file doesn't exist.
        yaml.YAMLError: If the file contains invalid YAML.
        ConfigValidationError: If the file parses to a non-dict (e.g., a list or scalar).
    """
    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ConfigValidationError(
            f"Config at {path}: expected YAML mapping, got {type(data).__name__}"
        )
    if resolve_refs:
        resolve_schema_refs(data)
    return data


def resolve_schema_refs(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve ``input_schema_ref`` / ``output_schema_ref`` to JSON Schema.

    If a config dict contains ``input_schema_ref`` or ``output_schema_ref``
    (a dotted Python path to a Pydantic model), import the model and call
    ``.model_json_schema()`` to populate the corresponding ``input_schema``
    / ``output_schema`` key.

    An explicit ``input_schema`` / ``output_schema`` key always takes
    precedence — the ref is only used when the inline schema is absent.

    This function mutates and returns *config* for convenience.

    Raises:
        ConfigValidationError: If the referenced path cannot be imported
            or the target is not a Pydantic BaseModel subclass.
    """
    for schema_key, ref_key in (
        ("input_schema", "input_schema_ref"),
        ("output_schema", "output_schema_ref"),
    ):
        ref = config.get(ref_key)
        if ref is None:
            continue
        if schema_key in config:
            # Explicit inline schema takes priority — skip resolution.
            continue

        config[schema_key] = _import_pydantic_schema(ref, ref_key)

    # Also resolve schema_refs inside pipeline_stages.
    for stage in config.get("pipeline_stages", []):
        if isinstance(stage, dict):
            for schema_key, ref_key in (
                ("input_schema", "input_schema_ref"),
                ("output_schema", "output_schema_ref"),
            ):
                ref = stage.get(ref_key)
                if ref is None:
                    continue
                if schema_key in stage:
                    continue
                stage[schema_key] = _import_pydantic_schema(ref, ref_key)

    return config


def _import_pydantic_schema(dotted_path: str, ref_key: str) -> dict[str, Any]:
    """Import a Pydantic model by dotted path and return its JSON Schema."""
    parts = dotted_path.rsplit(".", 1)
    if len(parts) != 2:
        raise ConfigValidationError(
            f"'{ref_key}' must be a fully qualified 'module.ClassName' path, got '{dotted_path}'"
        )
    module_path, class_name = parts
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise ConfigValidationError(
            f"'{ref_key}': cannot import module '{module_path}': {e}"
        ) from e

    cls = getattr(module, class_name, None)
    if cls is None:
        raise ConfigValidationError(
            f"'{ref_key}': module '{module_path}' has no attribute '{class_name}'"
        )

    # Check it's a Pydantic BaseModel.
    try:
        from pydantic import BaseModel
    except ImportError as e:  # pragma: no cover
        raise ConfigValidationError(
            f"'{ref_key}': pydantic is required for schema_ref resolution"
        ) from e

    if not (isinstance(cls, type) and issubclass(cls, BaseModel)):
        raise ConfigValidationError(
            f"'{ref_key}': '{dotted_path}' is not a Pydantic BaseModel subclass"
        )

    return cls.model_json_schema()


# ---------------------------------------------------------------------------
# Worker config validation
# ---------------------------------------------------------------------------

# Required top-level keys for each config type and their expected Python types.
_WORKER_REQUIRED: dict[str, type | None] = {
    "name": str,
}

_PIPELINE_REQUIRED: dict[str, type | None] = {
    "name": str,
    "pipeline_stages": list,
}

_ORCHESTRATOR_REQUIRED: dict[str, type | None] = {
    "name": str,
}


def validate_worker_config(  # noqa: PLR0912
    config: dict[str, Any], path: str | Path = "<unknown>"
) -> list[str]:
    """Validate a worker config dict.

    Checks for:

    - Required keys (name; system_prompt for LLM workers, processing_backend
      for processor workers)
    - Correct types for all known keys
    - Valid model tier values
    - Valid JSON Schema structure for input_schema/output_schema
    - Knowledge silos structural integrity
    - Numeric bounds (timeouts, token limits)
    """
    errors = _validate_base(config, _WORKER_REQUIRED, "worker", path)
    if not isinstance(config, dict):
        return errors

    pfx = f"worker config at {path}"

    # Determine worker kind — LLM workers need system_prompt, processors need backend.
    kind = config.get("worker_kind", "llm")
    if kind == "llm":
        if "system_prompt" not in config:
            errors.append(f"{pfx}: LLM worker missing required key 'system_prompt'")
        elif not isinstance(config["system_prompt"], str):
            errors.append(f"{pfx}: 'system_prompt' must be a string")
    elif kind == "processor":
        if "processing_backend" not in config:
            errors.append(f"{pfx}: processor worker missing 'processing_backend'")
        elif not isinstance(config["processing_backend"], str):
            errors.append(f"{pfx}: 'processing_backend' must be a string")
        else:
            errors.extend(_validate_processing_backend(config["processing_backend"], pfx))
    else:
        errors.append(f"{pfx}: 'worker_kind' must be 'llm' or 'processor', got '{kind}'")

    # Model tier
    tier = config.get("default_model_tier")
    if tier is not None and tier not in VALID_MODEL_TIERS:
        errors.append(
            f"{pfx}: 'default_model_tier' must be one of {sorted(VALID_MODEL_TIERS)}, got '{tier}'"
        )

    # Schema fields
    for schema_key in ("input_schema", "output_schema"):
        errors.extend(_validate_json_schema(config, schema_key, pfx))

    # Numeric bounds
    for key in ("timeout_seconds", "max_input_tokens", "max_output_tokens"):
        if key in config:
            val = config[key]
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                errors.append(f"{pfx}: '{key}' must be a number, got {type(val).__name__}")
            elif val <= 0:
                errors.append(f"{pfx}: '{key}' must be positive, got {val}")

    # reset_after_task must be true (workers are stateless)
    rat = config.get("reset_after_task")
    if rat is not None and rat is not True:
        errors.append(f"{pfx}: 'reset_after_task' must be true (workers are stateless)")

    # Knowledge silos
    if "knowledge_silos" in config:
        errors.extend(_validate_knowledge_silos(config["knowledge_silos"], path))

    # Knowledge sources (legacy)
    if "knowledge_sources" in config:
        ks = config["knowledge_sources"]
        if not isinstance(ks, list):
            errors.append(f"{pfx}: 'knowledge_sources' must be a list")
        else:
            for i, source in enumerate(ks):
                if (
                    isinstance(source, dict)
                    and "required" in source
                    and not isinstance(source["required"], bool)
                ):
                    errors.append(f"{pfx}: knowledge_sources[{i}]: 'required' must be a boolean")

    # File-ref resolution
    if "resolve_file_refs" in config:
        if not isinstance(config["resolve_file_refs"], list):
            errors.append(f"{pfx}: 'resolve_file_refs' must be a list of field names")
        if "workspace_dir" not in config:
            errors.append(f"{pfx}: 'resolve_file_refs' requires 'workspace_dir' to be set")

    return errors


# ---------------------------------------------------------------------------
# Pipeline config validation
# ---------------------------------------------------------------------------


def validate_pipeline_config(  # noqa: PLR0912, PLR0915
    config: dict[str, Any], path: str | Path = "<unknown>"
) -> list[str]:
    """Validate a pipeline orchestrator config.

    Checks for:
    - Required keys (name, pipeline_stages)
    - Each stage has name and worker_type
    - Stage names are unique
    - input_mapping values are valid dot-notation paths
    - depends_on references exist as stage names
    - condition syntax is valid (3-part: path op value)
    - Tier values are valid
    - No circular dependencies (basic check)
    """
    errors = _validate_base(config, _PIPELINE_REQUIRED, "pipeline", path)
    if not isinstance(config, dict):
        return errors

    pfx = f"pipeline config at {path}"
    stages = config.get("pipeline_stages", [])
    if not isinstance(stages, list):
        return errors  # Already caught by _validate_base

    # Timeout
    if "timeout_seconds" in config:
        _check_positive_number(config, "timeout_seconds", pfx, errors)

    # max_concurrent_goals
    mcg = config.get("max_concurrent_goals")
    if mcg is not None and (not isinstance(mcg, int) or isinstance(mcg, bool) or mcg < 1):
        errors.append(f"{pfx}: 'max_concurrent_goals' must be a positive integer")

    # Collect stage names for cross-reference validation.
    stage_names: set[str] = set()
    seen_names: list[str] = []

    for i, stage in enumerate(stages):
        sp = f"{pfx}: pipeline_stages[{i}]"
        if not isinstance(stage, dict):
            errors.append(f"{sp}: expected dict, got {type(stage).__name__}")
            continue

        # Required fields
        sname = stage.get("name")
        if sname is None:
            errors.append(f"{sp}: missing required key 'name'")
        elif not isinstance(sname, str):
            errors.append(f"{sp}: 'name' must be a string")
        else:
            if sname in stage_names:
                errors.append(f"{sp}: duplicate stage name '{sname}'")
            stage_names.add(sname)
            seen_names.append(sname)

        if "worker_type" not in stage:
            errors.append(f"{sp}: missing required key 'worker_type'")
        elif not isinstance(stage["worker_type"], str):
            errors.append(f"{sp}: 'worker_type' must be a string")

        # Tier validation
        tier = stage.get("tier")
        if tier is not None and tier not in VALID_MODEL_TIERS:
            errors.append(f"{sp}: 'tier' must be one of {sorted(VALID_MODEL_TIERS)}, got '{tier}'")

        # input_mapping validation
        mapping = stage.get("input_mapping")
        if mapping is not None:
            if not isinstance(mapping, dict):
                errors.append(f"{sp}: 'input_mapping' must be a dict")
            else:
                for target, source_path in mapping.items():
                    if not isinstance(source_path, str):
                        errors.append(f"{sp}: input_mapping['{target}'] must be a string path")
                    elif not source_path:
                        errors.append(f"{sp}: input_mapping['{target}'] must not be empty")
                    elif (
                        isinstance(source_path, str)
                        and source_path
                        and not is_mapping_literal(source_path)
                    ):
                        # Validate source path references goal.* or an existing stage
                        # (literal values wrapped in single quotes are skipped)
                        root = source_path.split(".")[0]
                        if root != "goal" and root not in stage_names:
                            errors.append(
                                f"{sp}: input_mapping['{target}'] references unknown "
                                f"source '{root}' (must be 'goal' or a preceding "
                                f"stage name)"
                            )

        # depends_on validation
        deps = stage.get("depends_on")
        if deps is not None:
            if not isinstance(deps, list):
                errors.append(f"{sp}: 'depends_on' must be a list")
            else:
                for dep in deps:
                    if not isinstance(dep, str):
                        errors.append(f"{sp}: depends_on entries must be strings")
                    elif dep not in stage_names and dep not in seen_names:
                        errors.append(f"{sp}: depends_on references unknown stage '{dep}'")

        # condition syntax check
        cond = stage.get("condition")
        if cond is not None:
            if not isinstance(cond, str):
                errors.append(f"{sp}: 'condition' must be a string")
            else:
                parts = cond.split()
                if len(parts) != 3:
                    errors.append(
                        f"{sp}: 'condition' must be 'path op value' "
                        f"(3 space-separated parts), got {len(parts)} parts"
                    )
                elif parts[1] not in ("==", "!="):
                    errors.append(
                        f"{sp}: condition operator must be '==' or '!=', got '{parts[1]}'"
                    )

        # Per-stage timeout
        if "timeout_seconds" in stage:
            _check_positive_number(stage, "timeout_seconds", sp, errors)

        # Per-stage I/O schemas (for inter-stage contract validation)
        for schema_key in ("input_schema", "output_schema"):
            if schema_key in stage:
                errors.extend(_validate_json_schema(stage, schema_key, sp))

    return errors


# ---------------------------------------------------------------------------
# Orchestrator config validation
# ---------------------------------------------------------------------------


def validate_orchestrator_config(  # noqa: PLR0912
    config: dict[str, Any], path: str | Path = "<unknown>"
) -> list[str]:
    """Validate a dynamic orchestrator (OrchestratorActor) config.

    Checks for:
    - Required keys (name)
    - system_prompt presence (warned, not error — could be in config)
    - checkpoint settings structure
    - Numeric bounds (timeouts, concurrency)
    - available_workers structure
    """
    errors = _validate_base(config, _ORCHESTRATOR_REQUIRED, "orchestrator", path)
    if not isinstance(config, dict):
        return errors

    pfx = f"orchestrator config at {path}"

    # System prompt (required for LLM-driven orchestrator)
    if "system_prompt" not in config:
        errors.append(f"{pfx}: missing 'system_prompt' (required for LLM orchestrator)")
    elif not isinstance(config["system_prompt"], str):
        errors.append(f"{pfx}: 'system_prompt' must be a string")

    # Checkpoint settings
    cp = config.get("checkpoint")
    if cp is not None:
        if not isinstance(cp, dict):
            errors.append(f"{pfx}: 'checkpoint' must be a dict")
        else:
            tt = cp.get("token_threshold")
            if tt is not None and (not isinstance(tt, int) or isinstance(tt, bool) or tt < 1):
                errors.append(f"{pfx}: checkpoint.token_threshold must be a positive integer")
            rw = cp.get("recent_window")
            if rw is not None and (not isinstance(rw, int) or isinstance(rw, bool) or rw < 0):
                errors.append(f"{pfx}: checkpoint.recent_window must be a non-negative integer")

    # Concurrency and timeout
    for key in ("max_concurrent_goals", "max_concurrent_tasks"):
        val = config.get(key)
        if val is not None and (not isinstance(val, int) or isinstance(val, bool) or val < 1):
            errors.append(f"{pfx}: '{key}' must be a positive integer")

    if "timeout_seconds" in config:
        _check_positive_number(config, "timeout_seconds", pfx, errors)

    # available_workers list
    aw = config.get("available_workers")
    if aw is not None:
        if not isinstance(aw, list):
            errors.append(f"{pfx}: 'available_workers' must be a list")
        else:
            for i, w in enumerate(aw):
                wp = f"{pfx}: available_workers[{i}]"
                if not isinstance(w, dict):
                    errors.append(f"{wp}: expected dict")
                    continue
                if "name" not in w:
                    errors.append(f"{wp}: missing required key 'name'")
                if "description" not in w:
                    errors.append(f"{wp}: missing required key 'description'")

    return errors


# ---------------------------------------------------------------------------
# Router rules validation
# ---------------------------------------------------------------------------


def validate_router_rules(  # noqa: PLR0912
    config: dict[str, Any], path: str | Path = "<unknown>"
) -> list[str]:
    """Validate router_rules.yaml.

    Checks for:
    - Top-level must be a dict
    - tier_overrides values are valid ModelTier values
    - rate_limits have valid structure (max_concurrent > 0)
    - rate_limits keys are valid tier names
    """
    errors: list[str] = []
    if not isinstance(config, dict):
        return [f"router rules at {path}: expected dict, got {type(config).__name__}"]

    pfx = f"router rules at {path}"

    # tier_overrides
    overrides = config.get("tier_overrides")
    if overrides is not None:
        if not isinstance(overrides, dict):
            errors.append(f"{pfx}: 'tier_overrides' must be a dict")
        else:
            for wtype, tier in overrides.items():
                if not isinstance(tier, str):
                    errors.append(f"{pfx}: tier_overrides['{wtype}'] must be a string")
                elif tier not in VALID_MODEL_TIERS:
                    errors.append(
                        f"{pfx}: tier_overrides['{wtype}'] = '{tier}' is not a "
                        f"valid tier (must be one of {sorted(VALID_MODEL_TIERS)})"
                    )

    # rate_limits
    limits = config.get("rate_limits")
    if limits is not None:
        if not isinstance(limits, dict):
            errors.append(f"{pfx}: 'rate_limits' must be a dict")
        else:
            for tier_name, limit_cfg in limits.items():
                lp = f"{pfx}: rate_limits['{tier_name}']"
                if tier_name not in VALID_MODEL_TIERS:
                    errors.append(
                        f"{lp}: unknown tier (must be one of {sorted(VALID_MODEL_TIERS)})"
                    )
                if not isinstance(limit_cfg, dict):
                    errors.append(f"{lp}: must be a dict")
                    continue
                mc = limit_cfg.get("max_concurrent")
                if mc is not None and (not isinstance(mc, int) or isinstance(mc, bool) or mc < 1):
                    errors.append(f"{lp}: 'max_concurrent' must be a positive integer")
                tpm = limit_cfg.get("tokens_per_minute")
                if tpm is not None and (
                    not isinstance(tpm, (int, float)) or isinstance(tpm, bool) or tpm <= 0
                ):
                    errors.append(f"{lp}: 'tokens_per_minute' must be a positive number")

    return errors


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_base(
    config: dict[str, Any],
    required: dict[str, type | None],
    config_type: str,
    path: str | Path,
) -> list[str]:
    """Check that required keys exist and have correct types."""
    errors: list[str] = []
    if not isinstance(config, dict):
        return [f"{config_type} config at {path}: expected dict, got {type(config).__name__}"]

    for key, expected_type in required.items():
        if key not in config:
            errors.append(f"{config_type} config at {path}: missing required key '{key}'")
        elif expected_type is not None and not isinstance(config[key], expected_type):
            errors.append(
                f"{config_type} config at {path}: key '{key}' expected "
                f"{expected_type.__name__}, got {type(config[key]).__name__}"
            )

    return errors


def _validate_json_schema(config: dict[str, Any], schema_key: str, pfx: str) -> list[str]:
    """Validate that a schema field is a well-formed JSON Schema object."""
    errors: list[str] = []
    if schema_key not in config:
        return errors
    schema = config[schema_key]

    if not isinstance(schema, dict):
        errors.append(
            f"{pfx}: '{schema_key}' should be a dict (JSON Schema object), "
            f"got {type(schema).__name__}"
        )
        return errors

    # Schema type check
    stype = schema.get("type")
    if stype is not None and stype not in VALID_SCHEMA_TYPES:
        errors.append(f"{pfx}: {schema_key}.type '{stype}' is not a valid JSON Schema type")

    # Required must be a list of strings
    req = schema.get("required")
    if req is not None:
        if not isinstance(req, list):
            errors.append(f"{pfx}: {schema_key}.required must be a list")
        elif not all(isinstance(r, str) for r in req):
            errors.append(f"{pfx}: {schema_key}.required entries must be strings")

    # Properties must be a dict of dicts
    props = schema.get("properties")
    if props is not None:
        if not isinstance(props, dict):
            errors.append(f"{pfx}: {schema_key}.properties must be a dict")
        else:
            for fname, fschema in props.items():
                if not isinstance(fschema, dict):
                    errors.append(f"{pfx}: {schema_key}.properties['{fname}'] must be a dict")

    return errors


def _check_positive_number(config: dict[str, Any], key: str, pfx: str, errors: list[str]) -> None:
    """Check that config[key] is a positive number."""
    val = config[key]
    if not isinstance(val, (int, float)) or isinstance(val, bool):
        errors.append(f"{pfx}: '{key}' must be a number, got {type(val).__name__}")
    elif val <= 0:
        errors.append(f"{pfx}: '{key}' must be positive, got {val}")


def _validate_processing_backend(backend: str, pfx: str) -> list[str]:
    """Validate that a processing_backend value is a valid dotted Python import path.

    Must have at least two dot-separated segments, and every segment must be a
    valid Python identifier (e.g., ``mypackage.backends.MyBackend``).
    """
    errors: list[str] = []
    segments = backend.split(".")
    if len(segments) < 2:
        errors.append(
            f"{pfx}: 'processing_backend' must be a fully qualified class path "
            f"with at least two segments (e.g., 'mypackage.backends.MyBackend'), "
            f"got '{backend}'"
        )
        return errors
    errors.extend(
        f"{pfx}: 'processing_backend' segment '{seg}' is not a valid "
        f"Python identifier in '{backend}'"
        for seg in segments
        if not seg.isidentifier()
    )
    return errors


def _validate_knowledge_silos(  # noqa: PLR0912
    silos: Any,
    path: str | Path,
) -> list[str]:
    """Validate the knowledge_silos config section.

    Each silo must have ``name`` (str) and ``type`` (str).
    Folder silos must have ``path`` (str).
    Tool silos must have ``provider`` (str) and ``config`` (dict).
    """
    errors: list[str] = []

    if not isinstance(silos, list):
        return [f"config at {path}: 'knowledge_silos' should be a list, got {type(silos).__name__}"]

    for i, silo in enumerate(silos):
        prefix = f"config at {path}: knowledge_silos[{i}]"

        if not isinstance(silo, dict):
            errors.append(f"{prefix}: expected dict, got {type(silo).__name__}")
            continue

        # Required fields for all silo types
        if "name" not in silo:
            errors.append(f"{prefix}: missing required key 'name'")
        elif not isinstance(silo["name"], str):
            errors.append(f"{prefix}: 'name' must be a string")

        if "type" not in silo:
            errors.append(f"{prefix}: missing required key 'type'")
            continue
        if not isinstance(silo["type"], str):
            errors.append(f"{prefix}: 'type' must be a string")
            continue

        # ``required`` (optional, default True) — when True, a missing
        # silo at runtime raises RequiredKnowledgeMissingError; when
        # False, log + skip + record on TaskResult.metadata.  Validate
        # the type here so a typo like ``required: "yes"`` is caught
        # at config-load time rather than producing surprising runtime
        # behaviour.
        if "required" in silo and not isinstance(silo["required"], bool):
            errors.append(f"{prefix}: 'required' must be a boolean if present")

        silo_type = silo["type"]

        if silo_type == "folder":
            if "path" not in silo:
                errors.append(f"{prefix}: folder silo missing required key 'path'")
            elif not isinstance(silo["path"], str):
                errors.append(f"{prefix}: 'path' must be a string")

            permissions = silo.get("permissions", "read")
            if permissions not in ("read", "read_write"):
                errors.append(
                    f"{prefix}: 'permissions' must be 'read' or 'read_write', got '{permissions}'"
                )

        elif silo_type == "tool":
            if "provider" not in silo:
                errors.append(f"{prefix}: tool silo missing required key 'provider'")
            elif not isinstance(silo["provider"], str):
                errors.append(f"{prefix}: 'provider' must be a string")

            if "config" in silo and not isinstance(silo["config"], dict):
                errors.append(f"{prefix}: 'config' must be a dict")

        else:
            errors.append(
                f"{prefix}: unknown silo type '{silo_type}' (expected 'folder' or 'tool')"
            )

    return errors
