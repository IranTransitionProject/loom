"""Tests for the finalization lease helper (Sprint 3 T5)."""

from __future__ import annotations

import asyncio

import pytest

from heddle.contrib.events.lease import (
    FINALIZATION_LEASE_TTL_SECONDS,
    finalization_lease,
    lease_key,
)
from heddle.core.kvstore import InMemoryKeyValueStore


def test_lease_key_format() -> None:
    assert lease_key("Foo", "bar") == "heddle:events:horizon:Foo:bar"


@pytest.mark.asyncio
async def test_first_claim_wins_second_loses() -> None:
    kv = InMemoryKeyValueStore()
    async with finalization_lease(kv, "T", "a", "framework:cascade") as first:
        assert first is True
    async with finalization_lease(kv, "T", "a", "framework:horizon") as second:
        assert second is False


@pytest.mark.asyncio
async def test_lease_value_records_projector_name() -> None:
    kv = InMemoryKeyValueStore()
    async with finalization_lease(kv, "T", "a", "framework:horizon"):
        held = await kv.get(lease_key("T", "a"))
        assert held is not None
        assert held.startswith("framework:horizon:")


@pytest.mark.asyncio
async def test_concurrent_claims_exactly_one_wins() -> None:
    kv = InMemoryKeyValueStore()

    async def claim(name: str) -> bool:
        async with finalization_lease(kv, "T", "a", name) as ok:
            return ok

    results = await asyncio.gather(claim("framework:cascade"), claim("framework:horizon"))
    assert sorted(results) == [False, True]


@pytest.mark.asyncio
async def test_lease_expires_and_can_be_reclaimed() -> None:
    kv = InMemoryKeyValueStore()
    async with finalization_lease(kv, "T", "a", "framework:cascade", ttl_seconds=1):
        pass
    # Sleep past TTL.
    await asyncio.sleep(1.1)
    async with finalization_lease(kv, "T", "a", "framework:horizon") as ok:
        assert ok is True


@pytest.mark.asyncio
async def test_default_ttl_is_thirty_seconds() -> None:
    assert FINALIZATION_LEASE_TTL_SECONDS == 30
