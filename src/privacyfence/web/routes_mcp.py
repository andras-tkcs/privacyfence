"""The Streamable HTTP MCP endpoint -- what took over the original
``privacyfence-bridge``'s four jobs (find/launch the daemon, fetch the
manifest, register one MCP tool per ``ToolSpec``, forward calls) for a
client that talks to PrivacyFence directly, no intermediate process
required (docs/https-connector-refactor-plan.md §8.1). The bridge itself
was retired at P5, once this transport had shipped a stable release.

P2 scope only: this is a hosting change for the *transport*, not the
approval protocol. A gated call reaching a connector here still blocks on
whichever ``ApprovalUI`` ``approval_ui.init_approval_ui()`` currently
resolves to (native or web, per ``web.approval_ui`` -- unchanged from P1),
exactly like a call arriving over the bridge's IPC socket used to before P5
retired it. Deferred approvals, concurrent pending approvals, and
``privacyfence_await_approval`` are P3's ``_popup_lock`` retirement, not
this module's -- see docs/https-connector-refactor-plan.md §12's phase
table ("P2 before P3" is deliberate: the deferred protocol is written once,
on the transport it ships on, instead of being added to the bridge/IPC
protocol first and thrown away one phase later).

Built on the official MCP Python SDK's low-level ``Server`` (dynamic tool
registration -- the tool set depends on which connectors are currently
built, so it can't be the decorator-per-tool ``FastMCP`` surface) plus
``StreamableHTTPSessionManager`` (D2/D10 in
docs/https-connector-refactor-plan.md §15).
"""
from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from mcp import types
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware, get_access_token
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.routes import build_resource_metadata_url, create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.lowlevel.server import Server as MCPServer
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import AnyHttpUrl
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp

from .. import __version__ as PRIVACYFENCE_VERSION
from ..connector import Connector
from ..principal import principal_scope
from . import mcp_tools
from .mcp_auth import StaticTokenVerifier, principal_from_access_token
from .mcp_dispatch import McpDispatcher
from .oauth_provider import IDP_CALLBACK_PATH, OrgOAuthProvider

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"


def _session_key(server: MCPServer) -> str:
    """The current request's session key -- a fresh ``uuid4`` handed out
    once per Streamable HTTP session by ``_session_lifespan`` below and
    threaded through every request in that session via
    ``request_context.lifespan_context`` (the low-level ``Server``'s own
    per-session state slot -- see ``mcp.server.lowlevel.server.Server.run``,
    which enters ``self.lifespan(self)`` once per session, before that
    session's first request). Plays the same role as ``id(writer)`` in
    ipc_server.py: stable for one logical connection, and nothing more."""
    return server.request_context.lifespan_context["session_key"]


def build_mcp_server(dispatcher: McpDispatcher) -> MCPServer:
    """Builds the low-level MCP ``Server``, wired to ``dispatcher`` for both
    tool listing and tool calls. A fresh ``Server`` per daemon process
    (there's exactly one dispatcher, and its connector set can change live --
    see ``McpDispatcher.connectors``), not a decorator per connector tool:
    the tool set is only known at request time.
    """

    @contextlib.asynccontextmanager
    async def _session_lifespan(_: MCPServer) -> AsyncIterator[dict[str, Any]]:
        # Entered once per Streamable HTTP session, exited when that
        # session ends (normal close, idle timeout, or crash) -- see
        # _session_key's docstring. The `finally` here is the direct
        # counterpart of ipc_server.py's `_handle_connection`'s own
        # `finally` block clearing `id(writer)` from
        # `_unattended_connections` when a bridge connection drops.
        session_key = uuid.uuid4().hex
        try:
            yield {"session_key": session_key}
        finally:
            dispatcher.end_session(session_key)

    server: MCPServer = MCPServer(
        "privacyfence", version=PRIVACYFENCE_VERSION, lifespan=_session_lifespan,
    )

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        tools = [
            mcp_tools.to_mcp_tool(spec)
            for connector in dispatcher.connectors.values()
            for spec in connector.tool_specs()
        ]
        tools.extend(mcp_tools.META_TOOLS)
        return tools

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        session_key = _session_key(server)
        # Entered once per tool call, in the one place this surface
        # dispatches one (P6, docs/https-connector-refactor-plan.md §9.1) --
        # every per-principal registry downstream (auto_accept.py,
        # audit_log.py, pii_detector.py, privacy_filter.py,
        # resource_names.py) resolves against whatever this sets for the
        # rest of the call, including everything gate.py's gated_call()
        # does. Always LOCAL_PRINCIPAL today -- see
        # mcp_auth.principal_from_access_token's own docstring for why.
        principal = principal_from_access_token(get_access_token())
        with principal_scope(principal):
            try:
                if name in mcp_tools.META_TOOL_NAMES:
                    result = await _dispatch_meta_tool(dispatcher, session_key, name, arguments)
                else:
                    result = await _dispatch_connector_tool(dispatcher, session_key, name, arguments)
            except Exception as exc:  # noqa: BLE001 -- surfaced to the client as a tool error, not a
                # transport-level failure, exactly like ipc_server.py's own
                # `{"id": ..., "error": str(exc)}` response to a "call" request.
                logger.info("Tool call %s failed: %s", name, exc)
                return mcp_tools.error_result(str(exc))
        return mcp_tools.to_call_tool_result(result)

    return server


