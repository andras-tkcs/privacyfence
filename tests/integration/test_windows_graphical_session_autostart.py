"""Real Task Scheduler autostart verification for the Windows installer
(docs/automated-test-strategy-plan.md Phase 7 item 2 and Phase 13 item 4;
the now-removed windows-support-plan.md 8.2).

``test_windows_packaged_smoke.py`` (Phase 6.2) already proves the installer
registers *a* Task Scheduler autostart task (``schtasks /query`` against it)
and that the alias exe it points at, once started, serves a real
daemon/MCP/approval/audit round trip -- but it starts that alias exe itself,
directly, as a subprocess, out of that test's own ``tmp_path``. Nothing
there ever asks Task Scheduler to run anything, and nothing there looks at
what the task actually says.

This module does both:

1. **The registered definition matches the autostart contract.** Not the
   template file in this repo -- the definition read back out of Task
   Scheduler itself (``schtasks /query /xml``), i.e. what the service
   actually parsed, normalized and stored: a ``LogonTrigger`` with no
   ``UserId`` (so it is scoped to any interactive logon, not the installing
   account), a ``Builtin\\Users`` principal bound to the ``Actions`` element
   by a matching ``id``/``Context`` pair, ``LeastPrivilege``, ``Parallel``
   multiple-instances, the real installed ``privacyfence-app.exe`` path as
   the action's ``Command``, the ``RestartOnFailure`` interval/count that
   carries crash-restart, and the two battery settings that would otherwise
   default to "don't start on battery power". Each of those has been a real,
   shipped bug at least once -- see ``installer/privacyfence-task.xml.tmpl``'s
   own header comment -- and every one of them was invisible to a test that
   only asked "does a task with this name exist". The same contract is
   asserted against the shipped template on every PR, on any OS, by
   ``tests/unit/test_windows_autostart_task_template.py``; both call
   ``tests/windows_task_contract.py``.
2. **Task Scheduler really starts the daemon, and really restarts it after a
   crash.** The service is asked to run the installed task; the process it
   launches is confirmed to be the installed exe, running as the logged-on
   user (``Win32_Process``'s ``GetOwner``, not assumed), serving the Phase 3
   daemon/MCP/approval/audit contract, and ending on "Quit PrivacyFence".
   A second test kills that process outright and watches Task Scheduler
   relaunch it -- a *different* pid, serving again, within the task's own
   ``<RestartOnFailure><Interval>PT1M</Interval>`` window (Phase 13 item 4).

**The one deliberate substitution, and the history behind it.** Task
Scheduler is asked to run the task *on demand* rather than by a user signing
in. ``AllowStartOnDemand`` and the trigger share every step that follows the
decision to run -- resolving the ``Builtin\\Users`` principal to a concrete
logged-on member, running the action with that member's ``LeastPrivilege``
token, in their profile -- so everything above is the real mechanism. What
it does not cover is the trigger's own firing, i.e. Task Scheduler deciding
*when*.

That gap is deliberate, and it replaces a substitution that could not work.
This module used to create a throwaway local account, call PowerShell's
``Start-Process -Credential`` for it, call that "signing in", and assert the
daemon turned up. It never did, on any run: ``CreateProcessWithLogonW``
(what that cmdlet, and ``runas.exe``, are built on) creates a *logon
session* but not the Terminal Services *session* logon that Task Scheduler's
``LogonTrigger`` subscribes to, so the trigger was never evaluated at all.
Task Scheduler said so itself once the failure message started asking it --
run 24 of ``windows-graphical-session.yml``, against ``main``, with the task
correctly registered::

    Status:                Ready
    Scheduled Task State:  Enabled
    Run As User:           Users
    Last Run Time:         11/30/1999 12:00:00 AM
    Last Result:           267011        (SCHED_S_TASK_HAS_NOT_RUN)

Registered, enabled, ready, never attempted. Nothing was wrong on the
installer side; the test was asserting something a hosted runner cannot
produce. So the trigger's own firing is now covered where it can actually be
covered: ``release-testing.md``'s Windows human checks, on a real machine
with a real sign-in, tracked on
`privacyfence/privacyfence#121 <https://github.com/privacyfence/privacyfence/issues/121>`_.
(An RDP loopback into the runner would create a genuine session logon, and
was considered -- it needs an RDP client that can run without a desktop of
its own, which a hosted runner does not have, so it would trade a gap that
is honestly described for one that is merely harder to see.)

The throwaway account is gone with it, for a reason worth stating because it
is a property of the shipped task rather than of the test: a task whose
principal is a *group* runs with the interactive token of a member who is
**signed in**, and the daemon lands in that member's own profile. On a
hosted runner exactly one account has a real interactive session -- the
runner's own -- so that is the account this module can have the task run
for, and a throwaway account (which, as above, cannot be given a session at
all) buys nothing. Run 25 showed the same thing from the other side: asking
for the run as that throwaway account returned ``ERROR: Access is denied.``

So, like ``test_linux_graphical_session_autostart.py`` and unlike every
other packaged test in this repo, this module does **not** isolate the
daemon's home directory under ``tmp_path``: Task Scheduler starts the daemon
with no injected environment, in the real profile of the real logged-on
account, which is the whole point. It refuses to run at all (skips) if that
account already has PrivacyFence state, and removes whatever it creates
afterwards -- see ``_real_home_state``. **Only ever run this against a
disposable CI account.**

The install also goes to a machine-wide directory rather than the
installer's own default. ``installer/privacyfence.iss`` is
``PrivilegesRequired=lowest``, so a silent install resolves ``{autopf}`` to
``{userpf}`` -- ``%LOCALAPPDATA%\\Programs\\PrivacyFence``, inside the
installing account's profile, which no *other* account can read. The shipped
task's ``Builtin\\Users`` principal only composes with a per-machine layout,
so that is what this test installs (see ``INSTALL_DIR``).

Skipped entirely unless running on real Windows, elevated (installing
machine-wide and managing a Task Scheduler task needs it), with a just-built
``dist/PrivacyFence-*-setup.exe`` on disk -- same posture as
``test_windows_packaged_smoke.py``, and, like the Linux module, run from its
own ``.github/workflows/windows-graphical-session.yml`` (packaging-related
``main`` pushes, weekly, and on demand) rather than ``build.yml``'s
tag-triggered release pipeline or ``tests.yml``'s per-PR jobs: this is the
same flakiest-and-most-expensive tier in ``docs/automated-test-strategy-
plan.md``'s taxonomy (Phase 7's own objective) the Linux module already
lives in, so a flaky run here must never block an actual release. The module
and workflow keep their "graphical session" names, which now read as the
tier they belong to rather than a literal description of what this module
drives -- renaming them would break the workflow's own run history and
``paths:`` triggers for no gain.
"""
from __future__ import annotations

