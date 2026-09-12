"""Real graphical-session autostart verification for the Linux ``.deb``
(docs/automated-test-strategy-plan.md Phase 7 item 1; docs/linux-local-deb-
packaging-plan.md P7.2).

``test_deb_packaged_lifecycle.py`` (Phase 6.3) already proves the install/
validate/remove/purge/upgrade lifecycle, including that ``desktop-file-
validate`` accepts the installed ``/etc/xdg/autostart/privacyfence.desktop``.
What it deliberately does *not* prove -- see that module's own docstring,
which only starts the daemon directly -- is P7.2's actual open question:
does a real graphical login *itself* start the daemon via that autostart
entry, with nothing else telling it to?

There is no real display manager (gdm/lightdm/sddm) available on a CI
runner to log into, so this module makes one deliberate substitution
instead of a hand-rolled one: it drives the *real* OS mechanism a modern
systemd-based desktop session already uses for XDG autostart --
``systemd-xdg-autostart-generator`` (systemd >= 247; shipped as part of
``systemd`` itself, confirmed present by this module's own skip condition)
turns every ``/etc/xdg/autostart/*.desktop`` entry into a transient
``app-<name>@autostart.service`` unit, gated behind
``xdg-desktop-autostart.target`` -- the exact target a real GNOME/KDE/Sway
session's own compositor/session manager starts once it comes up. Nothing
in this module parses or interprets ``privacyfence.desktop`` itself; it
only brings up a real ``systemd --user`` manager for the account (the same
``user@<uid>.service`` unit ``pam_systemd`` starts at a real login) and
starts that one real target -- standing in for the missing physical
login, exactly as P7.2's own text anticipates ("real or VM ... desktop").
Everything downstream -- the generated unit's shape, whether it actually
gets pulled in, whether starting it actually launches the packaged binary,
whether the daemon then serves a real MCP/approval/audit round trip, and
whether "Quit PrivacyFence" cleanly stops the *systemd unit*, not just the
process -- is exercised for real.

Unlike every other packaged/system test in this repo, this one does **not**
isolate ``$HOME`` under ``tmp_path``: the whole point is that systemd starts
the daemon the same way a real login would, with no injected environment.
It refuses to run at all (skips) if this account already has real
PrivacyFence state under ``$HOME``, and always removes whatever it creates
there afterwards -- see ``_real_home_state``. Only ever run this against a
disposable CI account.

A second, independent test in this module closes the other half of P7.2's
"where practical" ask -- the OAuth loopback browser-opening flow -- in the
one way a real graphical session uniquely enables:
``tests/platform/test_browser_launch_default.py`` (Phase 2.3) already
proves ``oauth_loopback.run_browser_oauth()``'s default path reaches the
real ``webbrowser.open`` function object, but it does so by *monkeypatching
that function itself*, headless, with no ``$DISPLAY`` -- by its own
docstring's admission, the stdlib ``webbrowser`` module's own real
browser-detection-and-subprocess-launch logic (the part that differs
between "no GUI available" and "a real X session is up") is never actually
exercised end to end anywhere in this repo. This module's second test
runs that unmocked, under a real Xvfb ``$DISPLAY``, pointed (via the
stdlib's own supported ``$BROWSER`` override) at a tiny real executable
that performs the actual loopback HTTP round trip itself, as a real
subprocess -- proving the genuine OS launch chain, not an injected
stand-in.

Both tests are skipped entirely unless running on real Linux, booted with
systemd as PID 1 (``/run/systemd/system`` -- a container typically fails
this, a GitHub-hosted ``ubuntu-latest`` runner, a real VM, passes it), with
whatever else each specific test additionally needs (a just-built ``.deb``
and passwordless root for the autostart test; ``Xvfb`` for the browser
test). This is the flakiest, most expensive tier in
``docs/automated-test-strategy-plan.md``'s whole taxonomy by design (see
Phase 7's own objective) -- scheduled on packaging-related ``main`` changes,
nightly/periodic runs, and release-candidate tags via its own
``.github/workflows/linux-graphical-session.yml``, deliberately kept out of
both the per-PR ``tests.yml`` jobs and ``build.yml``'s tag-triggered release
pipeline (a flaky run here must never block an actual release the way a
failure in ``test_deb_packaged_lifecycle.py`` correctly does).
"""
from __future__ import annotations

