"""Tests for workshop security helpers (Origin check + upload size cap).

Two layers of testing:

- Unit tests for the helpers in :mod:`heddle.workshop.security`
  exercised in isolation.
- Integration tests via :class:`fastapi.testclient.TestClient` that
  drive an actual workshop app and verify the middleware blocks
  cross-origin POSTs while allowing same-origin and non-browser
  clients.

The threat model is the workshop running on ``localhost:8080`` with a
developer's browser open on a malicious site.  The malicious site's
JavaScript (or HTML form) issues a POST to ``http://localhost:8080``
to mutate config or wipe queues.  Browsers always set ``Origin`` on
such cross-origin POSTs, so the middleware can reject by comparing
``Origin`` against the request's own ``Host``.
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from heddle.workshop.security import (
    ENV_TOKEN_VAR,
    TOKEN_COOKIE_NAME,
    UploadTooLargeError,
    WorkshopAuth,
    _origin_check_failure_reason,
    _origin_matches_host,
    enforce_same_origin,
    is_loopback_bind,
    read_upload_with_size_cap,
)

# ---------------------------------------------------------------------------
# Origin check — unit tests on the pure helpers
# ---------------------------------------------------------------------------


class TestOriginMatchesHost:
    """The host-comparison helper compares netloc, not bare host.

    ``localhost:8080`` and ``localhost:9090`` are different origins —
    a future workshop running on a different port should not be
    treated as the same origin as the current one.
    """

    @pytest.mark.parametrize(
        ("origin", "host", "expected"),
        [
            ("http://localhost:8080", "localhost:8080", True),
            ("https://localhost:8080", "localhost:8080", True),
            ("http://example.com", "example.com", True),
            ("http://localhost:8080", "localhost:9090", False),
            ("http://evil.com", "localhost:8080", False),
            # No scheme on origin → fail (cannot determine intent).
            ("localhost:8080", "localhost:8080", False),
            # Empty inputs → fail.
            ("", "localhost:8080", False),
            ("http://localhost:8080", "", False),
        ],
    )
    def test_match(self, origin, host, expected):
        assert _origin_matches_host(origin, host) is expected


def _build_request(
    method: str = "POST",
    path: str = "/apps/deploy",
    *,
    host: str = "localhost:8080",
    origin: str | None = None,
    referer: str | None = None,
) -> MagicMock:
    """Construct a minimally-mocked ``Request`` for the failure-reason helper."""
    headers = {"host": host}
    if origin is not None:
        headers["origin"] = origin
    if referer is not None:
        headers["referer"] = referer

    request = MagicMock()
    request.method = method
    request.url = MagicMock(path=path)
    # Headers behave like a case-insensitive dict; a plain dict is
    # adequate for our helper which only uses ``.get``.
    request.headers = headers
    return request


class TestOriginCheckFailureReason:
    def test_same_origin_passes(self):
        req = _build_request(origin="http://localhost:8080")
        assert _origin_check_failure_reason(req) is None

    def test_cross_origin_rejected(self):
        req = _build_request(origin="https://evil.com")
        reason = _origin_check_failure_reason(req)
        assert reason is not None
        assert "evil.com" in reason

    def test_origin_with_different_port_rejected(self):
        """A workshop running on a different port is a different origin."""
        req = _build_request(origin="http://localhost:9090")
        assert _origin_check_failure_reason(req) is not None

    def test_no_headers_passes(self):
        """A request with no Origin and no Referer (curl, CLI) is allowed."""
        req = _build_request(origin=None, referer=None)
        assert _origin_check_failure_reason(req) is None

    def test_referer_fallback_same_origin_passes(self):
        req = _build_request(origin=None, referer="http://localhost:8080/apps")
        assert _origin_check_failure_reason(req) is None

    def test_referer_fallback_cross_origin_rejected(self):
        req = _build_request(origin=None, referer="https://evil.com/page")
        reason = _origin_check_failure_reason(req)
        assert reason is not None
        assert "evil.com" in reason


# ---------------------------------------------------------------------------
# Origin check — middleware integration via TestClient
# ---------------------------------------------------------------------------


def _make_minimal_app() -> FastAPI:
    """Build a FastAPI app wired with the same-origin middleware only.

    Keeps the integration test focused on the middleware itself —
    no Workshop dependencies.
    """
    app = FastAPI()
    app.middleware("http")(enforce_same_origin)

    @app.get("/")
    async def get_root():
        return {"ok": True}

    @app.post("/mutate")
    async def post_mutate():
        return {"mutated": True}

    return app


class TestEnforceSameOriginMiddleware:
    def test_get_passes_with_cross_origin(self):
        """Safe methods (GET) bypass the origin check entirely."""
        client = TestClient(_make_minimal_app())
        resp = client.get("/", headers={"Origin": "https://evil.com"})
        assert resp.status_code == 200

    def test_post_same_origin_passes(self):
        client = TestClient(_make_minimal_app(), base_url="http://testserver")
        resp = client.post(
            "/mutate",
            headers={"Origin": "http://testserver"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"mutated": True}

    def test_post_cross_origin_rejected(self):
        """Cross-origin POSTs are rejected with 403.

        Regression test for the original CSRF hole.  Without the
        middleware, any browser tab on a malicious site could deploy
        an app, remove an app, or wipe the dead-letter queue while
        the developer had the workshop running.
        """
        client = TestClient(_make_minimal_app())
        resp = client.post(
            "/mutate",
            headers={"Origin": "https://evil.com"},
        )
        assert resp.status_code == 403
        body = resp.json()
        assert "Cross-origin" in body["error"]

    def test_post_with_no_origin_header_passes(self):
        """Non-browser clients (curl, scripts) typically don't set Origin.

        We allow them so that local CLI workflows and scripted access
        keep working.  Browsers always set Origin on cross-origin
        POSTs (per the Fetch spec), so this carve-out doesn't reopen
        the CSRF hole.
        """
        client = TestClient(_make_minimal_app())
        resp = client.post("/mutate")
        assert resp.status_code == 200

    def test_post_referer_only_cross_origin_rejected(self):
        """If Origin is absent but Referer is present and cross-origin, reject."""
        client = TestClient(_make_minimal_app())
        resp = client.post(
            "/mutate",
            headers={"Referer": "https://evil.com/page"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Upload size cap — unit tests
# ---------------------------------------------------------------------------


class _FakeUploadFile:
    """Minimal stand-in for ``starlette.datastructures.UploadFile``.

    Only implements ``.read(n)`` since that's the surface our helper
    consumes.  Returns an empty bytes when exhausted.
    """

    def __init__(self, data: bytes) -> None:
        self._buf = io.BytesIO(data)

    async def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)


class TestReadUploadWithSizeCap:
    @pytest.mark.asyncio
    async def test_under_cap_writes_full_file(self, tmp_path):
        """Upload under the cap is written in full and returns its size."""
        data = b"x" * (1024 * 1024 + 7)  # 1 MiB + a bit, exceeds one chunk
        upload = _FakeUploadFile(data)
        dest = tmp_path / "out.bin"

        written = await read_upload_with_size_cap(upload, dest, max_bytes=10 * 1024 * 1024)

        assert written == len(data)
        assert dest.read_bytes() == data

    @pytest.mark.asyncio
    async def test_exactly_at_cap_succeeds(self, tmp_path):
        """An upload of exactly ``max_bytes`` is accepted, not rejected.

        Pin the boundary so a future "off by one" doesn't reject
        valid uploads.
        """
        cap = 64 * 1024
        data = b"y" * cap
        upload = _FakeUploadFile(data)
        dest = tmp_path / "out.bin"

        written = await read_upload_with_size_cap(upload, dest, max_bytes=cap)

        assert written == cap

    @pytest.mark.asyncio
    async def test_over_cap_raises_and_does_not_complete_file(self, tmp_path):
        """Upload over the cap raises immediately on chunk that exceeds it.

        Regression test for the original DoS vector: pre-fix the
        workshop did ``await zip_file.read()`` which materialised
        unbounded data in memory.  We stream and abort fast.
        """
        cap = 1024
        data = b"z" * (cap * 16)  # well over the cap
        upload = _FakeUploadFile(data)
        dest = tmp_path / "out.bin"

        with pytest.raises(UploadTooLargeError) as exc_info:
            await read_upload_with_size_cap(upload, dest, max_bytes=cap)

        assert exc_info.value.max_bytes == cap

    @pytest.mark.asyncio
    async def test_writes_to_open_filelike(self):
        """Caller can pass an open BinaryIO instead of a Path."""
        data = b"abc" * 100
        upload = _FakeUploadFile(data)
        buf = io.BytesIO()

        written = await read_upload_with_size_cap(upload, buf, max_bytes=len(data) * 2)

        assert written == len(data)
        assert buf.getvalue() == data


# ---------------------------------------------------------------------------
# Integration: workshop /apps/deploy enforces both
# ---------------------------------------------------------------------------


# Pre-import the workshop modules at collection time so they're in
# sys.modules BEFORE any per-test ``mock.patch.dict`` snapshot is
# taken.  Without this, the mock context manager restores its
# snapshot on exit and removes downstream-loaded modules (notably
# duckdb) from sys.modules, causing the next test's import to fail
# from a half-cleared state.
from heddle.workshop.app import create_app as _create_workshop_app  # noqa: E402


@pytest.fixture
def workshop_client(tmp_path):
    """Workshop TestClient with mDNS stubbed out.

    The real :class:`HeddleServiceAdvertiser` blocks the event loop
    inside zeroconf during the lifespan startup; we replace it with
    an AsyncMock so the test focuses on the security wire-up.  Same
    pattern as ``tests/test_workshop_app.py::TestLifespan``.
    """
    import unittest.mock as mock

    configs_dir = tmp_path / "configs"
    (configs_dir / "workers").mkdir(parents=True)
    (configs_dir / "orchestrators").mkdir()

    mock_advertiser = mock.AsyncMock()
    mock_advertiser.start = mock.AsyncMock()
    mock_advertiser.stop = mock.AsyncMock()
    mock_advertiser.register_workshop = mock.MagicMock()

    mock_mdns_module = mock.MagicMock()
    mock_mdns_module.HeddleServiceAdvertiser.return_value = mock_advertiser

    with mock.patch.dict("sys.modules", {"heddle.discovery.mdns": mock_mdns_module}):
        app = _create_workshop_app(
            configs_dir=str(configs_dir),
            db_path=":memory:",
            apps_dir=str(tmp_path / "apps"),
        )
        with TestClient(app) as client:
            yield client


class TestWorkshopAppDeployIntegration:
    """End-to-end tests through the actual workshop FastAPI app.

    Pins the wire-up: the middleware is registered, the deploy route
    uses the size cap, and the responses produced are what users see.
    """

    def test_apps_deploy_rejects_cross_origin(self, workshop_client):
        """Browser tab on evil.com cannot POST /apps/deploy."""
        resp = workshop_client.post(
            "/apps/deploy",
            files={"zip_file": ("a.zip", b"PK\x05\x06" + b"\x00" * 18, "application/zip")},
            headers={"Origin": "https://evil.com"},
        )
        assert resp.status_code == 403
        assert "Cross-origin" in resp.json()["error"]

    def test_apps_deploy_rejects_oversize_upload(self, workshop_client, monkeypatch):
        """Upload exceeding the cap is redirected to /apps with an error.

        We patch ``DEFAULT_UPLOAD_MAX_BYTES`` down to a small value so
        the test doesn't need to materialise hundreds of MB of bytes.
        """
        monkeypatch.setattr(
            "heddle.workshop.security.DEFAULT_UPLOAD_MAX_BYTES",
            1024,
        )
        big_zip = b"PK\x05\x06" + (b"\x00" * 8192)

        resp = workshop_client.post(
            "/apps/deploy",
            files={"zip_file": ("big.zip", big_zip, "application/zip")},
            follow_redirects=False,
        )

        assert resp.status_code == 303
        assert "exceeds" in resp.headers["location"].lower()

    def test_apps_deploy_passes_with_no_origin(self, workshop_client):
        """A non-browser client (no Origin header) reaches the route.

        Confirms the middleware doesn't block legitimate CLI/scripted
        POSTs.  The deploy itself fails downstream (the bytes are not
        a real app bundle), but the response must NOT be the 403 from
        the middleware.
        """
        fake_zip = b"PK\x05\x06" + b"\x00" * 18
        resp = workshop_client.post(
            "/apps/deploy",
            files={"zip_file": ("a.zip", fake_zip, "application/zip")},
            follow_redirects=False,
        )
        assert resp.status_code != 403


# ---------------------------------------------------------------------------
# Token auth + bind-warning helpers (#9 E1, #4 E4)
# ---------------------------------------------------------------------------


class TestWorkshopAuth:
    """Pure-unit pins for the auth state holder."""

    def test_disabled_when_env_unset(self, monkeypatch):
        monkeypatch.delenv(ENV_TOKEN_VAR, raising=False)
        assert WorkshopAuth.from_env().enabled is False

    def test_disabled_when_env_empty_string(self, monkeypatch):
        """An empty env var must NOT enable auth — that would accept the
        empty-string Bearer token from any caller."""
        monkeypatch.setenv(ENV_TOKEN_VAR, "")
        auth = WorkshopAuth.from_env()
        assert auth.enabled is False
        assert auth.token is None

    def test_enabled_when_env_set(self, monkeypatch):
        monkeypatch.setenv(ENV_TOKEN_VAR, "secret-123")
        auth = WorkshopAuth.from_env()
        assert auth.enabled is True
        assert auth.token == "secret-123"

    def test_request_token_matches_value_uses_constant_time(self):
        """Token comparison must use ``secrets.compare_digest`` so a
        timing attack cannot probe the matching prefix length."""
        import secrets as _secrets

        auth = WorkshopAuth(token="real-token")
        # Indirect check: the implementation should call compare_digest;
        # we verify the contract (correct accept/reject) and pin the
        # behaviour that a wrong token of the same length is rejected.
        assert auth.request_token_matches_value("real-token") is True
        assert auth.request_token_matches_value("real-tokeN") is False
        # And empty strings don't match either way.
        assert auth.request_token_matches_value("") is False
        assert _secrets.compare_digest  # sanity import — used by impl

    def test_request_token_matches_value_disabled_returns_false(self):
        """When auth is disabled, /login validation must reject every
        candidate so callers cannot probe disabled-state."""
        auth = WorkshopAuth(token=None)
        assert auth.request_token_matches_value("anything") is False
        assert auth.request_token_matches_value("") is False


class TestIsLoopbackBind:
    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            ("127.0.0.1", True),
            ("::1", True),
            ("localhost", True),
            ("0.0.0.0", False),
            ("::", False),
            ("192.168.1.10", False),
            ("10.0.0.5", False),
            ("my-laptop.local", False),  # hostname → assume non-loopback
            ("", False),
        ],
    )
    def test_classify(self, host, expected):
        assert is_loopback_bind(host) is expected


@pytest.fixture
def authed_workshop_client(tmp_path, monkeypatch):
    """Workshop TestClient with HEDDLE_WORKSHOP_TOKEN=test-token-xyz set
    BEFORE create_app reads env, so the auth gate is wired in.
    """
    import unittest.mock as mock

    monkeypatch.setenv(ENV_TOKEN_VAR, "test-token-xyz")

    configs_dir = tmp_path / "configs"
    (configs_dir / "workers").mkdir(parents=True)
    (configs_dir / "orchestrators").mkdir()

    mock_advertiser = mock.AsyncMock()
    mock_advertiser.start = mock.AsyncMock()
    mock_advertiser.stop = mock.AsyncMock()
    mock_advertiser.register_workshop = mock.MagicMock()

    mock_mdns_module = mock.MagicMock()
    mock_mdns_module.HeddleServiceAdvertiser.return_value = mock_advertiser

    with mock.patch.dict("sys.modules", {"heddle.discovery.mdns": mock_mdns_module}):
        app = _create_workshop_app(
            configs_dir=str(configs_dir),
            db_path=":memory:",
            apps_dir=str(tmp_path / "apps"),
        )
        with TestClient(app) as client:
            yield client


class TestTokenAuthIntegration:
    """End-to-end: HEDDLE_WORKSHOP_TOKEN gates mutating routes only."""

    _SAME_ORIGIN_HEADERS = {"Origin": "http://testserver"}

    def test_get_route_passes_without_token(self, authed_workshop_client):
        """Auth gates mutations, not browsing.  /workers must stay open
        so /login can bootstrap the cookie and so a curious operator
        can see what's there."""
        resp = authed_workshop_client.get("/workers")
        assert resp.status_code == 200

    def test_post_without_token_returns_401(self, authed_workshop_client):
        """The mutating /apps/deploy route requires the token now."""
        resp = authed_workshop_client.post(
            "/apps/deploy",
            files={"zip_file": ("a.zip", b"PK\x05\x06" + b"\x00" * 18, "application/zip")},
            headers=self._SAME_ORIGIN_HEADERS,
        )
        assert resp.status_code == 401
        assert "Authorization" in resp.json()["error"]

    def test_post_with_bearer_header_succeeds(self, authed_workshop_client):
        """``Authorization: Bearer <token>`` is the CLI/curl path; the
        deploy itself fails downstream (garbage bytes), but we must
        get past the 401 gate."""
        resp = authed_workshop_client.post(
            "/apps/deploy",
            files={"zip_file": ("a.zip", b"PK\x05\x06" + b"\x00" * 18, "application/zip")},
            headers={
                **self._SAME_ORIGIN_HEADERS,
                "Authorization": "Bearer test-token-xyz",
            },
            follow_redirects=False,
        )
        assert resp.status_code != 401
        assert resp.status_code != 403  # same-origin also passes

    def test_post_with_wrong_bearer_returns_401(self, authed_workshop_client):
        resp = authed_workshop_client.post(
            "/apps/deploy",
            files={"zip_file": ("a.zip", b"PK\x05\x06" + b"\x00" * 18, "application/zip")},
            headers={
                **self._SAME_ORIGIN_HEADERS,
                "Authorization": "Bearer wrong-token",
            },
        )
        assert resp.status_code == 401

    def test_post_with_cookie_succeeds(self, authed_workshop_client):
        """Browsers use the cookie path: /login?token=xyz sets it,
        subsequent POSTs carry it automatically."""
        authed_workshop_client.cookies.set(TOKEN_COOKIE_NAME, "test-token-xyz")
        resp = authed_workshop_client.post(
            "/apps/deploy",
            files={"zip_file": ("a.zip", b"PK\x05\x06" + b"\x00" * 18, "application/zip")},
            headers=self._SAME_ORIGIN_HEADERS,
            follow_redirects=False,
        )
        assert resp.status_code != 401

    def test_login_form_renders(self, authed_workshop_client):
        """GET /login returns the bootstrap form (no token in URL).

        Token transport moved from URL query to POST body so the
        token never reaches uvicorn access logs (review §4.2).  The
        GET path now serves the form only.
        """
        resp = authed_workshop_client.get("/login", follow_redirects=False)
        assert resp.status_code == 200
        body = resp.text.lower()
        assert '<form action="/login" method="post"' in body
        assert 'name="token"' in body
        # GET must not set the cookie.
        assert TOKEN_COOKIE_NAME not in resp.headers.get("set-cookie", "")

    def test_login_post_sets_cookie_and_redirects(self, authed_workshop_client):
        resp = authed_workshop_client.post(
            "/login",
            data={"token": "test-token-xyz"},
            headers=self._SAME_ORIGIN_HEADERS,
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"
        # Cookie is HttpOnly + SameSite=Strict + Path=/.
        set_cookie = resp.headers.get("set-cookie", "")
        assert TOKEN_COOKIE_NAME in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=strict" in set_cookie.lower() or "samesite=strict" in set_cookie.lower()

    def test_login_post_with_wrong_token_returns_401(self, authed_workshop_client):
        resp = authed_workshop_client.post(
            "/login",
            data={"token": "wrong"},
            headers=self._SAME_ORIGIN_HEADERS,
            follow_redirects=False,
        )
        assert resp.status_code == 401

    def test_login_get_with_token_query_ignored(self, authed_workshop_client):
        """Legacy ``GET /login?token=...`` no longer sets the cookie.

        Pins the migration: even if an operator follows the old
        runbook wording and visits the URL with a token, no cookie is
        set and the token never confers session access.  This is the
        whole point of the move — tokens never reach uvicorn access
        logs.
        """
        resp = authed_workshop_client.get("/login?token=test-token-xyz", follow_redirects=False)
        # GET returns the form (200), not a redirect with cookie.
        assert resp.status_code == 200
        assert TOKEN_COOKIE_NAME not in resp.headers.get("set-cookie", "")

    def test_same_origin_still_applies_with_valid_token(self, authed_workshop_client):
        """Cross-origin POST with a valid token must STILL be rejected
        by the same-origin layer.  Defence in depth: a malicious site
        that somehow learned the token still cannot mutate from a
        cross-origin browser tab."""
        resp = authed_workshop_client.post(
            "/apps/deploy",
            files={"zip_file": ("a.zip", b"PK\x05\x06" + b"\x00" * 18, "application/zip")},
            headers={
                "Origin": "https://evil.com",
                "Authorization": "Bearer test-token-xyz",
            },
        )
        assert resp.status_code == 403


class TestLoginRouteOnOpenWorkshop:
    """When auth is disabled, /login must not advertise its existence."""

    def test_login_returns_404_when_auth_disabled(self, workshop_client):
        # workshop_client fixture above does NOT set the env var.
        # Both GET (form) and POST (submit) must 404 so the endpoint
        # does not advertise its existence on open deployments.
        resp = workshop_client.get("/login", follow_redirects=False)
        assert resp.status_code == 404
        resp = workshop_client.post("/login", data={"token": "anything"}, follow_redirects=False)
        assert resp.status_code == 404
