"""Tests for web/server.py -- the Host allowlist and security-header
middleware, the shared local-mode token, and the SEC-06 bootstrap flow
(docs/security-remediation-plan.md, Phase 1 item 1.2) that replaces it as
what a browser actually presents. See docs/https-connector-refactor-plan.md
§10.5 for the control table this covers (Host allowlist against DNS
rebinding, CSP/X-Frame-Options, Cache-Control -- the last one is per-route,
tested in test_routes_approvals.py instead).
"""
from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from privacyfence.principal import LOCAL_PRINCIPAL_ID, Principal, current_principal
from privacyfence.web.server import (
    DEFAULT_PORT,
    WebServer,
    _PrincipalScopeMiddleware,
    build_app,
    load_or_create_token,
)
from privacyfence.web.session_auth import SESSION_COOKIE, BootstrapStore, LocalSessionStore
from privacyfence.web_approval_ui import WebApprovalUI

TOKEN = "test-token-0123456789"


def _signed_in(client: TestClient, sessions: LocalSessionStore) -> str:
    """Mirrors test_routes_org_approvals.py's own ``_signed_in`` for
    OrgSessionStore -- creates a real session directly (bypassing the HTTP
    bootstrap exchange, which TestBootstrapFlow below exercises on its
    own) and sets it as the cookie, for tests that only care about what
    happens *after* authentication."""
    session_id = sessions.create()
    client.cookies.set(SESSION_COOKIE, session_id)
    return session_id


class TestHostAllowlist:
    def _client(self):
        sessions = LocalSessionStore()
        app = build_app(WebApprovalUI(), token=TOKEN, sessions=sessions, allowed_hosts=frozenset({"localhost"}))
        client = TestClient(app, base_url="http://localhost")
        _signed_in(client, sessions)
        return client

    def test_allowed_host_passes_through(self):
        r = self._client().get("/approvals")
        assert r.status_code == 200

    def test_disallowed_host_is_rejected_before_reaching_any_route(self):
        r = self._client().get("/approvals", headers={"Host": "evil.example.com"})
        assert r.status_code == 400

    def test_port_suffix_on_the_host_header_is_ignored_for_matching(self):
        r = self._client().get("/approvals", headers={"Host": "localhost:9999"})
        assert r.status_code == 200


class TestPrincipalScopeMiddleware:
    """P6, docs/https-connector-refactor-plan.md §9.1: "entered once per
    HTTP request, in exactly one place" for the browser surface -- proves
    the ASGI wiring actually scopes a real request (and only that request),
    not just that principal_scope() itself works (that's test_principal.py's
    job)."""

    async def _whoami_app(self, scope, receive, send) -> None:
        response = JSONResponse({"principal_id": current_principal().id})
        await response(scope, receive, send)

    def test_default_resolver_scopes_the_request_to_local_principal(self):
        app = _PrincipalScopeMiddleware(self._whoami_app, lambda request: current_principal())
        # current_principal() outside any request is LOCAL_PRINCIPAL -- the
        # resolver above just round-trips whatever's ambient, so this proves
        # the middleware calls it and enters the scope, not merely that it
        # exists.
        client = TestClient(app)
        r = client.get("/")
        assert r.json() == {"principal_id": LOCAL_PRINCIPAL_ID}

    def test_a_custom_resolver_is_honored_and_scopes_only_that_request(self):
        app = _PrincipalScopeMiddleware(self._whoami_app, lambda request: Principal(id="alice"))
        client = TestClient(app)

        r = client.get("/")

        assert r.json() == {"principal_id": "alice"}
        # And the scope doesn't leak past the request that opened it.
        assert current_principal().id == LOCAL_PRINCIPAL_ID

    def test_build_app_wires_a_custom_principal_resolver_through_over_real_http(self):
        # Exercises build_app()'s own principal_resolver parameter (not the
        # middleware in isolation, which the two tests above already cover)
        # against a plain, non-streaming route -- what's being checked is
        # only that build_app actually threads the parameter through to
        # _PrincipalScopeMiddleware, i.e. the resolver gets consulted at all.
        seen = []

        def resolver(request):
            seen.append(1)
            return Principal(id="alice")

        sessions = LocalSessionStore()
        app = build_app(WebApprovalUI(), token=TOKEN, sessions=sessions, principal_resolver=resolver)
        client = TestClient(app, base_url="http://localhost")
        _signed_in(client, sessions)

        r = client.get("/approvals")

        assert r.status_code == 200
        assert seen  # the custom resolver was actually consulted


