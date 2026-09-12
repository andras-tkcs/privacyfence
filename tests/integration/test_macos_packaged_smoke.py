"""Release-workflow smoke test against the actual packaged macOS artifact
(TST-15).

Every other test in this repo runs against source -- an editable install, or
(tests/integration/test_shim_mcp_contract.py) a freshly-built dist/shim.js
run straight from mcpb/shim/. None of that proves the thing end users
actually download -- ``dist/PrivacyFence-<version>.dmg``, produced by
``scripts/build_dmg.sh`` via PyInstaller (``PrivacyFenceApp.spec``) -- is
correctly laid out, actually starts, and actually speaks MCP once bundled.
PyInstaller's own module-discovery pass has silently dropped a package
before (a hidden import, a missing data file) in a way no source-tree test
can catch, since a source-tree test never leaves the interpreter that
already knows how to import everything.

This is deliberately narrow -- one round trip, not a real test suite for
the packaged app:

1. **Install**: mount the just-built DMG (``hdiutil attach``) and copy
   ``PrivacyFenceApp.app`` out of it, the way dragging it to ``/Applications``
   would -- minus actually writing to a shared runner's ``/Applications``,
   which direct execution of the bundle's own binary doesn't require (see
   ``running_packaged_daemon`` below).
2. **Start the daemon**: run the frozen binary directly (not via
   ``open``/Finder -- that's what would invoke Gatekeeper, which a bundle
   built and immediately run on this same machine was never quarantined
   for in the first place) with an isolated ``$HOME``, then mint a
   bootstrap link the same way a human with filesystem access to this
   machine but no daemon-log line handy would (``POST /api/bootstrap``
   with the persistent ``web_token`` read straight off disk -- see
   ``running_packaged_daemon`` below for why this, and not scraping the
   daemon's own stdout, is the only reliable way to get one: SEC-10's
   ``SecretRedactingFormatter`` redacts a ``bootstrap=<value>`` substring
   from every log line on principle, the startup line included).
3. **Connect via the MCP shim**: build and spawn the real
   ``mcpb/shim/dist/shim.js`` (same artifact Claude Desktop would run) over
   real stdio, exactly like test_shim_mcp_contract.py, pointed at the
   already-running daemon via the same ``$HOME``.
4. **Open the approval UI**: a real headless-Chromium page follows the
   bootstrap link (SEC-06), landing signed in on ``/approvals``.
5. **One synthetic Allow/Deny round trip**: call
   ``privacyfence_propose_auto_accept_rule_change`` (the one meta-tool that
   always opens a confirmation popup, so this needs no connector OAuth setup
   at all) over MCP, click "Confirm" on the real served card from the real
   browser, and assert the MCP call the whole time was blocked on returns
   the confirmed result once that happens.
6. **State lives outside the package**: delete the installed
   ``PrivacyFenceApp.app`` -- the actual "uninstall" gesture on macOS (drag
   to Trash; there's no installer/uninstaller pair the way Windows/Linux
   have) -- and confirm the rule change from step 5 is still on disk under
   ``$HOME/.privacyfence``, the same "user state survives package removal"
   property ``test_deb_packaged_lifecycle.py``/``test_windows_packaged_
   smoke.py`` assert for their own platforms' removal gesture.
7. **Signature/notarization**: when the DMG was built with ``--sign``/
   ``NOTARIZE_PROFILE`` (as ``build.yml``'s release job always does; a local
   unsigned dev build is legitimate and skips this instead of failing it),
   verify the installed bundle's code-signature chain (``codesign
   --verify --deep --strict``), that it's actually signed by a real
   Developer ID identity rather than ad-hoc, that Gatekeeper's own policy
   engine would accept it (``spctl --assess --type execute``), and that the
   DMG itself carries a stapled notarization ticket. This proves the
   artifact's Gatekeeper story automatically; the interactive first-launch
   "are you sure you want to open this" dialog a human actually sees stays
   manual, per this plan's own governing rule. Runs against its own private
   copy of the bundle (``signed_app_copy``), not the one step 6 deletes --
   see that fixture's own docstring for why.
8. **Upgrade in place** (docs/automated-test-strategy-plan.md Phase 6 item
   20 -- deliberately not built in the same PR as steps 1-7): install
   version N, apply real state through the daemon's own MCP surface,
   replace the bundle with a synthetically-relabeled version N+1 at the
   same ``$HOME`` (step 6 already established that "installing" a new
   version here is just replacing the ``.app`` bundle wholesale), and
   confirm the state survived and the new bundle still starts and serves.
   Its own MCP round trip is the lighter direct-``/mcp``-plus-HTTP-decide
   substitution ``test_deb_packaged_lifecycle.py``/``test_windows_packaged_
   smoke.py`` already use for their own scenarios, not steps 3-5's real
   shim/browser -- the shim artifact itself is already proven by steps 1-5;
   this step's own job is proving state survival across a bundle swap.

Skipped entirely unless running on real macOS with a just-built DMG on disk
(this only makes sense as a step in ``.github/workflows/build.yml``'s
``build`` job, right after ``scripts/build_dmg.sh`` -- see that workflow's
"Run packaged smoke test" step) and Node/the ``mcp``/``playwright`` test
extras available -- never runs as part of the ordinary ``pytest`` invocation
in tests.yml's ubuntu-latest job.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import plistlib
import re
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
import yaml

mcp_client = pytest.importorskip(
    "mcp", reason="mcp (Python MCP client, test-only) not installed -- pip install -e '.[test]'"
)
from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

pytest.importorskip(
    "playwright.sync_api",
    reason="playwright (test-only) not installed -- pip install -e '.[test]' && playwright install chromium",
)
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIM_DIR = REPO_ROOT / "mcpb" / "shim"
SHIM_ENTRY = SHIM_DIR / "dist" / "shim.js"
DIST_DIR = REPO_ROOT / "dist"
SETTINGS_EXAMPLE = REPO_ROOT / "src" / "privacyfence" / "resources" / "settings.yaml.example"
WEB_TOKEN_FILE_NAME = "web_token"  # web/server.py's TOKEN_FILE_NAME
MCP_TOKEN_FILE_NAME = "mcp_token"  # web/mcp_auth.py's MCP_TOKEN_FILE_NAME


def _built_dmgs() -> list[Path]:
    return sorted(DIST_DIR.glob("PrivacyFence-*.dmg")) if DIST_DIR.is_dir() else []


pytestmark = [
    pytest.mark.skipif(
        platform.system() != "Darwin",
        reason="only meaningful against a real .app/.dmg -- see the plan's TST-15 row",
    ),
    pytest.mark.skipif(
        not _built_dmgs(),
        reason=(
            "no dist/PrivacyFence-*.dmg built yet -- this is the release-workflow smoke test "
            "build.yml's `build` job runs after scripts/build_dmg.sh; run that script locally "
            "first to exercise this test outside CI"
        ),
    ),
    pytest.mark.skipif(shutil.which("node") is None, reason="Node not on PATH -- this test spawns the real shim"),
    # DMG mount/copy + a real PyInstaller cold start + npm install/build (first run per session)
    # + a real headless-browser round trip is comfortably slower than pure-Python socket tests --
    # same reasoning as test_shim_mcp_contract.py's own inflated timeout for the same npm-install
    # cost, plus this test's own daemon startup and browser work on top.
    pytest.mark.timeout(300),
    pytest.mark.packaged,
]


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


def _copy_app_from_dmg(dst_dir: Path) -> Path:
    """Mounts the just-built DMG (``hdiutil attach``) and copies
    ``PrivacyFenceApp.app`` out of it into ``dst_dir`` -- the "install" step,
    without writing to this runner's real ``/Applications``. Always detaches
    the mount on the way out, even on failure; the caller owns ``dst_dir``'s
    own lifecycle (this only ever writes into it, never removes it).

    A plain function, not a fixture, so more than one test can get its own
    independent copy of the bundle without sharing mutable state through a
    fixture -- see ``signed_app_copy`` and
    ``test_macos_upgrade_preserves_user_state`` below for why that
    independence matters here specifically."""
    dmg_path = _built_dmgs()[-1]
    mount_point = Path(tempfile.mkdtemp(prefix="pf-dmg-mount-"))
    subprocess.run(
        ["hdiutil", "attach", str(dmg_path), "-nobrowse", "-readonly", "-mountpoint", str(mount_point)],
        check=True, capture_output=True, text=True, timeout=60,
    )
    try:
        app_src = mount_point / "PrivacyFenceApp.app"
        assert app_src.is_dir(), f"PrivacyFenceApp.app missing from {dmg_path} (mounted at {mount_point})"
        app_dst = dst_dir / "PrivacyFenceApp.app"
        shutil.copytree(app_src, app_dst, symlinks=True)
        return app_dst
    finally:
        subprocess.run(
            ["hdiutil", "detach", str(mount_point), "-force"], capture_output=True, text=True, timeout=30,
        )
        shutil.rmtree(mount_point, ignore_errors=True)


@pytest.fixture(scope="module")
def installed_app() -> Path:
    """The shared copy of ``PrivacyFenceApp.app`` used by the primary
    round-trip test and (via ``signed_app_copy``'s own reasoning) nothing
    else -- module-scoped so the DMG is only ever mounted once per test
    session, not once per test."""
    install_dir = Path(tempfile.mkdtemp(prefix="pf-dmg-install-"))
    try:
        yield _copy_app_from_dmg(install_dir)
    finally:
        shutil.rmtree(install_dir, ignore_errors=True)


@dataclass
class RunningDaemon:
    process: subprocess.Popen
    home: Path
    base_url: str
    bootstrap_url: str
    web_token: str
    mcp_token: str

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"


def _wait_for_file(path: Path, proc: subprocess.Popen, log_path: Path, timeout: float = 30.0) -> str:
    """Polls for a file the daemon writes early in its own startup (the web/
    MCP token files, ``load_or_create_token()``/``web/mcp_auth.py``) --
    these are written before the server starts accepting connections, but
    poll rather than assume either is already flushed to disk the instant
    the socket answers. ``log_path`` (docs/automated-test-strategy-plan.md
    Phase 10) is the daemon's own redirected stdout/stderr, embedded in
    either failure message below -- same shape
    test_windows_packaged_smoke.py's identically-named helper already
    uses, and (unlike the in-memory buffer this replaced) still readable
    after the fact from wherever ``log_path`` lives under this test's own
    ``tmp_path``, not just from the exception message itself."""
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


@contextlib.contextmanager
def _running_daemon_at(exe: Path, home: Path):
    """Launches the real frozen daemon binary directly at ``exe`` -- not via
    ``open``/Finder, which is what would invoke Gatekeeper; a bundle built
    and run immediately on this same machine was never quarantined in the
    first place, so a direct exec is both sufficient and (see
    ``test_packaged_app_signature_and_notarization`` for the one place this
    module actually checks Gatekeeper's own verdict) the deliberately
    separate question. ``home`` is used as ``$HOME``
    (``paths.data_dir()`` resolves through ``Path.home()`` for a bundled
    app -- see paths.py); if ``settings.yaml`` doesn't already exist there
    it's seeded from ``SETTINGS_EXAMPLE``, and either way gets a real free
    port (so repeated boots -- one $HOME, two bundle versions -- never
    collide) and update checks disabled (this tier makes no real outbound
    network calls). Existing content otherwise survives untouched: this is
    what lets ``test_macos_upgrade_preserves_user_state`` boot the same
    ``$HOME`` twice, against two different bundle copies, and still find the
    first boot's state on the second.

    Mints its bootstrap URL via ``POST /api/bootstrap`` (SEC-06,
    web/server.py's ``_bootstrap_mint_route``) authorized by the persistent
    ``web_token`` read straight off disk, rather than scraping the daemon's
    own startup log line for one: SEC-10's ``SecretRedactingFormatter``
    (safe_errors.py) redacts any ``bootstrap=<value>`` substring out of
    every log line -- ``bootstrap`` is literally in its key-name allowlist
    -- so the one line that would otherwise carry it never actually does.
    Reading the raw persistent secret off disk and minting a fresh code
    through the same endpoint a human with only filesystem access (no live
    log line) would use is both correct in the same way and the only thing
    that actually works here. ``mcp_token`` is read the same way (no
    minting endpoint needed for it -- it's the daemon's own persistent MCP
    bearer token, web/mcp_auth.py), for callers that talk to ``/mcp``
    directly instead of through the real Node shim (the shim resolves its
    own copy from the same file, from inside the spawned process, so the
    primary round-trip test below never needs this field itself)."""
    assert exe.is_file(), f"{exe} missing -- PyInstaller output layout changed?"

    home.mkdir(parents=True, exist_ok=True)
    config_dir = home / ".privacyfence" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    settings_path = config_dir / "settings.yaml"
    if settings_path.exists():
        settings = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    else:
        settings = yaml.safe_load(SETTINGS_EXAMPLE.read_text(encoding="utf-8")) or {}
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    settings.setdefault("web", {})["port"] = port
    settings.setdefault("update_check", {})["enabled"] = False
    settings_path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    base_url = f"http://localhost:{port}"  # WebServer.base_url's own construction, host defaults to "localhost"

    env = {**os.environ, "HOME": str(home)}
    # Redirected straight to a file under `home` (docs/automated-test-
    # strategy-plan.md Phase 10), not `subprocess.PIPE` read on a background
    # thread -- the same daemon.log convention every other packaged/system
    # module in this repo already uses, which is what lets Phase 10's own
    # tests/diagnostics.py find and capture it on a failing test without
    # this module needing any capture code of its own.
    log_path = home / "daemon.log"
    log_fh = open(log_path, "wb")
    proc = subprocess.Popen([str(exe)], env=env, stdout=log_fh, stderr=subprocess.STDOUT)

    try:
        _wait_until_connectable("localhost", port)

        data_dir = home / ".privacyfence"
        web_token = _wait_for_file(data_dir / WEB_TOKEN_FILE_NAME, proc, log_path)
        mcp_token = _wait_for_file(data_dir / MCP_TOKEN_FILE_NAME, proc, log_path)

        resp = httpx.post(
            f"{base_url}/api/bootstrap", headers={"Authorization": f"Bearer {web_token}"}, timeout=10,
        )
        resp.raise_for_status()
        bootstrap_url = f"{base_url}/approvals?bootstrap={resp.json()['bootstrap']}"

        yield RunningDaemon(
            process=proc, home=home, base_url=base_url, bootstrap_url=bootstrap_url,
            web_token=web_token, mcp_token=mcp_token,
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log_fh.close()


@pytest.fixture
def running_packaged_daemon(installed_app, tmp_path):
    """The primary round-trip test's own daemon: ``installed_app``'s exe, a
    fresh scratch ``$HOME`` per test. Thin wrapper around
    ``_running_daemon_at`` -- see that function's own docstring for the
    actual mechanism. ``home`` lives under this test's own ``tmp_path``
    (docs/automated-test-strategy-plan.md Phase 10), not a bare
    ``tempfile.mkdtemp()`` this fixture used to manually ``shutil.rmtree()``
    on the way out -- pytest already owns ``tmp_path``'s own lifecycle
    (rotated, not deleted immediately), which is what lets a failing test's
    ``daemon.log`` still be there afterward for tests/diagnostics.py to
    capture, same as ``test_macos_upgrade_preserves_user_state``'s own
    ``home`` already does."""
    exe = installed_app / "Contents" / "MacOS" / "PrivacyFenceApp"
    home = tmp_path / "home"
    with _running_daemon_at(exe, home) as daemon:
        yield daemon


@pytest.fixture(scope="module")
def built_shim_entry() -> Path:
    """(Re)builds mcpb/shim/dist/shim.js once per module -- identical
    reasoning to test_shim_mcp_contract.py's own fixture of the same name
    (not imported from there: every contract test in this directory stays
    independently runnable, per that module's own precedent)."""
    if shutil.which("npm") is None:
        pytest.skip("npm not on PATH -- this fixture builds the shim via `npm install`/`npm run build`")
    try:
        subprocess.run(
            ["npm", "install", "--silent"], cwd=SHIM_DIR, check=True, capture_output=True, timeout=180,
        )
        subprocess.run(
            ["npm", "run", "build", "--silent"], cwd=SHIM_DIR, check=True, capture_output=True, timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"could not build mcpb/shim/dist/shim.js: {exc}")
    if not SHIM_ENTRY.exists():
        pytest.skip(f"{SHIM_ENTRY} missing after build")
    return SHIM_ENTRY


def _confirm_pending_rule_change(bootstrap_url: str) -> None:
    """Runs entirely on its own thread (see the test below) -- Playwright's
    sync API must never be invoked from a thread with a running asyncio
    event loop, which is exactly the thread the test's own ``await``s run
    on (see test_browser_smoke.py's ``browser`` fixture docstring for the
    same constraint from the other direction). Opens the real bootstrap
    link, follows the one pending confirmation card to its own page, and
    clicks "Confirm" -- the same real-DOM click
    test_browser_smoke.py's TestApprovalDecisionFlow proves against the
    injected bridge shim (web/routes_approvals.py's ``_bridge_shim``),
    exercised here against the actual packaged app instead of a dev
    WebServer."""
    parts = urlsplit(bootstrap_url)
    base_url = f"{parts.scheme}://{parts.netloc}"
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except PlaywrightError as exc:
            pytest.skip(f"Chromium not available for Playwright ({exc}) -- run `playwright install chromium`")
            return
        try:
            page = browser.new_page()
            page.goto(bootstrap_url)
            page.wait_for_load_state("load")
            assert page.url == f"{base_url}/approvals", f"bootstrap sign-in landed on {page.url!r}"

            row = page.wait_for_selector("[data-approval-id]", timeout=60_000)
            approval_id = row.get_attribute("data-approval-id")
            page.click(f'[data-approval-id="{approval_id}"] a.pf-btn-review')
            page.wait_for_url(f"{base_url}/approvals/{approval_id}")
            # The card's own JS only clears `aria-disabled` (what its click
            # handler actually gates on -- dialog_window_html.py's `_JS`)
            # once DOMContentLoaded fires; Playwright's click-actionability
            # checks the real `disabled` DOM property, not this ARIA
            # attribute, so a click issued before that would silently no-op
            # the same way test_browser_smoke.py's own card-page flow avoids
            # by waiting for "load" first.
            page.wait_for_load_state("load")
            page.locator('[data-pf-action="confirm"]').click()
            page.wait_for_url(f"{base_url}/approvals")
        finally:
            browser.close()


async def test_packaged_app_connects_over_mcp_and_completes_an_approval_round_trip(
    running_packaged_daemon, built_shim_entry, installed_app,
):
    """The one end-to-end assertion this whole module exists for: the real
    ``.mcpb`` shim, talking to the real packaged daemon started from the
    real DMG, round-trips a gated meta-tool call through a real human
    decision made by clicking a real button in a real browser."""
    params = StdioServerParameters(
        command="node", args=[str(built_shim_entry)], env={"HOME": str(running_packaged_daemon.home)},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert "privacyfence_propose_auto_accept_rule_change" in names
            assert "privacyfence_check_policy" in names

            call_task = asyncio.create_task(
                session.call_tool(
                    "privacyfence_propose_auto_accept_rule_change",
                    {
                        "target": "rule",
                        "operation": "add",
                        "operation_key": "gmail.read_message",
                        "rule_name": "trusted_sender_domain",
                        "value": ["example.com"],
                        "reason": "TST-15 packaged-app smoke test synthetic approval round trip",
                    },
                )
            )
            # Runs on a worker thread so its own (Playwright-internal) event
            # loop never collides with this coroutine's -- see
            # _confirm_pending_rule_change's own docstring.
            await asyncio.to_thread(_confirm_pending_rule_change, running_packaged_daemon.bootstrap_url)
            result = await call_task

    assert result.isError is not True, getattr(result, "content", result)
    assert result.structuredContent is not None
    assert result.structuredContent["confirmed"] is True
    assert result.structuredContent["changed"] is True
    assert "trusted_sender_domain" in result.structuredContent["description"]

    # Confirms the round trip actually reached persisted state, not just a
    # confirmed-but-inert in-memory result.
    settings_path = running_packaged_daemon.home / ".privacyfence" / "config" / "settings.yaml"
    assert "trusted_sender_domain" in settings_path.read_text(encoding="utf-8")

    # ── State lives outside the package (module docstring, §6) ───────────
    # macOS has no installer/uninstaller pair -- "uninstalling" is deleting
    # the .app bundle, same as dragging it to the Trash. That must never
    # touch $HOME/.privacyfence, the same "user state survives package
    # removal" property test_deb_packaged_lifecycle.py's `dpkg -r`/`dpkg -P`
    # and test_windows_packaged_smoke.py's silent uninstall each assert for
    # their own platform's removal gesture. The daemon process is still
    # running from this bundle's own binary at this point -- removing the
    # files out from under an already-exec'd process is ordinary POSIX
    # unlink semantics, not an error, on macOS. This deletes the shared
    # `installed_app` fixture's own copy, not a throwaway one -- deliberately,
    # to prove deletion of the *exact* running bundle is safe -- which is
    # exactly why `test_packaged_app_signature_and_notarization` below no
    # longer depends on `installed_app` still existing afterward (see that
    # test's own `signed_app_copy` fixture).
    shutil.rmtree(installed_app)
    assert not installed_app.exists()
    assert settings_path.exists(), "deleting PrivacyFenceApp.app must never touch $HOME/.privacyfence state"
    assert "trusted_sender_domain" in settings_path.read_text(encoding="utf-8")


@pytest.fixture
def signed_app_copy() -> Path:
    """A private copy of the extracted bundle, independent of the shared
    module-scoped ``installed_app`` fixture: the primary round-trip test
    above deletes *its* copy of ``installed_app`` partway through its own
    run (module docstring §6) to prove package removal never touches
    ``$HOME`` -- since pytest runs test functions in file-definition order
    by default, a signature test that depended on that same shared fixture
    would silently find nothing there afterward (``codesign`` against a
    missing path exits non-zero the same way an unsigned bundle does,
    which this test's own skip-on-nonzero logic can't tell apart from the
    real "unsigned" case). A dedicated copy sidesteps the ordering
    question entirely rather than depending on it."""
    install_dir = Path(tempfile.mkdtemp(prefix="pf-dmg-signtest-"))
    try:
        yield _copy_app_from_dmg(install_dir)
    finally:
        shutil.rmtree(install_dir, ignore_errors=True)


def test_packaged_app_signature_and_notarization(signed_app_copy):
    """§7 of the module docstring -- distinct from the MCP/approval round
    trip above, and deliberately its own test so a signature failure and an
    approval-protocol failure are never conflated in one report.

    ``scripts/build_dmg.sh``'s own ``--sign``/``NOTARIZE_PROFILE`` are both
    optional (a local dev build with no Developer ID identity is a
    legitimate, common case -- see that script's step 5/8 comments), so this
    skips outright, rather than failing, when the artifact under test wasn't
    built with them. ``build.yml``'s real release job always sets both (see
    its "Build .app and DMG" step), so this asserts the real chain there.
    """
    identify = subprocess.run(
        ["codesign", "-dv", "--verbose=4", str(signed_app_copy)],
        capture_output=True, text=True,
    )
    # codesign writes its `-d` info to stderr; an entirely unsigned bundle
    # exits 1 with "code object is not signed at all".
    if identify.returncode != 0:
        pytest.skip(
            f"PrivacyFenceApp.app is unsigned for this build (SIGN_IDENTITY unset) -- "
            f"run scripts/build_dmg.sh --sign '<identity>' to exercise this test: {identify.stderr}"
        )
    assert "Authority=Developer ID Application" in identify.stderr, (
        f"expected a real Developer ID signature, not an ad-hoc one:\n{identify.stderr}"
    )

    verify = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(signed_app_copy)],
        capture_output=True, text=True,
    )
    assert verify.returncode == 0, f"codesign --verify --deep --strict failed:\n{verify.stderr}"

    # Gatekeeper's own policy engine, asked the same question it asks on a
    # real first launch, without actually invoking the interactive
    # "are you sure you want to open this" dialog a human sees (that part
    # of the story stays manual, per this plan's own governing rule).
    assess = subprocess.run(
        ["spctl", "--assess", "--type", "execute", "--verbose", str(signed_app_copy)],
        capture_output=True, text=True,
    )
    assert assess.returncode == 0, f"Gatekeeper would reject this app (spctl --assess):\n{assess.stdout}{assess.stderr}"

    # Notarization is checked against the DMG itself (what ships), not the
    # extracted bundle -- `xcrun stapler staple` (build_dmg.sh step 8)
    # staples the ticket to the disk image. A signed-but-not-notarized local
    # build (NOTARIZE_PROFILE unset) is also legitimate and must not fail
    # this test -- `spctl`'s own "source=" line is what actually says
    # whether Apple's notarization ticket was found and accepted, without
    # this test needing network access to ask Apple directly the way
    # `xcrun stapler validate` can fall back to doing.
    dmg_path = _built_dmgs()[-1]
    dmg_assess = subprocess.run(
        ["spctl", "--assess", "--type", "open", "--context", "context:primary-signature", "--verbose", str(dmg_path)],
        capture_output=True, text=True,
    )
    dmg_assess_output = dmg_assess.stdout + dmg_assess.stderr
    if "source=Notarized Developer ID" not in dmg_assess_output:
        pytest.skip(
            f"{dmg_path} is signed but not notarized for this build (NOTARIZE_PROFILE unset): "
            f"{dmg_assess_output}"
        )
    assert dmg_assess.returncode == 0, f"spctl rejected the notarized DMG outright:\n{dmg_assess_output}"


# --------------------------------------------------------------------------- #
# Upgrade in place preserves user state (Phase 6 item 20)
# --------------------------------------------------------------------------- #

def _bump_bundle_version(app_path: Path) -> str:
    """Rewrites ``Contents/Info.plist``'s ``CFBundleShortVersionString`` to a
    value guaranteed different from the original -- the macOS analogue of
    ``test_deb_packaged_lifecycle.py``'s ``_synthetic_next_version_deb`` and
    ``test_windows_packaged_smoke.py``'s ``_synthetic_next_version_
    installer``: same bytes, just relabeled, since what this test needs
    proven (``$HOME`` survives a reinstall) doesn't depend on anything
    actually changing *inside* the bundle. Unlike either of those platforms'
    installers, macOS has no version-ordering (or any other) gate an
    "install" has to satisfy at all -- per the module docstring's own §6,
    "installing" a new version here is just replacing the ``.app`` bundle
    wholesale -- so any different string is enough to prove this is a
    distinct bundle rather than the identical one being re-applied."""
    info_plist_path = app_path / "Contents" / "Info.plist"
    with open(info_plist_path, "rb") as f:
        info = plistlib.load(f)
    new_version = f"{info['CFBundleShortVersionString']}+upgradetest1"
    info["CFBundleShortVersionString"] = new_version
    with open(info_plist_path, "wb") as f:
        plistlib.dump(info, f)
    return new_version


async def _propose_trusted_sender_rule(mcp_url: str, mcp_token: str, *, value: list[str]):
    """Applies a real auto-accept rule change over the daemon's own ``/mcp``
    Streamable HTTP endpoint directly -- unlike the primary round-trip test
    above, this doesn't go through the real Node shim or a real browser
    click: that specific substitution (direct MCP client + a raw HTTP POST
    to the decide route, see ``_resolve_pending_card`` below) is the same
    one ``test_deb_packaged_lifecycle.py``/``test_windows_packaged_smoke.py``
    already make for their own scenarios, for the same reason -- the shim
    artifact itself is already proven by the primary round-trip test in
    this module; this test's own job is proving state survival across a
    bundle swap, not re-proving the shim a second time."""
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
                        "reason": "tests/integration/test_macos_packaged_smoke.py upgrade-in-place scenario",
                    },
                )


async def _resolve_pending_card(base_url: str, bootstrap_url: str) -> None:
    """Bootstraps a real session cookie via the real ``?bootstrap=`` exchange
    (same endpoint ``_running_daemon_at`` itself already minted the code
    for), polls ``/approvals`` for the one pending card the concurrently-
    running MCP call above just opened, then resolves it via a direct HTTP
    POST to the real decide route -- ``test_deb_packaged_lifecycle.py``'s
    own helper of the same name, adapted to a bootstrap *URL* (this module's
    own shape) rather than a bare token."""
    async with httpx.AsyncClient(base_url=base_url, follow_redirects=True) as web_client:
        exchange = await web_client.get(bootstrap_url)
        assert exchange.status_code == 200, exchange.text
        session_id = web_client.cookies.get("pf_session")
        assert session_id, "bootstrap exchange did not set a pf_session cookie"

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
            f"/api/approvals/{approval_id}/decide", json={"result": "confirm", "csrf": session_id},
        )
        assert decide_resp.status_code == 200, decide_resp.text
        assert decide_resp.json() == {"status": "ok"}


