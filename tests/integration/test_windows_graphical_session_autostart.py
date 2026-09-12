"""Real graphical-session autostart verification for the Windows installer
(docs/automated-test-strategy-plan.md Phase 7 item 2; docs/windows-support-
plan.md 8.2).

``test_windows_packaged_smoke.py`` (Phase 6.2) already proves the installer
registers the Task Scheduler autostart task (``schtasks /query`` against it)
and that the alias exe it points at, once started, serves a real
daemon/MCP/approval/audit round trip -- but it starts that alias exe itself,
directly, as a subprocess. It never actually signs anyone in. What it
deliberately leaves open -- see that module's own docstring, point 2 -- is
this phase's own question: does a real interactive Windows logon *itself*
start the daemon via that task's ``/sc onlogon`` trigger, with nothing else
telling it to?

There is no physical sign-in to drive on a GitHub-hosted Windows runner, so
this module makes two deliberate substitutions instead of skipping the
question (the Linux analogue, ``test_linux_graphical_session_autostart.py``,
makes exactly one, for the same underlying reason -- no real login manager
in CI):

1. **A throwaway local account stands in for "someone signs in", instead of
   the CI runner's own already-logged-on account.** This test has no way to
   learn that account's password (nor should it), so it can't make it log on
   a *second* time -- and ``installer/privacyfence.iss``'s own ``schtasks
   /create`` call (see ``[Run]``) never passes ``/RU``, which per Microsoft's
   documented default for ``/SC ONLOGON`` means the trigger fires for *any*
   interactive logon, not just the installing user's. That's the same
   "whichever account is at the keyboard" scope the macOS LaunchAgent (keyed
   off the current console uid) and the Linux ``.deb``'s XDG autostart
   (keyed off the current desktop session) already have -- so a brand-new
   throwaway account, whose password this test mints and knows, is a valid
   stand-in for "a user signs in", not a special case the real trigger
   wouldn't also fire for.
2. **The throwaway account is a local Administrator**, even though the
   daemon it ends up running still runs at ``/rl limited`` (the scheduled
   task's own execution-level setting, independent of the account's own
   group membership -- this is exactly what's being verified: the task
   still requests the non-elevated token). This is *not* needed for
   anything this test is trying to prove; it works around a Windows Server
   default (the base image these runners are built from) that denies
   "Log on locally" to plain standard accounts entirely, which would make
   the throwaway account -- not just this test -- unable to sign in at all,
   Task Scheduler trigger or no.

Everything else is the real, unmocked mechanism: a genuine interactive
Windows logon for that account (``LOGON32_LOGON_INTERACTIVE`` via
``CreateProcessWithLogonW``, driven here through PowerShell's ``Start-
Process -Credential`` -- the same underlying API ``runas.exe`` uses, and the
standard documented way admins manually test an "At log on" Task Scheduler
trigger without a physical sign-in), Task Scheduler's own real trigger
evaluation of the real installed task, the real packaged
``privacyfence-app.exe`` alias it launches (confirmed running as the
account that just logged on, via ``Win32_Process``'s ``GetOwner``, not
assumed), a real daemon/MCP/approval/audit round trip against it (Phase 3's
own contract shape, reusing ``test_windows_packaged_smoke.py``'s own
helpers), and "Quit PrivacyFence" confirmed to actually end that real
process.

Skipped entirely unless running on real Windows, elevated (creating and
deleting a local user account needs it), with a just-built ``dist/
PrivacyFence-*-setup.exe`` on disk -- same posture as
``test_windows_packaged_smoke.py``, and, like
``test_linux_graphical_session_autostart.py``, run from its own
``.github/workflows/windows-graphical-session.yml`` (packaging-related
``main`` pushes, weekly, and on demand) rather than ``build.yml``'s
tag-triggered release pipeline or ``tests.yml``'s per-PR jobs: this is the
same flakiest-and-most-expensive tier in ``docs/automated-test-strategy-
plan.md``'s taxonomy (Phase 7's own objective) the Linux module already
lives in, so a flaky run here must never block an actual release.
"""
from __future__ import annotations

import asyncio
import base64
import os
import platform
import secrets
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest

