"""Tests for heddle.contrib.events.issuer_conventions (Sprint 1).

The two multi-segment-suffix cases below are explicitly required by
sprint-1 §6 and the cross-language wire contract — they must remain
passing in heddle, heddle-sdk Swift, and heddle-sdk .NET.
"""

import pytest

from heddle.contrib.events.issuer_conventions import (
    is_bridge_issuer,
    is_framework_issuer,
    is_observer_issuer,
    is_projector_issuer,
    is_recognized_issuer,
    is_system_issuer,
    is_user_issuer,
)

# -- Required multi-segment-suffix cases (sprint-1 §6) ----------------------


def test_is_user_issuer_accepts_emergency_correction_segments():
    assert is_user_issuer("user:system:emergency_correction:eng-42")


def test_is_framework_issuer_accepts_cascade_retry_segments():
    assert is_framework_issuer("framework:cascade:retry:3")


# -- Positive coverage for every prefix ------------------------------------


@pytest.mark.parametrize(
    ("fn", "value"),
    [
        (is_framework_issuer, "framework:cascade"),
        (is_observer_issuer, "observer:pf_job_status"),
        (is_projector_issuer, "projector:operation_labor_projector"),
        (is_user_issuer, "user:badge:206"),
        (is_user_issuer, "user:system:shoppulse_admin"),
        (is_system_issuer, "user:system:shoppulse_admin"),
        (is_bridge_issuer, "bridge:fault_classifier_llm"),
    ],
)
def test_validator_accepts_canonical_form(fn, value):
    assert fn(value)


# -- Bare-prefix and empty-string negatives --------------------------------


@pytest.mark.parametrize(
    ("fn", "bare"),
    [
        (is_framework_issuer, "framework:"),
        (is_observer_issuer, "observer:"),
        (is_projector_issuer, "projector:"),
        (is_user_issuer, "user:"),
        (is_system_issuer, "user:system:"),
        (is_bridge_issuer, "bridge:"),
    ],
)
def test_validator_rejects_bare_prefix(fn, bare):
    assert not fn(bare)


@pytest.mark.parametrize(
    "fn",
    [
        is_framework_issuer,
        is_observer_issuer,
        is_projector_issuer,
        is_user_issuer,
        is_system_issuer,
        is_bridge_issuer,
    ],
)
def test_validator_rejects_empty_string(fn):
    assert not fn("")


# -- Cross-prefix negatives ------------------------------------------------


def test_each_validator_rejects_other_prefixes():
    assert not is_framework_issuer("observer:x")
    assert not is_observer_issuer("framework:x")
    assert not is_projector_issuer("user:badge:1")
    assert not is_user_issuer("framework:x")
    assert not is_bridge_issuer("user:system:x")


# -- is_system_issuer is a strict subcheck of is_user_issuer ---------------


def test_is_system_issuer_rejects_user_badge():
    assert not is_system_issuer("user:badge:206")
    # And accepts both system forms (single-segment and multi-segment).
    assert is_system_issuer("user:system:shoppulse_admin")
    assert is_system_issuer("user:system:emergency_correction:eng-42")


def test_is_user_issuer_accepts_both_subkinds():
    assert is_user_issuer("user:badge:206")
    assert is_user_issuer("user:system:shoppulse_admin")


# -- is_recognized_issuer --------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "framework:cascade",
        "observer:pf_job_status",
        "projector:operation_labor_projector",
        "user:badge:206",
        "user:system:shoppulse_admin",
        "bridge:fault_classifier_llm",
    ],
)
def test_is_recognized_issuer_accepts_all_reserved_prefixes(value):
    assert is_recognized_issuer(value)


@pytest.mark.parametrize("value", ["arbitrary:value", "", "user", "framework", "x:"])
def test_is_recognized_issuer_rejects_other_strings(value):
    assert not is_recognized_issuer(value)
