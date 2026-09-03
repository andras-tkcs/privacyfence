"""Tests that the /mcp endpoint's own principal_scope() entry point (P6,
docs/https-connector-refactor-plan.md §9.1) actually scopes each tool call --
see test_routes_mcp.py for the wire-protocol tests this borrows its fixture
shape from.
"""
from __future__ import annotations

import contextlib

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from privacyfence.connector import Connector, ToolSpec
from privacyfence.principal import LOCAL_PRINCIPAL_ID, current_principal
from privacyfence.web.mcp_dispatch import McpDispatcher
from privacyfence.web.routes_mcp import build_mcp_asgi_app, mcp_lifespan

TOKEN = "mcp-test-token"


class PrincipalEchoingConnector(Connector):
    """Reports whatever current_principal() resolves to at call time, so a
    wire-level test can observe what routes_mcp.py's handle_call_tool
    actually scoped -- the same role EchoConnector plays in
    test_routes_mcp.py, but for principal_scope() instead of the plain
    argument round-trip."""

    @property
    def name(self) -> str:
        return "principal_echo"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="whoami", description="Returns the current principal id.",
                params=[], read_only=True,
            )
        ]

    async def call(self, tool: str, args: dict) -> object:
        return {"principal_id": current_principal().id}


@contextlib.asynccontextmanager
async def _connected_session(dispatcher: McpDispatcher, *, token: str = TOKEN):
    app, session_manager = build_mcp_asgi_app(dispatcher, token=token)
    transport = httpx.ASGITransport(app=app)

    async with mcp_lifespan(session_manager):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", headers={"Authorization": f"Bearer {token}"},
        ) as http_client:
            async with streamable_http_client(
                "http://testserver/mcp", http_client=http_client,
            ) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session


async def test_a_tool_call_over_mcp_sees_the_local_principal():
    # Today's only reachable outcome: StaticTokenVerifier only ever mints
    # client_id="local" (see mcp_auth.py's own docstring), so
    # principal_from_access_token always resolves to LOCAL_PRINCIPAL -- this
    # is the wire-level proof that routes_mcp.py's handle_call_tool actually
    # enters that scope around dispatch, not just that the helper function
    # computes the right value in isolation (see test_mcp_auth.py for that).
    dispatcher = McpDispatcher(lambda: {"principal_echo": PrincipalEchoingConnector()})
    async with _connected_session(dispatcher) as session:
        result = await session.call_tool("whoami", {"reason": "test"})
        assert result.structuredContent == {"principal_id": LOCAL_PRINCIPAL_ID}


async def test_principal_scope_does_not_leak_outside_the_call():
    dispatcher = McpDispatcher(lambda: {"principal_echo": PrincipalEchoingConnector()})
    async with _connected_session(dispatcher) as session:
        await session.call_tool("whoami", {"reason": "test"})
    # Back outside any request -- must be the default, not whatever the
    # request happened to scope (they're the same value today, but the
    # mechanism -- principal_scope's __exit__ resetting the contextvar --
    # is what this actually checks).
    assert current_principal().id == LOCAL_PRINCIPAL_ID
