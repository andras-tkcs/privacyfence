"""End-to-end proof that the real Phase 3 PWA interoperates with the real
Python daemon-side code (issue #55) -- not mocks on either side:

- A real X25519 pairing handshake between this PWA (in a real headless
  Chromium) and src/privacyfence/mobile_relay_pairing.py, through a real
  instance of the Phase 0 spike's relay server.
- A real approval round trip afterward, using the exact wire shape
  MobileRelayApprovalUI posts (src/privacyfence/mobile_relay_approval_ui.py),
  checking the PWA renders full parity content including the PII banner,
  and that the daemon receives a correctly HMAC-authenticated decision.
- The signed/pinned bundle-release mechanism (scripts/sign_pwa_bundle.py +
  js/release_verify.js + sw.js): a validly-signed bundle verifies cleanly,
  and -- the actual security property this all exists for -- a bundle
  tampered with *after* signing is detected and reported, not silently
  served.

Requires: a Chromium binary (this repo's CI/dev environment has one
pre-installed at a fixed path; see CHROMIUM_PATH below) and Node only
indirectly (none actually -- Playwright drives a real browser, so the PWA's
plain <script>-tag loading is exercised exactly as a real phone would hit
it, not via Node's `require()`).
"""
from __future__ import annotations

import base64
import http.server
import json
import shutil
import sys
import threading
from functools import partial
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "spikes" / "mobile-relay-phase0" / "relay"))

playwright_sync_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_sync_api.sync_playwright

from relay_server import make_server  # noqa: E402
import generate_pwa_release_key  # noqa: E402
import sign_pwa_bundle  # noqa: E402

from privacyfence.mobile_relay_client import MobileRelayClient  # noqa: E402
from privacyfence.mobile_relay_pairing import (  # noqa: E402
    DaemonIdentity,
    _generate_x25519_keypair,
    begin_pairing,
    complete_pairing,
)

# Matches the path Phase 0's own tests use for this pre-installed browser.
CHROMIUM_PATH = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"


def _serve_directory(directory: Path) -> tuple[http.server.ThreadingHTTPServer, str]:
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address
    return httpd, f"http://{host}:{port}"


@pytest.fixture()
def relay_url():
    server = make_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture()
def release_key(tmp_path):
    path = tmp_path / "release_key.json"
    generate_pwa_release_key.main(["-o", str(path)])
    data = json.loads(path.read_text())
    return {"path": path, "public_key": base64.b64decode(data["public_key_base64"])}


@pytest.fixture()
def signed_pwa(tmp_path, release_key):
    """A copy of the real mobile-approval-pwa/ directory, signed with a
    fresh throwaway test key (never the org's real, offline release key)
    and served over HTTP. Returns (pwa_url, bundle_dir) so a test can
    tamper with bundle_dir's files after the fact."""
    bundle_dir = tmp_path / "pwa"
    shutil.copytree(REPO_ROOT / "mobile-approval-pwa", bundle_dir)
    code = sign_pwa_bundle.main([
        "--release-key", str(release_key["path"]), "--bundle-dir", str(bundle_dir), "--bundle-version", "test",
    ])
    assert code == 0
    httpd, pwa_url = _serve_directory(bundle_dir)
    try:
        yield pwa_url, bundle_dir
    finally:
        httpd.shutdown()


@pytest.fixture()
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM_PATH)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture()
def daemon_identity():
    private_key, public_key = _generate_x25519_keypair()
    return DaemonIdentity(private_key=private_key, public_key=public_key)


def pair_phone_in_browser(page, pwa_url: str, qr_payload: dict, device_name: str = "Test Phone") -> None:
    page.goto(f"{pwa_url}/index.html")
    page.fill("#pairing-payload", json.dumps(qr_payload))
    page.fill("#device-name", device_name)
    page.click("#pair-button")
    page.wait_for_selector("#waiting-screen:not(.hidden)", timeout=10000)


