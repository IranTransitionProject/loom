"""
Test NATS adapter (unit tests, no infrastructure).

Tests the NATSBus and NATSSubscription classes from heddle.bus.nats_adapter.
All NATS interactions are mocked — no running NATS server is needed.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from heddle.bus.nats_adapter import NATSBus, NATSSubscription

# ---------------------------------------------------------------------------
# NATSBus — connection lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_calls_nats_connect_with_defaults():
    """connect() passes URL, reconnect settings, and callbacks to nats.connect."""
    bus = NATSBus(url="nats://localhost:4222")
    mock_nc = AsyncMock()

    with patch(
        "heddle.bus.nats_adapter.nats.connect", new_callable=AsyncMock, return_value=mock_nc
    ) as mock_connect:
        await bus.connect()

    mock_connect.assert_awaited_once()
    call_kwargs = mock_connect.call_args
    assert call_kwargs[0][0] == "nats://localhost:4222"
    assert call_kwargs[1]["reconnect_time_wait"] == 1
    assert call_kwargs[1]["max_reconnect_attempts"] == 60
    assert callable(call_kwargs[1]["reconnected_cb"])
    assert callable(call_kwargs[1]["disconnected_cb"])
    assert bus._nc is mock_nc


@pytest.mark.asyncio
async def test_close_drains_connection():
    """close() calls nc.drain() on the underlying NATS client."""
    bus = NATSBus()
    bus._nc = AsyncMock()

    await bus.close()

    bus._nc.drain.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_without_connection_is_noop():
    """close() does nothing if connect() was never called."""
    bus = NATSBus()
    # _nc is None by default — should not raise
    await bus.close()


# ---------------------------------------------------------------------------
# NATSBus — publish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_serializes_dict_to_json_bytes():
    """publish() JSON-encodes the dict and sends bytes to the subject."""
    bus = NATSBus()
    bus._nc = AsyncMock()
    data = {"task_id": "abc", "payload": 42}

    await bus.publish("heddle.tasks.incoming", data)

    expected_bytes = json.dumps(data).encode()
    bus._nc.publish.assert_awaited_once_with("heddle.tasks.incoming", expected_bytes)


# ---------------------------------------------------------------------------
# NATSBus — subscribe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_without_queue_group():
    """subscribe() without queue_group calls nc.subscribe(subject) only."""
    bus = NATSBus()
    mock_nats_sub = AsyncMock()
    bus._nc = AsyncMock()
    bus._nc.subscribe = AsyncMock(return_value=mock_nats_sub)

    sub = await bus.subscribe("heddle.tasks.incoming")

    bus._nc.subscribe.assert_awaited_once_with("heddle.tasks.incoming")
    assert isinstance(sub, NATSSubscription)


@pytest.mark.asyncio
async def test_subscribe_with_queue_group():
    """subscribe() with queue_group passes queue= kwarg to nc.subscribe."""
    bus = NATSBus()
    mock_nats_sub = AsyncMock()
    bus._nc = AsyncMock()
    bus._nc.subscribe = AsyncMock(return_value=mock_nats_sub)

    sub = await bus.subscribe("heddle.tasks.summarizer.local", queue_group="workers-summarizer")

    bus._nc.subscribe.assert_awaited_once_with(
        "heddle.tasks.summarizer.local",
        queue="workers-summarizer",
    )
    assert isinstance(sub, NATSSubscription)


# ---------------------------------------------------------------------------
# NATSBus — request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_serializes_and_deserializes():
    """request() sends JSON bytes and decodes the response."""
    bus = NATSBus()
    bus._nc = AsyncMock()

    response_data = {"status": "ok", "count": 3}
    mock_resp = MagicMock()
    mock_resp.data = json.dumps(response_data).encode()
    bus._nc.request = AsyncMock(return_value=mock_resp)

    result = await bus.request("heddle.health", {"check": True}, timeout=5.0)

    expected_bytes = json.dumps({"check": True}).encode()
    bus._nc.request.assert_awaited_once_with("heddle.health", expected_bytes, timeout=5.0)
    assert result == response_data


# ---------------------------------------------------------------------------
# NATSSubscription — iteration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscription_anext_decodes_json():
    """__anext__() decodes msg.data as JSON and returns a dict."""
    payload = {"task_id": "t1", "worker_type": "summarizer"}
    mock_msg = MagicMock()
    mock_msg.data = json.dumps(payload).encode()

    mock_nats_sub = AsyncMock()
    mock_nats_sub.next_msg = AsyncMock(return_value=mock_msg)

    sub = NATSSubscription(mock_nats_sub)
    result = await sub.__anext__()

    mock_nats_sub.next_msg.assert_awaited_once_with(timeout=None)
    assert result == payload


@pytest.mark.asyncio
async def test_subscription_anext_raises_stop_on_terminal_error():
    """A *terminal* error (connection closed, subscription invalid) ends iteration.

    Earlier the iterator caught any ``Exception`` and ended iteration —
    a single transient hiccup silently killed the actor's only inbound
    channel forever.  We now distinguish terminal from transient: only
    closed-connection / invalid-subscription errors raise
    ``StopAsyncIteration``; other errors retry (covered separately).
    """
    import nats.errors

    mock_nats_sub = AsyncMock()
    mock_nats_sub.next_msg = AsyncMock(
        side_effect=nats.errors.ConnectionClosedError()
    )

    sub = NATSSubscription(mock_nats_sub)

    with pytest.raises(StopAsyncIteration):
        await sub.__anext__()


# ---------------------------------------------------------------------------
# NATSSubscription — unsubscribe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscription_unsubscribe_delegates():
    """unsubscribe() delegates to the underlying NATS subscription."""
    mock_nats_sub = AsyncMock()
    sub = NATSSubscription(mock_nats_sub)

    await sub.unsubscribe()

    mock_nats_sub.unsubscribe.assert_awaited_once()


# ---------------------------------------------------------------------------
# NATSSubscription — malformed message handling (P1.3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscription_skips_malformed_json():
    """Malformed JSON messages are skipped; next valid message is returned."""
    bad_msg = MagicMock()
    bad_msg.data = b"not valid json {{"
    bad_msg.subject = "heddle.tasks.incoming"

    good_payload = {"task_id": "t2", "worker_type": "summarizer"}
    good_msg = MagicMock()
    good_msg.data = json.dumps(good_payload).encode()

    mock_nats_sub = AsyncMock()
    mock_nats_sub.next_msg = AsyncMock(side_effect=[bad_msg, good_msg])

    sub = NATSSubscription(mock_nats_sub)
    result = await sub.__anext__()

    # Should have skipped the bad message and returned the good one.
    assert result == good_payload
    assert mock_nats_sub.next_msg.await_count == 2


@pytest.mark.asyncio
async def test_subscription_skips_unicode_decode_error():
    """Messages with invalid encoding are skipped; next valid message is returned."""
    bad_msg = MagicMock()
    # Invalid UTF-8 bytes
    bad_msg.data = b"\x80\x81\x82"
    bad_msg.subject = "heddle.tasks.incoming"

    good_payload = {"ok": True}
    good_msg = MagicMock()
    good_msg.data = json.dumps(good_payload).encode()

    mock_nats_sub = AsyncMock()
    mock_nats_sub.next_msg = AsyncMock(side_effect=[bad_msg, good_msg])

    sub = NATSSubscription(mock_nats_sub)
    result = await sub.__anext__()

    assert result == good_payload
    assert mock_nats_sub.next_msg.await_count == 2


@pytest.mark.asyncio
async def test_subscription_multiple_malformed_before_valid():
    """Multiple consecutive malformed messages are all skipped."""
    bad1 = MagicMock()
    bad1.data = b"bad1"
    bad1.subject = "test"

    bad2 = MagicMock()
    bad2.data = b"bad2"
    bad2.subject = "test"

    good_payload = {"result": "ok"}
    good = MagicMock()
    good.data = json.dumps(good_payload).encode()

    mock_nats_sub = AsyncMock()
    mock_nats_sub.next_msg = AsyncMock(side_effect=[bad1, bad2, good])

    sub = NATSSubscription(mock_nats_sub)
    result = await sub.__anext__()

    assert result == good_payload
    assert mock_nats_sub.next_msg.await_count == 3


@pytest.mark.asyncio
async def test_subscription_terminal_error_after_malformed_still_stops():
    """A terminal NATS error after skipping malformed messages still ends iteration."""
    import nats.errors

    bad_msg = MagicMock()
    bad_msg.data = b"not json"
    bad_msg.subject = "test"

    mock_nats_sub = AsyncMock()
    mock_nats_sub.next_msg = AsyncMock(
        side_effect=[bad_msg, nats.errors.ConnectionClosedError()],
    )

    sub = NATSSubscription(mock_nats_sub)

    with pytest.raises(StopAsyncIteration):
        await sub.__anext__()


# ---------------------------------------------------------------------------
# NATSSubscription — transient-error resilience
#
# The previous shape collapsed every nats-py exception into
# StopAsyncIteration, which made any transient hiccup silently
# terminate the actor's only inbound channel.  These tests pin the
# new behaviour: terminal errors stop iteration; everything else
# retries.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscription_recovers_from_transient_error():
    """A transient error (e.g. StaleConnectionError) is retried, not terminal.

    Regression test: pre-fix, the very first transient nats-py error
    raised ``StopAsyncIteration`` and the actor lost its inbound
    channel forever.  The fix retries after a short sleep so a single
    slow-consumer signal or stale connection does not kill the actor.
    """
    import nats.errors

    payload = {"recovered": True}
    good_msg = MagicMock()
    good_msg.data = json.dumps(payload).encode()

    mock_nats_sub = AsyncMock()
    mock_nats_sub.next_msg = AsyncMock(
        side_effect=[
            nats.errors.StaleConnectionError(),
            good_msg,
        ],
    )

    sub = NATSSubscription(mock_nats_sub)
    # Patch the retry sleep down to 0 so the test does not actually
    # block for 100ms.  Production code uses a small backoff to avoid
    # busy-looping if the error persists.
    with patch("heddle.bus.nats_adapter._TRANSIENT_RETRY_SLEEP", 0.0):
        result = await sub.__anext__()

    assert result == payload
    assert mock_nats_sub.next_msg.await_count == 2


@pytest.mark.asyncio
async def test_subscription_recovers_from_multiple_transient_errors():
    """Several consecutive transient errors are all retried."""
    import nats.errors

    payload = {"got_through": True}
    good_msg = MagicMock()
    good_msg.data = json.dumps(payload).encode()

    mock_nats_sub = AsyncMock()
    mock_nats_sub.next_msg = AsyncMock(
        side_effect=[
            nats.errors.StaleConnectionError(),
            nats.errors.OutboundBufferLimitError(),
            ValueError("some unexpected library error"),
            good_msg,
        ],
    )

    sub = NATSSubscription(mock_nats_sub)
    with patch("heddle.bus.nats_adapter._TRANSIENT_RETRY_SLEEP", 0.0):
        result = await sub.__anext__()

    assert result == payload
    assert mock_nats_sub.next_msg.await_count == 4


@pytest.mark.asyncio
async def test_subscription_propagates_cancellation():
    """``asyncio.CancelledError`` must propagate, not be treated as transient.

    Without explicit handling, the broad ``except Exception`` catch
    would swallow shutdown signals (CancelledError is BaseException in
    Python 3.8+, so technically not caught — but we want a regression
    test that pins the behaviour explicitly).
    """
    import asyncio

    mock_nats_sub = AsyncMock()
    mock_nats_sub.next_msg = AsyncMock(side_effect=asyncio.CancelledError())

    sub = NATSSubscription(mock_nats_sub)

    with pytest.raises(asyncio.CancelledError):
        await sub.__anext__()


@pytest.mark.asyncio
async def test_subscription_terminal_after_transient_still_stops():
    """A terminal error after transient retries still ends iteration cleanly."""
    import nats.errors

    mock_nats_sub = AsyncMock()
    mock_nats_sub.next_msg = AsyncMock(
        side_effect=[
            nats.errors.StaleConnectionError(),
            nats.errors.ConnectionClosedError(),
        ],
    )

    sub = NATSSubscription(mock_nats_sub)
    with (
        patch("heddle.bus.nats_adapter._TRANSIENT_RETRY_SLEEP", 0.0),
        pytest.raises(StopAsyncIteration),
    ):
        await sub.__anext__()
