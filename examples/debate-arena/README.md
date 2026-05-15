# Heddle Debate Arena

Run LLM models against each other in structured debates, score them
with a panel of out-of-family judges, and produce a leaderboard plus a
head-to-head matchup matrix.

Inspired by [Lech Mazur's debate
benchmark](https://github.com/lechmazur/debate) — but built entirely on
Heddle Council + ChatBridge primitives, in a few hundred lines of code.

## What it does

1. Generates a round-robin schedule: every model debates every other
   model on every topic, both sides (with sides swapped). Three
   models and two topics produces 12 matchups.
2. For each matchup, runs a structured debate via Heddle's Council
   framework: PRO and CON take turns over `max_rounds` rounds, then a
   facilitator produces a balanced summary.
3. Sends the full transcript to a panel of judge LLMs (each a
   `ChatBridge`). Judges return JSON with the winner, a 0..1 margin,
   and rubric scores.
4. Aggregates judge verdicts by **majority vote** — a 2-2 or 1-1-1
   split is recorded as a draw, not broken by margin.
5. Builds a leaderboard sorted by win rate (then average margin) and
   a head-to-head matchup matrix (`wins-losses-draws` per cell).

## Quick start

```bash
# 1. Install
pip install heddle-ai[council,chatbridge]

# 2. Set up backends — at minimum, one for the debaters and judges
export ANTHROPIC_API_KEY=sk-...
# optional — for local debaters via Ollama
export OLLAMA_URL=http://localhost:11434

# 3. Run a 3-model, 2-topic tournament
python examples/debate-arena/run.py \
    configs/councils/debate_arena.yaml \
    --models claude-sonnet-4-20250514,claude-opus-4-20250514,claude-haiku-4-5-20251001 \
    --topics examples/debate-arena/topics.txt \
    --judges gpt-4o,gpt-4-turbo \
    --output debate_results.json
```

## How scoring works

Each judge is a separate `ChatBridge` — a session-aware LLM client.
The scorer formats the full debate transcript (turns and any audience
interjections), sends it to all judges in parallel via
`asyncio.gather`, parses each JSON verdict, and aggregates by majority
vote. Tied verdicts are recorded as draws.

The default rubric covers:

- `argument_quality` — substance and logical structure
- `rebuttal_strength` — how directly the debater engages with
  opposing claims
- `evidence_use` — concreteness and accuracy of cited support
- `rhetorical_skill` — clarity and persuasiveness
- `responsiveness` — engagement with the previous round

You can replace the rubric and the prompt:

```python
from heddle.contrib.council import JudgePanelScorer

scorer = JudgePanelScorer(
    judges=[bridge_a, bridge_b, bridge_c],
    rubric_fields=["correctness", "concision"],
    scoring_prompt=my_custom_template,  # must include {transcript}, {topic}, {agents}, {rubric_fields}
)
```

## Sample leaderboard output

```text
==================================================================
  LEADERBOARD
==================================================================

  rank model                       W   L   D  win_rate  avg_margin
  ----------------------------------------------------------------
  1    claude-opus-4-20250514      6   2   0     0.750       0.612
  2    claude-sonnet-4-20250514    4   3   1     0.500       0.483
  3    claude-haiku-4-5-20251001   2   7   1     0.200       0.395

==================================================================
  MATCHUP MATRIX (rows = model, cells = wins-losses-draws)
==================================================================

                claude-opus-  claude-sonne  claude-haik
  claude-opus-           —          3-1-0        3-1-0
  claude-sonne        1-3-0             —        3-2-1
  claude-haik         1-3-0         2-3-1            —
```

## What this showcases

- **Council** — multi-round structured debate via a built-in
  `structured_debate` protocol
- **Real per-debater ChatBridges** — every debater is dispatched
  through its own session-aware ChatBridge (Anthropic, OpenAI, LM
  Studio, Ollama) routed by model name via
  `heddle.contrib.chatbridge.chatbridge_spec`. Model differences are
  genuine, not label-only.
- **Scorer** — `JudgePanelScorer` aggregates panel verdicts; bring
  your own scorer by subclassing `Scorer`
- **Tournament** — `TournamentRunner` schedules round-robin matchups
  with concurrent execution
- **Mixed-vendor judging** — judges should come from a different
  family than debaters to reduce family-bias (Mazur's anti-bias rule)

## Customizing

**Add or change topics.** Edit `examples/debate-arena/topics.txt` or
pass `--topics "topic 1, topic 2"` directly on the command line.

**Same-provider mode.** Both debaters can be from the same provider —
e.g. `claude-sonnet-4-20250514,claude-haiku-4-5-20251001` — and
they will still produce genuinely different responses because each
runs through its own ChatBridge with its own model id. The honest
inverse of the old "label-only" mode.

**Custom debater factory.** The default factory wires each debater
to a ChatBridge picked from the model name. Replace it for
persona-differentiated debates or per-topic prompt customization:

```python
from heddle.contrib.chatbridge.discover import chatbridge_spec

def my_factory(model_key: str, role: str, topic: str) -> AgentConfig:
    bridge_path, bridge_kwargs = chatbridge_spec(model_key)
    return AgentConfig(
        name=f"debater_{model_key}",
        bridge=bridge_path,
        bridge_config=bridge_kwargs,
        tier=ModelTier.STANDARD,  # ignored when bridge is set
        role=f"You are {model_key}, specializing in {topic_field(topic)}. {role}",
        max_tokens_per_turn=1500,
    )
```

**Concurrency.** `--concurrency 4` runs four matchups in parallel.
API rate limits typically bite first; start at 1 and increase only if
you have headroom.

**Custom rubric.** Pass `rubric_fields=[...]` and a matching
`scoring_prompt=` template to `JudgePanelScorer`. Your prompt must
include `{transcript}`, `{topic}`, `{agents}`, and `{rubric_fields}`
placeholders.

## Tested by

`tests/test_examples_smoke.py` validates the council config this
example loads (`configs/councils/debate_arena.yaml`) and asserts
`run.py` imports cleanly on every CI run. End-to-end behaviour
requires live LLM backends and is exercised manually.