import asyncio
import contextlib
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

from tests.integration.test_deb_packaged_lifecycle import (  # noqa: E402
    MCP_TOKEN_FILE_NAME,
    OPT_DIR,
    WEB_TOKEN_FILE_NAME,
    AUTOSTART_DESKTOP_FILE,
    _bootstrap_session,
    _built_debs,
    _can_install_packages,
    _dpkg,
    _free_port,
    _prepare_home,
    _propose_trusted_sender_rule,
    _purge_if_present,
    _quit,
    _resolve_pending_card,
    _wait_until_connectable,
)

# Empirically confirmed against the real systemd-xdg-autostart-generator
# binary shipped in this environment (systemd 255, Ubuntu -- same lineage
# as GitHub's ubuntu-latest runner image): a source .desktop file named
# "<stem>.desktop" becomes unit "app-<stem>@autostart.service", listed
# under xdg-desktop-autostart.target.wants/. Derived from
# AUTOSTART_DESKTOP_FILE rather than hardcoded so it can never silently
# drift from the actual installed filename.
AUTOSTART_UNIT_NAME = f"app-{AUTOSTART_DESKTOP_FILE.stem}@autostart.service"

_XDG_AUTOSTART_GENERATOR_PATHS = (
    Path("/usr/lib/systemd/user-generators/systemd-xdg-autostart-generator"),
    Path("/lib/systemd/user-generators/systemd-xdg-autostart-generator"),
)


def _has_xdg_autostart_generator() -> bool:
    return any(p.exists() for p in _XDG_AUTOSTART_GENERATOR_PATHS)


def _systemd_is_init() -> bool:
    """The standard, documented way to detect "booted with systemd as PID
    1" (systemd's own docs). Most containers -- including whatever this
    module might accidentally be run in outside CI -- fail this; a real
    GitHub-hosted ``ubuntu-latest`` runner (a real VM, not a container)
    passes it."""
    return Path("/run/systemd/system").exists()


pytestmark = [
    pytest.mark.packaged,
    pytest.mark.skipif(
        platform.system() != "Linux",
        reason="only meaningful on real Linux -- see docs/linux-local-deb-packaging-plan.md P7.2",
    ),
    # Same heavy-setup timeout reasoning as test_deb_packaged_lifecycle.py:
    # a real dpkg install, a real systemd --user manager brought up from
    # scratch, a real daemon boot via a real login-equivalent target, and a
    # full MCP/approval/audit round trip.
    pytest.mark.timeout(180),
]


# --------------------------------------------------------------------------- #
# Small polling helpers -- same shape as test_deb_packaged_lifecycle.py's own
# _wait_until_connectable/_wait_for_file, adapted for a process this module
# doesn't itself parent (systemd does), so there is no Popen to poll.poll().
# --------------------------------------------------------------------------- #

def _wait_for_path(path: Path, *, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.1)
    raise AssertionError(f"{what} ({path}) never appeared within {timeout}s")


