"""Issuer-prefix validators for the events wire contract.

Every event and command carries ``metadata.issued_by`` with one of six
reserved prefixes. This module is the runtime check; the corresponding
documentation lives in ``heddle/docs/CONCEPTS.md``.

Reserved prefixes (v7 §4.1):

==================== =============================================
Prefix               Issuer
==================== =============================================
``framework:``       Internal framework projectors P1/P2/P3
``observer:{name}``  Scheduled PF observers
``projector:{name}`` Application projectors emitting events
``user:badge:{id}``  Shop-floor operator via badge scan
``user:system:{c}``  Application-mediated, non-operator-specific
``bridge:{type}``    (Post-M2) gateway/bridge for LLM workers
==================== =============================================

Multi-segment suffix rule (v7 §4.1):
    Each prefix governs only the leading segment(s) up to and including
    its named scope. Everything after is opaque to the validator and
    may contain additional colons. For example, :func:`is_user_issuer`
    accepts::

        user:badge:206
        user:system:emergency_correction:eng-42
        user:system:tool:abc:def

    Validators MUST NOT cap segment count.

Note: :func:`is_user_issuer` covers both ``user:badge:*`` and
``user:system:*`` forms. :func:`is_system_issuer` is a strict subcheck
that the issuer is specifically a ``user:system:*`` value — useful for
code paths that require a non-operator-issued action.
"""

from __future__ import annotations

# Reserved prefix string constants. Exported for callers that need to
# construct issuer strings (e.g. framework projectors building
# ``framework:cascade``, observers building ``observer:pf_job_status``).
FRAMEWORK_PREFIX = "framework:"
OBSERVER_PREFIX = "observer:"
PROJECTOR_PREFIX = "projector:"
USER_PREFIX = "user:"
USER_SYSTEM_PREFIX = "user:system:"
USER_BADGE_PREFIX = "user:badge:"
BRIDGE_PREFIX = "bridge:"

# Ordered tuple of top-level prefixes for "is this issuer recognized at
# all?" checks. Subprefixes (``user:badge:``, ``user:system:``)
# intentionally not listed here — they're sub-namespaces of ``user:``.
RESERVED_PREFIXES: tuple[str, ...] = (
    FRAMEWORK_PREFIX,
    OBSERVER_PREFIX,
    PROJECTOR_PREFIX,
    USER_PREFIX,
    BRIDGE_PREFIX,
)


def is_framework_issuer(issued_by: str) -> bool:
    """True iff ``issued_by`` is framework-internal (P1/P2/P3, bootstrap, etc.).

    Used by ``Aggregate.apply()`` to verify framework-only events such as
    ``InternalFinalized`` are not user-forgeable.
    """
    return issued_by.startswith(FRAMEWORK_PREFIX) and len(issued_by) > len(FRAMEWORK_PREFIX)


def is_observer_issuer(issued_by: str) -> bool:
    """True iff ``issued_by`` is a scheduled PF observer (or any future observer)."""
    return issued_by.startswith(OBSERVER_PREFIX) and len(issued_by) > len(OBSERVER_PREFIX)


def is_projector_issuer(issued_by: str) -> bool:
    """True iff ``issued_by`` is an application projector emitting events.

    Distinct from framework projectors (P1/P2/P3) which use ``framework:*``.
    """
    return issued_by.startswith(PROJECTOR_PREFIX) and len(issued_by) > len(PROJECTOR_PREFIX)


def is_user_issuer(issued_by: str) -> bool:
    """True iff ``issued_by`` is user-mediated (badge scan or application).

    Covers both ``user:badge:*`` (shop-floor operator) and
    ``user:system:*`` (application-mediated). Accepts arbitrary trailing
    segments — see module docstring on multi-segment suffix rule.
    """
    return issued_by.startswith(USER_PREFIX) and len(issued_by) > len(USER_PREFIX)


def is_system_issuer(issued_by: str) -> bool:
    """True iff ``issued_by`` is application-mediated (``user:system:*`` specifically).

    Stricter than :func:`is_user_issuer` — rejects shop-floor
    ``user:badge:*`` values. Useful for code paths that must reject
    operator-initiated actions (e.g. emergency-correction tooling per
    §4.12).
    """
    return issued_by.startswith(USER_SYSTEM_PREFIX) and len(issued_by) > len(USER_SYSTEM_PREFIX)


def is_bridge_issuer(issued_by: str) -> bool:
    """True iff ``issued_by`` is a gateway/bridge (post-M2; reserved for future use)."""
    return issued_by.startswith(BRIDGE_PREFIX) and len(issued_by) > len(BRIDGE_PREFIX)


def is_recognized_issuer(issued_by: str) -> bool:
    """True iff ``issued_by`` starts with any reserved prefix.

    Convenience helper for "did this come from a known issuer class at
    all?" Does NOT validate that the suffix is well-formed for the
    prefix's scheme — only that the leading prefix is one of the five
    reserved top-level prefixes.
    """
    return any(issued_by.startswith(p) for p in RESERVED_PREFIXES)
