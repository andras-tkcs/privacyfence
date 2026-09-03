"""Tests for web/routes_org_identity.py: /login, /oauth/idp/login-callback,
/logout -- the org-mode browser sign-in flow."""
from __future__ import annotations

from starlette.applications import Starlette
from starlette.testclient import TestClient

from privacyfence import org_identity as oi
from privacyfence.principal import Principal
from privacyfence.web import org_session, routes_org_identity as roi

BASE_URL = "https://pf.example.com"


def _idp() -> oi.IdpConfig:
    return oi.IdpConfig(
        issuer="https://idp.example.com", client_id="privacyfence", client_secret="s3cr3t",
        authorization_endpoint="https://idp.example.com/authorize",
        token_endpoint="https://idp.example.com/token", jwks_uri="https://idp.example.com/jwks",
    )


def _app(sessions: org_session.OrgSessionStore | None = None):
    sessions = sessions or org_session.OrgSessionStore()
    routes = roi.build_routes(idp=_idp(), sessions=sessions, base_url=BASE_URL)
    app = Starlette(routes=routes)
    return app, sessions


def _client(app) -> TestClient:
    return TestClient(app, base_url=BASE_URL, follow_redirects=False)


class TestLogin:
    def test_redirects_to_the_idps_authorization_endpoint(self):
        app, _sessions = _app()
        r = _client(app).get("/login")
        assert r.status_code == 302
        assert r.headers["location"].startswith("https://idp.example.com/authorize?")

    def test_authorization_url_carries_the_fixed_login_callback_redirect_uri(self):
        import urllib.parse as up

        app, _sessions = _app()
        r = _client(app).get("/login")
        qs = dict(up.parse_qsl(up.urlparse(r.headers["location"]).query))
        assert qs["redirect_uri"] == f"{BASE_URL}{roi.LOGIN_CALLBACK_PATH}"

    def test_no_store_cache_control(self):
        app, _sessions = _app()
        r = _client(app).get("/login")
        assert r.headers.get("cache-control") == "no-store"

    def test_open_redirect_via_next_is_rejected(self, monkeypatch):
        app, sessions = _app()

        def fake_exchange(idp, *, code, redirect_uri, code_verifier):
            return {"id_token": "irrelevant"}

        monkeypatch.setattr(roi.org_identity, "exchange_code_for_tokens", fake_exchange)
        monkeypatch.setattr(
            roi.org_identity, "verify_id_token",
            lambda idp, token, *, nonce: {"sub": "alice", "nonce": nonce},
        )
        client = _client(app)
        r = client.get("/login?next=https://evil.example.com/steal")
        # Extract state from the redirect URL and drive the callback.
        import urllib.parse as up
        qs = dict(up.parse_qsl(up.urlparse(r.headers["location"]).query))
        cb = client.get(f"{roi.LOGIN_CALLBACK_PATH}?code=abc&state={qs['state']}")
        assert cb.headers["location"] == roi.DEFAULT_NEXT_PATH  # not the attacker's URL

    def test_scheme_relative_next_is_rejected(self, monkeypatch):
        app, sessions = _app()
        monkeypatch.setattr(roi.org_identity, "exchange_code_for_tokens", lambda *a, **kw: {"id_token": "t"})
        monkeypatch.setattr(
            roi.org_identity, "verify_id_token", lambda idp, token, *, nonce: {"sub": "alice", "nonce": nonce},
        )
        client = _client(app)
        r = client.get("/login?next=//evil.example.com/steal")
        import urllib.parse as up
        qs = dict(up.parse_qsl(up.urlparse(r.headers["location"]).query))
        cb = client.get(f"{roi.LOGIN_CALLBACK_PATH}?code=abc&state={qs['state']}")
        assert cb.headers["location"] == roi.DEFAULT_NEXT_PATH

    def test_same_origin_next_is_honored(self, monkeypatch):
        app, sessions = _app()
        monkeypatch.setattr(roi.org_identity, "exchange_code_for_tokens", lambda *a, **kw: {"id_token": "t"})
        monkeypatch.setattr(
            roi.org_identity, "verify_id_token", lambda idp, token, *, nonce: {"sub": "alice", "nonce": nonce},
        )
        client = _client(app)
        r = client.get("/login?next=/settings")
        import urllib.parse as up
        qs = dict(up.parse_qsl(up.urlparse(r.headers["location"]).query))
        cb = client.get(f"{roi.LOGIN_CALLBACK_PATH}?code=abc&state={qs['state']}")
        assert cb.headers["location"] == "/settings"


