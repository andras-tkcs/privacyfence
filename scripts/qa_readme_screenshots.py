#!/usr/bin/env python3
"""Generates the two web-settings screenshots docs/images/screenshots/README.md
lists (`settings-connectors.png`, `settings-auto-accept-rules.png`), used by
the top-level README.md's "Local administration" section.

Through P9 that section showed the native menu bar/Auto-accept Rules window
(`scripts/qa_popup_smoke.py --scenario "Menu bar"`/"Settings window..."); P10
deleted that UI, so this is its web-settings-page replacement -- same
"real click against a real on-screen render, not mocked up by hand" posture,
just against a real embedded HTTP server + real browser (Playwright/
Chromium) instead of AppKit.

Requires `playwright` (`pip install playwright` -- not a project dependency,
install it locally) and a Chromium binary; see qa_web_smoke.py's own
docstring for where to point --chromium-path if none is auto-discovered.

    .venv/bin/pip install playwright   # once, locally -- not committed
    .venv/bin/python scripts/qa_readme_screenshots.py

Regenerate only when the settings page's visual design changes meaningfully
-- not for every settings_controller.py/settings_window_html.py change.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "images" / "screenshots"

# Representative, fake-but-plausible state -- enough for the two screenshots
# to show a populated, real-looking page rather than every field empty.
_SETTINGS_YAML = """\
connectors: {}
pii_detection:
  enabled: true
auto_accept_rules:
  gmail.read_message:
    - rule: i_am_sender
    - rule: trusted_sender_domain
      value: ["example.com", "partner.example.org"]
  gmail.archive_message:
    - rule: label_match
      value: ["Newsletters"]
auto_accept_grants:
  drive:
    folders:
      - id: "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
        name: "Shared Reports"
        read: true
"""

# Connectors shown as "connected" -- SettingsController marks a connector
# authed purely by name membership in this list, no real client needed.
_FAKE_CONNECTED = ["gmail", "drive", "slack", "calendar"]


def _build_server(tmp_dir: Path, port: int, token: str):
    from privacyfence import daemon_main, settings_controller as sc
    from privacyfence.web.server import WebServer
    from privacyfence.web_approval_ui import WebApprovalUI

    sc.data_dir = lambda: tmp_dir
    sc.org_dir = lambda: tmp_dir
    daemon_main.load_org_config = lambda: {}

    config_path = tmp_dir / "settings.yaml"
    config_path.write_text(_SETTINGS_YAML, encoding="utf-8")
    connector_host = SimpleNamespace(set_connectors=lambda c: None)
    controller = sc.SettingsController(
        str(config_path), connectors=list(_FAKE_CONNECTED), connector_host=connector_host,
    )

    web_ui = WebApprovalUI()
    server = WebServer(web_ui, host="127.0.0.1", port=port, token=token, controller=controller)
    server.start()
    return server


def _run(chromium_path: str | None) -> None:
    from playwright.sync_api import sync_playwright

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="pf-qa-readme-shots-"))
    port = 18701
    token = "qa-readme-screenshots-token-0123456789"
    try:
        server = _build_server(tmp_dir, port, token)
        time.sleep(0.3)
        base = f"http://127.0.0.1:{port}"

        launch_kwargs = {"args": ["--no-sandbox"]}
        if chromium_path:
            launch_kwargs["executable_path"] = chromium_path

        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_kwargs)
            page = browser.new_page(viewport={"width": 1000, "height": 720})

            page.goto(f"{base}/settings?token={token}")
            page.wait_for_selector("#app")
            page.click("[data-nav='connectors']")
            page.wait_for_timeout(200)
            path = OUT_DIR / "settings-connectors.png"
            page.locator("#app").screenshot(path=str(path))
            print(f"wrote {path}")

            page.click("[data-nav='rules']")
            page.wait_for_timeout(200)
            path = OUT_DIR / "settings-auto-accept-rules.png"
            page.locator("#app").screenshot(path=str(path))
            print(f"wrote {path}")

            browser.close()

        server.stop()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--chromium-path", default=None,
        help="Path to a Chromium/Chrome binary, if Playwright's own bundled browser isn't installed.",
    )
    args = parser.parse_args()

    try:
        _run(args.chromium_path)
    except ImportError as exc:
        print(f"qa_readme_screenshots.py: {exc} -- `pip install playwright` first.", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
