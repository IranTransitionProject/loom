"""
Streaming result collection for orchestrators.

Provides ``ResultStream``, an async iterator that yields ``TaskResult``
objects as they arrive from the message bus — rather than blocking until
all results are collected.

Lifecycle (publish-before-subscribe race avoidance)::

    stream = ResultStream(bus, subject, expected_ids, timeout)
    async with stream:                # subscribes
        await dispatch_subtasks(...)  # safe to publish now
        results = await stream.collect_all()

Subscribing BEFORE the caller publishes any task whose result we expect
is mandatory — NATS is at-most-once.  If a fast worker publishes its
result onto ``heddle.results.{goal_id}`` before we have an active
subscription, that result is lost and the goal will time out.  The
``async with`` block makes the ordering explicit at every call site.

Two consumption modes:

    1. **Batch** (backward compatible with pre-Strategy-A code)::

           async with ResultStream(bus, subject, expected_ids, timeout) as stream:
               await dispatch(...)
               results = await stream.collect_all()

    2. **Incremental** — enables progress callbacks and early exit::

           async with ResultStream(bus, subject, ids, timeout,
                                   on_result=my_progress_callback) as stream:
               await dispatch(...)
               async for result in stream:
                   # process each result as it arrives
                   ...

The ``on_result`` callback is invoked for every arriving result with the
signature ``(result, collected_count, expected_count) -> bool | None``.
Returning ``True`` signals early exit — the stream stops collecting and
the caller gets whatever has arrived so far.

This module is used by:

- ``OrchestratorActor._collect_results()`` — dynamic orchestrator
- Potentially by ``MCPBridge`` for richer progress reporting (future)

Design decisions:

- **Subscribe-before-publish enforced**: callers must use ``async with``
  (or explicit ``start()``).  Iterating without entering the context
  raises a :class:`RuntimeError` rather than silently lazy-subscribing —
  the lazy form was the original race and is now treated as a bug.
- **Single-use**: a ``ResultStream`` can only be iterated once (it owns
  the bus subscription lifecycle).
- **Callback errors are non-fatal**: if ``on_result`` raises, the error
  is logged and collection continues.
- **Duplicate filtering**: results for the same ``task_id`` are silently
  skipped (at-least-once delivery tolerance).
- **Unknown task_ids are ignored**: only results matching
  ``expected_task_ids`` are collected.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol

import structlog

from heddle.core.messages import TaskResult, parse_task_result

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from types import TracebackType

    from heddle.bus.base import MessageBus, Subscription

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Callback protocol
# ---------------------------------------------------------------------------


class ResultCallback(Protocol):
    """Callback invoked when a result arrives during streaming collection.

    Parameters
    ----------
    result : TaskResult
        The just-arrived result.
    collected : int
        How many results have been collected so far (including this one).
    expected : int
        Total number of expected results.

    Returns:
    -------
    bool | None
        Return ``True`` to signal early exit (stop collecting).
        Return ``None`` or ``False`` to continue.
    """

    async def __call__(  # noqa: D102
        self,
        result: TaskResult,
        collected: int,
        expected: int,
    ) -> bool | None: ...


# ---------------------------------------------------------------------------
# ResultStream
# ---------------------------------------------------------------------------


class ResultStream:
    """Async iterator that yields ``TaskResult`` objects as they arrive.

    Wraps a bus subscription for a specific result subject, filtering
    incoming messages to only those matching ``expected_task_ids``.

    The stream terminates when:

    - All expected results have arrived, OR
    - The timeout expires, OR
    - The ``on_result`` callback returns ``True`` (early exit), OR
    - The subscription is closed.

    After iteration, inspect :attr:`collected`, :attr:`timed_out`, and
    :attr:`early_exited` for post-mortem state.

    Parameters
    ----------
    bus : MessageBus
        The message bus to subscribe on.
    subject : str
        NATS subject to subscribe to (e.g. ``heddle.results.{goal_id}``).
    expected_task_ids : set[str]
        Set of task_ids we expect results for.
    timeout : float
        Maximum seconds to wait for all results.
    on_result : ResultCallback | None
        Optional callback invoked as each result arrives.

    Example:
    -------
    ::

        stream = ResultStream(
            bus=nats_bus,
            subject=f"heddle.results.{goal_id}",
            expected_task_ids={"task-1", "task-2", "task-3"},
            timeout=60.0,
            on_result=my_progress_handler,
        )

        # Batch mode (drop-in replacement for old collect):
        results = await stream.collect_all()

        # Or streaming mode:
        async for result in stream:
            print(f"Got {result.worker_type}: {result.status}")
    """

    def __init__(
        self,
        bus: MessageBus,
        subject: str,
        expected_task_ids: set[str],
        timeout: float,
        *,
        on_result: ResultCallback | None = None,
    ) -> None:
        self._bus = bus
        self._subject = subject
        self._expected_ids = frozenset(expected_task_ids)
        self._timeout = timeout
        self._on_result = on_result

        # Mutable state — populated during iteration.
        self._collected: dict[str, TaskResult] = {}
        self._timed_out: bool = False
        self._early_exited: bool = False
        self._consumed: bool = False
        # Subscription lifecycle.  ``_sub`` is set by ``start()``/``__aenter__``
        # and cleared by ``aclose()``/``__aexit__``.  Iterating without
        # entering the context raises (see ``__aiter__``) — this is the
        # publish-before-subscribe race fix: callers MUST subscribe
        # before they publish any task whose result we expect.
        self._sub: Subscription | None = None

    # ------------------------------------------------------------------
    # Read-only state inspection
    # ------------------------------------------------------------------

    @property
    def collected(self) -> dict[str, TaskResult]:
        """Map of task_id → TaskResult for all collected results."""
        return self._collected

    @property
    def expected_count(self) -> int:
        """Number of results we expect."""
        return len(self._expected_ids)

    @property
    def collected_count(self) -> int:
        """Number of results collected so far."""
        return len(self._collected)

    @property
    def all_collected(self) -> bool:
        """True when every expected result has arrived."""
        return self.collected_count >= self.expected_count

    @property
    def timed_out(self) -> bool:
        """True if collection ended due to timeout."""
        return self._timed_out

    @property
    def early_exited(self) -> bool:
        """True if collection ended due to on_result callback signaling stop."""
        return self._early_exited

    @property
    def pending_ids(self) -> frozenset[str]:
        """Task IDs that were expected but never arrived."""
        return self._expected_ids - frozenset(self._collected.keys())

    # ------------------------------------------------------------------
    # Consumption API
    # ------------------------------------------------------------------

    async def start(self) -> ResultStream:
        """Subscribe to the bus subject.

        MUST be called before the caller publishes any task whose result
        is expected on this subject — NATS is at-most-once.  Prefer
        ``async with stream:`` (which calls ``start`` on ``__aenter__``)
        to make the ordering explicit at the call site.

        Idempotent — calling twice is an error.  Returns ``self`` to
        allow ``stream = await ResultStream(...).start()`` if the caller
        prefers that style.
        """
        if self._sub is not None:
            raise RuntimeError("ResultStream.start() called twice")
        self._sub = await self._bus.subscribe(self._subject)
        return self

    async def aclose(self) -> None:
        """Release the subscription.  Idempotent.

        Safe to call from a ``finally`` block; the second call is a
        no-op.
        """
        if self._sub is not None:
            sub = self._sub
            self._sub = None
            await sub.unsubscribe()

    async def __aenter__(self) -> ResultStream:
        """Subscribe so the caller can publish without losing fast results."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Release the subscription regardless of how the block exits."""
        await self.aclose()

    async def collect_all(self) -> list[TaskResult]:
        """Consume the stream fully, returning all collected results as a list.

        Must be called from inside an ``async with`` block (or after an
        explicit ``start()``).
        """
        return [result async for result in self]

    def __aiter__(self) -> AsyncIterator[TaskResult]:
        """Return the async iterator (self — delegates to _stream)."""
        if self._sub is None:
            raise RuntimeError(
                "ResultStream must be started before iteration. "
                "Use 'async with stream:' or 'await stream.start()' "
                "BEFORE publishing tasks whose results you expect — "
                "subscribing afterwards loses any result that the worker "
                "publishes between dispatch and the first iteration."
            )
        if self._consumed:
            raise RuntimeError(
                "ResultStream has already been consumed. "
                "Create a new ResultStream for another iteration."
            )
        self._consumed = True
        return self._stream()

    async def _stream(self) -> AsyncIterator[TaskResult]:  # noqa: PLR0912, PLR0915
        """Internal async generator that drives the collection loop.

        Reads messages from the subscription owned by ``start()`` /
        ``__aenter__``, filters/deduplicates, invokes callbacks, and
        yields results.  The subscription is NOT released here; the
        ``async with`` block owns the unsubscribe (in ``aclose``).
        """
        # Bound here so a concurrent aclose() during iteration does not
        # pull the rug; we hold a local reference to the live sub.
        sub = self._sub
        if sub is None:  # pragma: no cover — guarded by __aiter__
            raise RuntimeError("ResultStream._stream called before start()")
        deadline = asyncio.get_running_loop().time() + self._timeout

        log = logger.bind(
            subject=self._subject,
            expected=self.expected_count,
        )
        log.debug("result_stream.started", timeout=self._timeout)

        try:
            while not self.all_collected and not self._early_exited:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    self._timed_out = True
                    log.warning(
                        "result_stream.timeout",
                        collected=self.collected_count,
                        expected=self.expected_count,
                    )
                    break

                # Wait for the next message from the bus.
                # ``TimeoutError`` means the deadline elapsed — pages
                # an operator alerting on stuck results.
                # ``StopAsyncIteration`` means the subscription was
                # closed cleanly (bus shutdown, actor disconnect) —
                # not actionable, log at info under a distinct event
                # name so alerting can distinguish.  Earlier both
                # paths logged ``result_stream.timeout`` and an
                # operator with paging on that event got woken up on
                # every clean shutdown.
                try:
                    data = await asyncio.wait_for(
                        sub.__anext__(),
                        timeout=remaining,
                    )
                except TimeoutError:
                    self._timed_out = True
                    log.warning(
                        "result_stream.timeout",
                        collected=self.collected_count,
                        expected=self.expected_count,
                    )
                    break
                except StopAsyncIteration:
                    self._timed_out = True
                    log.info(
                        "result_stream.closed",
                        collected=self.collected_count,
                        expected=self.expected_count,
                        reason="subscription_closed",
                    )
                    break

                # Filter: only accept results we dispatched. task_id lives on
                # the body (envelope payload), not the frame.
                task_id = (data.get("payload") or {}).get("task_id")
                if task_id not in self._expected_ids:
                    log.debug(
                        "result_stream.ignored",
                        task_id=task_id,
                        reason="not_expected",
                    )
                    continue

                # Deduplicate: skip results we already collected.
                if task_id in self._collected:
                    log.debug(
                        "result_stream.duplicate",
                        task_id=task_id,
                    )
                    continue

                # Parse the result.  ``parse_task_result`` shares the
                # skip-and-log behaviour with
                # :func:`heddle.orchestrator.dispatch.dispatch_and_wait_for_result`;
                # both log on the ``*.parse_error`` family so an operator
                # can grep both modules with one query.  ``subject`` /
                # ``expected`` were bound on ``log`` above; forward them
                # explicitly so the parse-error event keeps the same
                # shape it had before extraction.
                result = parse_task_result(
                    data,
                    log_event="result_stream.parse_error",
                    task_id=task_id,
                    subject=self._subject,
                    expected=self.expected_count,
                )
                if result is None:
                    continue

                self._collected[task_id] = result
                log.info(
                    "result_stream.collected",
                    task_id=task_id,
                    worker_type=result.worker_type,
                    status=result.status.value,
                    collected=self.collected_count,
                    expected=self.expected_count,
                )

                # Invoke callback (non-fatal on error).
                if self._on_result is not None:
                    try:
                        stop = self._on_result(
                            result,
                            self.collected_count,
                            self.expected_count,
                        )
                        if asyncio.iscoroutine(stop):
                            stop = await stop
                        if stop:
                            self._early_exited = True
                            log.info(
                                "result_stream.early_exit",
                                collected=self.collected_count,
                            )
                    except Exception as cb_err:
                        log.warning(
                            "result_stream.callback_error",
                            error=str(cb_err),
                        )

                yield result

        finally:
            # Subscription cleanup belongs to ``aclose()``/``__aexit__`` —
            # the caller's ``async with`` block owns the unsubscribe.
            log.debug(
                "result_stream.finished",
                collected=self.collected_count,
                timed_out=self._timed_out,
                early_exited=self._early_exited,
            )
