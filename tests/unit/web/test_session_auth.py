"""Tests for web/session_auth.py: the local-mode session store and the
SEC-06 bootstrap-code store (docs/security-remediation-plan.md, Phase 1
item 1.2). Mirrors test_org_session.py's own pattern for OrgSessionStore --
LocalSessionStore is deliberately its local-mode counterpart, minus the
per-principal identity org mode needs and local mode doesn't.
"""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response

from privacyfence.web import session_auth as sa


def _request_with_cookie(cookie_value: str | None) -> Request:
    headers = []
    if cookie_value is not None:
        headers.append((b"cookie", f"{sa.SESSION_COOKIE}={cookie_value}".encode()))
    scope = {"type": "http", "headers": headers, "method": "GET", "path": "/"}
    return Request(scope)


class TestLocalSessionStore:
    def test_create_then_touch_returns_true(self):
        store = sa.LocalSessionStore()
        session_id = store.create()
        assert store.touch(session_id) is True

    def test_unknown_session_id_returns_false(self):
        store = sa.LocalSessionStore()
        assert store.touch("does-not-exist") is False

    def test_two_sessions_get_two_distinct_ids(self):
        store = sa.LocalSessionStore()
        a = store.create()
        b = store.create()
        assert a != b

    def test_idle_expired_session_is_dropped(self, monkeypatch):
        store = sa.LocalSessionStore(idle_timeout_seconds=60, absolute_timeout_seconds=10_000)
        fake_now = [1000.0]
        monkeypatch.setattr(sa.time, "time", lambda: fake_now[0])
        session_id = store.create()

        fake_now[0] += 120  # older than idle_timeout_seconds
        assert store.touch(session_id) is False
        assert store.session_count == 0

    def test_touch_slides_the_idle_timeout_forward(self, monkeypatch):
        store = sa.LocalSessionStore(idle_timeout_seconds=60, absolute_timeout_seconds=10_000)
        fake_now = [1000.0]
        monkeypatch.setattr(sa.time, "time", lambda: fake_now[0])
        session_id = store.create()

        fake_now[0] += 50  # inside the window -- touches last_seen_at
        assert store.touch(session_id) is True
        fake_now[0] += 50  # would be expired from creation, but not from the touch above
        assert store.touch(session_id) is True

    def test_absolute_expired_session_is_dropped_even_with_recent_activity(self, monkeypatch):
        # The idle timeout alone can't save a session past its absolute
        # cap -- SEC-06's own "cookie with idle + absolute expiry": a
        # session touched every minute for a week must still die once the
        # absolute timeout from *creation* lapses.
        store = sa.LocalSessionStore(idle_timeout_seconds=10_000, absolute_timeout_seconds=100)
        fake_now = [1000.0]
        monkeypatch.setattr(sa.time, "time", lambda: fake_now[0])
        session_id = store.create()

        fake_now[0] += 50
        assert store.touch(session_id) is True  # well within the idle window
        fake_now[0] += 60  # 110s since creation -- past the absolute cap
        assert store.touch(session_id) is False
        assert store.session_count == 0

    def test_destroy_removes_the_session(self):
        store = sa.LocalSessionStore()
        session_id = store.create()
        store.destroy(session_id)
        assert store.touch(session_id) is False

    def test_destroy_unknown_session_is_a_no_op(self):
        store = sa.LocalSessionStore()
        store.destroy("does-not-exist")  # must not raise

    def test_session_count_reflects_live_sessions(self):
        store = sa.LocalSessionStore()
        assert store.session_count == 0
        store.create()
        assert store.session_count == 1


class TestBootstrapStore:
    def test_mint_then_consume_succeeds_exactly_once(self):
        store = sa.BootstrapStore()
        code = store.mint()
        assert store.consume(code) is True
        assert store.consume(code) is False  # single-use -- burned by the line above

    def test_two_mints_produce_distinct_codes(self):
        store = sa.BootstrapStore()
        assert store.mint() != store.mint()

    def test_unknown_code_is_rejected(self):
        store = sa.BootstrapStore()
        assert store.consume("not-a-real-code") is False

    def test_empty_code_is_rejected(self):
        store = sa.BootstrapStore()
        assert store.consume("") is False

    def test_expired_code_is_rejected_and_still_consumed(self, monkeypatch):
        store = sa.BootstrapStore(ttl_seconds=60)
        fake_now = [1000.0]
        monkeypatch.setattr(sa.time, "time", lambda: fake_now[0])
        code = store.mint()

        fake_now[0] += 120  # past the TTL

        assert store.consume(code) is False
        # ...and it's gone either way -- a second attempt (e.g. a replay
        # racing the first) doesn't get to try again just because the
        # first attempt failed on expiry rather than success.
        fake_now[0] = 1000.0  # even rewinding time doesn't resurrect it
        assert store.consume(code) is False


