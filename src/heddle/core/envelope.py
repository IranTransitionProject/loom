"""Base wire envelope — the single generic frame every heddle message rides.

`WireEnvelope` is the horizontal datagram: identity, timestamps, origin, an
opaque typed `payload`, and an open extension map. It carries **no** domain
semantics. Each message type (core's `TaskMessage`/`TaskResult`/
`OrchestratorGoal`, contrib's event/command/rejection bodies) becomes a *body*
that rides inside `payload`, discriminated by `payload_type`. This is the
composition mechanism (base + discriminator + opaque body), chosen over
Pydantic subclassing because `heddle-sdk`'s schema sync is a flat glob with no
`$ref`/`allOf` resolution — composition keeps every schema self-contained.

Parsing is **two-step** (mirrors `core.contracts`: the caller supplies the
model, core never hard-codes a body→type map):

    envelope = WireEnvelope.model_validate(data)   # validate the frame
    body = unwrap(envelope)                         # dispatch + validate the body

Body resolution goes through a process-global registry. Core owns the registry
*mechanism*; it never owns the *entries* and never imports contrib — each
module registers its own body types at import (`register_payload_type`). This
is the dependency inversion that keeps the one-way core↛contrib rule
(DESIGN_INVARIANTS #23): core references only `BaseModel`.

Bootstrap contract (the gotcha): registration runs on import of the owning
body module, so **a process that parses a generic envelope must have imported
that module first**, or `get_payload_model` raises a loud `KeyError`. There is
no silent fallback. Surfaces that must honor this: Workshop replay from a cold
process, test fixtures, and the MCP bridge across process boundaries. The
concrete aggregator of body-module imports is introduced in the sprint that
adds the first real body (core messages), not here — S1 ships only the base
and the mechanism.

Middleware Lane (DESIGN_INVARIANTS #22): `WireEnvelope` is the *one*
underscore-key carrier. `model_config = ConfigDict(extra="allow")` preserves
top-level `_`-prefixed middleware keys (e.g. `_trace_context`) and any
extension keys through validation and round-trip; bodies stay strict.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import uuid_utils
from pydantic import BaseModel, ConfigDict, Field, model_validator


def _uuid7() -> str:
    """Time-ordered UUIDv7 string — sortable, good for message/log identity."""
    return str(uuid_utils.uuid7())


def _now() -> datetime:
    return datetime.now(UTC)


class WireEnvelope(BaseModel):
    """The generic frame all heddle messages ride. Bodies live in `payload`."""

    # extra="allow" makes this the single Middleware-Lane carrier (#22): it
    # preserves top-level `_`-prefixed keys and extension fields unchanged.
    model_config = ConfigDict(extra="allow")

    message_id: str = Field(default_factory=_uuid7)
    payload_type: str
    """Dotted `<owner>.<BodyName>` discriminator, e.g. `core.TaskMessage`,
    `events.Event`. Resolves to a body model via the payload-type registry."""
    payload: dict[str, Any]
    """Opaque body slot — the specialization's typed body, validated in step
    two against the model registered for `payload_type`."""
    origin: str | None = None
    """Abstract principal that emitted this message (free-form; no vocabulary
    at the base level)."""
    correlation_id: str | None = None
    causation_id: str | None = None
    occurred_at: datetime = Field(default_factory=_now)
    """Domain time — when the thing this message is about happened."""
    recorded_at: datetime = Field(default_factory=_now)
    """Bus time — when this message was recorded onto the transport."""

    @model_validator(mode="before")
    @classmethod
    def _reject_unknown_non_underscore_keys(cls, data: Any) -> Any:
        """Allow `_`-prefixed Middleware-Lane keys, reject misspelled fields.

        `extra="allow"` is for the Middleware Lane (#22), not "anything goes":
        a top-level key that is neither a declared field nor `_`-prefixed is a
        typo (`correlaton_id`, `payload_typ`) that would otherwise survive
        silently. Reject it loudly here.
        """
        if not isinstance(data, dict):
            return data
        as_dict = cast("dict[str, Any]", data)
        allowed = set(cls.model_fields)
        unknown = sorted(key for key in as_dict if not key.startswith("_") and key not in allowed)
        if unknown:
            raise ValueError(
                f"unknown top-level key(s) {unknown} on WireEnvelope — "
                f"only declared fields and `_`-prefixed Middleware-Lane keys "
                f"are accepted (likely a misspelled field name)"
            )
        return as_dict


# --------------------------------------------------------------------------
# Payload-type registry — the body-resolution mechanism (core owns the map
# structure; modules inject their own entries; core never imports contrib).
# --------------------------------------------------------------------------

_PAYLOAD_REGISTRY: dict[str, type[BaseModel]] = {}
"""Mapping `payload_type` discriminator → body model.

Populated by :func:`register_payload_type` at body-module import. Treat as
read-only at runtime; tests snapshot/restore it to avoid cross-test pollution.
"""


def register_payload_type(payload_type: str, model: type[BaseModel]) -> None:
    """Register `model` as the body for `payload_type`.

    Called by a body module at import time. Raises if `payload_type` is already
    bound to a *different* model (prevents silent shadowing); re-registering the
    same model is a no-op so module reloads stay tolerant.
    """
    existing = _PAYLOAD_REGISTRY.get(payload_type)
    if existing is not None and existing is not model:
        raise ValueError(
            f"payload_type {payload_type!r} already registered to "
            f"{existing.__name__}; cannot reassign to {model.__name__}"
        )
    _PAYLOAD_REGISTRY[payload_type] = model


def get_payload_model(payload_type: str) -> type[BaseModel]:
    """Look up the body model for `payload_type`. Raises a loud ``KeyError``.

    The error names the likely cause — the owning body module was never
    imported (the bootstrap gotcha) — rather than failing silently.
    """
    try:
        return _PAYLOAD_REGISTRY[payload_type]
    except KeyError as exc:
        raise KeyError(
            f"no payload model registered for {payload_type!r}. "
            f"Did the module defining it get imported?"
        ) from exc


def wrap(payload_type: str, body: BaseModel, **frame: Any) -> WireEnvelope:
    """Build a `WireEnvelope` around a typed `body`.

    Dumps `body` in JSON mode so the `payload` dict is wire-serializable
    (datetimes, enums, etc. become JSON-native). Extra `frame` kwargs set
    envelope fields (`origin`, `correlation_id`, timestamps, …).
    """
    return WireEnvelope(
        payload_type=payload_type,
        payload=body.model_dump(mode="json"),
        **frame,
    )


def unwrap(envelope: WireEnvelope) -> BaseModel:
    """Validate and return the typed body for `envelope.payload_type` (step two)."""
    model = get_payload_model(envelope.payload_type)
    return model.model_validate(envelope.payload)


def parse(data: dict[str, Any]) -> tuple[WireEnvelope, BaseModel]:
    """Two-step parse: validate the frame, then dispatch and validate the body."""
    envelope = WireEnvelope.model_validate(data)
    return envelope, unwrap(envelope)
