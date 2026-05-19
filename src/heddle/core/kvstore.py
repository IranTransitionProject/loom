"""
Generic TTL-aware key-value store for cross-process state.

This ABC is the substrate for any Heddle component that needs string-keyed
persistence with optional expiry: orchestrator checkpoint compression,
aggregate snapshots (heddle.contrib.events), distributed locks, and so on.

Two implementations ship with heddle:
- ``InMemoryKeyValueStore`` (this module) — for tests and local development.
- ``RedisKeyValueStore`` (``heddle.contrib.redis.store``) — for production,
  backed by Valkey via the redis-py client.

Domain conventions
------------------
Keys are namespaced by *domain*. A domain is a stable name (e.g. ``"checkpoint"``)
mapped to a canonical key prefix (e.g. ``"heddle:checkpoint:"``). Consumers
register their domain at import time via ``register_domain()`` and look up the
prefix via ``domain_prefix()`` or ``make_key()``.

For consumers that prefer a handle whose keys are prefixed transparently, call
``store.scope(prefix)`` or ``scoped(store, domain_name)`` to get a
``ScopedKeyValueStore`` view.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

# ---------------------------------------------------------------------------
# ABC and implementations
# ---------------------------------------------------------------------------


class KeyValueStore(ABC):
    """Abstract TTL-aware key-value store.

    Implementations must handle:

    - String key-value storage.
    - TTL-based expiration (best-effort; lazy expiry is acceptable).
    - Returning ``None`` for missing or expired keys.
    """

    @abstractmethod
    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        """Store a value with optional TTL."""
        ...

    @abstractmethod
    async def get(self, key: str) -> str | None:
        """Retrieve a value, or ``None`` if missing/expired."""
        ...

    @abstractmethod
    async def set_if_not_exists(self, key: str, value: str, ttl_seconds: int) -> bool:
        """Atomically set ``key`` to ``value`` with TTL only if absent.

        Returns ``True`` if the value was stored (caller "won the
        race"), ``False`` if the key already existed (caller lost).
        TTL is mandatory — the SETNX-without-TTL footgun (orphaned
        leases on crash) is explicitly disallowed.

        Used by :func:`heddle.contrib.events.lease.finalization_lease`
        and any other coordination primitive that needs a single-writer
        guarantee with bounded recovery.
        """
        ...

    async def aclose(self) -> None:  # noqa: B027 — intentional no-op default
        """Release any I/O resources held by this store.

        Subclasses that hold open client connections override this to close
        them. Idempotent — safe to call more than once. Default is a no-op.
        """

    def scope(self, prefix: str) -> ScopedKeyValueStore:
        """Return a view that transparently prepends ``prefix`` to all keys.

        The view implements the same ``KeyValueStore`` interface, so it nests
        cleanly — a scoped view of a scoped view concatenates the prefixes.
        ``aclose()`` on a scoped view is a no-op; the underlying store owns
        the connection.
        """
        return ScopedKeyValueStore(self, prefix)


class InMemoryKeyValueStore(KeyValueStore):
    """In-memory key-value store for tests and local development.

    Values are stored in a dict with optional expiry timestamps.
    Expiry is checked lazily on ``get()`` — no background cleanup.
    """

    def __init__(self) -> None:
        # Maps key -> (value, expires_at | None)
        self._data: dict[str, tuple[str, float | None]] = {}

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        """Store a value with optional TTL."""
        expires_at = time.monotonic() + ttl_seconds if ttl_seconds else None
        self._data[key] = (value, expires_at)

    async def get(self, key: str) -> str | None:
        """Retrieve a value, or ``None`` if missing/expired."""
        entry = self._data.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and time.monotonic() > expires_at:
            del self._data[key]
            return None
        return value

    async def set_if_not_exists(self, key: str, value: str, ttl_seconds: int) -> bool:
        """Atomic set-if-absent. Lazily expires the existing entry first.

        Single-event-loop atomicity is sufficient for the test/dev use
        cases this store targets; multi-process coordination requires
        :class:`RedisKeyValueStore`.
        """
        entry = self._data.get(key)
        if entry is not None:
            _v, expires_at = entry
            if expires_at is None or time.monotonic() <= expires_at:
                return False
            del self._data[key]
        self._data[key] = (value, time.monotonic() + ttl_seconds)
        return True


class ScopedKeyValueStore(KeyValueStore):
    """A view of a parent ``KeyValueStore`` with a fixed key prefix.

    All keys passed to ``set``/``get`` have the configured prefix prepended
    before delegating to the parent. ``aclose()`` is a no-op — the parent
    owns the connection.

    Scoped views nest: ``store.scope("a:").scope("b:")`` produces a view
    whose effective prefix is ``"a:b:"``.
    """

    def __init__(self, parent: KeyValueStore, prefix: str) -> None:
        self._parent = parent
        self._prefix = prefix

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        """Store a value with the configured prefix prepended."""
        await self._parent.set(self._prefix + key, value, ttl_seconds)

    async def get(self, key: str) -> str | None:
        """Retrieve a value with the configured prefix prepended."""
        return await self._parent.get(self._prefix + key)

    async def set_if_not_exists(self, key: str, value: str, ttl_seconds: int) -> bool:
        """Delegate atomic set-if-absent with the configured prefix."""
        return await self._parent.set_if_not_exists(self._prefix + key, value, ttl_seconds)


# ---------------------------------------------------------------------------
# Domain registry
# ---------------------------------------------------------------------------


_DOMAIN_REGISTRY: dict[str, str] = {}


def register_domain(name: str, prefix: str) -> None:
    """Register a canonical key prefix for a named domain.

    Re-registering the same ``(name, prefix)`` pair is a no-op. Re-registering
    ``name`` with a different prefix raises ``ValueError`` — domain names are
    process-global and must be stable.

    Convention: prefixes end with ``":"`` to make composed keys readable.
    """
    existing = _DOMAIN_REGISTRY.get(name)
    if existing is not None and existing != prefix:
        raise ValueError(
            f"Domain {name!r} already registered with prefix {existing!r}; "
            f"refusing to re-register with {prefix!r}"
        )
    _DOMAIN_REGISTRY[name] = prefix


def domain_prefix(name: str) -> str:
    """Look up the canonical prefix for a registered domain.

    Raises ``KeyError`` if the domain has not been registered.
    """
    try:
        return _DOMAIN_REGISTRY[name]
    except KeyError:
        raise KeyError(f"No registered domain {name!r}; call register_domain() first") from None


def make_key(domain: str, *parts: str) -> str:
    """Build a fully-qualified key for a registered domain.

    ``make_key("checkpoint", goal_id, str(n))`` →
    ``"heddle:checkpoint:" + goal_id + ":" + str(n)``.
    """
    return domain_prefix(domain) + ":".join(parts)


def scoped(store: KeyValueStore, domain: str) -> ScopedKeyValueStore:
    """Convenience: return a ``ScopedKeyValueStore`` for a registered domain."""
    return store.scope(domain_prefix(domain))


# ---------------------------------------------------------------------------
# Default domain registrations shipped with heddle
# ---------------------------------------------------------------------------

# CheckpointManager keys are of the form
# ``heddle:checkpoint:{goal_id}:{checkpoint_number}`` and
# ``heddle:checkpoint:{goal_id}:latest``. Registering the prefix preserves
# bit-exact key layout for existing data in Valkey.
register_domain("checkpoint", "heddle:checkpoint:")