class TestSecurityHeaders:
    def _client(self):
        sessions = LocalSessionStore()
        app = build_app(WebApprovalUI(), token=TOKEN, sessions=sessions)
        client = TestClient(app, base_url="http://localhost")
        _signed_in(client, sessions)
        return client

    def test_content_security_policy_is_present_and_locked_down(self):
        r = self._client().get("/approvals")
        csp = r.headers.get("content-security-policy", "")
        assert "default-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp

    def test_frame_options_deny(self):
        r = self._client().get("/approvals")
        assert r.headers.get("x-frame-options") == "DENY"

    def test_content_type_options_nosniff(self):
        r = self._client().get("/approvals")
        assert r.headers.get("x-content-type-options") == "nosniff"

    def test_headers_are_present_even_on_an_error_response(self):
        app = build_app(WebApprovalUI(), token=TOKEN)
        r = TestClient(app, base_url="http://localhost").get("/approvals")  # unauthenticated -> 401
        assert r.status_code == 401
        assert r.headers.get("x-frame-options") == "DENY"

    def test_permissions_policy_denies_unused_powerful_features(self):
        # SEC-18 (docs/security-remediation-plan.md, Phase 3 item 3.5).
        r = self._client().get("/approvals")
        policy = r.headers.get("permissions-policy", "")
        assert "camera=()" in policy
        assert "microphone=()" in policy
        assert "geolocation=()" in policy

    def test_permissions_policy_leaves_webauthn_at_its_self_default(self):
        # web/routes_security.py's step-up flow needs these from this same
        # origin -- see _PERMISSIONS_POLICY's own comment.
        r = self._client().get("/approvals")
        policy = r.headers.get("permissions-policy", "")
        assert "publickey-credentials-get=(self)" in policy
        assert "publickey-credentials-create=(self)" in policy

    def test_cross_origin_opener_policy_is_same_origin(self):
        r = self._client().get("/approvals")
        assert r.headers.get("cross-origin-opener-policy") == "same-origin"

    def test_no_strict_transport_security_in_local_mode(self):
        # Local mode is plain http://localhost by design (module docstring)
        # -- sending HSTS there would be at best inert, at worst harmful
        # (see _SecurityHeadersMiddleware's own docstring).
        r = self._client().get("/approvals")
        assert "strict-transport-security" not in r.headers


