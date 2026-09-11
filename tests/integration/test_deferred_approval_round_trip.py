"""End-to-end deferred-approval round trip (TST-09,
docs/security-remediation-plan.md Phase 3.12): drives the full P3 deferred
protocol (gate.py's own module docstring) against a real, socket-bound
web/server.py WebServer -- the official ``mcp`` Python client for the
tool-call side, a real ``httpx`` POST against the real decide route for the
human-decision side -- rather than in-process unit coverage of each half
separately. tests/unit/test_gate.py proves gated_call's own state machine
and tests/unit/web/test_routes_approvals.py proves the decide route in
isolation, but nothing before this test proved that a human deciding
through the real HTTP approval surface actually unblocks a second,
identical MCP tool call the way docs/https-connector-refactor-plan.md §5
describes end to end: register-or-coalesce -> hold_window elapses ->
"approval_pending" returned to the MCP client -> POST /api/approvals/{id}/
decide -> the *next* identical call finds the decision in approvals.py's
ledger and releases without a second prompt.

Deliberately reuses test_mcp_daemon_contract.py's own real-socket-server
fixture shape (free port, wait-until-connectable, a minimal real Connector)
rather than a mocked transport -- see that module's own docstring for why
an in-process ASGI transport isn't enough to catch every real-network-stack
bug.
"""
from __future__ import annotations

import socket
import time
import uuid

import httpx
import pytest

mcp_client = pytest.importorskip(
    "mcp", reason="mcp (Python MCP client, test-only) not installed -- pip install -e '.[test]'"
)
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from privacyfence import approval_ui  # noqa: E402
from privacyfence import gate  # noqa: E402
from privacyfence import paths as paths_module  # noqa: E402
from privacyfence.approvals import PendingApprovalRegistry  # noqa: E402
from privacyfence.audit_log import init_audit_logger  # noqa: E402
from privacyfence.connector import Connector, ToolParam, ToolSpec  # noqa: E402
from privacyfence.web.mcp_dispatch import McpDispatcher  # noqa: E402
from privacyfence.web.server import WebServer  # noqa: E402
from privacyfence.web_approval_ui import WebApprovalUI  # noqa: E402

# docs/automated-test-strategy-plan.md Phase 0: real internal stack (a real
# socket-bound WebServer, the official mcp client, a real httpx POST), no
# external network -- integration per testing-policy.md's seven-layer
# taxonomy.
pytestmark = [pytest.mark.timeout(30), pytest.mark.integration]