import asyncio
import base64
import getpass
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest

pytest.importorskip("mcp", reason="mcp (Python MCP client, test-only) not installed -- pip install -e '.[test]'")

from tests.diagnostics import (  # noqa: E402
    capture_directory_manifest,
    copy_named_logs,
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
# module docstring. No space in the path: Inno parses `/DIR=` off its own raw
# command line rather than argv, so a value that needs quoting is one more
# thing to get wrong in a test whose subject is something else entirely.
INSTALL_DIR = Path(os.environ.get("SystemDrive", "C:") + "\\") / "PrivacyFenceAutostartTest"


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
        reason="a machine-wide install and Task Scheduler task management need an elevated shell",
    ),
    # A real silent install, a real Scheduler-driven daemon cold start, and a
    # full MCP/approval/audit round trip -- comfortably slower than the
    # suite's default timeout=30, same reasoning as every other
    # packaged/system test in this repo.
    pytest.mark.timeout(300),
]


# --------------------------------------------------------------------------- #
# Task Scheduler: what it stored, what it says happened, what it will do now
# --------------------------------------------------------------------------- #

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


def _session_table() -> str:
    """Who is actually signed in, as Windows itself reports it.

    Load-bearing for reading a failure here rather than merely decorative: a
    group-principal task runs with the interactive token of a *signed-in*
    member, so "no session" and "no daemon" are the same failure, and this is
    the only thing that tells them apart from a broken action."""
    result = subprocess.run(["query", "user"], capture_output=True, text=True, timeout=15)
    return (result.stdout + result.stderr).strip() or "(query user returned nothing)"


def _enable_task_scheduler_event_log() -> None:
    """Best-effort: make sure Task Scheduler's own operational channel is
    recording, since it is the only place that says *why* a task did or did
    not run again. Enabled by default on current Windows; enabling it is
    idempotent and costs nothing when it already is."""
    subprocess.run(
        ["wevtutil", "sl", "Microsoft-Windows-TaskScheduler/Operational", "/e:true"],
        capture_output=True, text=True, timeout=20,
    )


