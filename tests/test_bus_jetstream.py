"""Tests for the generic JetStream primitives in ``heddle.bus.jetstream``.

Two tiers:

- **Unit** (always run): fake ``JetStreamContext`` stand-ins assert that
  :func:`ensure_stream` builds the right ``StreamConfig`` and that
  :func:`publish` sets the dedup/CAS headers and translates the
  wrong-last-sequence ``APIError`` into :class:`WrongLastSequenceError`.
- **Integration** (``@pytest.mark.integration``, skipped unless ``NATS_URL``
  is set): exercise idempotent stream creation, ``Nats-Msg-Id`` dedup, CAS
  rejection, and the pull-consumer drain against a live JetStream server::

      NATS_URL=nats://localhost:4222 uv run pytest -q -m integration
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType, StreamConfig
from nats.js.errors import APIError

from heddle.bus import jetstream

# --------------------------------------------------------------------------
# Unit tier — fake JetStreamContext stand-ins, no live NATS.
# --------------------------------------------------------------------------


class _CapturingJS:
    """Records ``add_stream`` configs and ``publish`` calls."""

    def __init__(self) -> None:
        self.configs: list[StreamConfig] = []
        self.published: list[tuple[str, bytes, dict[str, str] | None]] = []

    async def add_stream(self, *, config: StreamConfig) -> None:
        self.configs.append(config)

    async def publish(
        self, subject: str, payload: bytes, headers: dict[str, str] | None = None
    ) -> Any:
        self.published.append((subject, payload, headers))
        return type("_Ack", (), {"duplicate": False, "seq": len(self.published)})()


class _RaisingJS:
    """Raises a preset exception from ``publish``."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def publish(
        self, subject: str, payload: bytes, headers: dict[str, str] | None = None
    ) -> Any:
        raise self._exc


@pytest.mark.asyncio
async def test_ensure_stream_builds_expected_config() -> None:
    js = _CapturingJS()
    await jetstream.ensure_stream(
        js,  # type: ignore[arg-type]
        name="HEDDLE_TEST",
        subjects=["heddle.test.>"],
        max_age_seconds=0,
        discard=DiscardPolicy.NEW,
        duplicate_window_seconds=600,
    )

    cfg = js.configs[0]
    assert cfg.name == "HEDDLE_TEST"
    assert cfg.subjects == ["heddle.test.>"]
    assert cfg.retention == RetentionPolicy.LIMITS
    # Durations are in SECONDS — nats-py StreamConfig.as_dict converts to ns.
    assert cfg.max_age == 0, "0 seconds → unbounded retention"
    assert cfg.max_msgs is None, "unbounded message count (nats-py None sentinel)"
    assert cfg.storage == StorageType.FILE
    assert cfg.discard == DiscardPolicy.NEW
    assert cfg.duplicate_window == 600, "seconds, not pre-multiplied nanoseconds"


@pytest.mark.asyncio
async def test_publish_sets_dedup_and_cas_headers() -> None:
    js = _CapturingJS()
    await jetstream.publish(
        js,  # type: ignore[arg-type]
        "heddle.test.x",
        b"{}",
        msg_id="abc-123",
        expected_last_subject_sequence=4,
    )

    _subject, _payload, headers = js.published[0]
    assert headers is not None
    assert headers["Nats-Msg-Id"] == "abc-123"
    assert headers["Nats-Expected-Last-Subject-Sequence"] == "4"


@pytest.mark.asyncio
async def test_publish_without_options_sends_no_headers() -> None:
    js = _CapturingJS()
    await jetstream.publish(js, "heddle.test.x", b"{}")  # type: ignore[arg-type]

    _subject, _payload, headers = js.published[0]
    assert headers is None, "no msg_id / CAS / extra headers → omit headers entirely"


@pytest.mark.asyncio
async def test_publish_translates_wrong_last_sequence_error() -> None:
    exc = APIError(
        err_code=jetstream._WRONG_LAST_SEQ_ERROR_CODE,
        description="wrong last sequence: 5",
    )
    js = _RaisingJS(exc)

    with pytest.raises(jetstream.WrongLastSequenceError) as caught:
        await jetstream.publish(
            js,  # type: ignore[arg-type]
            "heddle.test.x",
            b"{}",
            expected_last_subject_sequence=0,
        )

    assert caught.value.subject == "heddle.test.x"
    assert caught.value.expected_last_subject_sequence == 0
    assert isinstance(caught.value, jetstream.JetStreamError)


