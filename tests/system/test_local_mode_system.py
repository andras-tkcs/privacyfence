"""Canonical cross-platform system test (docs/automated-test-strategy-plan.md
Phase 3): one real daemon -> MCP -> approval -> audit scenario, proven
identical on ``ubuntu-latest``, ``windows-latest``, and ``macos-latest`` --
no new CI wiring needed for that (``pyproject.toml``'s ``testpaths =
["tests"]`` already collects this module wherever the full suite already
runs, same as Phase 2.3's ``tests/platform/`` before it).

Deliberately combines two patterns this repo already has, rather than
reinventing either (per this phase's own text in the plan, and per Phase
2.3's own status note: "Phase 3 should reuse this module's spawn/isolation
pattern rather than reinventing it, the same way its own text already says
to reuse test_mcp_daemon_contract.py's"):

- **Real process boundary** -- tests/platform/test_daemon_process_lifecycle.py's
  spawn technique: ``python -c <bootstrap>`` monkeypatches
  ``privacyfence.paths.data_dir`` to an isolated sandbox *before*
  ``privacyfence.daemon_main`` is ever imported, then runs the real
  ``daemon_main.main([])`` entry point as a genuinely separate OS process --
  the same one a systemd unit/LaunchAgent/mcpb shim spawn call would run.
  Every other daemon/MCP/approval test in this repo (including
  test_mcp_daemon_contract.py and test_deferred_approval_round_trip.py)
  drives ``WebServer``/``daemon_main`` functions as a plain in-process call;
  this is the one system-level scenario that also proves the process
  *boundary* itself -- real socket bind, real ``mcp_url``/lock-file
  discovery, a real HTTP round trip from a separate process, real clean
  shutdown -- identically across all three OSes.
- **MCP client + deferred-approval protocol** -- the official ``mcp`` Python
  client (test_mcp_daemon_contract.py) driving the real deferred-approval
  round trip (test_deferred_approval_round_trip.py: hold-window elapses ->
  ``approval_pending`` -> a real HTTP decide -> a second identical call
  finds the ledger and releases). Since this test's daemon is a real
  subprocess (not an in-process ``WebServer``), there is no shared Python
  object to hand a synthetic connector to the way those two in-process
  tests do -- so the bootstrap script below defines its own minimal
  ``Connector`` (same shape as test_deferred_approval_round_trip.py's own
  ``GatedTestConnector``, necessarily reproduced rather than imported since
  it has to exist inside the spawned process's own ``-c`` script) and
  monkeypatches ``daemon_main.build_connectors`` to return it instead of the
  real, credential-backed connector set -- no live provider needed, same as
  every other daemon/MCP test in this repo.

What's genuinely new here versus the modules above: a real daemon *process*
speaking the full contract end to end -- ``/mcp`` tool discovery and a
gated call, the real ``/approvals``/``/settings`` web surfaces reached via
a real one-time bootstrap exchange (``POST /api/bootstrap`` plus the real
``?bootstrap=`` redirect -- not a directly-constructed session, the way
test_deferred_approval_round_trip.py's own ``_decide`` helper takes a
shortcut only available in-process; see ``_bootstrap_session``'s own
docstring for why this mints its own code via that endpoint rather than
reading the link the daemon logs at startup), a real Allow and a real Deny
each resolved through that HTTP surface, the audit log inspected from disk
afterwards, and a graceful shutdown via the real
``POST /api/settings/quit_app`` action (proc.terminate()/SIGTERM, as
test_daemon_process_lifecycle.py's own termination test uses, does not
reliably exercise the same code path -- see run_app()'s own ``finally``
block, which only runs when ``_wait_for_shutdown()`` returns normally).

Cross-platform assertions are deliberately light on top of the shared
scenario -- Phase 2.3's own grounding pass already put OS-level
path/lock/process coverage under ``tests/platform/`` (state/config path
resolution, secure-directory creation, single-instance locking across a
real process boundary, daemon process spawning/discovery/cleanup);
duplicating any of that here would be pure churn. What this module adds on
top is the one thing tests/platform/ deliberately doesn't cover: that the
secure-directory permission floor (SEC-09's ``secure_mkdir``) that backs
every one of those state paths is actually in effect on the *audit log*
directory this exact scenario just wrote to, on every POSIX OS this runs
on (skipped on Windows, where ``Path.chmod`` doesn't carry the same
meaning -- same posture as every other POSIX-only permission assertion in
this repo, e.g. tests/unit/test_secure_files.py's own).
"""
from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
import yaml

import privacyfence

mcp_client = pytest.importorskip(
    "mcp", reason="mcp (Python MCP client, test-only) not installed -- pip install -e '.[test]'"
)
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

