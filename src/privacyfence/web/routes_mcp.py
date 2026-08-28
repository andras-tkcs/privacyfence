"""The Streamable HTTP MCP endpoint -- what replaces the bridge's four jobs
(find/launch the daemon, fetch the manifest, register one MCP tool per
``ToolSpec``, forward calls) for a client that talks to PrivacyFence
directly instead of through ``privacyfence-bridge``
(docs/https-connector-refactor-plan.md §8.1).

P2 scope only: this is a hosting change for the *transport*, not the
approval protocol. A gated call reaching a connector here still blocks on
whichever ``ApprovalUI`` ``approval_ui.init_approval_ui()`` currently
resolves to (native or web, per ``web.approval_ui`` -- unchanged from P1),
exactly like a call arriving over the bridge's IPC socket does today.
Deferred approvals, concurrent pending approvals, and
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
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.lowlevel.server import Server as MCPServer
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.routing import Route
from starlette.types import ASGIApp

from .. import __version__ as PRIVACYFENCE_VERSION
from ..connector import Connector
from . import mcp_tools
from .mcp_auth import StaticTokenVerifier
from .mcp_dispatch import McpDispatcher

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


def build_mcp_asgi_app(dispatcher: McpDispatcher, *, token: str) -> tuple[ASGIApp, StreamableHTTPSessionManager]:
    """Builds the ``/mcp`` endpoint app -- bearer-token authenticated
    (mcp_auth.py), audience-separated from the approval surface's session
    cookie (§10.3; see mcp_auth.py's own docstring). Returns the app
    alongside its session manager so server.py can fold ``mcp_lifespan``
    into the combined app's own lifespan.
    """
    server = build_mcp_server(dispatcher)
    session_manager = StreamableHTTPSessionManager(app=server, json_response=False, stateless=False)

    verifier = StaticTokenVerifier(token)
    protected = RequireAuthMiddleware(_StreamableHTTPASGIApp(session_manager), required_scopes=[])
    authenticated = AuthContextMiddleware(protected)
    app: ASGIApp = AuthenticationMiddleware(authenticated, backend=BearerAuthBackend(verifier))
    return app, session_manager


def mount_mcp(dispatcher: McpDispatcher, *, token: str) -> tuple[Route, StreamableHTTPSessionManager]:
    """The ``/mcp`` route -- an exact-path ``Route`` with no ``methods``
    restriction (matches GET/POST/DELETE alike, exactly like the official
    SDK's own FastMCP wiring does for the same endpoint), not a ``Mount``:
    Streamable HTTP clients address this one path directly, with no
    sub-path routing underneath it.
    """
    app, session_manager = build_mcp_asgi_app(dispatcher, token=token)
    return Route(MCP_PATH, endpoint=app), session_manager


__all__ = [
    "MCP_PATH",
    "build_mcp_server",
    "build_mcp_asgi_app",
    "mount_mcp",
    "mcp_lifespan",
]