class TestCacheControlOnSensitivePages:
    """SEC-18 (docs/security-remediation-plan.md, Phase 3 item 3.5): a sweep
    across every local-mode page that carries session- or approval-specific
    content, rather than trusting that each route author remembered
    Cache-Control: no-store on their own -- a future new page that forgets
    it fails here instead of shipping silently cacheable."""

    def test_the_approvals_page_is_no_store(self):
        sessions = LocalSessionStore()
        app = build_app(WebApprovalUI(), token=TOKEN, sessions=sessions)
        client = TestClient(app, base_url="http://localhost")
        _signed_in(client, sessions)
        r = client.get("/approvals")
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-store"

    def test_the_settings_page_is_no_store(self, tmp_path, monkeypatch):
        controller = _controller(tmp_path, monkeypatch)
        sessions = LocalSessionStore()
        app = build_app(WebApprovalUI(), token=TOKEN, sessions=sessions, controller=controller)
        client = TestClient(app, base_url="http://localhost")
        _signed_in(client, sessions)
        r = client.get("/settings")
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-store"

    def test_the_unauthorized_landing_page_is_no_store(self):
        # Regression test: this page names a live bearer-secret command
        # (session_auth.unauthorized_html) and, before SEC-18, shipped with
        # no Cache-Control header at all.
        app = build_app(WebApprovalUI(), token=TOKEN)
        r = TestClient(app, base_url="http://localhost").get("/approvals")
        assert r.status_code == 401
        assert r.headers.get("cache-control") == "no-store"


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

    # -- SEC-06: rotated whenever the installed version changes -------------- #

    def test_survives_a_restart_with_no_version_change(self, tmp_path, monkeypatch):
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        first = load_or_create_token()
        second = load_or_create_token()
        assert first == second
        version_file = tmp_path / "web_token_version"
        assert version_file.exists()
        assert oct(version_file.stat().st_mode)[-3:] == "600"

    def test_rotates_when_the_installed_version_changes(self, tmp_path, monkeypatch):
        from privacyfence import paths
        from privacyfence.web import server as server_module

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        monkeypatch.setattr(server_module, "__version__", "1.0.0")
        first = load_or_create_token()

        monkeypatch.setattr(server_module, "__version__", "2.0.0")
        second = load_or_create_token()

        assert first != second

    def test_a_pre_sec_06_token_with_no_version_marker_is_rotated_once(self, tmp_path, monkeypatch):
        # Every install from before this fix wrote web_token but never
        # web_token_version -- the first startup under this version must
        # not trust that old value forever just because the file exists.
        from privacyfence import paths

        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        (tmp_path / "web_token").write_text("pre-sec-06-token", encoding="utf-8")

        token = load_or_create_token()

        assert token != "pre-sec-06-token"
        # ...and it's now pinned to this version, so it survives a second
        # call within the same install exactly like any other token would.
        assert load_or_create_token() == token


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
# SEC-06 (docs/security-remediation-plan.md, Phase 1 item 1.2): the
# ?bootstrap=<code> one-time exchange that replaces the old ?token= link,
# and WebServer.mint_bootstrap_url()'s own wrapper around it.
# --------------------------------------------------------------------------- #

class TestBootstrapFlow:
    def _app(self):
        sessions = LocalSessionStore()
        bootstrap = BootstrapStore()
        app = build_app(WebApprovalUI(), token=TOKEN, sessions=sessions, bootstrap=bootstrap)
        return app, sessions, bootstrap

    def test_valid_code_mints_a_session_and_redirects_with_no_query_string(self):
        app, _sessions, bootstrap = self._app()
        client = TestClient(app, base_url="http://localhost", follow_redirects=False)
        code = bootstrap.mint()

        r = client.get(f"/approvals?bootstrap={code}")

        assert r.status_code == 303
        assert r.headers["location"] == "/approvals"
        assert "pf_session" in r.headers.get("set-cookie", "")

    def test_code_is_single_use(self):
        app, _sessions, bootstrap = self._app()
        client = TestClient(app, base_url="http://localhost", follow_redirects=False)
        code = bootstrap.mint()
        client.get(f"/approvals?bootstrap={code}")
        client.cookies.clear()

        r = client.get(f"/approvals?bootstrap={code}")

        assert "pf_session" not in r.headers.get("set-cookie", "")

    def test_unknown_code_mints_no_session(self):
        app, _sessions, _bootstrap = self._app()
        client = TestClient(app, base_url="http://localhost", follow_redirects=False)

        r = client.get("/approvals?bootstrap=not-a-real-code")

        assert r.status_code == 303
        assert r.headers["location"] == "/approvals"
        assert "pf_session" not in r.headers.get("set-cookie", "")
        follow = client.get(r.headers["location"])
        assert follow.status_code == 401

    def test_following_the_redirect_reaches_an_authenticated_page(self):
        app, _sessions, bootstrap = self._app()
        client = TestClient(app, base_url="http://localhost", follow_redirects=True)
        code = bootstrap.mint()

        r = client.get(f"/approvals?bootstrap={code}")

        assert r.status_code == 200
        assert "Nothing is waiting" in r.text

    def test_the_old_token_query_param_no_longer_authenticates(self):
        # SEC-06's whole point: ?token=<the persistent secret> is not
        # honored by any route any more, only ?bootstrap=<one-time code>.
        app, _sessions, _bootstrap = self._app()
        client = TestClient(app, base_url="http://localhost")

        r = client.get(f"/approvals?token={TOKEN}")

        assert r.status_code == 401