async def _dispatch_connector_tool(
    dispatcher: McpDispatcher, session_key: str, tool: str, arguments: dict[str, Any],
) -> Any:
    connector_name = _connector_for_tool(dispatcher.connectors, tool)
    if connector_name is None:
        raise ValueError(f"Unknown tool: {tool!r}")
    return await dispatcher.call(session_key, connector_name, tool, dict(arguments))


def _connector_for_tool(connectors: dict[str, Connector], tool: str) -> str | None:
    for connector in connectors.values():
        for spec in connector.tool_specs():
            if spec.name == tool:
                return connector.name
    return None


async def _dispatch_meta_tool(
    dispatcher: McpDispatcher, session_key: str, name: str, arguments: dict[str, Any],
) -> Any:
    reason = arguments.get("reason", "")
    if name == mcp_tools.CHECK_POLICY_TOOL.name:
        return dispatcher.check_policy(
            arguments["connector"], arguments["tool"], arguments.get("args") or {}, reason,
        )
    if name == mcp_tools.LIST_RULES_TOOL.name:
        return dispatcher.list_rules(reason)
    if name == mcp_tools.PROPOSE_RULE_CHANGE_TOOL.name:
        return await dispatcher.propose_rule_change(arguments)
    if name == mcp_tools.BEGIN_UNATTENDED_SESSION_TOOL.name:
        return dispatcher.begin_unattended_session(session_key, reason)
    if name == mcp_tools.END_UNATTENDED_SESSION_TOOL.name:
        return dispatcher.end_unattended_session(session_key, reason)
    if name == mcp_tools.AWAIT_APPROVAL_TOOL.name:
        return await dispatcher.await_approval(
            arguments.get("approval_ids") or [], arguments.get("timeout_seconds", 30),
        )
    raise ValueError(f"Unknown tool: {name!r}")  # pragma: no cover -- unreachable, META_TOOL_NAMES gates this


@contextlib.asynccontextmanager
async def mcp_lifespan(session_manager: StreamableHTTPSessionManager) -> AsyncIterator[None]:
    """The session manager's own ``run()`` task-group lifespan -- must stay
    open for as long as the ASGI app serving ``/mcp`` does (see
    ``StreamableHTTPSessionManager.run``'s own docstring). server.py folds
    this into the combined app's Starlette ``lifespan``."""
    async with session_manager.run():
        yield


class _StreamableHTTPASGIApp:
    """Thin class wrapper around ``session_manager.handle_request`` -- a
    plain async function would make Starlette's ``Route`` treat this
    endpoint as a ``func(request) -> response`` handler (defaulting to
    GET-only) instead of passing it the raw ASGI ``(scope, receive, send)``
    Streamable HTTP needs for GET/POST/DELETE alike; a class instance takes
    the raw-ASGI branch instead. Same reason the official SDK's own
    ``mcp.server.fastmcp.server.StreamableHTTPASGIApp`` exists.
    """

    def __init__(self, session_manager: StreamableHTTPSessionManager) -> None:
        self._session_manager = session_manager

    async def __call__(self, scope, receive, send) -> None:
        await self._session_manager.handle_request(scope, receive, send)


