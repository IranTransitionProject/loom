"""Workshop security helpers — origin check, upload size cap, optional token auth.

The Workshop is documented as a local developer dashboard, but its
mutating routes (deploy app, remove app, save worker, replay
dead-letter, …) accept POST requests.  Any browser tab the developer
has open can issue cross-origin POSTs to ``http://localhost:8080``
while the workshop is running, mutating config or wiping queues.

This module provides three reusable defences:

- :func:`enforce_same_origin` — an HTTP middleware that rejects
  unsafe-method requests whose ``Origin`` (or ``Referer`` fallback)
  does not match the request's own host.  Browsers always send
  ``Origin`` on cross-origin POSTs and the value cannot be forged
  from page JavaScript, so this is the standard CSRF defence.
- :func:`read_upload_with_size_cap` — streams an :class:`UploadFile`
  to disk in fixed-size chunks and rejects when the cumulative size
  exceeds a cap.  Replaces ``await upload.read()`` which is
  unbounded and OOMs the worker on multi-GB uploads.
- :class:`WorkshopAuth` + :func:`make_token_auth_middleware` — opt-in
  token auth gating mutating routes when the operator sets
  ``HEDDLE_WORKSHOP_TOKEN``.  Default = no auth, which is safe under
  the trusted-local default bind (127.0.0.1).  When the env var is
  set, mutating requests must carry ``Authorization: Bearer <token>``
  or the ``heddle_workshop_token`` cookie (set by visiting
  ``GET /login?token=...`` once).  Same-origin still applies on top.

All helpers are pure utility — no Workshop-specific state beyond what
the operator passes in — so they are easy to unit-test.
"""

from __future__ import annotations

import ipaddress
import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO
from urllib.parse import urlparse

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import Request, Response
    from starlette.datastructures import UploadFile


# Methods that are intentionally exempt from the same-origin check.
# These are non-mutating per HTTP semantics; if a route uses GET to
# perform a mutation, that's a separate bug for the route to fix.
_SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})


# Default cap for ZIP uploads (256 MiB).  Apps shipped through the
# workshop are configuration bundles, not data sets — anything
# materially larger than this is almost certainly a misuse or attack.
DEFAULT_UPLOAD_MAX_BYTES = 256 * 1024 * 1024  # 256 MiB

# Streaming chunk size for upload reads.  Tuned to hold the chunk in
# the small per-chunk buffer rather than the whole file in memory.
_UPLOAD_CHUNK_BYTES = 1024 * 1024  # 1 MiB


class UploadTooLargeError(Exception):
    """Raised when an upload exceeds the configured size cap."""

    def __init__(self, max_bytes: int) -> None:
        super().__init__(f"Upload exceeds the maximum size of {max_bytes} bytes.")
        self.max_bytes = max_bytes


def _origin_matches_host(origin: str, request_host: str) -> bool:
    """Return ``True`` if ``origin`` is on the same host as the request.

    The ``Host`` header lacks a scheme, so we accept both ``http://``
    and ``https://`` variants.  We compare on netloc (host:port)
    rather than bare host so ``localhost:8080`` and
    ``localhost:9090`` are correctly treated as different origins.
    """
    if not origin or not request_host:
        return False
    parsed = urlparse(origin)
    if not parsed.scheme or not parsed.netloc:
        return False
    return parsed.netloc == request_host


def _origin_check_failure_reason(request: Request) -> str | None:
    """Return a reason string if the request fails the origin check, else None.

    The check, in order:

    1. ``Origin`` is set → must match the request's ``Host``.
       Browsers always send ``Origin`` on cross-origin POSTs and the
       value cannot be forged from page JS, so this is the load-bearing
       branch for blocking CSRF.
    2. ``Origin`` is absent → fall back to ``Referer`` if present.
       Some legacy form submissions don't send ``Origin``; ``Referer``
       still appears.  We accept iff ``Referer``'s netloc matches
       ``Host``.
    3. Neither header is set → likely a non-browser client (curl,
       Heddle CLI itself).  Allow.

    Returns ``None`` on success.  On failure returns a short
    log-friendly explanation that the caller can surface in the 403
    response.
    """
    request_host = request.headers.get("host", "")
    origin = request.headers.get("origin")
    if origin is not None:
        if _origin_matches_host(origin, request_host):
            return None
        return f"Origin {origin!r} does not match Host {request_host!r}"

    referer = request.headers.get("referer")
    if referer is not None:
        ref_netloc = urlparse(referer).netloc
        if ref_netloc and ref_netloc != request_host:
            return f"Referer host {ref_netloc!r} does not match Host {request_host!r}"

    # No Origin and no mismatched Referer — non-browser client or
    # same-origin form submission.  Allow.
    return None


