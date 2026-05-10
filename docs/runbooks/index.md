# Operator Runbooks

Incident-style runbooks for the most common failure modes operators
hit while running Heddle in production. Each follows the same shape:

- **Symptom** — what an operator actually sees.
- **Diagnosis** — concrete steps to localise the cause (log greps,
  CLI commands, code references).
- **Mitigation** — what to do once you know.
- **Verify** — how to confirm the system is healthy again.
- **Followup** — root-cause work to prevent recurrence.

These complement, rather than replace, the
[Design Invariants](../DESIGN_INVARIANTS.md) (the *why* behind the
constraints) and the [Troubleshooting](../TROUBLESHOOTING.md) page
(quick-fix recipes for developer-facing setup issues). The runbooks
here assume a deployed system and an operator under time pressure.

## Catalogue

| Runbook | When to reach for it |
| ------- | -------------------- |
| [Debug missing worker results](debug-missing-results.md) | Caller times out but a worker logs success |
| [Interpret dead letters](interpret-dead-letters.md) | `heddle.tasks.dead_letter` is filling up |
| [Verify NATS connectivity](verify-nats-connectivity.md) | Actor startup is failing or flaky |
| [Deploy Workshop safely](deploy-workshop-safely.md) | Standing up Workshop outside loopback |
| [Recover from stuck eval runs](recover-stuck-eval-runs.md) | Eval Runner row sits at `running` forever |
| [Recover from a stuck orchestrator goal](recover-stuck-orchestrator-goal.md) | Goal never publishes a final result |

## Conventions

Log keys quoted in these runbooks are exact `structlog` event names.
Greppable with no escaping: `rg dispatch.parse_error` lands directly
on the call site that emits them. Where a runbook references a fix
commit (e.g. `b453298`), the commit message carries the full context
of the bug and the test pin that prevents regression.