# A real daemon subprocess plus several real HTTP round trips (tool calls,
# bootstrap exchanges, decide calls) takes meaningfully longer than the
# in-process contract tests' own timeout=30 -- generous rather than tight,
# since a slow/loaded CI runner (this runs on all three OS jobs) failing on
# wall-clock alone would be a false negative, not a real bug caught.
pytestmark = [pytest.mark.system, pytest.mark.timeout(90)]

# Runs *before* `from privacyfence import daemon_main` -- daemon_main.py
# computes PROJECT_ROOT (== data_dir()) as a module-level constant at
# import time, so the patch below must land first (identical technique to
# tests/platform/test_daemon_process_lifecycle.py's own ``_BOOTSTRAP`` --
# see that module's docstring for why this doesn't fake sys.frozen
# instead). sys.argv[1] is the sandbox directory to use.
#
# ``daemon_main.build_connectors`` is monkeypatched to hand back one
# synthetic, real (not mocked) connector with a single write (gate="popup")
# tool -- the same shape test_deferred_approval_round_trip.py's own
# GatedTestConnector uses, reproduced here rather than imported since this
# script runs inside the spawned process, with no access back into this
# test module's own namespace.
_BOOTSTRAP = """
import sys
from pathlib import Path

from privacyfence import paths

home = Path(sys.argv[1])
paths.data_dir = lambda: paths.secure_mkdir(home)

from privacyfence import daemon_main
from privacyfence import gate
from privacyfence.connector import Connector, ToolParam, ToolSpec


class SystemTestConnector(Connector):
    @property
    def name(self):
        return "system_test"

    def tool_specs(self):
        return [
            ToolSpec(
                name="system_test_send",
                description=(
                    "Sends a test message -- write-gated, used only by "
                    "tests/system/test_local_mode_system.py."
                ),
                params=[
                    ToolParam("message", "str", required=True),
                    ToolParam(
                        "reason", "str", required=True,
                        description="One sentence: why are you calling this tool right now?",
                    ),
                ],
            )
        ]

    async def call(self, tool, args):
        message = args["message"]
        return await gate.gated_call(
            connector=self.name, tool=tool, tool_name="Send test message",
            summary=f"Send: {message}", sender="", raw_data={"message": message},
            filtered_data={"message": message}, gate="popup",
            preview={"Message": message}, details_text=message, args=args,
        )


daemon_main.build_connectors = lambda config, org_config: [SystemTestConnector()]
sys.exit(daemon_main.main([]))
"""


def _free_port() -> int:
    """A real, currently-unused TCP port -- ``WebServer.start()`` doesn't
    report back the OS-assigned port for ``port=0`` (its ``mcp_url``
    property builds the URL from the *configured* port, not the bound
    socket), so a real port number is needed up front -- same as
    test_mcp_daemon_contract.py's/test_daemon_process_lifecycle.py's own
    identical helper."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _prepare_sandbox(tmp_path: Path, *, port: int) -> Path:
    """A fresh sandbox directory (what ``paths.data_dir()`` resolves to in
    the spawned process -- see ``_BOOTSTRAP`` above), pre-seeded with a
    real ``settings.yaml`` -- a real free port, update checks disabled (this
    tier makes no real outbound network calls), and a short
    ``approvals.hold_window_seconds`` so the deferred-approval protocol's
    first call reliably goes ``approval_pending`` within this test's own
    patience, with no artificial sleep needed to force it (same reasoning
    as test_deferred_approval_round_trip.py's own ``hold_window=0.05``)."""
    sandbox = tmp_path / "sandbox"
    example = Path(privacyfence.__file__).parent / "resources" / "settings.yaml.example"
    config = yaml.safe_load(example.read_text(encoding="utf-8"))
    config["web"]["port"] = port
    config["web"]["approvals"] = {"hold_window_seconds": 0.3}
    config["update_check"]["enabled"] = False
    config_dir = sandbox / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "settings.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return sandbox


@contextlib.contextmanager
def _daemon(sandbox: Path):
    log_path = sandbox.parent / "daemon.log"
    with open(log_path, "wb") as log_fh:
        proc = subprocess.Popen(
            [sys.executable, "-c", _BOOTSTRAP, str(sandbox)],
            cwd=str(sandbox), stdout=log_fh, stderr=subprocess.STDOUT,
        )
        try:
            yield proc, log_path
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


def _wait_for_mcp_url(proc: subprocess.Popen, sandbox: Path, log_path: Path, timeout: float = 20.0) -> str:
    mcp_url_file = sandbox / "mcp_url"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"daemon exited early (code {proc.returncode}) instead of starting -- log:\n"
                f"{log_path.read_text(errors='replace')}"
            )
        if mcp_url_file.exists():
            content = mcp_url_file.read_text(encoding="utf-8").strip()
            if content:
                return content
        time.sleep(0.1)
    raise AssertionError(
        f"mcp_url discovery file never appeared under {timeout}s -- log:\n{log_path.read_text(errors='replace')}"
    )


