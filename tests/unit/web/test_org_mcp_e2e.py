"""End-to-end proof of P7's own exit criterion (docs/https-connector-
refactor-plan.md §12: "Claude adds the connector by DCR; audience
separation test passes") -- drives the real ``/mcp`` Streamable HTTP
endpoint and the real OAuth 2.1 authorization-server routes
(``mount_org_oauth``) together, over an in-process ASGI transport, exactly
the way a real Claude client would: register via DCR, complete
``/authorize`` (with the IdP leg faked -- that dance has its own thorough
coverage in test_oauth_provider.py and test_org_identity.py), exchange the
code at ``/token``, then actually call a tool over ``/mcp`` with the
resulting bearer token and confirm it resolves to the signed-in human's own
Principal -- not the OAuth client's id, not the local principal.

Also covers §10.3's audience separation for org mode specifically: an
access token minted by this same authorization server must never be
accepted as an org-mode browser session (web/org_session.py), and a
browser session cookie must never be accepted as a bearer token on
``/mcp``. (Local mode's own audience-separation test predates this file --
see web/test_routes_mcp.py.)
"""
from __future__ import annotations

import contextlib
import urllib.parse as up

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette

from privacyfence import org_identity as oi
from privacyfence.connector import Connector, ToolSpec
from privacyfence.principal import current_principal
from privacyfence.web import org_session
from privacyfence.web.mcp_dispatch import McpDispatcher
from privacyfence.web.oauth_provider import OrgOAuthProvider
from privacyfence.web.routes_mcp import mcp_lifespan, mount_mcp, mount_org_oauth

ISSUER = "https://pf.example.com"
CLAUDE_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"


class WhoAmIConnector(Connector):
    @property
    def name(self) -> str:
        return "whoami"

    def tool_specs(self) -> list[ToolSpec]:
        return [ToolSpec(name="whoami", description="Returns the current principal.", params=[], read_only=True)]

    async def call(self, tool: str, args: dict) -> object:
        p = current_principal()
        return {"id": p.id, "email": p.email, "display_name": p.display_name, "is_admin": p.is_admin}


def _idp() -> oi.IdpConfig:
    return oi.IdpConfig(
        issuer="https://idp.example.com", client_id="privacyfence", client_secret="s3cr3t",
        authorization_endpoint="https://idp.example.com/authorize",
        token_endpoint="https://idp.example.com/token", jwks_uri="https://idp.example.com/jwks",
        admin_group_claim="groups", admin_group_values=("admins",),
    )


def _build_app(tmp_path, monkeypatch):
    monkeypatch.setattr("privacyfence.web.oauth_provider._clients_file_path", lambda: str(tmp_path / "clients.json"))
    provider = OrgOAuthProvider(_idp(), idp_callback_url=f"{ISSUER}/oauth/idp/callback")
    dispatcher = McpDispatcher(lambda: {"whoami": WhoAmIConnector()})
    mcp_route, session_manager = mount_mcp(dispatcher, verifier=provider)
    oauth_routes = mount_org_oauth(provider, issuer_url=ISSUER)
    app = Starlette(routes=[mcp_route, *oauth_routes])
    return app, provider, session_manager


async def _register_client(client: httpx.AsyncClient) -> dict:
    r = await client.post("/register", json={
        "redirect_uris": [CLAUDE_REDIRECT_URI], "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
    })
    assert r.status_code == 201, r.text
    return r.json()


async def _authorize_and_get_code(
    client: httpx.AsyncClient, *, client_id: str, monkeypatch, claims: dict,
) -> tuple[str, str]:
    """Drives /authorize -> (faked IdP) -> /oauth/idp/callback and returns
    ``(code, code_verifier)`` -- the code Claude's own client would receive
    at its redirect_uri, and the PKCE verifier it needs to redeem it."""
    verifier, challenge = oi.generate_pkce_pair()
    r = await client.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": CLAUDE_REDIRECT_URI,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "claudes-own-state",
    })
    assert r.status_code == 302, r.text
    own_state = dict(up.parse_qsl(up.urlparse(r.headers["location"]).query))["state"]

    monkeypatch.setattr("privacyfence.org_identity.exchange_code_for_tokens", lambda *a, **kw: {"id_token": "opaque"})
    monkeypatch.setattr(
        "privacyfence.org_identity.verify_id_token", lambda idp, token, *, nonce: {**claims, "nonce": nonce},
    )
    cb = await client.get("/oauth/idp/callback", params={"code": "idp-code", "state": own_state})
    assert cb.status_code == 302, cb.text
    parsed = up.urlparse(cb.headers["location"])
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == CLAUDE_REDIRECT_URI
    qs = dict(up.parse_qsl(parsed.query))
    assert qs["state"] == "claudes-own-state"
    return qs["code"], verifier


