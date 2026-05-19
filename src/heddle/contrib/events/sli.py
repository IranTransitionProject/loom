"""SLI instrumentation hooks for ``heddle.contrib.events`` (Sprint 3 T12).

Three signals the framework emits (numbers refer to the v7 §6 Sprint 3
deliverables):

1. ``command_handle_duration`` — histogram of
   :meth:`CommandHandler.handle` end-to-end latency. Labels:
   ``aggregate_type``, ``command_type``, ``outcome``.

2. ``dispatcher_fan_out_duration`` — histogram of
   :class:`EventDispatcher` per-event projector-loop latency.
   Labels: ``aggregate_type``.

3. ``lease_acquisition_duration`` — histogram of
   :func:`finalization_lease` claim latency. Labels: ``aggregate_type``,
   ``projector_name``, ``outcome``.

The module ships a no-op :class:`Recorder` by default. Applications
install an OpenTelemetry-backed (or any other) recorder once at
startup via :func:`install_recorder`. The shape is
OpenTelemetry-compatible: histograms with the listed labels.

Outcome conventions:

- Command handle: ``"success"`` | ``"rejected"`` | ``"concurrency_error"`` | ``"error"``.
- Lease acquisition: ``"claimed"`` | ``"preempted"``.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Generator


class Recorder(Protocol):
    """The shape an SLI recorder must satisfy.

    Mirrors OpenTelemetry histogram semantics: each observation is a
    duration in seconds plus a small fixed label set. Implementations
    are free to bucket, sample, or forward to an exporter as they see
    fit.
    """

    def observe_command_handle(
        self,
        *,
        aggregate_type: str,
        command_type: str,
        outcome: str,
        duration_seconds: float,
    ) -> None:
        """Record a `CommandHandler.handle()` duration with outcome label."""
        ...

    def observe_dispatcher_fan_out(
        self,
        *,
        aggregate_type: str,
        duration_seconds: float,
    ) -> None:
        """Record one event's projector fan-out duration."""
        ...

    def observe_lease_acquisition(
        self,
        *,
        aggregate_type: str,
        projector_name: str,
        outcome: str,
        duration_seconds: float,
    ) -> None:
        """Record a finalization-lease claim attempt with outcome label."""
        ...


class _NullRecorder:
    def observe_command_handle(
        self,
        *,
        aggregate_type: str,
        command_type: str,
        outcome: str,
        duration_seconds: float,
    ) -> None:
        return

    def observe_dispatcher_fan_out(
        self,
        *,
        aggregate_type: str,
        duration_seconds: float,
    ) -> None:
        return

    def observe_lease_acquisition(
        self,
        *,
        aggregate_type: str,
        projector_name: str,
        outcome: str,
        duration_seconds: float,
    ) -> None:
        return


_recorder: Recorder = _NullRecorder()


def install_recorder(recorder: Recorder) -> None:
    """Install an application-provided recorder. Call once at startup."""
    global _recorder  # noqa: PLW0603 — module-level singleton is the install API
    _recorder = recorder


def get_recorder() -> Recorder:
    """Return the currently-installed recorder (no-op by default)."""
    return _recorder


@contextmanager
def time_observation() -> Generator[Callable[[], float], None, None]:
    """Time a block; yield a callable returning elapsed seconds.

    Example::

        with time_observation() as elapsed:
            do_work()
            duration = elapsed()
    """
    start = time.monotonic()
    yield lambda: time.monotonic() - start
