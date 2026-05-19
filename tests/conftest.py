"""Shared test fixtures and markers."""

import os

import pytest

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")


def _ollama_available() -> bool:
    try:
        import httpx

        r = httpx.get("http://localhost:11434/api/tags", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


def _deepeval_available() -> bool:
    try:
        import deepeval  # noqa: F401

        return True
    except ImportError:
        return False


skip_no_deepeval = pytest.mark.skipif(
    not _deepeval_available() or not _ollama_available(),
    reason="DeepEval or Ollama not available",
)


# heddle.contrib.events runtime fixtures — see tests/fixtures.py.
# Star-import so each fixture is registered as a top-level conftest
# fixture and is discoverable from any test in the tree.
from tests.fixtures import (  # noqa: E402, F401
    command_handler,
    in_memory_event_log,
    in_memory_rejection_log,
    membership_projector,
    registry_isolation,
    wired_dispatcher,
)
