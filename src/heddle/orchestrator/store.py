"""
Backward-compatibility shim.

This module originally defined ``CheckpointStore`` and
``InMemoryCheckpointStore``. The store abstraction is now general-purpose
and lives in :mod:`heddle.core.kvstore`. The old names continue to work as
aliases for the new ones.

Migration: new code should import from ``heddle.core.kvstore`` directly.
Existing imports from this module are kept working through the v0.x series
and will be removed in v1.0.
"""

from __future__ import annotations

from heddle.core.kvstore import (
    InMemoryKeyValueStore as InMemoryCheckpointStore,
)
from heddle.core.kvstore import (
    KeyValueStore as CheckpointStore,
)

__all__ = ["CheckpointStore", "InMemoryCheckpointStore"]