def _can_connect(host: str, port: int) -> bool:
    with contextlib.suppress(OSError):
        with socket.create_connection((host, port), timeout=1.0):
            return True
    return False


def _wait_until_connectable(host: str, port: int, timeout: float = 10.0) -> None:
    """See test_daemon_process_lifecycle.py's identical helper for why a
    single ``_can_connect`` right after the ``mcp_url`` file appears can
    race a listener that isn't quite ready yet."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _can_connect(host, port):
            return
        time.sleep(0.05)
    raise AssertionError(f"nothing accepted a connection on {host}:{port} within {timeout}s of mcp_url appearing")


async def _bootstrap_session(web_client: httpx.AsyncClient, web_token: str, *, path: str) -> str:
    """Exactly the "still have filesystem access, no valid link handy" path
    ``unauthorized_html``'s own 401 page recommends -- ``POST
    /api/bootstrap`` (SEC-06) mints a fresh one-time code on demand,
    presented as a raw JSON field rather than a full URL specifically so it
    never needs to be logged (and, unlike the startup log lines, is never
    redacted by ``safe_errors.SecretRedactingFormatter`` -- that formatter's
    own key=value pattern matches the literal word ``bootstrap`` and would
    turn a URL logged with the code in it into ``bootstrap=[REDACTED]``,
    which is exactly why this test mints its own rather than trying to
    scrape one out of the daemon's log). Returns the ``pf_session`` cookie
    value now set on ``web_client``."""
    mint_resp = await web_client.post("/api/bootstrap", headers={"Authorization": f"Bearer {web_token}"})
    assert mint_resp.status_code == 200, mint_resp.text
    code = mint_resp.json()["bootstrap"]
    exchange_resp = await web_client.get(path, params={"bootstrap": code})
    assert exchange_resp.status_code == 200, exchange_resp.text
    session_id = web_client.cookies.get("pf_session")
    assert session_id, "bootstrap exchange did not set a pf_session cookie"
    return session_id


async def _call_tool(mcp_url: str, token: str, message: str):
    """One real MCP session: initialize -> tools/list -> call the
    synthetic gated tool -- same shape as test_mcp_daemon_contract.py's/
    test_deferred_approval_round_trip.py's own helpers. A fresh session per
    call, like those two, since the deferred protocol's own "re-issue the
    identical call" step is a fresh MCP request in production too (a new
    Claude tool-call turn), not a second call reusing one open session."""
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(headers=headers) as http_client:
        async with streamable_http_client(mcp_url, http_client=http_client) as (read, write, _get_session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                result = await session.call_tool(
                    "system_test_send", {"message": message, "reason": "system test round trip"},
                )
                return names, result


async def test_local_mode_daemon_mcp_approval_audit_contract(tmp_path):
    """The full contract, driven against a real daemon process: startup and
    discovery, the real web approval/settings surfaces reached through the
    real one-time bootstrap links, a gated tool call resolved Allow and a
    second resolved Deny (both through the real HTTP decide route), the
    audit log confirming both decisions, and a graceful shutdown."""
    port = _free_port()
    sandbox = _prepare_sandbox(tmp_path, port=port)

    with _daemon(sandbox) as (proc, log_path):
        # ── 1-3: startup, discovery, expected state files ──────────────
        mcp_url = _wait_for_mcp_url(proc, sandbox, log_path)
        parsed = urlparse(mcp_url)
        assert parsed.scheme == "http"
        assert parsed.path == "/mcp"
        assert parsed.port == port
        _wait_until_connectable(parsed.hostname, parsed.port)

        assert proc.poll() is None
        assert (sandbox / "privacyfence.lock").exists()
        assert (sandbox / "config" / "settings.yaml").exists()
        mcp_token = (sandbox / "mcp_token").read_text(encoding="utf-8").strip()
        assert mcp_token
        web_token = (sandbox / "web_token").read_text(encoding="utf-8").strip()
        assert web_token

        base_url = f"http://{parsed.hostname}:{parsed.port}"

        async with httpx.AsyncClient(base_url=base_url, follow_redirects=True) as web_client:
            # ── 4: GET /settings, GET /approvals -- unauthenticated first,
            # exactly what a browser with no session cookie gets back ────
            assert (await web_client.get("/approvals")).status_code == 401
            assert (await web_client.get("/settings")).status_code == 401

            # A real session, minted the same way an operator with disk
            # access (never a link out of the log -- see
            # ``_bootstrap_session``'s own docstring) gets one.
            session_id = await _bootstrap_session(web_client, web_token, path="/settings")
            assert (await web_client.get("/settings")).status_code == 200
            # Sessions aren't scoped to the path that minted them -- the
            # same cookie authenticates /approvals too, exactly as a
            # human's one browser session would.
            assert (await web_client.get("/approvals")).status_code == 200

            # ── 5-6: MCP tools/list + the first gated call ──────────────
            names, first = await _call_tool(mcp_url, mcp_token, "hello from the system test")
            assert "system_test_send" in names
            assert "privacyfence_check_policy" in names
            assert "privacyfence_begin_unattended_session" in names
            assert first.isError is not True
            assert first.structuredContent["status"] == "approval_pending"
            allow_id = first.structuredContent["approval_id"]
            assert allow_id

            # The pending card is visible on the real /approvals page,
            # human-readable via its tool_name -- not just present
            # internally.
            pending_page = await web_client.get("/approvals")
            assert pending_page.status_code == 200
            assert "Send test message" in pending_page.text

            # Resolve Allow through the real HTTP decide route -- exactly
            # what a human's browser does clicking the card's own Allow
            # button (session_auth.py's double-submit CSRF: the session
            # cookie value doubles as the token).
            decide_resp = await web_client.post(
                f"/api/approvals/{allow_id}/decide", json={"result": "accept", "csrf": session_id},
            )
            assert decide_resp.status_code == 200, decide_resp.text
            assert decide_resp.json() == {"status": "ok"}

            # Decided -- no longer listed as pending.
            after_allow_page = await web_client.get("/approvals")
            assert "Send test message" not in after_allow_page.text

            # A second, identical call finds the decision already in the
            # ledger and releases the real result -- no second prompt.
            _, second = await _call_tool(mcp_url, mcp_token, "hello from the system test")
            assert second.isError is not True
            assert second.structuredContent == {"message": "hello from the system test"}

            # ── 7: repeat, resolved Deny ─────────────────────────────────
            _, third = await _call_tool(mcp_url, mcp_token, "please deny me")
            assert third.structuredContent["status"] == "approval_pending"
            deny_id = third.structuredContent["approval_id"]

            deny_resp = await web_client.post(
                f"/api/approvals/{deny_id}/decide", json={"result": "deny", "csrf": session_id},
            )
            assert deny_resp.status_code == 200, deny_resp.text

            _, fourth = await _call_tool(mcp_url, mcp_token, "please deny me")
            assert fourth.isError is True

            # ── 8: audit log confirms both real decisions ────────────────
            audit_dir = sandbox / "logs" / "audit"
            decisions = []
            for jsonl_path in sorted(audit_dir.glob("*.jsonl")):
                for line in jsonl_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    if entry.get("connector") == "system_test":
                        decisions.append(entry.get("decision"))
            assert "approved" in decisions
            assert "rejected" in decisions

            # Cross-platform assertion this scenario adds on top of
            # tests/platform/'s own OS-level coverage (see module
            # docstring): the audit directory this exact scenario just
            # wrote to is actually restricted to the owner, on every POSIX
            # OS this test runs on. Windows' ``Path.chmod`` doesn't carry
            # the same meaning (see secure_files.secure_mkdir's own
            # docstring: "best effort on non-POSIX"), so this is skipped
            # there, same posture as every other POSIX-only permission
            # assertion in this repo.
            if sys.platform != "win32":
                assert audit_dir.stat().st_mode & 0o077 == 0

            # ── 9: graceful shutdown via the real "Quit PrivacyFence"
            # action -- proc.terminate()/SIGTERM (as
            # test_daemon_process_lifecycle.py's own termination test uses)
            # doesn't reliably run run_app()'s own ``finally`` block; this
            # does, since it's what actually calls request_shutdown() and
            # lets _wait_for_shutdown() return normally. ─────────────────
            quit_resp = await web_client.post(
                "/api/settings/quit_app", json={"csrf": session_id, "confirmed": True},
            )
            assert quit_resp.status_code == 200, quit_resp.text

        exit_code = proc.wait(timeout=15)
        assert exit_code == 0, f"daemon did not exit cleanly (code {exit_code}) -- log:\n{log_path.read_text(errors='replace')}"

    # Process cleanup on shutdown: the listening socket is gone with the
    # process, on every OS this runs on.
    assert not _can_connect("127.0.0.1", port)
    # Durable install state -- unlike the discovery-only mcp_url file (never
    # cleared by a clean quit; see WebServer.stop()'s own comment, not
    # called from run_app()'s shutdown path at all) -- still on disk after
    # the process is gone, exactly as a real install's state should be.
    assert (sandbox / "config" / "settings.yaml").exists()
    assert (sandbox / "logs" / "audit").exists()
