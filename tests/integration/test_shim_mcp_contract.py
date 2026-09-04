"""Cross-language contract test for the .mcpb shim (D11,
docs/https-connector-refactor-plan.md §12): does the real Node shim
(mcpb/shim/) actually speak Streamable HTTP to the real Python ``/mcp``
endpoint?

This is the shim's counterpart to test_bridge_daemon_contract.py -- same
reasoning, same shape (spawn the real built artifact, drive it with the
official ``mcp`` Python client over real stdio, assert the round trip
worked), a different transport underneath. §13 of that document names this
test explicitly: "P4b's shim (D11) gets its own, much smaller Node-side
test -- drive the shim's stdio transport with the mcp client against a real
/mcp, assert one initialize and one tools/call make the round trip with the
bearer header attached and the mcp_url file honoured. That is a passthrough
test, not a schema test: the shim knows no schemas, so there is nothing
else to assert."

Requires Node on PATH; skipped automatically otherwise -- same posture as
test_bridge_daemon_contract.py, and for the same reason this lives under
tests/integration/ rather than tests/unit/. Also requires the `mcp` package
(test-only, see pyproject.toml's [project.optional-dependencies].test).
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

mcp_client = pytest.importorskip(
    "mcp", reason="mcp (Python MCP client, test-only) not installed -- pip install -e '.[test]'"
)
from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

from privacyfence import paths as paths_module  # noqa: E402
from privacyfence.connector import Connector, ToolParam, ToolSpec  # noqa: E402
from privacyfence.web.mcp_dispatch import McpDispatcher  # noqa: E402
from privacyfence.web.server import WebServer  # noqa: E402
from privacyfence.web_approval_ui import WebApprovalUI  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIM_DIR = REPO_ROOT / "mcpb" / "shim"
SHIM_ENTRY = SHIM_DIR / "dist" / "shim.js"

pytestmark = [
    pytest.mark.skipif(
        shutil.which("node") is None,
        reason="Node not on PATH -- this contract test spawns the real shim",
    ),
    # npm install/build (only on the first run per session -- see
    # built_shim_entry) can be slow on a cold cache; the suite's global 30s
    # pytest-timeout is tuned for pure-Python socket tests, not this.
    #
    # This must stay above built_shim_entry's own subprocess timeouts (180s
    # install + 60s build = 240s): a slow-but-succeeding install needs room
    # to finish, and a genuinely stuck one should hit the fixture's own
    # subprocess.TimeoutExpired -> pytest.skip(...) path instead of being
    # killed here first (which surfaces as a hard failure, not a skip).
    pytest.mark.timeout(260),
]


class EchoConnector(Connector):
    """A minimal real connector -- exercises the manifest -> dynamic
    tool-registration -> tool-call path end to end, not a mocked stand-in.
    Deliberately the same shape as test_bridge_daemon_contract.py's
    EchoConnector -- both contract tests should be exercising the same kind
    of real round trip, just over different transports."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    @property
    def name(self) -> str:
        return "contract_test"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="contract_test_echo",
                description="Echoes its arguments back -- used only by the shim<->/mcp contract test.",
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
    ``mcp_url`` property, which is built from the ``port`` the caller
    passed in), so unlike a bridge test's ephemeral daemon socket, this test
    needs a real port number *before* starting the server."""
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
def shim_home():
    """A tmp HOME whose ~/.privacyfence/mcp_url and ~/.privacyfence/mcp_token
    both the real WebServer (monkeypatched to treat this directory as
    paths.data_dir() below) and the real shim subprocess (which derives
    them from $HOME exactly like production, via mcpb/shim/src/
    protocol.ts's os.homedir()) will agree on."""
    directory = Path(f"/tmp/pf-shim-ct-{uuid.uuid4().hex[:8]}")
    (directory / ".privacyfence").mkdir(parents=True)
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
async def running_mcp_server(shim_home, monkeypatch):
    # web/server.py and web/mcp_auth.py both resolve every file they write
    # (web_token, mcp_token, mcp_url) through paths.data_dir() -- patching
    # that one function is enough to redirect all of them into shim_home's
    # .privacyfence, matching what the real daemon does under a real HOME.
    monkeypatch.setattr(paths_module, "data_dir", lambda: shim_home / ".privacyfence")

    connector = EchoConnector()
    dispatcher = McpDispatcher(lambda: {"contract_test": connector})
    port = _free_port()
    server = WebServer(WebApprovalUI(), host="localhost", port=port, mcp_dispatcher=dispatcher)
    server.start()
    try:
        _wait_until_connectable("localhost", port)
        yield connector
    finally:
        server.stop()


@pytest.fixture(scope="session")
def built_shim_entry() -> Path:
    """(Re)builds mcpb/shim/dist/shim.js once per test session -- see
    test_bridge_daemon_contract.py's built_bridge_entry for the identical
    reasoning (skip rather than fail when npm/node aren't fully set up, so
    this test degrades gracefully in environments that only have `node` on
    PATH for other reasons)."""
    if shutil.which("npm") is None:
        pytest.skip("npm not on PATH -- this fixture builds the shim via `npm install`/`npm run build`")
    try:
        subprocess.run(
            ["npm", "install", "--silent"], cwd=SHIM_DIR, check=True, capture_output=True, timeout=180
        )
        subprocess.run(
            ["npm", "run", "build", "--silent"], cwd=SHIM_DIR, check=True, capture_output=True, timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"could not build mcpb/shim/dist/shim.js: {exc}")
    if not SHIM_ENTRY.exists():
        pytest.skip(f"{SHIM_ENTRY} missing after build")
    return SHIM_ENTRY


async def test_shim_proxies_a_real_initialize_and_tool_call_over_mcp(
    running_mcp_server, built_shim_entry, shim_home
):
    """The single highest-value assertion here: a real, freshly-built
    `node mcpb/shim/dist/shim.js`, given only $HOME (no config file, no
    token on the command line -- see mcpb/manifest.json.tmpl's
    server.mcp_config), discovers mcp_url/mcp_token itself, attaches the
    bearer header, and round-trips a real tool call through a real Python
    /mcp endpoint with neither side knowing the other's language."""
    params = StdioServerParameters(
        command="node",
        args=[str(built_shim_entry)],
        env={"HOME": str(shim_home)},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert "contract_test_echo" in names
            assert "privacyfence_check_policy" in names

            result = await session.call_tool(
                "contract_test_echo", {"message": "hello through the shim"}
            )
            assert result.isError is not True
            assert result.structuredContent == {"echoed": {"message": "hello through the shim"}}

    assert running_mcp_server.calls == [("contract_test_echo", {"message": "hello through the shim"})]
