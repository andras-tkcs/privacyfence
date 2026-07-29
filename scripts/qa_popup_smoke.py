#!/usr/bin/env python3
"""Local-only smoke test for approval_window.py's real modal loop: does a
real click on a real, on-screen "Allow once" / "Deny" / "Always allow"
button actually resolve show_native_approval() to the value its docstring
promises?

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

_scenarios() has at least one entry per tool in docs/approval-window-content-reference.md's
RG-1/RG-2/WG-1/WG-2/WG-3 tables (62 tools total, including every RG-1 tool sharing a dialog
shape, e.g. confluence_get_page/confluence_get_page_by_title) -- every dialog shape that doc
documents gets a real on-screen click, not just a representative handful. (RG-1 covers what used to
be three separate legacy-only shapes -- see that doc's "View groups" section for why v2 collapses
them; RG-2 is the one shape that's still genuinely distinct, the native-PDF body.) A handful of
RG-1 tools
additionally get two "RG-1 stress" readability variants (long text/many rows/columns, with and
without a PII banner) beyond their one baseline entry -- see the "RG-1 stress" section below for
why. Preview/details data is
realistic-but-synthetic, sourced from tests/fixtures/live/*/*.json (recorded, redacted real API
responses -- see scripts/qa_fixture_recorder.py) and docs/qa-environment-setup.md's own PFQA/
[QATEST] naming conventions, rather than generic placeholder strings -- see that doc's "one rule
this doc follows wherever it creates content" for why identity fields can look like a real
project/folder name but content never is. Cross-cutting mechanics this reference doc calls
"automatic on every group" (Deny, Always allow, the temp-accept disclosure caption, the
PII/content-flag banners, the visibility checklist, seen-count + Claude's reason together,
progressive disclosure, the Gmail-style header, native PDFView) are folded into specific tool
scenarios rather than kept as separate generic ones -- see the inline comment at each such
scenario in _scenarios().

One more, non-tool scenario runs last: the actual menu bar status item and, from it, the "Manage
Auto-accept Rules…" window (see _run_menu_bar_scenario's docstring) -- the menu bar redesign (PR
#60) has the same "real click actually reaching it" gap the rest of this script covers for
approval popups, just never exercised end to end before now.

Every tool-approval scenario renders through the one real card-stack rendering
(approval_window_html.py) -- the original hand-laid-out NSTextField/NSBox layout this replaced has
been fully removed from approval_window.py after visual sign-off. Each scenario's narrow/wide
shape (_TOOL_LAYOUT below) is a fixed, explicit per-tool assignment re-derived directly from the
"Approval windows design system" claude.ai/design project's own markup (turns 4-6) for every one
of the 30 dialog shapes that project actually mocked; tools it didn't explicitly mock got a
best-effort classification by analogy to the closest mocked sibling (documented inline at
_TOOL_LAYOUT), since confirmed correct against real screenshots and promoted into gate.py's own
copy of this table (kept in sync -- see gate.py's own _TOOL_LAYOUT comment).

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
    # Review-gate (read) dialogs only, or popup-gate (write) dialogs only:
    .venv/bin/python scripts/qa_popup_smoke.py --group rg --screenshot-dir /tmp/rg_shots
    .venv/bin/python scripts/qa_popup_smoke.py --group wg
"""
from __future__ import annotations

import argparse
import base64
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
from privacyfence.auto_accept import (  # noqa: E402
    TOOL_TO_OPERATION,
    WRITE_RULE_SUGGESTIONS,
    describe_rule_short,
)
from privacyfence.quicklook_preview import generate_thumbnail, init_quicklook_preview  # noqa: E402

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


def _wait_for_button_enabled(pid: int, title: str) -> str:
    """Block until a button with this exact title exists AND is enabled on
    our own process's first window -- returns "ready", "BUTTON_NOT_FOUND"
    (no such button ever appeared), or "TIMEOUT_BUTTON_DISABLED" (it
    exists but never became enabled within WINDOW_WAIT_TIMEOUT_SECONDS).

    v2's Deny/Allow once/Always allow start disabled and only become
    enabled once the card-stack webview finishes loading (see
    approval_window.py's webView_didFinishNavigation_ -- loadHTMLString_
    baseURL_ is asynchronous even for local content). This is the actual
    "the popup is ready" signal, distinct from _wait_for_window()'s "the
    window exists" -- the window appears (and passes _wait_for_window)
    the instant the NSPanel is created, well before the webview has
    painted anything, so a screenshot taken right after _wait_for_window
    alone can capture a still-blank webview (just the header and disabled
    buttons) depending on how fast the machine happens to render that
    run -- not reliably reproducible, and no --pause-seconds value fixes
    it for certain, only makes the race less likely to lose. Called both
    by the screenshot step (clicker(), below) and by _click_button()
    before it actually clicks, so neither can act on a stale window state.
    Legacy's buttons are never disabled, so this returns "ready" on its
    first check for that layout -- no behavior change there.
    """
    script = f'''
    tell application "System Events"
        set targetProcess to first process whose unix id is {pid}
        tell targetProcess
            if not (exists button "{title}" of window 1) then
                return "BUTTON_NOT_FOUND"
            end if
            set deadlineTime to (current date) + {WINDOW_WAIT_TIMEOUT_SECONDS}
            repeat
                if (enabled of button "{title}" of window 1) then return "ready"
                if (current date) > deadlineTime then return "TIMEOUT_BUTTON_DISABLED"
                delay 0.1
            end repeat
        end tell
    end tell
    '''
    return _run_applescript(script)


