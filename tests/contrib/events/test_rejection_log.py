"""Tests for RejectionLog ABC + InMemoryRejectionLog (Sprint 2 T5)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from heddle.contrib.events.envelopes import CommandMessage, CommandMetadata
from heddle.contrib.events.rejection_log import (
    InMemoryRejectionLog,
    RejectionEnvelope,
)


def _cmd(
    *,
    aggregate_type: str = "Job",
    aggregate_id: str = "39174-004",
    command_type: str = "ClockIn",
) -> CommandMessage:
    return CommandMessage(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        command_type=command_type,
        payload={"badge": "206"},
        metadata=CommandMetadata(issued_by="user:badge:206"),
        issued_at=datetime.now(timezone.utc),
    )


def _rej(
    cmd: CommandMessage, *, reason: str = "INVALID", detail: str = ""
) -> RejectionEnvelope:
    return RejectionEnvelope(
        command=cmd,
        reason=reason,
        detail=detail,
        rejected_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_append_then_load_round_trip() -> None:
    log = InMemoryRejectionLog()
    cmd = _cmd()
    await log.append(_rej(cmd, reason="INVALID", detail="phase=finalized"))

    loaded = [r async for r in log.load("Job")]
    assert len(loaded) == 1
    assert loaded[0].reason == "INVALID"
    assert loaded[0].detail == "phase=finalized"
    assert loaded[0].command.command_id == cmd.command_id


@pytest.mark.asyncio
async def test_filter_by_aggregate_type() -> None:
    log = InMemoryRejectionLog()
    await log.append(_rej(_cmd(aggregate_type="Job")))
    await log.append(_rej(_cmd(aggregate_type="Operation")))

    jobs = [r async for r in log.load("Job")]
    ops = [r async for r in log.load("Operation")]
    assert len(jobs) == 1
    assert len(ops) == 1
    assert jobs[0].command.aggregate_type == "Job"
    assert ops[0].command.aggregate_type == "Operation"


@pytest.mark.asyncio
async def test_filter_by_aggregate_id_when_supplied() -> None:
    log = InMemoryRejectionLog()
    await log.append(_rej(_cmd(aggregate_id="a-1")))
    await log.append(_rej(_cmd(aggregate_id="a-2")))

    a1 = [r async for r in log.load("Job", "a-1")]
    a2 = [r async for r in log.load("Job", "a-2")]
    assert [r.command.aggregate_id for r in a1] == ["a-1"]
    assert [r.command.aggregate_id for r in a2] == ["a-2"]


@pytest.mark.asyncio
async def test_aggregate_id_none_returns_all_of_type() -> None:
    log = InMemoryRejectionLog()
    await log.append(_rej(_cmd(aggregate_id="a-1")))
    await log.append(_rej(_cmd(aggregate_id="a-2")))

    rows = [r async for r in log.load("Job", None)]
    assert {r.command.aggregate_id for r in rows} == {"a-1", "a-2"}


def test_envelope_serialises_to_json() -> None:
    env = _rej(_cmd(), reason="X", detail="y")
    blob = env.model_dump_json()
    parsed = json.loads(blob)
    assert parsed["reason"] == "X"
    assert parsed["detail"] == "y"
    assert parsed["command"]["aggregate_type"] == "Job"

    # Round-trip via Pydantic preserves equality at the model level.
    restored = RejectionEnvelope.model_validate_json(blob)
    assert restored.reason == env.reason
    assert restored.command.command_id == env.command.command_id


def test_schema_file_exists() -> None:
    """Schema drift gate covers rejection_envelope.schema.json."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[3]
    schema = repo / "schemas" / "v1" / "rejection_envelope.schema.json"
    assert schema.exists(), f"expected schema file at {schema}"
    data = json.loads(schema.read_text())
    # Sanity: top-level fields land where we expect.
    assert data["title"] == "RejectionEnvelope"
    assert "reason" in data["properties"]
    assert "command" in data["properties"]
