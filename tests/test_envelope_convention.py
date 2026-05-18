"""Tests pinning the underscore-prefix middleware-lane convention.

See ``heddle-workspace/anchors/CONTRACT_MAP.md`` "Reserved
middleware lane" for the convention itself. These tests pin its
*runtime* behaviour: middleware-prefixed wire keys must coexist with
the typed Pydantic envelope without breaking validation, contaminating
``model_dump`` output, or being silently dropped.

The companion lint script ``tools/check_envelope_convention.py`` enforces
the structural rules (no underscore-prefixed Pydantic fields; no
non-underscore keys in middleware modules; no underscore properties
in schemas). These tests cover what the lint can't: runtime contract.

If any of these tests fail, the most likely cause is a refactor of
``model_config`` (e.g. flipping ``extra`` from the default
``"ignore"`` to ``"forbid"``) or a regression in the
``inject_trace_context`` / ``extract_trace_context`` helpers.
"""

from __future__ import annotations

from heddle.core.messages import ModelTier, TaskMessage
from heddle.tracing.otel import extract_trace_context, inject_trace_context


def _make_task() -> TaskMessage:
    return TaskMessage(
        worker_type="echo",
        payload={"text": "hello"},
        model_tier=ModelTier.LOCAL,
    )


def test_model_dump_emits_no_underscore_prefixed_keys() -> None:
    """A fresh ``TaskMessage.model_dump()`` contains only declared
    application fields. Middleware fields are not part of the typed
    envelope and must not appear unless explicitly injected after the
    dump (which is the actual convention — see
    ``TaskWorker._publish_result``).
    """
    dumped = _make_task().model_dump(mode="json")
    underscore_keys = [k for k in dumped if k.startswith("_")]
    assert not underscore_keys, (
        f"model_dump emitted underscore-prefixed key(s) {underscore_keys}; "
        f"middleware-lane fields must not appear in the typed envelope's "
        f"serialization output"
    )


def test_middleware_field_round_trips_through_model_validate() -> None:
    """A wire dict carrying ``_trace_context`` (and any other future
    middleware key) must survive ``TaskMessage.model_validate`` without
    raising. This is the load-bearing test for the convention's
    coexistence promise — without it, a sender that injects
    ``_trace_context`` would crash any receiver running the typed
    Pydantic envelope.
    """
    data = _make_task().model_dump(mode="json")
    data["_trace_context"] = {"traceparent": "00-deadbeef-feedface-01"}
    data["_some_future_middleware_field"] = {"any": "shape"}

    # Must not raise — extras are tolerated by default Pydantic config.
    revalidated = TaskMessage.model_validate(data)

    # And the middleware fields must not leak into the typed envelope's
    # serialization output — they're propagated on the dict, not the model.
    revalidated_dump = revalidated.model_dump(mode="json")
    assert "_trace_context" not in revalidated_dump
    assert "_some_future_middleware_field" not in revalidated_dump


def test_pydantic_extra_tolerates_unknown_underscore_keys() -> None:
    """If the Pydantic ``model_config`` ever regresses to
    ``extra="forbid"``, every middleware-injected key would start
    raising ``ValidationError`` on receivers — a silent wire break.
    This test fails loudly in that scenario.

    Constructed via ``model_validate`` (not direct constructor) since
    extras handling is a validation-time policy.
    """
    base = _make_task().model_dump(mode="json")
    base["_arbitrary_middleware_key"] = "anything"
    # Must succeed. If this raises ``ValidationError``, the model_config
    # has flipped to ``forbid`` and the middleware-lane convention is broken.
    TaskMessage.model_validate(base)


def test_inject_extract_uses_underscore_key_only() -> None:
    """Inject + extract round-trip and only touch the ``_trace_context``
    key on the carrier. Pins the contract for the tagged middleware
    module's wire-key shape.
    """
    carrier: dict = _make_task().model_dump(mode="json")
    pre_keys = set(carrier.keys())

    inject_trace_context(carrier)

    new_keys = set(carrier.keys()) - pre_keys
    # Either no new key (no active OTel span / OTel not installed) or
    # exactly the reserved underscore key. Never anything else.
    assert new_keys <= {"_trace_context"}, (
        f"inject_trace_context added unexpected keys {new_keys}; the only "
        f"key the tracing middleware is allowed to write is `_trace_context`"
    )

    # Extract must accept the same dict back without raising, regardless
    # of whether the key is present.
    _ = extract_trace_context(carrier)
