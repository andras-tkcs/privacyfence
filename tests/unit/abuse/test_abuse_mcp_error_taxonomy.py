"""Adversarial coverage for the /mcp endpoint's tool-call error path
(web/routes_mcp.py's ``handle_call_tool``).

The invariant under test: whatever a connector raises while handling a
tool call, the fake secret it's carrying never reaches the
``CallToolResult`` handed back to the MCP client -- only either the
exception's own message (for a type on safe_errors.PUBLIC_SAFE_EXCEPTION_
TYPES, which this codebase's own control-flow raises never embed
third-party text into) or a fixed generic message (everything else,
including every connector's own ``*ClientError`` family -- see
safe_errors.py's module docstring for why those aren't trusted by type
alone). Full-chain: a real connector, a real McpDispatcher, the real ASGI
app, driven with the official MCP client -- see test_routes_mcp.py for the
non-adversarial version of this same rig.

test_safe_errors.py covers safe_errors.py's own functions in isolation;
this is the version that proves the wiring in routes_mcp.py actually calls
them.
"""
from __future__ import annotations

import contextlib

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from privacyfence.connector import Connector, ToolParam, ToolSpec
from privacyfence.gate import GateDeniedError
from privacyfence.safe_errors import GENERIC_PUBLIC_MESSAGE
from privacyfence.web.mcp_dispatch import McpDispatcher
from privacyfence.web.routes_mcp import build_mcp_asgi_app, mcp_lifespan

TOKEN = "mcp-error-taxonomy-test-token"


class ConnectorClientError(Exception):
    """Stand-in for gmail_client.GmailClientError and its siblings -- a
    connector's own exception type, not one of the four builtins
    safe_errors.py allowlists."""


class BoomConnector(Connector):
    """A connector whose one tool always raises whatever ``self._exc``
    factory produces -- stands in for a connector call reaching an
    ``except Exception`` deep in a real client (gmail_client.py and
    friends' own ``f"... failed: {exc}"`` wrapping pattern) that embeds a
    third party's own exception text, which can itself carry a secret."""

    def __init__(self, exc_factory) -> None:
        self._exc_factory = exc_factory

    @property
    def name(self) -> str:
        return "boom"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="boom_call",
                description="Always raises.",
                params=[ToolParam("arg", "str", required=False)],
                read_only=True,
            )
        ]

    async def call(self, tool: str, args: dict) -> object:
        raise self._exc_factory()


@contextlib.asynccontextmanager
async def _session_calling(exc_factory):
    dispatcher = McpDispatcher(lambda: {"boom": BoomConnector(exc_factory)})
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
                    yield session


# Realistic shapes a connector's own exception message can end up carrying
# once it wraps a third-party HTTP client's exception text (gmail_client.py
# and friends' "f'{op} failed: {exc}'" pattern) or, for the OAuth modules,
# an entire token-exchange response body.
FAKE_SECRET_PAYLOADS = [
    pytest.param(
        lambda: ConnectorClientError("list_messages failed: Authorization: Bearer sk-fake-secret-abcdef123456"),
        "sk-fake-secret-abcdef123456",
        id="bearer-header-in-wrapped-http-error",
    ),
    pytest.param(
        lambda: ConnectorClientError("token refresh failed: refresh_token=1//0gFakeRefreshTokenShape"),
        "1//0gFakeRefreshTokenShape",
        id="oauth-refresh-token-in-wrapped-error",
    ),
    pytest.param(
        lambda: ConnectorClientError(
            "Atlassian OAuth did not return an access token: "
            "{'access_token': 'fake-atlassian-access-token-value', 'token_type': 'Bearer'}"
        ),
        "fake-atlassian-access-token-value",
        id="stringified-oauth-response-dict",
    ),
    pytest.param(
        lambda: ConnectorClientError("session bootstrap failed: session_id=abcdefgh12345678"),
        "abcdefgh12345678",
        id="session-id-in-wrapped-error",
    ),
    pytest.param(
        # The actual real-world shape, not just a stand-in: every
        # connector's own `_fetch`-style helper catches its *ClientError
        # and re-raises exactly this way (connectors/gmail.py and friends,
        # per docs/coding-and-testing-guidelines.md §2.7) -- a bare
        # RuntimeError, not ConnectorClientError, is what actually reaches
        # routes_mcp.py's handle_call_tool for a real connector failure.
        lambda: RuntimeError("list_messages failed: access_token=ya29.a0-fake-google-access-token"),
        "ya29.a0-fake-google-access-token",
        id="bare-runtime-error-wrapping-a-client-error-the-real-shape",
    ),
]


class TestFakeSecretsNeverReachTheMcpResult:
    @pytest.mark.parametrize("exc_factory, secret", FAKE_SECRET_PAYLOADS)
    async def test_secret_is_absent_from_the_tool_error_result(self, exc_factory, secret):
        async with _session_calling(exc_factory) as session:
            result = await session.call_tool("boom_call", {})
        assert result.isError is True
        text = result.content[0].text
        assert secret not in text

    @pytest.mark.parametrize("exc_factory, secret", FAKE_SECRET_PAYLOADS)
    async def test_untyped_connector_exception_gets_the_generic_message(self, exc_factory, secret):
        # Not just "the secret is missing" -- confirms *why*: an exception
        # type this module doesn't recognise as self-authored gets the
        # fixed generic message, not a redacted-but-still-exception-derived
        # one (belt and suspenders against a redaction pattern this suite
        # didn't think of).
        async with _session_calling(exc_factory) as session:
            result = await session.call_tool("boom_call", {})
        assert result.content[0].text == GENERIC_PUBLIC_MESSAGE


class TestAllowlistedExceptionsStillReachTheClient:
    """The other side of the taxonomy: this codebase's own self-authored
    control-flow exceptions -- a ValueError for a caller-correctable
    problem, gate.py's own GateDeniedError for a denial -- still reach the
    client verbatim. SEC-10 narrows what's trusted, it doesn't make every
    tool error opaque; see the bare-RuntimeError payload above for the one
    RuntimeError shape that's deliberately *not* on this side of the line."""

    async def test_value_error_message_passes_through(self):
        async with _session_calling(lambda: ValueError("attachments: no such file: '/tmp/report.pdf'")) as session:
            result = await session.call_tool("boom_call", {})
        assert result.isError is True
        assert "no such file" in result.content[0].text

    async def test_gate_denied_error_message_passes_through(self):
        # The real type gate.py raises for a user/policy denial (SEC-10
        # promoted this from a bare RuntimeError specifically so
        # public_message() could tell it apart from a connector's wrapped
        # failure -- see safe_errors.py's module docstring).
        async with _session_calling(lambda: GateDeniedError("Request denied by user")) as session:
            result = await session.call_tool("boom_call", {})
        assert result.isError is True
        assert "Request denied by user" in result.content[0].text
