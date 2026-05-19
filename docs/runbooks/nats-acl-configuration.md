# NATS ACL configuration — `heddle.contrib.events`

**Audience:** the operator deploying heddle's event-sourcing runtime
against a real NATS / JetStream cluster.
**Pairs with:** [ADR-013](../adr/013-nats-auth-model.md) (auth model
decision); v7 architecture §4.5 (belt-and-suspenders defence-in-depth).

This runbook gives concrete NATS server configuration for the
four-role auth model adopted in ADR-013. The structural defence here
is the publish ACL on `*.InternalFinalized`: only the `framework`
role may publish framework-internal finalisation events. The
application-layer backstop is the `Aggregate.apply()` provenance
check that raises `CorruptAggregateAlert` on a forged
`InternalFinalized`.

## The four roles

| Role | Who uses it | Publish | Subscribe |
|---|---|---|---|
| `framework` | P2 (cascade), P3 (horizon), framework command handlers | `heddle.events.>`, `heddle.commands.>`, `heddle.rejections.>`, `heddle.dedup.>` | all |
| `application` | Domain command issuers, regular application code | `heddle.events.*.*.*` **except** `*.InternalFinalized`; `heddle.commands.>`; `heddle.dedup.>` | all |
| `observer` | PF observers and similar ingest paths (Sprint 4a) | `heddle.events.*.*.*` **except** `*.InternalFinalized`; `heddle.commands.>` | `heddle.events.>` |
| `workshop` | Workshop UI, CLI tooling | `heddle.commands.>` | all |

`*.InternalFinalized` is the security-relevant subject. The Aggregate's
`apply()` provenance check is the application-layer defence; the
publish ACL is the structural belt-and-braces.

## NATS server config (multi-user, single account)

`nats-server` 2.10+, file-based config. Adapt for your operator
auth fabric (operator-NATS-resolver, NSC, JWT) — the user
permissions block is the same shape in all of them.

```hocon
accounts {
  HEDDLE {
    users = [
      {
        user: "framework"
        password: "$2a$..."   # bcrypt; rotate as needed
        permissions {
          publish = {
            allow = [
              "heddle.events.>",
              "heddle.commands.>",
              "heddle.rejections.>",
              "heddle.dedup.>"
            ]
          }
          subscribe = { allow = [">"] }
        }
      },
      {
        user: "application"
        password: "$2a$..."
        permissions {
          publish = {
            allow = [
              "heddle.events.>",
              "heddle.commands.>",
              "heddle.dedup.>"
            ]
            deny = [
              "heddle.events.*.*.InternalFinalized"
            ]
          }
          subscribe = { allow = [">"] }
        }
      },
      {
        user: "observer"
        password: "$2a$..."
        permissions {
          publish = {
            allow = [
              "heddle.events.>",
              "heddle.commands.>"
            ]
            deny = [
              "heddle.events.*.*.InternalFinalized"
            ]
          }
          subscribe = {
            allow = ["heddle.events.>"]
          }
        }
      },
      {
        user: "workshop"
        password: "$2a$..."
        permissions {
          publish = {
            allow = ["heddle.commands.>"]
          }
          subscribe = { allow = [">"] }
        }
      }
    ]
  }
}
```

### Why `deny` rather than a negative pattern in `allow`

`nats-server` permission rules use `allow` plus an explicit `deny`
list rather than pattern negation. The 4-token shape
`heddle.events.*.*.InternalFinalized` is what makes the deny match
the wire subject `heddle.events.{type}.{id}.InternalFinalized`.

The wildcard tokens are positional: `events.*.*.*` matches every
event regardless of aggregate, id, and event type. The deny shape
matches the same prefix but pins the trailing token. Together
they're equivalent to "publish events except internal finalisation."

## Verifying the ACL

```bash
# As framework — should succeed.
nats pub --user framework --password ... \
    heddle.events.Job.j-1.InternalFinalized '{}'

# As application — should be rejected with "permissions violation".
nats pub --user application --password ... \
    heddle.events.Job.j-1.InternalFinalized '{}'

# As application — domain event should succeed.
nats pub --user application --password ... \
    heddle.events.Job.j-1.JobShippedFromPF '{}'
```

The Sprint 3 integration test (`test_jetstream_event_log.py` /
`@pytest.mark.integration`) covers the `framework`-role success
path. A multi-user ACL integration test is out of scope for the
v7 done criterion — operators verify against their own cluster.

## Rotation and incident response

- Treat the `framework` user as the most privileged credential —
  compromise lets an attacker fabricate `InternalFinalized` events
  that the application-layer provenance check would still catch but
  that bypass the structural defence.
- Rotate passwords / JWT keys per your organisation's policy. The
  rotation does not require a heddle restart if the NATS server
  reloads its config (SIGHUP) and your client uses lazy reconnect.
- A `CorruptAggregateAlert` raised by `Aggregate.apply()` is the
  trip-wire that the publish ACL leaked. See v7 §4.12 for the
  manual recovery runbook.

## Migration to multi-account

If heddle is ever deployed in a multi-tenant SaaS context (post-M2
commercial path), the migration is config-only — split the `HEDDLE`
account into one account per tenant with the same user-role shape
inside each, plus exports/imports for any cross-tenant flow. The
application code does not change. ADR-013 captures the explicit
non-decision for now.