def pair_and_complete(page, pwa_url: str, session, daemon_identity, device_name: str = "Test Phone"):
    """Drives a real pairing to completion on *both* sides at once.

    The phone's completePairing() (js/pairing.js) doesn't resolve until it
    receives the daemon's signed ack -- and the daemon's complete_pairing()
    (mobile_relay_pairing.py) doesn't post that ack until it has picked up
    the phone's handshake request from the mailbox. Each side is waiting on
    the other, so they must run concurrently, exactly as a real daemon
    process and a real phone would: the daemon-side call runs on a
    background thread while this thread drives the browser through the
    pairing form and waits for it to reach the waiting screen.
    """
    result: dict[str, Any] = {}

    def run_daemon_side() -> None:
        try:
            result["device"] = complete_pairing(session, daemon_identity, poll_timeout_seconds=15)
        except Exception as exc:  # noqa: BLE001 - surfaced to the test thread below
            result["error"] = exc

    thread = threading.Thread(target=run_daemon_side, daemon=True)
    thread.start()
    try:
        pair_phone_in_browser(page, pwa_url, session.qr_payload(), device_name)
    finally:
        thread.join(timeout=20)

    if thread.is_alive():
        raise AssertionError("Daemon-side complete_pairing() never returned")
    if "error" in result:
        raise result["error"]
    return result["device"]


class TestPairingEndToEnd:
    def test_real_pairing_handshake_succeeds_both_sides(
        self, relay_url, signed_pwa, daemon_identity, browser, release_key,
    ):
        pwa_url, _bundle_dir = signed_pwa
        session = begin_pairing(relay_url, daemon_identity, pwa_release_public_key=release_key["public_key"])

        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        device = pair_and_complete(page, pwa_url, session, daemon_identity)

        assert not errors, f"JS errors during pairing: {errors}"
        assert device.device_name == "Test Phone"
        assert device.mailbox_id == session.mailbox_id

    def test_bundle_verifies_cleanly_after_a_real_pairing(
        self, relay_url, signed_pwa, daemon_identity, browser, release_key,
    ):
        pwa_url, _bundle_dir = signed_pwa
        session = begin_pairing(relay_url, daemon_identity, pwa_release_public_key=release_key["public_key"])

        page = browser.new_page()
        pair_and_complete(page, pwa_url, session, daemon_identity)

        page.wait_for_function(
            "document.getElementById('bundle-warning').classList.contains('hidden')", timeout=5000,
        )


class TestApprovalRoundTripEndToEnd:
    def test_pii_flagged_request_renders_and_approves(self, relay_url, signed_pwa, daemon_identity, browser, release_key):
        pwa_url, _bundle_dir = signed_pwa
        session = begin_pairing(relay_url, daemon_identity, pwa_release_public_key=release_key["public_key"])
        page = browser.new_page()
        device = pair_and_complete(page, pwa_url, session, daemon_identity)

        client = MobileRelayClient(device.to_config(relay_url))
        request_id = "req-approval-1"
        client.post_request(request_id, {
            "kind": "read_popup", "title": "gmail_get_message", "connector": "gmail",
            "preview": {"From": "alice@example.com"}, "details_text": "Full email body here.",
            "pii_flagged": True, "pii_categories": ["Email address"],
            "claude_reason": "Summarizing for the user.",
        }, ttl_seconds=30)

        page.wait_for_selector("#pending-screen:not(.hidden)", timeout=10000)
        pending_html = page.inner_html("#pending-card")
        assert "flagged personal data" in pending_html
        assert "Email address" in pending_html
        assert "gmail_get_message" in pending_html
        assert "Full email body here." in pending_html

        page.click("#approve-button")
        decision = client.poll_decision(request_id, overall_timeout_seconds=10)
        assert decision == "approved"

    def test_deny_round_trips_too(self, relay_url, signed_pwa, daemon_identity, browser, release_key):
        pwa_url, _bundle_dir = signed_pwa
        session = begin_pairing(relay_url, daemon_identity, pwa_release_public_key=release_key["public_key"])
        page = browser.new_page()
        device = pair_and_complete(page, pwa_url, session, daemon_identity)

        client = MobileRelayClient(device.to_config(relay_url))
        request_id = "req-approval-2"
        client.post_request(request_id, {"kind": "popup", "title": "gmail_send_message", "preview": {}}, ttl_seconds=30)

        page.wait_for_selector("#pending-screen:not(.hidden)", timeout=10000)
        page.click("#deny-button")
        decision = client.poll_decision(request_id, overall_timeout_seconds=10)
        assert decision == "denied"


