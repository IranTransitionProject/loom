"""Tests for heddle.contrib.events.errors (Sprint 2 T1)."""

import pytest

from heddle.contrib.events.errors import (
    AggregateInvariantError,
    BusResultTimeoutError,
    CommandRejected,
    ConcurrencyError,
    CorruptAggregateAlert,
    HeddleEventsError,
    UnknownEventVersionError,
)

_LEAVES = (
    UnknownEventVersionError,
    AggregateInvariantError,
    CommandRejected,
    ConcurrencyError,
    BusResultTimeoutError,
    CorruptAggregateAlert,
)


@pytest.mark.parametrize("cls", _LEAVES)
def test_inherits_from_base(cls: type[HeddleEventsError]) -> None:
    assert issubclass(cls, HeddleEventsError)
    assert issubclass(cls, Exception)


def _raise(cls: type[HeddleEventsError]) -> None:
    # CommandRejected requires reason/detail; build accordingly.
    if cls is CommandRejected:
        raise cls("X", "y")
    raise cls("boom")


@pytest.mark.parametrize("cls", _LEAVES)
def test_catchable_as_family(cls: type[HeddleEventsError]) -> None:
    with pytest.raises(HeddleEventsError):
        _raise(cls)


def test_command_rejected_with_reason_and_detail() -> None:
    err = CommandRejected("INVALID", "phase=finalized")
    assert err.reason == "INVALID"
    assert err.detail == "phase=finalized"
    assert str(err) == "INVALID: phase=finalized"


def test_command_rejected_with_reason_only() -> None:
    err = CommandRejected("INVALID")
    assert err.reason == "INVALID"
    assert err.detail == ""
    assert str(err) == "INVALID"
