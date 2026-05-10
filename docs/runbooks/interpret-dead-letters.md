# Interpret dead letters

The dead-letter queue (`heddle.tasks.dead_letter`) is the router's
escape hatch for tasks that can't be routed normally. When it fills,
the right question is *why*, not *how to drain it* — the reasons are
distinct and the fixes are different.

## Symptom

- Dead-letter consumer logs are growing: `dead_letter.received` lines
  with a non-empty `reason` field.
- Workshop `/dead-letters` view shows a non-empty table.
- Downstream callers experience missing results (often paired with
  [Debug missing worker results](debug-missing-results.md)).

## Diagnosis

Every dead-letter envelope carries a `reason` field set by the
router. There are three reasons today, each fixable independently:

| Reason prefix | Meaning | Where it comes from |
| ------------- | ------- | ------------------- |
| `invalid_task_message: ...` | Payload failed Pydantic validation as a `TaskMessage` | External sender violated the message contract |
| `unknown_tier: ...` | `model_tier` did not resolve via the router rules | Worker config drift, or sender set a wrong tier |
| `rate_limited: tier 'X' has no available capacity` | The token-bucket rate limiter for tier `X` exhausted | Sustained burst beyond `configs/router_rules.yaml` |

To inspect:

```bash
# CLI consumer (subscribes and logs each arriving entry)
uv run heddle dead-letter monitor --nats-url $NATS_URL

# Or the Workshop UI
# (HEDDLE_WORKSHOP_TOKEN gates auth — see deploy-workshop-safely.md)
open http://localhost:8000/dead-letters
```

Reading the table:

- `task_id` and `worker_type` come from the original payload (or
  `None` if the parse failed before they could be extracted —
  `invalid_task_message`).
- `reason` is the human-readable explanation.
- The full original `data` is preserved in the envelope so a replay
  reconstructs the exact same task.

## Mitigation

### `invalid_task_message`

The sender is wrong, not Heddle. Locate it:

```bash
# Trace where the bad task came from
rg "task_id\":.*<task_id_from_dead_letter>" --type log
```

External integrations (MCP gateway clients, CLI scripts,
third-party publishers) are the usual culprits. Fix the sender;
Heddle's role here is to fail loud, not to coerce malformed input.

### `unknown_tier`

Either the router rules dropped a tier mapping, or the sender set a
non-standard `model_tier`. Compare:

```bash
# What the sender requested
rg "task_id.*<id>" --type log | rg "model_tier"

# What the router knows about
cat configs/router_rules.yaml
```

Standard values are `local`, `standard`, `frontier` — anything else
needs an explicit `tier_overrides` mapping in `router_rules.yaml`.

### `rate_limited`

The tier's token bucket is empty. Three options:

1. **Raise the rate limit** in `configs/router_rules.yaml` and
   reload via `nats pub heddle.control.reload '{}'`.
2. **Add replicas** so each replica's share of the bucket goes
   further (queue groups load-balance, but the rate limiter is
   shared via NATS — confirm by reading `RateLimiter` docstring).
3. **Replay later** when the bucket has refilled. The replay flow
   re-injects the same task into `heddle.tasks.incoming`:

   ```bash
   # Workshop UI
   curl -X POST http://localhost:8000/dead-letters/{index}/replay
   ```

   The router will re-evaluate the task with current rate-limit
   state.

## Verify

After mitigation:

- `dead_letter.received` log lines stop accruing.
- The consumer's `replay_count` matches the entries you intentionally
  re-injected (Workshop view shows this).
- Downstream callers no longer report missing results for the
  affected `task_id`s.

## Followup

A persistently non-empty dead-letter queue is a signal, not a
configuration. If `rate_limited` reasons dominate, the capacity
model is wrong; if `invalid_task_message` dominates, an upstream
integration is broken; if `unknown_tier` dominates, the
`router_rules.yaml` is out of date with shipped worker configs.

Add an alert on `dead_letter.received` rate (e.g. > N/min sustained)
so the queue is observed, not just retained.
