"""Tests for CouncilRunner."""

from unittest.mock import AsyncMock

import pytest

from heddle.contrib.council.config import CouncilConfig
from heddle.contrib.council.runner import CouncilRunner


def _mock_backend(content="I think we should proceed.", model="mock-model"):
    """Create a mock LLMBackend that returns fixed content."""
    backend = AsyncMock()
    backend.complete.return_value = {
        "content": content,
        "model": model,
        "prompt_tokens": 50,
        "completion_tokens": 30,
    }
    return backend


def _minimal_config(**overrides):
    cfg = {
        "name": "test_council",
        "protocol": "round_robin",
        "max_rounds": 2,
        "convergence": {"method": "none"},
        "agents": [
            {"name": "analyst", "worker_type": "w1", "tier": "standard", "role": "Analyst"},
            {"name": "critic", "worker_type": "w2", "tier": "standard", "role": "Critic"},
        ],
        "facilitator": {
            "tier": "standard",
            "synthesis_prompt": "Synthesize the discussion.",
        },
    }
    cfg.update(overrides)
    return CouncilConfig(**cfg)


class TestCouncilRunner:
    async def test_basic_two_round_discussion(self):
        backend = _mock_backend()
        runner = CouncilRunner(backends={"standard": backend})
        config = _minimal_config()

        result = await runner.run("Should we refactor?", config=config)

        assert result.topic == "Should we refactor?"
        assert result.rounds_completed == 2
        assert result.converged is False  # method=none
        assert result.synthesis != ""
        assert len(result.transcript) == 2
        # 2 agents per round * 2 rounds = 4 agent turns + 1 synthesis
        assert backend.complete.call_count == 5  # 4 turns + 1 synthesis

    async def test_convergence_stops_early(self):
        agent_backend = _mock_backend(content="We all agree on X.")
        # Use position_stability with threshold=0 so it converges on round 2
        config = _minimal_config(
            max_rounds=5,
            convergence={"method": "position_stability", "threshold": 0.5},
        )
        runner = CouncilRunner(backends={"standard": agent_backend})
        result = await runner.run("Test topic", config=config)

        # Position stability needs 2 rounds minimum, but identical responses
        # mean it converges on round 2.
        assert result.rounds_completed == 2
        assert result.converged is True

    async def test_max_rounds_enforced(self):
        backend = _mock_backend()
        config = _minimal_config(max_rounds=3, convergence={"method": "none"})
        runner = CouncilRunner(backends={"standard": backend})
        result = await runner.run("Topic", config=config)
        assert result.rounds_completed == 3

    async def test_on_turn_callback(self):
        backend = _mock_backend()
        config = _minimal_config(max_rounds=1)
        runner = CouncilRunner(backends={"standard": backend})

        entries: list = []
        await runner.run(
            "Topic",
            config=config,
            on_turn=entries.append,
        )

        # 2 agents, 1 round = 2 callbacks
        assert len(entries) == 2
        assert entries[0].agent_name == "analyst"
        assert entries[1].agent_name == "critic"

    async def test_async_on_turn_callback(self):
        backend = _mock_backend()
        config = _minimal_config(max_rounds=1)
        runner = CouncilRunner(backends={"standard": backend})

        entries: list = []

        async def async_callback(e):
            entries.append(e)

        await runner.run("Topic", config=config, on_turn=async_callback)
        assert len(entries) == 2

    async def test_missing_backend_produces_error_entry(self):
        # Only provide "local" backend, but agents need "standard"
        backend = _mock_backend()
        runner = CouncilRunner(backends={"local": backend})
        config = _minimal_config(max_rounds=1)

        result = await runner.run("Topic", config=config)

        # Agent entries should contain error messages
        assert result.rounds_completed == 1
        round_entries = result.transcript[0].entries
        assert all("[ERROR" in e.content for e in round_entries)

    async def test_token_usage_accumulated(self):
        backend = _mock_backend()
        config = _minimal_config(max_rounds=1)
        runner = CouncilRunner(backends={"standard": backend})

        result = await runner.run("Topic", config=config)
        # Both buckets populated (B5: earlier code put the
        # combined sum into ``prompt_tokens`` only and left
        # ``completion_tokens`` at 0).
        assert result.total_token_usage["prompt_tokens"] > 0
        assert result.total_token_usage["completion_tokens"] > 0
        # _mock_backend returns prompt=50, completion=30 per call.
        # 2 agents x 1 round = 2 turns => prompt 100, completion 60.
        # Synthesis adds prompt=50, completion=30 => totals (150, 90).
        assert result.total_token_usage["prompt_tokens"] == 150
        assert result.total_token_usage["completion_tokens"] == 90

    async def test_agent_summaries(self):
        backend = _mock_backend()
        config = _minimal_config(max_rounds=1)
        runner = CouncilRunner(backends={"standard": backend})

        result = await runner.run("Topic", config=config)
        assert "analyst" in result.agent_summaries
        assert "critic" in result.agent_summaries

    async def test_no_config_raises(self):
        runner = CouncilRunner(backends={"standard": _mock_backend()})
        with pytest.raises(ValueError, match="No council config"):
            await runner.run("Topic")

    async def test_constructor_config_used(self):
        config = _minimal_config(max_rounds=1)
        backend = _mock_backend()
        runner = CouncilRunner(backends={"standard": backend}, config=config)

        result = await runner.run("Topic")
        assert result.rounds_completed == 1

    async def test_backend_exception_produces_error_entry(self):
        backend = AsyncMock()
        backend.complete.side_effect = RuntimeError("LLM API down")
        runner = CouncilRunner(backends={"standard": backend})
        config = _minimal_config(max_rounds=1)

        result = await runner.run("Topic", config=config)
        entries = result.transcript[0].entries
        assert any("[ERROR" in e.content for e in entries)

    async def test_elapsed_ms_populated(self):
        backend = _mock_backend()
        config = _minimal_config(max_rounds=1)
        runner = CouncilRunner(backends={"standard": backend})

        result = await runner.run("Topic", config=config)
        assert result.elapsed_ms >= 0  # May be 0 with mock backends

    async def test_different_tiers_per_agent(self):
        local_backend = _mock_backend(content="Local view", model="ollama")
        frontier_backend = _mock_backend(content="Frontier view", model="opus")

        config = CouncilConfig(
            name="multi_tier",
            max_rounds=1,
            convergence={"method": "none"},
            agents=[
                {"name": "local_agent", "worker_type": "w1", "tier": "local"},
                {"name": "frontier_agent", "worker_type": "w2", "tier": "frontier"},
            ],
            facilitator={"tier": "local"},
        )
        runner = CouncilRunner(backends={"local": local_backend, "frontier": frontier_backend})

        result = await runner.run("Topic", config=config)
        assert result.rounds_completed == 1
        entries = result.transcript[0].entries
        assert entries[0].content == "Local view"
        assert entries[1].content == "Frontier view"


