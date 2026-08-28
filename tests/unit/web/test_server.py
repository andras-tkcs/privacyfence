"""Tests for web/server.py -- the Host allowlist and security-header
middleware, and the shared local-mode token. See
docs/https-connector-refactor-plan.md §10.5 for the control table this
covers (Host allowlist against DNS rebinding, CSP/X-Frame-Options,
Cache-Control -- the last one is per-route, tested in
test_routes_approvals.py instead).
"""
from __future__ import annotations

from starlette.testclient import TestClient

from privacyfence.web.server import DEFAULT_PORT, WebServer, build_app, load_or_create_token
from privacyfence.web_approval_ui import WebApprovalUI

TOKEN = "test-token-0123456789"


class TestHostAllowlist:
    def _client(self):
        app = build_app(WebApprovalUI(), token=TOKEN, allowed_hosts=frozenset({"localhost"}))
        return TestClient(app, base_url="http://localhost")

    def test_allowed_host_passes_through(self):
        r = self._client().get(f"/approvals?token={TOKEN}")
        assert r.status_code == 200

    def test_disallowed_host_is_rejected_before_reaching_any_route(self):
        r = self._client().get(f"/approvals?token={TOKEN}", headers={"Host": "evil.example.com"})
        assert r.status_code == 400

    def test_port_suffix_on_the_host_header_is_ignored_for_matching(self):
        r = self._client().get(f"/approvals?token={TOKEN}", headers={"Host": "localhost:9999"})
        assert r.status_code == 200


class TestSecurityHeaders:
    def _client(self):
        app = build_app(WebApprovalUI(), token=TOKEN)
        return TestClient(app, base_url="http://localhost")

    def test_content_security_policy_is_present_and_locked_down(self):
        r = self._client().get(f"/approvals?token={TOKEN}")
        csp = r.headers.get("content-security-policy", "")
        assert "default-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp

    def test_frame_options_deny(self):
        r = self._client().get(f"/approvals?token={TOKEN}")
        assert r.headers.get("x-frame-options") == "DENY"

    def test_content_type_options_nosniff(self):
        r = self._client().get(f"/approvals?token={TOKEN}")
        assert r.headers.get("x-content-type-options") == "nosniff"

    def test_headers_are_present_even_on_an_error_response(self):
        r = self._client().get("/approvals")  # unauthenticated -> 401
        assert r.status_code == 401
        assert r.headers.get("x-frame-options") == "DENY"


class TestToken:
    def test_generates_and_persists_a_token(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        first = load_or_create_token()
        second = load_or_create_token()
        assert first == second
        token_file = tmp_path / "web_token"
        assert token_file.exists()
        assert oct(token_file.stat().st_mode)[-3:] == "600"

    def test_token_is_high_entropy(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        token = load_or_create_token()
        assert len(token) >= 32


class TestWebServerConstruction:
    def test_binds_to_localhost_by_default(self):
        server = WebServer(WebApprovalUI(), port=0, token=TOKEN)
        assert server.host == "localhost"

    def test_uses_the_default_port_constant(self):
        server = WebServer(WebApprovalUI(), token=TOKEN)
        assert server.port == DEFAULT_PORT

    def test_base_url_reflects_host_and_port(self):
        server = WebServer(WebApprovalUI(), host="localhost", port=1234, token=TOKEN)
        assert server.base_url == "http://localhost:1234"
