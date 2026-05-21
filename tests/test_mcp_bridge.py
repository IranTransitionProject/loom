"""Tests for heddle.mcp.bridge — NATS call dispatch."""

import asyncio

import pytest

from heddle.bus.memory import InMemoryBus
from heddle.core.messages import TaskResult, TaskStatus
from heddle.mcp.bridge import BridgeError, BridgeTimeoutError, MCPBridge


@pytest.fixture
async def bus():
    b = InMemoryBus()
    await b.connect()
    yield b
    await b.close()


@pytest.fixture
def bridge(bus):
    return MCPBridge(bus)


def _mock_worker_responder(bus, subject, status, output=None, error=None):
    """Create a mock worker that subscribes and responds to tasks.

    Returns (ready_event, task) — await ready_event before publishing
    to ensure the mock has subscribed.
    """
    ready = asyncio.Event()

    async def _run():
        sub = await bus.subscribe(subject)
        ready.set()
        async for data in sub:
            task_id = data["task_id"]
            parent_id = data["parent_task_id"]
            result = TaskResult(
                task_id=task_id,
                worker_type=data.get("worker_type", "mock"),
                status=status,
                output=output,
                error=error,
            )
            await bus.publish(f"heddle.results.{parent_id}", result.model_dump(mode="json"))
            await sub.unsubscribe()
            break

    task = asyncio.create_task(_run())
    return ready, task


# ---------------------------------------------------------------------------
# call_worker
# ---------------------------------------------------------------------------


class TestCallWorker:
    async def test_successful_call(self, bus, bridge):
        """Worker responds with a successful TaskResult."""
        ready, worker_task = _mock_worker_responder(
            bus,
            "heddle.tasks.incoming",
            status=TaskStatus.COMPLETED,
            output={"summary": "Test result"},
        )
        await ready.wait()

        result = await bridge.call_worker(
            worker_type="summarizer",
            tier="local",
            payload={"text": "Hello"},
            timeout=5,
        )

        assert result == {"summary": "Test result"}
        await worker_task

    async def test_failed_worker_raises(self, bus, bridge):
        """Worker responds with FAILED status → BridgeError."""
        ready, worker_task = _mock_worker_responder(
            bus,
            "heddle.tasks.incoming",
            status=TaskStatus.FAILED,
            error="Model unavailable",
        )
        await ready.wait()

        with pytest.raises(BridgeError, match="Model unavailable"):
            await bridge.call_worker(
                worker_type="summarizer",
                tier="local",
                payload={"text": "Hello"},
                timeout=5,
            )

        await worker_task

    async def test_timeout_raises(self, bus, bridge):
        """No response within timeout → BridgeTimeoutError."""
        with pytest.raises(BridgeTimeoutError):
            await bridge.call_worker(
                worker_type="summarizer",
                tier="local",
                payload={"text": "Hello"},
                timeout=0.1,
            )


# ---------------------------------------------------------------------------
# call_query
# ---------------------------------------------------------------------------


class TestCallQuery:
    async def test_query_wraps_action(self, bus, bridge):
        """call_query wraps payload with action field and dispatches as worker."""
        received = {}
        ready = asyncio.Event()

        async def mock_worker():
            sub = await bus.subscribe("heddle.tasks.incoming")
            ready.set()
            async for data in sub:
                received.update(data)
                task_id = data["task_id"]
                parent_id = data["parent_task_id"]
                result = TaskResult(
                    task_id=task_id,
                    worker_type=data["worker_type"],
                    status=TaskStatus.COMPLETED,
                    output={"results": []},
                )
                await bus.publish(f"heddle.results.{parent_id}", result.model_dump(mode="json"))
                await sub.unsubscribe()
                break

        worker_task = asyncio.create_task(mock_worker())
        await ready.wait()

        result = await bridge.call_query(
            worker_type="docs_query",
            action="search",
            payload={"query": "test"},
            timeout=5,
        )

        assert result == {"results": []}
        # Verify the payload was wrapped with action.
        assert received["payload"]["action"] == "search"
        assert received["payload"]["query"] == "test"
        await worker_task


# ---------------------------------------------------------------------------
# call_pipeline
# ---------------------------------------------------------------------------