class GatedTestConnector(Connector):
    """A minimal real connector with one write (``gate="popup"``) tool that
    actually goes through ``gate.gated_call`` -- unlike
    test_mcp_daemon_contract.py's own EchoConnector, which is deliberately
    gate-free (that test proves transport, not the gate). Real enough to
    exercise the deferred protocol end to end without pulling in any one
    real connector's client/auth machinery.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    @property
    def name(self) -> str:
        return "gated_test"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="gated_test_send",
                description="Sends a test message -- write-gated, used only by this integration test.",
                params=[
                    ToolParam("message", "str", required=True),
                    ToolParam(
                        "reason", "str", required=True,
                        description="One sentence: why are you calling this tool right now?",
                    ),
                ],
            )
        ]

    async def call(self, tool: str, args: dict) -> object:
        self.calls.append((tool, args))
        message = args["message"]
        return await gate.gated_call(
            connector=self.name, tool=tool, tool_name="Send test message",
            summary=f"Send: {message}", sender="", raw_data={"message": message},
            filtered_data={"message": message}, gate="popup",
            preview={"Message": message}, details_text=message, args=args,
        )


def _free_port() -> int:
    """A real, currently-unused TCP port -- see
    test_mcp_daemon_contract.py's identical helper for why this is needed
    up front rather than letting the OS pick one."""
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
def running_deferred_server(tmp_path, monkeypatch):
    home = tmp_path / f"pf-deferred-{uuid.uuid4().hex[:8]}" / ".privacyfence"
    home.mkdir(parents=True)
    monkeypatch.setattr(paths_module, "data_dir", lambda: home)
    init_audit_logger(str(tmp_path / "audit"))

    connector = GatedTestConnector()
    dispatcher = McpDispatcher(lambda: {"gated_test": connector})
    # A hold window short enough that the first call reliably goes pending
    # inside this test's own patience, with no artificial sleep needed to
    # force it: the interaction really is still running (nothing has
    # answered it yet) -- gated_call just stops waiting on it after this
    # many seconds, exactly as it would in production once a human takes
    # longer than the (much larger, real) default to decide.
    registry = PendingApprovalRegistry(hold_window=0.05, pending_ttl=30.0, ledger_ttl=30.0)
    web_ui = WebApprovalUI(registry=registry)
    approval_ui.init_approval_ui(web_ui)

    port = _free_port()
    server = WebServer(web_ui, host="localhost", port=port, mcp_dispatcher=dispatcher)
    server.start()
    try:
        _wait_until_connectable("localhost", port)
        yield connector, server
    finally:
        server.stop()


async def _call_gated_tool(server: WebServer, message: str):
    headers = {"Authorization": f"Bearer {server.mcp_token}"}
    async with httpx.AsyncClient(headers=headers) as http_client:
        async with streamable_http_client(server.mcp_url, http_client=http_client) as (read, write, _get_session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(
                    "gated_test_send", {"message": message, "reason": "integration test round trip"},
                )


async def _decide(server: WebServer, approval_id: str, result: str) -> httpx.Response:
    """Exactly what a human's browser does when they click Allow/Deny on a
    card at /approvals/{id} -- the session cookie doubles as the CSRF token
    (session_auth.py's own check_csrf), same as
    tests/unit/web/test_routes_approvals.py's ``_signed_in`` helper."""
    session_id = server.sessions.create()
    async with httpx.AsyncClient(
        base_url=f"http://localhost:{server.port}", cookies={"pf_session": session_id},
    ) as http_client:
        return await http_client.post(
            f"/api/approvals/{approval_id}/decide",
            json={"result": result, "csrf": session_id},
        )


async def test_deferred_approval_round_trip_accept(running_deferred_server):
    """The full P3 protocol, driven for real: a gated write's hold window
    elapses and comes back as approval_pending; deciding it Allow through
    the real HTTP decide route a human would use releases the data; a
    second, identical tool call then finds the decision already in the
    ledger and returns the real result -- with no second interaction ever
    started (proven by list_pending being empty afterwards, not just by
    the second call succeeding)."""
    connector, server = running_deferred_server

    first = await _call_gated_tool(server, "hello from the round trip test")
    assert first.isError is not True
    assert first.structuredContent["status"] == "approval_pending"
    approval_id = first.structuredContent["approval_id"]
    assert approval_id

    response = await _decide(server, approval_id, "accept")
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok"}

    second = await _call_gated_tool(server, "hello from the round trip test")
    assert second.isError is not True
    assert second.structuredContent == {"message": "hello from the round trip test"}

    # Both MCP calls reached the connector (dedupe never short-circuits a
    # pending result -- see mcp_dispatch.py's own comment on that), but
    # only one interaction was ever registered: nothing is left pending
    # after the second call resolved via the ledger, not a fresh popup.
    assert len(connector.calls) == 2
    assert not (await _wait_and_list_pending(server))


async def _wait_and_list_pending(server: WebServer) -> list:
    # web_ui isn't kept around by the fixture beyond construction, but
    # gate.py's global approval_ui singleton still points at the exact
    # instance the fixture built -- same object the /approvals routes serve
    # from, per web/server.py's own module docstring.
    return approval_ui.get_approval_ui().deferred_registry.list_pending()


async def test_deferred_approval_round_trip_deny(running_deferred_server):
    """Same shape, but a human clicking Deny must also reach the second
    call as a released (not re-prompted) outcome: gated_call raises
    GateDeniedError on a real deny (gate.py), which the MCP layer surfaces
    as an isError result rather than blocking on a third prompt."""
    connector, server = running_deferred_server

    first = await _call_gated_tool(server, "please deny me")
    assert first.structuredContent["status"] == "approval_pending"
    approval_id = first.structuredContent["approval_id"]

    response = await _decide(server, approval_id, "deny")
    assert response.status_code == 200, response.text

    second = await _call_gated_tool(server, "please deny me")
    assert second.isError is True

    assert len(connector.calls) == 2
    assert not (await _wait_and_list_pending(server))
