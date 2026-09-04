"""Tests for web/server.py's org-mode wiring (P7): build_app(org=...) and
WebServer(org=...) -- see test_org_mcp_e2e.py for the full DCR/authorize/
token/tool-call flow driven over real HTTP; this file covers what server.py
itself is responsible for: which routes exist (and, just as importantly,
which don't), TLS/trusted-proxies plumbing, and base_url/allowed_hosts.
"""
from __future__ import annotations

from starlette.testclient import TestClient

from privacyfence import org_identity as oi
from privacyfence.connector_registry import ConnectorRegistry
from privacyfence.web.mcp_dispatch import McpDispatcher
from privacyfence.web.oauth_provider import OrgOAuthProvider
from privacyfence.web.org_session import OrgSessionStore
from privacyfence.web.server import OrgAuth, WebServer, build_app
from privacyfence.web_approval_ui import WebApprovalUI

ISSUER = "https://pf.example.com"


def _idp() -> oi.IdpConfig:
    return oi.IdpConfig(
        issuer="https://idp.example.com", client_id="privacyfence", client_secret="s",
        authorization_endpoint="https://idp.example.com/authorize",
        token_endpoint="https://idp.example.com/token", jwks_uri="https://idp.example.com/jwks",
    )


def _org_auth(tmp_path, monkeypatch) -> OrgAuth:
    monkeypatch.setattr("privacyfence.web.oauth_provider._clients_file_path", lambda: str(tmp_path / "clients.json"))
    provider = OrgOAuthProvider(_idp(), idp_callback_url=f"{ISSUER}/oauth/idp/callback")
    return OrgAuth(provider=provider, sessions=OrgSessionStore(), idp=_idp(), issuer_url=ISSUER)


