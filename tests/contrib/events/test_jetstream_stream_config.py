"""Unit tests for JetStream stream-config helpers.

The event stream is the source of truth and MUST be configured as such
(unbounded age, discard-new, 10m dedup window) per v7 §4.2; command and
rejection streams keep age-based retention with discard-old.

Pure unit tests — a fake JetStreamContext captures the StreamConfig
passed to ``add_stream``; no live NATS required (the integration tests
in test_jetstream_event_log.py cover behavior against a real server).
"""

from __future__ import annotations

import pytest
from nats.js.api import DiscardPolicy, StreamConfig

from heddle.contrib.events.jetstream import stream_config

_NS = 1_000_000_000


class _CapturingJS:
    """Minimal JetStreamContext stand-in: records each add_stream config."""

    def __init__(self) -> None:
        self.configs: list[StreamConfig] = []

    async def add_stream(self, *, config: StreamConfig) -> None:
        self.configs.append(config)


@pytest.mark.asyncio
async def test_event_stream_is_source_of_truth() -> None:
    js = _CapturingJS()
    await stream_config.ensure_event_stream(js, "OperatorJobSession")  # type: ignore[arg-type]

    cfg = js.configs[0]
    assert cfg.max_age == 0, "event log must never age out — it is the source of truth"
    assert cfg.max_msgs is None, "unbounded message count (nats-py None sentinel)"
    assert cfg.discard == DiscardPolicy.NEW, "reject new writes when full, never drop old events"
    assert cfg.duplicate_window == 10 * 60 * _NS, "10m opportunistic dedup window"


@pytest.mark.asyncio
async def test_command_stream_keeps_age_retention_discard_old() -> None:
    js = _CapturingJS()
    await stream_config.ensure_command_stream(js, "OperatorJobSession")  # type: ignore[arg-type]

    cfg = js.configs[0]
    assert cfg.discard == DiscardPolicy.OLD
    assert cfg.max_age == 7 * 24 * 3600 * _NS


@pytest.mark.asyncio
async def test_rejection_stream_keeps_age_retention_discard_old() -> None:
    js = _CapturingJS()
    await stream_config.ensure_rejection_stream(js, "OperatorJobSession")  # type: ignore[arg-type]

    cfg = js.configs[0]
    assert cfg.discard == DiscardPolicy.OLD
    assert cfg.max_age == 30 * 24 * 3600 * _NS