class TestAuthenticated:
    # authenticated() reads the session cookie only -- there is no ``?token=``
    # (or ``?bootstrap=``) query-string path here to unit-test any more.
    # Pre-SEC-06, this module's authenticated() *was* the ``?token=`` check;
    # SEC-06 (module docstring above) replaced that with the cookie-based
    # LocalSessionStore this class exercises, and moved the query string to
    # a one-time, pre-authentication *exchange* (BOOTSTRAP_QUERY_PARAM,
    # consumed by web/server.py before authenticated() is ever called, not
    # by authenticated() itself). The regression coverage that an old-style
    # ``?token=<secret>`` URL is no longer honored by any route lives at the
    # route level in test_server.py's TestBootstrapFlow (it needs a live app
    # and dispatcher to demonstrate a *route*, not this function, rejects
    # it) -- see that class's own comment for the assertion.

    def test_no_cookie_is_not_authenticated(self):
        store = sa.LocalSessionStore()
        assert sa.authenticated(_request_with_cookie(None), store) is False

    def test_valid_cookie_authenticates(self):
        store = sa.LocalSessionStore()
        session_id = store.create()
        assert sa.authenticated(_request_with_cookie(session_id), store) is True

    def test_forged_cookie_is_not_authenticated(self):
        store = sa.LocalSessionStore()
        store.create()
        assert sa.authenticated(_request_with_cookie("forged-session-id"), store) is False

    def test_query_string_alone_does_not_authenticate(self):
        # No cookie at all, only a query param shaped like the retired
        # token link -- must not authenticate. authenticated() never even
        # looks at query_params, but this pins that contract so a future
        # change can't silently reintroduce a URL-carried credential here.
        store = sa.LocalSessionStore()
        session_id = store.create()
        scope = {
            "type": "http", "headers": [], "method": "GET", "path": "/",
            "query_string": f"token={session_id}".encode(),
        }
        assert sa.authenticated(Request(scope), store) is False


class TestCsrfAndOrigin:
    def test_matching_cookie_and_csrf_value_passes(self):
        request = _request_with_cookie("sess-abc")
        assert sa.check_csrf(request, "sess-abc") is True

    def test_mismatched_csrf_value_fails(self):
        request = _request_with_cookie("sess-abc")
        assert sa.check_csrf(request, "sess-different") is False

    def test_missing_cookie_fails_even_with_a_csrf_value(self):
        request = _request_with_cookie(None)
        assert sa.check_csrf(request, "sess-abc") is False

    def test_missing_csrf_value_fails_even_with_a_cookie(self):
        request = _request_with_cookie("sess-abc")
        assert sa.check_csrf(request, None) is False
        assert sa.check_csrf(request, "") is False

    def test_no_origin_header_is_accepted(self):
        scope = {"type": "http", "headers": [], "method": "POST", "path": "/"}
        assert sa.check_origin(Request(scope)) is True

    def test_matching_origin_is_accepted(self):
        scope = {
            "type": "http", "method": "POST", "path": "/",
            "headers": [(b"host", b"localhost:8765"), (b"origin", b"http://localhost:8765")],
            "scheme": "http", "server": ("localhost", 8765),
        }
        assert sa.check_origin(Request(scope)) is True

    def test_mismatched_origin_is_rejected(self):
        scope = {
            "type": "http", "method": "POST", "path": "/",
            "headers": [(b"host", b"localhost:8765"), (b"origin", b"https://evil.example.com")],
            "scheme": "http", "server": ("localhost", 8765),
        }
        assert sa.check_origin(Request(scope)) is False

    def test_check_csrf_compares_via_hmac_compare_digest(self, monkeypatch):
        # Not just "produces the right answer" -- check_csrf's own docstring
        # promises a constant-time compare specifically to avoid a timing
        # side-channel on the session id. Spy on hmac.compare_digest itself
        # so a future rewrite to a plain ``==`` (equally correct, but
        # timing-unsafe) fails this test even though every other assertion
        # in this class would still pass.
        calls = []
        real_compare_digest = sa.hmac.compare_digest
        monkeypatch.setattr(
            sa.hmac, "compare_digest",
            lambda a, b: calls.append((a, b)) or real_compare_digest(a, b),
        )
        request = _request_with_cookie("sess-abc")

        assert sa.check_csrf(request, "sess-abc") is True

        assert calls == [("sess-abc", "sess-abc")]


