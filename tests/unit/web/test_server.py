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

    def test_mcp_url_is_none_without_an_mcp_dispatcher(self):
        server = WebServer(WebApprovalUI(), token=TOKEN)
        assert server.mcp_url is None
        assert server.mcp_token is None

    def test_mcp_url_is_set_when_an_mcp_dispatcher_is_given(self):
        from privacyfence.web.mcp_dispatch import McpDispatcher

        server = WebServer(
            WebApprovalUI(), host="localhost", port=1234, token=TOKEN,
            mcp_dispatcher=McpDispatcher(lambda: {}), mcp_token="mcp-tok",
        )
        assert server.mcp_url == "http://localhost:1234/mcp"


# --------------------------------------------------------------------------- #
# D11 (docs/https-connector-refactor-plan.md §12): the mcp_url discovery
# file mcpb/shim reads to find /mcp without any config the user has to
# edit -- the direct successor of ipc.py's PORT_FILE. Only written/cleared
# when this WebServer actually has an mcp_dispatcher (i.e. web.mcp.enabled);
# a server started for the approval UI alone must not claim /mcp exists.
# --------------------------------------------------------------------------- #

class TestMcpUrlFile:
    def _server(self, tmp_path, monkeypatch, *, with_mcp: bool):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        kwargs = {}
        if with_mcp:
            from privacyfence.web.mcp_dispatch import McpDispatcher

            kwargs = {"mcp_dispatcher": McpDispatcher(lambda: {}), "mcp_token": "mcp-tok"}
        return WebServer(WebApprovalUI(), host="localhost", port=0, token=TOKEN, **kwargs)

    def test_start_writes_the_file_when_mcp_is_enabled(self, tmp_path, monkeypatch):
        server = self._server(tmp_path, monkeypatch, with_mcp=True)
        try:
            server.start()
            mcp_url_file = tmp_path / "mcp_url"
            assert mcp_url_file.exists()
            assert mcp_url_file.read_text(encoding="utf-8") == server.mcp_url
            assert oct(mcp_url_file.stat().st_mode)[-3:] == "600"
        finally:
            server.stop()

    def test_stop_clears_the_file(self, tmp_path, monkeypatch):
        server = self._server(tmp_path, monkeypatch, with_mcp=True)
        server.start()
        server.stop()
        assert not (tmp_path / "mcp_url").exists()

    def test_no_file_at_all_when_mcp_is_not_enabled(self, tmp_path, monkeypatch):
        server = self._server(tmp_path, monkeypatch, with_mcp=False)
        try:
            server.start()
            assert not (tmp_path / "mcp_url").exists()
        finally:
            server.stop()


# --------------------------------------------------------------------------- #
# §10.3's audience separation: the MCP bearer token and the approval
# surface's session cookie/CSRF token are different secrets, checked in
# different middleware, and neither is ever accepted on the other's routes.
# The one test in this class required to "fail loudly if the middleware is
# ever reordered" (§10.3/§13).
# --------------------------------------------------------------------------- #

class TestAudienceSeparation:
    MCP_TOKEN = "mcp-token-0123456789"

    def _app(self):
        from privacyfence.web.mcp_dispatch import McpDispatcher

        return build_app(
            WebApprovalUI(), token=TOKEN, mcp_dispatcher=McpDispatcher(lambda: {}), mcp_token=self.MCP_TOKEN,
        )

    def test_mcp_rejects_the_approval_surfaces_own_token_as_bearer_auth(self):
        client = TestClient(self._app(), base_url="http://localhost")
        resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                            headers={"Authorization": f"Bearer {TOKEN}"})
        assert resp.status_code == 401

    def test_mcp_accepts_only_its_own_token(self):
        # Unlike the 401 checks above (rejected in auth middleware, before
        # ever reaching the session manager), a real "initialize" needs the
        # session manager's task group running -- hence `with`, which is
        # what makes TestClient run the app's ASGI lifespan.
        with TestClient(self._app(), base_url="http://localhost") as client:
            resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                                headers={"Authorization": f"Bearer {self.MCP_TOKEN}"})
        assert resp.status_code != 401

    def test_approvals_decide_rejects_the_mcp_token_as_csrf(self):
        client = TestClient(self._app(), base_url="http://localhost")
        # A valid *session cookie* (from the approval surface's own token)
        # but the *MCP* token presented as the CSRF value -- the double-
        # submit check must still fail, since these are different secrets.
        client.cookies.set("pf_session", TOKEN)
        resp = client.post(
            "/api/approvals/some-id/decide",
            json={"result": "deny", "csrf": self.MCP_TOKEN},
            headers={"Authorization": f"Bearer {self.MCP_TOKEN}"},
        )
        assert resp.status_code == 401