async def _exchange_for_tokens(client: httpx.AsyncClient, *, client_id: str, code: str, code_verifier: str) -> dict:
    r = await client.post("/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": CLAUDE_REDIRECT_URI,
        "client_id": client_id, "code_verifier": code_verifier,
    })
    assert r.status_code == 200, r.text
    return r.json()


@contextlib.asynccontextmanager
async def _mcp_session(app, session_manager, *, access_token: str):
    """One MCP ClientSession, connected with ``access_token`` as its
    bearer token. Does *not* itself enter ``mcp_lifespan`` --
    ``StreamableHTTPSessionManager.run()`` may only be entered once per
    instance (it raises on a second ``async with``), so a test that opens
    more than one MCP session against the same app must wrap all of them
    in one shared ``async with mcp_lifespan(session_manager):`` itself
    (see ``_two_mcp_sessions`` below) rather than each grabbing its own.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url=ISSUER, headers={"Authorization": f"Bearer {access_token}"},
    ) as http_client:
        async with streamable_http_client(f"{ISSUER}/mcp", http_client=http_client) as (read, write, _sid):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def test_dcr_authorize_token_and_a_real_tool_call_resolve_to_the_signed_in_principal(tmp_path, monkeypatch):
    app, _provider, session_manager = _build_app(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ISSUER) as client:
        registration = await _register_client(client)
        code, verifier = await _authorize_and_get_code(
            client, client_id=registration["client_id"], monkeypatch=monkeypatch,
            claims={"sub": "alice", "email": "alice@example.com", "name": "Alice A.", "groups": ["admins"]},
        )
        tokens = await _exchange_for_tokens(
            client, client_id=registration["client_id"], code=code, code_verifier=verifier,
        )

    async with mcp_lifespan(session_manager):
        async with _mcp_session(app, session_manager, access_token=tokens["access_token"]) as session:
            result = await session.call_tool("whoami", {"reason": "test"})
            assert result.structuredContent == {
                "id": "alice", "email": "alice@example.com", "display_name": "Alice A.", "is_admin": True,
            }


async def test_two_different_humans_authorizing_the_same_claude_client_get_isolated_principals(tmp_path, monkeypatch):
    app, _provider, session_manager = _build_app(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ISSUER) as client:
        registration = await _register_client(client)

        code_a, verifier_a = await _authorize_and_get_code(
            client, client_id=registration["client_id"], monkeypatch=monkeypatch, claims={"sub": "alice"},
        )
        tokens_a = await _exchange_for_tokens(
            client, client_id=registration["client_id"], code=code_a, code_verifier=verifier_a,
        )

        code_b, verifier_b = await _authorize_and_get_code(
            client, client_id=registration["client_id"], monkeypatch=monkeypatch, claims={"sub": "bob"},
        )
        tokens_b = await _exchange_for_tokens(
            client, client_id=registration["client_id"], code=code_b, code_verifier=verifier_b,
        )

    async with mcp_lifespan(session_manager):
        async with _mcp_session(app, session_manager, access_token=tokens_a["access_token"]) as session:
            result = await session.call_tool("whoami", {"reason": "test"})
            assert result.structuredContent["id"] == "alice"

        async with _mcp_session(app, session_manager, access_token=tokens_b["access_token"]) as session:
            result = await session.call_tool("whoami", {"reason": "test"})
            assert result.structuredContent["id"] == "bob"


async def test_mcp_access_token_is_rejected_as_an_org_session_cookie(tmp_path, monkeypatch):
    """§10.3 audience separation, org-mode side: an MCP bearer token must
    never double as a browser session."""
    app, _provider, _session_manager = _build_app(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ISSUER) as client:
        registration = await _register_client(client)
        code, verifier = await _authorize_and_get_code(
            client, client_id=registration["client_id"], monkeypatch=monkeypatch, claims={"sub": "alice"},
        )
        tokens = await _exchange_for_tokens(
            client, client_id=registration["client_id"], code=code, code_verifier=verifier,
        )

    sessions = org_session.OrgSessionStore()
    # The MCP access token was never handed to OrgSessionStore.create() --
    # presenting it as a session cookie value must not resolve to anything.
    from starlette.requests import Request

    scope = {
        "type": "http", "method": "GET", "path": "/",
        "headers": [(b"cookie", f"{org_session.SESSION_COOKIE}={tokens['access_token']}".encode())],
    }
    assert org_session.authenticated(Request(scope), sessions) is None


async def test_org_session_cookie_is_rejected_as_an_mcp_bearer_token(tmp_path, monkeypatch):
    """§10.3 audience separation, the other direction: a browser session id
    must never verify as an MCP access token."""
    _app, provider, _session_manager = _build_app(tmp_path, monkeypatch)
    from privacyfence.principal import Principal

    sessions = org_session.OrgSessionStore()
    session_id = sessions.create(Principal(id="alice"))

    assert await provider.verify_token(session_id) is None


async def test_dcr_registration_is_visible_to_a_fresh_provider_instance(tmp_path, monkeypatch):
    """The DCR client store survives a daemon restart -- a fresh
    OrgOAuthProvider pointed at the same clients file sees clients an
    earlier one registered, so Claude never has to re-run DCR after a
    restart."""
    monkeypatch.setattr("privacyfence.web.oauth_provider._clients_file_path", lambda: str(tmp_path / "clients.json"))
    first = OrgOAuthProvider(_idp(), idp_callback_url=f"{ISSUER}/oauth/idp/callback")
    from mcp.shared.auth import OAuthClientInformationFull
    from pydantic import AnyUrl

    await first.register_client(OAuthClientInformationFull(
        client_id="claude-1", redirect_uris=[AnyUrl(CLAUDE_REDIRECT_URI)], token_endpoint_auth_method="none",
    ))

    second = OrgOAuthProvider(_idp(), idp_callback_url=f"{ISSUER}/oauth/idp/callback")
    assert await second.get_client("claude-1") is not None


class TestIdpCallbackRouteErrorHandling:
    """The /oauth/idp/callback route's own error branches (routes_mcp.py's
    mount_org_oauth) -- OrgOAuthProvider.handle_idp_callback's own logic is
    covered directly in test_oauth_provider.py; this covers the HTTP-level
    wrapping around it."""

    async def test_idp_error_param_returns_400_not_500(self, tmp_path, monkeypatch):
        app, _provider, _session_manager = _build_app(tmp_path, monkeypatch)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ISSUER) as client:
            r = await client.get("/oauth/idp/callback", params={"error": "access_denied", "state": "whatever"})
        assert r.status_code == 400

    async def test_missing_state_or_code_returns_400(self, tmp_path, monkeypatch):
        app, _provider, _session_manager = _build_app(tmp_path, monkeypatch)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ISSUER) as client:
            missing_code = await client.get("/oauth/idp/callback", params={"state": "s"})
            missing_state = await client.get("/oauth/idp/callback", params={"code": "c"})
        assert missing_code.status_code == 400
        assert missing_state.status_code == 400

    async def test_provider_failure_returns_400_not_500(self, tmp_path, monkeypatch):
        app, provider, _session_manager = _build_app(tmp_path, monkeypatch)

        async def failing_callback(*, state, code):
            raise ValueError("invalid or expired authorization attempt")

        monkeypatch.setattr(provider, "handle_idp_callback", failing_callback)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ISSUER) as client:
            r = await client.get("/oauth/idp/callback", params={"state": "s", "code": "c"})
        assert r.status_code == 400
