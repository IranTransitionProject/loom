# CLAUDE.md — Heddle

The canonical agent instructions for this repository live in
**[`AGENTS.md`](AGENTS.md)**. Read that first.

Cross-repo guidance (philosophy, invariants, wire-protocol contract,
skills, and subagents) lives in
**[`../heddle-agent-toolkit/`](../heddle-agent-toolkit/)** — installed
into this repo's `.claude/` via the toolkit's `install.sh`.

## Claude-specific notes

These notes don't fit AGENTS.md because they describe Claude's workflow
inside this repo, not the repo itself.

- When session history is compacted or missing, recover from
  `AGENTS.md` (this repo) + the toolkit anchors. Both should fit on one
  screen each.
- For non-trivial structural work, prefer spawning the
  `heddle-architect` subagent to design first. The plan returns to the
  top-level conversation; the architect's context stays separate.
- Prefer invoking `/heddle-orient` over silently re-reading docs at the
  start of a session.

## Session-starter queue

`session-starters/` (gitignored) holds the user's queue of design-chat
starters and Claude Code prompts. One file per queued session,
sortable-letter-prefixed (`A-…`, `B-…`). Read for context when the user
references "the next session" or a specific letter; never commit; never
echo contents into commit messages verbatim.

If anything in this file conflicts with `AGENTS.md`, follow `AGENTS.md`
and the current user request.