class TestBootstrapMintEndpoint:
    """POST /api/bootstrap -- minting a fresh bootstrap code on demand,
    once a previous session/code has already expired, without restarting
    the daemon. The raw local secret is presented as a Bearer header,
    never a query string."""

    def _app(self):
        bootstrap = BootstrapStore()
        app = build_app(WebApprovalUI(), token=TOKEN, bootstrap=bootstrap)
        return app, bootstrap

    def test_correct_bearer_secret_mints_a_usable_code(self):
        app, _bootstrap = self._app()
        client = TestClient(app, base_url="http://localhost", follow_redirects=False)

        r = client.post("/api/bootstrap", headers={"Authorization": f"Bearer {TOKEN}"})

        assert r.status_code == 200
        code = r.json()["bootstrap"]
        follow = client.get(f"/approvals?bootstrap={code}")
        assert "pf_session" in follow.headers.get("set-cookie", "")

    def test_missing_bearer_header_is_rejected(self):
        app, _bootstrap = self._app()
        r = TestClient(app, base_url="http://localhost").post("/api/bootstrap")
        assert r.status_code == 401

    def test_wrong_secret_is_rejected(self):
        app, _bootstrap = self._app()
        r = TestClient(app, base_url="http://localhost").post(
            "/api/bootstrap", headers={"Authorization": "Bearer wrong-secret"},
        )
        assert r.status_code == 401

    def test_secret_in_a_query_string_is_not_accepted(self):
        # verify_bearer_secret only ever reads the Authorization header --
        # SEC-06 is specifically about getting this secret out of URLs.
        app, _bootstrap = self._app()
        r = TestClient(app, base_url="http://localhost").post(f"/api/bootstrap?token={TOKEN}")
        assert r.status_code == 401


