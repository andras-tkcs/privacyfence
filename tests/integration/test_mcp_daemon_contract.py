"""Real-socket contract test for the ``/mcp`` endpoint -- the P5 successor
of test_bridge_daemon_contract.py, which drove the real Node bridge over
real MCP-over-stdio against a real ``ipc_server.IPCServer``.

That test existed because the bridge (TypeScript) and the daemon (Python)
were two independently hand-maintained implementations of one wire
protocol, and nothing else in the suite proved they agreed with each
other -- tests/unit/web/test_routes_mcp.py drives the real ASGI app, but
over an in-process ``httpx.ASGITransport`` with no real socket
(docs/https-connector-refactor-plan.md §13: "no real socket"), so a
real-network-stack bug (uvicorn startup, real TCP binding, real HTTP
framing) could still slip through.

P5 (§12 of that document) deleted the bridge and ``ipc_server.py`` once
both had a stable release behind them. What is left to contract-test here
is narrower, and needs no Node at all: does a real ``web/server.py``
``WebServer``, bound to a real loopback socket, actually speak Streamable
HTTP correctly to the official ``mcp`` Python client -- the same client
Claude Code/Desktop itself uses, and mcpb/shim/'s own stdio proxy sits in
front of for Desktop (covered separately, with the shim included, by
tests/integration/test_shim_mcp_contract.py). Needs only the `mcp`
package (test-only, see pyproject.toml's [project.optional-dependencies]
.test) -- no Node, no npm, no subprocess.
"""

from __future__ import annotations

import shutil
import socket
import time
import uuid
from pathlib import Path

import httpx
import pytest

mcp_client = pytest.importorskip(
    "mcp", reason="mcp (Python MCP client, test-only) not installed -- pip install -e '.[test]'"
)
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from privacyfence import paths as paths_module  # noqa: E402
from privacyfence.connector import Connector, ToolParam, ToolSpec  # noqa: E402
from privacyfence.web.mcp_dispatch import McpDispatcher  # noqa: E402
from privacyfence.web.server import WebServer  # noqa: E402
from privacyfence.web_approval_ui import WebApprovalUI  # noqa: E402

pytestmark = pytest.mark.timeout(30)


class EchoConnector(Connector):
    """A minimal real connector -- exercises the manifest -> dynamic
    tool-registration -> tool-call path end to end, not a mocked stand-in.
    Deliberately the same shape as test_shim_mcp_contract.py's own
    EchoConnector -- both contract tests exercise the same kind of real
    round trip, just with a different client in front."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    @property
    def name(self) -> str:
        return "contract_test"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="contract_test_echo",
                description="Echoes its arguments back -- used only by this contract test.",
                params=[ToolParam("message", "str", required=True)],
                read_only=True,
            )
        ]

    async def call(self, tool: str, args: dict) -> object:
        self.calls.append((tool, args))
        return {"echoed": args}


def _free_port() -> int:
    """A real, currently-unused TCP port -- WebServer.start() doesn't report
    back the OS-assigned port for ``port=0`` (see web/server.py's own
    ``mcp_url`` property), so this test needs a real port number before
    starting the server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_connectable(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    last_exc: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError as exc:
            last_exc = exc
            time.sleep(0.05)
    raise TimeoutError(f"{host}:{port} never became connectable") from last_exc


@pytest.fixture
def mcp_home():
    """A tmp HOME so this test's web_token/mcp_token/mcp_url land under an
    isolated directory rather than the repo's own paths.data_dir()."""
    directory = Path(f"/tmp/pf-mcp-ct-{uuid.uuid4().hex[:8]}")
    (directory / ".privacyfence").mkdir(parents=True)
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def running_mcp_server(mcp_home, monkeypatch):
    monkeypatch.setattr(paths_module, "data_dir", lambda: mcp_home / ".privacyfence")

    connector = EchoConnector()
    dispatcher = McpDispatcher(lambda: {"contract_test": connector})
    port = _free_port()
    server = WebServer(WebApprovalUI(), host="localhost", port=port, mcp_dispatcher=dispatcher)
    server.start()
    try:
        _wait_until_connectable("localhost", port)
        yield connector, server
    finally:
        server.stop()


async def test_real_mcp_client_lists_and_calls_the_real_daemons_tools_over_a_real_socket(
    running_mcp_server,
):
    """The single highest-value assertion here: the official `mcp` Python
    client, over a real loopback TCP socket (not an in-process ASGI
    transport), discovers a tool the daemon actually registered and a real
    tool call round-trips through both sides' Streamable HTTP framing
    unmodified."""
    connector, server = running_mcp_server
    headers = {"Authorization": f"Bearer {server.mcp_token}"}

    async with httpx.AsyncClient(headers=headers) as http_client:
        async with streamable_http_client(server.mcp_url, http_client=http_client) as (read, write, _get_session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                assert "contract_test_echo" in names
                assert "privacyfence_check_policy" in names
                assert "privacyfence_begin_unattended_session" in names
                assert "privacyfence_end_unattended_session" in names

                result = await session.call_tool("contract_test_echo", {"message": "hello over a real socket"})
                assert result.isError is not True
                assert result.structuredContent == {"echoed": {"message": "hello over a real socket"}}

    assert connector.calls == [("contract_test_echo", {"message": "hello over a real socket"})]
