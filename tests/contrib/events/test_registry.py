"""Tests for the AGGREGATE_REGISTRY decorator (Sprint 2 T3)."""

from __future__ import annotations

import pytest

from heddle.contrib.events.aggregate import (
    Aggregate,
    IntervalAggregate,
    RootAggregate,
)
from heddle.contrib.events.registry import (
    AGGREGATE_REGISTRY,
    get_aggregate_class,
    is_root_type,
    register_aggregate,
)

pytestmark = pytest.mark.usefixtures("registry_isolation")


def test_decorator_registers_and_sets_aggregate_type() -> None:
    @register_aggregate("Test1")
    class _T1(Aggregate):
        pass

    assert _T1.aggregate_type == "Test1"
    assert get_aggregate_class("Test1") is _T1
    assert AGGREGATE_REGISTRY["Test1"] is _T1


def test_re_registering_same_class_is_noop() -> None:
    @register_aggregate("Repeat")
    class _R(Aggregate):
        pass

    # Re-apply the decorator to the same class — must not raise.
    _R = register_aggregate("Repeat")(_R)  # noqa: N806
    assert get_aggregate_class("Repeat") is _R


def test_re_registering_different_class_raises() -> None:
    @register_aggregate("Collide")
    class _A(Aggregate):
        pass

    with pytest.raises(ValueError, match="already registered"):

        @register_aggregate("Collide")
        class _B(Aggregate):
            pass


def test_non_aggregate_decoration_raises() -> None:
    with pytest.raises(TypeError, match="must subclass Aggregate"):

        @register_aggregate("Bogus")
        class _Plain:
            pass


def test_get_aggregate_class_unknown_raises() -> None:
    with pytest.raises(KeyError, match="no aggregate registered"):
        get_aggregate_class("Nope")


def test_is_root_type_true_for_root() -> None:
    @register_aggregate("RT")
    class _RT(RootAggregate):
        pass

    assert is_root_type("RT") is True


def test_is_root_type_false_for_interval() -> None:
    @register_aggregate("IT")
    class _IT(IntervalAggregate):
        pass

    assert is_root_type("IT") is False


def test_is_root_type_false_for_plain_aggregate() -> None:
    @register_aggregate("PA")
    class _PA(Aggregate):
        pass

    assert is_root_type("PA") is False


def test_registry_isolation_fixture_resets_between_tests() -> None:
    @register_aggregate("LocalOnly")
    class _Local(Aggregate):
        pass

    assert "LocalOnly" in AGGREGATE_REGISTRY
    # The fixture's teardown will clear this; verified by the
    # second copy below not seeing it.


def test_registry_isolation_fixture_second_test() -> None:
    assert "LocalOnly" not in AGGREGATE_REGISTRY