pytest.importorskip("mcp", reason="mcp (Python MCP client, test-only) not installed -- pip install -e '.[test]'")

from tests.diagnostics import (  # noqa: E402
    capture_directory_manifest,
    failure_dir,
    suite_name_for,
    write_environment_info,
)
from tests.integration.test_windows_packaged_smoke import (  # noqa: E402
    ALIAS_EXE_NAME,
    MCP_TOKEN_FILE_NAME,
    TASK_NAME,
    WEB_TOKEN_FILE_NAME,
    _bootstrap_session,
    _built_installers,
    _free_port,
    _prepare_home,
    _propose_trusted_sender_rule,
    _resolve_pending_card,
    _quit,
    _run_installer,
    _task_exists,
    _wait_until_connectable,
)


@pytest.fixture
def _graphical_diagnostics(request):
    """docs/automated-test-strategy-plan.md Phase 10: this module's own
    tests/diagnostics.py capture call. Unlike test_windows_packaged_
    smoke.py's ``home`` (always under that test's own ``tmp_path``), this
    module's daemon boots into a *real* throwaway user's Windows profile --
    not known until the test body itself calls ``_wait_for_profile_dir`` --
    so the test registers it into this fixture's own mutable ``state`` dict
    once it's known, the same "register, then capture from teardown" shape
    test_deb_packaged_lifecycle.py's own ``_capture_installed_file_
    manifest``/``_clean_package_state`` pair uses for real installed system
    state. There's no ``daemon.log`` here either -- a Scheduler-launched
    process has no redirected stdout of its own -- so this reaches for
    ``schtasks /query ... /v`` (the task's own last-run result code) instead
    of a log file, same reasoning as test_linux_graphical_session_
    autostart.py's own ``journalctl`` capture for its systemd-launched one."""
    state: dict[str, Path] = {}
    yield state
    rep_call = getattr(request.node, "rep_call", None)
    if rep_call is None or not rep_call.failed:
        return
    dest = failure_dir(request.node.nodeid, suite=suite_name_for(__file__))
    write_environment_info(dest / "environment.txt")
    home = state.get("home")
    if home is not None:
        # A different filename than the generic per-`tmp_path` capture's own
        # `manifest.txt` (docs/automated-test-strategy-plan.md Phase 10's
        # tests/conftest.py hook also fires for this test, since it directly
        # takes `tmp_path` too, for `install_dir`) -- this is a second,
        # separate manifest (the throwaway user's real profile, not
        # `tmp_path`), not a replacement for it.
        capture_directory_manifest(home / ".privacyfence", dest / "manifest-profile-home.txt")
    task_info = subprocess.run(
        ["schtasks", "/query", "/tn", TASK_NAME, "/v", "/fo", "list"], capture_output=True, text=True,
    )
    (dest / "logs").mkdir(parents=True, exist_ok=True)
    (dest / "logs" / "schtasks-query.txt").write_text(task_info.stdout + task_info.stderr, encoding="utf-8")


def _is_admin() -> bool:
    """Cross-platform-safe by construction (like ``test_deb_packaged_
    lifecycle.py``'s own ``_can_install_packages``): this is called from a
    ``pytestmark`` skipif, which is evaluated at collection time on every
    platform, not just Windows."""
    if platform.system() != "Windows":
        return False
    import ctypes

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:
        return False


pytestmark = [
    pytest.mark.packaged,
    pytest.mark.skipif(
        platform.system() != "Windows",
        reason="only meaningful against a real installer -- see docs/windows-support-plan.md 8.2",
    ),
    pytest.mark.skipif(
        not _built_installers(),
        reason=(
            "no dist/PrivacyFence-*-setup.exe built yet -- this is the release-workflow smoke test "
            "build.yml's build-windows job runs after scripts/build_installer.ps1; run that script "
            "locally first to exercise this test outside CI"
        ),
    ),
    pytest.mark.skipif(
        not _is_admin(),
        reason="creating/deleting a local user account needs an elevated shell",
    ),
    # A real silent install, a real user-account creation, a real interactive
    # logon, a real daemon cold start, and a full MCP/approval/audit round
    # trip -- comfortably slower than the suite's default timeout=30, same
    # reasoning as every other packaged/system test in this repo.
    pytest.mark.timeout(180),
]


