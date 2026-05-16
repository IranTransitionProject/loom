# Releasing Heddle

End-to-end workflow for cutting a new release and publishing to PyPI.
This document is **agent-runnable**: an AI agent following these
steps in order should produce a clean release without needing to ask
clarifying questions, except where this doc says to confirm.

Each release is a tagged commit on `main` that triggers automated
PyPI publishing via [`.github/workflows/publish.yml`](https://github.com/getheddle/heddle/blob/main/.github/workflows/publish.yml)
— trusted publishing, no API token required.

---

## When to release

Cut a release when:

- A meaningful set of changes has accumulated under `[Unreleased]`
  in [`CHANGELOG.md`](https://github.com/getheddle/heddle/blob/main/CHANGELOG.md).
- A breaking change has landed and downstream consumers need a
  compatible artifact.
- The user explicitly requests it.

Skip a release for a single typo fix or a docs-only commit unless
the user asks.

## Pick the version

Heddle follows [Semantic Versioning](https://semver.org/).

| Bump | When |
|---|---|
| **Major** (`vX.0.0`) | Incompatible API changes. Pre-1.0, use sparingly for big direction shifts. |
| **Minor** (`vX.Y.0`) | Backward-compatible feature additions. Pre-1.0, also for breaking changes. |
| **Patch** (`vX.Y.Z`) | Backward-compatible bug fixes. |

Inspect the `[Unreleased]` section of `CHANGELOG.md`:

- `### Added` or `### Changed` → minor.
- `### Removed (breaking)` → minor (pre-1.0) or major (post-1.0).
- `### Fixed` / `### Deprecated` / `### Security` only → patch.

**Always confirm the version with the user before bumping.** Don't
choose unilaterally.

## Decide: per-release notes file?

Read [`docs/releases/README.md`](releases/README.md) for the
convention. Create `docs/releases/vX.Y.Z.md` when the release has:

- A breaking change with a migration guide too long for a CHANGELOG
  bullet.
- A major version bump or end of a long arc of work worth narrating.
- Multi-subsystem changes that benefit from a single explanation.

For routine releases (patches, small additions), skip the file. The
CHANGELOG entry is enough; the GitHub Release body is generated
from it.

---

## Steps

### 1. Bump the version

Edit `pyproject.toml`:

```toml
[project]
name = "heddle-ai"
version = "X.Y.Z"   # bump here
```

`pyproject.toml` is the only source of the version number — no
duplicate in `__init__.py` or elsewhere.

### 2. Close out the CHANGELOG

In [`CHANGELOG.md`](https://github.com/getheddle/heddle/blob/main/CHANGELOG.md):

- Rename `## [Unreleased]` → `## [X.Y.Z] — YYYY-MM-DD`.
- Add a fresh empty `## [Unreleased]` section *above* the new one.
- Update the link references at the bottom of the file:

```markdown
[Unreleased]: https://github.com/getheddle/heddle/compare/vX.Y.Z...HEAD
[X.Y.Z]: https://github.com/getheddle/heddle/releases/tag/vX.Y.Z
```

If you're adding a per-release notes file, cross-reference it from
the new `[X.Y.Z]` section:

```markdown
Full historical release notes: [`docs/releases/vX.Y.Z.md`](docs/releases/vX.Y.Z.md).
```

### 3. (Optional) Write the per-release notes file

If you decided one is warranted:

- Create `docs/releases/vX.Y.Z.md`. Use
  [`docs/releases/v0.9.2.md`](releases/v0.9.2.md) as a structural
  reference.
- Add it to `mkdocs.yml` nav under the `Release Notes` section,
  newest-first:

```yaml
  - Release Notes:
    - Overview: releases/README.md
    - vX.Y.Z: releases/vX.Y.Z.md     # new release on top
    - v0.9.2: releases/v0.9.2.md     # previous releases below
```

### 4. Verify before tagging

Tags are hard to unmake. Run the standard preflight subset and the
package build:

```bash
# Lint, type-check, unit tests.
/heddle-preflight   # or the equivalent commands manually

# Docs build with the new release nav entry.
VIRTUAL_ENV= uv run --extra docs mkdocs build --strict

# Package builds cleanly.
uv build
```

If any step fails, stop and fix before tagging.

### 5. Commit and push the release commit

```bash
git add pyproject.toml CHANGELOG.md
git add docs/releases/ mkdocs.yml   # if a release-notes file was added
git commit -m "release: vX.Y.Z"
git push origin main
```

Use a terse commit message. The narrative belongs in CHANGELOG and
the per-release file.

### 6. Tag and push the tag

```bash
git tag -a vX.Y.Z -m "Heddle vX.Y.Z"
git push origin vX.Y.Z
```

Use annotated tags (`-a`) — they carry the tagger and message;
lightweight tags lose both.

### 7. Create the GitHub Release

If a per-release notes file exists:

```bash
gh release create vX.Y.Z \
    --title "Heddle vX.Y.Z" \
    --notes-file docs/releases/vX.Y.Z.md
```

If only a CHANGELOG entry, extract its section as the release notes:

```bash
# Extract the [X.Y.Z] section, stopping at the next heading.
awk '/^## \[X\.Y\.Z\]/{flag=1; next} /^## \[/{flag=0} flag' CHANGELOG.md > /tmp/notes.md
gh release create vX.Y.Z --title "Heddle vX.Y.Z" --notes-file /tmp/notes.md
```

Creating the GitHub Release triggers
[`publish.yml`](https://github.com/getheddle/heddle/blob/main/.github/workflows/publish.yml), which builds the
package and publishes to PyPI via trusted publishing. **Do not run
`uv publish` manually** — trusted publishing is the only authorized
path.

### 8. Verify the release went out

- Check the Actions tab: the "Publish to PyPI" workflow run should
  succeed within ~3 minutes of the release being published.
- Confirm the new version on PyPI:
  <https://pypi.org/project/heddle-ai/>.
- Confirm the GitHub Release page shows the right notes:
  `https://github.com/getheddle/heddle/releases/tag/vX.Y.Z`.

### 9. (Post-release edits) Refresh the GitHub Release body

Per-release files are frozen by convention, but small post-release
typo fixes happen. After editing `docs/releases/vX.Y.Z.md` (or the
relevant CHANGELOG section if no per-release file exists):

```bash
gh release edit vX.Y.Z --notes-file docs/releases/vX.Y.Z.md
```

Otherwise the GitHub Release body drifts from the in-repo source.
**Do not edit the GitHub Release body through the web UI** — always
edit the file in the repo and refresh via `gh release edit`. The
in-repo file is the source of truth; the GitHub Release is the
discoverable surface.

---

## Rollback

PyPI **cannot un-publish** a version (only yank, which still leaves
the version visible). If a release goes out broken, the answer in
nearly every case is **ship the next patch as quickly as possible**.

Other rollback options exist but are mostly destructive:

- `gh release delete vX.Y.Z` — removes the GitHub Release, but the
  tag persists and PyPI still has the artifact.
- `git push --force origin vX.Y.Z` — rewrites the tag, breaks anyone
  who already pulled. Avoid unless the user explicitly authorizes.

When in doubt, cut the next patch.

---

## What an agent should NOT do unilaterally

- **Bump the version.** Confirm with the user first.
- **Push tags.** Stage the release commit, then ask the user to
  review before tagging.
- **Run `uv publish` or other direct PyPI publishing.** Trusted
  publishing via the workflow is the only authorized path.
- **Edit past-release CHANGELOG entries or per-release notes files**
  except for clear typo fixes.
- **Force-push tags.** Always confirm; this is destructive.

---

## Cross-references

- [`CHANGELOG.md`](https://github.com/getheddle/heddle/blob/main/CHANGELOG.md) — running history; the running
  list of `[Unreleased]` entries is the input to each release.
- [`docs/releases/README.md`](releases/README.md) — convention for
  when to write a per-release notes file.
- [`docs/CONTRIBUTING.md`](CONTRIBUTING.md) — broader contribution
  guidelines including the CHANGELOG-update requirement on
  behaviour-bearing commits.
- [`.github/workflows/publish.yml`](https://github.com/getheddle/heddle/blob/main/.github/workflows/publish.yml)
  — the PyPI publishing workflow that fires on release publish.