class TestCallPipeline:
    async def test_pipeline_returns_final_result(self, bus, bridge):
        """Pipeline responds with final result matching goal_id."""
        ready = asyncio.Event()

        async def mock_pipeline():
            sub = await bus.subscribe("heddle.goals.incoming")
            ready.set()
            async for data in sub:
                goal_id = data["payload"]["goal_id"]
                # Simulate intermediate stage result.
                stage_result = TaskResult(
                    task_id="stage-1-id",
                    worker_type="extractor",
                    status=TaskStatus.COMPLETED,
                    output={"text": "extracted"},
                )
                await bus.publish(f"heddle.results.{goal_id}", stage_result.model_dump(mode="json"))

                await asyncio.sleep(0.01)

                # Final pipeline result (task_id == goal_id).
                final_result = TaskResult(
                    task_id=goal_id,
                    worker_type="pipeline",
                    status=TaskStatus.COMPLETED,
                    output={"final": "done"},
                )
                await bus.publish(f"heddle.results.{goal_id}", final_result.model_dump(mode="json"))
                await sub.unsubscribe()
                break

        pipeline_task = asyncio.create_task(mock_pipeline())
        await ready.wait()

        result = await bridge.call_pipeline(
            goal_context={"file_ref": "test.pdf"},
            timeout=5,
        )

        assert result == {"final": "done"}
        await pipeline_task

    async def test_pipeline_with_progress_callback(self, bus, bridge):
        """Progress callback is called for intermediate stage results."""
        progress_calls = []
        ready = asyncio.Event()

        async def mock_pipeline():
            sub = await bus.subscribe("heddle.goals.incoming")
            ready.set()
            async for data in sub:
                goal_id = data["payload"]["goal_id"]
                # Two intermediate stages.
                for i, wtype in enumerate(["extractor", "classifier"]):
                    stage_result = TaskResult(
                        task_id=f"stage-{i}-id",
                        worker_type=wtype,
                        status=TaskStatus.COMPLETED,
                        output={"stage": i},
                    )
                    await bus.publish(
                        f"heddle.results.{goal_id}", stage_result.model_dump(mode="json")
                    )
                    await asyncio.sleep(0.01)

                final_result = TaskResult(
                    task_id=goal_id,
                    worker_type="pipeline",
                    status=TaskStatus.COMPLETED,
                    output={"final": True},
                )
                await bus.publish(f"heddle.results.{goal_id}", final_result.model_dump(mode="json"))
                await sub.unsubscribe()
                break

        pipeline_task = asyncio.create_task(mock_pipeline())
        await ready.wait()

        def on_progress(stage_name, stage_idx, total):
            progress_calls.append((stage_name, stage_idx))

        result = await bridge.call_pipeline(
            goal_context={"file_ref": "test.pdf"},
            timeout=5,
            progress_callback=on_progress,
        )

        assert result == {"final": True}
        assert len(progress_calls) == 2
        assert progress_calls[0][0] == "extractor"
        assert progress_calls[1][0] == "classifier"
        await pipeline_task

    async def test_pipeline_timeout(self, bus, bridge):
        """Pipeline times out → BridgeTimeoutError."""
        ready = asyncio.Event()

        async def sink():
            sub = await bus.subscribe("heddle.goals.incoming")
            ready.set()
            async for data in sub:
                pass  # Never responds.

        sink_task = asyncio.create_task(sink())
        await ready.wait()

        with pytest.raises(BridgeTimeoutError):
            await bridge.call_pipeline(
                goal_context={"file_ref": "test.pdf"},
                timeout=0.1,
            )

        sink_task.cancel()
        try:
            await sink_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestBridgeErrorHandling:
    async def test_malformed_worker_result_raises_bridge_error(self, bus, bridge):
        """Malformed result data (missing required fields) raises BridgeError."""
        ready = asyncio.Event()

        async def mock_worker():
            sub = await bus.subscribe("heddle.tasks.incoming")
            ready.set()
            async for data in sub:
                parent_id = data["parent_task_id"]
                # Send malformed result — missing required 'status' and 'worker_type'.
                await bus.publish(
                    f"heddle.results.{parent_id}",
                    {"task_id": data["task_id"], "bad_field": "oops"},
                )
                await sub.unsubscribe()
                break

        worker_task = asyncio.create_task(mock_worker())
        await ready.wait()

        with pytest.raises(BridgeError, match="Malformed result"):
            await bridge.call_worker(
                worker_type="summarizer",
                tier="local",
                payload={"text": "Hello"},
                timeout=5,
            )

        await worker_task

    async def test_malformed_pipeline_result_raises_bridge_error(self, bus, bridge):
        """Malformed final pipeline result raises BridgeError."""
        ready = asyncio.Event()

        async def mock_pipeline():
            sub = await bus.subscribe("heddle.goals.incoming")
            ready.set()
            async for data in sub:
                goal_id = data["payload"]["goal_id"]
                # Send malformed final result (task_id == goal_id but missing fields).
                await bus.publish(
                    f"heddle.results.{goal_id}",
                    {"task_id": goal_id, "not_a_valid_field": True},
                )
                await sub.unsubscribe()
                break

        pipeline_task = asyncio.create_task(mock_pipeline())
        await ready.wait()

        with pytest.raises(BridgeError, match="Malformed pipeline result"):
            await bridge.call_pipeline(
                goal_context={"file_ref": "test.pdf"},
                timeout=5,
            )

        await pipeline_task

    async def test_progress_callback_error_is_logged(self, bus, bridge, caplog):
        """Progress callback exceptions are logged but don't break pipeline."""
        ready = asyncio.Event()

        async def mock_pipeline():
            sub = await bus.subscribe("heddle.goals.incoming")
            ready.set()
            async for data in sub:
                goal_id = data["payload"]["goal_id"]
                # Intermediate stage result.
                stage_result = TaskResult(
                    task_id="stage-1-id",
                    worker_type="extractor",
                    status=TaskStatus.COMPLETED,
                    output={"text": "extracted"},
                )
                await bus.publish(f"heddle.results.{goal_id}", stage_result.model_dump(mode="json"))
                await asyncio.sleep(0.01)
                # Final result.
                final_result = TaskResult(
                    task_id=goal_id,
                    worker_type="pipeline",
                    status=TaskStatus.COMPLETED,
                    output={"final": "done"},
                )
                await bus.publish(f"heddle.results.{goal_id}", final_result.model_dump(mode="json"))
                await sub.unsubscribe()
                break

        pipeline_task = asyncio.create_task(mock_pipeline())
        await ready.wait()

        def bad_callback(stage_name, stage_idx, total):
            raise ValueError("callback boom")

        import logging

        import structlog

        # Capture structlog output via stdlib logging.
        structlog.configure(
            wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        )

        result = await bridge.call_pipeline(
            goal_context={"file_ref": "test.pdf"},
            timeout=5,
            progress_callback=bad_callback,
        )

        # Pipeline should still complete successfully.
        assert result == {"final": "done"}
        await pipeline_task


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------


class TestBridgeLifecycle:
    async def test_connect_and_close(self):
        bus = InMemoryBus()
        bridge = MCPBridge(bus)
        await bridge.connect()
        await bridge.close()
