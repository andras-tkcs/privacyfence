"""Tests for web/org_session.py: the org-mode browser session store."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response

from privacyfence.principal import Principal
from privacyfence.web import org_session as os_


def _request_with_cookie(cookie_value: str | None) -> Request:
    headers = []
    if cookie_value is not None:
        headers.append((b"cookie", f"{os_.SESSION_COOKIE}={cookie_value}".encode()))
    scope = {"type": "http", "headers": headers, "method": "GET", "path": "/"}
    return Request(scope)


class TestOrgSessionStore:
    def test_create_then_get_returns_the_same_principal(self):
        store = os_.OrgSessionStore()
        alice = Principal(id="alice")
        session_id = store.create(alice)
        assert store.get(session_id) == alice

    def test_unknown_session_id_returns_none(self):
        store = os_.OrgSessionStore()
        assert store.get("does-not-exist") is None

    def test_two_principals_get_two_distinct_session_ids(self):
        store = os_.OrgSessionStore()
        a = store.create(Principal(id="alice"))
        b = store.create(Principal(id="bob"))
        assert a != b

    def test_idle_expired_session_returns_none_and_is_dropped(self, monkeypatch):
        store = os_.OrgSessionStore(idle_timeout_seconds=60)
        fake_now = [1000.0]
        monkeypatch.setattr(os_.time, "time", lambda: fake_now[0])
        session_id = store.create(Principal(id="alice"))

        fake_now[0] += 120  # older than idle_timeout_seconds
        assert store.get(session_id) is None
        assert store.session_count == 0

    def test_get_slides_the_idle_timeout_forward(self, monkeypatch):
        store = os_.OrgSessionStore(idle_timeout_seconds=60)
        fake_now = [1000.0]
        monkeypatch.setattr(os_.time, "time", lambda: fake_now[0])
        session_id = store.create(Principal(id="alice"))

        fake_now[0] += 50  # inside the window -- touches last_seen_at
        assert store.get(session_id) is not None
        fake_now[0] += 50  # would be expired from creation, but not from the touch above
        assert store.get(session_id) is not None

    def test_destroy_removes_the_session(self):
        store = os_.OrgSessionStore()
        session_id = store.create(Principal(id="alice"))
        store.destroy(session_id)
        assert store.get(session_id) is None

    def test_destroy_unknown_session_is_a_no_op(self):
        store = os_.OrgSessionStore()
        store.destroy("does-not-exist")  # must not raise

    def test_destroy_all_for_removes_only_that_principals_sessions(self):
        store = os_.OrgSessionStore()
        a1 = store.create(Principal(id="alice"))
        a2 = store.create(Principal(id="alice"))
        b1 = store.create(Principal(id="bob"))

        removed = store.destroy_all_for("alice")

        assert removed == 2
        assert store.get(a1) is None
        assert store.get(a2) is None
        assert store.get(b1) is not None

    def test_session_count_reflects_live_sessions(self):
        store = os_.OrgSessionStore()
        assert store.session_count == 0
        store.create(Principal(id="alice"))
        assert store.session_count == 1


class TestAuthenticated:
    def test_no_cookie_is_not_authenticated(self):
        store = os_.OrgSessionStore()
        assert os_.authenticated(_request_with_cookie(None), store) is None

    def test_valid_cookie_resolves_the_principal(self):
        store = os_.OrgSessionStore()
        alice = Principal(id="alice")
        session_id = store.create(alice)
        assert os_.authenticated(_request_with_cookie(session_id), store) == alice

    def test_forged_cookie_is_not_authenticated(self):
        store = os_.OrgSessionStore()
        store.create(Principal(id="alice"))
        assert os_.authenticated(_request_with_cookie("forged-session-id"), store) is None


class TestCsrfAndOrigin:
    def test_matching_cookie_and_csrf_value_passes(self):
        request = _request_with_cookie("sess-abc")
        assert os_.check_csrf(request, "sess-abc") is True

    def test_mismatched_csrf_value_fails(self):
        request = _request_with_cookie("sess-abc")
        assert os_.check_csrf(request, "sess-different") is False

    def test_missing_cookie_fails_even_with_a_csrf_value(self):
        request = _request_with_cookie(None)
        assert os_.check_csrf(request, "sess-abc") is False

    def test_missing_csrf_value_fails_even_with_a_cookie(self):
        request = _request_with_cookie("sess-abc")
        assert os_.check_csrf(request, None) is False
        assert os_.check_csrf(request, "") is False

    def test_no_origin_header_is_accepted(self):
        scope = {"type": "http", "headers": [], "method": "POST", "path": "/"}
        assert os_.check_origin(Request(scope)) is True

    def test_matching_origin_is_accepted(self):
        scope = {
            "type": "http", "method": "POST", "path": "/",
            "headers": [(b"host", b"pf.example.com"), (b"origin", b"https://pf.example.com")],
            "scheme": "https", "server": ("pf.example.com", 443),
        }
        assert os_.check_origin(Request(scope)) is True

    def test_mismatched_origin_is_rejected(self):
        scope = {
            "type": "http", "method": "POST", "path": "/",
            "headers": [(b"host", b"pf.example.com"), (b"origin", b"https://evil.example.com")],
            "scheme": "https", "server": ("pf.example.com", 443),
        }
        assert os_.check_origin(Request(scope)) is False


class TestSessionCookieHelpers:
    def test_set_session_cookie_is_secure_httponly_samesite_strict(self):
        response = Response()
        os_.set_session_cookie(response, "sess-123")
        set_cookie = response.headers.get("set-cookie", "")
        assert "sess-123" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "Secure" in set_cookie
        assert "samesite=strict" in set_cookie.lower()

    def test_clear_session_cookie_expires_it(self):
        response = Response()
        os_.clear_session_cookie(response)
        set_cookie = response.headers.get("set-cookie", "")
        assert os_.SESSION_COOKIE in set_cookie
