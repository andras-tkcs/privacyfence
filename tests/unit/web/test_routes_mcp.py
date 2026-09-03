"""Tests for the /mcp Streamable HTTP endpoint (web/routes_mcp.py) --
the wire-protocol/auth layer sitting on top of McpDispatcher (see
test_mcp_dispatch.py for the dispatch logic itself).

Drives the real ASGI app with the official `mcp` Python client over an
in-process ASGI transport (httpx.ASGITransport) -- no real socket, per
docs/https-connector-refactor-plan.md §13 ("web/ unit tests: routes tested
against an in-process ASGI/HTTP test client, no real socket"). This is the
in-process equivalent of what P0 validated by hand (§11/§12 of that
document) and of what tests/integration/test_bridge_daemon_contract.py does
for the bridge, but for /mcp directly and without spawning a real process.
"""
from __future__ import annotations

import contextlib

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from privacyfence.connector import Connector, ToolParam, ToolSpec
from privacyfence.web.mcp_dispatch import McpDispatcher
from privacyfence.web.mcp_tools import META_TOOL_NAMES
from privacyfence.web.routes_mcp import build_mcp_asgi_app, mcp_lifespan


class EchoConnector(Connector):
    """A minimal real connector -- exercises manifest -> tool listing ->
    tool-call dispatch end to end, mirroring
    test_bridge_daemon_contract.py's own EchoConnector."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    @property
    def name(self) -> str:
        return "echo"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="echo_say",
                description="Echoes its arguments back.",
                params=[ToolParam("message", "str", required=True)],
                read_only=True,
            )
        ]

    async def call(self, tool: str, args: dict) -> object:
        self.calls.append((tool, args))
        return {"echoed": args}


TOKEN = "mcp-test-token"


@contextlib.asynccontextmanager
async def _connected_session(dispatcher: McpDispatcher, *, token: str = TOKEN):
    """Builds the /mcp app for ``dispatcher`` and yields a live, initialized
    ClientSession against it -- the happy-path fixture every wire-level test
    below starts from."""
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


def _raw_client(dispatcher: McpDispatcher, *, token: str = TOKEN) -> httpx.AsyncClient:
    app, _session_manager = build_mcp_asgi_app(dispatcher, token=token)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def _dispatcher(connectors: dict[str, Connector] | None = None, **kwargs) -> McpDispatcher:
    store = dict(connectors or {})
    return McpDispatcher(lambda: store, **kwargs)


_INIT_BODY = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}


# --------------------------------------------------------------------------- #
# Auth -- §10.3's audience separation starts here: no credential but the
# right bearer token gets past this layer at all.
# --------------------------------------------------------------------------- #

class TestAuth:
    async def test_missing_bearer_token_is_rejected(self):
        async with _raw_client(_dispatcher()) as client:
            resp = await client.post("/mcp", json=_INIT_BODY)
        assert resp.status_code == 401
        assert "Bearer" in resp.headers.get("www-authenticate", "")

    async def test_wrong_bearer_token_is_rejected(self):
        async with _raw_client(_dispatcher()) as client:
            resp = await client.post("/mcp", json=_INIT_BODY, headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

    async def test_correct_bearer_token_is_accepted(self):
        async with _connected_session(_dispatcher()):
            pass  # ClientSession.initialize() succeeding is the assertion.

    def test_build_mcp_asgi_app_requires_a_token_or_a_verifier(self):
        # P7: build_mcp_asgi_app grew an alternative to `token` (`verifier`,
        # for web/oauth_provider.py's OrgOAuthProvider) -- calling it with
        # neither is a caller bug, not a runtime condition to silently
        # tolerate.
        with pytest.raises(ValueError):
            build_mcp_asgi_app(_dispatcher())


# --------------------------------------------------------------------------- #
# Tool listing
# --------------------------------------------------------------------------- #

class TestListTools:
    async def test_lists_every_connector_tool_and_every_meta_tool(self):
        dispatcher = _dispatcher({"echo": EchoConnector()})
        async with _connected_session(dispatcher) as session:
            result = await session.list_tools()
        names = {t.name for t in result.tools}
        assert "echo_say" in names
        assert META_TOOL_NAMES <= names

    async def test_connector_tool_is_advertised_uniformly_read_only(self):
        # §8.1: every tool -- write tools included -- is advertised
        # read-only/non-destructive; the real gate is server-side.
        dispatcher = _dispatcher({"echo": EchoConnector()})
        async with _connected_session(dispatcher) as session:
            result = await session.list_tools()
        tool = next(t for t in result.tools if t.name == "echo_say")
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False

    async def test_reflects_a_live_connector_set_change_between_calls(self):
        store: dict[str, Connector] = {}
        dispatcher = McpDispatcher(lambda: store)
        async with _connected_session(dispatcher) as session:
            first = await session.list_tools()
            assert "echo_say" not in {t.name for t in first.tools}
            store["echo"] = EchoConnector()
            second = await session.list_tools()
            assert "echo_say" in {t.name for t in second.tools}


# --------------------------------------------------------------------------- #
# Calling a connector tool
# --------------------------------------------------------------------------- #

class TestCallConnectorTool:
    async def test_dispatches_to_the_connector_and_returns_structured_content(self):
        connector = EchoConnector()
        dispatcher = _dispatcher({"echo": connector})
        async with _connected_session(dispatcher) as session:
            result = await session.call_tool("echo_say", {"message": "hi"})
        assert result.isError is False
        assert result.structuredContent == {"echoed": {"message": "hi"}}
        assert connector.calls == [("echo_say", {"message": "hi"})]

    async def test_reason_is_popped_before_reaching_the_connector(self):
        connector = EchoConnector()
        dispatcher = _dispatcher({"echo": connector})
        async with _connected_session(dispatcher) as session:
            await session.call_tool("echo_say", {"message": "hi", "reason": "because"})
        assert connector.calls == [("echo_say", {"message": "hi"})]

    async def test_unknown_tool_is_a_tool_error_not_a_transport_error(self):
        dispatcher = _dispatcher({"echo": EchoConnector()})
        async with _connected_session(dispatcher) as session:
            result = await session.call_tool("not_a_real_tool", {})
        assert result.isError is True

    async def test_connector_exception_is_a_tool_error(self):
        class BoomConnector(EchoConnector):
            async def call(self, tool: str, args: dict) -> object:
                raise ValueError("boom")

        dispatcher = _dispatcher({"echo": BoomConnector()})
        async with _connected_session(dispatcher) as session:
            result = await session.call_tool("echo_say", {"message": "hi"})
        assert result.isError is True
        assert "boom" in result.content[0].text

    async def test_two_calls_in_one_session_share_dedupe_state(self):
        # Same MCP session -> same session_key -> the second identical call
        # is served from the completed-result cache instead of re-running
        # the connector (§6 job 2's coalescing, ported onto session_key).
        connector = EchoConnector()
        dispatcher = _dispatcher({"echo": connector})
        async with _connected_session(dispatcher) as session:
            await session.call_tool("echo_say", {"message": "hi"})
            await session.call_tool("echo_say", {"message": "hi"})
        assert len(connector.calls) == 1


# --------------------------------------------------------------------------- #
# Meta-tools -- one representative round trip per tool is enough here;
# test_mcp_dispatch.py already covers each one's own branch logic in depth.
# --------------------------------------------------------------------------- #

class TestMetaTools:
    async def test_check_policy_round_trips(self):
        # "gmail_list_messages" -- a real, globally-recognized (auto-gated)
        # tool name from auto_accept.TOOL_TO_GATE; check_policy looks that
        # up directly rather than through the connector's own tool_specs
        # (see mcp_dispatch.check_policy), so the connector fixture just has
        # to exist under this name, not actually expose that tool.
        dispatcher = _dispatcher({"echo": EchoConnector()})
        async with _connected_session(dispatcher) as session:
            result = await session.call_tool(
                "privacyfence_check_policy",
                {"connector": "echo", "tool": "gmail_list_messages", "reason": "planning"},
            )
        assert result.isError is False
        assert result.structuredContent["gate"] == "auto"

    async def test_begin_and_end_unattended_session_round_trip(self):
        dispatcher = _dispatcher({}, unattended_sessions_enabled=True)
        async with _connected_session(dispatcher) as session:
            begin = await session.call_tool("privacyfence_begin_unattended_session", {"reason": "scheduled"})
            assert begin.structuredContent == {"unattended": True}
            end = await session.call_tool("privacyfence_end_unattended_session", {"reason": "done"})
            assert end.structuredContent == {"unattended": False}

    async def test_begin_unattended_session_disabled_is_a_tool_error(self):
        dispatcher = _dispatcher({})  # unattended_sessions_enabled defaults False
        async with _connected_session(dispatcher) as session:
            result = await session.call_tool("privacyfence_begin_unattended_session", {"reason": "x"})
        assert result.isError is True
        assert "disabled" in result.content[0].text

    async def test_await_approval_round_trips_to_the_registry(self):
        # P3: privacyfence_await_approval, reaching the same registry a real
        # deferred approval would have registered into. No registry wired
        # here (no WebApprovalUI in this fixture), so every id comes back
        # "unknown" -- the wire round trip is what this test proves, the
        # status vocabulary itself is test_mcp_dispatch.py's job.
        dispatcher = _dispatcher({})
        async with _connected_session(dispatcher) as session:
            result = await session.call_tool(
                "privacyfence_await_approval", {"approval_ids": ["a1"], "timeout_seconds": 1},
            )
        assert result.isError is False
        assert result.structuredContent == {"a1": "unknown"}


# --------------------------------------------------------------------------- #
# Session lifecycle -- unattended-session cleanup on session end. Exercised
# at the McpDispatcher.end_session level in test_mcp_dispatch.py; this just
# confirms routes_mcp.py's own per-session lifespan actually calls it once
# the session ends, not just that end_session works in isolation.
# --------------------------------------------------------------------------- #

class TestSessionCleanup:
    async def test_ending_the_session_clears_its_unattended_flag(self):
        dispatcher = _dispatcher({}, unattended_sessions_enabled=True)
        app, session_manager = build_mcp_asgi_app(dispatcher, token=TOKEN)
        transport = httpx.ASGITransport(app=app)

        async with mcp_lifespan(session_manager):
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver", headers={"Authorization": f"Bearer {TOKEN}"},
            ) as http_client:
                async with streamable_http_client(
                    "http://testserver/mcp", http_client=http_client,
                ) as (read, write, _get_session_id):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        await session.call_tool("privacyfence_begin_unattended_session", {"reason": "x"})
                        assert dispatcher.unattended_session_count() == 1
                # streamable_http_client's own __aexit__ sends the DELETE
                # that terminates this Streamable HTTP session -- awaited
                # above, so by the time we're back here the per-session
                # lifespan's `finally` (routes_mcp.py's _session_lifespan)
                # has already run.
        assert dispatcher.unattended_session_count() == 0