def _click_button(pid: int, title: str) -> str:
    """Click a button on our own process's first window by its exact title
    -- returns "clicked", "BUTTON_NOT_FOUND"/"TIMEOUT_BUTTON_DISABLED" (see
    _wait_for_button_enabled), or an osascript-level error string. Assumes
    the window already exists (call _wait_for_window() first).

    Waits for the button to actually be enabled before clicking -- without
    this, a click landing before v2's webview finishes loading would
    "succeed" against a button that doesn't do anything yet, leaving
    show_native_approval() blocked in runModalForWindow_ forever (the
    modal never resolves) instead of failing the scenario cleanly.
    """
    wait_status = _wait_for_button_enabled(pid, title)
    if wait_status != "ready":
        return wait_status
    script = f'''
    tell application "System Events"
        set targetProcess to first process whose unix id is {pid}
        tell targetProcess
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
    pause_seconds: float = 0.3, screenshot_dir: Path | None = None,
    **popup_kwargs
) -> ScenarioResult:
    # Every real gated call always carries a claude_reason -- "reason" is a
    # required ToolSpec param on every tool, and gate.py's gated_call()
    # unconditionally reads it via current_reason()/reason_scope() (set by
    # ipc_server.py._call_connector() before any connector method runs), so
    # §2 ("Why Claude needs more data") is never actually absent in
    # production. Defaulted here (not per scenario below) so every one of
    # the ~60 scenarios matches that guarantee without individually setting
    # it -- an empty claude_reason would make §3's number collapse to "02"
    # instead of "03" (the section-numbering counter only advances for
    # sections that actually render), which is a QA-fixture artifact this
    # exists to prevent, not something that happens for real.
    tool_name_for_reason = _tool_name_from_scenario(name)
    popup_kwargs.setdefault(
        "claude_reason",
        f"Checking {(tool_name_for_reason or 'this').replace('_', ' ')} as requested.",
    )

    # Inject the real rendering's params from the scenario name alone -- see
    # _TOOL_LAYOUT's docstring -- rather than editing every individual
    # scenario call below. is_read is derived from the "RG-"/"WG-" prefix
    # every real tool scenario name already carries (docs/approval-window-
    # content-reference.md's own grouping); upload_forced only ever applies
    # to drive_upload_file (see gap #4 in the redesign's implementation
    # plan). Mirrors exactly what gate.py itself now does in production
    # (its own _TOOL_LAYOUT is this same table, promoted there after this
    # script's own screenshot-driven sign-off).
    tool_name = _tool_name_from_scenario(name)
    popup_kwargs.setdefault("layout", _TOOL_LAYOUT.get(tool_name, "narrow"))
    popup_kwargs.setdefault("is_read", name.startswith("RG-"))
    popup_kwargs.setdefault("upload_forced", tool_name == "drive_upload_file")
    # Always allow's own verbose button label (accept_all_hint) -- derived
    # the same way gate.py derives it for real write calls (from
    # WRITE_RULE_SUGGESTIONS' rule_name), not a second hardcoded copy per
    # scenario that could drift from that table. Only meaningful when the
    # scenario itself also sets allow_accept_all=True; harmless (ignored)
    # otherwise. Read-gate scenarios don't get one here -- suggest_rule()'s
    # own top match depends on live per-call data (e.g. whether the
    # fixture's sender matches my_email), not a static per-tool table like
    # WRITE_RULE_SUGGESTIONS, so each read scenario below sets its own
    # accept_all_hint directly where relevant instead.
    operation_key = TOOL_TO_OPERATION.get(tool_name or "")
    write_suggestion = WRITE_RULE_SUGGESTIONS.get(operation_key) if operation_key else None
    if write_suggestion is not None:
        popup_kwargs.setdefault("accept_all_hint", describe_rule_short(write_suggestion.rule_name))
    elif tool_name in _READ_ACCEPT_ALL_TOP_RULE:
        # Read tools' own top-priority rule name, per
        # docs/always-allow-rules-reference.md's Read tools tables -- not
        # a live suggest_rule() re-evaluation (that depends on per-call
        # data this synthetic fixture doesn't fully model, e.g. whether
        # the sender equals my_email), just the same representative
        # top-of-priority-order candidate the doc documents for each tool.
        popup_kwargs.setdefault("accept_all_hint", describe_rule_short(_READ_ACCEPT_ALL_TOP_RULE[tool_name]))

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
        # The window existing is not the same as it being ready to look
        # at: v2's webview loads asynchronously and the window appears the
        # instant the NSPanel is created, well before that finishes (see
        # _wait_for_button_enabled's own docstring) -- waiting on "Deny"
        # (always present, never conditional like "Always allow") becoming
        # enabled is the actual "safe to screenshot" signal. Without this,
        # a screenshot taken right after _wait_for_window alone could
        # capture just the header and disabled buttons, a race no
        # --pause-seconds value reliably avoids.
        ready_status = _wait_for_button_enabled(pid, "Deny")
        if ready_status != "ready":
            click_status_box.append(ready_status)
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

# Readability stress-test fixtures (RG-1 stress section below): every RG-1
# tool's baseline scenario above uses short, single-line content -- these
# exist purely to exercise the fixed-layout line-clamp/table machinery with
# long text, many rows/columns, and a PII banner, in isolation from each
# other (a long/no-PII and long/PII pair per tool, holding content length
# constant and varying only the PII banner) so a reviewer can tell which
# variable actually caused any given readability problem.
QA_LONG_PARAGRAPH = (
    "Synthetic PrivacyFence QA long-form test content [QATEST]. This paragraph exists purely to "
    "exercise the fixed-layout line-clamp behavior with a body of text long enough to overflow "
    "two, three, and four lines at the approval window's actual rendered width, so a reviewer can "
    "confirm truncation lands with a clean ellipsis instead of a ragged cutoff, an overlapping "
    "line, or a silently reflowed card. No real information is present anywhere in this sentence "
    "or any of the ones around it -- it is QA filler text only, safe to read, copy, or discard by "
    "any automated test or human reviewer without consequence."
)
QA_MANY_ATTENDEES = ", ".join(
    [f"{QA_PERSON} (organizer)"]
    + [f"QA Contact {i} <qatest.contact{i}@example.com>" for i in range(1, 9)]
)
QA_MANY_COMMENTS = [
    [f"QA Commenter {i}", f"2026-07-{10 + i:02d}",
     (QA_LONG_PARAGRAPH if i % 3 == 0 else f"Synthetic PrivacyFence QA comment {i} [QATEST]. No real information.")]
    for i in range(1, 7)
]
QA_MANY_TELEGRAM_ROWS = [
    [f"QA Contact {i}", f"2026-07-{10 + i:02d}T09:{i:02d}:00Z",
     (QA_LONG_PARAGRAPH if i % 4 == 0 else f"Synthetic PrivacyFence QA message {i} [QATEST]. No real information.")]
    for i in range(1, 13)
]
QA_MANY_SALESFORCE_FIELDS = {
    "Name": QA_ACCOUNT, "Industry": "Technology", "Type": "Customer", "Phone": QA_PHONE,
    "Website": "https://qa-placeholder.example.com", "BillingCity": "Budapest",
    "BillingCountry": "Hungary", "AnnualRevenue": "1000000", "NumberOfEmployees": "42",
    "AccountSource": "QA Seed Data", "Description": QA_LONG_PARAGRAPH,
    "OwnerId": "005QA00000000001", "CreatedDate": "2026-01-01T00:00:00Z",
    "LastModifiedDate": "2026-07-16T00:00:00Z", "Rating": "Hot",
}
QA_MANY_SALESFORCE_ROWS = [
    [f"{QA_ACCOUNT} {i}", "Prospecting", f"${1000 * i:,}", f"2026-0{(i % 9) + 1}-15", QA_PERSON]
    for i in range(1, 11)
]
QA_MANY_SEARCH_ROWS = [
    ["Account", f"{QA_ACCOUNT} {i}", f"001QA000001234{i}"] for i in range(1, 11)
]
QA_LONG_SUBJECT = (
    "Re: Fwd: Re: PrivacyFence QA quarterly financial summary and supporting documentation "
    "review needed before Friday's board meeting [QATEST]"
)
QA_LONG_FILENAME = (
    "PrivacyFence-QA-Quarterly-Financial-Summary-And-Supporting-Documentation-Bundle-"
    "2026-Q3-Draft-v12-FINAL-FINAL [QATEST].pdf"
)
# §2's own stress fixture -- a plausible but over-explaining Claude reason,
# long enough to exceed .pf-quote's 3-line clamp and get truncated with an
# ellipsis, to check the new hover-tooltip mechanism against real §2
# content specifically (every other stress scenario varies §1/§3/the right
# pane instead).
QA_LONG_CLAUDE_REASON = (
    "Checking the QA event details as requested, since the calendar list view only exposed the "
    "event's title and time slot and the user specifically asked whether any of the invited "
    "guests have a conflicting commitment that day, whether the meeting still has an open "
    "video-conferencing link attached, and whether the description contains any pre-reads or "
    "agenda items that need to be reviewed before the call starts [QATEST]."
)

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

# A real, visually-distinguishable PNG (240x160, nested blue/white/magenta
# squares -- Broadsheet's own accent/accent-2 colors) -- for
# gmail_download_attachment/drive_download_file/drive_upload_file's
# preview_bytes/preview_mime_type, so approval_window.py's image-render
# branch has something actually *visible* on screen, not a
# 1x1-transparent-pixel stand-in that "renders" but shows nothing to look at
# in a screenshot. See docs/file-type-support.md for the real feature this
# stands in for.
_TINY_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAPAAAACgCAYAAAAy2+FlAAAEDmlDQ1BrQ0dDb2xvclNwYWNlR2Vu"
    "ZXJpY1JHQgAAOI2NVV1oHFUUPpu5syskzoPUpqaSDv41lLRsUtGE2uj+ZbNt3CyTbLRBkMns3Z1p"
    "JjPj/KRpKT4UQRDBqOCT4P9bwSchaqvtiy2itFCiBIMo+ND6R6HSFwnruTOzu5O4a73L3PnmnO9+"
    "595z7t4LkLgsW5beJQIsGq4t5dPis8fmxMQ6dMF90A190C0rjpUqlSYBG+PCv9rt7yDG3tf2t/f/"
    "Z+uuUEcBiN2F2Kw4yiLiZQD+FcWyXYAEQfvICddi+AnEO2ycIOISw7UAVxieD/Cyz5mRMohfRSwo"
    "qoz+xNuIB+cj9loEB3Pw2448NaitKSLLRck2q5pOI9O9g/t/tkXda8Tbg0+PszB9FN8DuPaXKnKW"
    "4YcQn1Xk3HSIry5ps8UQ/2W5aQnxIwBdu7yFcgrxPsRjVXu8HOh0qao30cArp9SZZxDfg3h1wTzK"
    "xu5E/LUxX5wKdX5SnAzmDx4A4OIqLbB69yMesE1pKojLjVdoNsfyiPi45hZmAn3uLWdpOtfQOaVm"
    "ikEs7ovj8hFWpz7EV6mel0L9Xy23FMYlPYZenAx0yDB1/PX6dledmQjikjkXCxqMJS9WtfFCyH9X"
    "tSekEF+2dH+P4tzITduTygGfv58a5VCTH5PtXD7EFZiNyUDBhHnsFTBgE0SQIA9pfFtgo6cKGuho"
    "oeilaKH41eDs38Ip+f4At1Rq/sjr6NEwQqb/I/DQqsLvaFUjvAx+eWirddAJZnAj1DFJL0mSg/gc"
    "IpPkMBkhoyCSJ8lTZIxk0TpKDjXHliJzZPO50dR5ASNSnzeLvIvod0HG/mdkmOC0z8VKnzcQ2M/Y"
    "z2vKldduXjp9bleLu0ZWn7vWc+l0JGcaai10yNrUnXLP/8Jf59ewX+c3Wgz+B34Df+vbVrc16zTM"
    "Vgp9um9bxEfzPU5kPqUtVWxhs6OiWTVW+gIfywB9uXi7CGcGW/zk98k/kmvJ95IfJn/j3uQ+4c5z"
    "n3Kfcd+AyF3gLnJfcl9xH3OfR2rUee80a+6vo7EK5mmXUdyfQlrYLTwoZIU9wsPCZEtP6BWGhAlh"
    "L3p2N6sTjRdduwbHsG9kq32sgBepc+xurLPW4T9URpYGJ3ym4+8zA05u44QjST8ZIoVtu3qE7fWm"
    "dn5LPdqvgcZz8Ww8BWJ8X3w0PhQ/wnCDGd+LvlHs8dRy6bLLDuKMaZ20tZrqisPJ5ONiCq8yKhYM"
    "5cCgKOu66Lsc0aYOtZdo5QCwezI4wm9J/v0X23mlZXOfBjj8Jzv3WrY5D+CsA9D7aMs2gGfjve8A"
    "rD6mePZSeCfEYt8CONWDw8FXTxrPqx/r9Vt4biXeANh8vV7/+/16ffMD1N8AuKD/A/8leAvFY9bL"
    "AAAAOGVYSWZNTQAqAAAACAABh2kABAAAAAEAAAAaAAAAAAACoAIABAAAAAEAAADwoAMABAAAAAEA"
    "AACgAAAAAM4zxzoAAATvSURBVHgB7d2xbVVREARQjBxDNW6BNmgCqqAJaMMtUA0VGMnJprsS4s9I"
    "h+gF7/KGM3f0Q54+ffv59sEfAgQqBT5WphaaAIF3AQN2EQgUCxhwcXmiEzBgd4BAsYABF5cnOgED"
    "dgcIFAsYcHF5ohMwYHeAQLGAAReXJzoBA3YHCBQLGHBxeaITMGB3gECxgAEXlyc6AQN2BwgUCxhw"
    "cXmiEzBgd4BAsYABF5cnOgEDdgcIFAsYcHF5ohMwYHeAQLGAAReXJzoBA3YHCBQLGHBxeaITMGB3"
    "gECxgAEXlyc6AQN2BwgUCxhwcXmiEzBgd4BAsYABF5cnOoHnRxH8+fH1UZ/2XQL/XODz91///O/c"
    "/IV+gTdK3iEQKmDAocWIRWAjYMAbJe8QCBUw4NBixCKwETDgjZJ3CIQKGHBoMWIR2AgY8EbJOwRC"
    "BQw4tBixCGwEDHij5B0CoQIGHFqMWAQ2Aga8UfIOgVABAw4tRiwCGwED3ih5h0CogAGHFiMWgY2A"
    "AW+UvEMgVMCAQ4sRi8BGwIA3St4hECpgwKHFiEVgI2DAGyXvEAgVMODQYsQisBEw4I2SdwiEChhw"
    "aDFiEdgIGPBGyTsEQgUMOLQYsQhsBAx4o+QdAqECBhxajFgENgIGvFHyDoFQAQMOLUYsAhsBA94o"
    "eYdAqMDD/nfCR3n8fvryqE/77n8QeHl7/Q9fyfmEX+CcLiQhcBYw4DOZAwRyBAw4pwtJCJwFDPhM"
    "5gCBHAEDzulCEgJnAQM+kzlAIEfAgHO6kITAWcCAz2QOEMgRMOCcLiQhcBYw4DOZAwRyBAw4pwtJ"
    "CJwFDPhM5gCBHAEDzulCEgJnAQM+kzlAIEfAgHO6kITAWcCAz2QOEMgRMOCcLiQhcBYw4DOZAwRy"
    "BAw4pwtJCJwFDPhM5gCBHAEDzulCEgJnAQM+kzlAIEfAgHO6kITAWcCAz2QOEMgRMOCcLiQhcBYw"
    "4DOZAwRyBAw4pwtJCJwFDPhM5gCBHAEDzulCEgJnAQM+kzlAIEfAgHO6kITAWcCAz2QOEMgRMOCc"
    "LiQhcBYw4DOZAwRyBAw4pwtJCJwFDPhM5gCBHAEDzulCEgJnAQM+kzlAIEfAgHO6kITAWeD5fKL8"
    "wMvba/m/QHwCI+AXeCw8EagTMOC6ygQmMAIGPBaeCNQJGHBdZQITGAEDHgtPBOoEDLiuMoEJjIAB"
    "j4UnAnUCBlxXmcAERsCAx8ITgToBA66rTGACI2DAY+GJQJ2AAddVJjCBETDgsfBEoE7AgOsqE5jA"
    "CBjwWHgiUCdgwHWVCUxgBAx4LDwRqBMw4LrKBCYwAgY8Fp4I1AkYcF1lAhMYAQMeC08E6gQMuK4y"
    "gQmMgAGPhScCdQIGXFeZwARGwIDHwhOBOgEDrqtMYAIjYMBj4YlAnYAB11UmMIERMOCx8ESgTuDp"
    "07efb3WpBSZA4F3AL7CLQKBYwICLyxOdgAG7AwSKBQy4uDzRCRiwO0CgWMCAi8sTnYABuwMEigUM"
    "uLg80QkYsDtAoFjAgIvLE52AAbsDBIoFDLi4PNEJGLA7QKBYwICLyxOdgAG7AwSKBQy4uDzRCRiw"
    "O0CgWMCAi8sTnYABuwMEigUMuLg80QkYsDtAoFjAgIvLE52AAbsDBIoFDLi4PNEJGLA7QKBYwICL"
    "yxOdgAG7AwSKBQy4uDzRCRiwO0CgWOAvMrINdTs38y8AAAAASUVORK5CYII="
)

# A real fixture file (checked in, not a base64 blob like _TINY_PDF_BYTES/
# _TINY_PNG_BYTES above) for the QuickLook-fallback scenario below: an A4
# (210x297mm), multi-paragraph .docx -- QuickLook renders the file's actual
# page dimensions, so a real document here (as opposed to reusing
# _TINY_PNG_BYTES, which is square) gives that scenario a genuinely
# page-shaped thumbnail. Generated once via python-docx (not a runtime
# dependency of this script -- see the file's own contents for exactly
# what it holds).
_LOREM_IPSUM_DOCX_PATH = (
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "qa_assets" / "lorem_ipsum.docx"
)


def _quicklook_thumbnail_for_lorem_ipsum_docx() -> bytes:
    """Real generate_thumbnail() call, not a stand-in -- QuickLook Previews
    is off by default in production (menu-bar toggle), so this flips
    quicklook_preview.py's in-process flag on for this one call, the same
    way the menu bar's own toggle does, rather than touching settings.yaml
    or any daemon state. Best-effort like the real connector call sites
    (drive.py/gmail.py): returns b"" if quicklookd can't render it for any
    reason (timeout, disabled, missing renderer) -- the scenario then falls
    back to the plain metadata-only preview, exactly like a real miss
    would in production, rather than silently substituting fake bytes.
    """
    init_quicklook_preview(True)
    data = _LOREM_IPSUM_DOCX_PATH.read_bytes()
    return generate_thumbnail(data, _LOREM_IPSUM_DOCX_PATH.name) or b""


# Per-tool narrow/wide assignment -- re-derived directly from the "Approval windows design
# system" claude.ai/design project's own markup (turns 4-6: every .pf-win with an inline
# style="width:880px" is wide, everything else is narrow), not from memory or a length heuristic.
# Keyed by the bare tool name _tool_name_from_scenario() extracts from each scenario's own name
# string. Confirmed against real screenshots and promoted verbatim into gate.py's own copy of this
# table for production use -- keep the two in sync if either ever changes.
#
# All 17 read-gate tools the design canvas mocked are wide except calendar_get_event_details; all
# 13 write-gate tools it mocked are wide except calendar_create_event. The canvas's own mock also
# had slack_send_message/telegram_send_message/jira_add_comment as narrow, but that mock rendered
# the message/comment body inline inside its own §2 card via a "Show more" progressive-disclosure
# toggle this template deliberately never carried over (see build_card_stack_html's own docstring
# -- "no Show more/less anywhere ... every row has a fixed, truncated size"). Since our narrow
# layout genuinely has no mechanism at all to show details_text (see that function's own docstring:
# "no preview pane at all"), keeping these three narrow would silently drop the one thing being
# approved -- the actual message/comment text -- so they're wide here instead, same as every other
# tool whose details_text is real free-text content rather than a fixed disclosure sentence. Tools
# the canvas never mocked at all (everything below the first blank line in each connector's block)
# are a best-effort classification by analogy to the closest mocked sibling from the same connector
# -- wide only for tools that write/return a genuine prose body (doc/file content, page content,
# sheet cell values, chat/comment text); narrow for short structured field changes or a fixed
# disclosure sentence with nothing to actually preview.
_TOOL_LAYOUT: dict[str, str] = {
    # Confirmed wide from the design canvas directly (turns 4-6):
    "gmail_get_message": "wide", "gmail_get_thread": "wide",
    "gmail_download_attachment": "wide", "drive_download_file": "wide",
    "salesforce_get_record": "wide", "salesforce_search": "wide", "salesforce_run_report": "wide",
    "jira_get_issue": "wide", "confluence_get_page": "wide", "confluence_get_page_by_title": "wide",
    "telegram_get_messages": "wide", "telegram_search_messages": "wide",
    "drive_sheets_get_values": "wide", "slack_get_channel_history": "wide",
    "slack_get_thread_replies": "wide", "slack_search_messages": "wide",
    "drive_get_file_content": "wide",
    "gmail_create_draft": "wide", "gmail_reply_draft": "wide",
    "drive_sheets_write_range": "wide", "drive_upload_file": "wide",
    "jira_create_issue": "wide", "confluence_create_page": "wide",
    "calendar_get_event_details": "narrow", "calendar_create_event": "narrow",
    #
    # Deviates from the design canvas's own (narrow) mock -- see the
    # docstring above for why: our narrow layout has no mechanism at all to
    # show details_text, and these three carry a real message/comment body,
    # not a fixed disclosure sentence.
    "slack_send_message": "wide", "telegram_send_message": "wide", "jira_add_comment": "wide",
    #
    # Not mocked by the design canvas -- best-effort by analogy (see docstring above):
    "gmail_reply_all_draft": "wide",  # same shape as gmail_reply_draft
    "gmail_add_label": "narrow", "gmail_remove_label": "narrow", "gmail_archive_message": "narrow",
    "gmail_create_filter": "narrow", "gmail_update_filter": "narrow", "gmail_create_label": "narrow",
    "drive_write_doc_content": "wide", "drive_write_file_content": "wide",  # writing a prose body
    "drive_docs_edit_content": "wide",  # editing doc body content, same as writing it
    "drive_move_file": "narrow", "drive_sheets_add_sheet": "narrow",
    "drive_sheets_rename_sheet": "narrow", "drive_sheets_delete_dimensions": "narrow",
    "drive_sheets_format_range": "narrow", "drive_sheets_insert_dimensions": "narrow",
    "drive_add_comment": "wide",  # real comment body, like jira_add_comment (see that entry above)
    "tasks_create_task": "wide", "tasks_update_task": "wide",  # real notes body, when notes are given
    "drive_docs_format_content": "narrow",  # formatting only, no new body text
    "calendar_update_event": "narrow", "calendar_create_out_of_office": "narrow",
    "calendar_set_working_location": "narrow", "calendar_set_event_visibility": "narrow",
    "contacts_update": "narrow", "contacts_create": "narrow",
    "contacts_add_label": "narrow", "contacts_remove_label": "narrow",
    "jira_update_issue": "narrow", "jira_transition_issue": "narrow",  # field-change rows, not prose
    "confluence_update_page": "wide",  # editing page body, same as confluence_create_page
    "tasks_complete_task": "narrow",
    "tasks_uncomplete_task": "narrow", "tasks_move_task": "narrow",
}

# Read tools' own top-priority Always-allow rule name, per
# docs/always-allow-rules-reference.md's Read tools tables -- the
# WRITE_RULE_SUGGESTIONS-equivalent for the read side, except there's no
# single shared Python dict to derive this from directly (suggest_rule()'s
# actual pick depends on live per-call data via SUGGESTION_FAMILIES'
# priority order, not a static tool->rule mapping) -- so unlike
# _TOOL_LAYOUT above, this is one, kept in sync with that reference doc's
# own tables by hand. Tools with no read-gate Always-allow at all (none
# currently -- every RG-1/RG-2 tool has at least one candidate) simply
# don't appear here.
_READ_ACCEPT_ALL_TOP_RULE: dict[str, str] = {
    "gmail_get_message": "i_am_sender", "gmail_get_thread": "i_am_sender",
    "gmail_download_attachment": "i_am_sender",
    "drive_get_file_content": "i_am_owner", "drive_download_file": "i_am_owner",
    "drive_sheets_get_values": "i_am_owner",
    "slack_get_channel_history": "dm_with_myself", "slack_get_thread_replies": "dm_with_myself",
    "slack_search_messages": "approved_channel_all_results",
    "calendar_get_event_details": "i_am_organizer",
    "salesforce_get_record": "approved_object_types", "salesforce_run_report": "approved_report_ids",
    "salesforce_search": "approved_object_types",
    "jira_get_issue": "i_am_reporter",
    "confluence_get_page": "i_am_author", "confluence_get_page_by_title": "i_am_author",
    "telegram_get_messages": "approved_chats", "telegram_search_messages": "approved_chats_all_results",
}

_SCENARIO_TOOL_RE = re.compile(r"^\S+-\d+\s*·\s*([a-z_]+)")


def _tool_name_from_scenario(name: str) -> str | None:
    """Extracts e.g. "gmail_download_attachment" from "RG-1 · gmail_download_attachment (+ Show
    more → Allow once)". Returns None for the menu-bar scenario (no "RG-N ·"/"WG-N ·" prefix at
    all) -- _run_scenario only consults this when actually building a tool-approval popup, so a
    None here is never reached for that scenario in the first place."""
    m = _SCENARIO_TOOL_RE.match(name)
    return m.group(1) if m else None


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
    pause_seconds: float = 0.3, screenshot_dir: Path | None = None, only: str | None = None,
    group: str = "all",
) -> list[ScenarioResult]:
    """At least one scenario per tool in docs/approval-window-content-reference.md's RG-1/RG-2/
    WG-1/WG-2/WG-3 tables (61 tools total) -- every dialog *shape* that reference doc
    documents, not just a representative handful. A handful of RG-1 tools additionally get two
    "RG-1 stress" readability variants beyond their baseline entry -- see that section below.
    Cross-cutting mechanics that doc calls "automatic on every
    group" (Deny, Always allow, the temp-accept disclosure caption, the PII/content-flag banners,
    the visibility checklist, seen-count + Claude's reason together, progressive disclosure, the
    Gmail-style header, native PDFView) are folded into specific tool scenarios below rather than kept as
    separate generic ones -- see the inline notes at each such scenario for which mechanic it
    carries. This means every button, every banner/card shape, and every tool's exact preview
    field set all get a real on-screen click at least once, with no redundant duplicate coverage
    of the same mechanic twice.

    `only`, when given, restricts this to the scenarios whose name contains it (case-insensitive)
    -- see main()'s --scenario flag. Filtering happens here, before each matching call site's
    run(...) actually pops and clicks a real window, rather than after: skipped scenarios must
    never show a window at all, not just be dropped from the report.

    `group` ("all"/"rg"/"wg", see main()'s --group flag) restricts this to scenarios whose name
    starts with that literal prefix ("RG-"/"WG-") -- i.e. review-gate-only or popup-gate-only runs.
    Combines with `only` (both must match); every scenario name below is authored with an "RG-N ·"
    or "WG-N ·" prefix specifically so this prefix check is exact, not a heuristic.

    Every scenario renders through the one real rendering approval_window.py has --
    _run_scenario does the per-tool layout/is_read/upload_forced injection itself (see its own
    comment), so no change is needed to any of the individual scenario calls below for that.
    """
    results = []
    only_lower = only.lower() if only else None
    group_prefix = f"{group.upper()}-" if group != "all" else None

    def run(name: str, **kwargs) -> ScenarioResult | None:
        if only_lower is not None and only_lower not in name.lower():
            return None
        if group_prefix is not None and not name.startswith(group_prefix):
            return None
        return _run_scenario(
            name, pause_seconds=pause_seconds, screenshot_dir=screenshot_dir, **kwargs,
        )

    # ================================================================== #
    # RG-1 -- review popup (every read tool except drive_get_file_content,
    # RG-2 below). Used to be three separate shapes here (no checklist /
    # checklist / Gmail email header) -- collapsed into one (see
    # docs/approval-window-content-reference.md's "View groups" section).
    # ================================================================== #

    results.append(run(
        # Also the preview_bytes/preview_mime_type image-render mechanic
        # (merged via #96-#97 -- see the redesign's implementation plan):
        # _TINY_PNG_BYTES gives approval_window.py's image branch (an
        # inline <img> data URI) something real to render instead of
        # falling back to the plain-metadata view.
        "RG-1 · gmail_download_attachment (+ image preview)",
        click_title="Allow once", expected="accept",
        title="Download Gmail Attachment",
        preview={
            "From": QA_EMAIL, "Subject": QA_GMAIL_SUBJECT, "Attachment": "qa-smoke-test.png",
            "Type": "image/png", "Size": "1 KB",
        },
        new_info={
            "Content returned to Claude": "None — file bytes are never sent",
            "Will save to": "~/Downloads/qa-smoke-test.png",
        },
        details_text=QA_GMAIL_BODY,
        allow_accept_all=True,
        connector="gmail",
        preview_bytes=_TINY_PNG_BYTES, preview_mime_type="image/png",
    ))

    results.append(run(
        # Also the preview_bytes/preview_mime_type image-render mechanic --
        # see the gmail_download_attachment scenario above for why.
        "RG-1 · drive_download_file (+ image preview)",
        click_title="Allow once", expected="accept",
        title="Download Drive File",
        preview={
            "File": "PrivacyFence QA test image [QATEST].png", "Owner": QA_EMAIL, "Size": "1 KB",
            "Modified": "2026-07-16",
        },
        new_info={
            "Content returned to Claude": "None — file bytes are never sent",
            "Saved to": "~/Downloads/PrivacyFence QA test image [QATEST].png",
        },
        details_text="Ordinary, non-sensitive smoke-test file content.",
        allow_accept_all=True,
        connector="drive",
        preview_bytes=_TINY_PNG_BYTES, preview_mime_type="image/png",
    ))

    results.append(run(
        # The QuickLook-fallback mechanic (PR #100, quicklook_preview.py):
        # a non-image file with no Drive-generated thumbnailLink falls back
        # to a quicklookd-rendered thumbnail when QuickLook Previews is
        # enabled from the menu bar -- generate_thumbnail() always returns
        # PNG bytes, fed through the exact same preview_bytes/
        # preview_mime_type channel a direct image preview uses (see
        # drive.py's _download_file). Unlike the image-preview scenario
        # above, this one calls the real generate_thumbnail() against a
        # real, checked-in A4 .docx fixture (tests/fixtures/qa_assets/
        # lorem_ipsum.docx) instead of reusing _TINY_PNG_BYTES -- a real
        # quicklookd render is a genuinely page-shaped (portrait) thumbnail,
        # not the square placeholder a reused image fixture would give.
        "RG-1 · drive_download_file (+ QuickLook preview)",
        click_title="Allow once", expected="accept",
        title="Download Drive File",
        preview={
            "File": "PrivacyFence QA test doc [QATEST].docx", "Owner": QA_EMAIL, "Size": "36 KB",
            "Modified": "2026-07-16",
        },
        new_info={
            "Content returned to Claude": "None — file bytes are never sent",
            "Saved to": "~/Downloads/PrivacyFence QA test doc [QATEST].docx",
        },
        details_text="Synthetic lorem ipsum content. No real information. Safe to read, "
                      "download, or preview by any automated test.",
        allow_accept_all=True,
        connector="drive",
        preview_bytes=_quicklook_thumbnail_for_lorem_ipsum_docx(), preview_mime_type="image/png",
    ))

    results.append(run(
        # Also the redesign's new_info (§3) mechanic: real values (not an
        # abstract policy sentence), merged Attendees+Organizer (no separate
        # Organizer field on the UI), and allow_accept_all=True -- matches
        # auto_accept.py's real calendar.read_event_details rule for an
        # event the caller organizes with no external attendees (i_am_
        # organizer/no_external_attendees/non_private_event all hold for
        # this synthetic event), which the old fixture had wrong.
        "RG-1 · calendar_get_event_details",
        click_title="Allow once", expected="accept",
        title="Read Calendar Event",
        preview={"Title": QA_EVENT, "Time": QA_EVENT_TIME},
        new_info={
            "Attendees": f"{QA_PERSON} (organizer), QA Contact <{QA_CONTACT_EMAIL}>",
            "Location": "Remote",
            "Description": "Synthetic PrivacyFence QA test event description. No real information.",
        },
        details_text="Synthetic PrivacyFence QA test event. No real information.",
        claude_reason="Checking the QA event details as requested.",
        allow_accept_all=True,
        connector="calendar",
    ))

    results.append(run(
        # §2 stress: everything else matches the plain baseline above --
        # only claude_reason is long enough to exceed .pf-quote's 3-line
        # clamp and get ellipsis-truncated, isolating that one variable
        # (checks the new title="..." hover-tooltip mechanism against real
        # §2 content, not just §1/§3 rows or the right pane).
        "RG-1 · calendar_get_event_details (long §2 reason, truncated)",
        click_title="Allow once", expected="accept",
        title="Read Calendar Event",
        preview={"Title": QA_EVENT, "Time": QA_EVENT_TIME},
        new_info={
            "Attendees": f"{QA_PERSON} (organizer), QA Contact <{QA_CONTACT_EMAIL}>",
            "Location": "Remote",
            "Description": "Synthetic PrivacyFence QA test event description. No real information.",
        },
        details_text="Synthetic PrivacyFence QA test event. No real information.",
        claude_reason=QA_LONG_CLAUDE_REASON,
        allow_accept_all=True,
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
        new_info={
            "Description": "Full description text",
            "Comments": "Author, created date, and body per comment",
        },
        details_text="Synthetic PrivacyFence QA test issue. No real information. Safe to comment "
                      "on, update, or transition by any automated test.",
        preview_blocks=[
            {"type": "field", "label": "Reporter", "value": QA_PERSON},
            {"type": "heading", "label": "Description"},
            {"type": "text", "text": "Synthetic PrivacyFence QA test issue description. No real information."},
            {
                "type": "table", "caption": "Comments (1)", "headers": ["Author", "Date", "Comment"],
                "rows": [[QA_PERSON, "2026-07-16", "Synthetic PrivacyFence QA test comment. No real information."]],
            },
        ],
        allow_accept_all=True,
        connector="jira",
    ))

    results.append(run(
        "RG-1 · confluence_get_page",
        click_title="Allow once", expected="accept",
        title="Read Confluence Page",
        preview={"Title": QA_PAGE, "Space": QA_SPACE},
        new_info={
            "Author": QA_PERSON, "Last modified": "2026-07-16", "Page body": "Full page content",
        },
        details_text=(QA_PAGE_BODY + "\n") * 60 + "the last line, still present",
        allow_accept_all=True,
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
        preview={"Title": QA_PAGE, "Space": QA_SPACE},
        new_info={
            "Author": QA_PERSON, "Last modified": "2026-07-16", "Page body": "Full page content",
        },
        details_text=QA_PAGE_BODY,
        allow_accept_all=True,
        connector="confluence",
    ))

    results.append(run(
        "RG-1 · telegram_get_messages",
        click_title="Allow once", expected="accept",
        title="Read Telegram Messages",
        preview={"Chat": "Saved Messages"},
        new_info={"Messages": "1", "Message text": "Full sender name, text, and date per message"},
        details_text=QA_TELEGRAM_SEED,
        preview_tables=[{
            "headers": ["Sender", "Date", "Message"], "rows": [[QA_PERSON, "2026-07-16", QA_TELEGRAM_SEED]],
        }],
        table_only=True,
        allow_accept_all=True,
        connector="telegram",
    ))

    results.append(run(
        "RG-1 · telegram_search_messages",
        click_title="Allow once", expected="accept",
        title="Search Telegram Messages",
        preview={"Query": "QATEST"},
        new_info={"Results": "1", "Message text": "Full sender name, text, and date per message"},
        details_text=QA_TELEGRAM_SEED,
        preview_tables=[{
            "headers": ["Sender", "Date", "Message"], "rows": [[QA_PERSON, "2026-07-16", QA_TELEGRAM_SEED]],
        }],
        table_only=True,
        allow_accept_all=True,
        connector="telegram",
    ))

    results.append(run(
        "RG-1 · salesforce_get_record",
        click_title="Allow once", expected="accept",
        title="Read Salesforce Record",
        preview={"Object type": "Account", "Record ID": "001QA0000012345"},
        new_info={"Name": QA_ACCOUNT, "Field values": "Industry, Name"},
        details_text=f"Name: {QA_ACCOUNT}\nIndustry: (not set)",
        preview_tables=[{
            "headers": ["Field", "Value"], "rows": [["Industry", "Technology"], ["Name", QA_ACCOUNT]],
        }],
        table_only=True,
        allow_accept_all=True,
        connector="salesforce",
    ))

    results.append(run(
        "RG-1 · salesforce_run_report",
        click_title="Allow once", expected="accept",
        title="Run Salesforce Report",
        preview={"Report": QA_REPORT, "Report ID": "00OQA0000006789"},
        new_info={"Report data": "All report rows/aggregates"},
        details_text="1 row, 1 grouping -- synthetic PrivacyFence QA report output.",
        preview_tables=[{
            "headers": ["Account Name", "Amount"], "rows": [[QA_ACCOUNT, "$1,000"]],
            "footer": "Total: $1,000",
        }],
        table_only=True,
        allow_accept_all=True,
        connector="salesforce",
    ))

    results.append(run(
        # Also the Deny-click mechanic -- confirms Deny still resolves
        # correctly on an RG-1-shaped popup, not just the write-side one.
        "RG-1 · salesforce_search (Deny)",
        click_title="Deny", expected="deny",
        title="Search Salesforce",
        preview={"Search term": "PrivacyFence QA", "Object types": "Account"},
        new_info={"Results": "2", "Search results": "Object type, Name, and id per match"},
        details_text=f"{QA_ACCOUNT}\nPrivacyFence QA — Globex Test Co [QATEST]",
        preview_tables=[{
            "headers": ["Object type", "Name", "ID"],
            "rows": [
                ["Account", QA_ACCOUNT, "001QA0000012345"],
                ["Account", "PrivacyFence QA — Globex Test Co [QATEST]", "001QA0000067890"],
            ],
        }],
        table_only=True,
        allow_accept_all=True,
        connector="salesforce",
    ))

    # ================================================================== #
    # RG-1 stress -- same 9 dialog shapes above, but with long text, many
    # rows/columns, or both, each as a long/no-PII + long/PII pair (holding
    # content length constant, varying only the PII banner) so a
    # readability problem can be attributed to one specific cause rather
    # than several changing at once. See QA_LONG_PARAGRAPH's own comment.
    # ================================================================== #

    results.append(run(
        "RG-1 · calendar_get_event_details (long description, many attendees, no PII)",
        click_title="Allow once", expected="accept",
        title="Read Calendar Event",
        preview={"Title": QA_EVENT, "Time": QA_EVENT_TIME},
        new_info={
            "Attendees": QA_MANY_ATTENDEES, "Location": "Remote",
            "Description": QA_LONG_PARAGRAPH,
        },
        details_text=f"Attendees: {QA_MANY_ATTENDEES}\n\nDescription:\n{QA_LONG_PARAGRAPH}",
        claude_reason="Checking the QA event details as requested.",
        allow_accept_all=True,
        connector="calendar",
    ))

    results.append(run(
        "RG-1 · calendar_get_event_details (long description, many attendees, PII)",
        click_title="Allow once", expected="accept",
        title="Read Calendar Event",
        preview={"Title": QA_EVENT, "Time": QA_EVENT_TIME},
        new_info={
            "Attendees": QA_MANY_ATTENDEES, "Location": "Remote",
            "Description": f"{QA_LONG_PARAGRAPH} Call {QA_PHONE} to confirm attendance.",
        },
        details_text=f"Attendees: {QA_MANY_ATTENDEES}\n\nDescription:\n"
                      f"{QA_LONG_PARAGRAPH} Call {QA_PHONE} to confirm attendance.",
        claude_reason="Checking the QA event details as requested.",
        allow_accept_all=True,
        pii_categories=["Phone number"],
        connector="calendar",
    ))

    results.append(run(
        "RG-1 · jira_get_issue (long description, many comments, no PII)",
        click_title="Allow once", expected="accept",
        title="Read Jira Issue",
        preview={
            "Project": QA_PROJECT, "Key": QA_JIRA_KEY, "Summary": QA_JIRA_SUMMARY,
            "Status": "To Do", "Assignee": "Unassigned",
        },
        new_info={
            "Description": "Full description text",
            "Comments": "Author, created date, and body per comment",
        },
        details_text=f"Reporter: {QA_PERSON}\n\nDescription:\n{QA_LONG_PARAGRAPH}",
        preview_blocks=[
            {"type": "field", "label": "Reporter", "value": QA_PERSON},
            {"type": "heading", "label": "Description"},
            {"type": "text", "text": QA_LONG_PARAGRAPH},
            {
                "type": "table", "caption": f"Comments ({len(QA_MANY_COMMENTS)})",
                "headers": ["Author", "Date", "Comment"], "rows": QA_MANY_COMMENTS,
            },
        ],
        allow_accept_all=True,
        connector="jira",
    ))

    results.append(run(
        "RG-1 · jira_get_issue (long description, many comments, PII)",
        click_title="Allow once", expected="accept",
        title="Read Jira Issue",
        preview={
            "Project": QA_PROJECT, "Key": QA_JIRA_KEY, "Summary": QA_JIRA_SUMMARY,
            "Status": "To Do", "Assignee": "Unassigned",
        },
        new_info={
            "Description": "Full description text",
            "Comments": "Author, created date, and body per comment",
        },
        details_text=f"Reporter: {QA_PERSON}\n\nDescription:\n{QA_LONG_PARAGRAPH} Reach the reporter at {QA_PHONE}.",
        preview_blocks=[
            {"type": "field", "label": "Reporter", "value": QA_PERSON},
            {"type": "heading", "label": "Description"},
            {"type": "text", "text": f"{QA_LONG_PARAGRAPH} Reach the reporter at {QA_PHONE}."},
            {
                "type": "table", "caption": f"Comments ({len(QA_MANY_COMMENTS)})",
                "headers": ["Author", "Date", "Comment"], "rows": QA_MANY_COMMENTS,
            },
        ],
        allow_accept_all=True,
        pii_categories=["Phone number"],
        connector="jira",
    ))

    results.append(run(
        "RG-1 · confluence_get_page (long body, no PII)",
        click_title="Allow once", expected="accept",
        title="Read Confluence Page",
        preview={"Title": QA_PAGE, "Space": QA_SPACE},
        new_info={
            "Author": QA_PERSON, "Last modified": "2026-07-16", "Page body": "Full page content",
        },
        details_text=(QA_LONG_PARAGRAPH + "\n\n") * 3 + "The final line, still present.",
        allow_accept_all=True,
        connector="confluence",
    ))

    results.append(run(
        "RG-1 · confluence_get_page (long body, PII)",
        click_title="Allow once", expected="accept",
        title="Read Confluence Page",
        preview={"Title": QA_PAGE, "Space": QA_SPACE},
        new_info={
            "Author": QA_PERSON, "Last modified": "2026-07-16", "Page body": "Full page content",
        },
        details_text=(QA_LONG_PARAGRAPH + "\n\n") * 3 + f"Contact {QA_PERSON} at {QA_PHONE} with questions.",
        allow_accept_all=True,
        pii_categories=["Phone number"],
        connector="confluence",
    ))

    results.append(run(
        "RG-1 · telegram_get_messages (many long messages, no PII)",
        click_title="Allow once", expected="accept",
        title="Read Telegram Messages",
        preview={"Chat": "Saved Messages"},
        new_info={
            "Messages": str(len(QA_MANY_TELEGRAM_ROWS)),
            "Message text": "Full sender name, text, and date per message",
        },
        details_text="\n".join(f"[{r[1]}] {r[0]}: {r[2]}" for r in QA_MANY_TELEGRAM_ROWS),
        preview_tables=[{"headers": ["Sender", "Date", "Message"], "rows": QA_MANY_TELEGRAM_ROWS}],
        table_only=True,
        allow_accept_all=True,
        connector="telegram",
    ))

    results.append(run(
        "RG-1 · telegram_get_messages (many long messages, PII)",
        click_title="Allow once", expected="accept",
        title="Read Telegram Messages",
        preview={"Chat": "Saved Messages"},
        new_info={
            "Messages": str(len(QA_MANY_TELEGRAM_ROWS) + 1),
            "Message text": "Full sender name, text, and date per message",
        },
        details_text="\n".join(f"[{r[1]}] {r[0]}: {r[2]}" for r in QA_MANY_TELEGRAM_ROWS)
                      + f"\n[2026-07-16T09:00:00Z] {QA_PERSON}: Call me at {QA_PHONE} when you land.",
        preview_tables=[{
            "headers": ["Sender", "Date", "Message"],
            "rows": QA_MANY_TELEGRAM_ROWS + [[QA_PERSON, "2026-07-16T09:00:00Z", f"Call me at {QA_PHONE} when you land."]],
        }],
        table_only=True,
        allow_accept_all=True,
        pii_categories=["Phone number"],
        connector="telegram",
    ))

    results.append(run(
        "RG-1 · salesforce_get_record (many fields, no PII)",
        click_title="Allow once", expected="accept",
        title="Read Salesforce Record",
        preview={"Object type": "Account", "Record ID": "001QA0000012345"},
        new_info={
            "Name": QA_ACCOUNT, "Field values": ", ".join(sorted(QA_MANY_SALESFORCE_FIELDS)),
        },
        details_text="Fields:\n" + "\n".join(f"{k}: {v}" for k, v in sorted(QA_MANY_SALESFORCE_FIELDS.items())),
        preview_tables=[{
            "headers": ["Field", "Value"],
            "rows": [[k, QA_MANY_SALESFORCE_FIELDS[k]] for k in sorted(QA_MANY_SALESFORCE_FIELDS)],
        }],
        table_only=True,
        allow_accept_all=True,
        connector="salesforce",
    ))

    results.append(run(
        "RG-1 · salesforce_get_record (many fields, PII)",
        click_title="Allow once", expected="accept",
        title="Read Salesforce Record",
        preview={"Object type": "Account", "Record ID": "001QA0000012345"},
        new_info={
            "Name": QA_ACCOUNT, "Field values": ", ".join(sorted(QA_MANY_SALESFORCE_FIELDS)),
        },
        details_text="Fields:\n" + "\n".join(f"{k}: {v}" for k, v in sorted(QA_MANY_SALESFORCE_FIELDS.items())),
        preview_tables=[{
            "headers": ["Field", "Value"],
            "rows": [[k, QA_MANY_SALESFORCE_FIELDS[k]] for k in sorted(QA_MANY_SALESFORCE_FIELDS)],
        }],
        table_only=True,
        allow_accept_all=True,
        pii_categories=["Phone number"],
        connector="salesforce",
    ))

    results.append(run(
        "RG-1 · salesforce_run_report (many columns and rows, no PII)",
        click_title="Allow once", expected="accept",
        title="Run Salesforce Report",
        preview={"Report": QA_REPORT, "Report ID": "00OQA0000006789"},
        new_info={"Report data": "All report rows/aggregates"},
        details_text=f"{len(QA_MANY_SALESFORCE_ROWS)} rows, 1 grouping -- synthetic PrivacyFence QA report output.",
        preview_tables=[{
            "headers": ["Account Name", "Stage", "Amount", "Close Date", "Owner"],
            "rows": QA_MANY_SALESFORCE_ROWS,
            "footer": "Total: $550,000",
        }],
        table_only=True,
        allow_accept_all=True,
        connector="salesforce",
    ))

    results.append(run(
        "RG-1 · salesforce_run_report (many columns and rows, PII)",
        click_title="Allow once", expected="accept",
        title="Run Salesforce Report",
        preview={"Report": QA_REPORT, "Report ID": "00OQA0000006789"},
        new_info={"Report data": "All report rows/aggregates"},
        details_text=f"{len(QA_MANY_SALESFORCE_ROWS)} rows, 1 grouping -- synthetic PrivacyFence QA report output.",
        preview_tables=[{
            "headers": ["Account Name", "Stage", "Amount", "Close Date", "Owner"],
            "rows": QA_MANY_SALESFORCE_ROWS,
            "footer": "Total: $550,000",
        }],
        table_only=True,
        allow_accept_all=True,
        pii_categories=["Phone number"],
        connector="salesforce",
    ))

    results.append(run(
        "RG-1 · salesforce_search (many results, no PII)",
        click_title="Allow once", expected="accept",
        title="Search Salesforce",
        preview={"Search term": "PrivacyFence QA", "Object types": "Account"},
        new_info={
            "Results": str(len(QA_MANY_SEARCH_ROWS)), "Search results": "Object type, Name, and id per match",
        },
        details_text="\n".join(f"{r[0]} — {r[1]} (id={r[2]})" for r in QA_MANY_SEARCH_ROWS),
        preview_tables=[{"headers": ["Object type", "Name", "ID"], "rows": QA_MANY_SEARCH_ROWS}],
        table_only=True,
        allow_accept_all=True,
        connector="salesforce",
    ))

    results.append(run(
        "RG-1 · salesforce_search (many results, PII)",
        click_title="Allow once", expected="accept",
        title="Search Salesforce",
        preview={"Search term": "PrivacyFence QA", "Object types": "Account"},
        new_info={
            "Results": str(len(QA_MANY_SEARCH_ROWS)), "Search results": "Object type, Name, and id per match",
        },
        details_text="\n".join(f"{r[0]} — {r[1]} (id={r[2]})" for r in QA_MANY_SEARCH_ROWS),
        preview_tables=[{"headers": ["Object type", "Name", "ID"], "rows": QA_MANY_SEARCH_ROWS}],
        table_only=True,
        allow_accept_all=True,
        pii_categories=["Email address"],
        connector="salesforce",
    ))

    results.append(run(
        "RG-1 · gmail_download_attachment (long subject and filename, no PII)",
        click_title="Allow once", expected="accept",
        title="Download Gmail Attachment",
        preview={
            "From": QA_EMAIL, "Subject": QA_LONG_SUBJECT, "Attachment": QA_LONG_FILENAME,
            "Type": "application/pdf", "Size": "4.2 MB",
        },
        new_info={
            "Content returned to Claude": "None — file bytes are never sent",
            "Will save to": f"~/Downloads/{QA_LONG_FILENAME}",
        },
        details_text=QA_GMAIL_BODY,
        allow_accept_all=True,
        connector="gmail",
    ))

    results.append(run(
        "RG-1 · gmail_download_attachment (long subject and filename, PII)",
        click_title="Allow once", expected="accept",
        title="Download Gmail Attachment",
        preview={
            "From": QA_EMAIL, "Subject": QA_LONG_SUBJECT, "Attachment": QA_LONG_FILENAME,
            "Type": "application/pdf", "Size": "4.2 MB",
        },
        new_info={
            "Content returned to Claude": "None — file bytes are never sent",
            "Will save to": f"~/Downloads/{QA_LONG_FILENAME}",
        },
        details_text=f"{QA_GMAIL_BODY} Call {QA_PHONE} with questions.",
        allow_accept_all=True,
        pii_categories=["Phone number"],
        connector="gmail",
    ))

    results.append(run(
        "RG-1 · drive_download_file (long filename, no PII)",
        click_title="Allow once", expected="accept",
        title="Download Drive File",
        preview={
            "File": QA_LONG_FILENAME, "Owner": QA_EMAIL, "Size": "128.4 MB", "Modified": "2026-07-16",
        },
        new_info={
            "Content returned to Claude": "None — file bytes are never sent",
            "Saved to": f"~/Downloads/{QA_LONG_FILENAME}",
        },
        details_text="Ordinary, non-sensitive smoke-test file content.",
        allow_accept_all=True,
        connector="drive",
    ))

    results.append(run(
        "RG-1 · drive_download_file (long filename, PII)",
        click_title="Allow once", expected="accept",
        title="Download Drive File",
        preview={
            "File": QA_LONG_FILENAME, "Owner": QA_EMAIL, "Size": "128.4 MB", "Modified": "2026-07-16",
        },
        new_info={
            "Content returned to Claude": "None — file bytes are never sent",
            "Saved to": f"~/Downloads/{QA_LONG_FILENAME}",
        },
        details_text=f"Ordinary, non-sensitive smoke-test file content. Contact {QA_PHONE} if corrupted.",
        allow_accept_all=True,
        pii_categories=["Phone number"],
        connector="drive",
    ))

    # ================================================================== #
    # RG-1 (continued) -- same shape as above, plus an "AI will receive"
    # checklist appended to §3.
    # ================================================================== #

    results.append(run(
        # The "kitchen sink" scenario: every row that CAN legally coexist
        # on one dialog, all rendered together -- seen-count, summary box,
        # visibility checklist, PII banner, and Claude's reason (rows 2-5
        # and 7 in docs/approval-window-content-reference.md's anatomy
        # table) -- plus the Always-allow-click mechanic riding along on
        # the same click. Nothing else in this file combines all five
        # cards; the write-side content-flag banner can't appear here at
        # all (review-gate only) -- see that doc's "Cross-cutting" section
        # for exactly which rows are mutually exclusive. This is also the
        # one scenario meant to be captured on its own via --scenario for a
        # README screenshot that shows every card at once.
        "RG-1 · gmail_get_thread (+ reason, seen-count, PII banner, Always allow -- all cards)",
        click_title="Always allow", expected="accept_all",
        title="Read Gmail Thread",
        preview={
            "Subject": QA_GMAIL_SUBJECT, "Participants": QA_EMAIL, "Dates": "2026-07-16 – 2026-07-16",
        },
        new_info={"Messages": "2"},
        details_text=f"From: {QA_EMAIL}\n{QA_GMAIL_BODY} The refund IBAN [QATEST] is attached.\n\n"
                      f"From: {QA_EMAIL}\nSynthetic PrivacyFence QA reply. No real information.",
        preview_blocks=[
            {"type": "heading", "label": "Message 1"},
            {"type": "field", "label": "From", "value": QA_EMAIL},
            {"type": "field", "label": "Date", "value": "2026-07-16"},
            {"type": "text", "text": f"{QA_GMAIL_BODY} The refund IBAN [QATEST] is attached."},
            {"type": "heading", "label": "Message 2"},
            {"type": "field", "label": "From", "value": QA_EMAIL},
            {"type": "field", "label": "Date", "value": "2026-07-16"},
            {"type": "text", "text": "Synthetic PrivacyFence QA reply. No real information."},
        ],
        allow_accept_all=True,
        visibility={"Thread messages": "allow", "Attachments": "block"},
        claude_reason="Checking recent QA thread activity as requested.",
        seen_count=2,
        pii_categories=["IBAN (bank account number)"],
        connector="gmail",
    ))

    results.append(run(
        "RG-1 · drive_sheets_get_values",
        click_title="Allow once", expected="accept",
        title="Read Sheet Values",
        preview={"Spreadsheet": QA_SHEET, "Owner": QA_EMAIL, "Range": "A1:C10"},
        # Real, A1:C10-shaped synthetic cell data -- not a plain sentence --
        # so the right pane's table actually matches what "Range" claims.
        details_text="\n".join(
            ", ".join(row) for row in
            [["Item", "Quantity", "Status"]] + [[f"QA Item {i} [QATEST]", str(i), "OK"] for i in range(1, 10)]
        ),
        preview_tables=[{
            "rows": [["Item", "Quantity", "Status"]] + [[f"QA Item {i} [QATEST]", str(i), "OK"] for i in range(1, 10)],
        }],
        table_only=True,
        allow_accept_all=True,
        visibility={"Cell values": "allow"},
        connector="drive",
    ))

    results.append(run(
        # Also the "kitchen sink" mechanic: Claude's reason + a nonzero
        # seen-count rendered together, alongside the visibility
        # checklist this view already has -- confirms the taller,
        # multi-section window still doesn't shift the button row.
        "RG-1 · slack_get_channel_history (+ reason → seen-count)",
        click_title="Allow once", expected="accept",
        title="Read Slack Channel History",
        preview={"Channel": QA_SLACK_CHANNEL},
        new_info={"Messages": "2"},
        details_text=f"{QA_SLACK_SEED}\n{QA_SLACK_REPLY}",
        preview_tables=[{
            "headers": ["Sender", "Date", "Message"],
            "rows": [[QA_PERSON, "2026-07-16", QA_SLACK_SEED], [QA_PERSON, "2026-07-16", QA_SLACK_REPLY]],
        }],
        table_only=True,
        allow_accept_all=True,
        visibility={"Message text": "allow", "Usernames": "redact"},
        claude_reason="Checking recent QA channel activity as requested.",
        seen_count=2,
        connector="slack",
    ))

    results.append(run(
        "RG-1 · slack_get_thread_replies",
        click_title="Allow once", expected="accept",
        title="Read Slack Thread Replies",
        preview={"Channel": QA_SLACK_CHANNEL},
        new_info={"Replies": "1"},
        details_text=QA_SLACK_REPLY,
        preview_tables=[{
            "headers": ["Sender", "Date", "Message"],
            "rows": [[QA_PERSON, "2026-07-16", QA_SLACK_SEED], [QA_PERSON, "2026-07-16", QA_SLACK_REPLY]],
        }],
        table_only=True,
        allow_accept_all=True,
        visibility={"Message text": "allow", "Usernames": "redact"},
        connector="slack",
    ))

    results.append(run(
        "RG-1 · slack_search_messages",
        click_title="Allow once", expected="accept",
        title="Search Slack Messages",
        preview={"Query": "QATEST"},
        new_info={"Results": "2"},
        details_text=f"{QA_SLACK_SEED}\n{QA_SLACK_REPLY}",
        preview_tables=[{
            "headers": ["Channel", "Sender", "Date", "Message"],
            "rows": [
                [QA_SLACK_CHANNEL, QA_PERSON, "2026-07-16", QA_SLACK_SEED],
                [QA_SLACK_CHANNEL, QA_PERSON, "2026-07-16", QA_SLACK_REPLY],
            ],
        }],
        table_only=True,
        allow_accept_all=True,
        visibility={"Message text": "allow", "Usernames": "redact"},
        connector="slack",
    ))

    # ================================================================== #
    # RG-1 (continued) -- gmail_get_message. content_kind="email" is passed
    # here since gmail.py's real call site still sets it, but it has no
    # rendering effect today -- see docs/approval-window-content-
    # reference.md's row-8 note. This is an ordinary RG-1 dialog.
    # ================================================================== #

    results.append(run(
        # Also the PII banner+badges mechanic -- a realistic combination (a
        # message body that happens to contain a phone number), and a case
        # the design-review pass specifically wanted covered end to end.
        "RG-1 · gmail_get_message (+ email header, + PII banner)",
        click_title="Allow once", expected="accept",
        title="Read Gmail Message",
        preview={"From": QA_EMAIL, "Subject": QA_GMAIL_SUBJECT, "Date": "2026-07-16"},
        new_info={"To": QA_EMAIL, "Labels": "INBOX, IMPORTANT"},
        details_text=f"{QA_GMAIL_BODY} Call me back at 555-0142 [QATEST] to confirm.",
        allow_accept_all=True,
        visibility={"Message body": "allow", "Attachments": "block"},
        content_kind="email",
        pii_categories=["Phone number"],
        connector="gmail",
    ))

    # ================================================================== #
    # RG-2 -- review popup with native PDF body. The one review-gate shape
    # that's still genuinely distinct (see docs/approval-window-content-
    # reference.md's "View groups" section).
    # ================================================================== #

    results.append(run(
        # Also the native-PDFView mechanic.
        "RG-2 · drive_get_file_content (+ PDFView)",
        click_title="Allow once", expected="accept",
        title="Read Drive File Content",
        preview={
            "File": "PrivacyFence QA test file [QATEST].pdf", "Owner": QA_EMAIL,
            "Size": "18 KB", "Modified": "2026-07-16",
        },
        details_text="[binary content — this text should not be visible; the PDFView should be]",
        allow_accept_all=True,
        visibility={"File metadata": "allow", "Document content": "allow"},
        pdf_bytes=_TINY_PDF_BYTES,
        connector="drive",
    ))

    # ================================================================== #
    # WG-1 and WG-2 -- popup-gate, Deny / Allow once (WG-1: never Always
    # allow) or Deny / Allow once / conditionally Always allow (WG-2: 26
    # tools across auto_accept.WRITE_RULE_SUGGESTIONS) -- grouped by
    # connector below rather than split into two contiguous blocks, so
    # each tool's scenario sits next to its sibling tools from the same
    # connector; see each scenario's own "WG-1 ·"/"WG-2 ·" name prefix
    # for which group it's actually in, and
    # docs/always-allow-rules-reference.md for the exact rule each WG-2
    # tool's Always allow proposes. allow_accept_all=True on every WG-2
    # scenario below -- see gate.py's own allow_accept_all = (suggest_
    # write_rule(...) is not None) for why that's unconditional per tool,
    # not a per-scenario author's choice.
    # ================================================================== #

    results.append(run(
        # Also the content-flag banner+badges mechanic.
        "WG-2 · gmail_create_draft (+ content-flag banner)",
        click_title="Allow once", expected="accept",
        title="Create Gmail Draft",
        preview={"To": QA_EMAIL, "Cc": QA_CC_EMAIL, "Subject": f"Re: {QA_GMAIL_SUBJECT}"},
        details_text="Please wire the deposit per the attached IBAN [QATEST].",
        allow_accept_all=True,
        write_content_flags=["IBAN (bank account number)"],
        connector="gmail",
    ))

    results.append(run(
        "WG-2 · gmail_reply_draft",
        click_title="Allow once", expected="accept",
        title="Create Gmail Reply Draft",
        preview={"In reply to": QA_GMAIL_SUBJECT, "To": QA_EMAIL},
        details_text="Synthetic PrivacyFence QA reply draft. No real information.",
        allow_accept_all=True,
        connector="gmail",
    ))

    results.append(run(
        "WG-2 · gmail_reply_all_draft",
        click_title="Allow once", expected="accept",
        title="Create Gmail Reply-All Draft",
        preview={"In reply to": QA_GMAIL_SUBJECT, "To": QA_EMAIL, "Also to": QA_CC_EMAIL},
        details_text="Synthetic PrivacyFence QA reply-all draft. No real information.",
        allow_accept_all=True,
        connector="gmail",
    ))

    results.append(run(
        "WG-2 · gmail_add_label",
        click_title="Allow once", expected="accept",
        title="Add Gmail Label",
        preview={"From": QA_EMAIL, "Subject": QA_GMAIL_SUBJECT, "Label": "QATEST"},
        details_text="Label will be added; no other content changes.",
        allow_accept_all=True,
        connector="gmail",
    ))

    results.append(run(
        "WG-2 · gmail_remove_label",
        click_title="Allow once", expected="accept",
        title="Remove Gmail Label",
        preview={"From": QA_EMAIL, "Subject": QA_GMAIL_SUBJECT, "Label": "QATEST"},
        details_text="Label will be removed; no other content changes.",
        allow_accept_all=True,
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
        details_text="Filter will be created with the criteria and actions above.",
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
        "WG-2 · drive_write_doc_content",
        click_title="Allow once", expected="accept",
        title="Write Google Doc Content",
        preview={"File": QA_DRIVE_DOC, "Owner": QA_EMAIL},
        details_text="Synthetic PrivacyFence QA doc content. No real information.",
        allow_accept_all=True,
        connector="drive",
    ))

    results.append(run(
        # Also the preview_bytes/preview_mime_type image-render mechanic --
        # see the gmail_download_attachment RG-1 scenario above for why.
        "WG-2 · drive_upload_file (+ image preview)",
        click_title="Allow once", expected="accept",
        title="Upload Drive File",
        preview={
            "File": "PrivacyFence QA upload [QATEST].png", "Source": "~/Desktop/qa-smoke-test.png",
            "Size": "1 KB", "Folder": QA_DRIVE_FOLDER,
        },
        details_text="Synthetic PrivacyFence QA upload content. No real information.",
        allow_accept_all=True,
        connector="drive",
        preview_bytes=_TINY_PNG_BYTES, preview_mime_type="image/png",
    ))

    results.append(run(
        # Long-text upload -- the read-side stress scenarios already cover
        # long/many-row content; this is the write-side equivalent for
        # drive_upload_file specifically.
        "WG-2 · drive_upload_file (long text content)",
        click_title="Allow once", expected="accept",
        title="Upload Drive File",
        preview={
            "File": "PrivacyFence QA long upload [QATEST].txt", "Source": "~/Desktop/qa-long-upload.txt",
            "Size": "4 KB", "Folder": QA_DRIVE_FOLDER,
        },
        details_text=(QA_LONG_PARAGRAPH + "\n\n") * 3 + "The final line, still present.",
        allow_accept_all=True,
        connector="drive",
    ))

    results.append(run(
        # QuickLook-fallback mechanic for the *upload* side (see
        # drive_download_file's own QuickLook scenario above for the
        # download-side one) -- a non-image local file with no
        # Drive-generated thumbnail (there can't be one yet; it hasn't
        # been uploaded) falls back to a real quicklookd render of the
        # same checked-in A4 lorem ipsum .docx fixture.
        "WG-2 · drive_upload_file (+ QuickLook preview)",
        click_title="Allow once", expected="accept",
        title="Upload Drive File",
        preview={
            "File": "PrivacyFence QA test doc [QATEST].docx", "Source": "~/Desktop/lorem_ipsum.docx",
            "Size": "36 KB", "Folder": QA_DRIVE_FOLDER,
        },
        details_text="Synthetic lorem ipsum content. No real information. Safe to read, "
                      "upload, or preview by any automated test.",
        allow_accept_all=True,
        connector="drive",
        preview_bytes=_quicklook_thumbnail_for_lorem_ipsum_docx(), preview_mime_type="image/png",
    ))

    results.append(run(
        "WG-2 · drive_write_file_content",
        click_title="Allow once", expected="accept",
        title="Write Drive File Content",
        preview={"File": QA_DRIVE_FILE, "Owner": QA_EMAIL},
        details_text="Synthetic PrivacyFence QA file content. No real information.",
        allow_accept_all=True,
        connector="drive",
    ))

    results.append(run(
        "WG-2 · drive_move_file",
        click_title="Allow once", expected="accept",
        title="Move Drive File",
        preview={"File": QA_DRIVE_FILE, "Owner": QA_EMAIL, "Folder": f"{QA_DRIVE_FOLDER} → Archive [QATEST]"},
        details_text="File will be moved to the new folder; its content is unchanged.",
        allow_accept_all=True,
        connector="drive",
    ))

    results.append(run(
        "WG-2 · drive_sheets_add_sheet",
        click_title="Allow once", expected="accept",
        title="Add Sheet Tab",
        preview={
            "Spreadsheet": QA_SHEET, "Owner": QA_EMAIL, "New tab": "QATEST Sheet2",
            "Size": "26 columns x 1000 rows",
        },
        details_text="A new tab will be added with the settings shown above.",
        allow_accept_all=True,
        connector="drive",
    ))

    results.append(run(
        "WG-2 · drive_sheets_rename_sheet",
        click_title="Allow once", expected="accept",
        title="Rename Sheet Tab",
        preview={
            "Spreadsheet": QA_SHEET, "Owner": QA_EMAIL, "Tab title": "Sheet1 → QATEST renamed",
        },
        details_text="The tab above will be renamed; its contents are unchanged.",
        allow_accept_all=True,
        connector="drive",
    ))

    results.append(run(
        "WG-2 · drive_sheets_delete_dimensions",
        click_title="Allow once", expected="accept",
        title="Delete Sheet Rows/Columns",
        preview={
            "Spreadsheet": QA_SHEET, "Owner": QA_EMAIL, "Tab": "Sheet1",
            "Action": "Delete 2 COLUMNS starting at index 3",
        },
        details_text="Synthetic PrivacyFence QA dimension delete. No real information.",
        allow_accept_all=True,
        connector="drive",
    ))

    results.append(run(
        "WG-1 · slack_send_message",
        click_title="Allow once", expected="accept",
        title="Send Slack Message",
        # "In thread" shows the thread's first message, not the raw
        # timestamp id.
        preview={"Channel": QA_SLACK_CHANNEL, "In thread": QA_SLACK_SEED},
        details_text="Synthetic PrivacyFence QA reply. No real information. [QATEST]",
        allow_accept_all=False,
        connector="slack",
    ))

    results.append(run(
        # Also the content-flag banner+badges mechanic, on a NARROW dialog
        # (gmail_create_draft's own content-flag scenario is WIDE) --
        # confirms the banner/badges render correctly in both shapes.
        "WG-1 · slack_send_message (+ content-flag banner)",
        click_title="Allow once", expected="accept",
        title="Send Slack Message",
        preview={"Channel": QA_SLACK_CHANNEL},
        details_text="Here's the refund IBAN [QATEST].",
        allow_accept_all=False,
        write_content_flags=["IBAN (bank account number)"],
        connector="slack",
    ))

    results.append(run(
        "WG-2 · calendar_create_event",
        click_title="Allow once", expected="accept",
        title="Create Calendar Event",
        preview={
            "Title": "PrivacyFence QA smoke event [QATEST]",
            "Time": "2027-04-01 09:00–09:30 (Europe/Budapest)",
            "Calendar": QA_CALENDAR, "Location": "Remote",
        },
        details_text="Synthetic PrivacyFence QA test event. No real information.",
        allow_accept_all=True,
        connector="calendar",
    ))

    results.append(run(
        # Also the content-flag banner+badges mechanic.
        "WG-2 · calendar_create_event (+ content-flag banner)",
        click_title="Allow once", expected="accept",
        title="Create Calendar Event",
        preview={
            "Title": "PrivacyFence QA dial-in [QATEST]",
            "Time": "2027-04-01 09:00–09:30 (Europe/Budapest)",
            "Calendar": QA_CALENDAR, "Location": "Remote",
        },
        details_text="Dial in with the conference PIN [QATEST] included in this invite.",
        allow_accept_all=True,
        write_content_flags=["PIN/access code"],
        connector="calendar",
    ))

    results.append(run(
        "WG-2 · calendar_update_event",
        click_title="Allow once", expected="accept",
        title="Update Calendar Event",
        # Event/Calendar/Start/End always appear -- old → new only for the
        # fields this call is actually changing (Event's title, Start);
        # Calendar never changes (no destination-calendar param on this
        # tool) and End isn't changing on this call, so both are the plain
        # current value.
        preview={
            "Event": f"{QA_EVENT} → {QA_EVENT} (Rescheduled)",
            "Calendar": QA_CALENDAR,
            "Start": "2027-03-15T10:00:00+01:00 → 2027-03-15T14:00:00+01:00",
            "End": "2027-03-15T11:00:00+01:00",
        },
        details_text="Event, Start will be updated; description is unchanged.",
        allow_accept_all=True,
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
        details_text="Working location will be set as shown above; no other calendar changes.",
        allow_accept_all=False,
        connector="calendar",
    ))

    results.append(run(
        "WG-2 · calendar_set_event_visibility",
        click_title="Allow once", expected="accept",
        title="Set Event Visibility",
        preview={"Event": QA_EVENT, "Calendar": QA_CALENDAR, "Visibility": "default → private"},
        details_text="Only the event's visibility will change; no other fields are affected.",
        allow_accept_all=True,
        connector="calendar",
    ))

    results.append(run(
        "WG-1 · contacts_update",
        click_title="Allow once", expected="accept",
        title="Update Contact",
        # Name/Emails/Phones always appear -- old → new only for the
        # fields this call is actually changing (Emails here); Name/Phones
        # aren't changing on this call, so both are the plain current
        # value.
        preview={
            "Name": QA_CONTACT,
            "Emails": f"{QA_CONTACT_EMAIL} → qatest.updated@example.com",
            "Phones": QA_PHONE,
        },
        details_text="Emails will be updated; notes unchanged.",
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
        # Also the content-flag banner+badges mechanic, on a NARROW dialog.
        "WG-1 · contacts_create (+ content-flag banner)",
        click_title="Allow once", expected="accept",
        title="Create Contact",
        preview={
            "Name": "PrivacyFence QA New Contact [QATEST]", "Emails": "qatest.new@example.com",
            "Phones": "555-0199",
        },
        details_text="Notes: SSN [QATEST] on file from the old contact card.",
        allow_accept_all=False,
        write_content_flags=["SSN (national ID)"],
        connector="contacts",
    ))

    results.append(run(
        "WG-1 · contacts_add_label",
        click_title="Allow once", expected="accept",
        title="Add Contact Label",
        preview={"Name": QA_CONTACT, "Label": "QATEST"},
        details_text="Label will be added to this contact; no other fields change.",
        allow_accept_all=False,
        connector="contacts",
    ))

    results.append(run(
        "WG-1 · contacts_remove_label",
        click_title="Allow once", expected="accept",
        title="Remove Contact Label",
        preview={"Name": QA_CONTACT, "Label": "QATEST"},
        details_text="Label will be removed from this contact; no other fields change.",
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
        "WG-2 · jira_create_issue",
        click_title="Allow once", expected="accept",
        title="Create Jira Issue",
        preview={
            "Project": QA_PROJECT, "Type": "Task", "Summary": "PrivacyFence QA smoke issue [QATEST]",
            "Priority": "Medium",
        },
        details_text="Synthetic PrivacyFence QA test issue. No real information.",
        # v2's right pane: a label-styled "Description" heading above the
        # body, same treatment jira_get_issue's own Description gets.
        preview_blocks=[
            {"type": "heading", "label": "Description"},
            {"type": "text", "text": "Synthetic PrivacyFence QA test issue. No real information."},
        ],
        allow_accept_all=True,
        connector="jira",
    ))

    results.append(run(
        "WG-2 · jira_add_comment",
        click_title="Allow once", expected="accept",
        title="Comment on Jira Issue",
        preview={"Issue": QA_JIRA_KEY},
        details_text="Synthetic PrivacyFence QA comment. No real information. [QATEST]",
        allow_accept_all=True,
        connector="jira",
    ))

    results.append(run(
        # Also the content-flag banner+badges mechanic, on a WIDE dialog
        # (jira_create_issue's own content-flag scenario is a plain preview
        # field, not a details/comment body) -- confirms the banner renders
        # correctly alongside a right-pane details column too.
        "WG-2 · jira_add_comment (+ content-flag banner)",
        click_title="Allow once", expected="accept",
        title="Comment on Jira Issue",
        preview={"Issue": QA_JIRA_KEY},
        details_text="Customer's card is 4111 1111 1111 1111 [QATEST], please refund.",
        allow_accept_all=True,
        write_content_flags=["Card number"],
        connector="jira",
    ))

    results.append(run(
        "WG-2 · jira_update_issue",
        click_title="Allow once", expected="accept",
        title="Update Jira Issue",
        preview={"Issue": QA_JIRA_KEY, "Priority": "Medium → High"},
        details_text="Synthetic PrivacyFence QA issue update. No real information.",
        allow_accept_all=True,
        connector="jira",
    ))

    results.append(run(
        "WG-2 · jira_transition_issue",
        click_title="Allow once", expected="accept",
        title="Transition Jira Issue",
        preview={"Issue": QA_JIRA_KEY, "Status": "To Do → In Progress"},
        details_text="The status change above is the only change; no other fields are affected.",
        allow_accept_all=True,
        connector="jira",
    ))

    results.append(run(
        "WG-2 · confluence_create_page",
        click_title="Allow once", expected="accept",
        title="Create Confluence Page",
        preview={"Space": QA_SPACE, "Title": "PrivacyFence QA smoke page [QATEST]"},
        details_text="Synthetic PrivacyFence QA test page. No real information.",
        allow_accept_all=True,
        connector="confluence",
    ))

    results.append(run(
        # Also the content-flag banner+badges mechanic, on a WIDE dialog.
        "WG-2 · confluence_create_page (+ content-flag banner)",
        click_title="Allow once", expected="accept",
        title="Create Confluence Page",
        preview={"Space": QA_SPACE, "Title": "PrivacyFence QA smoke page [QATEST]"},
        details_text="Runbook step 3: rotate the API key [QATEST] shown on the vendor portal.",
        allow_accept_all=True,
        write_content_flags=["API key"],
        connector="confluence",
    ))

    results.append(run(
        "WG-2 · confluence_update_page",
        click_title="Allow once", expected="accept",
        title="Update Confluence Page",
        preview={"Page ID": "qa-placeholder-id-3", "Space": QA_SPACE, "Title": QA_PAGE},
        details_text=QA_PAGE_BODY,
        allow_accept_all=True,
        connector="confluence",
    ))

    results.append(run(
        "WG-2 · tasks_create_task",
        click_title="Allow once", expected="accept",
        title="Create Task",
        preview={
            "Task list": QA_TASK_LIST, "Title": "PrivacyFence QA smoke task [QATEST]", "Due": "2027-03-20",
        },
        details_text="Synthetic PrivacyFence QA test task notes. No real information.",
        # v2's right pane: a label-styled "Notes" heading above the body,
        # same treatment jira_create_issue's Description gets.
        preview_blocks=[
            {"type": "heading", "label": "Notes"},
            {"type": "text", "text": "Synthetic PrivacyFence QA test task notes. No real information."},
        ],
        allow_accept_all=True,
        connector="tasks",
    ))

    results.append(run(
        "WG-2 · tasks_update_task",
        click_title="Allow once", expected="accept",
        title="Update Task",
        # Task/Due only appear as old → new diffs since they're actually
        # changing on this call; Task list never changes via this tool.
        preview={
            "Task list": QA_TASK_LIST,
            "Task": f"{QA_TASK} → {QA_TASK} (updated)",
            "Due": "2027-03-15 → 2027-03-20",
        },
        details_text="Synthetic PrivacyFence QA test task notes update. No real information.",
        preview_blocks=[
            {"type": "heading", "label": "Notes"},
            {"type": "text", "text": "Synthetic PrivacyFence QA test task notes update. No real information."},
        ],
        allow_accept_all=True,
        connector="tasks",
    ))

    results.append(run(
        "WG-2 · tasks_complete_task",
        click_title="Allow once", expected="accept",
        title="Complete Task",
        preview={"Task list": QA_TASK_LIST, "Task": QA_TASK},
        details_text="Task will be marked as completed; title and notes are unchanged.",
        allow_accept_all=True,
        connector="tasks",
    ))

    results.append(run(
        "WG-2 · tasks_uncomplete_task",
        click_title="Allow once", expected="accept",
        title="Uncomplete Task",
        preview={"Task list": QA_TASK_LIST, "Task": QA_TASK},
        details_text="Task will be marked as not completed; title and notes are unchanged.",
        allow_accept_all=True,
        connector="tasks",
    ))

    results.append(run(
        "WG-2 · tasks_move_task",
        click_title="Allow once", expected="accept",
        title="Move Task",
        preview={"Task": QA_TASK, "List": f"{QA_TASK_LIST} → {QA_CONTRAST_TASK_LIST}"},
        details_text="Task will be moved to the new list; title and notes are unchanged.",
        allow_accept_all=True,
        connector="tasks",
    ))

    # ================================================================== #
    # WG-3 -- popup-gate, Deny / Allow once / conditionally Always allow,
    # *and* the temp-accept disclosure caption (Allow once also arms a
    # 5-minute same-file grace window, disclosed via a caption above the
    # buttons rather than a separate button -- there is no "Allow for 5
    # min" click to test anymore, see gate.py) (6 tools, all also in
    # auto_accept.WRITE_RULE_SUGGESTIONS -- allow_accept_all=True below)
    # ================================================================== #

    results.append(run(
        # The write-side "kitchen sink": every row a popup-gate dialog can
        # legally show, all rendered together -- summary box, seen-count,
        # amber content-flag banner, Claude's reason (rows 1-2 and 6-7 in
        # docs/approval-window-content-reference.md's anatomy table), and
        # the temp-accept disclosure caption riding along on the same
        # Allow-once click. A write never gets the AI-visibility checklist
        # or the red PII banner (review-gate only -- see that doc's
        # "Cross-cutting" section), so this is the actual ceiling for a
        # write dialog: the write-side counterpart to the RG-1
        # gmail_get_thread "all cards" scenario above. Also the one
        # scenario meant to be captured on its own via --scenario for a
        # README screenshot showing a write dialog's full card set.
        "WG-3 · drive_sheets_write_range (+ reason, seen-count, content-flag banner, temp-accept disclosure -- all cards)",
        click_title="Allow once", expected="accept",
        title="Write Sheet Range",
        preview={"Spreadsheet": QA_SHEET, "Owner": QA_EMAIL, "Range": "A1:C10"},
        # Real, A1:C10-shaped synthetic cell data -- not a plain sentence --
        # so the right pane's table actually matches what "Range" claims,
        # same fix and same reasoning as drive_sheets_get_values's own
        # scenario above.
        details_text="\n".join(
            ", ".join(row) for row in
            [["Category", "Q2 Budget", "Actual"]]
            + [[f"QA Line {i} [QATEST]", f"${1000 * i:,.2f}", f"${900 * i:,.2f}"] for i in range(1, 10)]
        ),
        preview_tables=[{
            "rows": (
                [["Category", "Q2 Budget", "Actual"]]
                + [[f"QA Line {i} [QATEST]", f"${1000 * i:,.2f}", f"${900 * i:,.2f}"] for i in range(1, 10)]
            ),
        }],
        table_only=True,
        allow_accept_all=True,
        temp_accept_eligible=True,
        write_content_flags=["Financial figures (currency amounts)"],
        claude_reason="Filling in the QA budget row as requested.",
        seen_count=3,
        connector="drive",
    ))

    results.append(run(
        "WG-3 · drive_sheets_format_range",
        click_title="Allow once", expected="accept",
        title="Format Sheet Range",
        preview={
            "Spreadsheet": QA_SHEET, "Owner": QA_EMAIL, "Range": "A1:C10", "Format": "Bold header row",
        },
        details_text="The formatting above will be applied to the range; other formatting is unchanged.",
        allow_accept_all=True,
        temp_accept_eligible=True,
        connector="drive",
    ))

    results.append(run(
        "WG-3 · drive_sheets_insert_dimensions",
        click_title="Allow once", expected="accept",
        title="Insert Sheet Rows/Columns",
        preview={
            "Spreadsheet": QA_SHEET, "Owner": QA_EMAIL, "Tab": "Sheet1",
            "Action": "Insert 3 ROWS before index 5",
        },
        details_text="Synthetic PrivacyFence QA dimension insert. No real information.",
        allow_accept_all=True,
        temp_accept_eligible=True,
        connector="drive",
    ))

    results.append(run(
        "WG-3 · drive_add_comment",
        click_title="Allow once", expected="accept",
        title="Add Drive Comment",
        preview={"File": QA_DRIVE_FILE, "Owner": QA_EMAIL},
        details_text="Synthetic PrivacyFence QA comment. No real information. [QATEST]",
        allow_accept_all=True,
        temp_accept_eligible=True,
        connector="drive",
    ))

    results.append(run(
        "WG-3 · drive_docs_edit_content",
        click_title="Allow once", expected="accept",
        title="Edit Google Doc Content",
        preview={"File": QA_DRIVE_DOC, "Owner": QA_EMAIL, "Match": "the one matching occurrence"},
        details_text="Synthetic PrivacyFence QA doc edit. No real information.",
        allow_accept_all=True,
        temp_accept_eligible=True,
        connector="drive",
    ))

    results.append(run(
        "WG-3 · drive_docs_format_content",
        click_title="Allow once", expected="accept",
        title="Format Google Doc Content",
        preview={"File": QA_DRIVE_DOC, "Owner": QA_EMAIL, "Format": "Italic selection"},
        details_text="Synthetic PrivacyFence QA doc formatting. No real information.",
        allow_accept_all=True,
        temp_accept_eligible=True,
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
    # rather than leaving them for whatever runs after it. Neither an "RG-"
    # nor a "WG-" scenario, so a --group rg/wg run skips it entirely, same
    # as every other group filter above.
    # ================================================================== #
    menu_bar_name = "Menu bar · status item → Manage Auto-accept Rules… window"
    if group_prefix is None and (only_lower is None or only_lower in menu_bar_name.lower()):
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
             "match against the scenario name shown in the report table, e.g. 'gmail_get_thread' or "
             "'Menu bar' for the menu-bar/rules-window scenario), instead of the full ~83-scenario "
             "suite (82 tool-approval scenarios plus the one menu-bar scenario). For grabbing a "
             "single updated screenshot -- e.g. for README.md -- without sitting through the whole "
             "run: --scenario 'gmail_get_thread' --screenshot-dir docs/images/screenshots. "
             "Combines with --group (both must match). Matches nothing -> an empty report and a "
             "nonzero exit code, same as any other all-failed run.",
    )
    parser.add_argument(
        "--group", choices=["all", "rg", "wg"], default="all",
        help="'all' (default): every scenario. 'rg': review-gate (read) scenarios only -- those "
             "whose name starts with 'RG-', per docs/approval-window-content-reference.md's view "
             "groups. 'wg': popup-gate (write) scenarios only ('WG-' prefix). Either excludes the "
             "menu-bar scenario, which is neither. Combines with --scenario (both must match) -- "
             "e.g. --group rg --scenario gmail to see only Gmail's read-side dialogs.",
    )
    args = parser.parse_args()

    results: list[ScenarioResult] = []
    exit_code = 0

    def work() -> None:
        nonlocal exit_code
        try:
            if args.screenshot_dir is not None:
                args.screenshot_dir.mkdir(parents=True, exist_ok=True)
            results.extend(_scenarios(
                args.pause_seconds, args.screenshot_dir, args.scenario,
                group=args.group,
            ))
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