class TestWebServerBootstrap:
    def test_mint_bootstrap_url_embeds_a_fresh_code_under_the_given_path(self):
        server = WebServer(WebApprovalUI(), host="localhost", port=1234, token=TOKEN)
        url = server.mint_bootstrap_url("/approvals")
        assert url.startswith("http://localhost:1234/approvals?bootstrap=")

    def test_each_call_mints_a_different_code(self):
        server = WebServer(WebApprovalUI(), host="localhost", port=1234, token=TOKEN)
        first = server.mint_bootstrap_url("/approvals")
        second = server.mint_bootstrap_url("/approvals")
        assert first != second


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

        self.sessions = LocalSessionStore()
        return build_app(
            WebApprovalUI(), token=TOKEN, sessions=self.sessions,
            mcp_dispatcher=McpDispatcher(lambda: {}), mcp_token=self.MCP_TOKEN,
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
        # A valid *session cookie* (a real local-mode session, not the raw
        # secret) but the *MCP* token presented as the CSRF value -- the
        # double-submit check must still fail, since these are different
        # secrets.
        _signed_in(client, self.sessions)
        resp = client.post(
            "/api/approvals/some-id/decide",
            json={"result": "deny", "csrf": self.MCP_TOKEN},
            headers={"Authorization": f"Bearer {self.MCP_TOKEN}"},
        )
        assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# P4 (docs/https-connector-refactor-plan.md §16): /settings and
# /api/state/stream folded into the same combined app, sharing the
# approval surface's own session -- see build_app()'s own docstring for why
# this is the deliberate contrast with MCP's separate audience.
# --------------------------------------------------------------------------- #

def _controller(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from privacyfence import daemon_main, resource_names, settings_controller as sc, update_checker

    monkeypatch.setattr(resource_names, "_cache_file", lambda: tmp_path / "resource_name_cache.json")
    monkeypatch.setattr(update_checker, "_cache_file", lambda: tmp_path / "update_check_cache.json")
    monkeypatch.setattr(sc, "check_for_update", lambda **kw: None)
    monkeypatch.setattr(daemon_main, "load_org_config", lambda: {})
    org_dir_path = tmp_path / "org"
    org_dir_path.mkdir()
    monkeypatch.setattr(sc, "org_dir", lambda: org_dir_path)
    data_dir_path = tmp_path / "data"
    data_dir_path.mkdir()
    monkeypatch.setattr(sc, "data_dir", lambda: data_dir_path)
    config_path = tmp_path / "settings.yaml"
    config_path.write_text("auto_accept_rules: {}\nconnectors: {}\n", encoding="utf-8")
    connector_host = SimpleNamespace(set_connectors=lambda conns: None)
    return sc.SettingsController(str(config_path), connectors=[], connector_host=connector_host)


class TestSettingsFoldedIntoTheCombinedApp:
    def test_settings_page_shares_the_approval_surfaces_session(self, tmp_path, monkeypatch):
        controller = _controller(tmp_path, monkeypatch)
        sessions = LocalSessionStore()
        app = build_app(WebApprovalUI(), token=TOKEN, sessions=sessions, controller=controller)
        client = TestClient(app, base_url="http://localhost")
        _signed_in(client, sessions)

        r = client.get("/settings")

        assert r.status_code == 200
        assert "PrivacyFence — Settings" in r.text

    def test_no_controller_means_no_settings_route(self):
        sessions = LocalSessionStore()
        app = build_app(WebApprovalUI(), token=TOKEN, sessions=sessions)
        client = TestClient(app, base_url="http://localhost")
        _signed_in(client, sessions)

        r = client.get("/settings")

        assert r.status_code == 404

    # No 200-success request test against a real GET here -- the stream's
    # generator never terminates (same reasoning web/test_routes_approvals.
    # py's own TestApprovalsStream documents: TestClient's synchronous,
    # fully-buffering client.get() can't drive an endless SSE response
    # without hanging). Only the auth boundary -- which short-circuits
    # before the generator is ever entered -- is exercised here; the real
    # streaming behavior is what P0/P1's manual Chromium checks cover.

    def test_state_stream_requires_auth(self):
        from privacyfence.web.state_stream import StateStream

        web_ui = WebApprovalUI()
        stream = StateStream(settings_snapshot=lambda: None, list_pending=web_ui.deferred_registry.list_pending)
        app = build_app(web_ui, token=TOKEN, state_stream=stream)
        client = TestClient(app, base_url="http://localhost")

        r = client.get("/api/state/stream")

        assert r.status_code == 401

    def test_web_server_provisions_the_state_stream_automatically(self):
        # Unlike build_app() (tested directly above), WebServer always
        # builds and wires a StateStream -- see its own __init__.
        server = WebServer(WebApprovalUI(), port=0, token=TOKEN)
        assert server.state_stream is not None


class TestWebServerWiresTheStateStream:
    def test_controller_is_registered_as_a_change_listener(self, tmp_path, monkeypatch):
        controller = _controller(tmp_path, monkeypatch)
        server = WebServer(WebApprovalUI(), port=0, token=TOKEN, controller=controller)

        assert server.state_stream is not None
        assert server.state_stream.push_settings in controller._change_listeners

    def test_controller_mutation_reaches_the_stream(self, tmp_path, monkeypatch):
        controller = _controller(tmp_path, monkeypatch)
        server = WebServer(WebApprovalUI(), port=0, token=TOKEN, controller=controller)
        pushed = []
        monkeypatch.setattr(server.state_stream, "_broadcast", lambda event, data: pushed.append((event, data)))

        # _push_snapshot -- not every mutating method, which already
        # returns its own snapshot directly to its own caller -- is what
        # notifies out-of-band listeners (a rule reloaded from the IPC
        # thread, an async op finishing on its own background thread; see
        # settings_controller.py's own docstring on _push_snapshot/
        # on_change). This proves the wiring reaches state_stream, the
        # same path any of those real triggers goes through.
        controller._push_snapshot()

        assert pushed and pushed[0][0] == "settings"

    def test_main_dispatcher_is_registered_for_headless_call_on_main(self, tmp_path, monkeypatch):
        from privacyfence import settings_controller as sc
        from privacyfence.web import state_stream as ss

        controller = _controller(tmp_path, monkeypatch)
        WebServer(WebApprovalUI(), port=0, token=TOKEN, controller=controller)

        recorded = []
        monkeypatch.setattr(ss, "_loop", None)  # no real loop running in this test
        sc.call_on_main(lambda x: recorded.append(x), "hi")
        assert recorded == ["hi"]  # falls back to running inline with no loop captured yet
