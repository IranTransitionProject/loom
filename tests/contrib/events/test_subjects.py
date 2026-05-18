"""Tests for heddle.contrib.events.subjects helpers (Sprint 1)."""

import pytest

from heddle.contrib.events.subjects import (
    command_stream_name,
    command_subject,
    command_subject_filter,
    event_stream_name,
    event_subject,
    event_subject_filter,
    rejection_stream_name,
    rejection_subject,
    rejection_subject_filter,
)


def test_event_subject_format():
    assert (
        event_subject("Job", "39174-004", "JobShippedFromPF")
        == "heddle.events.Job.39174-004.JobShippedFromPF"
    )


def test_command_subject_format():
    assert (
        command_subject("Operation", "39174-004:laser-cut", "RecordLabor")
        == "heddle.commands.Operation.39174-004:laser-cut.RecordLabor"
    )


def test_rejection_subject_format():
    assert (
        rejection_subject("Job", "39174-004", "JobClockIn")
        == "heddle.rejections.Job.39174-004.JobClockIn"
    )


def test_subject_rejects_dots_in_token():
    with pytest.raises(ValueError, match="aggregate_type"):
        event_subject("Job.Bad", "39174-004", "X")


def test_subject_rejects_wildcards():
    with pytest.raises(ValueError, match="aggregate_id"):
        event_subject("Job", "*", "X")
    with pytest.raises(ValueError, match="event_type"):
        event_subject("Job", "abc", ">")


def test_subject_rejects_empty_token():
    with pytest.raises(ValueError, match="must not be empty"):
        event_subject("Job", "", "X")


def test_stream_name_upper():
    assert event_stream_name("Operation") == "HEDDLE_EVENTS_OPERATION"
    assert command_stream_name("Job") == "HEDDLE_COMMANDS_JOB"
    assert rejection_stream_name("OperatorJobSession") == ("HEDDLE_REJECTIONS_OPERATORJOBSESSION")


def test_event_subject_filter():
    assert event_subject_filter("Job") == "heddle.events.Job.>"
    assert command_subject_filter("Job") == "heddle.commands.Job.>"
    assert rejection_subject_filter("Job") == "heddle.rejections.Job.>"