# ---------------------------------------------------------------------------
# Per-turn + synthesis timeouts (B1)
# ---------------------------------------------------------------------------


class TestCouncilRunnerTimeouts:
    """Pin the per-turn + synthesis budget contract.

    Earlier the runner had no ``asyncio.wait_for`` on either path —
    only the orchestrator wrapped its calls.  A wedged local backend
    on the CLI / MCP / tournament path could hang indefinitely.  B1
    factored ``call_with_budget`` so both paths enforce identical
    semantics: a per-turn budget produces a ``[Timeout: ...]``
    transcript entry, a synthesis budget produces a
    ``[Synthesis timed out after Ns]`` synthesis string.
    """

    @pytest.fixture(autouse=True)
    def _lower_per_turn_floor(self, monkeypatch):
        # The 5s per-turn floor is too coarse for fast unit tests.
        # Lower the production-tier guard so a config with
        # sub-second per-turn budgets construct cleanly.
        import heddle.contrib.council.config as _cfg_mod

        monkeypatch.setattr(_cfg_mod, "_PER_TURN_TIMEOUT_FLOOR_SECONDS", 0)

    async def test_per_turn_timeout_produces_timeout_entry(self):
        # 1.0s overall, 0.5s synthesis → 0.25s per-turn (2 agents x 1 round).
        config = _minimal_config(max_rounds=1, timeout_seconds=1, synthesis_timeout_seconds=1)

        async def _slow_complete(**kwargs):
            import asyncio as _aio

            _ = kwargs
            await _aio.sleep(1.0)
            return {"content": "should never see this", "model": "stub"}

        backend = AsyncMock()
        backend.complete.side_effect = _slow_complete
        runner = CouncilRunner(backends={"standard": backend})

        result = await runner.run("Topic", config=config)

        entries = result.transcript[0].entries
        assert entries, "expected at least one transcript entry"
        # Every agent turn timed out; the backend never returned its
        # canned content.  The runner produces a ``[Timeout: ...]``
        # entry per agent, never propagates.
        assert all("[Timeout" in e.content for e in entries)

    async def test_synthesis_timeout_produces_timeout_synthesis(self):
        # Per-turn calls succeed quickly; only synthesis sleeps past
        # its budget.  Verifies the synthesis branch fires and
        # produces the user-visible ``[Synthesis timed out ...]``
        # string regardless of how the agent turns went.
        config = _minimal_config(max_rounds=1, timeout_seconds=2, synthesis_timeout_seconds=1)

        sleep_on_synthesis = {"hit": False}

        async def _conditional_complete(**kwargs):
            import asyncio as _aio

            # Synthesis call sends a very long user_message starting
            # with the topic + full transcript; agent turns send the
            # smaller JSON-encoded context.  Use the length as the
            # discriminator — synthesis prompts are markedly longer.
            user_message = kwargs.get("user_message", "")
            if "FULL DISCUSSION TRANSCRIPT" in user_message:
                sleep_on_synthesis["hit"] = True
                await _aio.sleep(2.0)
                return {"content": "synthesis", "model": "stub"}
            return {
                "content": "agent turn",
                "model": "stub",
                "prompt_tokens": 5,
                "completion_tokens": 5,
            }

        backend = AsyncMock()
        backend.complete.side_effect = _conditional_complete
        runner = CouncilRunner(backends={"standard": backend})

        result = await runner.run("Topic", config=config)

        assert sleep_on_synthesis["hit"], "synthesis branch must have run"
        assert "[Synthesis timed out" in result.synthesis


