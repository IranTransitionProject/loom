"""Shared timeout helper for the council framework.

The council has two execution paths — :class:`CouncilOrchestrator`
(NATS, dispatched via :mod:`heddle.orchestrator.dispatch`) and
:class:`CouncilRunner` (NATS-free, called directly).  Both must
enforce the same per-turn budget and synthesis budget so a wedged
backend cannot starve the deliberation indefinitely.

The orchestrator already wrapped its synthesis call in
``asyncio.wait_for``; the runner did not.  This module factors the
wrapper into one helper so the contract is identical on both paths,
and surfaces a typed :class:`CouncilTimeoutError` so callers can
distinguish "council budget exceeded" from a generic
:class:`asyncio.TimeoutError` raised somewhere downstream.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Awaitable

T = TypeVar("T")


class CouncilTimeoutError(TimeoutError):
    """Raised when a council per-turn or synthesis budget is exceeded.

    Subclasses :class:`TimeoutError` (the builtin, which
    ``asyncio.TimeoutError`` aliases since Python 3.11) so existing
    ``except TimeoutError`` blocks still catch it, but the typed
    subclass plus ``label`` / ``timeout_seconds`` attributes let
    callers attribute the timeout to a specific phase (agent turn,
    synthesis, convergence judge) without parsing strings.
    """

    def __init__(self, label: str, timeout_seconds: float) -> None:
        self.label = label
        self.timeout_seconds = timeout_seconds
        super().__init__(f"{label} exceeded {timeout_seconds:.1f}s budget")


async def call_with_budget(
    coro: Awaitable[T],
    *,
    timeout_seconds: float,
    label: str,
) -> T:
    """Await ``coro`` with a council budget; raise :class:`CouncilTimeoutError` on expiry.

    Thin wrapper over :func:`asyncio.wait_for`.  Centralised here so
    the runner and orchestrator share one place to evolve budget
    semantics (e.g. add structured logging on every budget hit).
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout_seconds)
    except TimeoutError as exc:
        raise CouncilTimeoutError(label, timeout_seconds) from exc
