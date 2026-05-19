"""Internal pytest fixtures for heddle's own test suite.

Public-facing test utilities — factories, fake aggregates — live in
:mod:`heddle.contrib.events.testing` and are importable by downstream
apps. The fixtures here are heddle-only; they assemble the in-memory
runtime against the public fixtures-friendly entry points.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio

from heddle.contrib.events.command_handler import CommandHandler
from heddle.contrib.events.dispatcher import EventDispatcher
from heddle.contrib.events.event_log import InMemoryEventLog
from heddle.contrib.events.projectors import (
    CascadeProjector,
    ScopeMembershipProjector,
)
from heddle.contrib.events.registry import AGGREGATE_REGISTRY
from heddle.contrib.events.rejection_log import InMemoryRejectionLog


@pytest.fixture
def registry_isolation() -> Iterator[None]:
    """Snapshot and restore AGGREGATE_REGISTRY around a test.

    Use in any test that registers new aggregates (including
    accidentally via importing modules that decorate at import).
    Imports that happened BEFORE the fixture activates remain
    registered; only test-local additions are rolled back.
    """
    snapshot = dict(AGGREGATE_REGISTRY)
    yield
    AGGREGATE_REGISTRY.clear()
    AGGREGATE_REGISTRY.update(snapshot)


@pytest_asyncio.fixture
async def in_memory_event_log() -> AsyncIterator[InMemoryEventLog]:
    yield InMemoryEventLog()


@pytest_asyncio.fixture
async def in_memory_rejection_log() -> AsyncIterator[InMemoryRejectionLog]:
    yield InMemoryRejectionLog()


@pytest_asyncio.fixture
async def command_handler(
    in_memory_event_log: InMemoryEventLog,
    in_memory_rejection_log: InMemoryRejectionLog,
) -> AsyncIterator[CommandHandler]:
    yield CommandHandler(in_memory_event_log, in_memory_rejection_log)


@pytest_asyncio.fixture
async def membership_projector() -> AsyncIterator[ScopeMembershipProjector]:
    yield ScopeMembershipProjector()


@pytest_asyncio.fixture
async def wired_dispatcher(
    in_memory_event_log: InMemoryEventLog,
    membership_projector: ScopeMembershipProjector,
    command_handler: CommandHandler,
) -> AsyncIterator[EventDispatcher]:
    """Dispatcher pre-wired with P1 + P2.

    P3 deliberately not included (Sprint 2 stub — adds nothing to
    test fan-out).
    """
    dispatcher = EventDispatcher(in_memory_event_log)
    dispatcher.register(membership_projector)
    dispatcher.register(CascadeProjector(membership_projector, command_handler))
    try:
        yield dispatcher
    finally:
        await dispatcher.stop()
