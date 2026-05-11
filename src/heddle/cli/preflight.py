"""
Pre-flight checks for Heddle CLI commands.

Provides reusable checks that run before long-lived actors start:
- NATS connectivity (hard fail if unreachable)
- Environment variable presence (warnings for missing keys)
- Config file readability and YAML validity

All checks are designed to be fast (short timeouts) and produce
actionable error messages with fix suggestions.
"""

from __future__ import annotations

import os

import nats as nats_lib


async def check_nats_connectivity(nats_url: str, timeout: float = 5.0) -> tuple[bool, str]:
    """Try to connect to NATS. Returns (success, message).

    Attempts a quick connect/disconnect cycle to verify the NATS server
    is reachable. Uses a short timeout to avoid blocking startup.

    Args:
        nats_url: NATS server URL (e.g., ``nats://localhost:4222``).
        timeout: Connection timeout in seconds.

    Returns:
        Tuple of (success, message). On failure the message includes
        an actionable fix suggestion.
    """
    try:
        # ``connect_timeout`` is documented as ``float`` in nats-py.
        # Earlier this cast to ``int`` silently truncated values
        # below 1.0 to 0, disabling the timeout entirely; pass the
        # float through unchanged.
        nc = await nats_lib.connect(nats_url, connect_timeout=timeout)
        await nc.drain()
        return (True, f"Connected to NATS at {nats_url}")
    except Exception as exc:
        return (
            False,
            f"Cannot connect to NATS at {nats_url}. Is NATS running? "
            f"Try: docker run -p 4222:4222 nats:latest  (error: {exc})",
        )


def check_env_vars(tier: str) -> list[str]:
    """Check required env vars based on model tier. Returns list of warnings.

    Each tier relies on specific environment variables for its LLM backend.
    Missing variables produce warnings (not errors) because the operator
    may set them later or use a different backend.

    Args:
        tier: Model tier — ``"local"``, ``"standard"``, or ``"frontier"``.

    Returns:
        List of warning strings. Empty if all expected vars are present.
    """
    warnings: list[str] = []

    # TODO(local-runtime-registry): replace this hardcoded LM Studio /
    # Ollama check with a loop over the planned ``LocalRuntime`` registry
    # in ``heddle.worker.backends`` so adding a new local-tier runtime
    # (Exo, vLLM, …) does not require editing this function.
    if tier == "local":
        if not os.getenv("LM_STUDIO_URL") and not os.getenv("OLLAMA_URL"):
            warnings.append(
                "Neither LM_STUDIO_URL nor OLLAMA_URL is set. "
                "Local tier workers need one of them. "
                "Set: export LM_STUDIO_URL=http://localhost:1234/v1  "
                "or:  export OLLAMA_URL=http://localhost:11434"
            )
    elif tier in ("standard", "frontier") and not os.getenv("ANTHROPIC_API_KEY"):
        warnings.append(
            f"ANTHROPIC_API_KEY not set. Required for {tier} tier workers. "
            "Set it with: export ANTHROPIC_API_KEY=your-key"
        )

    # Check for OpenAI-compatible backend vars (informational).
    if not os.getenv("OPENAI_API_KEY") and not os.getenv("OPENAI_BASE_URL"):
        # Only warn if no other backend is configured for the tier.
        pass  # Not a warning — OpenAI-compatible is optional.

    return warnings


def check_config_readable(config_path: str) -> tuple[bool, str]:
    """Check that config file exists and is valid YAML.

    Args:
        config_path: Path to the YAML config file.

    Returns:
        Tuple of (success, message). On failure the message describes
        the specific problem (file not found, invalid YAML, etc.).
    """
    import yaml

    if not os.path.isfile(config_path):
        return (False, f"Config file not found: {config_path}")

    try:
        with open(config_path) as f:
            data = yaml.safe_load(f)
        if data is None:
            return (False, f"Config file is empty: {config_path}")
        return (True, f"Config file is valid YAML: {config_path}")
    except yaml.YAMLError as exc:
        return (False, f"Config file has invalid YAML: {config_path} ({exc})")
