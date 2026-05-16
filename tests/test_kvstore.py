"""Tests for KeyValueStore, ScopedKeyValueStore, and the domain registry."""

from __future__ import annotations

import pytest

from heddle.core.kvstore import (
    InMemoryKeyValueStore,
    ScopedKeyValueStore,
    domain_prefix,
    make_key,
    register_domain,
    scoped,
)

# ---------------------------------------------------------------------------
# InMemoryKeyValueStore basics (mirror of test_store.py against new name)
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> InMemoryKeyValueStore:
    return InMemoryKeyValueStore()


@pytest.mark.asyncio
async def test_kv_set_get_roundtrip(store: InMemoryKeyValueStore) -> None:
    await store.set("k", "v")
    assert await store.get("k") == "v"


@pytest.mark.asyncio
async def test_kv_missing_returns_none(store: InMemoryKeyValueStore) -> None:
    assert await store.get("nope") is None


@pytest.mark.asyncio
async def test_kv_ttl_expiry(store: InMemoryKeyValueStore, monkeypatch: pytest.MonkeyPatch) -> None:
    t = 1000.0
    monkeypatch.setattr("time.monotonic", lambda: t)
    await store.set("k", "v", ttl_seconds=60)

    monkeypatch.setattr("time.monotonic", lambda: t + 30)
    assert await store.get("k") == "v"

    monkeypatch.setattr("time.monotonic", lambda: t + 61)
    assert await store.get("k") is None


@pytest.mark.asyncio
async def test_kv_aclose_is_noop(store: InMemoryKeyValueStore) -> None:
    await store.aclose()
    await store.aclose()  # idempotent


# ---------------------------------------------------------------------------
# ScopedKeyValueStore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scope_prepends_prefix(store: InMemoryKeyValueStore) -> None:
    view = store.scope("p:")
    await view.set("k", "v")

    # Through the view, the key is "k".
    assert await view.get("k") == "v"
    # Through the parent, the key is "p:k".
    assert await store.get("p:k") == "v"
    # Through the parent, the unprefixed key does not exist.
    assert await store.get("k") is None


@pytest.mark.asyncio
async def test_scope_nests(store: InMemoryKeyValueStore) -> None:
    view = store.scope("a:").scope("b:")
    await view.set("k", "v")
    assert await store.get("a:b:k") == "v"


@pytest.mark.asyncio
async def test_scope_returns_kvstore_type(store: InMemoryKeyValueStore) -> None:
    view = store.scope("p:")
    assert isinstance(view, ScopedKeyValueStore)


# ---------------------------------------------------------------------------
# Domain registry
# ---------------------------------------------------------------------------


def test_register_domain_idempotent_same_value() -> None:
    register_domain("test_idempotent", "ti:")
    # Re-registering with the same prefix is allowed.
    register_domain("test_idempotent", "ti:")
    assert domain_prefix("test_idempotent") == "ti:"


def test_register_domain_conflict_raises() -> None:
    register_domain("test_conflict", "tc:")
    with pytest.raises(ValueError, match="already registered"):
        register_domain("test_conflict", "tc-different:")


def test_domain_prefix_unknown_raises() -> None:
    with pytest.raises(KeyError, match="No registered domain"):
        domain_prefix("definitely_not_registered_anywhere")


def test_make_key_joins_parts() -> None:
    register_domain("test_makekey", "tm:")
    assert make_key("test_makekey", "a", "b", "c") == "tm:a:b:c"


def test_make_key_with_one_part() -> None:
    register_domain("test_makekey_one", "tmo:")
    assert make_key("test_makekey_one", "single") == "tmo:single"


@pytest.mark.asyncio
async def test_scoped_convenience(store: InMemoryKeyValueStore) -> None:
    register_domain("test_scoped", "ts:")
    view = scoped(store, "test_scoped")
    await view.set("k", "v")
    assert await store.get("ts:k") == "v"


# ---------------------------------------------------------------------------
# Heddle-default registrations
# ---------------------------------------------------------------------------


def test_checkpoint_domain_registered_at_import() -> None:
    """`checkpoint` is registered when the module is imported."""
    assert domain_prefix("checkpoint") == "heddle:checkpoint:"
