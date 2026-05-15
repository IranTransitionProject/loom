# Governance

**Heddle — Config-driven multi-LLM workflows**
**Effective:** 2026
**Contact:** admin@getheddle.dev

---

## Mission Constraint

This project exists to produce reliable, well-tested, openly available
infrastructure for AI workflow orchestration. All governance decisions must be
evaluated against a single test: does this decision serve that mission or does
it serve something else?

Vendor lock-in, proprietary dependency capture, or architectural decisions
that compromise the framework's generality for the benefit of a single
organization are incompatible with the mission and constitute grounds for
leadership review.

---

## Ownership Structure

| Role | Holder | Scope |
|------|--------|-------|
| Founder / Copyright Holder | Hooman (admin@getheddle.dev) | Full authority while active |
| GitHub Org Owner | IranTransitionProject org | Administrative control |
| Co-owner (succession backup) | To be designated <!-- TODO(governance): designate before the next signed release per the "not optional" rule below --> | Prevents single-point-of-failure |

**A designated co-owner with GitHub org Owner rights must be in place at all times.**
This is not optional. A project with one owner has no succession — it has a
single point of failure. **Status (2026-05-11):** governance debt — the
co-owner slot is unfilled. Designation is on the next-release punch list.

---

## Succession

### Voluntary Transfer

The Founder may transfer leadership at any time by:

1. Identifying a successor who accepts the mission constraint above
2. Documenting the transfer in this file via a signed commit
3. Transferring GitHub org ownership
4. Updating the copyright notice to reflect the new steward

The Founder retains copyright over original contributions. Transferred stewardship
covers operational and editorial control, not retroactive copyright reassignment.

### Incapacity or Abandonment

If the Founder is unable to act and the project has been inactive for 90+ days:

1. The designated co-owner assumes interim stewardship
2. Interim steward documents the assumption in this file
3. A successor search begins within 30 days of assumption

### Succession Criteria

A successor must:

- Accept the mission constraint without reservation
- Commit to maintaining the project's open-source character
- Maintain the MPL 2.0 public license (alternative licensing rights
  revert to the copyright holder and do not automatically transfer)

---

## Licensing

**Default:** All source code in this repository is licensed under the Mozilla
Public License 2.0. See `LICENSE` for terms.

**Alternative licensing:** The copyright holder may grant alternative licensing
arrangements to specific parties (e.g., organizations with copyleft restrictions).
Such arrangements:

- Are granted in writing by the copyright holder only
- Do not affect the MPL 2.0 terms available to all other parties
- Require a signed Contributor License Agreement (CLA) from any contributor
  whose work is included in the alternative-licensed material

**Note on succession:** Alternative licensing authority belongs to the copyright
holder, not the operational steward. A successor who does not hold copyright
cannot grant alternative licenses on the Founder's original work without explicit
written delegation.

---

## Contributions

All contributors must sign the Contributor License Agreement (CLA) before any
pull request is merged. See `docs/CONTRIBUTING.md`.

The CLA grants the project the right to sublicense contributions, which is
necessary to preserve alternative licensing flexibility. It does not transfer
copyright ownership — contributors retain copyright over their own work.

---

## Amendments

This document may be amended by the current copyright holder or designated
steward via a documented commit to the main branch. All amendments are logged
in git history. No amendment may:

- Remove the mission constraint
- Eliminate the MPL 2.0 public license
- Retroactively alter contributor rights under a previously signed CLA