@pytest.mark.timeout(300)   # builds a second bundle copy *and* boots the daemon twice -- the
                             # module's default timeout=300 already assumes one boot plus a real
                             # browser round trip, so this needs the same headroom for the second one.
async def test_macos_upgrade_preserves_user_state(tmp_path):
    """Phase 6 item 20 -- deliberately not built in the same PR as this
    module's own 6.1 gap-closing work: install version N, use it to create
    real on-disk state (an applied auto-accept rule, via the real MCP round
    trip -- not a hand-written settings.yaml), replace it with a
    synthetically-relabeled version N+1 at the same ``$HOME``, and confirm
    the state survived and the new bundle still starts and serves. Own,
    independent bundle copies throughout (``_copy_app_from_dmg``) -- see
    ``signed_app_copy``'s own docstring for why this module doesn't share
    mutable fixture state like that across tests.
    """
    install_dir = Path(tempfile.mkdtemp(prefix="pf-dmg-upgrade-"))
    home = tmp_path / "home"
    try:
        # ── Install version N; create real on-disk state through the real
        # MCP/approval round trip ─────────────────────────────────────────
        app_n = _copy_app_from_dmg(install_dir)
        exe_n = app_n / "Contents" / "MacOS" / "PrivacyFenceApp"
        with _running_daemon_at(exe_n, home) as daemon:
            propose_task = asyncio.create_task(
                _propose_trusted_sender_rule(daemon.mcp_url, daemon.mcp_token, value=["preupgrade.example.com"])
            )
            await _resolve_pending_card(daemon.base_url, daemon.bootstrap_url)
            result = await propose_task
            assert result.isError is not True, getattr(result, "content", result)
            assert result.structuredContent["changed"] is True

        settings_path = home / ".privacyfence" / "config" / "settings.yaml"
        assert "preupgrade.example.com" in settings_path.read_text(encoding="utf-8")

        # ── "Install" a synthetically-relabeled version N+1 -- delete the
        # old bundle and put a fresh copy in its place (module docstring
        # §6: this is the macOS install/uninstall gesture; a real second
        # PyInstaller build would multiply this module's already-heavy
        # setup cost for no additional coverage -- see
        # _bump_bundle_version's own docstring) ───────────────────────────
        shutil.rmtree(app_n)
        app_n1 = _copy_app_from_dmg(install_dir)
        new_version = _bump_bundle_version(app_n1)
        exe_n1 = app_n1 / "Contents" / "MacOS" / "PrivacyFenceApp"

        with open(app_n1 / "Contents" / "Info.plist", "rb") as f:
            info = plistlib.load(f)
        assert info["CFBundleShortVersionString"] == new_version

        # ── State survived the upgrade untouched ──────────────────────────
        assert "preupgrade.example.com" in settings_path.read_text(encoding="utf-8")

        # ── The upgraded bundle still starts and serves, without
        # clobbering the state it just inherited ─────────────────────────
        with _running_daemon_at(exe_n1, home) as daemon:
            resp = httpx.get(daemon.bootstrap_url, follow_redirects=True, timeout=10)
            assert resp.status_code == 200, resp.text

        assert "preupgrade.example.com" in settings_path.read_text(encoding="utf-8")
    finally:
        shutil.rmtree(install_dir, ignore_errors=True)
