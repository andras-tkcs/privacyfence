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
RG-1/RG-2/WG-1/WG-2/WG-3 tables (67 tools total, including every RG-1 tool sharing a dialog
shape, e.g. confluence_get_page/confluence_get_page_by_title) -- every dialog shape that doc
documents gets a real on-screen click, not just a representative handful. (RG-2 is the one shape
that's genuinely distinct from RG-1's, the native-PDF body -- see that doc's "View groups"
section.) A handful of RG-1 tools additionally get two "RG-1 stress" readability variants (long
text/many rows/columns, with and without a PII banner) beyond their one baseline entry -- see the
"RG-1 stress" section below for why. Each of the four multi-candidate operations in
auto_accept.SUGGESTION_FAMILIES (drive reads, calendar_get_event_details, jira_get_issue,
confluence reads) additionally gets one "(N Always-allow candidates)" variant beyond its
single-candidate baseline -- issue #151's multi-button window, showing 2+ real "Always allow"
buttons rendered in their own row instead of one hinted button (see approval_window_html.py's
_button_row_html); every other RG-1/RG-2 tool's baseline entry itself already covers the
single-candidate (0 or 1 button) case with no dedicated variant needed. Preview/details data is
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

Seven more, non-tool scenarios run last: the actual tray status item and, from it, the settings
window issue #120 replaced the old NSMenu tree / "Manage Auto-accept Rules…" native window with
(see the "Settings window" scenarios near the bottom of _scenarios()) -- exercising real clicks
into that window's own web content, the same web-content-click technique issue #141 brought to
every tool-approval scenario's own Deny/Allow once/Always allow above (see _click_button's own
docstring). 104 scenarios total: 97 tool-approval scenarios plus these seven.

