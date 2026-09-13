"""Real Task Scheduler autostart verification for the Windows installer
(docs/automated-test-strategy-plan.md Phase 7 item 2 and Phase 13 item 4;
the now-removed windows-support-plan.md 8.2).

``test_windows_packaged_smoke.py`` (Phase 6.2) already proves the installer
registers *a* Task Scheduler autostart task (``schtasks /query`` against it)
and that the alias exe it points at, once started, serves a real
daemon/MCP/approval/audit round trip -- but it starts that alias exe itself,
directly, as a subprocess, under the installing account, out of that test's
own ``tmp_path``. Nothing there ever asks Task Scheduler to run anything.

This module asks exactly that, for the task the installer really
registered, and it asks it for an account that installed nothing:

1. **The registered definition matches the autostart contract.** Not the
   template file in this repo -- the definition read back out of Task
   Scheduler itself (``schtasks /query /xml``), i.e. what the service
   actually parsed, normalized and stored: a ``LogonTrigger`` with no
   ``UserId`` (so it is scoped to any interactive logon, not the
   installing account), a ``Builtin\\Users`` ``GroupId`` principal bound to
   the ``Actions`` element by a matching ``id``/``Context`` pair,
   ``LeastPrivilege``, ``Parallel`` multiple-instances, the real installed
   ``privacyfence-app.exe`` path as the action's ``Command``, and the
   ``RestartOnFailure`` interval/count that carries crash-restart. Each of
   those has been a real, shipped bug at least once -- see
   ``installer/privacyfence-task.xml.tmpl``'s own header comment -- and
   every one of them was invisible to a test that only asked "does a task
   with this name exist".
2. **Task Scheduler itself starts the daemon for a throwaway account, in
   that account's own profile**, and the resulting process really is
   running as that account (``Win32_Process``'s ``GetOwner``, not assumed),
   really does serve the Phase 3 daemon/MCP/approval/audit contract, and
   really does end on "Quit PrivacyFence".
3. **Crash-restart works** (Phase 13 item 4): the Scheduler-started daemon
   is killed outright, and Task Scheduler is observed relaunching it --
   a *different* pid, for the same installed exe, within the task's own
   ``<RestartOnFailure><Interval>PT1M</Interval>`` window.

The one deliberate substitution: **Task Scheduler is asked to run the task
on demand from inside a real logon of the throwaway account, rather than by
that account signing in.** ``AllowStartOnDemand`` and the trigger share
every step that follows the decision to run -- resolving the ``GroupId``
principal to a concrete logged-on member, minting that member's
``LeastPrivilege`` token, building its environment and profile, and
launching the action -- so everything above is the real mechanism. What the
substitution does *not* cover is the trigger's own firing, i.e. Task
Scheduler deciding *when*.

That gap is deliberate, and it replaces a previous substitution that simply
did not work. This module used to call PowerShell's ``Start-Process
-Credential`` "signing in" and assert the daemon turned up afterwards. It
never did, on any run: ``CreateProcessWithLogonW`` (which is what that
cmdlet, and ``runas.exe``, are built on) creates a *logon session* but not
the Terminal Services *session* logon that Task Scheduler's ``LogonTrigger``
subscribes to, so the trigger was never evaluated at all. Task Scheduler
said so itself once the failure message started asking it -- run 24 of
``windows-graphical-session.yml``, against ``main``, with the task correctly
registered::

    Status:                Ready
    Scheduled Task State:  Enabled
    Run As User:           Users
    Last Run Time:         11/30/1999 12:00:00 AM
    Last Result:           267011        (SCHED_S_TASK_HAS_NOT_RUN)

Registered, enabled, ready, never attempted. No installer-side or task-XML
change can turn that green, because nothing was wrong on the installer
side; the test was asserting something a hosted runner cannot produce. So
the trigger's own firing is now covered where it can actually be covered:
``release-testing.md``'s Windows human checks, on a real machine with a
real sign-in, tracked on
`privacyfence/privacyfence#121 <https://github.com/privacyfence/privacyfence/issues/121>`_.
(An RDP loopback into the runner would create a genuine session logon, and
was considered -- it needs an RDP client that can run without a desktop of
its own, which a hosted runner does not have, so it would trade a gap that
is honestly described for one that is merely harder to see.)

Two smaller deliberate choices, both forced by the same "another account
must be able to run it" requirement:

* **The throwaway account is a local Administrator.** Not needed for
  anything here -- the daemon still runs at the task's own
  ``LeastPrivilege`` run level, which is part of what's verified -- but the
  Windows Server image these runners come from denies "Log on locally" to
  plain standard accounts outright, which would stop the account being
  usable at all.
* **The install goes to a machine-wide directory, not the installer's own
  default.** ``installer/privacyfence.iss`` is ``PrivilegesRequired=lowest``,
  so a silent install resolves ``{autopf}`` to ``{userpf}`` --
  ``%LOCALAPPDATA%\\Programs\\PrivacyFence``, inside the *installing*
  account's profile, which no other account can read. The shipped task's
  ``Builtin\\Users`` principal only composes with a per-machine layout, so
  that is what this test installs (see ``INSTALL_DIR``).

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
lives in, so a flaky run here must never block an actual release. The
module and workflow keep their "graphical session" names, which now read as
the tier they belong to rather than a literal description of what this
particular module drives -- renaming them would break the workflow's own
run history and ``paths:`` triggers for no gain.
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
from tests.windows_task_contract import assert_task_xml_matches_autostart_contract  # noqa: E402

# Machine-wide, and deliberately not the installer's own default -- see the
# module docstring's third bullet. No space in the path: Inno parses
# `/DIR=` off its own raw command line rather than argv, so a value that
# needs quoting is one more thing to get wrong in a test whose subject is
# something else entirely.
INSTALL_DIR = Path(os.environ.get("SystemDrive", "C:") + "\\") / "PrivacyFenceAutostartTest"


@pytest.fixture
def _graphical_diagnostics(request):
    """docs/automated-test-strategy-plan.md Phase 10: this module's own
    tests/diagnostics.py capture call. Unlike test_windows_packaged_
    smoke.py's ``home`` (always under that test's own ``tmp_path``), this
    module's daemon boots into a *real* throwaway user's Windows profile --
    not known until the fixture that creates it runs -- so that fixture
    registers it into this one's own mutable ``state`` dict once it's
    known, the same "register, then capture from teardown" shape
    test_deb_packaged_lifecycle.py's own ``_capture_installed_file_
    manifest``/``_clean_package_state`` pair uses for real installed system
    state. There's no ``daemon.log`` here either -- a Scheduler-launched
    process has no redirected stdout of its own -- so this reaches for
    ``schtasks /query`` (the task's own last-run result code, and the
    definition the service actually stored) instead of a log file, same
    reasoning as test_linux_graphical_session_autostart.py's own
    ``journalctl`` capture for its systemd-launched one."""
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
        # takes `tmp_path` too, for the install log) -- this is a second,
        # separate manifest (the throwaway user's real profile, not
        # `tmp_path`), not a replacement for it.
        capture_directory_manifest(home / ".privacyfence", dest / "manifest-profile-home.txt")
    task_info = subprocess.run(
        ["schtasks", "/query", "/tn", TASK_NAME, "/v", "/fo", "list"], capture_output=True, text=True,
    )
    (dest / "logs").mkdir(parents=True, exist_ok=True)
    (dest / "logs" / "schtasks-query.txt").write_text(task_info.stdout + task_info.stderr, encoding="utf-8")
    # The stored definition, not this repo's template: which of the two
    # disagrees with the other is the whole question whenever a task
    # registers but then behaves unexpectedly.
    (dest / "logs" / "schtasks-query-xml.txt").write_text(_registered_task_xml_or_error(), encoding="utf-8")


def _task_state_summary() -> str:
    """The registered task's own view of what happened, as ``schtasks
    /query /v`` reports it.

    A task existing and a task having actually run are two different
    things, and the assertions below can only observe the second one
    indirectly (no daemon process turned up). Task Scheduler knows which of
    them failed: "Last Run Time" and "Last Result" say whether it ever tried
    to run the action at all, and "Scheduled Task State" / "Status" say
    whether the task is even enabled and ready. Putting those lines straight
    into the failure message is the same move that turned the registration
    failure underneath this one from eight opaque runs into a single
    readable error -- the schtasks output was always there, it just was not
    anywhere a failing run could show it.
    """
    result = subprocess.run(
        ["schtasks", "/query", "/tn", TASK_NAME, "/v", "/fo", "list"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        return f"(schtasks /query failed, exit {result.returncode}): {result.stdout}{result.stderr}"
    wanted = (
        "Status:", "Last Run Time:", "Last Result:", "Next Run Time:",
        "Scheduled Task State:", "Run As User:", "Task To Run:",
    )
    lines = [
        line.strip() for line in result.stdout.splitlines()
        if line.strip().startswith(wanted)
    ]
    return "\n".join(lines) if lines else result.stdout


def _decode_console_output(raw: bytes) -> str:
    """``schtasks /query /xml`` writes UTF-16 (with a BOM) when its output
    is redirected, unlike the ANSI text every other ``schtasks`` output mode
    produces -- so this reads bytes and decides, rather than letting
    ``subprocess``'s own ``text=True`` guess the console code page and
    mangle it."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def _registered_task_xml() -> str:
    """The task definition as *Task Scheduler itself* stores it.

    Deliberately not installer/privacyfence-task.xml.tmpl and not the
    substituted copy Setup handed to ``schtasks``: the service parses,
    validates, normalizes and stores its own version of that document, and
    the gap between the two is exactly where this task's real bugs have
    lived (an ``id``/``Context`` pair that registered fine but bound the
    principal to nothing that runs; a schema version whose absence silently
    dropped the 1.2-only Settings elements)."""
    result = subprocess.run(
        ["schtasks", "/query", "/tn", TASK_NAME, "/xml", "ONE"], capture_output=True, timeout=20,
    )
    text = _decode_console_output(result.stdout) + _decode_console_output(result.stderr)
    assert result.returncode == 0, f"schtasks /query /xml failed (exit {result.returncode}):\n{text}"
    start = text.find("<?xml")
    if start < 0:
        start = text.find("<Task")
    assert start >= 0, f"schtasks /query /xml returned no task document:\n{text}"
    return text[start:]