def build_mcp_asgi_app(
    dispatcher: McpDispatcher, *, token: str | None = None, verifier: TokenVerifier | None = None,
    resource_metadata_url: AnyHttpUrl | None = None,
) -> tuple[ASGIApp, StreamableHTTPSessionManager]:
    """Builds the ``/mcp`` endpoint app -- bearer-token authenticated,
    audience-separated from the approval surface's session cookie (§10.3).
    Returns the app alongside its session manager so server.py can fold
    ``mcp_lifespan`` into the combined app's own lifespan.

    ``verifier`` is the seam P7 plugs org mode into: pass
    ``web/oauth_provider.py``'s ``OrgOAuthProvider`` (which satisfies
    ``TokenVerifier`` via its own ``verify_token``) instead of building
    ``StaticTokenVerifier(token)`` for local mode's single shared secret --
    exactly one of ``token``/``verifier`` should be given.
    ``resource_metadata_url`` (RFC 9728, org mode only) is threaded into a
    401 response's ``WWW-Authenticate`` header so a client that gets one
    knows where to discover this server's authorization server; local
    mode has no such document to point to, so it stays ``None`` there.
    """
    server = build_mcp_server(dispatcher)
    session_manager = StreamableHTTPSessionManager(app=server, json_response=False, stateless=False)

    if verifier is None:
        if token is None:
            raise ValueError("build_mcp_asgi_app needs either token or verifier")
        verifier = StaticTokenVerifier(token)
    protected = RequireAuthMiddleware(
        _StreamableHTTPASGIApp(session_manager), required_scopes=[], resource_metadata_url=resource_metadata_url,
    )
    authenticated = AuthContextMiddleware(protected)
    app: ASGIApp = AuthenticationMiddleware(authenticated, backend=BearerAuthBackend(verifier))
    return app, session_manager


def mount_mcp(
    dispatcher: McpDispatcher, *, token: str | None = None, verifier: TokenVerifier | None = None,
    resource_metadata_url: AnyHttpUrl | None = None,
) -> tuple[Route, StreamableHTTPSessionManager]:
    """The ``/mcp`` route -- an exact-path ``Route`` with no ``methods``
    restriction (matches GET/POST/DELETE alike, exactly like the official
    SDK's own FastMCP wiring does for the same endpoint), not a ``Mount``:
    Streamable HTTP clients address this one path directly, with no
    sub-path routing underneath it.
    """
    app, session_manager = build_mcp_asgi_app(
        dispatcher, token=token, verifier=verifier, resource_metadata_url=resource_metadata_url,
    )
    return Route(MCP_PATH, endpoint=app), session_manager


def mount_org_oauth(provider: OrgOAuthProvider, *, issuer_url: str) -> list[Route]:
    """Org mode's OAuth 2.1 authorization-server + resource-metadata
    surface (P7, docs/https-connector-refactor-plan.md §9.4): the SDK's own
    ``create_auth_routes`` builds ``/.well-known/oauth-authorization-
    server``, ``/authorize``, ``/token``, ``/register`` (DCR) and
    ``/revoke`` against ``provider`` -- see that function's own module for
    why none of that protocol machinery is hand-rolled here (same D2
    reasoning as the MCP SDK itself). ``create_protected_resource_routes``
    builds the RFC 9728 ``/.well-known/oauth-protected-resource/mcp``
    document pointing at this same issuer. The one route the SDK has no
    opinion on -- ``provider``'s own IdP-facing callback -- is added
    alongside them; see ``OrgOAuthProvider.handle_idp_callback``'s own
    docstring for what it does.
    """
    issuer = AnyHttpUrl(issuer_url)
    resource_url = AnyHttpUrl(f"{issuer_url.rstrip('/')}{MCP_PATH}")
    routes = create_auth_routes(
        provider, issuer_url=issuer,
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    )
    routes.extend(create_protected_resource_routes(
        resource_url=resource_url, authorization_servers=[issuer], resource_name="PrivacyFence",
    ))

    async def idp_callback(request: Request) -> Response:
        idp_error = request.query_params.get("error")
        if idp_error:
            logger.info("MCP client authorization declined by IdP: %s", idp_error)
            return PlainTextResponse("Authorization was not completed.", status_code=400)
        state = request.query_params.get("state", "")
        code = request.query_params.get("code", "")
        if not state or not code:
            return PlainTextResponse("Invalid IdP callback.", status_code=400)
        try:
            redirect_url = await provider.handle_idp_callback(state=state, code=code)
        except Exception as exc:  # noqa: BLE001 -- any failure here ends the same way: authorization didn't complete
            logger.warning("MCP client authorization failed: %s", exc)
            return PlainTextResponse("Authorization failed. Please try again.", status_code=400)
        return RedirectResponse(redirect_url, status_code=302, headers={"Cache-Control": "no-store"})

    routes.append(Route(IDP_CALLBACK_PATH, idp_callback))
    return routes


def protected_resource_metadata_url(issuer_url: str) -> AnyHttpUrl:
    """The URL ``build_mcp_asgi_app``'s ``resource_metadata_url`` needs --
    factored out so server.py doesn't have to import ``mcp.server.auth.
    routes`` itself just to compute it."""
    return build_resource_metadata_url(AnyHttpUrl(f"{issuer_url.rstrip('/')}{MCP_PATH}"))


__all__ = [
    "MCP_PATH",
    "build_mcp_server",
    "build_mcp_asgi_app",
    "mount_mcp",
    "mcp_lifespan",
]
