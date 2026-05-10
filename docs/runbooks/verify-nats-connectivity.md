# Verify NATS connectivity

NATS is the only link between Heddle actors. Connectivity is the
first thing to verify when anything seems off — `connection refused`
at startup is much easier to fix than a half-connected mesh hiding
behind a confusing downstream error.

## Symptom

- Actor startup fails with `Cannot connect to NATS at <url>`.
- Actor logs `actor.connected` but no `actor.subscribed`.
- Workers process tasks but no results reach the orchestrator
  (often paired with [Debug missing worker results](debug-missing-results.md)).

## Diagnosis

Heddle runs three checks automatically on every actor start (worker,
processor, pipeline, orchestrator, scheduler, router, MCP). They
fire from
[`heddle.cli.preflight`](../api/core.md) before the actor enters its
run loop, and `--skip-preflight` opts out of all three.

### 1. NATS connectivity (`check_nats_connectivity`)

What it does: opens a short-timeout NATS connection, drains it,
returns success/failure.

What it catches: server down, wrong URL, network partition, auth
failure (NATS-side).

What it does **not** catch: NATS reachable but a specific subject
unsubscribed; broken subject routing; rate-limit exhaustion.

Test manually:

```bash
# Use any heddle actor command with a known-bad URL to exercise the check
uv run heddle worker --config configs/workers/echo.yaml \
  --nats-url nats://does-not-exist:4222
# Expected: "Cannot connect to NATS at nats://does-not-exist:4222.
# Is NATS running? Try: docker run -p 4222:4222 nats:latest"
```

Or from outside Heddle:

```bash
# nats-cli (separate tool, not part of Heddle)
nats --server=$NATS_URL stream ls
```

### 2. Environment variables (`check_env_vars`)

What it does: for each model tier, checks the tier-specific env
vars are present.

| Tier | Required env vars |
| ---- | ----------------- |
| `local` | `LM_STUDIO_URL` or `OLLAMA_URL` (either) |
| `standard` | `ANTHROPIC_API_KEY` |
| `frontier` | `ANTHROPIC_API_KEY` |

Missing vars produce **warnings**, not failures — the operator may
intend to set them at run time or use a different backend.

### 3. Config readability (`check_config_readable`)

What it does: confirms the YAML config file exists and parses as
YAML. Does not validate the schema (that's `heddle validate`).

What it catches: typo in the `--config` path, broken symlink,
truncated upload, illegal YAML (e.g. mixed tabs/spaces).

## Mitigation

| Diagnosis | Action |
| --------- | ------ |
| `Cannot connect to NATS` | Start NATS (`docker run -p 4222:4222 nats:latest`); confirm port reachable; confirm `--nats-url` matches |
| Env var warning | Export the missing var, or pick a different tier (`--tier`) |
| Config not readable | `ls -la $CONFIG`; `uv run heddle validate $CONFIG` for schema-level issues |
| Connected but no subscription | Read the actor log for an exception between `actor.connected` and `actor.subscribed`; usually a config-validation failure during `setup()` |

For the production case where preflight needs to run in a Kubernetes
liveness/readiness probe context: the placeholder probes in
[KUBERNETES.md](../KUBERNETES.md#health-checks) explicitly do
**not** exercise NATS. Use a sidecar `/healthz` exporter or a TCP
probe against the NATS port — the project does not ship a standalone
`heddle preflight` CLI today, despite the function being importable.

## Verify

```bash
uv run heddle worker --config configs/workers/echo.yaml --nats-url $NATS_URL
# Look for:
# - "Connected to NATS at $NATS_URL" (preflight)
# - "actor.connected" (BaseActor.connect)
# - "actor.subscribed" with the worker's subject
```

Then publish a smoke task:

```bash
uv run heddle submit "echo" --nats-url $NATS_URL
# Then observe the result on heddle.results.default
```

## Followup

Persistent NATS flakiness usually means one of: pod IPs shifting
without DNS catching up, NAT/firewall interfering with
long-lived TCP connections, or a NATS upgrade with subtle protocol
changes. Heddle's `NATSBus` adapter does not implement custom
reconnect logic — it relies on the `nats-py` client's defaults. If
you need tighter control, configure it through
`nats.connect(reconnect_time_wait=..., max_reconnect_attempts=...)`
by wrapping the adapter rather than monkey-patching it.
