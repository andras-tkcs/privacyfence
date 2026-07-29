"""Cross-language contract test: does the real Node bridge (bridge/) actually
speak the same wire protocol as the real Python daemon's IPCServer?

Nothing else in the test suite answers this. bridge/test/*.test.ts exercises
the bridge against hand-written Node fakes of the daemon (testDaemon.ts and
friends); tests/unit/test_ipc_server.py exercises the daemon against a
hand-rolled Python client. Both encode one side's *understanding* of the
protocol at the time they were written -- neither ever talks to the other
side's real process, so a drift in either implementation (a renamed field, a
changed error shape, a manifest change) would only be caught by a human
running a real Claude session, not by CI.

This spawns a real `node bridge/dist/bridge.js` (or, for the version-mismatch
case, the unbundled TypeScript source under tsx -- see
test_bridge_refuses_a_stale_daemon_version) and a real privacyfence.ipc_server
.IPCServer, and drives the bridge with the official `mcp` Python client over
real MCP-over-stdio -- the same protocol Claude Desktop/Code actually speaks
to it.

Requires Node on PATH; skipped automatically otherwise, so the default
`pytest tests/unit` run (no Node dependency) is unaffected -- this lives
under tests/integration/ specifically so it's easy to select or exclude by
path too. Also requires the `mcp` package (test-only -- see
pyproject.toml's [project.optional-dependencies].test; never shipped in the
daemon or the bridge, only used here to drive a real MCP client).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

mcp_client = pytest.importorskip(
    "mcp", reason="mcp (Python MCP client, test-only) not installed -- pip install -e '.[test]'"
)
from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

from privacyfence import __version__ as REAL_VERSION  # noqa: E402
from privacyfence import ipc_server as ipc_server_module  # noqa: E402
from privacyfence.connector import Connector, ToolParam, ToolSpec  # noqa: E402
from privacyfence.ipc_server import IPCServer  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_DIR = REPO_ROOT / "bridge"
BRIDGE_ENTRY = BRIDGE_DIR / "dist" / "bridge.js"

pytestmark = [
    pytest.mark.skipif(
        shutil.which("node") is None,
        reason="Node not on PATH -- this contract test spawns the real bridge",
    ),
    # npm install/build (only on the first run per session -- see
    # built_bridge_entry) can be slow on a cold cache; the suite's global
    # 30s pytest-timeout is tuned for pure-Python socket tests, not this.
    pytest.mark.timeout(120),
]


class EchoConnector(Connector):
    """A minimal real connector -- exercises the manifest -> dynamic
    tool-registration -> tool-call path end to end, not a mocked stand-in."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    @property
    def name(self) -> str:
        return "contract_test"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="contract_test_echo",
                description="Echoes its arguments back -- used only by the bridge<->daemon contract test.",
                params=[ToolParam("message", "str", required=True)],
                read_only=True,
            )
        ]

    async def call(self, tool: str, args: dict) -> object:
        self.calls.append((tool, args))
        return {"echoed": args}


@pytest.fixture
def bridge_home():
    """A tmp HOME whose ~/.privacyfence/privacyfence.sock both the real
    IPCServer (monkeypatched to this exact path below) and the real bridge
    subprocess (which derives its socket path from $HOME exactly like
    production) will agree on. Short path, same AF_UNIX sun_path constraint
    as tests/unit/test_ipc_server.py's short_socket_path."""
    directory = Path(f"/tmp/pf-ct-{uuid.uuid4().hex[:8]}")
    (directory / ".privacyfence").mkdir(parents=True)
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
async def running_daemon(bridge_home, monkeypatch):
    socket_path = str(bridge_home / ".privacyfence" / "privacyfence.sock")
    monkeypatch.setattr(ipc_server_module, "SOCKET_PATH", socket_path)
    connector = EchoConnector()
    server = IPCServer([connector])
    await server.start()
    try:
        yield connector
    finally:
        await server.stop()


@pytest.fixture(scope="session")
def built_bridge_entry() -> Path:
    """(Re)builds bridge/dist/bridge.js with BRIDGE_VERSION pinned to this
    checkout's real version, once per test session, so
    test_bridge_lists_and_calls_the_real_daemons_tools always compares two
    versions that are actually supposed to match -- not whatever version
    happened to be baked in by someone's last manual `npm run build`.
    esbuild's define bakes BRIDGE_VERSION into the bundle at build time (see
    bridge/build.mjs), so it can't be overridden by env var at spawn time
    the way the unbundled dev path can (see
    test_bridge_refuses_a_stale_daemon_version).
    """
    try:
        subprocess.run(
            ["npm", "install", "--silent"], cwd=BRIDGE_DIR, check=True, capture_output=True, timeout=180
        )
        subprocess.run(
            ["npm", "run", "build", "--silent"],
            cwd=BRIDGE_DIR,
            check=True,
            capture_output=True,
            timeout=60,
            env={**os.environ, "BRIDGE_VERSION": REAL_VERSION},
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"could not build bridge/dist/bridge.js: {exc}")
    if not BRIDGE_ENTRY.exists():
        pytest.skip(f"{BRIDGE_ENTRY} missing after build")
    return BRIDGE_ENTRY


async def test_bridge_lists_and_calls_the_real_daemons_tools(running_daemon, built_bridge_entry, bridge_home):
    """The single highest-value assertion here: a real, freshly-built
    `node bridge/dist/bridge.js`, talking to a real IPCServer, discovers a
    tool the daemon actually registered (via the real manifest wire format)
    and a real tool call round-trips through both languages' framing/
    serialization assumptions unmodified."""
    params = StdioServerParameters(
        command="node",
        args=[str(built_bridge_entry)],
        env={"HOME": str(bridge_home)},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert "contract_test_echo" in names
            assert "privacyfence_check_policy" in names
            assert "privacyfence_begin_unattended_session" in names
            assert "privacyfence_end_unattended_session" in names

            result = await session.call_tool("contract_test_echo", {"message": "hello from the real daemon"})
            assert result.isError is not True
            assert result.structuredContent == {"echoed": {"message": "hello from the real daemon"}}

    assert running_daemon.calls == [("contract_test_echo", {"message": "hello from the real daemon"})]


async def test_bridge_refuses_a_stale_daemon_version(running_daemon, bridge_home):
    """The version-mismatch guard is exactly the kind of behavior that's easy
    for two independent implementations to silently drift on (different
    message wording, different exit code, or one side forgetting to check at
    all). Runs the unbundled TypeScript source under tsx rather than the
    bundled dist/bridge.js: esbuild bakes BRIDGE_VERSION in at build time
    (see built_bridge_entry's docstring), but manifest.ts's checkVersionMatch
    logic itself is identical either way -- only the packaging differs, and
    that's already covered by scripts/build_mcpb.sh actually succeeding.
    """
    if not (BRIDGE_DIR / "node_modules" / ".bin" / "tsx").exists():
        pytest.skip("bridge/node_modules not installed -- run `cd bridge && npm install` first")

    params = StdioServerParameters(
        command="node",
        args=["--import", "tsx", "src/index.ts"],
        cwd=str(BRIDGE_DIR),
        env={"HOME": str(bridge_home), "BRIDGE_VERSION": "0.0.1-deliberately-stale"},
    )
    with pytest.raises(Exception):  # noqa: B017 -- broken pipe / connection closed, exact type is mcp/anyio internals
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