class TestLoginCallback:
    def _drive_login(self, client, monkeypatch, *, claims: dict, next_path: str | None = None):
        monkeypatch.setattr(roi.org_identity, "exchange_code_for_tokens", lambda *a, **kw: {"id_token": "opaque"})
        monkeypatch.setattr(
            roi.org_identity, "verify_id_token",
            lambda idp, token, *, nonce: {**claims, "nonce": nonce},
        )
        login_url = "/login" + (f"?next={next_path}" if next_path else "")
        r = client.get(login_url)
        import urllib.parse as up
        qs = dict(up.parse_qsl(up.urlparse(r.headers["location"]).query))
        return client.get(f"{roi.LOGIN_CALLBACK_PATH}?code=abc123&state={qs['state']}")

    def test_happy_path_sets_a_session_cookie_and_redirects_to_next(self, monkeypatch):
        app, sessions = _app()
        client = _client(app)
        cb = self._drive_login(client, monkeypatch, claims={"sub": "alice", "email": "alice@example.com"})

        assert cb.status_code == 302
        assert cb.headers["location"] == roi.DEFAULT_NEXT_PATH
        assert org_session.SESSION_COOKIE in cb.cookies
        session_id = cb.cookies[org_session.SESSION_COOKIE]
        assert sessions.get(session_id).id == "alice"

    def test_idp_error_param_fails_cleanly(self):
        app, _sessions = _app()
        client = _client(app)
        r = client.get(f"{roi.LOGIN_CALLBACK_PATH}?error=access_denied&state=whatever")
        assert r.status_code == 400

    def test_missing_state_fails_cleanly(self):
        app, _sessions = _app()
        r = _client(app).get(f"{roi.LOGIN_CALLBACK_PATH}?code=abc")
        assert r.status_code == 400

    def test_unknown_state_fails_cleanly(self):
        app, _sessions = _app()
        r = _client(app).get(f"{roi.LOGIN_CALLBACK_PATH}?code=abc&state=never-issued")
        assert r.status_code == 400

    def test_state_is_single_use(self, monkeypatch):
        app, sessions = _app()
        client = _client(app)
        monkeypatch.setattr(roi.org_identity, "exchange_code_for_tokens", lambda *a, **kw: {"id_token": "opaque"})
        monkeypatch.setattr(
            roi.org_identity, "verify_id_token", lambda idp, token, *, nonce: {"sub": "alice", "nonce": nonce},
        )
        r = client.get("/login")
        import urllib.parse as up
        qs = dict(up.parse_qsl(up.urlparse(r.headers["location"]).query))
        state = qs["state"]

        first = client.get(f"{roi.LOGIN_CALLBACK_PATH}?code=abc&state={state}")
        second = client.get(f"{roi.LOGIN_CALLBACK_PATH}?code=abc&state={state}")

        assert first.status_code == 302
        assert second.status_code == 400  # the state was already consumed

    def test_idp_exchange_failure_fails_cleanly_not_500(self, monkeypatch):
        app, _sessions = _app()
        client = _client(app)

        def failing_exchange(*a, **kw):
            raise RuntimeError("IdP unreachable")

        monkeypatch.setattr(roi.org_identity, "exchange_code_for_tokens", failing_exchange)
        r = client.get("/login")
        import urllib.parse as up
        qs = dict(up.parse_qsl(up.urlparse(r.headers["location"]).query))
        cb = client.get(f"{roi.LOGIN_CALLBACK_PATH}?code=abc&state={qs['state']}")
        assert cb.status_code == 400

    def test_idp_token_response_missing_id_token_fails_cleanly_not_500(self, monkeypatch):
        app, _sessions = _app()
        client = _client(app)
        monkeypatch.setattr(roi.org_identity, "exchange_code_for_tokens", lambda *a, **kw: {"access_token": "x"})
        r = client.get("/login")
        import urllib.parse as up
        qs = dict(up.parse_qsl(up.urlparse(r.headers["location"]).query))
        cb = client.get(f"{roi.LOGIN_CALLBACK_PATH}?code=abc&state={qs['state']}")
        assert cb.status_code == 400


class TestLogout:
    def test_clears_the_session_cookie_and_the_server_side_session(self):
        app, sessions = _app()
        alice_session = sessions.create(Principal(id="alice"))
        client = _client(app)
        client.cookies.set(org_session.SESSION_COOKIE, alice_session)

        r = client.post("/logout")

        assert r.status_code == 302
        assert sessions.get(alice_session) is None

    def test_get_is_not_allowed(self):
        app, _sessions = _app()
        r = _client(app).get("/logout")
        assert r.status_code == 405


class TestSafeNextPath:
    """Unit-level coverage of the open-redirect defense -- see
    TestLogin.test_open_redirect_via_next_is_rejected/test_scheme_relative_
    next_is_rejected for the same thing driven through the real /login ->
    callback round trip."""

    def test_none_and_empty_default(self):
        assert roi._safe_next_path(None) == roi.DEFAULT_NEXT_PATH
        assert roi._safe_next_path("") == roi.DEFAULT_NEXT_PATH

    def test_not_starting_with_a_slash_defaults(self):
        assert roi._safe_next_path("evil.example.com") == roi.DEFAULT_NEXT_PATH
        assert roi._safe_next_path("https://evil.example.com") == roi.DEFAULT_NEXT_PATH

    def test_scheme_relative_defaults(self):
        assert roi._safe_next_path("//evil.example.com/steal") == roi.DEFAULT_NEXT_PATH

    def test_leading_backslash_defaults(self):
        # Browsers normalize a leading "\" to "/" before navigating, so
        # "/\evil.example.com" is a protocol-relative redirect to a
        # real browser even though it reads as an ordinary same-origin
        # path to plain Python string/URL handling.
        assert roi._safe_next_path("/\\evil.example.com") == roi.DEFAULT_NEXT_PATH
        assert roi._safe_next_path("\\\\evil.example.com") == roi.DEFAULT_NEXT_PATH

    def test_mixed_slash_and_backslash_defaults(self):
        assert roi._safe_next_path("/\\/evil.example.com") == roi.DEFAULT_NEXT_PATH

    def test_ordinary_same_origin_path_is_honored(self):
        assert roi._safe_next_path("/settings") == "/settings"
        assert roi._safe_next_path("/approvals/abc123") == "/approvals/abc123"


class TestLoginAttemptStorePruning:
    def test_stale_attempts_are_pruned_on_the_next_create(self, monkeypatch):
        store = roi._LoginAttemptStore()
        fake_now = [1000.0]
        monkeypatch.setattr(roi.time, "time", lambda: fake_now[0])
        old_state, _attempt, _challenge = store.create(next_path="/x")

        fake_now[0] += roi._LOGIN_ATTEMPT_TTL_SECONDS + 1
        store.create(next_path="/y")  # triggers _prune()

        assert store.pop(old_state) is None
