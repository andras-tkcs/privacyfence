"""Release-workflow smoke test against the actual packaged macOS artifact
(docs/security-remediation-plan.md, Phase 3 item 3.10, TST-15).

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

Skipped entirely unless running on real macOS with a just-built DMG on disk
(this only makes sense as a step in ``.github/workflows/build.yml``'s
``build`` job, right after ``scripts/build_dmg.sh`` -- see that workflow's
"Run packaged smoke test" step) and Node/the ``mcp``/``playwright`` test
extras available -- never runs as part of the ordinary ``pytest`` invocation
in tests.yml's ubuntu-latest job.
"""
from __future__ import annotations

import asyncio
import os
import platform
import shutil
import socket
import subprocess
import tempfile
import threading
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


@pytest.fixture(scope="module")
def installed_app() -> Path:
    """Mounts the just-built DMG and copies ``PrivacyFenceApp.app`` out of
    it into a scratch directory -- the "install" step, without writing to
    this runner's real ``/Applications``."""
    dmg_path = _built_dmgs()[-1]
    mount_point = Path(tempfile.mkdtemp(prefix="pf-dmg-mount-"))
    install_dir = Path(tempfile.mkdtemp(prefix="pf-dmg-install-"))
    subprocess.run(
        ["hdiutil", "attach", str(dmg_path), "-nobrowse", "-readonly", "-mountpoint", str(mount_point)],
        check=True, capture_output=True, text=True, timeout=60,
    )
    try:
        app_src = mount_point / "PrivacyFenceApp.app"
        assert app_src.is_dir(), f"PrivacyFenceApp.app missing from {dmg_path} (mounted at {mount_point})"
        app_dst = install_dir / "PrivacyFenceApp.app"
        shutil.copytree(app_src, app_dst, symlinks=True)
        yield app_dst
    finally:
        subprocess.run(
            ["hdiutil", "detach", str(mount_point), "-force"], capture_output=True, text=True, timeout=30,
        )
        shutil.rmtree(mount_point, ignore_errors=True)
        shutil.rmtree(install_dir, ignore_errors=True)


@dataclass
class RunningDaemon:
    process: subprocess.Popen
    home: Path
    bootstrap_url: str


@pytest.fixture
def running_packaged_daemon(installed_app):
    """Launches the real frozen daemon binary directly -- not via ``open``/
    Finder, which is what would invoke Gatekeeper; a bundle built and run
    immediately on this same machine was never quarantined in the first
    place, so a direct exec is both sufficient. ``$HOME`` is an isolated
    scratch directory (``paths.data_dir()`` resolves through
    ``Path.home()`` for a bundled app -- see paths.py), pre-seeded with a
    settings.yaml pinned to a free port so this doesn't collide with
    anything already using the real default (8765).

    Mints its bootstrap URL via ``POST /api/bootstrap`` (SEC-06,
    web/server.py's ``_bootstrap_mint_route``) authorized by the persistent
    ``web_token`` read straight off disk, rather than scraping the
    daemon's own startup log line for one: SEC-10's
    ``SecretRedactingFormatter`` (safe_errors.py) redacts any
    ``bootstrap=<value>`` substring out of every log line -- ``bootstrap``
    is literally in its key-name allowlist -- so the one line that would
    otherwise carry it never actually does. Reading the raw persistent
    secret off disk and minting a fresh code through the same endpoint a
    human with only filesystem access (no live log line) would use is both
    correct in the same way and the only thing that actually works here.
    """
    exe = installed_app / "Contents" / "MacOS" / "PrivacyFenceApp"
    assert exe.is_file(), f"{exe} missing -- PyInstaller output layout changed?"

    home = Path(tempfile.mkdtemp(prefix="pf-smoke-home-"))
    config_dir = home / ".privacyfence" / "config"
    config_dir.mkdir(parents=True)
    settings = yaml.safe_load(SETTINGS_EXAMPLE.read_text(encoding="utf-8")) or {}
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    settings.setdefault("web", {})["port"] = port
    (config_dir / "settings.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")
    base_url = f"http://localhost:{port}"  # WebServer.base_url's own construction, host defaults to "localhost"

    env = {**os.environ, "HOME": str(home)}
    proc = subprocess.Popen(
        [str(exe)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    output: list[str] = []
    threading.Thread(target=lambda: output.extend(iter(proc.stdout.readline, "")), daemon=True).start()

    try:
        _wait_until_connectable("localhost", port)

        # load_or_create_token() (web/server.py) writes this before the
        # server starts accepting connections, but poll rather than assume
        # it's already flushed to disk the instant the socket answers.
        token_path = home / ".privacyfence" / WEB_TOKEN_FILE_NAME
        deadline = time.monotonic() + 30
        while not token_path.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert token_path.exists(), (
            f"{token_path} never appeared (daemon exit code {proc.poll()}):\n" + "".join(output)
        )
        web_token = token_path.read_text(encoding="utf-8").strip()

        resp = httpx.post(
            f"{base_url}/api/bootstrap", headers={"Authorization": f"Bearer {web_token}"}, timeout=10,
        )
        resp.raise_for_status()
        bootstrap_url = f"{base_url}/approvals?bootstrap={resp.json()['bootstrap']}"

        yield RunningDaemon(process=proc, home=home, bootstrap_url=bootstrap_url)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        shutil.rmtree(home, ignore_errors=True)


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
    running_packaged_daemon, built_shim_entry,
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
