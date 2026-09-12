"""Packaged-artifact lifecycle test for the Windows installer
(docs/automated-test-strategy-plan.md Phase 6 item 6.2; docs/windows-support-
plan.md Phase 4/6).

The same role ``tests/integration/test_macos_packaged_smoke.py`` (TST-15)
plays for the DMG and ``tests/integration/test_deb_packaged_lifecycle.py``
(Phase 6.3) plays for the ``.deb``: install the actual built artifact -- not
a source checkout, not an editable dev install -- and exercise it as closely
as possible to how a real user would.

1. **Install**: a real silent run of ``scripts/build_installer.ps1``'s
   ``dist/PrivacyFence-<version>-setup.exe`` (Inno Setup 6) --
   ``/VERYSILENT /SUPPRESSMSGBOXES``, same as a user clicking through the
   wizard with every default accepted. ``/DIR=`` is overridden to a scratch
   directory under this test's own ``tmp_path`` rather than the real
   ``%ProgramFiles%``: ``installer/privacyfence.iss`` already declares
   ``PrivilegesRequired=lowest`` specifically so a per-user-writable install
   location needs no admin elevation at all -- this test relies on exactly
   that, so it runs the same way an unprivileged CI runner does.
2. **Validate the autostart entry**: ``installer/privacyfence.iss``'s
   ``[Run]`` section registers the Task Scheduler task
   (docs/windows-support-plan.md Phase 3) as part of the (silent) install
   itself, not a separate opt-in step -- ``schtasks /query`` against it is
   the one thing that would silently no-op at next logon if the install step
   ever stopped wiring it up.
3. **Start the real installed daemon** (``privacyfence-app.exe``, not
   ``PrivacyFenceApp.exe`` directly -- the same alias name both the Task
   Scheduler task and the mcpb shim's own ``DEFAULT_APP_PATH`` look for, see
   ``build_installer.ps1`` step 4) and run the Phase 3
   (``tests/system/test_local_mode_system.py``) daemon -> MCP -> approval ->
   audit contract's own shape against it: a real bootstrap session, an MCP
   tool call resolved through the real HTTP decide route both Allow and
   Deny, the audit log read back from disk, and a graceful shutdown via the
   real "Quit PrivacyFence" action.

   **One deliberate substitution** from Phase 3's own scenario, for the same
   reason ``test_macos_packaged_smoke.py``/``test_deb_packaged_lifecycle.py``
   already made it: Phase 3 injects a synthetic ``Connector`` by
   monkeypatching ``daemon_main.build_connectors`` *before* ``daemon_main``
   is ever imported -- only possible when the test controls the Python
   import itself, which a packaged, frozen daemon started as its own binary
   never does. This module instead drives
   ``privacyfence_propose_auto_accept_rule_change``, the one built-in
   meta-tool that always blocks on a confirmation dialog with no
   connector/credential of any kind behind it -- same tool, same reasoning.
   Like ``test_deb_packaged_lifecycle.py`` (and unlike the macOS module's
   real headless-Chromium click), this one resolves the pending card via a
   direct HTTP POST to ``/api/approvals/<id>/decide`` with the
   bootstrap-minted session cookie as CSRF -- no Node/Playwright dependency
   needed here, keeping this job's prerequisites to exactly what
   ``scripts/build_installer.ps1`` itself already needs.
4. **Uninstall**: a real silent run of the installer's own generated
   ``unins000.exe`` -- package-owned files (the whole install directory) and
   the Task Scheduler task (removed by the ``.iss``'s own
   ``[UninstallRun]``) are gone afterward; per-user state under the
   isolated ``%USERPROFILE%\\.privacyfence\\`` this test pointed the daemon
   at is untouched (``installer/privacyfence.iss``'s own ``[UninstallDelete]``
   comment: the installer never reaches into that directory at all).

Skipped entirely unless running on real Windows with a just-built
``dist/PrivacyFence-*-setup.exe`` on disk -- this only makes sense as a step
in ``.github/workflows/build.yml``'s ``build-windows`` job, right after
``scripts/build_installer.ps1``, never as part of the ordinary ``pytest``
invocation in ``tests.yml``'s per-PR jobs (same posture as the macOS/Linux
packaged tests).

Upgrade/state-preservation testing (installing version N+1 over N) is a
deliberate follow-up, not this module -- see this plan's Phase 6 item 20:
"don't build both in one PR."
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import platform
import re
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest
import yaml

mcp_client = pytest.importorskip(
    "mcp", reason="mcp (Python MCP client, test-only) not installed -- pip install -e '.[test]'"
)
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
DIST_DIR = REPO_ROOT / "dist"
SETTINGS_EXAMPLE = REPO_ROOT / "src" / "privacyfence" / "resources" / "settings.yaml.example"

TASK_NAME = "PrivacyFence"  # installer/privacyfence.iss's #define TaskName
MAIN_EXE_NAME = "PrivacyFenceApp.exe"
ALIAS_EXE_NAME = "privacyfence-app.exe"  # what the Task Scheduler task/mcpb shim both look for

WEB_TOKEN_FILE_NAME = "web_token"  # web/server.py's TOKEN_FILE_NAME
MCP_TOKEN_FILE_NAME = "mcp_token"  # web/mcp_auth.py's MCP_TOKEN_FILE_NAME


def _built_installers() -> list[Path]:
    return sorted(DIST_DIR.glob("PrivacyFence-*-setup.exe")) if DIST_DIR.is_dir() else []


pytestmark = [
    pytest.mark.packaged,
    pytest.mark.skipif(
        platform.system() != "Windows",
        reason="only meaningful against a real installer -- see docs/windows-support-plan.md Phase 4",
    ),
    pytest.mark.skipif(
        not _built_installers(),
        reason=(
            "no dist/PrivacyFence-*-setup.exe built yet -- this is the release-workflow smoke test "
            "build.yml's build-windows job runs after scripts/build_installer.ps1; run that script "
            "locally first to exercise this test outside CI"
        ),
    ),
    # A real silent install + a real daemon cold start + a real silent uninstall is comfortably
    # slower than the suite's default timeout=30 -- same reasoning as every other packaged/system
    # test in this repo.
    pytest.mark.timeout(180),
]


# --------------------------------------------------------------------------- #
# Task Scheduler helpers
# --------------------------------------------------------------------------- #

def _task_exists(name: str = TASK_NAME) -> bool:
    result = subprocess.run(["schtasks", "/query", "/tn", name], capture_output=True, text=True, timeout=15)
    return result.returncode == 0


def _delete_task_if_present(name: str = TASK_NAME) -> None:
    if _task_exists(name):
        subprocess.run(["schtasks", "/delete", "/tn", name, "/f"], capture_output=True, text=True, timeout=15)


@pytest.fixture(autouse=True)
def _clean_task_state():
    """Every test in this module installs/uninstalls the real Task Scheduler
    task -- machine-wide-per-user state, not something ``tmp_path`` isolates.
    Guarantee a clean slate on both sides so a failure partway through never
    leaves the runner with a stray task registered."""
    _delete_task_if_present()
    yield
    _delete_task_if_present()


# --------------------------------------------------------------------------- #
# Daemon process lifecycle -- same isolated-per-user-profile technique the
# macOS/Linux packaged tests use for $HOME, adapted to Windows' USERPROFILE
# (what Path.home()/os.path.expanduser resolve through there -- paths.py's
# data_dir()).
# --------------------------------------------------------------------------- #

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_connectable(host: str, port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last_exc: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError as exc:
            last_exc = exc
            time.sleep(0.1)
    raise TimeoutError(f"{host}:{port} never became connectable") from last_exc


def _wait_for_file(path: Path, proc: subprocess.Popen, log_path: Path, timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"daemon exited early (code {proc.poll()}) instead of starting -- log:\n"
                f"{log_path.read_text(errors='replace')}"
            )
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                return content
        time.sleep(0.1)
    raise AssertionError(f"{path} never appeared within {timeout}s -- log:\n{log_path.read_text(errors='replace')}")


class RunningDaemon:
    def __init__(self, process: subprocess.Popen, home: Path, port: int, web_token: str, mcp_token: str):
        self.process = process
        self.home = home
        self.port = port
        self.base_url = f"http://localhost:{port}"
        self.mcp_url = f"{self.base_url}/mcp"
        self.web_token = web_token
        self.mcp_token = mcp_token


def _prepare_home(home: Path, *, port: int) -> None:
    """Pre-seeds an isolated per-user-profile's ``settings.yaml``: a real
    free port (so repeated boots in this module never collide with each
    other or anything else on the runner) and update checks disabled (this
    tier makes no real outbound network calls)."""
    config_dir = home / ".privacyfence" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    settings_path = config_dir / "settings.yaml"
    settings = yaml.safe_load(SETTINGS_EXAMPLE.read_text(encoding="utf-8")) or {}
    settings.setdefault("web", {})["port"] = port
    settings.setdefault("update_check", {})["enabled"] = False
    settings_path.write_text(yaml.safe_dump(settings), encoding="utf-8")


def _running_daemon(exe: Path, home: Path):
    """Starts the real installed ``privacyfence-app.exe`` alias (not
    ``PrivacyFenceApp.exe`` directly -- proving the alias itself resolves
    and execs correctly is part of what this module is for) with an
    isolated ``%USERPROFILE%``, returning a context manager that always
    terminates it on the way out."""
    assert exe.is_file(), f"{exe} missing -- was the installer actually run?"
    port = _free_port()
    home.mkdir(parents=True, exist_ok=True)
    _prepare_home(home, port=port)
    # Path.home()/os.path.expanduser() resolve through USERPROFILE on
    # Windows, not HOME -- see paths.py's data_dir() and
    # docs/windows-support-plan.md Phase 6.3's own note on the analogous
    # ~-expansion fix. HOME is set alongside it defensively; it costs
    # nothing and some third-party code (this daemon's own dependencies
    # included) still checks it first.
    env = {**os.environ, "USERPROFILE": str(home), "HOME": str(home)}
    log_path = home / "daemon.log"

    @contextlib.contextmanager
    def _cm():
        with open(log_path, "wb") as log_fh:
            proc = subprocess.Popen([str(exe)], env=env, stdout=log_fh, stderr=subprocess.STDOUT)
            try:
                _wait_until_connectable("localhost", port)
                data_dir = home / ".privacyfence"
                web_token = _wait_for_file(data_dir / WEB_TOKEN_FILE_NAME, proc, log_path)
                mcp_token = _wait_for_file(data_dir / MCP_TOKEN_FILE_NAME, proc, log_path)
                yield RunningDaemon(proc, home, port, web_token, mcp_token)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)

    return _cm()


# --------------------------------------------------------------------------- #
# The daemon -> MCP -> approval -> audit round trip -- Phase 3's own shape,
# identical to test_deb_packaged_lifecycle.py's (see that module's docstring
# point 3 for why this substitution needs no connector/credential).
# --------------------------------------------------------------------------- #

async def _bootstrap_session(web_client: httpx.AsyncClient, web_token: str, *, path: str = "/settings") -> str:
    mint_resp = await web_client.post("/api/bootstrap", headers={"Authorization": f"Bearer {web_token}"})
    assert mint_resp.status_code == 200, mint_resp.text
    code = mint_resp.json()["bootstrap"]
    exchange_resp = await web_client.get(path, params={"bootstrap": code})
    assert exchange_resp.status_code == 200, exchange_resp.text
    session_id = web_client.cookies.get("pf_session")
    assert session_id, "bootstrap exchange did not set a pf_session cookie"
    return session_id


async def _propose_trusted_sender_rule(mcp_url: str, mcp_token: str, *, value: list[str]):
    headers = {"Authorization": f"Bearer {mcp_token}"}
    async with httpx.AsyncClient(headers=headers) as http_client:
        async with streamable_http_client(mcp_url, http_client=http_client) as (read, write, _sid):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(
                    "privacyfence_propose_auto_accept_rule_change",
                    {
                        "target": "rule",
                        "operation": "add",
                        "operation_key": "gmail.read_message",
                        "rule_name": "trusted_sender_domain",
                        "value": value,
                        "reason": "tests/integration/test_windows_packaged_smoke.py packaged installer lifecycle scenario",
                    },
                )


async def _resolve_pending_card(web_client: httpx.AsyncClient, session_id: str, *, decision: str) -> None:
    deadline = time.monotonic() + 20.0
    approval_id = None
    while time.monotonic() < deadline:
        page = await web_client.get("/approvals")
        assert page.status_code == 200, page.text
        match = re.search(r'<div class="pf-approval-row" data-approval-id="([0-9a-f]{16,})"', page.text)
        if match:
            approval_id = match.group(1)
            break
        await asyncio.sleep(0.1)
    assert approval_id, "no pending approval card appeared on /approvals"
    decide_resp = await web_client.post(
        f"/api/approvals/{approval_id}/decide", json={"result": decision, "csrf": session_id},
    )
    assert decide_resp.status_code == 200, decide_resp.text
    assert decide_resp.json() == {"status": "ok"}


async def _quit(web_client: httpx.AsyncClient, session_id: str) -> None:
    resp = await web_client.post("/api/settings/quit_app", json={"csrf": session_id, "confirmed": True})
    assert resp.status_code == 200, resp.text


async def _run_daemon_mcp_approval_audit_scenario(daemon: RunningDaemon) -> None:
    async with httpx.AsyncClient(base_url=daemon.base_url, follow_redirects=True) as web_client:
        assert (await web_client.get("/approvals")).status_code == 401
        assert (await web_client.get("/settings")).status_code == 401

        session_id = await _bootstrap_session(web_client, daemon.web_token)
        assert (await web_client.get("/settings")).status_code == 200

        # -- MCP discovery: the real MCP surface, no connector configured ----
        headers = {"Authorization": f"Bearer {daemon.mcp_token}"}
        async with httpx.AsyncClient(headers=headers) as http_client:
            async with streamable_http_client(daemon.mcp_url, http_client=http_client) as (read, write, _sid):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    names = {t.name for t in tools.tools}
        assert "privacyfence_propose_auto_accept_rule_change" in names
        assert "privacyfence_check_policy" in names

        # -- Allow round trip -------------------------------------------------
        allow_task = asyncio.create_task(
            _propose_trusted_sender_rule(daemon.mcp_url, daemon.mcp_token, value=["allowed.example.com"])
        )
        await _resolve_pending_card(web_client, session_id, decision="confirm")
        allow_result = await allow_task
        assert allow_result.isError is not True, getattr(allow_result, "content", allow_result)
        assert allow_result.structuredContent["confirmed"] is True
        assert allow_result.structuredContent["changed"] is True
        assert "trusted_sender_domain" in allow_result.structuredContent["description"]

        # -- Deny round trip ----------------------------------------------------
        deny_task = asyncio.create_task(
            _propose_trusted_sender_rule(daemon.mcp_url, daemon.mcp_token, value=["denied.example.com"])
        )
        await _resolve_pending_card(web_client, session_id, decision="cancel")
        deny_result = await deny_task
        assert deny_result.isError is True

        # -- Audit log confirms both real decisions ------------------------------
        audit_dir = daemon.home / ".privacyfence" / "logs" / "audit"
        decisions = []
        for jsonl_path in sorted(audit_dir.glob("*.jsonl")):
            for line in jsonl_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("connector") == "rule":
                    decisions.append(entry.get("decision"))
        assert "rule_changed_via_bridge_proposal" in decisions
        assert "rejected" in decisions

        # -- Graceful shutdown via the real "Quit PrivacyFence" action -----------
        await _quit(web_client, session_id)

    exit_code = daemon.process.wait(timeout=15)
    assert exit_code == 0, (
        f"daemon did not exit cleanly (code {exit_code}) -- log:\n"
        f"{(daemon.home / 'daemon.log').read_text(errors='replace')}"
    )


# --------------------------------------------------------------------------- #
# The one test -- install / validate / start+scenario / uninstall
# --------------------------------------------------------------------------- #

def _run_installer(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    result = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
    return result


async def test_windows_install_validate_scenario_uninstall_lifecycle(tmp_path):
    setup_exe = _built_installers()[-1]
    install_dir = tmp_path / "install"
    home = tmp_path / "home"
    log_path = tmp_path / "install.log"

    # ── Install ──────────────────────────────────────────────────────────
    # /DIR overrides installer/privacyfence.iss's DefaultDirName -- this is
    # what lets PrivilegesRequired=lowest install without ever needing an
    # admin elevation prompt: the target is a directory this test's own
    # user already owns, not the real %ProgramFiles%.
    result = _run_installer(
        str(setup_exe),
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/SP-", "/NORESTART",
        f"/DIR={install_dir}",
        f"/LOG={log_path}",
    )
    assert result.returncode == 0, (
        f"installer failed (exit {result.returncode}):\n{result.stdout}{result.stderr}\n"
        f"---- install log ----\n{log_path.read_text(errors='replace') if log_path.exists() else '(missing)'}"
    )

    main_exe = install_dir / MAIN_EXE_NAME
    alias_exe = install_dir / ALIAS_EXE_NAME
    assert main_exe.is_file(), f"{main_exe} missing after silent install"
    assert alias_exe.is_file(), f"{alias_exe} missing after silent install"
    assert list(install_dir.glob("*.mcpb")), f"no .mcpb found in {install_dir} after install"

    # ── Validate the autostart entry (installer/privacyfence.iss's [Run]) ──
    assert _task_exists(), f"Task Scheduler task {TASK_NAME!r} missing after install"

    # ── Start the real installed daemon; run the Phase 3 scenario ────────
    with _running_daemon(alias_exe, home) as daemon:
        await _run_daemon_mcp_approval_audit_scenario(daemon)

    settings_path = home / ".privacyfence" / "config" / "settings.yaml"
    assert "allowed.example.com" in settings_path.read_text(encoding="utf-8")

    # ── Uninstall (silent) ─────────────────────────────────────────────────
    uninstaller = install_dir / "unins000.exe"
    assert uninstaller.is_file(), f"{uninstaller} missing -- was the install actually silent/complete?"
    uninstall_result = _run_installer(str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
    assert uninstall_result.returncode == 0, (
        f"uninstall failed (exit {uninstall_result.returncode}):\n"
        f"{uninstall_result.stdout}{uninstall_result.stderr}"
    )

    # Inno's uninstaller spawns a short-lived helper process to delete its
    # own directory/log after the foreground process it just waited on
    # exits -- poll rather than assume the directory is already gone the
    # instant the process above returns.
    deadline = time.monotonic() + 15.0
    while install_dir.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not main_exe.exists(), f"{main_exe} should be gone after silent uninstall"
    assert not alias_exe.exists(), f"{alias_exe} should be gone after silent uninstall"

    # ── The scheduled task is removed too (installer/privacyfence.iss's
    # [UninstallRun]) ──────────────────────────────────────────────────────
    assert not _task_exists(), f"Task Scheduler task {TASK_NAME!r} should be gone after uninstall"

    # ── User state under the isolated %USERPROFILE% is untouched
    # (installer/privacyfence.iss's own [UninstallDelete] comment: the
    # installer never reaches into %USERPROFILE%\.privacyfence) ────────────
    assert settings_path.exists(), "uninstall must never touch %USERPROFILE%\\.privacyfence"
    assert "allowed.example.com" in settings_path.read_text(encoding="utf-8")