async def enforce_same_origin(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """ASGI middleware that rejects cross-origin mutating requests.

    Wire into a FastAPI app via ``app.middleware("http")``.  Returns
    a JSON 403 with a short ``error`` field on rejection; the actual
    cross-origin Origin and Host are logged but not echoed in the
    response body to avoid information leakage.

    Safe methods (GET/HEAD/OPTIONS) are passed through unchanged.
    """
    from fastapi.responses import JSONResponse

    if request.method in _SAFE_METHODS:
        return await call_next(request)

    failure_reason = _origin_check_failure_reason(request)
    if failure_reason is not None:
        # Delay structlog import so this module can be used in tests
        # that don't pull in the full Heddle stack.
        import structlog

        structlog.get_logger().warning(
            "workshop.cross_origin_rejected",
            method=request.method,
            path=request.url.path,
            reason=failure_reason,
        )
        return JSONResponse(
            status_code=403,
            content={
                "error": (
                    "Cross-origin request rejected.  The workshop only accepts "
                    "mutating requests from its own origin."
                ),
            },
        )

    return await call_next(request)


async def read_upload_with_size_cap(
    upload: UploadFile,
    destination: BinaryIO | Path,
    *,
    max_bytes: int | None = None,
) -> int:
    """Stream ``upload`` to ``destination`` in chunks, enforcing ``max_bytes``.

    Earlier the workshop did ``content = await upload.read()`` which
    materialises the whole upload in memory before the size is known.
    A multi-GB POST OOMs the worker before any validation runs.  We
    stream in 1 MiB chunks and raise :class:`UploadTooLargeError` as
    soon as the cumulative size crosses the cap.

    Returns the number of bytes written on success.

    Args:
        upload: A FastAPI/Starlette ``UploadFile``.
        destination: Either an open binary file-like (``BinaryIO``)
            or a :class:`pathlib.Path`.  When a Path is given the file
            is created/truncated and closed at the end.
        max_bytes: Cap in bytes.  ``None`` (the default) reads the
            module-level :data:`DEFAULT_UPLOAD_MAX_BYTES` at call time,
            so tests can patch the constant via ``monkeypatch.setattr``
            without having to thread an override through every caller.
    """
    if max_bytes is None:
        max_bytes = DEFAULT_UPLOAD_MAX_BYTES
    written = 0
    close_after = False
    if isinstance(destination, Path):
        out: BinaryIO = destination.open("wb")
        close_after = True
    else:
        out = destination

    try:
        while True:
            chunk = await upload.read(_UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                raise UploadTooLargeError(max_bytes)
            out.write(chunk)
        return written
    finally:
        if close_after:
            out.close()


# ---------------------------------------------------------------------------
# Optional token auth
# ---------------------------------------------------------------------------

# Env var the operator sets to enable token auth.  Empty string or
# unset → auth is disabled (current default behaviour, safe under
# loopback bind).
ENV_TOKEN_VAR = "HEDDLE_WORKSHOP_TOKEN"

# Cookie name used for the browser-friendly path.  Set by visiting
# ``GET /login?token=...`` once; subsequent forms POST with this
# cookie attached so the operator doesn't have to set the header
# manually on every request.
TOKEN_COOKIE_NAME = "heddle_workshop_token"

# Hosts treated as loopback for the bind-warning check.  ``localhost``
# resolves to a loopback address on every reasonable system; the IPv4
# and IPv6 loopback strings are the literal forms uvicorn accepts.
_LOOPBACK_LITERALS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})


