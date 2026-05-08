# Heddle Vision — Ad-Hoc Personal & SMB Compute Clusters

**Status:** Stated direction.
**Date:** 2026-04-29.
**Full thinking:** [`getheddle/warp-design`](https://github.com/getheddle/warp-design)
(separate repo for design exploration, ADRs, research notes, evolution
log — running ahead of this canonical doc during exploration).

## What heddle is becoming

Heddle today is an actor-mesh framework with strong single-machine and
traditional-cloud-cluster deployment stories. Heddle is *becoming* the
substrate for **ad-hoc personal and small-business AI compute clusters**:
a control plane that pools capacity across a few Macs (and eventually
Jetsons, Linux servers, Windows machines), routes workloads by privacy +
deadline + cost policy, bursts to cloud only when local can't meet the
constraint, and recommends hardware purchases based on observed workload
patterns.

This is additive to heddle's existing audience. The actor mesh, NATS bus,
MCP gateway, RAG pipeline, scheduler, Workshop, and TUI all stay. New
primitives layer on top.

## Why now

Two trends meet:

- **Personal-axis silicon is now production-grade for AI workloads.** Apple
  Silicon's unified memory, heterogeneous cores, and Neural Engines mean
  a Mac Studio can run inference workloads that needed a small GPU cluster
  three years ago. NVIDIA Jetson Orin and similar edge accelerators
  reinforce the trend.
- **Hyperscaler-axis silicon is now economical at scale.** AWS Graviton,
  Trainium, and Inferentia are built for cheapest compute per workload.
  Hyperscalers run continuous, specialized workloads that personal devices
  can't, at unit costs that personal devices can't match.

The two axes optimize for orthogonal things: *best computer per user* vs.
*cheapest compute per workload*. They aren't substitutes — they're
complementary layers of the AI future. But today's tooling treats them in
isolation.

The gap is **a control plane that uses both layers cooperatively** with
privacy, cost, and operability as first-class concerns. Heddle is
positioned to fill it.

## Distinguishing claims

Five things heddle aims to do that nothing else combines today:

1. **AI-workflow shape** — not just inference, but workers + pipelines +
   councils, all as first-class units of compute that can ship across nodes.
2. **Heterogeneity-native capacity model** — Apple Silicon's unified memory
   - Neural Engine + heterogeneous cores get a real vocabulary, not "CPU +
   memory + GPU#" lowest-common-denominator.
3. **Privacy-aware routing** — workloads are tagged; tags constrain
   placement. A "personal-data" workload never leaves the local cluster.
4. **Cost arbitrage** — local sunk-cost compute vs. cloud variable cost,
   with policy controls and a real cost model.
5. **Hardware advisor** — longitudinal workload analysis becomes purchase
   advice ("a Mac Studio M5 Ultra pays back in 9 months at your inference
   rate; or a Jetson AGX Orin if your workload shape is...").

Plus an **SMB-friendly UX**: feels like AirDrop or HomeKit, not like
`kubectl`.

## Phased delivery

| Phase | Deliverable | Where it lives |
|---|---|---|
| 0 | **warp daemon-core** — Swift agent on macOS supervising existing heddle services, registers via SMAppService, mDNS announce, foundation modules wired | `getheddle/warp` |
| 1 | **Capacity reporter** — agent publishes its live capacity over NATS; Workshop renders fleet view | `getheddle/warp` + `getheddle/heddle` (Workshop UI) |
| 2 | **Capacity-aware routing** — pipeline stages route by best-fit capacity; stateless graceful drain | `getheddle/heddle` (scheduler) + `getheddle/warp` |
| 3 | **Budget + cloud arbitrage** — cloud-burst policy engine, per-user budgets | `getheddle/heddle` |
| 4 | **Hardware advisor** — workload-pattern analysis, ROI projections | `getheddle/heddle` (probably as a Workshop module) |
| 5 | **Linux + Windows agents** — Rust agent for non-macOS; protocol formalized | New repos under `getheddle/` |
| 6 | **UX polish** — first-class macOS app for cluster setup; SMB onboarding flow | New repo, native macOS app |

Each phase is independently useful. None depends on the next phase
shipping.

## What this is not

- **Not a Kubernetes replacement** for ops-team-shaped deployments. K8s is
  the right tool when you have an ops team, a homogeneous fleet, and
  data-center networking. Heddle's lane is the opposite environment.
- **Not Apple-only forever.** macOS is the v0 environment because of
  the platform's mixed-load performance, Thunderbolt M2M links, native
  privacy primitives, and operator familiarity. Linux/Windows agents
  follow.
- **Not a cloud replacement.** Cloud is the right answer for large
  training, massive datasets, and cross-organization collaboration.
  Heddle uses cloud when local can't.
- **Not a research project.** Production direction with phased delivery.

## How this affects existing heddle work

The actor mesh, NATS bus, MCP gateway, RAG pipeline, scheduler, Workshop,
TUI, and tracing all stay and continue to evolve. The cluster work
*builds on* these — capacity-aware routing extends the existing
`PipelineOrchestrator`; the budget controller is a new module that talks
to existing schedulers; the hardware advisor is a new Workshop tab.

There is no break in compatibility planned. Heddle deployments that
don't want any of the cluster features stay on the single-machine /
traditional-cluster path forever.

## Where to learn more

- **Full thinking:** [`getheddle/warp-design`](https://github.com/getheddle/warp-design)
- **Vision detail:** [`warp-design/VISION_AD_HOC_CLUSTERS.md`](https://github.com/getheddle/warp-design/blob/main/VISION_AD_HOC_CLUSTERS.md)
- **Architectural sketch:** [`warp-design/exploration/CLUSTER_ARCHITECTURE.md`](https://github.com/getheddle/warp-design/blob/main/exploration/CLUSTER_ARCHITECTURE.md)
- **Prior art survey:** [`warp-design/exploration/PRIOR_ART.md`](https://github.com/getheddle/warp-design/blob/main/exploration/PRIOR_ART.md)
- **v0 daemon scope:** [`warp-design/daemon-v0/SCOPE.md`](https://github.com/getheddle/warp-design/blob/main/daemon-v0/SCOPE.md)
- **Decision records:** [`warp-design/decisions/`](https://github.com/getheddle/warp-design/tree/main/decisions)