# --------------------------------------------------------------------------- #
# PowerShell helpers -- Start-Process -Credential and Win32_Process/GetOwner
# have no schtasks.exe-style command-line-only equivalent, so this module
# drops into PowerShell for exactly those two things (everything else here
# uses the same plain cmd-tool-via-subprocess style as
# test_windows_packaged_smoke.py/test_deb_packaged_lifecycle.py).
# --------------------------------------------------------------------------- #

def _windows_powershell_env() -> dict[str, str]:
    """Returns an environment for spawning Windows PowerShell (``powershell.exe``,
    the 5.1 engine) that can actually autoload its own built-in modules.

    This pytest process itself runs under a PowerShell *7* (``pwsh``) step in
    CI (see this workflow's own ``shell:`` line), and pwsh sets ``$env:
    PSModulePath`` to its own module search path -- one that doesn't include
    Windows PowerShell 5.1's module directory. A nested ``powershell.exe``
    process (spawned below) inherits that env var as plain process
    environment and, unlike a real top-level 5.1 session, never recomputes
    it -- so autoloading its own built-in cmdlets (``ConvertTo-SecureString``
    from ``Microsoft.PowerShell.Security``, ``Get-CimInstance`` from
    ``CimCmdlets``, etc.) fails with "the module could not be loaded", 100%
    reproducibly, regardless of the throwaway account or password involved.
    Prepending Windows PowerShell 5.1's own system module directory restores
    the lookup those cmdlets need."""
    env = dict(os.environ)
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    system_modules = os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0", "Modules")
    existing = env.get("PSModulePath", "")
    if system_modules.lower() not in existing.lower():
        env["PSModulePath"] = f"{system_modules};{existing}" if existing else system_modules
    return env


def _run_powershell(script: str, *, timeout: float = 60.0) -> subprocess.CompletedProcess:
    # -EncodedCommand (UTF-16LE, base64) sidesteps every bit of cmd/argv
    # quoting hazard a multi-line script with embedded single/double quotes
    # would otherwise hit going through subprocess's argv on Windows.
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=timeout, env=_windows_powershell_env(),
    )


def _run_as_user(username: str, password: str, exe: str, args: str, *, timeout: float = 30.0) -> None:
    """Performs a real interactive Windows logon for *username*
    (``LOGON32_LOGON_INTERACTIVE`` via ``CreateProcessWithLogonW``, which is
    what ``Start-Process -Credential`` uses -- the same primitive
    ``runas.exe`` is built on) and runs *exe* under it, waiting for it to
    exit. This -- not a physical sign-in -- is this module's one real
    substitution; see its own docstring."""
    password_escaped = password.replace("'", "''")
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        f"$secpw = ConvertTo-SecureString -String '{password_escaped}' -AsPlainText -Force\n"
        f"$cred = New-Object System.Management.Automation.PSCredential('{username}', $secpw)\n"
        f"$p = Start-Process -FilePath '{exe}' -ArgumentList '{args}' -Credential $cred "
        "-WorkingDirectory 'C:\\Windows' -WindowStyle Hidden -PassThru -Wait\n"
        "exit $p.ExitCode\n"
    )
    result = _run_powershell(script, timeout=timeout)
    assert result.returncode == 0, (
        f"logon-equivalent process for {username!r} failed (exit {result.returncode}):\n"
        f"{result.stdout}{result.stderr}"
    )


def _find_process_by_exe_path(exe_path: str) -> tuple[str, str] | None:
    """Returns ``(pid, owner)`` for the running process whose
    ``Win32_Process.ExecutablePath`` matches *exe_path* exactly, or ``None``
    -- confirming both that the task's action actually launched (not just
    that *some* process with a similar name exists somewhere) and, via
    ``GetOwner``, which account it's actually running as."""
    quoted = exe_path.replace("'", "''")
    script = (
        f"$p = Get-CimInstance Win32_Process | Where-Object {{ $_.ExecutablePath -eq '{quoted}' }} "
        "| Select-Object -First 1\n"
        "if ($null -eq $p) { exit 1 }\n"
        "$owner = Invoke-CimMethod -InputObject $p -MethodName GetOwner\n"
        "Write-Output ($p.ProcessId.ToString() + '|' + $owner.User)\n"
    )
    result = _run_powershell(script, timeout=15)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    pid, _, owner = result.stdout.strip().partition("|")
    return pid, owner