class WorkshopAuth:
    """Holds the active token (or ``None`` when auth is disabled).

    Construct via :meth:`from_env` so callers don't need to know the
    env var name; the constructor itself accepts an explicit token for
    tests.
    """

    def __init__(self, token: str | None = None) -> None:
        # Coerce empty-string-from-env to None so a typo like
        # ``HEDDLE_WORKSHOP_TOKEN=`` doesn't enable auth with the
        # empty string (which would then accept any empty header).
        self.token: str | None = token if token else None

    @classmethod
    def from_env(cls) -> WorkshopAuth:
        """Read ``HEDDLE_WORKSHOP_TOKEN`` from the process environment."""
        return cls(token=os.environ.get(ENV_TOKEN_VAR))

    @property
    def enabled(self) -> bool:
        """``True`` if auth is active (env var is set to a non-empty value)."""
        return self.token is not None

    def request_token_matches_value(self, candidate: str) -> bool:
        """Return ``True`` if ``candidate`` matches the configured token.

        Used by the ``/login`` route to validate the URL-supplied
        token before setting the cookie.  Constant-time comparison.
        Returns ``False`` when auth is disabled so callers cannot
        learn the disabled-state by passing an empty string.
        """
        if self.token is None:
            return False
        return secrets.compare_digest(candidate, self.token)

    def request_token_matches(self, request: Request) -> bool:
        """Return ``True`` if the request carries the configured token.

        Checked sources, in order:

        1. ``Authorization: Bearer <token>`` header — for CLI / curl /
           fetch clients.
        2. ``heddle_workshop_token`` cookie — for browsers that have
           visited ``GET /login?token=...`` once.

        Comparison uses :func:`secrets.compare_digest` so a mismatch
        does not leak the matching prefix length via timing.
        """
        if self.token is None:
            return True  # auth disabled — every request is "valid"
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer ") and secrets.compare_digest(
            auth_header[len("Bearer ") :], self.token
        ):
            return True
        cookie = request.cookies.get(TOKEN_COOKIE_NAME)
        return bool(cookie and secrets.compare_digest(cookie, self.token))


def make_token_auth_middleware(
    auth: WorkshopAuth,
) -> Callable[[Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]]:
    """Build an HTTP middleware that gates mutating routes on ``auth``.

    No-op when ``auth.enabled`` is False, so the default (no env var)
    keeps the workshop's current open-access behaviour for local use.
    Safe methods (GET/HEAD/OPTIONS) pass through unchanged — auth is
    about preventing unauthorised mutations, not browse-blocking, and
    keeping GET open lets the browser-friendly ``/login`` route
    bootstrap the cookie.
    """
    from fastapi.responses import JSONResponse

    async def middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not auth.enabled or request.method in _SAFE_METHODS:
            return await call_next(request)
        if auth.request_token_matches(request):
            return await call_next(request)
        # Delay structlog import so the helper stays light when used
        # by tests that don't pull in the full Heddle stack.
        import structlog

        structlog.get_logger().warning(
            "workshop.token_auth_rejected",
            method=request.method,
            path=request.url.path,
        )
        return JSONResponse(
            status_code=401,
            content={
                "error": (
                    "Unauthorized.  Send 'Authorization: Bearer <token>' or "
                    "set the heddle_workshop_token cookie via "
                    "GET /login?token=...  HEDDLE_WORKSHOP_TOKEN must match."
                ),
            },
        )

    return middleware


def is_loopback_bind(host: str) -> bool:
    """Return ``True`` when ``host`` binds the workshop to a loopback address only.

    ``localhost``, ``127.0.0.1``, and ``::1`` are the hostnames uvicorn
    documents for loopback-only.  Any other value (``0.0.0.0``,
    ``::``, a LAN IP, an interface name, ...) reaches the network and
    should make the operator answer "is auth set?".
    """
    if host in _LOOPBACK_LITERALS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Hostname (e.g. ``my-laptop.local``) — assume non-loopback so
        # we err on the side of warning the operator.
        return False
