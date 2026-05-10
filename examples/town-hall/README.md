# Town Hall Debate

An interactive demo of Heddle's Council framework where LLM agents
debate a topic while you participate as an audience member.

## What happens

Three agents — an **Advocate**, an **Opponent**, and a **Moderator** —
conduct a structured debate over multiple rounds. The advocate argues
for the proposition, the opponent argues against it, and the moderator
summarizes each round and poses sharpening questions.

In interactive mode, **you are the audience**. Type a message at any
time and it appears in the agents' context as an "[AUDIENCE REACTIONS]"
block. Agents may choose to engage with your point, incorporate it
into their argument, or ignore it — just like a real town hall.

After the final round, a facilitator synthesizes the debate and
declares which side made the more compelling case.

## Quick start

```bash
# Install heddle with council support
pip install heddle[council]

# Set up your API key
export ANTHROPIC_API_KEY=sk-...

# Watch a debate
python examples/town-hall/run.py \
    configs/councils/town_hall_debate.yaml \
    --topic "AI will replace most knowledge workers within 10 years"

# Join the audience (interactive mode)
python examples/town-hall/run.py \
    configs/councils/town_hall_debate.yaml \
    --topic "Remote work is better than office work" \
    --interactive --name "Skeptical Developer"

# Save the full transcript
python examples/town-hall/run.py \
    configs/councils/town_hall_debate.yaml \
    --topic "Open source beats proprietary software" \
    --interactive --output result.json
```

## Fun topics to try

- "AI will replace most knowledge workers within 10 years"
- "Tabs are better than spaces"
- "Pineapple belongs on pizza"
- "Remote work is better than office work"
- "Social media does more harm than good"
- "The best programming language is Python"
- "College degrees are no longer worth the cost"
- "Self-driving cars should be allowed everywhere"

## What this demonstrates

| Heddle feature | How it's used |
|----------------|---------------|
| **Council framework** | Multi-round deliberation with convergence detection |
| **Structured debate protocol** | Opening statements → rebuttals → closing |
| **Audience interjections** | `entry_type="interjection"` — agents see them but can choose to engage or ignore |
| **LLM-as-judge convergence** | Automatic detection of when positions have stabilized |
| **Mixed model tiers** | Moderator runs on `frontier`, debaters on `standard` |
| **Facilitator synthesis** | Final balanced summary with verdict |

## How audience interjections work

When you type a message in interactive mode, it's injected into the
council's transcript as an interjection (not a panelist turn). The
protocol presents it to agents in a separate block:

```text
[AUDIENCE REACTIONS]
- Skeptical Developer: What about the economic data from Europe?

You may address audience points if relevant, or continue the main discussion.
```

This framing gives agents genuine choice — they can engage with your
point if it's relevant, or continue their line of argument. The
selective engagement is what makes it feel like a real town hall rather
than a forced Q&A.

## Tested by

`tests/test_examples_smoke.py` validates the council config this
example loads (`configs/councils/town_hall_debate.yaml`) and asserts
`run.py` imports cleanly on every CI run. End-to-end behaviour
requires live LLM backends and is exercised manually.
