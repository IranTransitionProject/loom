"""
Test Redis checkpoint store (unit tests, no infrastructure).

Tests the RedisCheckpointStore from heddle.contrib.redis.store.
All Redis interactions are mocked — no running Redis server is needed.
"""

from unittest.mock import AsyncMock, patch

import pytest

from heddle.contrib.redis.store import RedisCheckpointStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_redis():
    """Create a mock redis client with AsyncMock methods."""
    client = AsyncMock()
    client.set = AsyncMock()
    client.get = AsyncMock()
    return client


@pytest.fixture
def store(mock_redis):
    """Create a RedisCheckpointStore with a mocked redis client."""
    with patch("heddle.contrib.redis.store.redis.from_url", return_value=mock_redis):
        s = RedisCheckpointStore("redis://localhost:6379")
    return s


# ---------------------------------------------------------------------------
# set()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_without_ttl(store, mock_redis):
    """set() without TTL calls redis.set(key, value) without expiry."""
    await store.set("goal:abc", "checkpoint-data")

    mock_redis.set.assert_awaited_once_with("goal:abc", "checkpoint-data")


@pytest.mark.asyncio
async def test_set_with_ttl(store, mock_redis):
    """set() with TTL calls redis.set(key, value, ex=ttl_seconds)."""
    await store.set("goal:abc", "checkpoint-data", ttl_seconds=3600)

    mock_redis.set.assert_awaited_once_with("goal:abc", "checkpoint-data", ex=3600)


# ---------------------------------------------------------------------------
# get()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_decoded_bytes(store, mock_redis):
    """get() decodes bytes from redis into a string."""
    mock_redis.get.return_value = b"checkpoint-data"

    result = await store.get("goal:abc")

    mock_redis.get.assert_awaited_once_with("goal:abc")
    assert result == "checkpoint-data"


@pytest.mark.asyncio
async def test_get_returns_string_directly(store, mock_redis):
    """get() returns a string as-is when redis returns a string (decode_responses=True)."""
    mock_redis.get.return_value = "checkpoint-data"

    result = await store.get("goal:abc")

    assert result == "checkpoint-data"


@pytest.mark.asyncio
async def test_get_returns_none_when_key_missing(store, mock_redis):
    """get() returns None when the key does not exist in redis."""
    mock_redis.get.return_value = None

    result = await store.get("goal:nonexistent")

    assert result is None


# ---------------------------------------------------------------------------
# set_if_not_exists()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_if_not_exists_returns_true_when_acquired(store, mock_redis):
    """set_if_not_exists() returns True when SET NX EX succeeds."""
    mock_redis.set.return_value = True

    result = await store.set_if_not_exists("lease:abc", "owner", ttl_seconds=30)

    mock_redis.set.assert_awaited_once_with("lease:abc", "owner", ex=30, nx=True)
    assert result is True


@pytest.mark.asyncio
async def test_set_if_not_exists_returns_false_when_key_exists(store, mock_redis):
    """set_if_not_exists() returns False when the key is already set."""
    mock_redis.set.return_value = None

    result = await store.set_if_not_exists("lease:abc", "owner", ttl_seconds=30)

    assert result is False


# ---------------------------------------------------------------------------
# aclose()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_closes_underlying_client(store, mock_redis):
    """aclose() awaits the underlying redis client's aclose()."""
    mock_redis.aclose = AsyncMock()

    await store.aclose()

    mock_redis.aclose.assert_awaited_once()