class TestSessionCookieHelpers:
    def test_set_session_cookie_is_httponly_samesite_strict(self):
        response = Response()
        sa.set_session_cookie(response, "sess-123")
        set_cookie = response.headers.get("set-cookie", "")
        assert "sess-123" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "samesite=strict" in set_cookie.lower()

    def test_set_session_cookie_omits_secure_in_local_mode(self):
        # The flag that actually differs between the two modes (module
        # docstring; contrast web/org_session.py's set_session_cookie,
        # which passes secure=True). Local mode's own transport is
        # deliberate plain-HTTP loopback (D1, security-and-compliance.md),
        # and a Secure cookie is silently *dropped* by the browser over
        # plain HTTP -- so asserting its absence here isn't pedantry, it's
        # the difference between the session cookie working at all and a
        # user stuck unable to sign in.
        response = Response()
        sa.set_session_cookie(response, "sess-123")
        set_cookie = response.headers.get("set-cookie", "")
        assert "secure" not in set_cookie.lower()

    def test_clear_session_cookie_expires_it(self):
        response = Response()
        sa.clear_session_cookie(response)
        set_cookie = response.headers.get("set-cookie", "")
        assert sa.SESSION_COOKIE in set_cookie


class TestVerifyBearerSecret:
    def test_correct_bearer_header_passes(self):
        scope = {"type": "http", "headers": [(b"authorization", b"Bearer s3cr3t")], "method": "POST", "path": "/"}
        assert sa.verify_bearer_secret(Request(scope), "s3cr3t") is True

    def test_wrong_secret_fails(self):
        scope = {"type": "http", "headers": [(b"authorization", b"Bearer wrong")], "method": "POST", "path": "/"}
        assert sa.verify_bearer_secret(Request(scope), "s3cr3t") is False

    def test_missing_header_fails(self):
        scope = {"type": "http", "headers": [], "method": "POST", "path": "/"}
        assert sa.verify_bearer_secret(Request(scope), "s3cr3t") is False

    def test_non_bearer_scheme_fails(self):
        scope = {"type": "http", "headers": [(b"authorization", b"Basic s3cr3t")], "method": "POST", "path": "/"}
        assert sa.verify_bearer_secret(Request(scope), "s3cr3t") is False

    def test_compares_via_hmac_compare_digest(self, monkeypatch):
        # Same reasoning as TestCsrfAndOrigin's equivalent spy: this is the
        # one remaining check against the persistent local install secret
        # (module docstring), so it's exactly where a timing side-channel
        # would matter most if a future edit swapped the constant-time
        # compare for a plain ``==``.
        calls = []
        real_compare_digest = sa.hmac.compare_digest
        monkeypatch.setattr(
            sa.hmac, "compare_digest",
            lambda a, b: calls.append((a, b)) or real_compare_digest(a, b),
        )
        scope = {"type": "http", "headers": [(b"authorization", b"Bearer s3cr3t")], "method": "POST", "path": "/"}

        assert sa.verify_bearer_secret(Request(scope), "s3cr3t") is True

        assert calls == [("s3cr3t", "s3cr3t")]


class TestUnauthorizedHtml:
    def test_is_no_store(self):
        # SEC-18 (docs/security-remediation-plan.md, Phase 3 item 3.5):
        # this page names a live bearer-secret command (the exact
        # ~/.privacyfence/web_token curl invocation) -- it must never be
        # cached, and previously carried no Cache-Control header at all.
        scope = {
            "type": "http", "headers": [], "method": "GET", "path": "/approvals",
            "scheme": "http", "server": ("localhost", 8765),
        }
        response = sa.unauthorized_html(Request(scope))
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"