Every tool-approval scenario renders through the one real card-stack rendering
(approval_window_html.py). Each scenario's narrow/wide shape (_TOOL_LAYOUT below) is a fixed,
explicit per-tool assignment, documented inline at _TOOL_LAYOUT and kept in sync with gate.py's own
copy of the same table (see gate.py's own _TOOL_LAYOUT comment).

Usage (the project's own venv, not a bare system python3 -- this needs the
same pyobjc/AppKit packages the app itself depends on, which only the venv
has installed):
    .venv/bin/python scripts/qa_popup_smoke.py
    .venv/bin/python scripts/qa_popup_smoke.py --report-file /tmp/popup_smoke.md
    .venv/bin/python scripts/qa_popup_smoke.py --pause-seconds 3   # slow down to actually look
    .venv/bin/python scripts/qa_popup_smoke.py --screenshot-dir /tmp/popup_smoke_shots
    # One scenario only, e.g. to refresh a single README.md screenshot -- the three screenshots
    # README.md actually uses (as of this writing) predate issue #120's settings window and still
    # show the old menu bar/native "Manage Auto-accept Rules…" window; refreshing those captions/
    # images is a separate, not-yet-done pass, out of scope for this file. The two tool-approval
    # examples below are still accurate as-is; a third example, for the new settings window's own
    # "does the tray open it" screenshot, is the one genuinely new one:
    .venv/bin/python scripts/qa_popup_smoke.py --scenario "gmail_get_thread" \\
        --screenshot-dir docs/images/screenshots --pause-seconds 3
    .venv/bin/python scripts/qa_popup_smoke.py --scenario "drive_sheets_write_range" \\
        --screenshot-dir docs/images/screenshots --pause-seconds 3
    .venv/bin/python scripts/qa_popup_smoke.py --scenario "status item → window opens" \\
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
import yaml  # noqa: E402
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

from privacyfence import daemon_main, menu_bar  # noqa: E402
from privacyfence.approval_window import show_native_approval  # noqa: E402
from privacyfence.auto_accept import (  # noqa: E402
    TOOL_TO_OPERATION,
    WRITE_RULE_SUGGESTIONS,
    describe_rule_short,
)
from privacyfence.text_extraction import extract_text, preview_blocks_for  # noqa: E402

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
    """Block until a Deny/Allow once/Always allow element with this exact
    displayed text exists AND is enabled on our own process's first window
    -- returns "ready", "BUTTON_NOT_FOUND" (no such element ever appeared),
    or "TIMEOUT_BUTTON_DISABLED" (it exists but never became enabled within
    WINDOW_WAIT_TIMEOUT_SECONDS).

    Issue #141 moved Deny/Allow once/Always allow off native NSButtons and
    into the same card-stack WKWebView content everything else in the
    approval window renders (role="button", aria-disabled toggled by the
    page's own DOMContentLoaded handling -- see approval_window_html.py's
    ``_button_row_html``/``_JS``) -- so this now walks `entire contents of
    window 1` the same way _wait_for_web_element (below, originally written
    for the *settings*-window's own web content) already does, rather than
    addressing a native `button "{title}" of window 1`. Title is tried
    first, description second, same unverified-on-real-hardware fallback
    reasoning as that section's own header comment -- these buttons carry
    ``aria-label`` (see _button_row_html), which WebKit may map to either
    depending on OS/WebKit version, same open question as every other
    aria-label lookup in this file.

    v2's Deny/Allow once/Always allow start disabled -- and the panel
    itself starts fully transparent (alphaValue 0) -- and only become
    enabled/visible once the card-stack webview finishes loading (see
    approval_window.py's webView_didFinishNavigation_ -- loadHTMLString_
    baseURL_ is asynchronous even for local content). This is the actual
    "the popup is ready" signal, distinct from _wait_for_window()'s "the
    window exists" -- System Events' accessibility tree lists the NSPanel
    (and passes _wait_for_window) the instant it's created and ordered
    front, regardless of its alphaValue, well before the webview has
    painted anything, so a screenshot taken right after _wait_for_window
    alone would capture an empty, still-invisible panel depending on how
    fast the machine happens to render that run -- not reliably
    reproducible, and no --pause-seconds value fixes it for certain, only
    makes the race less likely to lose. Called both by the screenshot step
    (clicker(), below) and by _click_button() before it actually clicks, so
    neither can act on a stale window state.
    """
    script = f'''
    tell application "System Events"
        set targetProcess to first process whose unix id is {pid}
        tell targetProcess
            set deadlineTime to (current date) + {WINDOW_WAIT_TIMEOUT_SECONDS}
            repeat
                set matches to (every UI element of (entire contents of window 1) whose title is "{title}")
                if (count of matches) = 0 then
                    set matches to (every UI element of (entire contents of window 1) whose description is "{title}")
                end if
                if (count of matches) > 0 then
                    if (enabled of item 1 of matches) then return "ready"
                else if (current date) > deadlineTime then
                    return "BUTTON_NOT_FOUND"
                end if
                if (current date) > deadlineTime then return "TIMEOUT_BUTTON_DISABLED"
                delay 0.1
            end repeat
        end tell
    end tell
    '''
    return _run_applescript(script)


def _click_button(pid: int, title: str) -> str:
    """Click a Deny/Allow once/Always allow element on our own process's
    first window by its exact displayed text -- returns "clicked",
    "BUTTON_NOT_FOUND"/"TIMEOUT_BUTTON_DISABLED" (see
    _wait_for_button_enabled), or an osascript-level error string. Assumes
    the window already exists (call _wait_for_window() first).

    See _wait_for_button_enabled's own docstring for why this walks
    `entire contents of window 1` (web content) rather than addressing a
    native `button "{title}" of window 1` -- same title-then-description
    fallback, same unverified-on-real-hardware status.

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
            set matches to (every UI element of (entire contents of window 1) whose title is "{title}")
            if (count of matches) = 0 then
                set matches to (every UI element of (entire contents of window 1) whose description is "{title}")
            end if
            if (count of matches) = 0 then return "BUTTON_NOT_FOUND"
            click item 1 of matches
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


# ---------------------------------------------------------------------------- #
# Settings-window (webview) content -- issue #120's settings_window_html.py
# renders every clickable element as a plain <div> with role="button"/"tab"/
# "radio"/"switch"/"checkbox" and a stable aria-label (added specifically so
# this script can address WKWebView content the same way _click_button
# above addresses one -- see that module's own accessibility-pass comments).
# _click_button/_wait_for_button_enabled above use this exact same
# `entire contents of window 1` technique now too (issue #141 moved
# approval_window.py's own Deny/Allow once/Always allow off native
# NSButtons and into its card-stack webview's content, the same way
# settings_window_html.py's controls already were) -- this section's own
# helpers (_click_web_element/_wait_for_web_element/etc.) stayed separate
# rather than merging the two into one shared helper, since their aria-label
# vocabularies serve genuinely different content (this section's own
# toggle/radio/tab roles have no approval-window equivalent). System Events
# can, in general, walk into a WKWebView's accessibility tree the same way
# it walks a window's native subviews, but unlike a native NSButton (a
# direct child of the window, addressable as `button "title" of window 1`),
# a web-content element sits several levels deep under an AXWebArea --
# `entire contents of window 1` is the standard AppleScript idiom for a
# recursive search that reaches it regardless of nesting depth.
#
# UNVERIFIED ON REAL HARDWARE, same limitation as every other honesty note
# in this file's module docstring applies here too, doubly so: this was the
# first WKWebView-content UI-scripting attempt in this repo (approval_
# window.py's own webview was still display-only and native-button-driven,
# never addressed via System Events, until issue #141), so there was no
# working precedent to copy exactly when this section was first written.
# The two known open questions this can't resolve without an
# actual run: (1) whether WebKit populates an ARIA `aria-label` into the AX
# element's `title` or its `description` for a given role (this repo's own
# testing suggests it varies by mapped role -- e.g. a button's accessible
# name is typically exposed as title, a checkbox/radio's more often as
# description -- hence the two-strategy fallback below, not a single
# lookup), and (2) whether `entire contents` actually descends into
# WKWebView's tree at all on the OS/WebKit version this runs against, or
# whether reaching web content needs an explicit `UI elements of group 1 of
# window 1`-style path instead. If scenarios below fail with
# WEB_ELEMENT_NOT_FOUND on a real run, start by using Accessibility
# Inspector.app (Xcode's) on the settings window to see exactly what AX
# tree WebKit is actually exposing, then adjust these two helpers -- not
# the individual scenario functions, which should never need to know this
# level of detail.
# ---------------------------------------------------------------------------- #

def _wait_for_web_element(pid: int, aria_label: str) -> str:
    """Block until a web-content element with this aria-label exists inside
    our own process's first window -- returns "ready" or
    "TIMEOUT_NO_WEB_ELEMENT". See this section's own header comment for the
    title-vs-description fallback and its unverified status."""
    script = f'''
    tell application "System Events"
        set targetProcess to first process whose unix id is {pid}
        tell targetProcess
            set deadlineTime to (current date) + {WINDOW_WAIT_TIMEOUT_SECONDS}
            repeat
                set matches to (every UI element of (entire contents of window 1) whose title is "{aria_label}")
                if (count of matches) = 0 then
                    set matches to (every UI element of (entire contents of window 1) whose description is "{aria_label}")
                end if
                if (count of matches) > 0 then return "ready"
                if (current date) > deadlineTime then return "TIMEOUT_NO_WEB_ELEMENT"
                delay 0.1
            end repeat
        end tell
    end tell
    '''
    return _run_applescript(script)


def _click_web_element(pid: int, aria_label: str) -> str:
    """Click a settings-window web-content element by its aria-label --
    returns "clicked", "TIMEOUT_NO_WEB_ELEMENT", or an osascript-level
    error string. See this section's own header comment."""
    wait_status = _wait_for_web_element(pid, aria_label)
    if wait_status != "ready":
        return wait_status
    script = f'''
    tell application "System Events"
        set targetProcess to first process whose unix id is {pid}
        tell targetProcess
            set matches to (every UI element of (entire contents of window 1) whose title is "{aria_label}")
            if (count of matches) = 0 then
                set matches to (every UI element of (entire contents of window 1) whose description is "{aria_label}")
            end if
            if (count of matches) = 0 then return "WEB_ELEMENT_NOT_FOUND"
            click item 1 of matches
        end tell
    end tell
    return "clicked"
    '''
    return _run_applescript(script)


def _web_element_value(pid: int, aria_label: str) -> str:
    """AX `value` of a web-content element by its aria-label -- for a
    role="switch"/"radio"/"checkbox" element this is WebKit's mapping of
    its aria-checked state (typically "1"/"0" or "true"/"false" depending
    on OS version -- UNVERIFIED which, see this section's header comment),
    used by the PII-sub-toggle scenario below to confirm a visual on/off
    state, not just that a click landed. Returns the AX value as a string,
    or "WEB_ELEMENT_NOT_FOUND"/an osascript-level error string."""
    script = f'''
    tell application "System Events"
        set targetProcess to first process whose unix id is {pid}
        tell targetProcess
            set matches to (every UI element of (entire contents of window 1) whose title is "{aria_label}")
            if (count of matches) = 0 then
                set matches to (every UI element of (entire contents of window 1) whose description is "{aria_label}")
            end if
            if (count of matches) = 0 then return "WEB_ELEMENT_NOT_FOUND"
            return (value of item 1 of matches) as string
        end tell
    end tell
    '''
    return _run_applescript(script)


def _set_web_element_text(pid: int, aria_label: str, text: str) -> str:
    """Focus a settings-window text input by aria-label, select-all, type
    replacement text, then Tab away to blur it (settings_window_html.py's
    inputs commit on blur/Enter, not per keystroke -- see that module's own
    docstring) -- returns "typed" or a failure status. Uses `keystroke`,
    not AX's `value of` setter, so this exercises the same real keyboard
    event path a human typing would, matching every other interaction in
    this script (System-Events-driven, no mocking)."""
    wait_status = _wait_for_web_element(pid, aria_label)
    if wait_status != "ready":
        return wait_status
    script = f'''
    tell application "System Events"
        set targetProcess to first process whose unix id is {pid}
        tell targetProcess
            set matches to (every UI element of (entire contents of window 1) whose title is "{aria_label}")
            if (count of matches) = 0 then
                set matches to (every UI element of (entire contents of window 1) whose description is "{aria_label}")
            end if
            if (count of matches) = 0 then return "WEB_ELEMENT_NOT_FOUND"
            set focused of item 1 of matches to true
            keystroke "a" using command down
            keystroke "{text}"
            keystroke tab
        end tell
    end tell
    return "typed"
    '''
    return _run_applescript(script)


def _web_element_enabled(pid: int, aria_label: str) -> str:
    """AX `enabled` of a web-content element by its aria-label -- "true"/
    "false" (AppleScript's boolean-to-string coercion), or
    "WEB_ELEMENT_NOT_FOUND"/an osascript-level error string. Distinct from
    _web_element_value() above: that reads aria-checked (on/off), this reads
    whether the element is interactive at all -- used by the PII sub-toggle
    scenario below to confirm the *disabled* visual state a dimmed
    (opacity-only CSS, not a native `disabled` attribute -- there is none
    for a plain <div>) sub-toggle is supposed to carry. See
    settings_window_html.py's toggleHtml(): a disabled toggle renders
    `aria-disabled="true"` and omits `tabindex`/`data-action` entirely,
    rather than anything a browser's own disabled-input semantics would
    normally give WebKit's accessibility mapping to key off of for free.

    UNVERIFIED, same as every other AX-mapping claim in this section's own
    header comment: whether System Events' generic `enabled` property
    actually reflects WebKit's mapping of `aria-disabled` for a
    role="switch" element it exposes (as opposed to, say, always reporting
    true for any element WebKit exposes at all, since `aria-disabled`
    doesn't remove an element from the accessibility tree the way a truly
    native disabled control would) is exactly the kind of thing this
    section's header comment says needs Accessibility Inspector.app to
    confirm on a real run, not asserted here.
    """
    script = f'''
    tell application "System Events"
        set targetProcess to first process whose unix id is {pid}
        tell targetProcess
            set matches to (every UI element of (entire contents of window 1) whose title is "{aria_label}")
            if (count of matches) = 0 then
                set matches to (every UI element of (entire contents of window 1) whose description is "{aria_label}")
            end if
            if (count of matches) = 0 then return "WEB_ELEMENT_NOT_FOUND"
            return (enabled of item 1 of matches) as string
        end tell
    end tell
    '''
    return _run_applescript(script)


def _wait_for_disk_config(
    config_path: str, predicate: Callable[[dict], bool], timeout: float = WINDOW_WAIT_TIMEOUT_SECONDS,
) -> dict | None:
    """Poll `config_path` (a settings.yaml this scenario group owns -- see
    QA_SETTINGS_WINDOW_SETTINGS_YAML below) until its parsed contents
    satisfy `predicate`, or `timeout` elapses. Returns the satisfying config
    dict, or None on timeout.

    A Python-side poll, not an AppleScript one like every _wait_for_* above
    -- what's being waited on here is this same process writing a file
    (SettingsController._save_config(), invoked synchronously from
    userContentController_didReceiveScriptMessage_ on the main thread once
    WebKit's own JS -> native message delivery reaches it -- see
    settings_window.py's own module docstring for that bridge), not
    real user-facing UI state, so there is nothing for System Events to
    watch here. Still a poll rather than a single read-after-sleep: that
    JS -> native message delivery has no synchronous completion signal this
    script can observe from the calling thread, so a fixed sleep would be
    exactly the kind of race _wait_for_button_enabled's own docstring warns
    about for approval popups.
    """
    deadline = time.time() + timeout
    while True:
        try:
            cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            cfg = {}
        if predicate(cfg):
            return cfg
        if time.time() > deadline:
            return None
        time.sleep(0.1)


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
    # to drive_upload_file. Mirrors exactly what gate.py itself does in
    # production (its own _TOOL_LAYOUT is this same table).
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

    # A bare click_title="Always allow" only matches the real on-screen
    # button when accept_all_hint is empty -- once one is set (by either
    # branch above, or by a scenario setting accept_all_hint itself), the
    # button's actual title includes it (see approval_window.py's
    # _action_buttons: f"Always allow — {hint}"). Keep the two in sync
    # here instead of requiring every such scenario to hardcode the exact
    # hinted string, which would silently drift from _RULE_SHORT_HINTS.
    if click_title == "Always allow" and popup_kwargs.get("accept_all_hint"):
        click_title = f"Always allow — {popup_kwargs['accept_all_hint']}"

    # show_native_approval() itself no longer takes allow_accept_all/
    # accept_all_hint (issue #151's multi-button rewrite) -- it takes
    # accept_all_choices: list[(rule_name, short_label)], one entry per
    # matching candidate. Translated here, once, rather than touching every
    # individual scenario call below: allow_accept_all=True + a single
    # accept_all_hint (the overwhelming majority of scenarios -- one
    # candidate) becomes a one-entry list, same visual/behavioral result as
    # the old allow_accept_all=True/accept_all_hint pair. A scenario can
    # instead set accept_all_hints=[...] directly (2+ entries) to render the
    # real multi-button row for one of the four auto_accept.SUGGESTION_
    # FAMILIES operations -- see the "(N Always-allow candidates)" scenarios
    # below. The dummy per-entry rule_name here is never shown on screen
    # (only chosen_index and the label matter for this script); it only has
    # to be unique enough that click_title's own derivation above (matching
    # entry 0's hint) still lines up with the real first button's label.
    accept_all_hints = popup_kwargs.pop("accept_all_hints", None)
    allow_accept_all = popup_kwargs.pop("allow_accept_all", False)
    accept_all_hint = popup_kwargs.pop("accept_all_hint", "")
    if accept_all_hints is not None:
        popup_kwargs["accept_all_choices"] = [
            (f"candidate_{i}", hint) for i, hint in enumerate(accept_all_hints)
        ]
    elif allow_accept_all:
        popup_kwargs["accept_all_choices"] = [("candidate", accept_all_hint)]
    else:
        popup_kwargs["accept_all_choices"] = []

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
        # at: v2's webview loads asynchronously, and the NSPanel is created
        # (and passes _wait_for_window) fully transparent, well before that
        # finishes (see _wait_for_button_enabled's own docstring) -- waiting
        # on "Deny" (always present, never conditional like "Always allow")
        # becoming enabled is the actual "safe to screenshot" signal.
        # Without this, a screenshot taken right after _wait_for_window
        # alone could capture an empty, still-invisible panel, a race no
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

    # chosen_index (meaningful only when actual == "accept_all") isn't part
    # of this script's own pass/fail signal -- click_status/expected already
    # confirm the right button resolved the dialog; which accept_all_choices
    # index it carried is exercised structurally by _run_scenario's own
    # click_title <-> accept_all_hint(s) derivation above, not re-checked here.
    actual, _chosen_index = show_native_approval(**popup_kwargs)

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

# A synthetic settings.yaml for the Settings-window scenario group below (see
# _run_settings_window_scenarios) -- distinct from every tool-approval scenario's fixtures above,
# and deliberately minimal rather than "rich": several of these scenarios need *specific* starting
# conditions to stay unambiguous, most importantly zero pre-existing Drive folder grants -- the
# add/remove-grant scenario's own aria-label ("Trusted Folders resource ID") is shared by *every*
# row of that grant type (settings_window_html.py doesn't index it per row), so it resolves to
# exactly one element the instant "+ Add folder…" creates it only if none existed beforehand;
# otherwise _click_web_element/_set_web_element_text's "item 1 of matches" pick would be ambiguous.
# QA_NEW_DRIVE_FOLDER_ID is a made-up resource id, not a real one -- see
# docs/qa-environment-setup.md's Drive section for what a real one looks like.
QA_NEW_DRIVE_FOLDER_ID = "1QASettingsWindowGrantId00000002"
QA_SETTINGS_WINDOW_SETTINGS_YAML = """\
pii_detection:
  enabled: true
connectors:
  gmail:
    enabled: true
  drive:
    enabled: true
auto_accept_rules:
  gmail.read_message:
    - rule: trusted_sender_domain
      value:
        - example.com
logging:
  level: INFO
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
# _TINY_PNG_BYTES above) for the rich-markdown-preview scenario below: a
# multi-paragraph .docx, run through the real text_extraction.extract_text()
# path drive.py/gmail.py/confluence.py all use -- same DOCX/PPTX/XLSX
# extraction real downloads/uploads get, not a stand-in.
_LOREM_IPSUM_DOCX_PATH = (
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "qa_assets" / "lorem_ipsum.docx"
)
_LOREM_IPSUM_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _markdown_preview_blocks_for_lorem_ipsum_docx(details: str) -> list[dict]:
    """Real extract_text() call against the checked-in fixture, fed through
    the same preview_blocks_for() helper drive.py's real download/upload
    call sites use -- proves the "markdown" block type renders the fixture's
    actual headings/paragraphs, not a placeholder."""
    data = _LOREM_IPSUM_DOCX_PATH.read_bytes()
    return preview_blocks_for(details, extract_text(data, _LOREM_IPSUM_DOCX_MIME))


# Per-tool narrow/wide assignment, not from memory or a length heuristic. Keyed by the bare tool
# name _tool_name_from_scenario() extracts from each scenario's own name string. Confirmed against
# real screenshots and promoted verbatim into gate.py's own copy of this table for production use
# -- keep the two in sync if either ever changes.
#
# slack_send_message/telegram_send_message/jira_add_comment render their message/comment body
# via details_text, and NARROW has no mechanism at all to show details_text (see
# build_card_stack_html's own docstring: "no preview pane at all" -- every row has a fixed,
# truncated size, no "Show more/less" progressive-disclosure escape hatch). Keeping these three
# narrow would silently drop the one thing being approved -- the actual message/comment text -- so
# they're wide here instead, same as every other tool whose details_text is real free-text content
# rather than a fixed disclosure sentence. Every other tool not directly confirmed against a
# screenshot gets a best-effort classification by analogy to the closest sibling from the same
# connector -- wide only for tools that write/return a genuine prose body (doc/file content, page
# content, sheet cell values, chat/comment text); narrow for short structured field changes or a
# fixed disclosure sentence with nothing to actually preview.
_TOOL_LAYOUT: dict[str, str] = {
    # Confirmed wide against a real screenshot:
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
    # See the docstring above for why: NARROW has no mechanism at all to
    # show details_text, and these three carry a real message/comment body,
    # not a fixed disclosure sentence.
    "slack_send_message": "wide", "telegram_send_message": "wide", "jira_add_comment": "wide",
    #
    # Not directly confirmed -- best-effort by analogy (see docstring above):
    "gmail_reply_all_draft": "wide",  # same shape as gmail_reply_draft
    "confluence_download_attachment": "wide",  # same shape as gmail_download_attachment
    "gmail_create_draft_with_attachments": "wide",  # same shape as gmail_create_draft
    "gmail_reply_draft_with_attachments": "wide",  # same shape as gmail_reply_draft
    "gmail_reply_all_draft_with_attachments": "wide",  # same shape as gmail_reply_all_draft
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
    "confluence_download_attachment": "wide",  # real extracted-content preview, like the other download tools above
    "tasks_complete_task": "narrow",
    "tasks_uncomplete_task": "narrow", "tasks_move_task": "narrow",
}

# Read tools' own first-declared Always-allow rule name (auto_accept.
# SUGGESTION_FAMILIES' fixed declaration order, for the four multi-candidate
# operations -- see the "(N Always-allow candidates)" scenarios above for
# the rest of each family), per docs/always-allow-rules-reference.md's Read
# tools tables -- the WRITE_RULE_SUGGESTIONS-equivalent for the read side,
# except there's no single shared Python dict to derive this from directly
# (suggest_rule()'s actual pick depends on live per-call data, e.g. whether
# the fixture's sender/owner matches my_email, not a static tool->rule
# mapping) -- so unlike _TOOL_LAYOUT above, this one is kept in sync with
# that reference doc's own tables by hand. Tools with no read-gate
# Always-allow at all (none currently -- every RG-1/RG-2 tool has at least
# one candidate) simply don't appear here.
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
    more → Allow once)". Returns None for the settings-window scenarios (no "RG-N ·"/"WG-N ·"
    prefix at all) -- _run_scenario only consults this when actually building a tool-approval
    popup, so a None here is never reached for those scenarios in the first place."""
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


# ---------------------------------------------------------------------------- #
# Settings-window scenarios -- not tool-approval dialogs; exercise the real tray status item and,
# from it, the webview settings window issue #120 replaced the NSMenu tree / rules_manager_
# window.py's native "Manage Auto-accept Rules…" window with. Ported from this file's own deleted
# _run_menu_bar_scenario (see git history at 1f367ca for its exact shape) up through building the
# app and clicking the tray open -- everything past that point is new, since the old function's
# own destination (a native rules window addressed by NSButton/NSMenuItem the same way an approval
# popup is) no longer exists; this instead drives WKWebView content by aria-label via
# _click_web_element/_wait_for_web_element/_web_element_value/_web_element_enabled/
# _set_web_element_text (see that section's own header comment above _wait_for_web_element for the
# open questions those helpers carry into a real run).
#
# Seven scenarios, one per _SETTINGS_WINDOW_SCENARIO_NAMES entry, but built on ONE shared
# app/window -- unlike every tool-approval scenario above (each of which is a fully independent
# show_native_approval() call), opening a second real settings window per check would mean either
# six extra tray-icon-click round trips (slow, and each one a chance to leave a stray window behind
# if a later step fails) or awkwardly tearing down and rebuilding the whole app between checks. A
# real user also only opens Settings once and clicks around inside it, which this mirrors more
# directly than seven independent opens would. The trade-off: `--scenario` filtering here selects
# which of the seven *results* gets reported/screenshotted (see `wanted` below), not whether the
# shared window gets opened and navigated at all -- if any of the seven names matches, the whole
# group still runs end to end. Documented here once rather than repeated at every step function's
# own docstring below.
# ---------------------------------------------------------------------------- #

_SETTINGS_WINDOW_SCENARIO_NAMES: list[str] = [
    "Settings window · status item → window opens",
    "Settings window · navigate all 6 nav sections",
    "Settings window · PII Detection Gate sub-toggle dimming",
    "Settings window · edit an Auto-accept Rules text field (on-disk round trip)",
    "Settings window · add/remove a Trusted-resource grant row (on-disk round trip)",
    "Settings window · Privacy Filter category segmented control (on-disk round trip)",
    "Settings window · Audit Log level selector (on-disk round trip)",
]

# (nav label, an aria-label unique to that page's own content -- not the nav item itself, which
# settings_window_html.py's renderNav() renders unchanged on every page, and not a page's subnav
# tab labels, which can collide across pages, e.g. both Rules' per-connector tabs and Privacy's
# per-group tabs have a "Gmail" entry -- since render() replaces #app's entire innerHTML on every
# call, whichever page is actually up is the only one whose markers exist at any given moment, so
# this is unambiguous in practice despite the string overlap across pages that are never
# simultaneously on screen).
_SETTINGS_NAV_STEPS: list[tuple[str, str]] = [
    ("General", "PII Detection Gate"),
    ("Connectors", "Gmail enabled"),
    ("Auto-accept Rules", "Search rules"),
    ("Privacy Filter", "Message body policy"),
    ("Audit Log", "Export Audit Log"),
    ("About", "Open GitHub repository"),
]

_RULE_EDIT_NEW_VALUE = "example.com, qa-popup-smoke.example.com"
_RULE_EDIT_NEW_VALUE_LIST = ["example.com", "qa-popup-smoke.example.com"]


def _rule_edit_landed(cfg: dict) -> bool:
    rows = (cfg.get("auto_accept_rules") or {}).get("gmail.read_message") or []
    return bool(rows) and rows[0].get("value") == _RULE_EDIT_NEW_VALUE_LIST


def _drive_folder_entries(cfg: dict) -> list[dict]:
    return ((cfg.get("auto_accept_grants") or {}).get("drive") or {}).get("folders") or []


def _settings_open_window_step(
    pid: int, pause_seconds: float, screenshot_dir: Path | None, slug: str
) -> str:
    """Real click on the real tray status item, then on "Settings…" -- the same two-click
    path _run_menu_bar_scenario used (see git history at 1f367ca), just landing on the new settings
    window instead of the deleted rules_manager_window.py. Returns "clicked" on full success (the
    window actually appeared), or the first failing helper's own status string otherwise.

    No _wait_for_button_enabled-style "is the content actually ready" wait here, unlike
    _run_scenario's clicker() for approval popups -- there is no single always-present Deny-style
    element on this window to poll enabled/disabled the way _wait_for_button_enabled does there (it's
    one WKWebView, and which controls even exist varies by nav section), and every web-content
    interaction below already waits for its own target element to exist via
    _wait_for_web_element/_click_web_element/_set_web_element_text, which is this window's actual
    equivalent "is it ready" signal.
    """
    status = _click_menu_bar_icon(pid)
    if status != "clicked":
        return status
    time.sleep(pause_seconds)
    if screenshot_dir is not None:
        _screenshot_own_window(pid, screenshot_dir / f"{slug}-menu.png")
    time.sleep(pause_seconds)
    status = _click_menu_item(pid, "Settings…")
    if status != "clicked":
        return status
    status = _wait_for_window(pid)
    if status != "ready":
        return status
    time.sleep(pause_seconds)
    if screenshot_dir is not None:
        _screenshot_own_window(pid, screenshot_dir / f"{slug}-window.png")
    return "clicked"


def _settings_nav_sections_step(
    pid: int, pause_seconds: float, screenshot_dir: Path | None, slug: str
) -> str:
    """Click through all six of settings_window_html.py's NAV_ITEMS in order, confirming each
    landed via that page's own marker aria-label (see _SETTINGS_NAV_STEPS' own comment above).
    Screenshots each page if screenshot_dir is given. Returns "clicked" on full success, or a
    message naming which nav item failed and how."""
    for label, marker in _SETTINGS_NAV_STEPS:
        status = _click_web_element(pid, label)
        if status != "clicked":
            return f"click nav item {label!r}: {status}"
        status = _wait_for_web_element(pid, marker)
        if status != "ready":
            return f"landed on {label!r} but its own marker {marker!r} never appeared: {status}"
        time.sleep(pause_seconds)
        if screenshot_dir is not None:
            _screenshot_own_window(pid, screenshot_dir / f"{slug}-{_slugify(label)}.png")
    return "clicked"


def _settings_pii_toggle_step(pid: int, pause_seconds: float) -> str:
    """Toggle the PII Detection Gate master switch off, then confirm its two sub-toggles ("Detect
    IP addresses"/"Detect financial figures") actually followed -- not just visually dimmed (a
    screenshot can't tell "opacity: .4" from "opacity: 1" reliably enough to assert on), but
    reporting aria-disabled via AX (_web_element_enabled), which is the thing a screenshot-only
    check couldn't verify. QA_SETTINGS_WINDOW_SETTINGS_YAML starts pii_detection.enabled: true, so
    the sub-toggles are expected enabled *before* the click -- checked first, so a false pass here
    can't be explained by the sub-toggles having already been disabled for an unrelated reason.
    Restores the master switch back on afterward (best-effort, not re-verified -- see the inline
    comment at that point) since every other settings-window scenario in this file re-navigates to
    whatever page/state it needs rather than depending on this one's state, but leaving the tray's
    PII gate off for the rest of a real interactive run would be a surprising side effect of a QA
    script. Returns "clicked" on full success, or a message naming which check failed."""
    status = _click_web_element(pid, "General")
    if status != "clicked":
        return status
    status = _wait_for_web_element(pid, "PII Detection Gate")
    if status != "ready":
        return status
    for label in ("Detect IP addresses", "Detect financial figures"):
        enabled = _web_element_enabled(pid, label)
        if enabled != "true":
            return f"{label!r} not enabled at baseline (config starts pii_detection.enabled: true): {enabled!r}"
    status = _click_web_element(pid, "PII Detection Gate")
    if status != "clicked":
        return status
    time.sleep(pause_seconds)
    master_value = _web_element_value(pid, "PII Detection Gate")
    if master_value not in ("0", "false"):
        return f"master switch didn't report off after the click (value={master_value!r})"
    for label in ("Detect IP addresses", "Detect financial figures"):
        enabled = _web_element_enabled(pid, label)
        if enabled != "false":
            return f"{label!r} stayed enabled after the master switch went off (enabled={enabled!r})"
    # Restore to on -- best-effort, not re-verified: the dimming behavior this scenario exists to
    # check is already fully confirmed above (both the off-state and the disabled sub-toggles), and
    # a failed restore click here wouldn't invalidate that.
    _click_web_element(pid, "PII Detection Gate")
    return "clicked"


def _settings_rule_edit_step(pid: int, config_path: str) -> str:
    """Edit the Gmail "Read message" rule row's value field (a real text input, committed on blur
    -- see settings_window_html.py's own docstring on text-input commit semantics), then confirm
    the new value actually round-tripped to disk via update_rule_row() -> _save_and_reload() ->
    _save_config(), rather than just reading it back out of the DOM (which would only prove the
    input kept what was typed into it, not that Python ever received/saved it). Returns "clicked"
    on full success, or a message naming which step failed."""
    status = _click_web_element(pid, "Auto-accept Rules")
    if status != "clicked":
        return status
    status = _wait_for_web_element(pid, "Search rules")
    if status != "ready":
        return status
    # Gmail is rules.connectors[0] (RULES_MENU_GROUPS' own order -- see settings_controller.py) and
    # is selected by default on the Rules page's first render, but clicked explicitly anyway: both
    # to match a real user's flow, and because the nav-sections scenario above may have already
    # visited this page and left its client-only ui.rulesConnector on some other connector.
    status = _click_web_element(pid, "Gmail")
    if status != "clicked":
        return f"select Gmail connector tab: {status}"
    field_label = "Read message value, row 1"
    status = _wait_for_web_element(pid, field_label)
    if status != "ready":
        return status
    status = _set_web_element_text(pid, field_label, _RULE_EDIT_NEW_VALUE)
    if status != "typed":
        return status
    cfg = _wait_for_disk_config(config_path, _rule_edit_landed)
    if cfg is None:
        return (
            "on-disk auto_accept_rules.gmail.read_message[0].value never became "
            f"{_RULE_EDIT_NEW_VALUE_LIST!r} after typing"
        )
    return "clicked"


def _settings_grant_add_remove_step(pid: int, config_path: str) -> str:
    """Add a new Drive "Trusted Folders" grant row, set its resource ID, confirm it round-tripped
    to disk (add_grant_row()/update_grant_row()), then remove it and confirm the removal
    round-tripped too (remove_grant_row()) -- see QA_SETTINGS_WINDOW_SETTINGS_YAML's own comment
    for why this needs zero pre-existing Drive folder grants to stay unambiguous. Returns "clicked"
    on full success, or a message naming which step failed."""
    status = _click_web_element(pid, "Auto-accept Rules")
    if status != "clicked":
        return status
    status = _wait_for_web_element(pid, "Search rules")
    if status != "ready":
        return status
    status = _click_web_element(pid, "Drive")
    if status != "clicked":
        return f"select Drive connector tab: {status}"
    add_label = "Add folder…"
    status = _wait_for_web_element(pid, add_label)
    if status != "ready":
        return status
    status = _click_web_element(pid, add_label)
    if status != "clicked":
        return status
    cfg = _wait_for_disk_config(config_path, lambda cfg: len(_drive_folder_entries(cfg)) == 1)
    if cfg is None:
        return "add_grant_row never produced exactly one on-disk auto_accept_grants.drive.folders entry"
    id_field = "Trusted Folders resource ID"
    status = _wait_for_web_element(pid, id_field)
    if status != "ready":
        return status
    status = _set_web_element_text(pid, id_field, QA_NEW_DRIVE_FOLDER_ID)
    if status != "typed":
        return status
    cfg = _wait_for_disk_config(
        config_path,
        lambda cfg: bool(_drive_folder_entries(cfg))
        and _drive_folder_entries(cfg)[0].get("id") == QA_NEW_DRIVE_FOLDER_ID,
    )
    if cfg is None:
        return f"on-disk auto_accept_grants.drive.folders[0].id never became {QA_NEW_DRIVE_FOLDER_ID!r} after typing"
    # row.name is still "" (never set above) and row.id is now the id just typed -- see
    # settings_window_html.py's renderRules(): "Remove " + (row.name || row.id || gs.title).
    remove_label = f"Remove {QA_NEW_DRIVE_FOLDER_ID}"
    status = _wait_for_web_element(pid, remove_label)
    if status != "ready":
        return status
    status = _click_web_element(pid, remove_label)
    if status != "clicked":
        return status
    cfg = _wait_for_disk_config(config_path, lambda cfg: len(_drive_folder_entries(cfg)) == 0)
    if cfg is None:
        return "remove_grant_row never emptied auto_accept_grants.drive.folders on disk"
    return "clicked"


def _settings_privacy_category_step(pid: int, config_path: str) -> str:
    """Flip the Gmail privacy group's "Message body" category from its default policy (unset ->
    "allow", see privacy_filter._parse_group) to "Redact" via the segmented control, then confirm
    it round-tripped to disk via set_category_policy(). Returns "clicked" on full success, or a
    message naming which step failed."""
    status = _click_web_element(pid, "Privacy Filter")
    if status != "clicked":
        return status
    status = _wait_for_web_element(pid, "Message body policy")
    if status != "ready":
        return status
    # "privacy" (config key for the Gmail group -- PRIVACY_GROUP_LABELS' first entry, see
    # settings_controller.py) is selected by default on the Privacy Filter page's first render, but
    # clicked explicitly anyway for the same reason the rule-edit scenario above clicks its own
    # connector tab explicitly.
    status = _click_web_element(pid, "Gmail")
    if status != "clicked":
        return f"select Gmail privacy group tab: {status}"
    option_label = "Message body policy: Redact"
    status = _wait_for_web_element(pid, option_label)
    if status != "ready":
        return status
    status = _click_web_element(pid, option_label)
    if status != "clicked":
        return status
    cfg = _wait_for_disk_config(
        config_path, lambda cfg: ((cfg.get("privacy") or {}).get("categories") or {}).get("body") == "redact",
    )
    if cfg is None:
        return "on-disk privacy.categories.body never became 'redact' after clicking the segmented control"
    return "clicked"


def _settings_audit_log_level_step(pid: int, config_path: str) -> str:
    """Change the Audit Log page's log-level segmented control to DEBUG, then confirm it
    round-tripped to disk via set_log_level(). Returns "clicked" on full success, or a message
    naming which step failed."""
    status = _click_web_element(pid, "Audit Log")
    if status != "clicked":
        return status
    status = _wait_for_web_element(pid, "Export Audit Log")
    if status != "ready":
        return status
    option_label = "Log level: DEBUG"
    status = _wait_for_web_element(pid, option_label)
    if status != "ready":
        return status
    status = _click_web_element(pid, option_label)
    if status != "clicked":
        return status
    cfg = _wait_for_disk_config(config_path, lambda cfg: (cfg.get("logging") or {}).get("level") == "DEBUG")
    if cfg is None:
        return "on-disk logging.level never became 'DEBUG' after clicking the segmented control"
    return "clicked"


def _run_settings_window_scenarios(
    pause_seconds: float = 0.3, screenshot_dir: Path | None = None, only_lower: str | None = None,
) -> list[ScenarioResult]:
    """Builds one throwaway PrivacyFenceMenuBar off a temp settings.yaml (see
    QA_SETTINGS_WINDOW_SETTINGS_YAML), same reasoning and same rumps-private-API construction
    _run_menu_bar_scenario used to (see git history at 1f367ca) -- never touches the user's real
    config, and reaches into rumps' private rumps.rumps.NSApp/initializeStatusBar to attach a real
    NSStatusItem without starting a second, nested AppHelper.runEventLoop() (this process is
    already inside its own, started by main() below; starting another would never return).

    Runs all seven of _SETTINGS_WINDOW_SCENARIO_NAMES' steps against that one shared app/window
    (see this section's own header comment for why), then closes the window and removes the status
    item on the way out, the same "leave no window/status-item mess for whatever runs after it"
    contract _run_menu_bar_scenario used to carry -- this is always the last thing _scenarios()
    runs, but that contract is kept anyway rather than assumed away by ordering.

    Returns [] (no app ever built) if `only_lower` matches none of the seven names -- mirrors
    every other scenario's filter-before-clicking-anything contract in spirit, even though (per
    this section's header comment) a partial match still runs the whole shared group internally.
    """
    names = _SETTINGS_WINDOW_SCENARIO_NAMES
    wanted = {n for n in names if only_lower is None or only_lower in n.lower()}
    if not wanted:
        return []

    pid = os.getpid()
    fake_ipc_server = SimpleNamespace(
        unattended_session_count=lambda: 0,
        set_unattended_changed_listener=lambda callback: None,
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(QA_SETTINGS_WINDOW_SETTINGS_YAML)
        config_path = f.name

    app_holder: list[Any] = []

    def build_app() -> None:
        # Never touches the real org config file. Unlike the pre-#120 menu_bar.py this was ported
        # from (see git history at 1f367ca), settings_controller.py's methods each do their own
        # `from .daemon_main import load_org_config` *inside* the function body rather than once at
        # menu_bar.py module scope, so patching menu_bar.load_org_config (the old target) would no
        # longer intercept anything -- daemon_main.load_org_config is the actual name each of those
        # lazy imports re-resolves at call time, so that's what gets patched here instead.
        daemon_main.load_org_config = lambda: {}
        # set_log_level() (exercised by the Audit Log scenario below) calls daemon_main.
        # setup_logging(cfg) after saving, which resolves its log *file* path against the real
        # PROJECT_ROOT/data_dir() (not this scenario group's own temp config_path -- settings.yaml
        # location and log-file location are resolved independently) and replaces this whole
        # process's root logger handlers wholesale. Left un-stubbed, that scenario would create a
        # real file under the real data dir and hijack this script's own logging for the rest of
        # its run -- stubbed to a no-op for the same "stay inside this scenario group's own
        # sandbox" reason load_org_config is stubbed above. The thing that scenario actually checks
        # (logging.level landing correctly in the temp settings.yaml) doesn't depend on this call
        # succeeding.
        daemon_main.setup_logging = lambda cfg: None
        # Stubs out the same immediate background-threaded GitHub update-check completion callback
        # _run_menu_bar_scenario used to stub -- see its own docstring at 1f367ca for exactly why (a
        # completion landing while the settings window is open would push a state re-render at an
        # uncontrolled time, racing this function's own scripted clicks).
        menu_bar.PrivacyFenceMenuBar._on_update_check_timer = lambda self, _timer=None: None
        app = menu_bar.PrivacyFenceMenuBar(
            config_path, connectors=["gmail", "drive"],
            ipc_server=fake_ipc_server, connector_objs=[],
        )
        # Mirrors rumps.App.run() (rumps/rumps.py) up to, but not including, its final
        # AppHelper.runEventLoop() call -- see this function's docstring for why that call is
        # skipped here.
        nsapp = NSApplication.sharedApplication()
        if nsapp.activationPolicy() == NSApplicationActivationPolicyProhibited:
            nsapp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        nsapp.activateIgnoringOtherApps_(True)
        app._nsapp = _rumps_internal.NSApp.alloc().init()
        app._nsapp._app = app.__dict__
        nsapp.setDelegate_(app._nsapp)
        app._nsapp.initializeStatusBar()
        app_holder.append(app)

    def cleanup() -> None:
        if not app_holder:
            return
        app = app_holder[0]
        settings_window = app._settings_window
        if settings_window is not None and settings_window.window is not None:
            settings_window.window.close()
        status_item = getattr(app._nsapp, "nsstatusitem", None)
        if status_item is not None:
            NSStatusBar.systemStatusBar().removeStatusItem_(status_item)

    results: list[ScenarioResult] = []

    def add_result(name: str, button_clicked: str, expected: str, status: str) -> None:
        if name not in wanted:
            return
        actual = expected if status == "clicked" else None
        results.append(ScenarioResult(
            name=name, button_clicked=button_clicked, expected=expected, actual=actual, click_status=status,
        ))

    try:
        _run_on_main_thread_sync(build_app)
    except Exception as exc:  # noqa: BLE001 - surfaced as every wanted scenario's own failure below, not a crash
        os.unlink(config_path)
        setup_status = f"setup error: {exc!r}"
        for name in names:
            add_result(name, "(setup)", "shown", setup_status)
        return results

    slug0 = _slugify(names[0])
    status = _settings_open_window_step(pid, pause_seconds, screenshot_dir, slug0)
    add_result(names[0], "Settings…", "shown", status)

    if status != "clicked":
        # Nothing past this point can run without a window -- every other wanted scenario still
        # gets a row in the report (this file's convention elsewhere is that a skipped scenario is
        # never silently dropped, only filtered out entirely by --scenario/--group before it would
        # have run at all), rather than just omitting six rows and leaving a reader to wonder why.
        blocked = f"blocked: {names[0]!r} failed ({status})"
        for name in names[1:]:
            add_result(name, "(blocked)", "n/a", blocked)
    else:
        slug1 = _slugify(names[1])
        status = _settings_nav_sections_step(pid, pause_seconds, screenshot_dir, slug1)
        add_result(
            names[1], "General → Connectors → Auto-accept Rules → Privacy Filter → Audit Log → About",
            "navigated all 6 sections", status,
        )

        status = _settings_pii_toggle_step(pid, pause_seconds)
        add_result(names[2], "PII Detection Gate", "sub-toggles dim off / undim on", status)

        status = _settings_rule_edit_step(pid, config_path)
        add_result(
            names[3], "Read message value, row 1", f"on-disk value == {_RULE_EDIT_NEW_VALUE_LIST!r}", status,
        )

        status = _settings_grant_add_remove_step(pid, config_path)
        add_result(
            names[4], f"Add folder… / Remove {QA_NEW_DRIVE_FOLDER_ID}",
            "on-disk grant row added then removed", status,
        )

        status = _settings_privacy_category_step(pid, config_path)
        add_result(names[5], "Message body policy: Redact", "on-disk privacy.categories.body == 'redact'", status)

        status = _settings_audit_log_level_step(pid, config_path)
        add_result(names[6], "Log level: DEBUG", "on-disk logging.level == 'DEBUG'", status)

    _run_on_main_thread_sync(cleanup)
    os.unlink(config_path)
    return results


def _scenarios(
    pause_seconds: float = 0.3, screenshot_dir: Path | None = None, only: str | None = None,
    group: str = "all",
) -> list[ScenarioResult]:
    """At least one scenario per tool in docs/approval-window-content-reference.md's RG-1/RG-2/
    WG-1/WG-2/WG-3 tables (67 tools total) -- every dialog *shape* that reference doc
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
    # RG-2 below) -- see docs/approval-window-content-reference.md's
    # "View groups" section.
    # ================================================================== #

    results.append(run(
        # Also the preview_bytes/preview_mime_type image-render mechanic:
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
        # The Markdown-extraction preview mechanic (text_extraction.py +
        # markdown_to_html.py, replacing the old QuickLook-thumbnail
        # fallback): a non-image file with no Drive-generated thumbnailLink
        # falls back to the file's own extracted content -- headings/
        # paragraphs rendered rich via the "markdown" preview_blocks entry
        # (see drive.py's _download_file and preview_blocks_for()) -- calling
        # the real extract_text() against a real, checked-in .docx fixture
        # (tests/fixtures/qa_assets/lorem_ipsum.docx), not a placeholder.
        "RG-1 · drive_download_file (+ markdown preview)",
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
        preview_blocks=_markdown_preview_blocks_for_lorem_ipsum_docx(
            "Synthetic lorem ipsum content. No real information. Safe to read, "
            "download, or preview by any automated test."
        ),
    ))

    results.append(run(
        # Also the new_info (§3) mechanic: real values (not an
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
        # Issue #151's multi-button window: this event matches 2 of
        # calendar_read_event's 3 candidates (auto_accept.SUGGESTION_
        # FAMILIES) -- organizer *and* no external attendees -- so the real
        # popup renders both as their own Always-allow buttons in a
        # dedicated row above Deny/Allow once, instead of a single hinted
        # button (see approval_window_html.py's _button_row_html). Resolved
        # via plain "Allow once" (not either Always-allow button) since this
        # scenario's own job is the button-row layout, not rule creation --
        # see test_gate.py for the "which candidate got clicked" behavior.
        "RG-1 · calendar_get_event_details (2 Always-allow candidates)",
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
        accept_all_hints=["if I organize it", "no external attendees"],
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
        # Issue #151's multi-button window, jira_read_issue's own family
        # (auto_accept.SUGGESTION_FAMILIES) -- reporter, assignee, and
        # project key all three match this synthetic issue, so the real
        # popup renders 3 Always-allow buttons wrapping in their own row
        # (see approval_window_html.py's _button_row_html) -- the largest
        # candidate count of any of the four multi-candidate operations.
        "RG-1 · jira_get_issue (3 Always-allow candidates)",
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
        accept_all_hints=["if I'm reporter", "if I'm assignee", "this project"],
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
        # Issue #151's multi-button window, confluence_read_page's own
        # family (auto_accept.SUGGESTION_FAMILIES) -- author and space key
        # both match this synthetic page, so the real popup renders 2
        # Always-allow buttons instead of one hinted button (see
        # approval_window_html.py's _button_row_html).
        "RG-1 · confluence_get_page (2 Always-allow candidates)",
        click_title="Allow once", expected="accept",
        title="Read Confluence Page",
        preview={"Title": QA_PAGE, "Space": QA_SPACE},
        new_info={
            "Author": QA_PERSON, "Last modified": "2026-07-16", "Page body": "Full page content",
        },
        details_text=QA_PAGE_BODY,
        accept_all_hints=["if I'm author", "this space"],
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

    results.append(run(
        # Issue #151's multi-button window, drive_read's own family
        # (auto_accept.SUGGESTION_FAMILIES, shared by every drive_read
        # operation key -- resource_grants.DRIVE_FOLDER_READ_TARGETS) --
        # this synthetic file is both owned by the caller and in an
        # approved folder, so the real popup renders 2 Always-allow buttons
        # instead of one hinted button (see approval_window_html.py's
        # _button_row_html). drive_get_file_content stands in for all
        # three drive_read operations (drive_get_file_content/drive_
        # download_file/drive_sheets_get_values) -- they share this same
        # candidate family and button-row rendering.
        "RG-2 · drive_get_file_content (2 Always-allow candidates)",
        click_title="Allow once", expected="accept",
        title="Read Drive File Content",
        preview={
            "File": "PrivacyFence QA test file [QATEST].pdf", "Owner": QA_EMAIL,
            "Size": "18 KB", "Modified": "2026-07-16",
        },
        details_text="[binary content — this text should not be visible; the PDFView should be]",
        visibility={"File metadata": "allow", "Document content": "allow"},
        pdf_bytes=_TINY_PDF_BYTES,
        accept_all_hints=["if I own it", "this folder"],
        connector="drive",
    ))

    # ================================================================== #
    # WG-1 and WG-2 -- popup-gate, Deny / Allow once (WG-1: never Always
    # allow) or Deny / Allow once / conditionally Always allow (WG-2: 29
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
        # Same shape as gmail_create_draft, plus the Attachments preview row
        # _format_attachment_preview() builds ("name (N,NNN bytes)").
        "WG-2 · gmail_create_draft_with_attachments",
        click_title="Allow once", expected="accept",
        title="Create Gmail Draft with Attachments",
        preview={
            "To": QA_EMAIL, "Subject": f"Re: {QA_GMAIL_SUBJECT}",
            "Attachments": "qa-smoke-test.png (1,024 bytes)",
        },
        details_text="Synthetic PrivacyFence QA draft with attachment. No real information.",
        allow_accept_all=True,
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
        "WG-2 · gmail_reply_draft_with_attachments",
        click_title="Allow once", expected="accept",
        title="Create Gmail Reply Draft with Attachments",
        preview={
            "In reply to": QA_GMAIL_SUBJECT, "To": QA_EMAIL,
            "Attachments": "qa-smoke-test.png (1,024 bytes)",
        },
        details_text="Synthetic PrivacyFence QA reply draft with attachment. No real information.",
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
        "WG-2 · gmail_reply_all_draft_with_attachments",
        click_title="Allow once", expected="accept",
        title="Create Gmail Reply-All Draft with Attachments",
        preview={
            "In reply to": QA_GMAIL_SUBJECT, "To": QA_EMAIL, "Also to": QA_CC_EMAIL,
            "Attachments": "qa-smoke-test.png (1,024 bytes)",
        },
        details_text="Synthetic PrivacyFence QA reply-all draft with attachment. No real information.",
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
        # Markdown-extraction preview mechanic for the *upload* side (see
        # drive_download_file's own scenario above for the download-side
        # one) -- a non-image local file with no Drive-generated thumbnail
        # (there can't be one yet; it hasn't been uploaded) falls back to
        # the file's own extracted content instead, via the same real
        # extract_text() call against the checked-in lorem ipsum .docx
        # fixture.
        "WG-2 · drive_upload_file (+ markdown preview)",
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
        preview_blocks=_markdown_preview_blocks_for_lorem_ipsum_docx(
            "Synthetic lorem ipsum content. No real information. Safe to read, "
            "upload, or preview by any automated test."
        ),
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
        "WG-1 · slack_create_group_chat",
        click_title="Allow once", expected="accept",
        title="Create Slack Group Chat",
        preview={"Participants": "PrivacyFence QA Bot 1, PrivacyFence QA Bot 2"},
        details_text="Participants: PrivacyFence QA Bot 1, PrivacyFence QA Bot 2",
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
    # Settings window -- not tool-approval dialogs; exercises the actual tray status item and the
    # webview settings window issue #120 replaced the old NSMenu tree / native "Manage Auto-accept
    # Rules…" window with (see _run_settings_window_scenarios' own docstring for what these seven
    # scenarios cover and why they share one app/window instead of running fully independently like
    # every scenario above). Kept last, after every popup scenario above: its status item and
    # non-modal window mustn't sit on screen alongside an approval popup -- _screenshot_own_window
    # assumes only one of our own windows is ever on screen at a time, and this group cleans its own
    # window/status item up on the way out rather than leaving them for whatever runs after it.
    # Neither an "RG-" nor a "WG-" scenario, so a --group rg/wg run skips it entirely, same as every
    # other group filter above.
    # ================================================================== #
    if group_prefix is None:
        results.extend(_run_settings_window_scenarios(
            pause_seconds=pause_seconds, screenshot_dir=screenshot_dir, only_lower=only_lower,
        ))

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
             "'Settings window' for the settings-window scenarios), instead of the full 104-scenario "
             "suite (97 tool-approval scenarios plus the seven settings-window scenarios). For "
             "grabbing a single updated screenshot -- e.g. for README.md -- without sitting through "
             "the whole run: --scenario 'gmail_get_thread' --screenshot-dir docs/images/screenshots. "
             "Combines with --group (both must match). Matches nothing -> an empty report and a "
             "nonzero exit code, same as any other all-failed run.",
    )
    parser.add_argument(
        "--group", choices=["all", "rg", "wg"], default="all",
        help="'all' (default): every scenario. 'rg': review-gate (read) scenarios only -- those "
             "whose name starts with 'RG-', per docs/approval-window-content-reference.md's view "
             "groups. 'wg': popup-gate (write) scenarios only ('WG-' prefix). Either excludes the "
             "seven settings-window scenarios, which are neither. Combines with --scenario (both "
             "must match) -- "
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
