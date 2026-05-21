"""Composition root for WireEnvelope payload-type registration.

Importing this module registers every built-in `WireEnvelope` body type so
that `heddle.core.envelope.get_payload_model` / `parse` can resolve them. A
process that parses generic envelopes should import `heddle.bootstrap` once at
startup; otherwise a body whose module was never imported raises a loud
`KeyError` (the bootstrap gotcha — see `heddle.core.envelope`).

This lives **above** `heddle.core` on purpose: it imports both core message
bodies and (in a later sprint) `heddle.contrib.*` bodies, and core must never
import contrib (DESIGN_INVARIANTS #23). The composition root is the one place
allowed to know about both.

Registration is a side effect of importing the owning body module:

- `heddle.core.messages` registers `core.OrchestratorGoal` (and, once they ride
  the envelope, `core.TaskMessage` / `core.TaskResult`).
- contrib event/command/rejection bodies will be added here when they ride the
  envelope.
"""

from __future__ import annotations

import importlib

# Side-effect import: loading the module runs its `register_payload_type`
# calls. Done via importlib so it reads as a deliberate side effect (no
# "unused import" from either ruff or pyright) and to keep the list of body
# modules data-like as it grows in later sprints.
_BODY_MODULES = (
    "heddle.core.messages",  # registers core.* payload types
    # S3 adds: "heddle.contrib.events.envelopes"
)

for _mod in _BODY_MODULES:
    importlib.import_module(_mod)