class TestBundleTamperDetection:
    """The actual security property Phase 3's signing mechanism exists to
    prove: a bundle modified after signing must be detected, not silently
    served as if nothing happened."""

    def test_tampered_file_is_detected_after_pinning(self, signed_pwa, release_key, browser):
        pwa_url, bundle_dir = signed_pwa
        (bundle_dir / "js" / "app.js").write_text(
            (bundle_dir / "js" / "app.js").read_text() + "\n// tampered by attacker\n"
        )

        page = browser.new_page()
        page.goto(f"{pwa_url}/index.html")

        result = page.evaluate(
            """
            (publicKeyB64) => new Promise(async (resolve) => {
                navigator.serviceWorker.addEventListener("message", (event) => {
                    if (event.data && event.data.type === "bundle-verification-result"
                        && event.data.result.status !== "skipped") {
                        resolve(event.data.result);
                    }
                });
                const reg = await navigator.serviceWorker.ready;
                reg.active.postMessage({ type: "pin-release-key", publicKeyB64 });
            })
            """,
            base64.b64encode(release_key["public_key"]).decode("ascii"),
        )

        assert result["status"] == "failed"
        assert "app.js" in result["reason"]

    def test_before_any_pairing_verification_is_skipped_not_falsely_trusted(self, signed_pwa, browser):
        """No pinned key yet (nothing paired) -- there's nothing sensitive
        to protect, so this must report "skipped", never "verified" (which
        would be a false, unearned trust claim)."""
        pwa_url, _bundle_dir = signed_pwa
        page = browser.new_page()
        page.goto(f"{pwa_url}/index.html")

        result = page.evaluate(
            """
            () => new Promise((resolve) => {
                navigator.serviceWorker.addEventListener("message", (event) => {
                    if (event.data && event.data.type === "bundle-verification-result") {
                        resolve(event.data.result);
                    }
                });
            })
            """
        )

        assert result["status"] == "skipped"

    def test_wrong_pinned_key_is_rejected(self, signed_pwa, browser):
        """A key that never signed this bundle (e.g. a MITM trying to pin
        its own key) must fail verification, not succeed."""
        pwa_url, _bundle_dir = signed_pwa
        wrong_private_key_path_holder = {}
        # A second, unrelated release keypair -- simulates an attacker's own key.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            wrong_key_path = Path(tmp) / "wrong_key.json"
            generate_pwa_release_key.main(["-o", str(wrong_key_path)])
            wrong_public_key_b64 = json.loads(wrong_key_path.read_text())["public_key_base64"]

        page = browser.new_page()
        page.goto(f"{pwa_url}/index.html")

        result = page.evaluate(
            """
            (publicKeyB64) => new Promise(async (resolve) => {
                navigator.serviceWorker.addEventListener("message", (event) => {
                    if (event.data && event.data.type === "bundle-verification-result"
                        && event.data.result.status !== "skipped") {
                        resolve(event.data.result);
                    }
                });
                const reg = await navigator.serviceWorker.ready;
                reg.active.postMessage({ type: "pin-release-key", publicKeyB64 });
            })
            """,
            wrong_public_key_b64,
        )

        assert result["status"] == "failed"
        assert "signature" in result["reason"].lower()