def _registered_task_xml_or_error() -> str:
    """Diagnostics-path variant: never raises, so a teardown capture can't
    turn a real failure into an error inside the fixture."""
    try:
        return _registered_task_xml()
    except Exception as exc:  # noqa: BLE001 -- diagnostics only, any failure is itself the datum
        return f"(could not read the registered task XML: {exc!r})"


def _install_log_tail(log_path: Path, *, max_chars: int = 8000) -> str:
    """The last *max_chars* of Inno's own ``/LOG=`` output, or a plain
    ``(missing)``/``(empty)`` marker. Never the whole file: this onedir
    bundle's own [Files] copy log alone runs to tens of thousands of
    lines, and CurStepChanged(ssPostInstall)'s own RegisterAutostartTask
    call -- the part actually worth seeing on a registration failure --
    is logged only after every one of those file-copy lines, so returning
    the whole file risks it never actually reaching whatever captured
    this assertion's own output (a CI log viewer's own size limit,
    included)."""
    if not log_path.exists():
        return "(missing)"
    text = log_path.read_text(errors="replace")
    if not text:
        return "(empty)"
    return text[-max_chars:]


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
        reason="only meaningful against a real installer -- see the now-removed windows-support-plan.md 8.2",
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
    # A real silent install, a real user-account creation, a real logon, a
    # real Scheduler-driven daemon cold start, and a full MCP/approval/audit
    # round trip -- comfortably slower than the suite's default timeout=30,
    # same reasoning as every other packaged/system test in this repo.
    pytest.mark.timeout(300),
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


