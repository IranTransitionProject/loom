"""Test fixtures local to tests/contrib/events/.

Sprint 2 T2/T3 ships only ``registry_isolation`` here. T10 lifts the
full fixture set into ``heddle/tests/fixtures.py`` and re-exports
from the top-level ``conftest.py``; this file is the scaffold until
that lands.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from heddle.contrib.events.registry import AGGREGATE_REGISTRY


@pytest.fixture
def registry_isolation() -> Iterator[None]:
    """Snapshot and restore AGGREGATE_REGISTRY around a test.

    Use in any test that registers new aggregates (including
    accidentally via importing modules that decorate at import).
    Imports that happened BEFORE the fixture activates remain
    registered; only test-local additions are rolled back.
    """
    snapshot = dict(AGGREGATE_REGISTRY)
    yield
    AGGREGATE_REGISTRY.clear()
    AGGREGATE_REGISTRY.update(snapshot)
