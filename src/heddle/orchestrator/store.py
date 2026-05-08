"""
Checkpoint storage abstraction.

Defines the CheckpointStore ABC and an in-memory implementation for testing.
Production deployments use RedisCheckpointStore from heddle.contrib.redis.store.

Storage contract:
    set(key, value, ttl_seconds) — persist a string value with optional expiry
    get(key) — retrieve a string value (or None if missing/expired)

This is intentionally minimal. The CheckpointManager handles serialization
and key naming; the store is just a key-value backend.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod


class CheckpointStore(ABC):
    """Abstract key-value store for checkpoint persistence.

    Implementations must handle:
    - String key-value storage
    - TTL-based expiration (best-effort; lazy expiry is acceptable)
    - Returning None for missing or expired keys
    """

    @abstractmethod
    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        """Store a value with optional TTL."""
        ...

    @abstractmethod
    async def get(self, key: str) -> str | None:
        """Retrieve a value, or None if missing/expired."""
        ...

    async def aclose(self) -> None:  # noqa: B027 — intentional no-op default
        """Release any I/O resources held by this store.

        Subclasses that hold open client connections (e.g. Redis)
        override this to close them.  Idempotent — safe to call more
        than once.  Default is a no-op.
        """


class InMemoryCheckpointStore(CheckpointStore):
    """In-memory checkpoint store for testing and local development.

    Values are stored in a dict with optional expiry timestamps.
    Expiry is checked lazily on get() — no background cleanup.
    """

    def __init__(self) -> None:
        # Maps key -> (value, expires_at | None)
        self._data: dict[str, tuple[str, float | None]] = {}

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        """Store a value with optional TTL."""
        expires_at = time.monotonic() + ttl_seconds if ttl_seconds else None
        self._data[key] = (value, expires_at)

    async def get(self, key: str) -> str | None:
        """Retrieve a value, or None if missing/expired."""
        entry = self._data.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and time.monotonic() > expires_at:
            del self._data[key]
            return None
        return value