def _task_scheduler_events(count: int = 40) -> str:
    """The last *count* Task Scheduler operational events, newest first.

    ``schtasks /query /v`` reports only the outcome of the last run --
    "Last Result: 1" and nothing about what the service decided afterwards.
    Whether a restart was attempted, skipped, or never considered is in this
    log and nowhere else."""
    result = subprocess.run(
        [
            "wevtutil", "qe", "Microsoft-Windows-TaskScheduler/Operational",
            f"/c:{count}", "/rd:true", "/f:text",
        ],
        capture_output=True, text=True, timeout=60,
    )
    text = (result.stdout + result.stderr).strip()
    return text or "(no Task Scheduler operational events -- is the channel enabled?)"


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
    dropped the 1.2-only Settings elements; schema defaults the template
    never mentioned, which is how the battery settings were found)."""
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


def _daemon_log_tail(home: Path, *, max_chars: int = 4000) -> str:
    """The daemon's own log, which is the only thing it leaves behind when
    Task Scheduler starts it.

    ``daemon_main.setup_logging`` writes to ``<data dir>/logs/
    privacyfence.log`` -- for a bundled app, ``%USERPROFILE%\\.privacyfence\\
    logs\\privacyfence.log`` -- and ``main()`` logs an explicit
    ``Fatal error: ...`` with a traceback there before returning 1. A
    Scheduler-launched process has no console and no redirected stdout, so
    unlike ``test_windows_packaged_smoke.py`` (which captures the daemon's
    stdout to a file it can quote on failure) this file is the *only* place
    a daemon that died on startup says why. ``Last Result: 1`` from
    ``schtasks /query`` says only that it did."""
    log_path = home / ".privacyfence" / "logs" / "privacyfence.log"
    if not log_path.exists():
        return f"({log_path} missing -- the daemon never got as far as setting up logging)"
    text = log_path.read_text(errors="replace")
    return text[-max_chars:] if text else "(empty)"


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


# --------------------------------------------------------------------------- #
# PowerShell helper -- Win32_Process/GetOwner has no schtasks.exe-style
# command-line-only equivalent, so this module drops into PowerShell for
# exactly that (everything else here uses the same plain
# cmd-tool-via-subprocess style as test_windows_packaged_smoke.py/
# test_deb_packaged_lifecycle.py).
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
    it -- so autoloading its own built-in cmdlets (``Get-CimInstance`` from
    ``CimCmdlets``, etc.) fails with "the module could not be loaded", 100%
    reproducibly. Prepending Windows PowerShell 5.1's own system module
    directory restores the lookup those cmdlets need."""
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


