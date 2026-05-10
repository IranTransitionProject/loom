# Heddle Blind Taste Test

Multiple LLMs answer the same question independently. A panel of
out-of-family judges grades every response on every rubric dimension —
but the transcript they see has names replaced with `Participant A` /
`B` / `C`. After all prompts run, the script maps the anonymous labels
back to model identities and reveals the winner.

This is the post-hoc-judging cousin of [debate-arena](../debate-arena/):
the models do not compete with each other directly — they each answer
the same prompt independently and are graded on raw answer quality.

## What this honestly does (and doesn't do)

**Does:**

- Dispatches each model through its own [`ChatBridge`](../../src/heddle/contrib/chatbridge/),
  so `claude-sonnet-4-20250514`, `gpt-4o`, and `lmstudio/google/gemma-3-4b`
  produce genuinely different answers — there is no "label-only" mode.
- Anonymizes the transcript at score time via
  [`RubricScorer`](../../src/heddle/contrib/council/scorer.py)
  before sending it to the judge panel, so each judge grades content
  rather than a brand it might recognize.
- Aggregates per-dimension scores per model across many prompts and
  reveals identities only at the end.

**Does not:**

- Use Heddle's `delphi` protocol for *peer* anonymization — that
  protocol shines in multi-round consensus building, where agents
  see each others' anonymized positions across rounds. Blind taste
  test is single-shot with empty peer visibility, so we use
  `round_robin` (which is functionally identical here) and put the
  anonymization where it actually matters: judging.

## Why this matters

Most LLM benchmarks have brand bias — evaluators (and even automated
LLM-as-judge pipelines) often know which model produced which output,
and that leaks into the score. Blind judging removes the leak.

The cooking analogy isn't accidental: the wine industry's blind
tasting protocols and the original Pepsi Challenge exist precisely
because people score *labels* differently from *content* when they
can see the labels. Heddle's `RubricScorer` is a small implementation
of the same idea, applied to LLM evaluation.

## Quick start

```bash
# 1. Install
pip install heddle[council,chatbridge]

# 2. Set up backends — at minimum, one for the participants and one
#    for the judges (both can be the same provider; out-of-family is
#    preferred but not required).
export ANTHROPIC_API_KEY=sk-...
export OPENAI_API_KEY=sk-...
# Optional: a local LM Studio model for variety
export LM_STUDIO_URL=http://localhost:1234/v1

# 3. Run a 3-model, 10-prompt taste test
python examples/blind-taste-test/run.py \
    configs/councils/blind_taste_test.yaml \
    --models claude-sonnet-4-20250514,gpt-4o,lmstudio/google/gemma-3-4b \
    --prompts examples/blind-taste-test/prompts.txt \
    --judges claude-opus-4-20250514,gpt-4o-mini \
    --output blind_taste_results.json
```

Single ad-hoc prompt:

```bash
python examples/blind-taste-test/run.py \
    configs/councils/blind_taste_test.yaml \
    --models claude-sonnet-4-20250514,gpt-4o \
    --prompts "Explain SQL injection to a five-year-old" \
    --judges claude-opus-4-20250514
```

## How blind judging works

Each judge is a `ChatBridge` — a session-aware LLM client. For each
prompt, `RubricScorer`:

1. Builds an alias map (`Participant A` → `models[0]`, `B` → `models[1]`,
   …) keyed by **slot order**, not alphabetical agent name, so the
   same model always sits in the same alias across prompts.
2. Renders the transcript with names replaced by aliases.
3. Sends the anonymized transcript to every judge in parallel via
   `asyncio.gather`.
4. Parses each judge's JSON `scores` block — `{alias: {dim: 0..1}}` —
   and averages per-(alias, dimension) across the panel.

Each judge sees a prompt like this (verbatim from the default):

```text
You are blindly evaluating anonymous responses to a question.
You do NOT know which AI model produced each response.
Judge on quality of the answer, not on style or branding.

QUESTION: {topic}

PARTICIPANTS: Participant A, Participant B, Participant C

RESPONSES:
[PARTICIPANT A]
...
[PARTICIPANT B]
...

Score EVERY participant on EVERY rubric dimension (0.0 to 1.0):
accuracy, depth, clarity, creativity, conciseness

Return JSON ONLY ...
```

The default rubric covers `accuracy`, `depth`, `clarity`, `creativity`,
and `conciseness`. Override with `--rubric correctness,concision,...`.

## Sample terminal output

The reveal phase is designed to look clean in screenshots:

```text
================================================================
  THE REVEAL
================================================================

  Participant A  →  claude-sonnet-4-20250514  ★ WINNER
  Participant B  →  gpt-4o
  Participant C  →  lmstudio/google/gemma-3-4b

  Final leaderboard

  #1  claude-sonnet-4-20250514         0.871  ████████████████░░░░ ★
  #2  gpt-4o                           0.804  ███████████████░░░░░
  #3  lmstudio/google/gemma-3-4b       0.692  █████████████░░░░░░░
```

## What this showcases

- **Real multi-vendor variety** — every participant runs through its
  own `ChatBridge` (extended in `CouncilRunner` to honour
  `agent.bridge`), so model differences are genuine.
- **`RubricScorer`** — independent per-participant per-dimension
  scoring with judge-side anonymization. Subclass of `Scorer`; lives
  alongside `JudgePanelScorer` in the same module.
- **Stable alias mapping across prompts** — the same model always
  sits in the same alias slot, so per-dimension trends across
  prompts are interpretable.
- **Honest framing** — the example's nominal claim is *blind LLM
  evaluation*, and that is exactly what the code does. Multi-round
  Delphi (peer-anonymized consensus building) is a separate
  concern; this example does not pretend otherwise.

## Customizing

**Add or change prompts.** Edit
[`prompts.txt`](prompts.txt) (one prompt per line, `#` for
comments) or pass `--prompts "your single inline prompt"`.

**Custom rubric.** Pass `--rubric correctness,concision,empathy`.
The default judge prompt expands `{rubric_fields}` and asks the
judge to score *every* participant on *every* dimension.

**Custom scoring prompt.** Construct `RubricScorer` directly with
`scoring_prompt=` and pass to your own `run_one_prompt`-style loop.
The template must include `{transcript}`, `{topic}`,
`{participants}`, and `{rubric_fields}` placeholders.

**More than 3 participants.** Add agent slots to
[`configs/councils/blind_taste_test.yaml`](../../configs/councils/blind_taste_test.yaml)
matching the count you want; `--models` then accepts that many
identifiers. The script trims to the supplied model count.

**No reveal.** Pass `--no-reveal` to print only the anonymous
rankings — handy when running a tournament that will be revealed
manually later.

## Tested by

`tests/test_examples_smoke.py` validates the council config this
example loads (`configs/councils/blind_taste_test.yaml`) and asserts
`run.py` imports cleanly on every CI run. End-to-end behaviour
requires live LLM backends and is exercised manually.
