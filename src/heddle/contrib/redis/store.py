"""
Valkey-backed key-value store.

Production implementation of :class:`heddle.core.kvstore.KeyValueStore` using
``redis.asyncio`` (redis-py). The redis-py client library works unchanged
with Valkey.

Install with: ``pip install heddle-ai[redis]``.

Connection defaults:
    ``redis://redis:6379`` — matches the Docker Compose / k8s service name.
    For local dev: ``redis://localhost:6379``.
"""

from __future__ import annotations

import redis.asyncio as redis

from heddle.core.kvstore import KeyValueStore


class RedisKeyValueStore(KeyValueStore):
    """Valkey-backed key-value store (via redis-py client).

    Thin wrapper around ``redis.asyncio`` that implements the
    ``KeyValueStore`` interface. Handles connection lifecycle and TTL-based
    expiry natively. The redis-py client works unchanged with Valkey.
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
        """Retrieve a value, or ``None`` if missing/expired."""
        result = await self._redis.get(key)
        if result is None:
            return None
        if isinstance(result, bytes):
            return result.decode()
        return result

    async def set_if_not_exists(
        self, key: str, value: str, ttl_seconds: int
    ) -> bool:
        """Atomic SET NX EX (Valkey/Redis primitive)."""
        result = await self._redis.set(key, value, ex=ttl_seconds, nx=True)
        return bool(result)

    async def aclose(self) -> None:
        """Close the underlying Redis client connection pool."""
        await self._redis.aclose()


# Backward-compat alias. Existing code that imports RedisCheckpointStore
# continues to work. Removed at v1.0.
RedisCheckpointStore = RedisKeyValueStore


__all__ = ["RedisCheckpointStore", "RedisKeyValueStore"]
