# ADR-010: Condition-evaluation defaults — fail-closed by default, env-gated legacy

**Status:** Accepted.
**Pairs with:** [Invariant 10](../DESIGN_INVARIANTS.md#10-condition-evaluation-malformed--false-skip-missing-path--false-skip).
**Source commit:** `4ee9314` (2026-05-11, G7 — fail-closed flip).

## Context

`PipelineOrchestrator._evaluate_condition` (in
`src/heddle/orchestrator/pipeline.py:711-762`) parses simple
three-token condition strings (e.g.
`extract.output.needs_ocr == true`) and returns a boolean that
gates whether the corresponding pipeline stage runs.

Three failure modes need defaults:

1. **Malformed condition** — the string doesn't split into exactly
   three tokens. A typo: `extract.output.needs_ocr==true` (no
   spaces), `if extract.output.needs_ocr then run`, or a deleted
   token mid-edit.
2. **Missing path** — the path resolves through `context` and
   finds nothing. The expected case for conditional stages whose
   upstream didn't produce the optional field.
3. **Unknown operator** — the parse succeeded but the operator
   isn't `==` or `!=`. Could be a typo (`=`, `<>`), could be a
   future operator the framework doesn't support yet.

The original shape (pre-G7) defaulted malformed → TRUE (fail-open,
run the stage) and missing path → FALSE (fail-closed, skip). The
reasoning at the time was that a structural typo shouldn't silently
drop work, while a missing field is the expected outcome for an
optional dependency.

G7's review of the production-pipeline drift found the opposite
problem: a YAML typo could broaden the pipeline in ways the author
never intended. A condition string with a single missing space
(`extract.output.needs_ocr==true`) ran a stage that the author had
written intending to gate it. The fail-open default surfaced as
silent over-execution.

The decision was which default better matches operator expectations
under realistic conditions.

## Decision

**Malformed conditions and unknown operators both fail-closed
(skip the stage) by default. Setting `HEDDLE_STRICT_CONDITIONS=0`
restores the legacy fail-open behaviour for one-release migration
of pipelines that depended on the prior shape. Missing paths
continue to fail-closed (skip) — that decision did not change.**

The post-G7 matrix:

| Failure mode | Default (no env) | Legacy (`HEDDLE_STRICT_CONDITIONS=0`) |
| ------------ | ---------------- | ------------------------------------- |
| Malformed condition (wrong token count) | Skip (FALSE) | Run (TRUE) |
| Missing path | Skip (FALSE) | Skip (FALSE) |
| Unknown operator (`<`, `>=`, etc.) | Skip (FALSE) | Run (TRUE) |

All three paths log a warning (`pipeline.invalid_condition`,
`pipeline.condition_missing_path`, `pipeline.unsupported_operator`)
so the operator can grep for them in either mode.

The asymmetry between malformed (now FALSE) and the prior shape
(TRUE) is the load-bearing change; missing path was always FALSE and
remains so.

## Alternatives considered

### Keep fail-open on malformed (rejected, original shape)

Status quo ante: malformed → TRUE so a typo doesn't silently drop a
stage.

- **Rejected because** the production failure mode is the
  opposite of what the original rationale assumed. A typo in a
  condition string is far more likely to be discovered by
  "this stage ran when I didn't want it to" (visible — the stage
  produced output) than by "this stage didn't run" (also visible
  but harder to trace to a condition typo). Fail-closed makes
  both error modes equally surfacable via the log line
  `pipeline.invalid_condition`.
- The fail-open default also conflicts with the missing-path
  default — operators reading the code couldn't see why two
  similar failure modes had opposite signs. Unifying to
  fail-closed restores the principle: any uncertainty skips the
  stage.

### Hard error on malformed (rejected)

Raise `PipelineStageError` and abort the entire pipeline run.

- **Rejected because** a single typed condition shouldn't kill
  the whole pipeline. The stage that owns the condition is the
  one that should fail; downstream stages that don't depend on
  it should still run. The skip-with-warning gives operators the
  same visibility without the blast radius.

### Hard error opt-in via env (rejected)

`HEDDLE_STRICT_CONDITIONS=error` raises; default skips.

- **Rejected because** the third state adds operator surface area
  without a clear "I always want errors" use case. Pipelines run
  by an evaluation harness already collect logs; a hard error
  doesn't give them more information than a log line.

### Apply the asymmetry by operator type (rejected)

Treat unknown operators as "user clearly meant something" (fail
open) and treat malformed strings (whole-condition parse failure)
as "definitely a typo" (fail closed).

- **Rejected because** the operator-typo case is exactly the one
  most likely to broaden the pipeline silently (`extract.output.x =
  true` skipping the equals-equals). Fail-open on unknown
  operators recreates the failure mode G7 set out to fix.
- The unified fail-closed shape is also simpler to document and
  remember.

## Consequences

**Enables:**

- YAML typos in condition strings surface as "stage didn't run"
  plus `pipeline.invalid_condition` in the logs, rather than as
  silent over-execution.
- The default behaviour is consistent across malformed, missing,
  and unknown-operator paths: when in doubt, skip and log.
- Operators upgrading from the pre-G7 shape have a single env
  var (`HEDDLE_STRICT_CONDITIONS=0`) to restore the prior
  behaviour during pipeline migration, rather than facing a
  flag day.

**Costs:**

- The env-var escape hatch is a temporary measure. A future
  release will drop it; pipelines that rely on the legacy
  fail-open behaviour will need to be migrated. The flag's
  existence in the code is itself a doc-debt item.
- Pipelines written before G7 that depended on fail-open
  behaviour for legitimate "operator hasn't finished writing
  this condition yet" use cases will skip stages they used to
  run. The migration cost is real but bounded — a `grep -r
  condition: docs/configs/` enumerates the surface.
