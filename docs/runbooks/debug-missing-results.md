# Debug missing worker results

The caller (orchestrator, pipeline, MCP bridge, or CLI) times out
waiting for a result, but the worker logs show the task processed
successfully. This is the most pernicious class of failure in
Heddle's request-reply path because the worker side looks healthy.

## Symptom

- Caller log: `dispatch.timeout`, `result_stream.timeout`, or a
  generic `asyncio.TimeoutError` propagated up.
- Worker log: `worker.task_completed` (or equivalent) with the same
  `task_id`, well within the timeout window.
- Sometimes intermittent — the same payload succeeds on a retry.

## Diagnosis

Walk the request-reply path from worker → caller:

1. **Did the worker publish to the right subject?**

    The worker publishes to `heddle.results.{parent_task_id}` (or
    `heddle.results.default` if no parent). If the worker computed
    the subject from a stale or wrong `parent_task_id`, the caller's
    subscription will not see it.

    ```bash
    # On the worker side
    rg "publishing.*heddle.results\." worker.log
    ```

    Cross-check `parent_task_id` between the dispatched TaskMessage
    and the published TaskResult.

2. **Did the caller's subscription see anything?**

    Look for `result_stream.ignored` (wrong task_id), `result_stream.
    duplicate` (already collected), or `result_stream.parse_error`
    (malformed payload).

    ```bash
    rg "result_stream\." caller.log
    rg "dispatch\." caller.log
    ```

    A `*.parse_error` line means a *matching* result arrived but was
    Pydantic-rejected — and the consumer correctly kept waiting
    (commit `b453298`, Invariant 17 supporting fix). The error
    string in the log identifies which field was malformed.

3. **Did the caller subscribe before publishing?**

    Subscribe-before-publish is
    [Design Invariant 17](../DESIGN_INVARIANTS.md#17-subscribe-before-publish-for-orchestrator--worker-request-reply).
    NATS is at-most-once. If a fast worker published its result
    before the caller's subscription went live, the result was
    delivered to nobody — and the caller times out.

    Both `dispatch_and_wait_for_result` (in
    [`heddle.orchestrator.dispatch`](../api/orchestrator.md)) and
    `ResultStream` (in `heddle.orchestrator.stream`) enforce the
    ordering. **Custom callers that publish directly** are the usual
    place this regresses; check the call site.

4. **Did the caller's timeout fire before the worker started?**

    A late result arriving after the orchestrator's per-task timeout
    is dropped by design (commit `cc49783`). The orchestrator's
    final result will include `metadata.timeout` with `pending_task_ids`,
    `expected_count`, `collected_count`, and `timeout_seconds`:

    ```bash
    # Pull the final result JSON and look at the timeout block
    rg "orchestrator.goal_completed" caller.log
    # then look at the corresponding TaskResult.output.metadata.timeout
    ```

    If `pending_task_ids` lists your `task_id`, the worker simply
    didn't reply in time — not a delivery loss.

## Mitigation

| Diagnosis | Action |
| --------- | ------ |
| `*.parse_error` for a matching task_id | Fix the malformed payload at the worker; the framework already keeps waiting for the next valid one |
| Subscribe-before-publish reversed | Route the call site through `dispatch_and_wait_for_result` or `ResultStream` (do not roll your own) |
| Late result after timeout | Increase `timeout_seconds` on the orchestrator config, or split the goal so individual tasks are cheaper |
| Wrong `parent_task_id` | Audit the dispatch path; the orchestrator wires this in `_dispatch_subtasks` |

If you must replay the task manually:

```bash
# Inspect the dead-letter queue first — the task may already be there
uv run heddle dead-letter monitor --nats-url $NATS_URL
```

See also: [Interpret dead letters](interpret-dead-letters.md).

## Verify

Re-dispatch the same payload and confirm:

- A single `worker.task_completed` line.
- A single `result_stream.collected` line on the caller (or, for
  single-result waits, a returned `TaskResult` not `None`).
- No `*.parse_error` lines.

## Followup

If this fires repeatedly on the same worker, treat the worker output
schema as suspect — the parse-error resilience is a safety net, not a
fix. Add the failing payload as a fixture under
`tests/test_contracts.py` (or the worker-specific test file) and
tighten the worker's output validation so the schema rejects the bad
shape at the worker side instead of at the caller.
