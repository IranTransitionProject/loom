"""
Valkey-backed checkpoint store.

Production implementation of CheckpointStore using redis.asyncio (redis-py).
The redis-py client library works unchanged with Valkey.
Install with: pip install heddle-ai[redis]

Connection defaults:
    redis://redis:6379 — matches the Docker Compose / k8s service name.
    For local dev: redis://localhost:6379
"""

from __future__ import annotations

import redis.asyncio as redis

from heddle.orchestrator.store import CheckpointStore


class RedisCheckpointStore(CheckpointStore):
    """Valkey-backed checkpoint store (via redis-py client).

    Thin wrapper around redis.asyncio that implements the CheckpointStore
    interface. Handles connection lifecycle and TTL-based expiry natively.
    The redis-py client works unchanged with Valkey.
    """

    def __init__(self, redis_url: str = "redis://redis:6379") -> None:
        self._redis = redis.from_url(redis_url)

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        """Store a value with optional TTL."""
        if ttl_seconds:
            await self._redis.set(key, value, ex=ttl_seconds)
        else:
            await self._redis.set(key, value)

    async def get(self, key: str) -> str | None:
        """Retrieve a value by key."""
        result = await self._redis.get(key)
        if result is None:
            return None
        # redis.asyncio returns bytes by default
        if isinstance(result, bytes):
            return result.decode()
        return result

    async def aclose(self) -> None:
        """Close the underlying Redis client connection pool."""
        await self._redis.aclose()
