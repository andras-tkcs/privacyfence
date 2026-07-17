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
"""
from __future__ import annotations

import argparse
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


def _run_scenario(name: str, *, click_title: str, expected: str, **popup_kwargs) -> ScenarioResult:
    pid = os.getpid()
    click_status_box: list[str] = []

    def clicker() -> None:
        # Fired from a background thread, same as the click has to happen
        # from a different thread than the one show_native_approval() will
        # block on below (the AppKit modal loop). A short head start lets
        # the window actually get created before System Events starts
        # polling for it.
        time.sleep(0.3)
        click_status_box.append(_click_button(pid, click_title))

    clicker_thread = threading.Thread(target=clicker, daemon=True)
    clicker_thread.start()

    actual = show_native_approval(**popup_kwargs)

    clicker_thread.join(timeout=WINDOW_WAIT_TIMEOUT_SECONDS + 5)
    click_status = click_status_box[0] if click_status_box else "clicker thread never finished"
    return ScenarioResult(
        name=name, button_clicked=click_title, expected=expected, actual=actual, click_status=click_status,
    )


def _scenarios() -> list[ScenarioResult]:
    results = []

    results.append(_run_scenario(
        "Plain popup, Allow once",
        click_title="Allow once", expected="accept",
        title="PrivacyFence — QA smoke test (plain)",
        preview={"from": "qa-smoke@example.com"},
        details_text="Ordinary, non-sensitive smoke-test content.",
        allow_accept_all=False,
    ))

    results.append(_run_scenario(
        "Plain popup, Deny",
        click_title="Deny", expected="deny",
        title="PrivacyFence — QA smoke test (deny path)",
        preview={"from": "qa-smoke@example.com"},
        details_text="Ordinary, non-sensitive smoke-test content.",
        allow_accept_all=False,
    ))

    results.append(_run_scenario(
        "PII-tinted popup, Allow once",
        click_title="Allow once", expected="accept",
        title="PrivacyFence — QA smoke test (PII-tinted)",
        preview={"from": "qa-smoke@example.com"},
        details_text="His SSN is 123-45-6789 on file.",
        allow_accept_all=False,
        pii_categories=["US Social Security Number"],
    ))

    results.append(_run_scenario(
        "Review-gate popup, Always allow",
        click_title="Always allow", expected="accept_all",
        title="PrivacyFence — QA smoke test (Always allow)",
        preview={"from": "qa-smoke@example.com"},
        details_text="Ordinary, non-sensitive smoke-test content.",
        allow_accept_all=True,
    ))

    results.append(_run_scenario(
        "Write-gate popup, Allow for 5 min",
        click_title="Allow for 5 min", expected="accept_temp",
        title="PrivacyFence — QA smoke test (temp accept)",
        preview={"file": "qa-smoke-test-file.txt"},
        details_text="Ordinary, non-sensitive smoke-test content.",
        allow_accept_all=False,
        allow_temp_accept=True,
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
    args = parser.parse_args()

    results: list[ScenarioResult] = []
    exit_code = 0

    def work() -> None:
        nonlocal exit_code
        try:
            results.extend(_scenarios())
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