def _run_as_user(
    username: str,
    password: str,
    exe: str,
    args: str,
    *,
    timeout: float = 30.0,
    output_dir: Path | None = None,
) -> str:
    """Runs *exe* as *username* and waits for it to exit, via a real
    ``CreateProcessWithLogonW`` logon (what ``Start-Process -Credential``,
    and ``runas.exe``, are built on). Returns whatever the child wrote on
    stdout/stderr when *output_dir* is given (Start-Process can only
    redirect to files, not pipes), or ``""``.

    This is **not** a stand-in for someone signing in, and this module no
    longer uses it as one: the call creates a logon session but no Terminal
    Services session, so it raises none of the session-logon notifications
    Task Scheduler's ``LogonTrigger`` subscribes to -- see the module
    docstring for the run that established that. It is used here for what it
    genuinely is: a way to materialize the account's Windows profile, and a
    way to run a command (``schtasks /run``) as that account."""
    password_escaped = password.replace("'", "''")
    # -WindowStyle and the -Redirect* parameters are never passed together:
    # the exact `-Credential ... -WindowStyle Hidden` shape below is the one
    # real runs of this workflow have already proven works on a hosted
    # runner's PowerShell 5.1, and -WindowStyle is documented under
    # Start-Process's ShellExecute parameter set while the redirects are not,
    # so a run that needs the child's output simply drops it.
    stdout_path = stderr_path = None
    if output_dir is None:
        options = "-WindowStyle Hidden"
    else:
        stdout_path = output_dir / f"{Path(exe).stem}-{secrets.token_hex(4)}.out"
        stderr_path = output_dir / f"{stdout_path.stem}.err"
        options = f"-RedirectStandardOutput '{stdout_path}' -RedirectStandardError '{stderr_path}'"
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        f"$secpw = ConvertTo-SecureString -String '{password_escaped}' -AsPlainText -Force\n"
        f"$cred = New-Object System.Management.Automation.PSCredential('{username}', $secpw)\n"
        f"$p = Start-Process -FilePath '{exe}' -ArgumentList '{args}' -Credential $cred "
        f"-WorkingDirectory 'C:\\Windows' {options} -PassThru -Wait\n"
        "exit $p.ExitCode\n"
    )
    result = _run_powershell(script, timeout=timeout)
    child_output = ""
    for path in (stdout_path, stderr_path):
        if path is not None and path.exists():
            child_output += path.read_text(errors="replace")
    assert result.returncode == 0, (
        f"running {exe} {args} as {username!r} failed (exit {result.returncode}):\n"
        f"{result.stdout}{result.stderr}{child_output}"
    )
    return child_output


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


