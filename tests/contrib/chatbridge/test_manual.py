"""Tests for ManualChatBridge."""

import asyncio

import pytest

from heddle.contrib.chatbridge.manual import ManualChatBridge


class TestManualCallbackMode:
    async def test_callback_returns_response(self):
        async def responder(message, context, session_id):
            return f"Human says: {message}"

        bridge = ManualChatBridge(on_prompt=responder)
        resp = await bridge.send_turn("What do you think?", {}, "sess_1")
        assert resp.content == "Human says: What do you think?"
        assert resp.model == "human"
        assert resp.stop_reason == "human_input"

    async def test_callback_timeout(self):
        async def slow_responder(message, context, session_id):
            await asyncio.sleep(10)
            return "too late"

        bridge = ManualChatBridge(on_prompt=slow_responder, timeout_seconds=0.1)
        with pytest.raises(asyncio.TimeoutError):
            await bridge.send_turn("Hello?", {}, "sess_1")


class TestManualQueueMode:
    async def test_queue_flow(self):
        prompt_q: asyncio.Queue = asyncio.Queue()
        response_q: asyncio.Queue = asyncio.Queue()

        bridge = ManualChatBridge(
            prompt_queue=prompt_q,
            response_queue=response_q,
            timeout_seconds=5.0,
        )

        # Simulate external responder.
        async def respond():
            prompt = await prompt_q.get()
            assert "session_id" in prompt
            await response_q.put("I agree with the proposal.")

        task = asyncio.create_task(respond())
        resp = await bridge.send_turn("Review this", {"round": 1}, "sess_1")
        await task

        assert resp.content == "I agree with the proposal."

    async def test_queue_timeout(self):
        prompt_q: asyncio.Queue = asyncio.Queue()
        response_q: asyncio.Queue = asyncio.Queue()

        bridge = ManualChatBridge(
            prompt_queue=prompt_q,
            response_queue=response_q,
            timeout_seconds=0.1,
        )

        with pytest.raises(asyncio.TimeoutError):
            await bridge.send_turn("Hello?", {}, "sess_1")


class TestManualSessionInfo:
    async def test_session_info_after_turns(self):
        async def responder(message, context, session_id):
            return "ok"

        bridge = ManualChatBridge(on_prompt=responder)
        await bridge.send_turn("Turn 1", {}, "sess_1")
        await bridge.send_turn("Turn 2", {}, "sess_1")

        info = await bridge.get_session_info("sess_1")
        assert info.bridge_type == "manual"
        assert info.message_count == 4  # 2 system + 2 human


class TestManualValidation:
    async def test_no_handler_raises(self):
        bridge = ManualChatBridge()
        with pytest.raises(ValueError, match="on_prompt"):
            await bridge.send_turn("Hello", {}, "sess_1")


class TestManualRollback:
    async def test_user_message_rolled_back_on_callback_error(self):
        """J6 / D2: a failing callback leaves session.messages untouched.

        For the manual bridge the analog of "API call failed" is
        "human callback raised before responding."  The bridge
        promises that ``session.messages`` only grows after the
        callback returns a response, so the next prompt isn't a
        ghost of the failed one.
        """

        async def good_responder(message, context, session_id):
            return f"ok: {message}"

        async def bad_responder(message, context, session_id):
            raise RuntimeError("operator walked off")

        # Seed one successful turn so the rollback delta is visible.
        bridge = ManualChatBridge(on_prompt=good_responder)
        await bridge.send_turn("seed", {}, "sess_x")
        assert len(bridge._sessions["sess_x"].messages) == 2

        # Swap in the failing responder.  A bridge that appended
        # eagerly would leave the failed prompt in history.
        bridge._on_prompt = bad_responder
        with pytest.raises(RuntimeError, match="walked off"):
            await bridge.send_turn("dropped", {}, "sess_x")
        assert len(bridge._sessions["sess_x"].messages) == 2
        assert all(m.get("content") != "dropped" for m in bridge._sessions["sess_x"].messages)

    async def test_user_message_rolled_back_on_callback_timeout(self):
        """Timeout is the other manual-bridge failure mode; same invariant."""

        async def slow_responder(message, context, session_id):
            await asyncio.sleep(10)
            return "too late"

        async def good_responder(message, context, session_id):
            return f"ok: {message}"

        bridge = ManualChatBridge(on_prompt=good_responder)
        await bridge.send_turn("seed", {}, "sess_x")
        assert len(bridge._sessions["sess_x"].messages) == 2

        bridge._on_prompt = slow_responder
        bridge._timeout = 0.05
        with pytest.raises(asyncio.TimeoutError):
            await bridge.send_turn("dropped", {}, "sess_x")
        assert len(bridge._sessions["sess_x"].messages) == 2
        assert all(m.get("content") != "dropped" for m in bridge._sessions["sess_x"].messages)
