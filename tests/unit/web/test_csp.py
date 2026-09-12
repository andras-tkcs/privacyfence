"""Tests for web/csp.py -- the shared nonce/CSP helpers (SEC-08).
web/test_server.py covers the middleware/header-emission side end-to-end;
this module covers the small pure helpers in isolation.
"""
from __future__ import annotations

from starlette.requests import Request

from privacyfence.web.csp import new_nonce, nonce_for, set_nonce


def _request(state: dict | None = None) -> Request:
    scope = {"type": "http", "headers": [], "state": state if state is not None else {}}
    return Request(scope)


class TestNewNonce:
    def test_returns_a_non_empty_string(self):
        assert isinstance(new_nonce(), str)
        assert new_nonce()

    def test_two_calls_differ(self):
        assert new_nonce() != new_nonce()


class TestNonceFor:
    def test_reads_the_value_set_by_the_middleware(self):
        request = _request({"csp_nonce": "from-middleware"})
        assert nonce_for(request) == "from-middleware"

    def test_falls_back_to_a_fresh_value_when_middleware_absent(self):
        # Several route modules' own tests build a bare create_app() with
        # none of web/server.py's middleware stack -- this must not raise.
        request = _request({})
        assert nonce_for(request)


class TestSetNonce:
    def test_overrides_what_nonce_for_then_returns(self):
        request = _request({"csp_nonce": "original"})
        set_nonce(request, "overridden")
        assert nonce_for(request) == "overridden"

    def test_visible_on_the_same_scope_state_dict_a_middleware_would_read(self):
        # web/server.py's _SecurityHeadersMiddleware reads scope["state"]
        # directly (not via Request.state) when it builds the response
        # header, after the route has already run -- set_nonce has to
        # mutate that same dict, not shadow it.
        scope = {"type": "http", "headers": [], "state": {"csp_nonce": "original"}}
        request = Request(scope)
        set_nonce(request, "overridden")
        assert scope["state"]["csp_nonce"] == "overridden"
