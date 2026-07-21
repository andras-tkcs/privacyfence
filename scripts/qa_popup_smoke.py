#!/usr/bin/env python3
"""Local-only smoke test for approval_window.py's real modal loop: does a
real click on a real, on-screen "Allow once" / "Deny" / "Always allow" /
"Allow for 5 min" button actually resolve show_native_approval() to the
value its docstring promises?

tests/unit/test_approval_window.py already builds the real AppKit view tree
for every popup shape and asserts on its content -- buttons, PII
tint/banner, summary rows, details text -- without ever calling
runApproval_() or NSApplication.runModalForWindow_(). This script is the
one thing that coverage deliberately leaves untested: the modal loop itself
actually blocking, and a real click actually reaching it. That's exactly
the class of failure those construction-only tests can't catch (e.g. the
modal loop wired to the wrong window, or a button whose target/action never
actually fires).

This is NOT a pytest test and NEVER runs in CI:
  - It requires macOS (real AppKit — approval_window.py has no other
    implementation) and Accessibility permission granted to whatever
    process runs it (Terminal, an IDE, ...), since it drives a real click
    via `System Events`. Granting that to a hosted CI runner isn't
    something this project's tests.yml does, and isn't worth doing for a
    failure mode this narrow.
  - It pops real, visible windows on your screen for a couple of seconds
    each while it runs — run it locally, not headless.

Run it whenever approval_window.py's modal-loop plumbing changes
(build_panel() itself, i.e. everything about window *content*, is already
covered by test_approval_window.py on every PR and doesn't need this).
Paste the printed report into the PR description under a "## Popup smoke
check" heading -- see docs/testing-policy.md §2.2.

_scenarios() has one entry per tool in docs/approval-window-content-reference.md's RG-1/RG-2/
RG-3/RG-4/WG-1/WG-2 tables (62 tools total, including every RG-1 tool sharing a dialog shape,
e.g. confluence_get_page/confluence_get_page_by_title) -- every dialog shape that doc documents
gets a real on-screen click, not just a representative handful. Preview/details data is
realistic-but-synthetic, sourced from tests/fixtures/live/*/*.json (recorded, redacted real API
responses -- see scripts/qa_fixture_recorder.py) and docs/qa-environment-setup.md's own PFQA/
[QATEST] naming conventions, rather than generic placeholder strings -- see that doc's "one rule
this doc follows wherever it creates content" for why identity fields can look like a real
project/folder name but content never is. Cross-cutting mechanics this reference doc calls
"automatic on every group" (Deny, Always allow, Allow for 5 min, the PII/content-flag banners,
the visibility checklist, seen-count + Claude's reason together, progressive disclosure, the
Gmail-style header, native PDFView) are folded into specific tool scenarios rather than kept as
separate generic ones -- see the inline comment at each such scenario in _scenarios().

One more, non-tool scenario runs last: the actual menu bar status item and, from it, the "Manage
Auto-accept Rules…" window (see _run_menu_bar_scenario's docstring) -- the menu bar redesign (PR
#60) has the same "real click actually reaching it" gap the rest of this script covers for
approval popups, just never exercised end to end before now.

Usage (the project's own venv, not a bare system python3 -- this needs the
same pyobjc/AppKit packages the app itself depends on, which only the venv
has installed):
    .venv/bin/python scripts/qa_popup_smoke.py
    .venv/bin/python scripts/qa_popup_smoke.py --report-file /tmp/popup_smoke.md
    .venv/bin/python scripts/qa_popup_smoke.py --pause-seconds 3   # slow down to actually look
    .venv/bin/python scripts/qa_popup_smoke.py --screenshot-dir /tmp/popup_smoke_shots
    # One scenario only, e.g. to refresh a single README.md screenshot -- the three screenshots
    # README.md actually uses (as of this writing) come from these three scenario names, one
    # popup-gate, one review-gate, one menu-bar:
    .venv/bin/python scripts/qa_popup_smoke.py --scenario "gmail_get_thread" \\
        --screenshot-dir docs/images/screenshots --pause-seconds 3
    .venv/bin/python scripts/qa_popup_smoke.py --scenario "drive_sheets_write_range" \\
        --screenshot-dir docs/images/screenshots --pause-seconds 3
    .venv/bin/python scripts/qa_popup_smoke.py --scenario "Menu bar" \\
        --screenshot-dir docs/images/screenshots --pause-seconds 3
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

if sys.platform != "darwin":
    print(
        "qa_popup_smoke.py requires macOS (real AppKit) -- nothing to run on this platform.",
        file=sys.stderr,
    )
    sys.exit(1)

import Quartz  # noqa: E402
import rumps  # noqa: E402
from AppKit import (  # noqa: E402
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSApplicationActivationPolicyProhibited,
    NSBitmapImageRep,
    NSPNGFileType,
    NSStatusBar,
)
from PyObjCTools import AppHelper  # noqa: E402
from rumps import rumps as _rumps_internal  # noqa: E402

from privacyfence import menu_bar  # noqa: E402
from privacyfence.approval_window import show_native_approval  # noqa: E402

WINDOW_WAIT_TIMEOUT_SECONDS = 8.0


@dataclass
class ScenarioResult:
    name: str
    button_clicked: str
    expected: str
    actual: str | None
    click_status: str  # "clicked" | "TIMEOUT_NO_WINDOW" | "BUTTON_NOT_FOUND" | an osascript error

    @property
    def passed(self) -> bool:
        return self.click_status == "clicked" and self.actual == self.expected


def _run_applescript(script: str) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".applescript", delete=False, encoding="utf-8") as f:
        f.write(script)
        fname = f.name
    try:
        result = subprocess.run(
            ["osascript", fname], capture_output=True, text=True, timeout=WINDOW_WAIT_TIMEOUT_SECONDS + 5,
        )
        out = result.stdout.strip()
        if result.returncode != 0:
            return f"osascript error: {result.stderr.strip() or out}"
        return out
    except subprocess.TimeoutExpired:
        return "osascript timed out"
    finally:
        try:
            os.unlink(fname)
        except OSError:
            pass


def _wait_for_window(pid: int) -> str:
    """Block until our own process's first window appears -- returns "ready",
    or "TIMEOUT_NO_WINDOW" if it never appeared within
    WINDOW_WAIT_TIMEOUT_SECONDS.

    Split out from _click_button() (which used to poll and click in one
    osascript call) so a screenshot can be taken in between: after the
    window exists but before the click that may dismiss it.

    Targets the process by unix id, not by name -- a plain `python3
    scripts/...` invocation's process name varies by how Python itself was
    installed/framework-built, but its pid is unambiguous.
    """
    script = f'''
    tell application "System Events"
        set targetProcess to first process whose unix id is {pid}
        set deadlineTime to (current date) + {WINDOW_WAIT_TIMEOUT_SECONDS}
        repeat
            if (exists window 1 of targetProcess) then return "ready"
            if (current date) > deadlineTime then
                return "TIMEOUT_NO_WINDOW"
            end if
            delay 0.1
        end repeat
    end tell
    '''
    return _run_applescript(script)


def _click_button(pid: int, title: str) -> str:
    """Click a button on our own process's first window by its exact title
    -- returns "clicked", "BUTTON_NOT_FOUND" (the window has no button with
    this exact title -- e.g. the button set didn't match what the scenario
    expected), or an osascript-level error string. Assumes the window
    already exists (call _wait_for_window() first).
    """
    script = f'''
    tell application "System Events"
        set targetProcess to first process whose unix id is {pid}
        tell targetProcess
            if not (exists button "{title}" of window 1) then
                return "BUTTON_NOT_FOUND"
            end if
            click button "{title}" of window 1
        end tell
    end tell
    return "clicked"
    '''
    return _run_applescript(script)


def _click_menu_bar_icon(pid: int) -> str:
    """Click our own process's (one and only) menu bar status item --
    returns "clicked", "TIMEOUT_NO_STATUS_ITEM" if it never appeared within
    WINDOW_WAIT_TIMEOUT_SECONDS, or an osascript-level error string.

    "menu bar 2" is System Events' name for a process's status-bar extras,
    distinct from "menu bar 1" (the app's own File/Edit/... menu bar, which
    an accessory-policy app like this one doesn't have) -- the same
    real-click-via-System-Events approach _click_button uses for approval
    windows, just targeting the status item instead of a window button.
    Clicking it opens its menu the same way a real user click would; no
    separate "open the menu" call exists or is needed.
    """
    script = f'''
    tell application "System Events"
        set targetProcess to first process whose unix id is {pid}
        set deadlineTime to (current date) + {WINDOW_WAIT_TIMEOUT_SECONDS}
        repeat
            if (exists menu bar item 1 of menu bar 2 of targetProcess) then exit repeat
            if (current date) > deadlineTime then return "TIMEOUT_NO_STATUS_ITEM"
            delay 0.1
        end repeat
        click menu bar item 1 of menu bar 2 of targetProcess
    end tell
    return "clicked"
    '''
    return _run_applescript(script)


def _click_menu_item(pid: int, title: str) -> str:
    """Click an item by exact title in our own process's open status-item
    menu (call _click_menu_bar_icon() first to actually open it) -- returns
    "clicked", "MENU_ITEM_NOT_FOUND", or an osascript-level error string.
    Clicking a real menu item ends that menu's tracking session the same
    way a real user's click would -- no separate "close the menu" step
    exists or is needed, mirroring _click_button's resolve-by-clicking
    contract for approval windows.
    """
    script = f'''
    tell application "System Events"
        set targetProcess to first process whose unix id is {pid}
        tell targetProcess
            if not (exists menu item "{title}" of menu 1 of menu bar item 1 of menu bar 2) then
                return "MENU_ITEM_NOT_FOUND"
            end if
            click menu item "{title}" of menu 1 of menu bar item 1 of menu bar 2
        end tell
    end tell
    return "clicked"
    '''
    return _run_applescript(script)


_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _slugify(name: str) -> str:
    return _SLUG_RE.sub("_", name).strip("_")


def _screenshot_own_window(pid: int, path: Path) -> bool:
    """Screenshot the first on-screen window owned by our own process (there's
    only ever one at a time -- show_native_approval()'s modal loop means
    scenarios never overlap) and write it to `path` as a PNG. Returns whether
    a window was found and captured; a miss isn't treated as a scenario
    failure, it's a photo opportunity that arrived too late for this run's
    window.

    No extra macOS permission needed: the Screen Recording permission gate
    only applies to capturing *other* processes' windows, not your own.

    kCGWindowImageBoundsIgnoreFraming is required, not kCGWindowImageDefault
    -- the default pads the captured image with the window's drop shadow, at
    a size that doesn't even scale cleanly with the window's actual point
    size (verified empirically: a 300x178pt window came back as 824x580px
    with the default option, vs. a clean 600x356px -- exactly 2x retina --
    with this one).
    """
    window_list = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
    )
    window_id = next(
        (w["kCGWindowNumber"] for w in window_list if w.get("kCGWindowOwnerPID") == pid), None
    )
    if window_id is None:
        return False
    image = Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow, window_id,
        Quartz.kCGWindowImageBoundsIgnoreFraming,
    )
    if image is None:
        return False
    bitmap = NSBitmapImageRep.alloc().initWithCGImage_(image)
    png_data = bitmap.representationUsingType_properties_(NSPNGFileType, None)
    return bool(png_data.writeToFile_atomically_(str(path), True))


def _run_scenario(
    name: str, *, click_title: str, expected: str, pre_click_title: str | None = None,
    pause_seconds: float = 0.3, screenshot_dir: Path | None = None, **popup_kwargs
) -> ScenarioResult:
    pid = os.getpid()
    click_status_box: list[str] = []

    def clicker() -> None:
        # Fired from a background thread, same as the click has to happen
        # from a different thread than the one show_native_approval() will
        # block on below (the AppKit modal loop). A head start (0.3s by
        # default, --pause-seconds to look before each click) lets the
        # window actually get created before System Events starts polling
        # for it -- and, at a larger value, gives a human time to actually
        # look at what's on screen before it's clicked away.
        time.sleep(pause_seconds)
        wait_status = _wait_for_window(pid)
        if wait_status != "ready":
            click_status_box.append(wait_status)
            return
        if screenshot_dir is not None:
            # Taken as the popup first appears, before any click -- for a
            # pre_click_title scenario that's the collapsed ("Show more"
            # not yet clicked) state, not the expanded one.
            _screenshot_own_window(pid, screenshot_dir / f"{_slugify(name)}.png")
        if pre_click_title is not None:
            # A non-terminal click (e.g. "Show more") that must NOT resolve
            # the modal loop -- if it did, the final click below would hit
            # BUTTON_NOT_FOUND/TIMEOUT_NO_WINDOW against an already-closed
            # window, which is exactly the failure mode this catches.
            pre_status = _click_button(pid, pre_click_title)
            if pre_status != "clicked":
                click_status_box.append(f"pre-click {pre_click_title!r} failed: {pre_status}")
                return
            time.sleep(pause_seconds)
        click_status_box.append(_click_button(pid, click_title))

    clicker_thread = threading.Thread(target=clicker, daemon=True)
    clicker_thread.start()

    actual = show_native_approval(**popup_kwargs)

    # Two sleeps happen before a click lands on a pre_click_title scenario
    # (pre-click, then the final click), so the join timeout has to cover
    # both, not just one -- otherwise a large --pause-seconds would make
    # this time out while the clicker thread is still legitimately waiting.
    sleeps = 2 if pre_click_title is not None else 1
    clicker_thread.join(timeout=sleeps * pause_seconds + WINDOW_WAIT_TIMEOUT_SECONDS + 5)
    click_status = click_status_box[0] if click_status_box else "clicker thread never finished"
    return ScenarioResult(
        name=name, button_clicked=click_title, expected=expected, actual=actual, click_status=click_status,
    )


# ------------------------------------------------------------------------ #
# Realistic-but-synthetic identity data, sourced from tests/fixtures/live/*/*.json (recorded,
# redacted real API responses -- see scripts/qa_fixture_recorder.py and that directory's own
# README) and docs/qa-environment-setup.md's own PFQA/[QATEST] conventions. Never copied from a
# real message/contact/event; PFQA-prefixed names identify which real project/folder a fixture
# lives in, [QATEST] tags content that's safe to read/act on -- see that doc's "one rule this
# doc follows wherever it creates content."
# ------------------------------------------------------------------------ #
QA_EMAIL = "qa-placeholder@example.com"
QA_CC_EMAIL = "qa-cc@example.com"
QA_CONTACT_EMAIL = "qatest.contact@example.com"
QA_PHONE = "555-0142"
QA_PERSON = "QA Placeholder"
QA_GMAIL_SUBJECT = "PrivacyFence QA seed message [QATEST]"
QA_GMAIL_BODY = (
    "Synthetic PrivacyFence QA test message. No real information. Safe to read, "
    "label, archive, or delete by any automated test."
)
QA_DRIVE_FOLDER = "PrivacyFence QA Sandbox"
QA_DRIVE_FILE = "PrivacyFence QA test file [QATEST].txt"
QA_DRIVE_DOC = "PrivacyFence QA test doc [QATEST]"
QA_SHEET = "PrivacyFence QA test sheet [QATEST]"
QA_SLACK_CHANNEL = "privacyfence-qa-control"
QA_SLACK_SEED = "PrivacyFence QA seed message [QATEST]. No real information. Safe to read/reply/delete."
QA_SLACK_REPLY = "PrivacyFence QA seed reply [QATEST]. No real information."
QA_CALENDAR = "PrivacyFence test [PFQA]"
QA_EVENT = "PrivacyFence QA seed event [QATEST]"
QA_EVENT_TIME = "2027-03-15 10:00–10:30 (Europe/Budapest)"
QA_CONTACT = "PrivacyFence QA Test Contact [QATEST]"
QA_TASK_LIST = "PrivacyFence QA List"
QA_CONTRAST_TASK_LIST = "PrivacyFence QA Contrast List"
QA_TASK = "PrivacyFence QA seed task [QATEST]"
QA_PROJECT = "PrivacyFence QA Test"
QA_JIRA_KEY = "PFQA-1"
QA_JIRA_SUMMARY = "PrivacyFence QA seed issue [QATEST]"
QA_SPACE = "PrivacyFence QA Test"
QA_PAGE = "PrivacyFence QA seed page [QATEST]"
QA_PAGE_BODY = (
    "Synthetic PrivacyFence QA test page. No real information. Safe to read, comment on, "
    "or edit by any automated test."
)
QA_ACCOUNT = "PrivacyFence QA — Acme Test Co [QATEST]"
QA_REPORT = "PrivacyFence QA Report"
QA_TELEGRAM_SEED = "PrivacyFence QA seed message [QATEST]. No real information."

# A synthetic settings.yaml for the menu-bar scenario -- enough auto_accept_grants/auto_accept_
# rules spread across a few connectors (gmail, drive, sheets, slack) that the Auto-accept Rules
# window's sidebar and rows have real, multi-section content to screenshot, same PFQA/[QATEST]
# naming as everything else in this file. QA_DRIVE_SANDBOX_FOLDER_ID/QA_SLACK_CONTROL_CHANNEL_ID
# are made up, not real resource ids -- see docs/qa-environment-setup.md's Drive/Slack sections for
# what the real equivalents look like.
QA_DRIVE_SANDBOX_FOLDER_ID = "1QATestSandboxFolderId00000000001"
QA_SLACK_CONTROL_CHANNEL_ID = "C0QATESTCONTROL0001"
QA_MENU_BAR_SETTINGS_YAML = f"""\
pii_detection:
  enabled: true
connectors:
  gmail:
    enabled: true
  drive:
    enabled: true
  slack:
    enabled: true
auto_accept_grants:
  drive:
    folders:
      - id: "{QA_DRIVE_SANDBOX_FOLDER_ID}"
        name: "{QA_DRIVE_FOLDER}"
        read: true
  slack:
    channels:
      - id: "{QA_SLACK_CONTROL_CHANNEL_ID}"
        name: "{QA_SLACK_CHANNEL}"
        read: true
auto_accept_rules:
  gmail.read_message:
    - rule: trusted_sender_domain
      value:
        - example.com
  sheets.write_range:
    - rule: approved_sandbox_folder
      value:
        - "{QA_DRIVE_SANDBOX_FOLDER_ID}"
"""

_TINY_PDF_BYTES = (
    b"%PDF-1.1\n"
    b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
    b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
    b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >> endobj\n"
    b"xref\n0 4\n0000000000 65535 f \n"
    b"trailer << /Size 4 /Root 1 0 R >>\n"
    b"startxref\n0\n%%EOF"
)


def _run_on_main_thread_sync(func: Callable[[], Any]) -> Any:
    """Run `func` on the main thread and block the calling thread until it
    finishes, re-raising any exception it raised. AppHelper.callAfter() is
    fire-and-forget; this adds the synchronous, return/exception-propagating
    contract show_native_approval() gets for free from performSelectorOn
    MainThread_withObject_waitUntilDone_ (which needs an NSObject method,
    not a plain closure -- not worth a helper class for the two call sites
    below).

    Only ever called here while the main thread is idling in AppHelper.
    runEventLoop() between scenarios, never while it's inside another
    blocking call (a modal approval window, an open menu's tracking
    session) -- callAfter's plain performSelectorOnMainThread scheduling
    isn't guaranteed to interrupt those, so this deliberately isn't used
    for anything that needs to.
    """
    done = threading.Event()
    result_box: list[Any] = []
    error_box: list[BaseException] = []

    def wrapper() -> None:
        try:
            result_box.append(func())
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread below
            error_box.append(exc)
        finally:
            done.set()

    AppHelper.callAfter(wrapper)
    done.wait()
    if error_box:
        raise error_box[0]
    return result_box[0] if result_box else None


def _run_menu_bar_scenario(
    name: str, *, pause_seconds: float = 0.3, screenshot_dir: Path | None = None
) -> ScenarioResult:
    """Not a tool-approval dialog -- exercises the actual menu bar status
    item and, from it, the "Manage Auto-accept Rules..." window (rules_
    manager_window.py, added by the menu bar redesign in PR #60): a real
    click on the real on-screen status item, then a real click on a real
    menu item within the menu that click opens, exactly the "did a real
    click actually reach it" concern this script's module docstring raises
    about approval windows -- the redesign has no construction-only test
    covering that its own menu wiring still resolves to a click landing on
    the right window, the same gap this whole script exists to cover for
    approval popups. Screenshots twice: the open status-item menu (the
    "menu layout"), then the rules window it opens into -- see main()'s
    --screenshot-dir.

    Fits the same ScenarioResult shape as a popup scenario even though
    there's no approve/deny decision here: click_status carries the real
    failure mode (no status item found, menu item not found, the window
    never appeared, ...) and, on full success, is set to "clicked" with
    actual==expected=="shown" so .passed means exactly what it means for
    every other scenario in this file -- a real click actually reached the
    thing it was supposed to reach.

    Builds its own throwaway PrivacyFenceMenuBar off a temp settings.yaml
    (see QA_MENU_BAR_SETTINGS_YAML) rather than the user's real config --
    same reasoning as every other scenario's synthetic preview/details data:
    this only ever needs to look realistic, never touch what's actually
    installed. Reaches into rumps' private rumps.rumps.NSApp/initializeStatusBar
    to attach a real NSStatusItem without also starting a second, nested
    AppHelper.runEventLoop() -- App.run() normally does both in one call,
    but this process is already inside its own runEventLoop() (started by
    main() below), and starting another would never return.
    """
    pid = os.getpid()
    fake_ipc_server = SimpleNamespace(
        unattended_session_count=lambda: 0,
        set_unattended_changed_listener=lambda callback: None,
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(QA_MENU_BAR_SETTINGS_YAML)
        config_path = f.name

    app_holder: list[Any] = []

    def build_app() -> None:
        # Never touches the real org config file -- see this function's
        # docstring; menu_bar.load_org_config is a plain module-level
        # function reference, reassigning it here is enough (nothing else
        # in this short-lived process depends on the original).
        menu_bar.load_org_config = lambda: {}
        app = menu_bar.PrivacyFenceMenuBar(
            config_path, connectors=["gmail", "drive", "slack"],
            ipc_server=fake_ipc_server, connector_objs=[],
        )
        # Mirrors rumps.App.run() (rumps/rumps.py) up to, but not
        # including, its final AppHelper.runEventLoop() call -- see this
        # function's docstring for why that call is skipped here.
        nsapp = NSApplication.sharedApplication()
        if nsapp.activationPolicy() == NSApplicationActivationPolicyProhibited:
            nsapp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        nsapp.activateIgnoringOtherApps_(True)
        app._nsapp = _rumps_internal.NSApp.alloc().init()
        app._nsapp._app = app.__dict__
        nsapp.setDelegate_(app._nsapp)
        app._nsapp.initializeStatusBar()
        app_holder.append(app)

    def cleanup(app: Any) -> None:
        if app._rules_manager is not None and app._rules_manager.window is not None:
            app._rules_manager.window.close()
        status_item = getattr(app._nsapp, "nsstatusitem", None)
        if status_item is not None:
            NSStatusBar.systemStatusBar().removeStatusItem_(status_item)

    def fail(click_status: str) -> ScenarioResult:
        if app_holder:
            _run_on_main_thread_sync(lambda: cleanup(app_holder[0]))
        os.unlink(config_path)
        return ScenarioResult(
            name=name, button_clicked="Manage Auto-accept Rules…", expected="shown",
            actual=None, click_status=click_status,
        )

    try:
        _run_on_main_thread_sync(build_app)
    except Exception as exc:  # noqa: BLE001 - surfaced as this scenario's failure, not a crash
        os.unlink(config_path)
        return ScenarioResult(
            name=name, button_clicked="Manage Auto-accept Rules…", expected="shown",
            actual=None, click_status=f"setup error: {exc!r}",
        )

    time.sleep(pause_seconds)
    status = _click_menu_bar_icon(pid)
    if status != "clicked":
        return fail(status)

    time.sleep(pause_seconds)
    if screenshot_dir is not None:
        _screenshot_own_window(pid, screenshot_dir / f"{_slugify(name)}-menu.png")
    time.sleep(pause_seconds)

    status = _click_menu_item(pid, "Manage Auto-accept Rules…")
    if status != "clicked":
        return fail(status)

    wait_status = _wait_for_window(pid)
    if wait_status != "ready":
        return fail(wait_status)

    time.sleep(pause_seconds)
    if screenshot_dir is not None:
        _screenshot_own_window(pid, screenshot_dir / f"{_slugify(name)}-rules-window.png")

    _run_on_main_thread_sync(lambda: cleanup(app_holder[0]))
    os.unlink(config_path)
    return ScenarioResult(
        name=name, button_clicked="Manage Auto-accept Rules…", expected="shown",
        actual="shown", click_status="clicked",
    )


def _scenarios(
    pause_seconds: float = 0.3, screenshot_dir: Path | None = None, only: str | None = None
) -> list[ScenarioResult]:
    """One scenario per tool in docs/approval-window-content-reference.md's RG-1/RG-2/RG-3/RG-4/
    WG-1/WG-2 tables (61 tools total) -- every dialog *shape* that reference doc documents, not
    just a representative handful. Cross-cutting mechanics that doc calls "automatic on every
    group" (Deny, Always allow, Allow for 5 min, the PII/content-flag banners, the visibility
    checklist, seen-count + Claude's reason together, progressive disclosure, the Gmail-style
    header, native PDFView) are folded into specific tool scenarios below rather than kept as
    separate generic ones -- see the inline notes at each such scenario for which mechanic it
    carries. This means every button, every banner/card shape, and every tool's exact preview
    field set all get a real on-screen click at least once, with no redundant duplicate coverage
    of the same mechanic twice.

    `only`, when given, restricts this to the scenarios whose name contains it (case-insensitive)
    -- see main()'s --scenario flag. Filtering happens here, before each matching call site's
    run(...) actually pops and clicks a real window, rather than after: skipped scenarios must
    never show a window at all, not just be dropped from the report.
    """
    results = []
    only_lower = only.lower() if only else None

    def run(name: str, **kwargs) -> ScenarioResult | None:
        if only_lower is not None and only_lower not in name.lower():
            return None
        return _run_scenario(name, pause_seconds=pause_seconds, screenshot_dir=screenshot_dir, **kwargs)

    # ================================================================== #
    # RG-1 -- plain review popup (summary box only, no AI-visibility checklist)
    # ================================================================== #

    results.append(run(
        "RG-1 · gmail_download_attachment",
        click_title="Allow once", expected="accept",
        title="Download Gmail Attachment",
        preview={
            "From": QA_EMAIL, "Subject": QA_GMAIL_SUBJECT, "Attachment name": "qa-smoke-test.pdf",
            "Type": "application/pdf", "Size": "24 KB", "Will save to": "~/Downloads/qa-smoke-test.pdf",
        },
        details_text=QA_GMAIL_BODY,
        allow_accept_all=False,
        connector="gmail",
    ))

    results.append(run(
        "RG-1 · drive_download_file",
        click_title="Allow once", expected="accept",
        title="Download Drive File",
        preview={
            "File": QA_DRIVE_FILE, "Owner": QA_EMAIL, "Size": "1.2 KB", "Modified": "2026-07-16",
            "Saved to": f"~/Downloads/{QA_DRIVE_FILE}",
        },
        details_text="Ordinary, non-sensitive smoke-test file content.",
        allow_accept_all=False,
        connector="drive",
    ))

    results.append(run(
        "RG-1 · calendar_get_event_details",
        click_title="Allow once", expected="accept",
        title="Read Calendar Event",
        preview={
            "Title": QA_EVENT, "Time": QA_EVENT_TIME, "Organizer": QA_PERSON, "Attendees": "none",
        },
        details_text="Synthetic PrivacyFence QA test event. No real information.",
        allow_accept_all=False,
        connector="calendar",
    ))

    results.append(run(
        "RG-1 · jira_get_issue",
        click_title="Allow once", expected="accept",
        title="Read Jira Issue",
        preview={
            "Project": QA_PROJECT, "Key": QA_JIRA_KEY, "Summary": QA_JIRA_SUMMARY,
            "Status": "To Do", "Assignee": "Unassigned",
        },
        details_text="Synthetic PrivacyFence QA test issue. No real information. Safe to comment "
                      "on, update, or transition by any automated test.",
        allow_accept_all=False,
        connector="jira",
    ))

    results.append(run(
        # Also the progressive-disclosure mechanic: "Show more" is a
        # non-terminal click that must resize the window in place without
        # resolving the modal loop, so the following "Allow once" click
        # still has to land on the same (now taller) window -- exactly
        # the kind of thing a title-bar-height miscalculation in
        # _rebuild_content would silently break.
        "RG-1 · confluence_get_page (+ Show more → Allow once)",
        click_title="Allow once", expected="accept", pre_click_title="Show more",
        title="Read Confluence Page",
        preview={
            "Title": QA_PAGE, "Space": QA_SPACE, "Author": QA_PERSON, "Last modified": "2026-07-16",
        },
        details_text=(QA_PAGE_BODY + "\n") * 60 + "the last line, still present",
        allow_accept_all=False,
        connector="confluence",
    ))

    results.append(run(
        # Same dialog shape as confluence_get_page above (same row in the
        # reference doc's RG-1 table) -- a distinct tool since it resolves
        # by title rather than page ID, not just a duplicate of the one
        # above.
        "RG-1 · confluence_get_page_by_title",
        click_title="Allow once", expected="accept",
        title="Read Confluence Page",
        preview={
            "Title": QA_PAGE, "Space": QA_SPACE, "Author": QA_PERSON, "Last modified": "2026-07-16",
        },
        details_text=QA_PAGE_BODY,
        allow_accept_all=False,
        connector="confluence",
    ))

    results.append(run(
        "RG-1 · telegram_get_messages",
        click_title="Allow once", expected="accept",
        title="Read Telegram Messages",
        preview={"Chat": "Saved Messages", "Messages": "1"},
        details_text=QA_TELEGRAM_SEED,
        allow_accept_all=False,
        connector="telegram",
    ))

    results.append(run(
        "RG-1 · telegram_search_messages",
        click_title="Allow once", expected="accept",
        title="Search Telegram Messages",
        preview={"Query": "QATEST", "Results": "1"},
        details_text=QA_TELEGRAM_SEED,
        allow_accept_all=False,
        connector="telegram",
    ))

    results.append(run(
        "RG-1 · salesforce_get_record",
        click_title="Allow once", expected="accept",
        title="Read Salesforce Record",
        preview={"Object type": "Account", "Name": QA_ACCOUNT, "Record ID": "001QA0000012345"},
        details_text=f"Name: {QA_ACCOUNT}\nIndustry: (not set)",
        allow_accept_all=False,
        connector="salesforce",
    ))

    results.append(run(
        "RG-1 · salesforce_run_report",
        click_title="Allow once", expected="accept",
        title="Run Salesforce Report",
        preview={"Report": QA_REPORT, "Report ID": "00OQA0000006789"},
        details_text="1 row, 1 grouping -- synthetic PrivacyFence QA report output.",
        allow_accept_all=False,
        connector="salesforce",
    ))

    results.append(run(
        # Also the Deny-click mechanic -- confirms Deny still resolves
        # correctly on an RG-1-shaped popup, not just the write-side one.
        "RG-1 · salesforce_search (Deny)",
        click_title="Deny", expected="deny",
        title="Search Salesforce",
        preview={
            "Search term": "PrivacyFence QA", "Object types": "Account", "Results": "2",
        },
        details_text=f"{QA_ACCOUNT}\nPrivacyFence QA — Globex Test Co [QATEST]",
        allow_accept_all=False,
        connector="salesforce",
    ))

    # ================================================================== #
    # RG-2 -- review popup + "AI will receive" checklist, plain body
    # ================================================================== #

    results.append(run(
        # The "kitchen sink" scenario: every row that CAN legally coexist
        # on one dialog, all rendered together -- seen-count, summary box,
        # visibility checklist, PII banner, and Claude's reason (rows 2-5
        # and 7 in docs/approval-window-content-reference.md's anatomy
        # table) -- plus the Always-allow-click mechanic riding along on
        # the same click. Nothing else in this file combines all five
        # cards; the RG-3 gmail_get_message scenario below trades the
        # summary box away for the email header (content_kind="email"
        # suppresses it per that doc's row 3), and the write-side
        # content-flag banner can't appear here at all (review-gate
        # only) -- see that doc's "Cross-cutting" section for exactly
        # which rows are mutually exclusive. This is also the one
        # scenario meant to be captured on its own via --scenario for a
        # README screenshot that shows every card at once.
        "RG-2 · gmail_get_thread (+ reason, seen-count, PII banner, Always allow -- all cards)",
        click_title="Always allow", expected="accept_all",
        title="Read Gmail Thread",
        preview={
            "Subject": QA_GMAIL_SUBJECT, "Participants": QA_EMAIL, "Messages": "2",
            "Dates": "2026-07-16 – 2026-07-16",
        },
        details_text=f"From: {QA_EMAIL}\n{QA_GMAIL_BODY} The refund IBAN [QATEST] is attached.\n\n"
                      f"From: {QA_EMAIL}\nSynthetic PrivacyFence QA reply. No real information.",
        allow_accept_all=True,
        visibility={"Sender & metadata": "redact", "Thread messages": "allow", "Attachments": "block"},
        claude_reason="Checking recent QA thread activity as requested.",
        seen_count=2,
        pii_categories=["IBAN (bank account number)"],
        connector="gmail",
    ))

    results.append(run(
        "RG-2 · drive_sheets_get_values",
        click_title="Allow once", expected="accept",
        title="Read Sheet Values",
        preview={"Spreadsheet": QA_SHEET, "Owner": QA_EMAIL, "Range": "A1:C10"},
        details_text="Synthetic PrivacyFence QA test spreadsheet values. No real information.",
        allow_accept_all=False,
        visibility={"Cell values": "allow"},
        connector="drive",
    ))

    results.append(run(
        # Also the "kitchen sink" mechanic: Claude's reason + a nonzero
        # seen-count rendered together, alongside the visibility
        # checklist this view already has -- confirms the taller,
        # multi-section window still doesn't shift the button row.
        "RG-2 · slack_get_channel_history (+ reason → seen-count)",
        click_title="Allow once", expected="accept",
        title="Read Slack Channel History",
        preview={
            "Channel": QA_SLACK_CHANNEL, "Messages": "2", "First message": QA_SLACK_SEED,
        },
        details_text=f"{QA_SLACK_SEED}\n{QA_SLACK_REPLY}",
        allow_accept_all=False,
        visibility={"Message text": "allow", "Usernames": "redact"},
        claude_reason="Checking recent QA channel activity as requested.",
        seen_count=2,
        connector="slack",
    ))

    results.append(run(
        "RG-2 · slack_get_thread_replies",
        click_title="Allow once", expected="accept",
        title="Read Slack Thread Replies",
        preview={
            "Channel": QA_SLACK_CHANNEL, "Thread starter": QA_SLACK_SEED, "Replies": "1",
        },
        details_text=QA_SLACK_REPLY,
        allow_accept_all=False,
        visibility={"Message text": "allow", "Usernames": "redact"},
        connector="slack",
    ))

    results.append(run(
        "RG-2 · slack_search_messages",
        click_title="Allow once", expected="accept",
        title="Search Slack Messages",
        preview={"Query": "QATEST", "Results": "2"},
        details_text=f"{QA_SLACK_SEED}\n{QA_SLACK_REPLY}",
        allow_accept_all=False,
        visibility={"Message text": "allow", "Usernames": "redact"},
        connector="slack",
    ))

    # ================================================================== #
    # RG-3 -- review popup + checklist + Gmail-style email header body (no summary box)
    # ================================================================== #

    results.append(run(
        # Also the email-header mechanic (content_kind="email") and the
        # PII banner+badges mechanic, composed together -- a realistic
        # combination (a message body that happens to contain a phone
        # number), and a case the design-review pass specifically wanted
        # covered end to end.
        "RG-3 · gmail_get_message (+ email header, + PII banner)",
        click_title="Allow once", expected="accept",
        title="Read Gmail Message",
        preview={"From": QA_EMAIL, "To": QA_EMAIL, "Subject": QA_GMAIL_SUBJECT, "Date": "2026-07-16"},
        details_text=f"{QA_GMAIL_BODY} Call me back at 555-0142 [QATEST] to confirm.",
        allow_accept_all=False,
        visibility={"Sender & metadata": "redact", "Message body": "allow", "Attachments": "block"},
        content_kind="email",
        pii_categories=["Phone number"],
        connector="gmail",
    ))

    # ================================================================== #
    # RG-4 -- review popup + checklist + optional native PDFView body
    # ================================================================== #

    results.append(run(
        # Also the native-PDFView mechanic.
        "RG-4 · drive_get_file_content (+ PDFView)",
        click_title="Allow once", expected="accept",
        title="Read Drive File Content",
        preview={
            "File": "PrivacyFence QA test file [QATEST].pdf", "Owner": QA_EMAIL,
            "Size": "18 KB", "Modified": "2026-07-16",
        },
        details_text="[binary content — this text should not be visible; the PDFView should be]",
        allow_accept_all=False,
        visibility={"File metadata": "allow", "Document content": "allow"},
        pdf_bytes=_TINY_PDF_BYTES,
        connector="drive",
    ))

    # ================================================================== #
    # WG-1 -- popup-gate, Deny / Allow once (38 tools)
    # ================================================================== #

    results.append(run(
        # Also the content-flag banner+badges mechanic.
        "WG-1 · gmail_create_draft (+ content-flag banner)",
        click_title="Allow once", expected="accept",
        title="Create Gmail Draft",
        preview={"To": QA_EMAIL, "Cc": QA_CC_EMAIL, "Subject": f"Re: {QA_GMAIL_SUBJECT}"},
        details_text="Please wire the deposit per the attached IBAN [QATEST].",
        allow_accept_all=False,
        write_content_flags=["IBAN (bank account number)"],
        connector="gmail",
    ))

    results.append(run(
        "WG-1 · gmail_reply_draft",
        click_title="Allow once", expected="accept",
        title="Create Gmail Reply Draft",
        preview={"In reply to": QA_GMAIL_SUBJECT, "To": QA_EMAIL},
        details_text="Synthetic PrivacyFence QA reply draft. No real information.",
        allow_accept_all=False,
        connector="gmail",
    ))

    results.append(run(
        "WG-1 · gmail_reply_all_draft",
        click_title="Allow once", expected="accept",
        title="Create Gmail Reply-All Draft",
        preview={"In reply to": QA_GMAIL_SUBJECT, "To": QA_EMAIL, "Also to": QA_CC_EMAIL},
        details_text="Synthetic PrivacyFence QA reply-all draft. No real information.",
        allow_accept_all=False,
        connector="gmail",
    ))

    results.append(run(
        "WG-1 · gmail_add_label",
        click_title="Allow once", expected="accept",
        title="Add Gmail Label",
        preview={"From": QA_EMAIL, "Subject": QA_GMAIL_SUBJECT, "Label": "QATEST"},
        details_text=QA_GMAIL_BODY,
        allow_accept_all=False,
        connector="gmail",
    ))

    results.append(run(
        "WG-1 · gmail_remove_label",
        click_title="Allow once", expected="accept",
        title="Remove Gmail Label",
        preview={"From": QA_EMAIL, "Subject": QA_GMAIL_SUBJECT, "Label": "QATEST"},
        details_text=QA_GMAIL_BODY,
        allow_accept_all=False,
        connector="gmail",
    ))

    results.append(run(
        "WG-1 · gmail_archive_message",
        click_title="Allow once", expected="accept",
        title="Archive Gmail Message",
        preview={"From": QA_EMAIL, "Subject": QA_GMAIL_SUBJECT},
        details_text=QA_GMAIL_BODY,
        allow_accept_all=False,
        connector="gmail",
    ))

    results.append(run(
        "WG-1 · gmail_create_filter",
        click_title="Allow once", expected="accept",
        title="Create Gmail Filter",
        preview={"Criteria": f"from:{QA_EMAIL}", "Actions": "Apply label QATEST"},
        details_text="Synthetic PrivacyFence QA filter. No real information.",
        allow_accept_all=False,
        connector="gmail",
    ))

    results.append(run(
        "WG-1 · gmail_update_filter",
        click_title="Allow once", expected="accept",
        title="Update Gmail Filter",
        preview={
            "Filter ID": "ANe1Bmh_qa0001", "Criteria": f"from:{QA_EMAIL}", "Actions": "Apply label QATEST",
        },
        details_text="Synthetic PrivacyFence QA filter update. No real information.",
        allow_accept_all=False,
        connector="gmail",
    ))

    results.append(run(
        "WG-1 · gmail_create_label",
        click_title="Allow once", expected="accept",
        title="Create Gmail Label",
        preview={"Label": "QATEST"},
        details_text="Synthetic PrivacyFence QA label. No real information.",
        allow_accept_all=False,
        connector="gmail",
    ))

    results.append(run(
        "WG-1 · drive_write_doc_content",
        click_title="Allow once", expected="accept",
        title="Write Google Doc Content",
        preview={"File": QA_DRIVE_DOC, "Owner": QA_EMAIL},
        details_text="Synthetic PrivacyFence QA doc content. No real information.",
        allow_accept_all=False,
        connector="drive",
    ))

    results.append(run(
        "WG-1 · drive_upload_file",
        click_title="Allow once", expected="accept",
        title="Upload Drive File",
        preview={
            "File": "PrivacyFence QA upload [QATEST].txt", "Source": "~/Desktop/qa-smoke-test.txt",
            "Size": "0.8 KB", "Destination": QA_DRIVE_FOLDER,
        },
        details_text="Synthetic PrivacyFence QA upload content. No real information.",
        allow_accept_all=False,
        connector="drive",
    ))

    results.append(run(
        "WG-1 · drive_write_file_content",
        click_title="Allow once", expected="accept",
        title="Write Drive File Content",
        preview={"File": QA_DRIVE_FILE, "Owner": QA_EMAIL},
        details_text="Synthetic PrivacyFence QA file content. No real information.",
        allow_accept_all=False,
        connector="drive",
    ))

    results.append(run(
        "WG-1 · drive_move_file",
        click_title="Allow once", expected="accept",
        title="Move Drive File",
        preview={"File": QA_DRIVE_FILE, "Owner": QA_EMAIL, "Move to folder": f"{QA_DRIVE_FOLDER} / Archive"},
        details_text="Synthetic PrivacyFence QA file move. No real information.",
        allow_accept_all=False,
        connector="drive",
    ))

    results.append(run(
        "WG-1 · drive_sheets_add_sheet",
        click_title="Allow once", expected="accept",
        title="Add Sheet Tab",
        preview={
            "Spreadsheet": QA_SHEET, "Owner": QA_EMAIL, "New tab": "QATEST Sheet2",
            "Size": "26 columns x 1000 rows",
        },
        details_text="Synthetic PrivacyFence QA new sheet tab. No real information.",
        allow_accept_all=False,
        connector="drive",
    ))

    results.append(run(
        "WG-1 · drive_sheets_rename_sheet",
        click_title="Allow once", expected="accept",
        title="Rename Sheet Tab",
        preview={
            "Spreadsheet": QA_SHEET, "Owner": QA_EMAIL, "Tab id": "0", "New title": "QATEST renamed",
        },
        details_text="Synthetic PrivacyFence QA sheet rename. No real information.",
        allow_accept_all=False,
        connector="drive",
    ))

    results.append(run(
        "WG-1 · drive_sheets_delete_dimensions",
        click_title="Allow once", expected="accept",
        title="Delete Sheet Rows/Columns",
        preview={
            "Spreadsheet": QA_SHEET, "Owner": QA_EMAIL, "Tab id": "0",
            "Action": "Delete 2 COLUMNS starting at index 3",
        },
        details_text="Synthetic PrivacyFence QA dimension delete. No real information.",
        allow_accept_all=False,
        connector="drive",
    ))

    results.append(run(
        "WG-1 · slack_send_message",
        click_title="Allow once", expected="accept",
        title="Send Slack Message",
        preview={"Channel": QA_SLACK_CHANNEL, "In thread": "1700000001.000100"},
        details_text="Synthetic PrivacyFence QA reply. No real information. [QATEST]",
        allow_accept_all=False,
        connector="slack",
    ))

    results.append(run(
        "WG-1 · calendar_create_event",
        click_title="Allow once", expected="accept",
        title="Create Calendar Event",
        preview={
            "Title": "PrivacyFence QA smoke event [QATEST]",
            "Time": "2027-04-01 09:00–09:30 (Europe/Budapest)",
            "Calendar": QA_CALENDAR, "Location": "Remote",
        },
        details_text="Synthetic PrivacyFence QA test event. No real information.",
        allow_accept_all=False,
        connector="calendar",
    ))

    results.append(run(
        "WG-1 · calendar_update_event",
        click_title="Allow once", expected="accept",
        title="Update Calendar Event",
        preview={"Event": QA_EVENT, "Calendar": QA_CALENDAR, "Start": "2027-03-15 10:00 → 11:00"},
        details_text="Synthetic PrivacyFence QA test event update. No real information.",
        allow_accept_all=False,
        connector="calendar",
    ))

    results.append(run(
        "WG-1 · calendar_create_out_of_office",
        click_title="Allow once", expected="accept",
        title="Create Out of Office",
        preview={
            "Title": "PrivacyFence QA OOO [QATEST]", "Time": "2027-03-20 – 2027-03-21",
            "Auto-decline": "Yes",
        },
        details_text="Synthetic PrivacyFence QA out-of-office event. No real information.",
        allow_accept_all=False,
        connector="calendar",
    ))

    results.append(run(
        "WG-1 · calendar_set_working_location",
        click_title="Allow once", expected="accept",
        title="Set Working Location",
        preview={"Date": "2027-03-22", "Location": "Home", "Building": "n/a", "Label": "Remote"},
        details_text="Synthetic PrivacyFence QA working-location entry. No real information.",
        allow_accept_all=False,
        connector="calendar",
    ))

    results.append(run(
        "WG-1 · calendar_set_event_visibility",
        click_title="Allow once", expected="accept",
        title="Set Event Visibility",
        preview={"Event": QA_EVENT, "Calendar": QA_CALENDAR, "Visibility": "default → private"},
        details_text="Synthetic PrivacyFence QA test event. No real information.",
        allow_accept_all=False,
        connector="calendar",
    ))

    results.append(run(
        "WG-1 · contacts_update",
        click_title="Allow once", expected="accept",
        title="Update Contact",
        preview={"Contact": QA_CONTACT, "Emails": QA_CONTACT_EMAIL, "Phones": QA_PHONE},
        details_text="Synthetic PrivacyFence QA contact update. No real information.",
        allow_accept_all=False,
        connector="contacts",
    ))

    results.append(run(
        "WG-1 · contacts_create",
        click_title="Allow once", expected="accept",
        title="Create Contact",
        preview={
            "Name": "PrivacyFence QA New Contact [QATEST]", "Emails": "qatest.new@example.com",
            "Phones": "555-0199",
        },
        details_text="Synthetic PrivacyFence QA contact creation. No real information.",
        allow_accept_all=False,
        connector="contacts",
    ))

    results.append(run(
        "WG-1 · contacts_add_label",
        click_title="Allow once", expected="accept",
        title="Add Contact Label",
        preview={"Contact": QA_CONTACT, "Label": "QATEST"},
        details_text="Synthetic PrivacyFence QA contact label. No real information.",
        allow_accept_all=False,
        connector="contacts",
    ))

    results.append(run(
        "WG-1 · contacts_remove_label",
        click_title="Allow once", expected="accept",
        title="Remove Contact Label",
        preview={"Contact": QA_CONTACT, "Label": "QATEST"},
        details_text="Synthetic PrivacyFence QA contact label removal. No real information.",
        allow_accept_all=False,
        connector="contacts",
    ))

    results.append(run(
        "WG-1 · telegram_send_message",
        click_title="Allow once", expected="accept",
        title="Send Telegram Message",
        preview={"Chat": "Saved Messages"},
        details_text="Synthetic PrivacyFence QA reply. No real information. [QATEST]",
        allow_accept_all=False,
        connector="telegram",
    ))

    results.append(run(
        "WG-1 · jira_create_issue",
        click_title="Allow once", expected="accept",
        title="Create Jira Issue",
        preview={
            "Project": QA_PROJECT, "Type": "Task", "Summary": "PrivacyFence QA smoke issue [QATEST]",
            "Priority": "Medium",
        },
        details_text="Synthetic PrivacyFence QA test issue. No real information.",
        allow_accept_all=False,
        connector="jira",
    ))

    results.append(run(
        "WG-1 · jira_add_comment",
        click_title="Allow once", expected="accept",
        title="Comment on Jira Issue",
        preview={"Issue": QA_JIRA_KEY},
        details_text="Synthetic PrivacyFence QA comment. No real information. [QATEST]",
        allow_accept_all=False,
        connector="jira",
    ))

    results.append(run(
        "WG-1 · jira_update_issue",
        click_title="Allow once", expected="accept",
        title="Update Jira Issue",
        preview={"Issue": QA_JIRA_KEY, "Priority": "Medium → High"},
        details_text="Synthetic PrivacyFence QA issue update. No real information.",
        allow_accept_all=False,
        connector="jira",
    ))

    results.append(run(
        "WG-1 · jira_transition_issue",
        click_title="Allow once", expected="accept",
        title="Transition Jira Issue",
        preview={"Issue": QA_JIRA_KEY, "Status": "To Do → In Progress"},
        details_text="Synthetic PrivacyFence QA issue transition. No real information.",
        allow_accept_all=False,
        connector="jira",
    ))

    results.append(run(
        "WG-1 · confluence_create_page",
        click_title="Allow once", expected="accept",
        title="Create Confluence Page",
        preview={"Space": QA_SPACE, "Title": "PrivacyFence QA smoke page [QATEST]"},
        details_text="Synthetic PrivacyFence QA test page. No real information.",
        allow_accept_all=False,
        connector="confluence",
    ))

    results.append(run(
        "WG-1 · confluence_update_page",
        click_title="Allow once", expected="accept",
        title="Update Confluence Page",
        preview={"Page ID": "qa-placeholder-id-3", "Space": QA_SPACE, "Title": QA_PAGE},
        details_text=QA_PAGE_BODY,
        allow_accept_all=False,
        connector="confluence",
    ))

    results.append(run(
        "WG-1 · tasks_create_task",
        click_title="Allow once", expected="accept",
        title="Create Task",
        preview={
            "Task list": QA_TASK_LIST, "Title": "PrivacyFence QA smoke task [QATEST]", "Due": "2027-03-20",
        },
        details_text="Synthetic PrivacyFence QA test task. No real information.",
        allow_accept_all=False,
        connector="tasks",
    ))

    results.append(run(
        "WG-1 · tasks_update_task",
        click_title="Allow once", expected="accept",
        title="Update Task",
        preview={
            "Task list": QA_TASK_LIST, "Task": QA_TASK,
            "New title": f"{QA_TASK} (updated)",
        },
        details_text="Synthetic PrivacyFence QA test task update. No real information.",
        allow_accept_all=False,
        connector="tasks",
    ))

    results.append(run(
        "WG-1 · tasks_complete_task",
        click_title="Allow once", expected="accept",
        title="Complete Task",
        preview={"Task list": QA_TASK_LIST, "Task": QA_TASK},
        details_text="Synthetic PrivacyFence QA test task. No real information.",
        allow_accept_all=False,
        connector="tasks",
    ))

    results.append(run(
        "WG-1 · tasks_uncomplete_task",
        click_title="Allow once", expected="accept",
        title="Uncomplete Task",
        preview={"Task list": QA_TASK_LIST, "Task": QA_TASK},
        details_text="Synthetic PrivacyFence QA test task. No real information.",
        allow_accept_all=False,
        connector="tasks",
    ))

    results.append(run(
        "WG-1 · tasks_move_task",
        click_title="Allow once", expected="accept",
        title="Move Task",
        preview={"Task": QA_TASK, "From list": QA_TASK_LIST, "To list": QA_CONTRAST_TASK_LIST},
        details_text="Synthetic PrivacyFence QA test task. No real information.",
        allow_accept_all=False,
        connector="tasks",
    ))

    # ================================================================== #
    # WG-2 -- popup-gate, Deny / Allow once / Allow for 5 min (6 tools)
    # ================================================================== #

    results.append(run(
        # The write-side "kitchen sink": every row a popup-gate dialog can
        # legally show, all rendered together -- summary box, seen-count,
        # amber content-flag banner, and Claude's reason (rows 1-2 and 6-7
        # in docs/approval-window-content-reference.md's anatomy table) --
        # plus the Allow-for-5-min button riding along on the same click.
        # A write never gets the AI-visibility checklist or the red PII
        # banner (review-gate only -- see that doc's "Cross-cutting"
        # section), so this is the actual ceiling for a write dialog: the
        # write-side counterpart to the RG-2 gmail_get_thread "all cards"
        # scenario above. Also the one scenario meant to be captured on
        # its own via --scenario for a README screenshot showing a write
        # dialog's full card set.
        "WG-2 · drive_sheets_write_range (+ reason, seen-count, content-flag banner, Allow for 5 min -- all cards)",
        click_title="Allow for 5 min", expected="accept_temp",
        title="Write Sheet Range",
        preview={"Spreadsheet": QA_SHEET, "Owner": QA_EMAIL, "Range": "A1:C10"},
        details_text="Synthetic PrivacyFence QA sheet write: Q2 budget figures, $12,400.00 [QATEST].",
        allow_accept_all=False,
        allow_temp_accept=True,
        write_content_flags=["Financial figures (currency amounts)"],
        claude_reason="Filling in the QA budget row as requested.",
        seen_count=3,
        connector="drive",
    ))

    results.append(run(
        "WG-2 · drive_sheets_format_range",
        click_title="Allow once", expected="accept",
        title="Format Sheet Range",
        preview={
            "Spreadsheet": QA_SHEET, "Owner": QA_EMAIL, "Range": "A1:C10", "Format": "Bold header row",
        },
        details_text="Synthetic PrivacyFence QA sheet formatting. No real information.",
        allow_accept_all=False,
        allow_temp_accept=True,
        connector="drive",
    ))

    results.append(run(
        "WG-2 · drive_sheets_insert_dimensions",
        click_title="Allow once", expected="accept",
        title="Insert Sheet Rows/Columns",
        preview={
            "Spreadsheet": QA_SHEET, "Owner": QA_EMAIL, "Tab id": "0",
            "Action": "Insert 3 ROWS before index 5",
        },
        details_text="Synthetic PrivacyFence QA dimension insert. No real information.",
        allow_accept_all=False,
        allow_temp_accept=True,
        connector="drive",
    ))

    results.append(run(
        "WG-2 · drive_add_comment",
        click_title="Allow once", expected="accept",
        title="Add Drive Comment",
        preview={"File": QA_DRIVE_FILE, "Owner": QA_EMAIL},
        details_text="Synthetic PrivacyFence QA comment. No real information. [QATEST]",
        allow_accept_all=False,
        allow_temp_accept=True,
        connector="drive",
    ))

    results.append(run(
        "WG-2 · drive_docs_edit_content",
        click_title="Allow once", expected="accept",
        title="Edit Google Doc Content",
        preview={"File": QA_DRIVE_DOC, "Owner": QA_EMAIL, "Match": "the one matching occurrence"},
        details_text="Synthetic PrivacyFence QA doc edit. No real information.",
        allow_accept_all=False,
        allow_temp_accept=True,
        connector="drive",
    ))

    results.append(run(
        "WG-2 · drive_docs_format_content",
        click_title="Allow once", expected="accept",
        title="Format Google Doc Content",
        preview={"File": QA_DRIVE_DOC, "Owner": QA_EMAIL, "Format": "Italic selection"},
        details_text="Synthetic PrivacyFence QA doc formatting. No real information.",
        allow_accept_all=False,
        allow_temp_accept=True,
        connector="drive",
    ))

    # ================================================================== #
    # Menu bar -- not a tool-approval dialog; exercises the actual menu bar
    # status item and the "Manage Auto-accept Rules..." window it opens
    # (see _run_menu_bar_scenario's docstring). Kept last, after every
    # popup scenario above: its status item and non-modal window mustn't
    # sit on screen alongside an approval popup -- _screenshot_own_window
    # assumes only one of our own windows is ever on screen at a time, and
    # this scenario cleans its own window/status item up on the way out
    # rather than leaving them for whatever runs after it.
    # ================================================================== #
    menu_bar_name = "Menu bar · status item → Manage Auto-accept Rules… window"
    if only_lower is None or only_lower in menu_bar_name.lower():
        results.append(
            _run_menu_bar_scenario(menu_bar_name, pause_seconds=pause_seconds, screenshot_dir=screenshot_dir)
        )

    return [r for r in results if r is not None]


def _render_report(results: list[ScenarioResult]) -> str:
    lines = [
        "## PrivacyFence popup smoke check",
        "",
        "Command: `python3 scripts/qa_popup_smoke.py`",
        "",
        "| Scenario | Button clicked | Expected | Actual | Click status | Result |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        status = "✅ pass" if r.passed else "❌ FAIL"
        lines.append(
            f"| {r.name} | {r.button_clicked} | `{r.expected}` | `{r.actual}` | {r.click_status} | {status} |"
        )
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    lines.append("")
    lines.append(f"{passed}/{total} scenarios passed.")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--report-file", help="Also write the printed report to this path (not committed to the repo)."
    )
    parser.add_argument(
        "--pause-seconds", type=float, default=0.3,
        help="Seconds to wait before each click (default: 0.3, just enough for the window to "
             "appear). Raise this (e.g. 3) to actually look at each popup before it's clicked away.",
    )
    parser.add_argument(
        "--screenshot-dir", type=Path,
        help="Save one PNG per scenario (named after the scenario, taken as its popup first "
             "appears, before any click) to this directory. Created if it doesn't exist. No "
             "extra macOS permission needed beyond what this script already requires -- "
             "capturing your own process's window doesn't need Screen Recording access.",
    )
    parser.add_argument(
        "--scenario",
        help="Run only the scenario(s) whose name contains this text (case-insensitive substring "
             "match against the scenario name shown in the report table, e.g. 'gmail_get_thread', "
             "'RG-4', or 'Menu bar' for the menu-bar/rules-window scenario), instead of the full "
             "~63-scenario suite (62 tool-approval scenarios plus the one menu-bar scenario). For "
             "grabbing a single updated screenshot -- e.g. for README.md -- without sitting "
             "through the whole run: --scenario 'gmail_get_thread' --screenshot-dir "
             "docs/images/screenshots. Matches nothing -> an empty report and a nonzero exit "
             "code, same as any other all-failed run.",
    )
    args = parser.parse_args()

    results: list[ScenarioResult] = []
    exit_code = 0

    def work() -> None:
        nonlocal exit_code
        try:
            if args.screenshot_dir is not None:
                args.screenshot_dir.mkdir(parents=True, exist_ok=True)
            results.extend(_scenarios(args.pause_seconds, args.screenshot_dir, args.scenario))
        except Exception as exc:  # noqa: BLE001 - surfaced via the report/exit code below, not swallowed
            print(f"qa_popup_smoke.py: scenario run raised {exc!r}", file=sys.stderr)
            exit_code = 1
        finally:
            # AppHelper.runEventLoop() (NSApplicationMain under the hood)
            # does not reliably hand control back to Python once
            # stopEventLoop() fires below -- print/write the report and
            # exit the whole process from here, the thread that's actually
            # guaranteed to keep running, instead of after runEventLoop()
            # returns, which may never happen.
            report = _render_report(results)
            print(report)
            if args.report_file:
                with open(args.report_file, "w", encoding="utf-8") as f:
                    f.write(report + "\n")
            if not results or any(not r.passed for r in results):
                exit_code = 1
            sys.stdout.flush()
            sys.stderr.flush()
            AppHelper.stopEventLoop()
            os._exit(exit_code)

    # show_native_approval() (like gate.py's real callers) must be invoked
    # from a thread other than the one driving the AppKit run loop --
    # approval_window.py's module docstring explains why. AppHelper's event
    # loop, not a full rumps.App(), is enough to pump the main thread here;
    # this script has no menu bar UI of its own.
    threading.Thread(target=work, daemon=True).start()
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
