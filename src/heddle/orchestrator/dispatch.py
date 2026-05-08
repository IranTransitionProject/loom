"""Shared dispatch helper for orchestrator → worker request-reply.

Both :class:`heddle.orchestrator.pipeline.PipelineOrchestrator` and
:class:`heddle.contrib.council.orchestrator.CouncilOrchestrator` need
the same subscribe → publish → wait dance to dispatch a task to a
worker over NATS and collect its single matching result.  The
implementations diverged briefly during this session's bug-fix work
(``636bdc0`` — subscribe-before-publish ordering); pulling the body
into one function keeps the ordering invariant in one place so a
future orchestrator can't accidentally invert it.

Why subscribe-before-publish (load-bearing comment, not just trivia):

NATS is at-most-once.  If the worker finishes between ``publish`` and
``subscribe``, the result is delivered to nobody and the caller times
out.  The fix is to subscribe first, then publish — guaranteeing the
subscription is live before any worker can possibly respond.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from heddle.core.messages import TaskResult

if TYPE_CHECKING:
    from heddle.bus.base import MessageBus
    from heddle.core.messages import TaskMessage


_INCOMING_SUBJECT = "heddle.tasks.incoming"


async def dispatch_and_wait_for_result(
    bus: MessageBus,
    task: TaskMessage,
    task_data: dict[str, Any],
    goal_id: str,
    timeout: float,
) -> TaskResult | None:
    """Subscribe → publish → wait, in that order.

    Args:
        bus: The message bus to subscribe + publish on.
        task: The :class:`TaskMessage` whose result we're waiting for
            (used to filter the result subscription by ``task.task_id``).
        task_data: Pre-serialised task payload to publish on
            ``heddle.tasks.incoming`` (typically ``task.model_dump(mode="json")``).
        goal_id: Identifier whose result subject (``heddle.results.{goal_id}``)
            we subscribe to.
        timeout: Seconds to wait for the matching result.

    Returns:
        The matching :class:`TaskResult`, or ``None`` if no result
        arrived within ``timeout``.
    """
    result_future: asyncio.Future[TaskResult] = asyncio.get_running_loop().create_future()
    subject = f"heddle.results.{goal_id}"

    # Step 1: subscribe FIRST so the subscription is live before any
    # worker can respond.  NATS is at-most-once; a result published
    # without a live subscriber is dropped silently.
    sub = await bus.subscribe(subject)

    async def _consume() -> None:
        async for data in sub:
            if data.get("task_id") == task.task_id:
                with contextlib.suppress(asyncio.InvalidStateError):
                    result_future.set_result(TaskResult(**data))
                break

    consume_task = asyncio.create_task(_consume())

    try:
        # Step 2: now safe to publish — subscription is live.
        await bus.publish(_INCOMING_SUBJECT, task_data)
        # Step 3: wait for the matching result (or timeout).
        return await asyncio.wait_for(result_future, timeout=timeout)
    except TimeoutError:
        return None
    finally:
        # Cancel + AWAIT the consumer so it has a chance to exit cleanly
        # before we drop the subscription.  Cancelling without awaiting
        # races with ``unsubscribe()`` on the same subscription object.
        consume_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consume_task
        await sub.unsubscribe()
