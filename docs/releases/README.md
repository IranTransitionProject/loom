# Release notes

Per-release deep-dive notes for Heddle. Each file in this directory
corresponds to one tagged release (e.g. `v0.9.2.md` ↔ tag `v0.9.2`).

## When to add a file here

These files exist for releases that need **more than a CHANGELOG entry
can carry**. The CHANGELOG (`../../CHANGELOG.md`) is always the
running history; per-release files are optional and add depth for
specific releases.

Add a file when the release includes any of:

- A **breaking change** with a migration guide longer than a few lines.
- A **major version** bump or a release that closes a long arc of
  work worth narrating.
- **Cross-cutting changes** that touch multiple subsystems and benefit
  from a single explanatory document.

Skip the file when the release is **routine** — a few bug fixes, a
docs touch-up, dependency bumps. The CHANGELOG bullets are enough.

## Conventions

- **One file per release**, named `vX.Y.Z.md` (matches the git tag).
- **Frozen at release time.** Past-release files don't change after
  the tag lands, except for typo fixes. Treat them as artifacts of
  the release, not living docs.
- **Cross-link from CHANGELOG.md** when this file exists, using a
  relative path: `Full historical release notes: [docs/releases/vX.Y.Z.md](docs/releases/vX.Y.Z.md).`
- **Attach to the GitHub Release** at creation time:
  `gh release create vX.Y.Z --notes-file docs/releases/vX.Y.Z.md`.
- **After post-release edits, refresh the GitHub Release** so the
  rendered notes don't drift from the file:
  `gh release edit vX.Y.Z --notes-file docs/releases/vX.Y.Z.md`.
  The in-repo file is the source of truth; the GitHub Release is the
  discoverable surface.
- **Do not edit the GitHub Release body through the web UI** —
  changes are easy to lose and impossible to PR-review. Always edit
  the file in the repo and refresh via `gh release edit`.

## Full release workflow

The end-to-end workflow for cutting a new release (version bump,
CHANGELOG close-out, tag, GitHub Release, PyPI publish) lives in
[`../RELEASING.md`](../RELEASING.md). That doc is agent-runnable —
follow it step-by-step.

## Format

There's no strict template. Existing files set the shape — see
[`v0.9.2.md`](v0.9.2.md) as the reference. Common sections:

- Headline (date, previous version, breaking-change flag, commit count).
- Summary (one paragraph of what shipped and why).
- Section per major change with rationale, before/after, and migration
  steps where applicable.
- Sometimes: contributor thanks, upgrade guide, deprecation timeline.

## Why these don't live at the repo root

Root-level `RELEASE_NOTES_vX.Y.Z.md` files clutter the listing,
don't scale past a few releases, and sit outside the MkDocs `docs/`
tree (so they can't be included in the site nav or rendered with the
rest of the docs). `docs/releases/` is the conventional home and
matches the layout used by Django, Rails, and other long-lived
projects.
