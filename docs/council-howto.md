# Multi-Agent Deliberation with Councils

Councils let multiple AI agents discuss a topic iteratively, each with their
own context, role, and perspective. Instead of one LLM processing everything,
a council runs structured multi-round debates where agents react to each
other's positions until they converge or hit a round limit.

## When to use a council vs. a pipeline

| Use case | Approach |
|----------|----------|
| Steps A, B, C run in order, each feeding the next | **Pipeline** |
| Multiple experts each weigh in, then a facilitator synthesizes | **Council** |
| Blind review where reviewers shouldn't see each other's work | Pipeline with **parallel stages** |
| Iterative refinement through debate and rebuttal | **Council** |
| Fixed, repeatable data transformation | **Pipeline** |
| Open-ended analysis where the best answer emerges from discussion | **Council** |

Councils are designed for tasks where **the quality of the answer improves
through iteration** — architecture reviews, policy analysis, risk assessment,
adversarial stress-testing, consensus-building.

---

## Quick start

### 1. Define agents in a council config

```yaml
# configs/councils/architecture_review.yaml
name: "architecture_review"
protocol: "round_robin"
max_rounds: 3
timeout_seconds: 300
# Budget reserved for the facilitator's synthesis call.  Subtracted
# from timeout_seconds to compute the per-turn budget:
#   per_turn = (timeout_seconds - synthesis_timeout_seconds) / (max_rounds * len(agents))
# The implied per-turn budget must be >= 5s or the config is rejected
# at load time.  Default: 60s.
synthesis_timeout_seconds: 60

convergence:
  method: "llm_judge"
  threshold: 0.8
  backend_tier: "standard"

agents:
  - name: "architect"
    worker_type: "reviewer"
    tier: "standard"
    role: "Senior software architect — focus on system design and scalability"
    sees_transcript_from: ["all"]

  - name: "security_expert"
    worker_type: "reviewer"
    tier: "standard"
    role: "Security specialist — focus on threat modeling and attack surface"
    sees_transcript_from: ["all"]

  - name: "critic"
    worker_type: "reviewer"
    tier: "frontier"
    role: "Devil's advocate — stress-test all proposals, find weaknesses"
    sees_transcript_from: ["architect", "security_expert"]

facilitator:
  tier: "standard"
  synthesis_prompt: |
    Synthesize the team's discussion into recommendations.
    Highlight agreements, unresolved tensions, and action items.
  convergence_prompt: |
    Rate agreement among participants from 0.0 to 1.0.
    Respond with JSON: {"score": 0.X, "reason": "..."}
```

### 2. Run without infrastructure (CouncilRunner)

```python
from heddle.worker.backends import build_backends_from_env
from heddle.contrib.council.config import load_council_config
from heddle.contrib.council.runner import CouncilRunner

config = load_council_config("configs/councils/architecture_review.yaml")
runner = CouncilRunner(build_backends_from_env())

result = await runner.run("Should we migrate to microservices?", config=config)

print(result.synthesis)
print(f"Rounds: {result.rounds_completed}, Converged: {result.converged}")
```

### 3. Run via MCP tools

Add to your MCP gateway config:

```yaml
tools:
  council:
    configs_dir: "configs/councils"
    enable: [start, status, transcript, intervene, stop]
```

Then from Claude Desktop or any MCP client:

- `council.start` — start a discussion
- `council.status` — check progress
- `council.transcript` — read the full discussion
- `council.intervene` — inject a human message mid-discussion
- `council.stop` — stop early and synthesize

---

## Core concepts

### Agents

Each agent has:

- **name** — unique identifier within the council
- **worker_type** — which Heddle worker config to use (or `bridge` for external LLMs)
- **tier** — which model to use (`local`, `standard`, `frontier`)
- **role** — system-prompt-level instructions defining the agent's perspective
- **sees_transcript_from** — visibility filter (which other agents' contributions this agent can see)
- **max_tokens_per_turn** — token budget per response

### Protocols

Protocols define **who speaks when** and **what they see**:

| Protocol | Behavior |
|----------|----------|
| `round_robin` | All agents speak every round in config order |
| `structured_debate` | Phase 1: opening statements. Phase 2+: rebuttals. Final: closing |
| `delphi` | Anonymized positions (agents see "Participant A", not real names). Convergence score fed back each round to reduce anchoring bias |

### Convergence detection

Controls when to stop the discussion:

| Method | How it works |
|--------|-------------|
| `none` | Run all `max_rounds`, never stop early |
| `position_stability` | Compare each agent's position across rounds using text similarity. Stop when average similarity exceeds `threshold` |
| `llm_judge` | Ask an LLM to rate agreement 0-1 after each round. Stop when score exceeds `threshold` |

### Transcript management

The `TranscriptStore` maintains the full discussion history with:

- **Per-agent visibility filtering** — agents only see what their
  `sees_transcript_from` config allows
- **Token-budget truncation** — when the transcript exceeds the budget,
  the oldest entries are dropped (preserving recent context)
- **Convergence scores** attached to each round

### Audience participation

External participants can inject messages into a running council
discussion. Agents see these as a separate `[AUDIENCE REACTIONS]`
block and may choose to engage or ignore them.

**Key components:**

- **`TranscriptEntry.entry_type`** — `"turn"` (default, panelist) or
  `"interjection"` (audience). Backward compatible: existing code
  that omits `entry_type` gets `"turn"`.
- **`TranscriptStore.inject_interjection(agent_name, content, role)`** —
  add an audience contribution to the current round. Thread-safe.
- **`CouncilRunner.inject(agent_name, content, role)`** — inject a
  spectator interjection while `run()` is executing. Safe to call
  from another thread or coroutine.
- **MCP `council.intervene` action** — set `as_spectator: true` to
  tag the message as an interjection instead of a panelist turn.

**Example — interactive Town Hall Debate:**

```bash
python examples/town-hall/run.py \
    configs/councils/town_hall_debate.yaml \
    --topic "Remote work is better than office work" \
    --interactive
```

Type messages while the debate runs; your input appears in the next
agent's context under `[AUDIENCE REACTIONS]`.

### Post-hoc scoring

Once a council finishes, two scorers can grade the result. Both
implement the `Scorer` ABC; both return a `ScoringResult`.

| Scorer | Use case |
|--------|---------|
| `JudgePanelScorer` | Adversarial / two-side debates. Each judge picks one winner; the panel aggregates by majority vote (ties → draws). |
| `RubricScorer` | Independent per-participant evaluation (Q&A panels, blind taste tests). Judges grade *every* participant on *every* rubric dimension. Anonymizes the transcript at score time so judges grade content, not branding; the alias map (`agent_name → "Participant A/B/C"`) lands in `metadata` for the caller's reveal step. |

`TournamentRunner` schedules round-robin matchups across many
models / topics, dispatches each through the configured scorer, and
aggregates a leaderboard plus a head-to-head matchup matrix. Both
the [debate-arena](../examples/debate-arena/) and
[blind-taste-test](../examples/blind-taste-test/) examples are full
working demonstrations.

---

## ChatBridge — external LLM adapters

Not every council participant needs to be a standard Heddle worker.
ChatBridge adapters let you bring in external LLM providers or human
participants as full council members.

`CouncilRunner` dispatches each agent through its configured bridge
when `agent.bridge` is set (cached per agent name across rounds, so
session history is preserved). When `agent.bridge` is unset, the
runner falls back to the tier-based `LLMBackend` path. Mixing both
in one council is fine — agents are routed independently.

### Available adapters

| Adapter | Provider | Key feature |
|---------|----------|-------------|
| `AnthropicChatBridge` | Claude API | Session-aware, messages accumulate |
| `OpenAIChatBridge` | OpenAI / ChatGPT | GPT-4o, GPT-4, etc.; rescues thinking-model output from `reasoning_content` |
| `OllamaChatBridge` | Ollama (local) | Local models with conversation history |
| `LMStudioChatBridge` | LM Studio | OpenAI-compatible `/v1` server (MLX or llama.cpp); subclass of `OpenAIChatBridge` |
| `ManualChatBridge` | Human | Callback or queue-based, with timeout |

### Using a ChatBridge agent in a council

```yaml
agents:
  - name: "gpt_perspective"
    bridge: "heddle.contrib.chatbridge.openai.OpenAIChatBridge"
    bridge_config:
      model: "gpt-4o"
      api_key_env: "OPENAI_API_KEY"
    tier: "standard"  # ignored when ``bridge`` is set
    role: "External perspective — challenge assumptions from a different model's viewpoint"
    max_tokens_per_turn: 1500   # propagated to the bridge unless bridge_config overrides
```

For programmatic construction, `chatbridge_spec(model_name)` from
`heddle.contrib.chatbridge.discover` returns `(dotted_path, kwargs)`
ready to drop into an `AgentConfig` — the [debate-arena](../examples/debate-arena/)
and [blind-taste-test](../examples/blind-taste-test/) examples use it
to wire one bridge per debater from a CLI `--models` list.

### Human-in-the-loop

```python
from heddle.contrib.chatbridge.manual import ManualChatBridge

async def ask_human(message, context, session_id):
    print(f"\n--- Council asks you ({session_id}) ---")
    print(message[:500])
    return input("Your response: ")

bridge = ManualChatBridge(on_prompt=ask_human, timeout_seconds=300)
```

### ChatBridge as a standard Heddle worker

Any ChatBridge can be wrapped as a `ProcessingBackend` for use in
regular pipelines (not just councils):

```yaml
name: "gpt4_processor"
processing_backend: "heddle.contrib.chatbridge.worker.ChatBridgeBackend"
processing_config:
  bridge_class: "heddle.contrib.chatbridge.openai.OpenAIChatBridge"
  model: "gpt-4o"
  api_key_env: "OPENAI_API_KEY"
```

---

## Design patterns

### Pattern 1: Architecture review council

Three agents with different expertise, full visibility, critic runs on
a stronger model:

```yaml
agents:
  - name: "architect"
    worker_type: "reviewer"
    tier: "standard"
    role: "System design and scalability"
    sees_transcript_from: ["all"]
  - name: "security"
    worker_type: "reviewer"
    tier: "standard"
    role: "Security and threat modeling"
    sees_transcript_from: ["all"]
  - name: "critic"
    worker_type: "reviewer"
    tier: "frontier"
    role: "Find weaknesses in every proposal"
    sees_transcript_from: ["architect", "security"]
```

### Pattern 2: Delphi consensus

Anonymous positions to reduce anchoring bias, with convergence feedback:

```yaml
protocol: "delphi"
convergence:
  method: "llm_judge"
  threshold: 0.85
agents:
  - name: "expert_a"
    worker_type: "analyst"
    tier: "standard"
    role: "Domain expert A"
    sees_transcript_from: ["all"]
  - name: "expert_b"
    worker_type: "analyst"
    tier: "standard"
    role: "Domain expert B"
    sees_transcript_from: ["all"]
```

Agents see "Participant A", "Participant B" instead of real names.

### Pattern 3: Mixed-vendor deliberation

Use different LLM providers for diversity of perspective:

```yaml
agents:
  - name: "claude_analyst"
    worker_type: "analyst"
    tier: "standard"
    role: "Analytical perspective (Claude)"
  - name: "gpt_analyst"
    bridge: "heddle.contrib.chatbridge.openai.OpenAIChatBridge"
    bridge_config:
      model: "gpt-4o"
      api_key_env: "OPENAI_API_KEY"
    role: "Alternative perspective (GPT-4)"
  - name: "local_analyst"
    bridge: "heddle.contrib.chatbridge.ollama.OllamaChatBridge"
    bridge_config:
      model: "llama3.2:3b"
    role: "Efficiency-focused perspective (local model)"
```

---

## Installation

```bash
pip install heddle-ai[council]              # Council framework (no new deps)
pip install heddle-ai[chatbridge]           # ChatBridge adapters (adds openai)
pip install heddle-ai[council,chatbridge]   # Both
```

Or from source:

```bash
uv sync --extra council --extra chatbridge
```

---

## API reference

See the [Contrib API reference](api/contrib.md) for class-level documentation
of `CouncilRunner`, `CouncilConfig`, `ChatBridge`, and all adapter classes.
