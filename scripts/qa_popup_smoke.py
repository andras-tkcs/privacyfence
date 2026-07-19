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

Usage (the project's own venv, not a bare system python3 -- this needs the
same pyobjc/AppKit packages the app itself depends on, which only the venv
has installed):
    .venv/bin/python scripts/qa_popup_smoke.py
    .venv/bin/python scripts/qa_popup_smoke.py --report-file /tmp/popup_smoke.md
    .venv/bin/python scripts/qa_popup_smoke.py --pause-seconds 3   # slow down to actually look
"""
from __future__ import annotations

import argparse
import functools
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

if sys.platform != "darwin":
    print(
        "qa_popup_smoke.py requires macOS (real AppKit) -- nothing to run on this platform.",
        file=sys.stderr,
    )
    sys.exit(1)

from PyObjCTools import AppHelper  # noqa: E402

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


def _click_button(pid: int, title: str) -> str:
    """Wait for our own process's first window to appear, then click a
    button on it by its exact title -- returns "clicked", "TIMEOUT_NO_WINDOW"
    (the popup never appeared within WINDOW_WAIT_TIMEOUT_SECONDS),
    "BUTTON_NOT_FOUND" (the window appeared but has no button with this
    exact title -- e.g. the button set didn't match what the scenario
    expected), or an osascript-level error string.

    Targets the process by unix id, not by name -- a plain `python3
    scripts/...` invocation's process name varies by how Python itself was
    installed/framework-built, but its pid is unambiguous.
    """
    script = f'''
    tell application "System Events"
        set targetProcess to first process whose unix id is {pid}
        set deadlineTime to (current date) + {WINDOW_WAIT_TIMEOUT_SECONDS}
        repeat
            if (exists window 1 of targetProcess) then exit repeat
            if (current date) > deadlineTime then
                return "TIMEOUT_NO_WINDOW"
            end if
            delay 0.1
        end repeat
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


def _run_scenario(
    name: str, *, click_title: str, expected: str, pre_click_title: str | None = None,
    pause_seconds: float = 0.3, **popup_kwargs
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


def _scenarios(pause_seconds: float = 0.3) -> list[ScenarioResult]:
    results = []
    # Bound once here rather than passing pause_seconds=pause_seconds at
    # each of the ~11 call sites below.
    run = functools.partial(_run_scenario, pause_seconds=pause_seconds)

    results.append(run(
        "Plain popup, Allow once",
        click_title="Allow once", expected="accept",
        title="PrivacyFence — QA smoke test (plain)",
        preview={"from": "qa-smoke@example.com"},
        details_text="Ordinary, non-sensitive smoke-test content.",
        allow_accept_all=False,
    ))

    results.append(run(
        "Plain popup, Deny",
        click_title="Deny", expected="deny",
        title="PrivacyFence — QA smoke test (deny path)",
        preview={"from": "qa-smoke@example.com"},
        details_text="Ordinary, non-sensitive smoke-test content.",
        allow_accept_all=False,
    ))

    results.append(run(
        # Also exercises the sensitivity badge below the banner --
        # pii_categories drives both, no separate scenario needed.
        "PII-tinted popup, Allow once",
        click_title="Allow once", expected="accept",
        title="PrivacyFence — QA smoke test (PII-tinted)",
        preview={"from": "qa-smoke@example.com"},
        details_text="His SSN is 123-45-6789 on file.",
        allow_accept_all=False,
        pii_categories=["US Social Security Number"],
    ))

    results.append(run(
        "Review-gate popup, Always allow",
        click_title="Always allow", expected="accept_all",
        title="PrivacyFence — QA smoke test (Always allow)",
        preview={"from": "qa-smoke@example.com"},
        details_text="Ordinary, non-sensitive smoke-test content.",
        allow_accept_all=True,
    ))

    results.append(run(
        "Write-gate popup, Allow for 5 min",
        click_title="Allow for 5 min", expected="accept_temp",
        title="PrivacyFence — QA smoke test (temp accept)",
        preview={"file": "qa-smoke-test-file.txt"},
        details_text="Ordinary, non-sensitive smoke-test content.",
        allow_accept_all=False,
        allow_temp_accept=True,
    ))

    # The five scenarios above predate Phases 1a/1b/2/3 -- none of them ever
    # set visibility/claude_reason/write_content_flags/seen_count, so a real
    # on-screen run never actually exercised those sections' rendering or
    # confirmed they don't break the click-through modal loop (e.g. a taller
    # window from more sections stacked could silently shift button
    # coordinates). Added to close that gap.

    results.append(run(
        "Review-gate popup with AI-visibility checklist, Allow once",
        click_title="Allow once", expected="accept",
        title="PrivacyFence — QA smoke test (visibility checklist)",
        preview={"from": "qa-smoke@example.com"},
        details_text="Ordinary, non-sensitive smoke-test content.",
        allow_accept_all=False,
        visibility={"Message body": "allow", "Attachments": "block", "Sender metadata": "redact"},
    ))

    results.append(run(
        # Also exercises the sensitivity badge below the banner, same as
        # the PII-tinted scenario above -- write_content_flags drives both.
        "Write-gate popup with content-flag banner, Allow once",
        click_title="Allow once", expected="accept",
        title="PrivacyFence — QA smoke test (content-flag banner)",
        preview={"to": "qa-smoke@example.com"},
        details_text="Please wire the deposit per the attached IBAN.",
        allow_accept_all=False,
        write_content_flags=["IBAN (bank account number)"],
    ))

    results.append(run(
        "Popup with reason + seen-count + visibility all present, Allow once",
        click_title="Allow once", expected="accept",
        title="PrivacyFence — QA smoke test (kitchen sink)",
        preview={"from": "qa-smoke@example.com", "subject": "Weekly digest"},
        details_text="Ordinary, non-sensitive smoke-test content, long enough to show a real "
                      "reading-time estimate above the details pane rather than a trivial one.",
        allow_accept_all=True,
        visibility={"Message body": "allow", "Attachments": "block"},
        claude_reason="Summarizing the weekly digest for the user, as requested.",
        seen_count=3,
    ))

    # Phase 3 additions: the details pane is now a WKWebView, which can
    # render either the Gmail-style email header or a native PDFView
    # instead of plain text -- neither was covered by a click-through
    # scenario before, so a regression in either rendering path (e.g. the
    # window becoming non-interactive, or the click landing on the wrong
    # coordinates because the pane's content changed the effective layout)
    # would only ever have been caught by construction-only unit tests, not
    # by an actual on-screen click.

    results.append(run(
        "Review-gate popup with Gmail-style email header, Allow once",
        click_title="Allow once", expected="accept",
        title="PrivacyFence — QA smoke test (email header)",
        preview={"From": "alice@example.com", "To": "bob@example.com",
                 "Subject": "Q3 numbers", "Date": "2026-07-01"},
        details_text="Ordinary, non-sensitive smoke-test email body.",
        allow_accept_all=False,
        content_kind="email",
    ))

    results.append(run(
        "Review-gate popup with native PDFView, Allow once",
        click_title="Allow once", expected="accept",
        title="PrivacyFence — QA smoke test (PDFView)",
        preview={"File": "qa-smoke-test.pdf"},
        details_text="[binary content — this text should not be visible; the PDFView should be]",
        allow_accept_all=False,
        pdf_bytes=(
            b"%PDF-1.1\n"
            b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
            b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
            b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >> endobj\n"
            b"xref\n0 4\n0000000000 65535 f \n"
            b"trailer << /Size 4 /Root 1 0 R >>\n"
            b"startxref\n0\n%%EOF"
        ),
    ))

    results.append(run(
        # "Show more" is a non-terminal click -- it must resize the window
        # in place without resolving the modal loop, so the following
        # "Allow once" click still has to land on the same (now taller)
        # window. Exactly the kind of thing a title-bar-height miscalculation
        # in _rebuild_content would silently break: unit tests confirmed the
        # frame math, but only a real click sequence confirms the click
        # target itself is still where System Events expects it after resize.
        "Review-gate popup, Show more then Allow once",
        click_title="Allow once", expected="accept", pre_click_title="Show more",
        title="PrivacyFence — QA smoke test (progressive disclosure)",
        preview={"from": "qa-smoke@example.com"},
        details_text="line one\n" * 200 + "the last line, still present",
        allow_accept_all=False,
    ))

    return results


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
    args = parser.parse_args()

    results: list[ScenarioResult] = []
    exit_code = 0

    def work() -> None:
        nonlocal exit_code
        try:
            results.extend(_scenarios(args.pause_seconds))
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