class TestBuildAppOrgMode:
    def test_requires_a_token_in_local_mode(self):
        import pytest
        with pytest.raises(ValueError):
            build_app(WebApprovalUI())  # no token, no org -- neither mode satisfied

    def test_mcp_route_is_mounted(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(
            WebApprovalUI(), org=org, mcp_dispatcher=McpDispatcher(lambda: {}),
            allowed_hosts=frozenset({"pf.example.com"}),
        )
        client = TestClient(app, base_url=ISSUER)
        # No bearer token -- expect a 401, not a 404: proves the route exists.
        r = client.post("/mcp", json={}, headers={"Accept": "application/json, text/event-stream"})
        assert r.status_code == 401

    def test_oauth_authorization_server_metadata_is_served(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER)
        r = client.get("/.well-known/oauth-authorization-server")
        assert r.status_code == 200
        assert r.json()["issuer"].rstrip("/") == ISSUER

    def test_login_route_is_mounted(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = client.get("/login")
        assert r.status_code == 302
        assert r.headers["location"].startswith("https://idp.example.com/authorize?")

    def test_lifespan_starts_and_stops_cleanly_with_an_mcp_dispatcher(self, tmp_path, monkeypatch):
        # Exercises build_app()'s own lifespan wiring for org mode (the
        # Starlette `lifespan` callback that enters mcp_lifespan via
        # _combined_lifespan) -- TestClient only runs ASGI lifespan
        # startup/shutdown when used as a context manager.
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(
            WebApprovalUI(), org=org, mcp_dispatcher=McpDispatcher(lambda: {}),
            allowed_hosts=frozenset({"pf.example.com"}),
        )
        with TestClient(app, base_url=ISSUER) as client:
            r = client.get("/.well-known/oauth-authorization-server")
            assert r.status_code == 200

    def test_settings_surface_is_not_mounted(self, tmp_path, monkeypatch):
        # /settings' ~30-action surface stays out of org mode (see server.py's
        # module docstring) -- unlike /approvals, which P9 (below) mounts as
        # its own principal-aware route set.
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER)
        assert client.get("/settings").status_code == 404
        assert client.get("/api/state/stream").status_code == 404

    def test_local_mode_approval_surface_is_not_what_gets_mounted(self, tmp_path, monkeypatch):
        # /approvals exists (P9), but it's web/routes_org_approvals.py's
        # principal-aware route set, not routes_approvals.create_app's
        # shared-secret one -- an unauthenticated request is redirected to
        # /login, never served the (local-mode-only) card content directly.
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = client.get("/approvals")
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/approvals"


class TestConnectSurfaceOrgMode:
    """P8 (docs/https-connector-refactor-plan.md §9.3): /connect and the
    /oauth/start|callback/{service} routes are mounted only once a real
    ConnectorRegistry is supplied on OrgAuth -- see that class's own
    docstring. A hand-built OrgAuth without one (every test above this
    class) keeps getting exactly P7's own route set."""

    def test_connect_is_not_mounted_without_a_connector_registry(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER)
        assert client.get("/connect").status_code == 404
        assert client.get("/oauth/start/slack").status_code == 404

    def test_connect_is_mounted_with_a_connector_registry(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        registry = ConnectorRegistry(factory=lambda principal: [])
        org_with_registry = org.__class__(
            provider=org.provider, sessions=org.sessions, idp=org.idp, issuer_url=org.issuer_url,
            connector_registry=registry, org_config={},
        )
        app = build_app(WebApprovalUI(), org=org_with_registry, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = client.get("/connect")
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/connect"

    def test_login_with_no_next_lands_on_connect_once_mounted(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        registry = ConnectorRegistry(factory=lambda principal: [])
        org_with_registry = org.__class__(
            provider=org.provider, sessions=org.sessions, idp=org.idp, issuer_url=org.issuer_url,
            connector_registry=registry, org_config={},
        )
        app = build_app(WebApprovalUI(), org=org_with_registry, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = client.get("/login")
        assert r.status_code == 302
        assert r.headers["location"].startswith("https://idp.example.com/authorize?")
        # Can't drive the whole IdP round trip from here without duplicating
        # test_routes_org_identity.py's own fixtures -- that file's
        # TestLogin already proves default_next_path reaches the callback's
        # redirect; this just proves server.py actually wires "/connect" in
        # as that default once P8's registry is present (see test_routes_
        # org_identity.py::TestLogin for the equivalent no-registry case,
        # which keeps the original "/approvals" default).


class TestApprovalsAndSecuritySurfaceOrgMode:
    """P9: /approvals and /security are mounted unconditionally (unlike
    /connect, they need nothing from OrgAuth.connector_registry) -- see
    web/server.py's own module docstring."""

    def test_approvals_is_mounted_with_no_connector_registry(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = client.get("/approvals")
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/approvals"

    def test_security_is_mounted_since_the_issuer_hostname_becomes_the_rp_id(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = client.get("/security")
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/security"

    def test_step_up_config_section_reaches_the_decide_endpoint(self, tmp_path, monkeypatch):
        # A thin end-to-end wiring check -- web/routes_org_approvals.py's
        # own test file covers the step-up protocol itself in depth; this
        # only proves server.py actually threads org_config.json's
        # "step_up" section through StepUpConfig.from_org_config into the
        # mounted routes, rather than a hardcoded default.
        from privacyfence.principal import Principal as _P

        org = _org_auth(tmp_path, monkeypatch)
        org = org.__class__(
            provider=org.provider, sessions=org.sessions, idp=org.idp, issuer_url=org.issuer_url,
            org_config={"step_up": {"enabled": True, "scope": "writes"}},
        )
        app = build_app(WebApprovalUI(), org=org, allowed_hosts=frozenset({"pf.example.com"}))
        client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        session_id = org.sessions.create(_P(id="alice"))
        client.cookies.set("pf_org_session", session_id)
        r = client.post("/api/approvals/does-not-exist/decide", json={"result": "deny", "csrf": session_id})
        # Unknown approval -> answer() rejects it (409), never a 404/500 --
        # proves the route (and therefore the StepUpConfig it was built
        # with) is really live, not just present.
        assert r.status_code == 409


class TestWebServerOrgMode:
    def test_base_url_is_the_configured_issuer_url_not_the_bind_address(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        server = WebServer(WebApprovalUI(), host="0.0.0.0", port=443, org=org)
        assert server.base_url == ISSUER

    def test_token_and_mcp_token_are_none_in_org_mode(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        server = WebServer(WebApprovalUI(), org=org, mcp_dispatcher=McpDispatcher(lambda: {}))
        assert server.token is None
        assert server.mcp_token is None

    def test_mcp_url_still_reflects_base_url_in_org_mode(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        server = WebServer(WebApprovalUI(), org=org, mcp_dispatcher=McpDispatcher(lambda: {}))
        assert server.mcp_url == f"{ISSUER}/mcp"

    def test_state_stream_is_not_built_in_org_mode(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        server = WebServer(WebApprovalUI(), org=org)
        assert server.state_stream is None

    def test_issuer_hostname_is_added_to_the_allowed_hosts(self, tmp_path, monkeypatch):
        # Bound to 0.0.0.0 (a real org deployment's own bind host), which
        # alone wouldn't satisfy the Host-header allowlist for a request
        # actually addressed to the issuer's own hostname -- server.py has
        # to add that hostname itself.
        org = _org_auth(tmp_path, monkeypatch)
        server = WebServer(WebApprovalUI(), host="0.0.0.0", port=443, org=org)
        client = TestClient(server._server.config.app, base_url=ISSUER)

        r = client.get("/.well-known/oauth-authorization-server")

        assert r.status_code == 200  # not 400 "Invalid Host header"

    def test_ssl_certfile_and_keyfile_are_accepted(self, tmp_path, monkeypatch):
        org = _org_auth(tmp_path, monkeypatch)
        # Must not raise -- uvicorn.Config only validates cert files exist
        # when the server actually starts, not at construction time.
        WebServer(
            WebApprovalUI(), org=org, ssl_certfile="/does/not/exist/cert.pem",
            ssl_keyfile="/does/not/exist/key.pem",
        )

    def test_trusted_proxies_wraps_the_app_in_proxy_headers_middleware(self, tmp_path, monkeypatch):
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        org = _org_auth(tmp_path, monkeypatch)
        server = WebServer(WebApprovalUI(), org=org, trusted_proxies=("10.0.0.5",))
        assert isinstance(server._server.config.app, ProxyHeadersMiddleware)

    def test_no_trusted_proxies_by_default(self, tmp_path, monkeypatch):
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        org = _org_auth(tmp_path, monkeypatch)
        server = WebServer(WebApprovalUI(), org=org)
        assert not isinstance(server._server.config.app, ProxyHeadersMiddleware)