@pytest.mark.asyncio
async def test_publish_reraises_other_api_errors() -> None:
    exc = APIError(err_code=10059, description="stream not found")
    js = _RaisingJS(exc)

    with pytest.raises(APIError):
        await jetstream.publish(js, "heddle.test.x", b"{}")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Integration tier — live JetStream required.
# --------------------------------------------------------------------------

NATS_URL = os.environ.get("NATS_URL")


@pytest.fixture
async def js() -> Any:
    import nats

    nc = await nats.connect(NATS_URL or "nats://localhost:4222")
    try:
        yield nc.jetstream()
    finally:
        await nc.drain()


@pytest.mark.integration
@pytest.mark.skipif(NATS_URL is None, reason="NATS_URL not set")
async def test_ensure_stream_is_idempotent(js: Any) -> None:
    name = f"BUS_JS_IDEM_{uuid.uuid4().hex[:8].upper()}"
    subject = f"bustest.idem.{name.lower()}.>"
    try:
        await jetstream.ensure_stream(js, name=name, subjects=[subject])
        # Second call with identical config must not raise.
        await jetstream.ensure_stream(js, name=name, subjects=[subject])
        info = await js.stream_info(name)
        assert info.config.name == name
    finally:
        await js.delete_stream(name)


@pytest.mark.integration
@pytest.mark.skipif(NATS_URL is None, reason="NATS_URL not set")
async def test_publish_dedup_drops_duplicate_msg_id(js: Any) -> None:
    name = f"BUS_JS_DEDUP_{uuid.uuid4().hex[:8].upper()}"
    subject = f"bustest.dedup.{name.lower()}"
    try:
        await jetstream.ensure_stream(
            js, name=name, subjects=[subject], duplicate_window_seconds=600
        )
        first = await jetstream.publish(js, subject, b'{"n":1}', msg_id="dup-1")
        second = await jetstream.publish(js, subject, b'{"n":1}', msg_id="dup-1")

        assert not first.duplicate, "fresh publish is not a duplicate (nats-py: None)"
        assert second.duplicate is True, "repeat Nats-Msg-Id within window is a dedup"
        info = await js.stream_info(name)
        assert info.state.messages == 1, "duplicate stored once"
    finally:
        await js.delete_stream(name)


@pytest.mark.integration
@pytest.mark.skipif(NATS_URL is None, reason="NATS_URL not set")
async def test_cas_rejects_stale_expected_sequence(js: Any) -> None:
    name = f"BUS_JS_CAS_{uuid.uuid4().hex[:8].upper()}"
    subject = f"bustest.cas.{name.lower()}"
    try:
        await jetstream.ensure_stream(js, name=name, subjects=[subject])
        # Empty subject → expected last sequence 0 succeeds.
        await jetstream.publish(js, subject, b'{"n":1}', expected_last_subject_sequence=0)
        # Subject's last sequence is now 1; expecting 0 again is stale.
        with pytest.raises(jetstream.WrongLastSequenceError):
            await jetstream.publish(js, subject, b'{"n":2}', expected_last_subject_sequence=0)
    finally:
        await js.delete_stream(name)


@pytest.mark.integration
@pytest.mark.skipif(NATS_URL is None, reason="NATS_URL not set")
async def test_pull_drains_in_order(js: Any) -> None:
    name = f"BUS_JS_PULL_{uuid.uuid4().hex[:8].upper()}"
    subject = f"bustest.pull.{name.lower()}"
    try:
        await jetstream.ensure_stream(js, name=name, subjects=[subject])
        for n in range(3):
            await jetstream.publish(js, subject, str(n).encode())

        received: list[bytes] = []
        async for msg in jetstream.pull(js, subject=subject, durable="bus-pull-test"):
            received.append(msg.data)
            await msg.ack()

        assert received == [b"0", b"1", b"2"], "stream-order drain"
    finally:
        await js.delete_stream(name)
