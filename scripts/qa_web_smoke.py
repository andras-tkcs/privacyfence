#!/usr/bin/env python3
"""Local-only smoke test for the web surfaces (`/settings`, `/approvals`,
docs/https-connector-refactor-plan.md §16): does a real click, in a real
browser, against the real embedded HTTP server (not the Starlette
TestClient every test in tests/unit/web/ uses), actually round-trip?

tests/unit/web/ and tests/unit/test_web_shell.py/test_approval_list_html.py
already cover HTML/JSON construction and route-level behavior on every PR --
CSRF, allowlist, argument validation, the JS strings this script's own pages
embed. What none of that exercises is a *real browser* actually parsing and
running that JS: script tag *order* (a script referencing a DOM element or
another script's global that appears later in the document silently no-ops
instead of raising -- this script's own "toast after a decision" scenario
below is a regression test for exactly that bug, found by running this
script during development), the CSP actually permitting what the page needs
(a stricter `default-src 'none'` policy silently breaking service worker
registration, say), and whether a click really reaches the code a unit test
only proved exists.

This is NOT a pytest test and NEVER runs in CI: it needs `playwright`
(`pip install playwright` -- not a project dependency, install it locally)
and a Chromium binary. If Playwright's own browser isn't installed
(`playwright install chromium`), point `--chromium-path` at one already on
disk (this repo's own CI sandbox, when it has one, sets
`PLAYWRIGHT_BROWSERS_PATH`; on such a machine the binary is usually under
`$PLAYWRIGHT_BROWSERS_PATH/chromium-*/chrome-linux/chrome` or the macOS
equivalent).

When to run this: whenever web_shell.py, approval_list_html.py,
web/routes_approvals.py's or web/routes_settings.py's own JS-emitting
functions, resources/sw.js, or web/server.py's CSP change. Not on every
settings_controller.py/settings_window_html.py change -- those are covered
by tests/unit/test_settings_window_html.py's construction-only assertions
the same way qa_popup_smoke.py's own docstring describes for the native
window.

    .venv/bin/pip install playwright   # once, locally -- not committed
    .venv/bin/python scripts/qa_web_smoke.py

Paste the printed report into the PR description under a
`## Web smoke check` heading, same convention as testing-policy.md §2.1/§2.2.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


@dataclass
class ScenarioResult:
    name: str
    passed: bool
    detail: str = ""
    console_errors: list[str] = field(default_factory=list)


def _render_report(results: list[ScenarioResult]) -> str:
    lines = ["Web smoke check", "================"]
    for r in results:
        lines.append(f"[{'PASS' if r.passed else 'FAIL'}] {r.name}" + (f" -- {r.detail}" if r.detail else ""))
        for err in r.console_errors:
            lines.append(f"    console error: {err}")
    passed = sum(1 for r in results if r.passed)
    lines.append(f"\n{passed}/{len(results)} scenarios passed.")
    return "\n".join(lines)


def _build_server(tmp_dir: Path, port: int, token: str):
    from privacyfence import daemon_main, settings_controller as sc
    from privacyfence.web.server import WebServer
    from privacyfence.web_approval_ui import WebApprovalUI

    sc.data_dir = lambda: tmp_dir
    sc.org_dir = lambda: tmp_dir
    daemon_main.load_org_config = lambda: {}

    config_path = tmp_dir / "settings.yaml"
    config_path.write_text("auto_accept_rules: {}\nconnectors: {}\n", encoding="utf-8")
    ipc_server = SimpleNamespace(set_connectors=lambda c: None, set_unattended_changed_listener=lambda cb: None)
    controller = sc.SettingsController(str(config_path), connectors=[], ipc_server=ipc_server)

    web_ui = WebApprovalUI()
    server = WebServer(web_ui, host="127.0.0.1", port=port, token=token, controller=controller)
    server.start()
    return server, web_ui, controller


def _register_card(web_ui) -> tuple[threading.Thread, dict, str]:
    box: dict = {}

    def run() -> None:
        box["result"] = web_ui.show_popup(
            "Send email", {"To": "a@example.com"}, "body text", connector="gmail",
            accept_all_choices=[("always_allow", "")],
        )

    t = threading.Thread(target=run, daemon=True)
    t.start()
    deadline = time.time() + 3
    while web_ui.current() is None and time.time() < deadline:
        time.sleep(0.02)
    card = web_ui.current()
    if card is None:
        raise RuntimeError("card never registered")
    return t, box, card.id


def _run(chromium_path: str | None) -> list[ScenarioResult]:
    from playwright.sync_api import sync_playwright

    results: list[ScenarioResult] = []
    tmp_dir = Path(tempfile.mkdtemp(prefix="pf-qa-web-"))
    port = 18700
    token = "qa-web-smoke-token-0123456789"
    try:
        server, web_ui, controller = _build_server(tmp_dir, port, token)
        time.sleep(0.3)
        base = f"http://127.0.0.1:{port}"

        launch_kwargs = {"args": ["--no-sandbox"]}
        if chromium_path:
            launch_kwargs["executable_path"] = chromium_path

        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_kwargs)
            page = browser.new_page()
            console_errors: list[str] = []
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda exc: console_errors.append(str(exc)))

            def scenario(name: str, fn) -> None:
                console_errors.clear()
                try:
                    detail = fn() or ""
                    results.append(ScenarioResult(name, True, detail, list(console_errors)))
                except Exception as exc:  # noqa: BLE001 - captured as a failed scenario, not a crash
                    results.append(ScenarioResult(name, False, repr(exc), list(console_errors)))

            def settings_loads_and_round_trips() -> str:
                page.goto(f"{base}/settings?token={token}")
                page.wait_for_selector("#app")
                before = controller.snapshot()["general"]["pii_enabled"]
                page.click("[data-action='toggle_pii_detection']")
                time.sleep(0.3)
                after = controller.snapshot()["general"]["pii_enabled"]
                assert after != before, "toggle did not reach the server"
                return f"pii_enabled {before} -> {after}"

            scenario("settings page loads, toggle round-trips", settings_loads_and_round_trips)

            def approvals_empty_state() -> str:
                page.goto(f"{base}/approvals?token={token}")
                page.wait_for_selector(".pf-approvals-empty")
                return "empty state rendered"

            scenario("approvals list: empty state", approvals_empty_state)

            def approvals_row_and_deny() -> str:
                t, box, card_id = _register_card(web_ui)
                page.goto(f"{base}/approvals?token={token}")
                page.wait_for_selector(f'[data-approval-id="{card_id}"]', timeout=3000)
                page.click(f'[data-deny="{card_id}"]')
                t.join(timeout=2)
                assert box.get("result") == ("deny", None), box.get("result")
                return "row rendered, Deny click resolved the blocked gate call"

            scenario("approvals list: row + Deny-from-row", approvals_row_and_deny)

            def card_decide_returns_to_list_with_toast() -> str:
                t, box, card_id = _register_card(web_ui)
                page.goto(f"{base}/approvals/{card_id}?token={token}")
                page.wait_for_selector("[data-pf-action='deny']", timeout=3000)
                page.click("[data-pf-action='deny']")
                page.wait_for_url(f"{base}/approvals", timeout=3000)
                page.wait_for_timeout(300)
                toast = page.query_selector("#pf-shell-toast")
                classes = toast.get_attribute("class") if toast else ""
                text = toast.inner_text() if toast else ""
                assert "shown" in (classes or ""), f"toast not shown (class={classes!r})"
                assert text, "toast has no text"
                t.join(timeout=2)
                return f"navigated to /approvals, toast: {text!r}"

            scenario("card decide -> return-to-list toast (regression: script order)", card_decide_returns_to_list_with_toast)

            def service_worker_registers() -> str:
                page.goto(f"{base}/approvals?token={token}")
                page.wait_for_timeout(500)
                states = page.evaluate(
                    "navigator.serviceWorker.getRegistrations()"
                    ".then(rs => rs.map(r => r.active && r.active.state))"
                )
                assert states and states[0] == "activated", f"service worker did not activate: {states!r}"
                return f"states={states!r}"

            scenario("service worker registers under the page's CSP", service_worker_registers)

            browser.close()

        server.stop()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--chromium-path", default=None,
        help="Path to a Chromium/Chrome binary, if Playwright's own bundled browser isn't installed.",
    )
    parser.add_argument("--report-file", type=Path, default=None)
    args = parser.parse_args()

    try:
        results = _run(args.chromium_path)
    except ImportError as exc:
        print(f"qa_web_smoke.py: {exc} -- `pip install playwright` first (see this script's own docstring).",
              file=sys.stderr)
        sys.exit(2)

    report = _render_report(results)
    print(report)
    if args.report_file:
        args.report_file.write_text(report + "\n", encoding="utf-8")
    sys.exit(0 if results and all(r.passed for r in results) else 1)


if __name__ == "__main__":
    main()