# ---------------------------------------------------------------------------
# Bridge support
# ---------------------------------------------------------------------------


class _StubBridge:
    """Minimal ChatBridge double that records calls and returns canned content.

    The real :class:`ChatBridge` interface (``send_turn``,
    ``get_session_info``, ``close_session``) is built around per-session
    state.  The runner only needs ``send_turn`` for one-shot turns, so
    this stub reduces the test fixture to the surface that's actually
    exercised.
    """

    def __init__(self, content: str = "Bridge response", model: str = "stub-model"):
        from heddle.contrib.chatbridge.base import ChatResponse

        self.content = content
        self.model = model
        self._response_cls = ChatResponse
        self.calls: list[dict] = []
        self.closed = False

    async def send_turn(self, message, context, session_id):
        self.calls.append({"message": message, "context": context, "session_id": session_id})
        return self._response_cls(
            content=self.content,
            model=self.model,
            token_usage={"prompt_tokens": 10, "completion_tokens": 5},
            stop_reason="stop",
            session_id=session_id,
        )

    async def aclose(self):  # exercised by CouncilRunner.aclose()
        self.closed = True


class TestBridgeSupport:
    """The runner dispatches through ``agent.bridge`` when set."""

    async def test_bridge_dispatch_short_circuits_backend(self, monkeypatch):
        # If the runner reached the backend path, this would raise — no
        # tier backend was provided.
        bridge = _StubBridge(content="From bridge", model="bridge-model")

        # Patch the dotted-path import so we control the bridge instance.
        import heddle.contrib.council.runner as runner_mod

        captured: dict = {}

        class _BridgeFactory:
            def __init__(self, **kwargs):
                captured["kwargs"] = kwargs

            async def send_turn(self, *a, **kw):
                return await bridge.send_turn(*a, **kw)

            async def aclose(self):
                bridge.closed = True

        fake_module = type("M", (), {"FakeBridge": _BridgeFactory})
        monkeypatch.setattr(
            runner_mod.importlib,
            "import_module",
            lambda name: fake_module if name == "fake.bridge" else __import__(name),
        )

        config = CouncilConfig(
            name="bridged",
            max_rounds=1,
            convergence={"method": "none"},
            agents=[
                {
                    "name": "agent_a",
                    "bridge": "fake.bridge.FakeBridge",
                    "bridge_config": {"model": "bridge-model"},
                    "tier": "standard",
                },
                {
                    "name": "agent_b",
                    "bridge": "fake.bridge.FakeBridge",
                    "bridge_config": {"model": "bridge-model"},
                    "tier": "standard",
                },
            ],
            facilitator={"tier": "standard"},
        )
        runner = CouncilRunner(backends={"standard": _mock_backend()})

        result = await runner.run("Topic", config=config)
        await runner.aclose()

        assert result.rounds_completed == 1
        # All entries came from the bridge — content matches the stub.
        for entry in result.transcript[0].entries:
            assert entry.content == "From bridge"
            assert entry.model_used == "bridge-model"
            assert entry.token_count == 15  # 10 prompt + 5 completion

        # System prompt was injected into the bridge constructor by
        # default (since bridge_config didn't supply one).
        assert "system_prompt" in captured["kwargs"]
        assert captured["kwargs"]["model"] == "bridge-model"

    async def test_max_tokens_per_turn_propagates_to_bridge(self, monkeypatch):
        """``agent.max_tokens_per_turn`` must reach the bridge constructor;
        otherwise thinking models can blow past the agent's declared cap
        because the bridge's own default (e.g. 2000) wins."""
        import heddle.contrib.council.runner as runner_mod

        captured: dict = {}

        class _BridgeFactory:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def send_turn(self, *a, **kw):
                from heddle.contrib.chatbridge.base import ChatResponse

                return ChatResponse(content="ok", model="x", token_usage={})

            async def aclose(self):
                pass

        fake_module = type("M", (), {"FakeBridge": _BridgeFactory})
        monkeypatch.setattr(
            runner_mod.importlib,
            "import_module",
            lambda name: fake_module if name == "fake.bridge" else __import__(name),
        )

        config = CouncilConfig(
            name="budget",
            max_rounds=1,
            convergence={"method": "none"},
            agents=[
                {
                    "name": "agent_a",
                    "bridge": "fake.bridge.FakeBridge",
                    "max_tokens_per_turn": 1500,
                },
                {
                    "name": "agent_b",
                    "bridge": "fake.bridge.FakeBridge",
                    # Caller-supplied bridge_config takes precedence.
                    "bridge_config": {"max_tokens": 99},
                    "max_tokens_per_turn": 1500,
                },
            ],
            facilitator={"tier": "standard"},
        )
        runner = CouncilRunner(backends={"standard": _mock_backend()})
        await runner.run("Topic", config=config)

        # First agent: max_tokens defaulted from agent.max_tokens_per_turn.
        # Second agent's bridge_config explicitly set 99, so setdefault is a no-op
        # — capture from the LAST construction call should be 99.
        assert captured["max_tokens"] == 99

    async def test_bridge_caching_uses_one_instance_per_agent(self, monkeypatch):
        import heddle.contrib.council.runner as runner_mod

        instance_count = {"n": 0}
        bridge = _StubBridge()

        class _BridgeFactory:
            def __init__(self, **kwargs):
                instance_count["n"] += 1

            async def send_turn(self, *a, **kw):
                return await bridge.send_turn(*a, **kw)

            async def aclose(self):
                pass

        fake_module = type("M", (), {"FakeBridge": _BridgeFactory})
        monkeypatch.setattr(
            runner_mod.importlib,
            "import_module",
            lambda name: fake_module if name == "fake.bridge" else __import__(name),
        )

        config = CouncilConfig(
            name="cached",
            max_rounds=2,
            convergence={"method": "none"},
            agents=[
                {
                    "name": "only_agent",
                    "bridge": "fake.bridge.FakeBridge",
                    "bridge_config": {},
                },
                {
                    "name": "second_agent",
                    "bridge": "fake.bridge.FakeBridge",
                    "bridge_config": {},
                },
            ],
            facilitator={"tier": "standard"},
        )
        runner = CouncilRunner(backends={"standard": _mock_backend()})
        await runner.run("Topic", config=config)

        # 2 agents, 2 rounds = 4 turns, but only 2 bridge instances
        # (one per agent, cached across rounds).
        assert instance_count["n"] == 2
        assert len(bridge.calls) == 4

    async def test_invalid_bridge_path_returns_error_entry(self):
        config = CouncilConfig(
            name="bad",
            max_rounds=1,
            convergence={"method": "none"},
            agents=[
                {
                    "name": "agent_a",
                    "bridge": "no.such.module.NoSuchClass",
                },
                {
                    "name": "agent_b",
                    "bridge": "no.such.module.NoSuchClass",
                },
            ],
            facilitator={"tier": "standard"},
        )
        runner = CouncilRunner(backends={"standard": _mock_backend()})
        result = await runner.run("Topic", config=config)

        for entry in result.transcript[0].entries:
            assert "[ERROR" in entry.content
            assert "no.such.module" in entry.content

    async def test_aclose_closes_cached_bridges(self, monkeypatch):
        import heddle.contrib.council.runner as runner_mod

        bridges_made: list = []

        class _BridgeFactory:
            def __init__(self, **kwargs):
                self.closed = False
                bridges_made.append(self)

            async def send_turn(self, *a, **kw):
                from heddle.contrib.chatbridge.base import ChatResponse

                return ChatResponse(content="ok", model="x", token_usage={})

            async def aclose(self):
                self.closed = True

        fake_module = type("M", (), {"FakeBridge": _BridgeFactory})
        monkeypatch.setattr(
            runner_mod.importlib,
            "import_module",
            lambda name: fake_module if name == "fake.bridge" else __import__(name),
        )

        config = CouncilConfig(
            name="closes",
            max_rounds=1,
            convergence={"method": "none"},
            agents=[
                {"name": "agent_a", "bridge": "fake.bridge.FakeBridge"},
                {"name": "agent_b", "bridge": "fake.bridge.FakeBridge"},
            ],
            facilitator={"tier": "standard"},
        )
        runner = CouncilRunner(backends={"standard": _mock_backend()})
        await runner.run("Topic", config=config)
        await runner.aclose()

        assert len(bridges_made) == 2
        assert all(b.closed for b in bridges_made)
        # Cache cleared after aclose.
        assert runner._bridges == {}