def _wait_for_path_content(path: Path, *, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                return content
        time.sleep(0.2)
    raise AssertionError(f"{path} never appeared/populated within {timeout}s")


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


# --------------------------------------------------------------------------- #
# Fixtures -- the real (un-isolated) home, and one real install per test
# --------------------------------------------------------------------------- #

@pytest.fixture
def _real_home_state(request):
    """The daemon's home is the real logged-on account's profile, not
    ``tmp_path``.

    Task Scheduler launches the action itself, with no injected environment
    -- that is the whole point here -- so there is nowhere to redirect
    ``%USERPROFILE%`` to even if this module wanted to. Same posture, and
    same safeguards, as ``test_linux_graphical_session_autostart.py``'s own
    identically-named fixture: skip rather than run if this account already
    has PrivacyFence state, and remove whatever this test creates.

    Also doubles as this module's docs/automated-test-strategy-plan.md Phase
    10 diagnostics capture, for the same reason that module's does -- the
    generic per-``tmp_path`` capture in ../conftest.py cannot see any of
    this, and a Scheduler-launched process has no redirected stdout of its
    own to collect either, so this reaches for what Task Scheduler knows
    (the task's own stored definition and last-run result) instead."""
    real_home = Path.home()
    state_dir = real_home / ".privacyfence"
    if state_dir.exists():
        pytest.skip(
            f"{state_dir} already exists -- this test boots the daemon into the real profile with "
            "no isolation (the whole point is a real, un-injected Task Scheduler launch); only run "
            "it against a disposable account with no existing PrivacyFence state"
        )
    try:
        yield real_home
    finally:
        rep_call = getattr(request.node, "rep_call", None)
        if rep_call is not None and rep_call.failed:
            dest = failure_dir(request.node.nodeid, suite=suite_name_for(__file__))
            write_environment_info(dest / "environment.txt")
            capture_directory_manifest(state_dir, dest / "manifest-profile-home.txt")
            (dest / "logs").mkdir(parents=True, exist_ok=True)
            task_info = subprocess.run(
                ["schtasks", "/query", "/tn", TASK_NAME, "/v", "/fo", "list"],
                capture_output=True, text=True,
            )
            (dest / "logs" / "schtasks-query.txt").write_text(
                task_info.stdout + task_info.stderr, encoding="utf-8",
            )
            # The stored definition, not this repo's template: which of the
            # two disagrees with the other is the whole question whenever a
            # task registers but then behaves unexpectedly.
            (dest / "logs" / "schtasks-query-xml.txt").write_text(
                _registered_task_xml_or_error(), encoding="utf-8",
            )
            (dest / "logs" / "query-user.txt").write_text(_session_table(), encoding="utf-8")
            (dest / "logs" / "taskscheduler-events.txt").write_text(
                _task_scheduler_events(120), encoding="utf-8",
            )
            # The daemon's own log, if it got far enough to write one -- see
            # _daemon_log_tail for why this is the only thing a
            # Scheduler-launched daemon leaves behind.
            copy_named_logs(state_dir, dest / "logs")
        shutil.rmtree(state_dir, ignore_errors=True)


class _Installed:
    """What a test needs to know about the installation the fixture made:
    the profile the daemon will boot into, the port its settings.yaml was
    seeded with, and where the installed alias exe actually is."""

    def __init__(self, *, home: Path, port: int, log_path: Path) -> None:
        self.home = home
        self.port = port
        self.log_path = log_path
        self.alias_exe = str(INSTALL_DIR / ALIAS_EXE_NAME)


@pytest.fixture
def _installed(_real_home_state, tmp_path):
    """One real silent install per test, and its full teardown -- both tests
    below need the same installed-and-registered starting state, and neither
    may leave a task or a daemon behind for the other (or for whatever runs
    next on this machine)."""
    home = _real_home_state
    port = _free_port()
    _prepare_home(home, port=port)

    if INSTALL_DIR.exists():  # a previous run that died before its own cleanup
        _kill_alias_processes()
        shutil.rmtree(INSTALL_DIR, ignore_errors=True)

    _enable_task_scheduler_event_log()
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
        yield _Installed(home=home, port=port, log_path=log_path)
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


def _start_task() -> None:
    """Ask Task Scheduler to run the installed task now.

    The module docstring's one deliberate substitution for a user signing
    in: the trigger decides *when*, and everything after that decision --
    resolving the ``Builtin\\Users`` principal to a signed-in member, that
    member's ``LeastPrivilege`` token, their profile, the action itself --
    is the same code path either way."""
    result = subprocess.run(
        ["schtasks", "/run", "/tn", TASK_NAME], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"schtasks /run failed (exit {result.returncode}):\n{result.stdout}{result.stderr}\n"
        f"---- who is signed in (query user) ----\n{_session_table()}\n"
        f"---- task state (schtasks /query /v) ----\n{_task_state_summary()}"
    )


def _start_task_and_wait_for_daemon(installed: _Installed) -> tuple[str, str]:
    _start_task()
    found = _wait_for_alias_process(installed.alias_exe, timeout=30.0)
    assert found, (
        f"{installed.alias_exe} never appeared as a running process within 30s of Task Scheduler "
        f"being asked to run {TASK_NAME!r}\n"
        f"---- who is signed in (query user) ----\n{_session_table()}\n"
        f"---- task state (schtasks /query /v) ----\n{_task_state_summary()}\n"
        f"---- daemon log (tail) ----\n{_daemon_log_tail(installed.home)}"
    )
    pid, owner = found
    # A group principal runs the action as a signed-in member, in that
    # member's own profile -- which is the account running this test, since
    # it is the only one with a real session here.
    assert getpass.getuser().lower() in owner.lower(), (
        f"{ALIAS_EXE_NAME} (pid {pid}) is running as {owner!r}, not the signed-in account "
        f"({getpass.getuser()!r}) the Builtin\\Users principal should have resolved to"
    )
    return pid, owner


# --------------------------------------------------------------------------- #
# The tests -- Phase 7 item 2 / 8.2 (Scheduler-started daemon, real system
# contract) and Phase 13 item 4 (crash-restart).
# --------------------------------------------------------------------------- #

async def test_installed_task_definition_starts_the_packaged_daemon(_installed):
    _assert_registered_task_matches_autostart_contract(_installed.alias_exe)

    pid, _owner = _start_task_and_wait_for_daemon(_installed)

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
    first_pid, _owner = _start_task_and_wait_for_daemon(_installed)
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
        f"---- task state (schtasks /query /v) ----\n{_task_state_summary()}\n"
        f"---- Task Scheduler operational log (newest first) ----\n{_task_scheduler_events()}\n"
        f"---- daemon log (tail) ----\n{_daemon_log_tail(_installed.home)}"
    )
    restarted_pid, restarted_owner = restarted
    assert restarted_pid != first_pid
    assert getpass.getuser().lower() in restarted_owner.lower(), (
        f"the relaunched {ALIAS_EXE_NAME} (pid {restarted_pid}) runs as {restarted_owner!r}, not "
        f"{getpass.getuser()!r}"
    )
    # Not just a process: the restarted daemon has to actually serve again.
    _wait_for_path_content(_installed.home / ".privacyfence" / WEB_TOKEN_FILE_NAME, timeout=20)
    _wait_until_connectable("localhost", _installed.port)
