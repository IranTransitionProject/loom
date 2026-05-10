# Recover from a stuck orchestrator goal

The dynamic `OrchestratorActor` decomposes a goal into subtasks,
collects their results, synthesises a final answer, and publishes a
single `TaskResult` to `heddle.results.{goal_id}`. A stuck goal is
one where the orchestrator received the goal but never published the
final result.

This is distinct from a *slow* goal — slow goals make progress on
subtasks. A stuck goal makes no progress at all, or makes progress
but never finalises.

## Symptom

- Caller waiting on `heddle.results.{goal_id}` times out.
- Orchestrator logs `orchestrator.goal_started` but no
  `orchestrator.goal_completed` for the same `goal_id`.
- (Variant) Orchestrator logs `orchestrator.goal_completed` but the
  final result's `metadata.timeout` block names tasks that should
  have responded.

## Diagnosis

Localise to one of four phases:

1. **Decomposition** — the LLM-backed `GoalDecomposer` is still
   producing subtasks. Look for `orchestrator.decomposition_*`
   events. A frontier model on a hard goal can take a minute or
   more — not stuck, just slow.

2. **Dispatch** — subtasks dispatched but no responses. See
   [Debug missing worker results](debug-missing-results.md) for
   the per-task diagnosis. The orchestrator-level signature is
   `orchestrator.subtask_dispatched` events with no matching
   `result_stream.collected`.

3. **Collection timeout** — some subtasks responded, others didn't,
   and the per-goal timeout fired. The final result is **published**
   in this case (commit `cc49783`), with a `metadata.timeout` block:

    ```json
    {
      "metadata": {
        "timeout": {
          "expected_count": 5,
          "collected_count": 3,
          "timeout_seconds": 60,
          "pending_task_ids": ["task-abc", "task-def"]
        }
      }
    }
    ```

    This is not stuck — this is a goal that completed with partial
    results. The caller should treat `metadata.timeout` as a
    first-class signal.

4. **Shutdown during processing** — orchestrator process was
   SIGTERMed mid-goal. The shutdown grace logic (commits `ca1ffad`
   and `e670569`) drains in-flight handlers within
   `shutdown_grace_seconds` (default 5.0). If the goal didn't
   complete in that window, it was force-cancelled and the
   `goal_id` has no terminal result — by design, NATS is
   at-most-once and there's no goal-state persistence outside the
   checkpoint manager.

## Mitigation

| Diagnosis | Action |
| --------- | ------ |
| Slow decomposition | Wait, or shrink the goal; consider `model_tier=local` if the LLM is the bottleneck |
| Subtasks not responding | Apply [Debug missing worker results](debug-missing-results.md) per `task_id` |
| Collection timeout (partial result published) | Update the caller to handle `metadata.timeout`; if the partial answer is acceptable, no action needed |
| Shutdown force-cancelled | Re-submit the goal; if a checkpoint exists, the rebooted orchestrator will continue from it |

For caller-side handling of timeout metadata:

```python
result = await wait_for_goal(goal_id)
if result.output.get("metadata", {}).get("timeout"):
    pending = result.output["metadata"]["timeout"]["pending_task_ids"]
    # ... handle partial completion ...
```

To prevent shutdown force-cancellation on next deploy, tune
`shutdown_grace_seconds` on the orchestrator config. The default
5s is tight for fast unit tests and loose for a typical LLM call;
raise it to match your slowest expected subtask.

## Verify

After a re-submission:

- `orchestrator.goal_started` then `orchestrator.goal_completed` for
  the new `goal_id`.
- Final result on `heddle.results.{new_goal_id}` with either an empty
  `metadata.timeout` block or no `metadata.timeout` at all (full
  collection).

## Followup

Recurring stuck goals usually point to one of:

- A worker pool that can't keep up with the orchestrator's
  decomposition rate. Add replicas (queue groups load-balance
  automatically) or raise the per-task `timeout_seconds`.
- A subtask producing malformed output that the result stream
  skip-logs (`result_stream.parse_error`) and the orchestrator
  patiently waits past the timeout for. Fix the worker output
  schema rather than the orchestrator timeout.
- Goals whose decomposition produces more subtasks than the
  configured `max_concurrent_tasks`. The orchestrator does not
  fail — it serialises — but the elapsed time will exceed naive
  estimates. Raise the cap or reduce decomposition aggressiveness.

The framework's role is to **always publish a terminal result** for
a goal it accepted (modulo SIGTERM after grace). Operator effort
should focus on getting the per-task path healthy; the orchestrator
itself rarely needs intervention.
