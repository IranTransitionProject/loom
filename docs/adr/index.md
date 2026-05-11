# Architecture Decision Records

The [Design Invariants](../DESIGN_INVARIANTS.md) document captures
*what* is true about Heddle's architecture and *how it fails* when
violated. These ADRs capture the *why* — what alternatives were
considered, what they would have cost, and why we rejected them.

ADRs are written once at decision time and not revised. If a
decision changes, write a new ADR that supersedes the old one
rather than editing the old text. This preserves the historical
record of how the architecture evolved.

## Format

Each ADR follows this skeleton:

- **Status** — Accepted (the default for everything here),
  Superseded by ADR-NNN, or Deprecated.
- **Context** — what problem the decision solves and what
  constraints shape it.
- **Decision** — the chosen approach, stated as a single
  declarative sentence.
- **Alternatives considered** — each option that was on the
  table, with the reason it lost.
- **Consequences** — what this decision enables and what it
  costs.

## Index

| ID | Title | Pairs with |
| -- | ----- | ---------- |
| [ADR-001](001-stateless-workers.md) | Stateless workers, no process-local cache | [Invariant 1](../DESIGN_INVARIANTS.md#1-worker-statelessness-is-enforced-not-optional) |
| [ADR-002](002-deterministic-router.md) | Deterministic router, no LLM in routing path | [Invariant 2](../DESIGN_INVARIANTS.md#2-the-router-is-deterministic--no-llm-in-the-routing-path) |
| [ADR-003](003-shallow-json-schema.md) | Shallow JSON Schema for I/O contracts | [Invariant 5](../DESIGN_INVARIANTS.md#5-json-schema-validation-is-intentionally-shallow) |
| [ADR-004](004-skip-not-crash-on-malformed.md) | Skip-and-log on malformed messages, not crash | [Invariant 8](../DESIGN_INVARIANTS.md#8-malformed-nats-messages-are-skipped-not-crashed) |
| [ADR-005](005-subscribe-before-publish.md) | Subscribe before publish for request-reply | [Invariant 17](../DESIGN_INVARIANTS.md#17-subscribe-before-publish-for-orchestrator--worker-request-reply) |
| [ADR-006](006-tri-state-synthesizer-partition.md) | Tri-state synthesizer partition (succeeded / failed / in_flight) | commit `15a9af4` |
| [ADR-007](007-council-budget-and-per-turn-floor.md) | Council synthesis budget and 5s per-turn floor | [council-howto](../council-howto.md) |
| [ADR-008](008-council-execution-path-unification.md) | Council execution paths share one budget helper | [ADR-007](007-council-budget-and-per-turn-floor.md) |
| [ADR-009](009-per-goal-state-isolation.md) | Per-goal state isolation, lockless concurrency | [Invariant 7](../DESIGN_INVARIANTS.md#7-per-goal-state-isolation-enables-concurrency-without-locks) |
| [ADR-010](010-condition-eval-defaults.md) | Condition-evaluation defaults — fail-closed by default, env-gated legacy | [Invariant 10](../DESIGN_INVARIANTS.md#10-condition-evaluation-malformed--false-skip-missing-path--false-skip) |
| [ADR-011](011-first-completed-vs-gather.md) | Pipeline parallel levels use FIRST_COMPLETED, not gather | [Invariant 16](../DESIGN_INVARIANTS.md#16-pipeline-parallel-levels-use-first_completed-not-gather) |

## When to write a new ADR

Add an ADR when a design decision:

- Was non-obvious — a reasonable person could have picked
  differently.
- Will be revisited — future contributors will ask "why didn't
  we just X?" and need to find a real answer.
- Touches a load-bearing invariant — something that, if
  reversed, would break the framework's value proposition.

Don't write ADRs for matters of style, library choice that
could go either way, or implementation details below the
invariant level. Those belong in code comments or the
[Coding Guide](../CODING_GUIDE.md).