def _wait_for_path_content(path: Path, *, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                return content
        time.sleep(0.1)
    raise AssertionError(f"{path} never appeared/populated within {timeout}s")


def _wait_for_unit_property(systemctl_user, unit: str, prop: str, expected: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = systemctl_user("show", unit, "-p", prop, "--value").stdout.strip()
        if last == expected:
            return
        time.sleep(0.2)
    status = systemctl_user("status", unit, check=False).stdout
    raise AssertionError(
        f"{unit}'s {prop} never reached {expected!r} (last seen {last!r}) within {timeout}s:\n{status}"
    )


def _current_user() -> str:
    return getpass.getuser()


# --------------------------------------------------------------------------- #
# $HOME guard/cleanup -- see module docstring for why this is the one
# packaged/system test in this repo that deliberately does NOT isolate
# $HOME under tmp_path.
# --------------------------------------------------------------------------- #

@pytest.fixture
def _real_home_state():
    real_home = Path.home()
    state_dir = real_home / ".privacyfence"
    if state_dir.exists():
        pytest.skip(
            f"{state_dir} already exists -- this test boots the daemon into the real $HOME with "
            "no isolation (the whole point is a real, un-injected login); only run it against a "
            "disposable account with no existing PrivacyFence state"
        )
    _purge_if_present()
    try:
        yield real_home
    finally:
        _purge_if_present()
        shutil.rmtree(state_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Test 1 -- P7.2: real login-equivalent XDG autostart actually starts the
# packaged daemon, which then serves a real daemon/MCP/approval/audit round
# trip (Phase 3's own contract shape), and "Quit PrivacyFence" stops the
# real systemd unit, not just the process.
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(
    not _built_debs(),
    reason=(
        "no dist/privacyfence_*.deb built yet -- run scripts/build_deb.sh first, same as "
        "test_deb_packaged_lifecycle.py's own identical skip"
    ),
)
@pytest.mark.skipif(
    shutil.which("dpkg") is None or shutil.which("dpkg-deb") is None,
    reason="dpkg/dpkg-deb not on PATH (apt-get install dpkg-dev)",
)
@pytest.mark.skipif(
    not _can_install_packages(),
    reason="installing a .deb needs root -- run as root or with passwordless sudo",
)
@pytest.mark.skipif(not _systemd_is_init(), reason="not booted with systemd as PID 1 -- needs a real VM, not a container")
@pytest.mark.skipif(
    not _has_xdg_autostart_generator(),
    reason="systemd-xdg-autostart-generator not present -- needs systemd >= 247 on a desktop-capable Ubuntu/Debian",
)
@pytest.mark.skipif(
    shutil.which("loginctl") is None or shutil.which("systemctl") is None,
    reason="loginctl/systemctl not on PATH",
)
async def test_deb_autostart_activates_daemon_via_real_login_session(_real_home_state):
    home = _real_home_state
    user = _current_user()
    uid = os.getuid()

    deb_path = _built_debs()[-1]
    port = _free_port()

    # Pre-seed the real $HOME's settings.yaml the same way _prepare_home
    # does for every other packaged/system test -- a real free port, update
    # checks off (this tier makes no real outbound network calls) -- but
    # against the *real* $HOME, before the daemon's own first boot, exactly
    # what a real first login would find already in place from an earlier
    # "Authenticate..." session.
    _prepare_home(home, port=port)

    # ── Install; confirm the postinst's own contract (P3.3): a fresh
    # install must never itself start the daemon -- only the *next*
    # graphical login's XDG autostart should ─────────────────────────────
    _dpkg("-i", str(deb_path))
    time.sleep(1.0)
    assert not (home / ".privacyfence" / WEB_TOKEN_FILE_NAME).exists(), (
        "installing the .deb must never itself start the daemon -- only the next login should"
    )

    # ── "Log in": bring up a real systemd --user manager for this account,
    # exactly the unit pam_systemd starts at a real login, then trigger the
    # same target a real desktop session's own compositor/session manager
    # pulls in once it's up. See module docstring for why this is the one
    # deliberate substitution here -- everything else below is the real OS
    # mechanism. ───────────────────────────────────────────────────────────
    subprocess.run(
        ["sudo", "-n", "loginctl", "enable-linger", user], check=True, capture_output=True, text=True, timeout=15,
    )
    subprocess.run(
        ["sudo", "-n", "systemctl", "start", f"user@{uid}.service"],
        check=True, capture_output=True, text=True, timeout=15,
    )

    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}")
    _wait_for_path(runtime_dir / "bus", timeout=30, what="user session D-Bus socket")
    user_env = {
        **os.environ,
        "XDG_RUNTIME_DIR": str(runtime_dir),
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime_dir}/bus",
    }

    def systemctl_user(*args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["systemctl", "--user", *args], env=user_env, capture_output=True, text=True, timeout=30, check=check,
        )

    # A real login's user manager always (re-)runs its generators
    # (systemd-xdg-autostart-generator among them) on its own startup;
    # daemon-reload makes that moment explicit and repeatable here, since
    # the .deb's postinst just dropped a new autostart entry after this
    # manager may already have been running for other CI reasons.
    systemctl_user("daemon-reload")

    unit = AUTOSTART_UNIT_NAME
    unit_def = systemctl_user("cat", unit)
    assert "/usr/bin/privacyfence-app" in unit_def.stdout, (
        f"{unit} wasn't generated correctly from the installed autostart entry:\n{unit_def.stdout}"
    )
    assert "PartOf=graphical-session.target" in unit_def.stdout

    wants = systemctl_user("show", "xdg-desktop-autostart.target", "-p", "Wants", "--value")
    assert unit in wants.stdout.split(), (
        f"{unit} is not pulled in by xdg-desktop-autostart.target -- a real desktop session "
        f"would never start it at login:\n{wants.stdout}"
    )

    # The actual "login" moment -- the same target a real GNOME/KDE/Sway
    # session starts once its compositor/session manager comes up.
    systemctl_user("start", "xdg-desktop-autostart.target")

    _wait_for_unit_property(systemctl_user, unit, "ActiveState", "active", timeout=20)
    main_pid = systemctl_user("show", unit, "-p", "MainPID", "--value").stdout.strip()
    assert main_pid and main_pid != "0", (
        f"{unit} is active but reports no MainPID:\n{systemctl_user('status', unit, check=False).stdout}"
    )
    exe_link = os.readlink(f"/proc/{main_pid}/exe")
    expected_exe = str(OPT_DIR / "PrivacyFenceApp")
    assert exe_link == expected_exe, f"systemd started {exe_link!r}, not the packaged binary at {expected_exe!r}"

    web_token = _wait_for_path_content(home / ".privacyfence" / WEB_TOKEN_FILE_NAME, timeout=20)
    mcp_token = _wait_for_path_content(home / ".privacyfence" / MCP_TOKEN_FILE_NAME, timeout=20)
    _wait_until_connectable("localhost", port)

    base_url = f"http://localhost:{port}"
    mcp_url = f"{base_url}/mcp"

    # ── Phase 3's own daemon/MCP/approval/audit contract shape, against a
    # daemon that this test never itself started a process for ───────────
    async with httpx.AsyncClient(base_url=base_url, follow_redirects=True) as web_client:
        session_id = await _bootstrap_session(web_client, web_token)
        assert (await web_client.get("/settings")).status_code == 200

        allow_task = asyncio.create_task(
            _propose_trusted_sender_rule(mcp_url, mcp_token, value=["autostart.example.com"])
        )
        await _resolve_pending_card(web_client, session_id, decision="confirm")
        allow_result = await allow_task
        assert allow_result.isError is not True, getattr(allow_result, "content", allow_result)
        assert allow_result.structuredContent["changed"] is True

        await _quit(web_client, session_id)

    # ── Graceful shutdown propagates back to systemd: the unit deactivates
    # and the real process is actually gone -- not just unreachable over
    # HTTP -- proving "Quit PrivacyFence" stops the real systemd-managed
    # service, not merely the daemon's own event loop. ────────────────────
    _wait_for_unit_property(systemctl_user, unit, "ActiveState", "inactive", timeout=20)
    assert not Path(f"/proc/{main_pid}").exists(), f"pid {main_pid} still alive after Quit PrivacyFence"

    settings_path = home / ".privacyfence" / "config" / "settings.yaml"
    assert "autostart.example.com" in settings_path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Test 2 -- P7.2 "where practical": the real (unmocked) OAuth loopback
# browser-opening flow, under a real Xvfb $DISPLAY.
# --------------------------------------------------------------------------- #

def _free_x_display() -> int:
    for candidate in range(50, 100):
        if not Path(f"/tmp/.X11-unix/X{candidate}").exists():
            return candidate
    raise RuntimeError("no free X display number found in :50-:99")


@contextlib.contextmanager
def _xvfb_display():
    display_num = _free_x_display()
    proc = subprocess.Popen(
        ["Xvfb", f":{display_num}", "-screen", "0", "1024x768x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        socket_path = Path(f"/tmp/.X11-unix/X{display_num}")
        _wait_for_path(socket_path, timeout=10, what="Xvfb's X11 socket")
        yield f":{display_num}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.skipif(not _systemd_is_init(), reason="not booted with systemd as PID 1 -- needs a real VM, not a container")
@pytest.mark.skipif(shutil.which("Xvfb") is None, reason="Xvfb not on PATH (apt-get install xvfb)")
@pytest.mark.timeout(60)
def test_oauth_loopback_reaches_a_real_launched_browser_process(tmp_path, monkeypatch):
    import webbrowser

    from privacyfence.oauth_loopback import run_browser_oauth

    capture_file = tmp_path / "captured_urls.txt"
    script_path = tmp_path / "fake-browser.py"
    port = _free_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    # A real executable, launched as a real OS subprocess by the stdlib's
    # own (unmocked) webbrowser.open() -- it performs the actual loopback
    # HTTP round trip itself, the same way a real browser hits redirect_uri
    # after a user finishes consent, just scripted instead of clicked.
    # Explicitly disables proxying for this one loopback call, same
    # reasoning as tests/platform/test_browser_launch_default.py's own
    # _NO_PROXY_SESSION: a redirect to 127.0.0.1 must never go through
    # whatever HTTP(S)_PROXY the runner's environment happens to set.
    script_path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from urllib.parse import urlparse, parse_qs\n"
        "from urllib.request import build_opener, ProxyHandler\n"
        "\n"
        "url = sys.argv[1]\n"
        f"with open({str(capture_file)!r}, 'a', encoding='utf-8') as f:\n"
        "    f.write(url + '\\n')\n"
        "state = parse_qs(urlparse(url).query)['state'][0]\n"
        "opener = build_opener(ProxyHandler({}))\n"
        f"opener.open({redirect_uri!r} + '?code=auth-code-123&state=' + state, timeout=5)\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)

    with _xvfb_display() as display:
        monkeypatch.setenv("DISPLAY", display)
        monkeypatch.setenv("BROWSER", str(script_path))
        # webbrowser caches its browser-detection result process-wide the
        # first time anything calls webbrowser.get() -- force it to
        # re-detect now that $DISPLAY/$BROWSER describe this real
        # graphical session (see module docstring for why this is the one
        # gap test_browser_launch_default.py's own monkeypatch-webbrowser.
        # open approach leaves open).
        monkeypatch.setattr(webbrowser, "_tryorder", None)

        def build_authorize_url(uri: str, state: str, code_challenge: str) -> str:
            return f"https://provider.example/authorize?state={state}&redirect_uri={uri}"

        exchanged: dict = {}

        def exchange(code: str, uri: str, verifier: str) -> dict:
            exchanged.update(code=code, redirect_uri=uri, verifier=verifier)
            return {"access_token": "tok-123"}

        result = run_browser_oauth(build_authorize_url, exchange, port=port, timeout=15)

    assert exchanged.get("code") == "auth-code-123"
    assert result == {"access_token": "tok-123"}
    captured = capture_file.read_text(encoding="utf-8").strip().splitlines()
    assert captured and captured[0].startswith("https://provider.example/authorize?state="), captured
