# ADR-013: NATS auth model for `heddle.contrib.events`

**Status:** Accepted (Sprint 3, 2026-05-19).
**Pairs with:** [runbook: nats-acl-configuration](../runbooks/nats-acl-configuration.md)
(concrete config); `heddle-contrib-events-m2-architecture-v7.md`
§4.5 (publish-ACL backstop on `*.InternalFinalized`).

## Context

Sprint 3 adds the structural belt-and-braces defence for
framework-internal events: a NATS publish ACL that prevents
non-framework callers from publishing `*.InternalFinalized` events.
The application-layer defence (`Aggregate.apply()` provenance check
raising `CorruptAggregateAlert`) is unchanged.

Implementing the publish ACL forces a question that v7 left open:
how is heddle's deployment authenticated against NATS? Two
realistic shapes:

- **Multi-account.** One NATS account per logical boundary
  (framework / application / observer / workshop). Cross-account
  flow uses NATS account exports/imports. Strong isolation; one
  message hop per cross-boundary publish.
- **Multi-user, single-account.** One NATS account; multiple
  users inside it with different publish/subscribe permission
  blocks. Permission boundary is per-user; no message-fabric
  isolation.

The decision shapes operator runbooks, the Sprint 3 ACL config,
and any future commercial deployment.

## Decision

**Adopt multi-user, single-account.** Heddle ships with four
predefined user roles inside one NATS account:

- `framework` — P2/P3 framework projectors, framework command
  handlers. Publishes everything heddle owns; subscribes to all.
- `application` — domain command issuers and regular application
  code. Publishes events except `*.InternalFinalized`; publishes
  commands and dedup announcements; subscribes to all.
- `observer` — PF observers and similar ingest paths (Sprint 4a).
  Same publish posture as `application` minus dedup, but
  narrower subscribe (events only).
- `workshop` — the Workshop UI and CLI. Publishes commands only;
  subscribes to all for live-view rendering.

The concrete NATS server config and verification commands live in
the [`nats-acl-configuration` runbook](../runbooks/nats-acl-configuration.md).

## Alternatives considered

### Multi-account (rejected for M2)

One NATS account per role; cross-account flow via exports/imports.

- **Rejected because** the trust boundary in heddle's deployment
  is between *components within one team's deployment*, not
  between *tenants*. Multi-account adds operational complexity
  (managing the export/import graph, debugging routing across
  accounts) for an isolation property heddle does not need at
  this scale.
- Per-message latency: each cross-account publish takes an extra
  hop through the export/import routing. Single-digit
  microseconds in practice, but it's non-zero, and the framework
  pays it on every projector→command-handler round-trip. With
  multi-user-single-account the message stays on the same
  account and skips the hop.
- The benefit of multi-account — strong isolation between
  unrelated workloads sharing a NATS cluster — applies when
  heddle is one of several tenants on shared infrastructure. The
  current target deployment (Naimor SMB on-prem) is single-
  tenant.

### Single user, ACL-less (rejected)

One NATS user; trust the application code not to publish
`*.InternalFinalized` from non-framework call sites.

- **Rejected because** it leaves the publish-ACL backstop
  unwired. v7 §4.5 explicitly requires belt-and-braces:
  application-layer provenance check **plus** structural defence.
  The ACL is the structural half. Removing it means the only
  defence against a forged `InternalFinalized` is whatever the
  receiving aggregate's `apply()` notices — which is still
  enforced, but the cost of a misfire (recovery via the §4.12
  runbook) is high enough that a structural prevention is worth
  the modest operator-config work.

### Per-worker user (rejected)

One NATS user per heddle component (one for each projector, one
for each application worker, etc.).

- **Rejected because** the granularity is theatre. The publish
  ACL needs to distinguish "framework-finalises" from "everyone
  else"; finer-grained users add credential management overhead
  without adding security properties. The four-role shape is
  the natural cut.

## Consequences

**Enables:**

- The publish ACL on `*.InternalFinalized` is operationally
  workable from a single config file. Operators don't need to
  understand NATS account exports/imports to deploy heddle.
- Future commercial multi-tenant deployment can wrap each tenant
  in its own NATS account with the same four-role shape inside.
  The migration is config-only — application code doesn't change.
- The Workshop and CLI get a deliberately narrow surface
  (`workshop` user only publishes commands), which the runbook
  encodes as an operator constraint, not a heddle-internal
  assumption.

**Costs:**

- Heddle must document the four-user shape as part of its
  deployment surface. New operators read one runbook
  ([nats-acl-configuration](../runbooks/nats-acl-configuration.md))
  to understand what credentials their deployment needs.
- The single-account isolation property is weaker than multi-
  account. A compromise of the `framework` credential lets an
  attacker fabricate any heddle subject. The application-layer
  provenance check still catches forged
  `InternalFinalized` events; rotation policy is the operator's
  responsibility.
- A future multi-tenant migration is non-zero work, but it's
  config-only and bounded — the worst case is "split the one
  account into N accounts and add exports/imports for any flow
  that crosses tenant boundaries." No application code change.

## Out of scope

- JWT vs static-password auth: orthogonal. The four-role shape
  works with either.
- NATS leaf-node deployment: orthogonal. The ACL config travels
  with the account; leaf vs hub is an operational topology
  concern.
- Audit logging of publish-permission rejections: a future
  follow-up if `CorruptAggregateAlert` events start showing up
  in production. Today, the ACL silently rejects and the
  application-layer alert is the visible signal.