# --------------------------------------------------------------------------- #
# Throwaway local account -- see module docstring for why this stands in for
# "someone signs in" and why it's a local Administrator.
# --------------------------------------------------------------------------- #

def _random_username() -> str:
    return "pfglogon" + secrets.token_hex(3)


def _random_password() -> str:
    # Fixed upper/lower/digit/symbol characters guarantee this satisfies the
    # default Windows local-account complexity policy regardless of what
    # secrets.token_hex happens to produce.
    return f"Pf!{secrets.token_hex(6)}Aa1"


def _create_local_user(username: str, password: str) -> None:
    result = subprocess.run(
        ["net", "user", username, password, "/add", "/y"], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, f"net user /add failed:\n{result.stdout}{result.stderr}"
    # See module docstring point 2 -- purely to satisfy "Log on locally"
    # rights on the Windows Server base image these runners use, not to
    # change anything about how the daemon itself ends up running (the
    # scheduled task's own /rl limited governs that).
    admin_result = subprocess.run(
        ["net", "localgroup", "Administrators", username, "/add"], capture_output=True, text=True, timeout=20,
    )
    assert admin_result.returncode == 0, f"net localgroup Administrators /add failed:\n{admin_result.stdout}{admin_result.stderr}"


def _delete_local_user(username: str) -> None:
    subprocess.run(["net", "user", username, "/delete"], capture_output=True, text=True, timeout=20)


def _logoff_sessions(username: str) -> None:
    """Best-effort cleanup only -- never raises. Ends any session left open
    under *username* so its profile isn't still mounted when this test tries
    to delete the account/profile directory afterward."""
    try:
        result = subprocess.run(["query", "user"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if parts and parts[0].lstrip(">").lower() == username.lower() and len(parts) > 2 and parts[2].isdigit():
            subprocess.run(["logoff", parts[2]], capture_output=True, text=True, timeout=15)


def _wait_for_profile_dir(username: str, *, timeout: float = 30.0) -> Path:
    users_root = Path(os.environ.get("SystemDrive", "C:") + "\\") / "Users"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        candidates = [p for p in users_root.glob(f"{username}*") if p.is_dir()]
        if candidates:
            return candidates[0]
        time.sleep(0.5)
    raise AssertionError(f"no profile directory for {username!r} appeared under {users_root} within {timeout}s")


@pytest.fixture
def _throwaway_user():
    username = _random_username()
    password = _random_password()
    _create_local_user(username, password)
    try:
        yield username, password
    finally:
        _logoff_sessions(username)
        _delete_local_user(username)
        users_root = Path(os.environ.get("SystemDrive", "C:") + "\\") / "Users"
        for candidate in users_root.glob(f"{username}*"):
            for _attempt in range(10):
                shutil.rmtree(candidate, ignore_errors=True)
                if not candidate.exists():
                    break
                time.sleep(0.5)


def _wait_for_path_content(path: Path, *, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                return content
        time.sleep(0.2)
    raise AssertionError(f"{path} never appeared/populated within {timeout}s")


# --------------------------------------------------------------------------- #
# The test -- Phase 7 item 2 / 8.2: install, sign in (real logon-equivalent),
# verify autostart, exercise the Phase 3 system contract.
# --------------------------------------------------------------------------- #

async def test_installer_autostart_activates_daemon_via_real_logon_session(
    _throwaway_user, tmp_path, _graphical_diagnostics,
):
    username, password = _throwaway_user
    setup_exe = _built_installers()[-1]
    install_dir = tmp_path / "install"

    # ── Materialize a real Windows user profile for the throwaway account
    # *before* installing anything, via one real interactive logon (the same
    # technique the actual triggering logon below uses) -- so the settings.
    # yaml pre-seed just below has somewhere to write, and so the triggering
    # logon later is never this account's very first one at all. ───────────
    _run_as_user(username, password, "cmd.exe", "/c exit")
    home = _wait_for_profile_dir(username, timeout=30)
    _graphical_diagnostics["home"] = home

    port = _free_port()
    _prepare_home(home, port=port)

    # ── Install (as this test's own -- not the throwaway -- account; see
    # module docstring point 1 for why the installer's own schtasks /create
    # having no /RU makes this the exact real-world trigger scope) ─────────
    install_result = _run_installer(
        str(setup_exe), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/SP-", "/NORESTART",
        f"/DIR={install_dir}", f"/LOG={tmp_path / 'install.log'}",
    )
    assert install_result.returncode == 0, (
        f"installer failed (exit {install_result.returncode}):\n{install_result.stdout}{install_result.stderr}"
    )
    assert _task_exists(), f"Task Scheduler task {TASK_NAME!r} missing after install"

    # A silent install's own [Run] "launch now" step is skipifsilent -- it
    # must never fire under /VERYSILENT (test_windows_packaged_smoke.py's
    # own lifecycle test relies on the same fact); only the next real
    # logon's ONLOGON trigger should ever start the daemon.
    assert not (home / ".privacyfence" / WEB_TOKEN_FILE_NAME).exists(), (
        "a silent install must never itself start the daemon -- only a real logon should"
    )

    # ── The actual "sign in" moment -- a real interactive logon for the
    # throwaway account. See module docstring for why this is this test's
    # one deliberate substitution for a physical sign-in. ──────────────────
    _run_as_user(username, password, "cmd.exe", "/c exit")

    alias_exe_path = str(install_dir / ALIAS_EXE_NAME)
    deadline = time.monotonic() + 30.0
    found = None
    while time.monotonic() < deadline:
        found = _find_process_by_exe_path(alias_exe_path)
        if found:
            break
        time.sleep(0.5)
    assert found, (
        f"{alias_exe_path} never appeared as a running process within 30s of {username}'s logon -- "
        f"the ONLOGON task trigger never fired"
    )
    pid, owner = found
    assert username.lower() in owner.lower(), (
        f"{ALIAS_EXE_NAME} (pid {pid}) is running as {owner!r}, not the account that just logged on ({username!r})"
    )

    web_token = _wait_for_path_content(home / ".privacyfence" / WEB_TOKEN_FILE_NAME, timeout=20)
    mcp_token = _wait_for_path_content(home / ".privacyfence" / MCP_TOKEN_FILE_NAME, timeout=20)
    _wait_until_connectable("localhost", port)

    base_url = f"http://localhost:{port}"
    mcp_url = f"{base_url}/mcp"

    # ── Phase 3's own daemon/MCP/approval/audit contract shape, against a
    # daemon this test never itself started a process for ─────────────────
    async with httpx.AsyncClient(base_url=base_url, follow_redirects=True) as web_client:
        session_id = await _bootstrap_session(web_client, web_token)
        assert (await web_client.get("/settings")).status_code == 200

        allow_task = asyncio.create_task(
            _propose_trusted_sender_rule(mcp_url, mcp_token, value=["autologon.example.com"])
        )
        await _resolve_pending_card(web_client, session_id, decision="confirm")
        allow_result = await allow_task
        assert allow_result.isError is not True, getattr(allow_result, "content", allow_result)
        assert allow_result.structuredContent["changed"] is True

        await _quit(web_client, session_id)

    # ── Graceful shutdown propagates to the real process the trigger
    # started -- not just makes it unreachable over HTTP ───────────────────
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if _find_process_by_exe_path(alias_exe_path) is None:
            break
        time.sleep(0.5)
    else:
        pytest.fail(f"{ALIAS_EXE_NAME} (pid {pid}) still running after Quit PrivacyFence")

    settings_path = home / ".privacyfence" / "config" / "settings.yaml"
    assert "autologon.example.com" in settings_path.read_text(encoding="utf-8")

    # ── Cleanup: silent uninstall, same flow as test_windows_packaged_
    # smoke.py's own lifecycle test ─────────────────────────────────────────
    uninstaller = install_dir / "unins000.exe"
    if uninstaller.is_file():
        _run_installer(str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
