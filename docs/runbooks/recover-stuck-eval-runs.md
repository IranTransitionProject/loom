# Recover from stuck eval runs

The Workshop Eval Runner persists each suite invocation as an
`eval_runs` row with a status column. The row should transition
`running → completed` or `running → failed` on every invocation.
A row pinned at `running` indicates the runner crashed in a way
that escaped the status-update path.

## Symptom

- Workshop `/evals` page shows a run that has been `running` for
  longer than `timeout_seconds * len(cases)`.
- New eval submissions for the same suite collide with the stale
  row (depending on suite-level locking).
- Worker logs show the cases completed; nothing wrote the summary
  back.

## Diagnosis

There are three places a run can get stuck, and each has its own
fingerprint in the log:

1. **A case raised** — should not leak, but if it did, look for
   `eval.case_save_failed`. The `return_exceptions=True` gather
   pattern means one bad case can't cancel its siblings, but a
   bug in the per-case persistence path could leave the case row
   in a partial state. The suite-level row still terminates;
   only the case row is affected.

2. **The suite-level finally bailed** — look for
   `eval.suite_status_write_failed`. Commit history (look at
   `eval_runner.py` git log) shows the try/finally that wraps the
   `status='failed'` update. If the DB write itself failed
   (locked database, disk full, schema drift), the runner logs
   this event and re-raises. The row is still stuck, but you
   have the exception traceback in logs.

3. **The runner process died** — no `eval.suite_completed` or
   `eval.suite_failed`. Look for SIGKILL/OOM in the process
   manager, container restart in K8s, or
   `actor.shutdown_requested` if a graceful shutdown landed
   mid-run.

## Mitigation

### Case 1 — case-level save failure

Inspect the failing case row directly:

```sql
SELECT id, case_name, status, error
FROM eval_cases
WHERE run_id = '<stuck_run_id>';
```

If a case row is `running` while siblings are `completed`/`failed`,
manually update it to `failed` with the error from the log line:

```sql
UPDATE eval_cases
SET status = 'failed',
    error = 'Manual recovery: see eval.case_save_failed at <timestamp>'
WHERE id = '<case_id>';
```

Then update the suite row through the same machinery:

```sql
UPDATE eval_runs
SET status = 'failed',
    completed_at = CURRENT_TIMESTAMP,
    error = 'Manual recovery'
WHERE id = '<stuck_run_id>';
```

### Case 2 — suite-level finally failed to write

The runner code already attempts the failure write inside a
try/except so it doesn't re-mask the original error. If even
that bailed, the DB itself is the problem — fix the DB first:

```bash
# DuckDB locked? Find the holder.
lsof | rg <db-path>

# Disk full?
df -h $(dirname <db-path>)
```

Then run the SQL update above to terminate the stale row.

### Case 3 — process death

There is no in-process recovery. Restart the runner, then run
the suite-row SQL update to terminate the stale row.

If process death is recurring, the run length is likely beyond
what your process manager tolerates (OOM-killed; container
restart policy). Either shrink the suite (fewer cases, smaller
timeout) or move the runner to a more permissive deployment
target.

## Verify

```sql
-- No rows should be 'running' from before the current process started
SELECT id, status, created_at, completed_at
FROM eval_runs
WHERE status = 'running'
ORDER BY created_at DESC;
```

New eval submissions for the same suite should now run without
collision. Re-run a quick suite end-to-end and confirm
`eval.suite_completed` lands.

## Followup

Persistent stuck runs almost always trace back to one of:

- A suite that's too large for the timeout, hitting per-case
  cancellation but leaving the suite-row update non-atomic with
  the case rows.
- A DuckDB file shared across multiple processes — DuckDB's
  single-writer model means a holder elsewhere can starve the
  runner indefinitely.

The framework guarantees the row will terminate **if the process
survives long enough to run the finally block**. Operator effort
should focus on that survival, not on rewriting the finally block.