def _wait_for_alias_process(
    exe_path: str, *, timeout: float, different_from: str | None = None,
) -> tuple[str, str] | None:
    """Polls for the installed alias exe, optionally requiring a *different*
    pid than one already seen (which is what makes "Task Scheduler
    relaunched it" distinguishable from "the old process is still here")."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = _find_process_by_exe_path(exe_path)
        if found and (different_from is None or found[0] != different_from):
            return found
        time.sleep(0.5)
    return None


def _wait_until_alias_process_gone(exe_path: str, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _find_process_by_exe_path(exe_path) is None:
            return True
        time.sleep(0.5)
    return False


# --------------------------------------------------------------------------- #
# Throwaway local account -- see module docstring for why this account exists
# and why it's a local Administrator.
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
    # See module docstring -- purely to satisfy "Log on locally" rights on
    # the Windows Server base image these runners use, not to change
    # anything about how the daemon itself ends up running (the scheduled
    # task's own LeastPrivilege run level governs that, and this module
    # asserts it does).
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
# Install / uninstall, shared by both tests below
# --------------------------------------------------------------------------- #

class _Installed:
    """What a test needs to know about the installation the fixture made:
    the throwaway account it's going to be started for, that account's real
    profile, the port its settings.yaml was seeded with, and where the
    installed alias exe actually is."""

    def __init__(self, *, username: str, password: str, home: Path, port: int, log_path: Path) -> None:
        self.username = username
        self.password = password
        self.home = home
        self.port = port
        self.log_path = log_path
        self.alias_exe = str(INSTALL_DIR / ALIAS_EXE_NAME)


def _kill_alias_processes() -> None:
    """Best-effort: end any daemon this module's install left running, so a
    failed test never leaks a process holding the install directory open."""
    subprocess.run(
        ["taskkill", "/f", "/im", ALIAS_EXE_NAME], capture_output=True, text=True, timeout=30,
    )


def _remove_task() -> None:
    """Stop and delete the task *before* uninstalling, not after.

    The uninstaller removes it too ([UninstallRun]), but the crash-restart
    test deliberately leaves a task with pending restart attempts behind:
    left registered, Task Scheduler can relaunch the daemon out of the
    directory the uninstaller is in the middle of deleting."""
    subprocess.run(["schtasks", "/end", "/tn", TASK_NAME], capture_output=True, text=True, timeout=20)
    subprocess.run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"], capture_output=True, text=True, timeout=20)


@pytest.fixture
def _installed(_throwaway_user, tmp_path, _graphical_diagnostics):
    """One real silent install per test, and its full teardown -- both tests
    below need the same installed-and-registered starting state, and neither
    may leave a task or a daemon behind for the other (or for whatever runs
    next on this machine)."""
    username, password = _throwaway_user

    # Materialize a real Windows user profile for the throwaway account
    # *before* installing anything, via one real logon -- so the
    # settings.yaml pre-seed just below has somewhere to write.
    _run_as_user(username, password, "cmd.exe", "/c exit")
    home = _wait_for_profile_dir(username, timeout=30)
    _graphical_diagnostics["home"] = home

    port = _free_port()
    _prepare_home(home, port=port)

    if INSTALL_DIR.exists():  # a previous run that died before its own cleanup
        _kill_alias_processes()
        shutil.rmtree(INSTALL_DIR, ignore_errors=True)

    # Installed as this test's own -- not the throwaway -- account, which is
    # the point: the task the installer registers names a group principal,
    # so it has to work for an account that installed nothing.
    log_path = tmp_path / "install.log"
    setup_exe = _built_installers()[-1]
    install_result = _run_installer(
        str(setup_exe), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/SP-", "/NORESTART",
        f"/DIR={INSTALL_DIR}", f"/LOG={log_path}",
    )
    assert install_result.returncode == 0, (
        f"installer failed (exit {install_result.returncode}):\n{install_result.stdout}{install_result.stderr}\n"
        f"---- install log (tail) ----\n{_install_log_tail(log_path)}"
    )
    # RegisterAutostartTask (installer/privacyfence.iss's [Code] section)
    # doesn't abort Setup on its own failure, so a silent install can still
    # exit 0 with no task actually registered -- the install log (Inno's
    # own /LOG= output, which records every [Code] Exec call and its
    # result, schtasks' own stdout/stderr included) is the only way to see
    # why, short of downloading this test's own diagnostics artifact by
    # hand. Only the *tail*: this onedir bundle's own per-file [Files] copy
    # log alone runs to tens of thousands of lines, which previously pushed
    # the actually useful part (CurStepChanged(ssPostInstall)'s own
    # RegisterAutostartTask call, logged only after every file is already
    # copied) past what a CI log viewer -- or this test's own captured
    # stdout -- keeps readily available.
    assert _task_exists(), (
        f"Task Scheduler task {TASK_NAME!r} missing after install\n"
        f"---- install log (tail) ----\n{_install_log_tail(log_path)}"
    )

    # A silent install's own [Run] "launch now" step is skipifsilent -- it
    # must never fire under /VERYSILENT (test_windows_packaged_smoke.py's
    # own lifecycle test relies on the same fact); only Task Scheduler
    # should ever start the daemon in this module.
    assert not (home / ".privacyfence" / WEB_TOKEN_FILE_NAME).exists(), (
        "a silent install must never itself start the daemon -- only the autostart task should"
    )

    try:
        yield _Installed(username=username, password=password, home=home, port=port, log_path=log_path)
    finally:
        _remove_task()
        _kill_alias_processes()
        uninstaller = INSTALL_DIR / "unins000.exe"
        if uninstaller.is_file():
            _run_installer(str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
        shutil.rmtree(INSTALL_DIR, ignore_errors=True)


def _assert_registered_task_matches_autostart_contract(exec_path: str) -> None:
    """The same contract tests/unit/test_windows_autostart_task_template.py
    holds the shipped template to (tests/windows_task_contract.py), asserted
    here against the document that actually governs: the one Task Scheduler
    parsed, normalized and stored when the installer registered it."""
    assert_task_xml_matches_autostart_contract(_registered_task_xml(), exec_path=exec_path)


def _start_task_as(installed: _Installed) -> None:
    """Asks Task Scheduler to run the installed task, from inside a real
    logon of the throwaway account -- the module docstring's one deliberate
    substitution for that account signing in. Everything downstream of
    "run this task now" is the same code path the trigger uses: the
    ``GroupId`` principal resolving to this concrete account, its
    ``LeastPrivilege`` token, its profile and environment, and the action
    itself."""
    output = _run_as_user(
        installed.username, installed.password, "schtasks.exe", f"/run /tn {TASK_NAME}",
        output_dir=installed.home,
    )
    assert "SUCCESS" in output.upper() or output.strip() == "", (
        f"schtasks /run, as {installed.username!r}, did not report success:\n{output}\n"
        f"---- task state (schtasks /query /v) ----\n{_task_state_summary()}"
    )


# --------------------------------------------------------------------------- #
# The tests -- Phase 7 item 2 / 8.2 (Scheduler-started daemon, real system
# contract) and Phase 13 item 4 (crash-restart).
# --------------------------------------------------------------------------- #

async def test_installed_task_starts_daemon_for_a_non_installing_account(_installed):
    _assert_registered_task_matches_autostart_contract(_installed.alias_exe)

    _start_task_as(_installed)

    found = _wait_for_alias_process(_installed.alias_exe, timeout=30.0)
    assert found, (
        f"{_installed.alias_exe} never appeared as a running process within 30s of Task Scheduler "
        f"being asked to run {TASK_NAME!r} for {_installed.username!r}\n"
        f"---- task state (schtasks /query /v) ----\n{_task_state_summary()}"
    )
    pid, owner = found
    assert _installed.username.lower() in owner.lower(), (
        f"{ALIAS_EXE_NAME} (pid {pid}) is running as {owner!r}, not the account the task was started "
        f"for ({_installed.username!r}) -- the Builtin\\Users principal did not resolve to the "
        f"member that asked for the run"
    )

    web_token = _wait_for_path_content(_installed.home / ".privacyfence" / WEB_TOKEN_FILE_NAME, timeout=20)
    mcp_token = _wait_for_path_content(_installed.home / ".privacyfence" / MCP_TOKEN_FILE_NAME, timeout=20)
    _wait_until_connectable("localhost", _installed.port)

    base_url = f"http://localhost:{_installed.port}"
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

    # ── Graceful shutdown propagates to the real process Task Scheduler
    # started -- not just makes it unreachable over HTTP ───────────────────
    assert _wait_until_alias_process_gone(_installed.alias_exe, timeout=20.0), (
        f"{ALIAS_EXE_NAME} (pid {pid}) still running after Quit PrivacyFence"
    )

    settings_path = _installed.home / ".privacyfence" / "config" / "settings.yaml"
    assert "autologon.example.com" in settings_path.read_text(encoding="utf-8")


# The wait below is the task's own <RestartOnFailure><Interval>PT1M</Interval>
# plus room for Task Scheduler's own scheduling granularity, on top of an
# install and a cold daemon start -- well past this module's own 300s
# default, let alone the suite's 30s one.
@pytest.mark.timeout(480)
async def test_task_scheduler_restarts_the_daemon_after_it_crashes(_installed):
    """docs/automated-test-strategy-plan.md Phase 13 item 4: the task's
    ``<RestartOnFailure>`` -- the Windows analogue of the macOS
    LaunchAgent's ``KeepAlive``/``SuccessfulExit=false`` and the Linux
    ``.deb``'s systemd ``Restart=on-failure`` -- shipping proven rather
    than merely registered. A daemon that is killed outright ends its task
    instance with a non-zero result, which is exactly the condition that
    setting exists to answer."""
    _start_task_as(_installed)

    first = _wait_for_alias_process(_installed.alias_exe, timeout=30.0)
    assert first, (
        f"{_installed.alias_exe} never started, so there is nothing to crash\n"
        f"---- task state (schtasks /query /v) ----\n{_task_state_summary()}"
    )
    first_pid, _owner = first
    _wait_until_connectable("localhost", _installed.port)

    # A real crash, not a graceful quit: /f is a TerminateProcess, so the
    # task instance ends non-zero and never gets to clean up after itself.
    kill = subprocess.run(
        ["taskkill", "/pid", first_pid, "/f"], capture_output=True, text=True, timeout=30,
    )
    assert kill.returncode == 0, f"taskkill on pid {first_pid} failed:\n{kill.stdout}{kill.stderr}"
    assert _wait_until_alias_process_gone(_installed.alias_exe, timeout=20.0), (
        f"{ALIAS_EXE_NAME} (pid {first_pid}) survived taskkill /f, so nothing crashed to restart from"
    )

    restarted = _wait_for_alias_process(_installed.alias_exe, timeout=180.0, different_from=first_pid)
    assert restarted, (
        f"Task Scheduler never relaunched {ALIAS_EXE_NAME} within 180s of pid {first_pid} being killed -- "
        f"the task's <RestartOnFailure> (PT1M, 3 attempts) did not take effect\n"
        f"---- task state (schtasks /query /v) ----\n{_task_state_summary()}"
    )
    restarted_pid, restarted_owner = restarted
    assert restarted_pid != first_pid
    assert _installed.username.lower() in restarted_owner.lower(), (
        f"the relaunched {ALIAS_EXE_NAME} (pid {restarted_pid}) runs as {restarted_owner!r}, not "
        f"{_installed.username!r}"
    )
    # Not just a process: the restarted daemon has to actually serve again.
    _wait_for_path_content(_installed.home / ".privacyfence" / WEB_TOKEN_FILE_NAME, timeout=20)
    _wait_until_connectable("localhost", _installed.port)
