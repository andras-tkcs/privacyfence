"""Small, always-on Playwright suite driving the real web approval surface
in a real headless browser (docs/security-remediation-plan.md, Phase 2 item
2.5, TST-06).

Every other test of this surface (tests/unit/web/) drives it through either
``starlette.testclient.TestClient`` (an in-process ASGI transport, no real
socket -- see test_routes_approvals.py's own docstring) or, for the MCP
endpoint, a real socket but a scripted client (tests/integration/
test_mcp_daemon_contract.py). Neither exercises what only a real browser
actually does: parses and enforces the ``Content-Security-Policy`` header,
applies real cookie ``SameSite`` semantics to a real ``fetch()``, runs the
page's own JS event loop (SSE ``EventSource``, ``navigator.credentials``),
and lays out a real DOM. web/routes_approvals.py's own ``TestInjectShim``
class documents exactly the kind of bug that gap allowed through once
already: "a real bug found by actually clicking Allow in headless
Chromium... found by actually driving a served card in headless Chromium
and clicking Allow." This module is that manual repro, made permanent.

Requires the ``playwright`` package (test-only, see pyproject.toml's
``[project.optional-dependencies].test``) *and* a downloaded Chromium
build (``playwright install chromium``); skipped automatically if either
is missing, same posture test_shim_mcp_contract.py takes for a missing
Node binary -- this suite is "always-on" in the sense that CI always has
both (see .github/workflows/tests.yml's ``Install Playwright browsers``
step), not that it forces every contributor's machine to.

Both checks the plan's own TST-06 row lists (a no-inline-script CSP check
and "PDF preview actually renders") now assert real pass/fail outcomes --
see ``TestSecurityHeadersCsp``/``TestPdfPreview`` below. Both were
previously written as a skip/an ``xfail`` respectively, against the
pre-SEC-08 policy (docs/security-remediation-plan.md Phase 3.1):
``script-src``/``style-src`` were a blanket ``'unsafe-inline'`` with no
``object-src`` exception, which both left the "no-inline-script" check with
nothing real to assert against and left the card's own PDF ``<embed>``
blocked by the implicit ``default-src 'none'`` fallback (the "currently
likely broken -- no test catches this" bug the plan's Phase 3 table named
3.1 to fix). web/csp.py's ``build_csp()`` is what closed both.
"""
from __future__ import annotations

import http.server
import logging
import re
import socket
import threading
import time
import uuid
from pathlib import Path

import pytest

pytest.importorskip(
    "playwright.sync_api",
    reason="playwright (test-only) not installed -- pip install -e '.[test]' && playwright install chromium",
)
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from pypdf import PdfWriter  # noqa: E402

from privacyfence import org_identity as oi  # noqa: E402
from privacyfence import paths as paths_module  # noqa: E402
from privacyfence.principal import Principal  # noqa: E402
from privacyfence.web import org_session  # noqa: E402
from privacyfence.web.oauth_provider import OrgOAuthProvider  # noqa: E402
from privacyfence.web.org_session import OrgSessionStore  # noqa: E402
from privacyfence.web.routes_approvals import _DECIDED_MESSAGE, _DENIED_MESSAGE  # noqa: E402
from privacyfence.web.server import OrgAuth, WebServer  # noqa: E402
from privacyfence.web.session_auth import SESSION_COOKIE as _LOCAL_SESSION_COOKIE  # noqa: E402
from privacyfence.web_approval_ui import WebApprovalUI  # noqa: E402

pytestmark = pytest.mark.timeout(60)

ISSUER = "https://idp.example.com"


def _free_port() -> int:
    """See tests/integration/test_mcp_daemon_contract.py's own
    ``_free_port`` -- WebServer.start() doesn't report back the OS-assigned
    port for ``port=0``, so a real port number is needed before starting
    the server (and before a browser can be pointed at it)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_connectable(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    last_exc: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError as exc:
            last_exc = exc
            time.sleep(0.05)
    raise TimeoutError(f"{host}:{port} never became connectable") from last_exc


def _idp() -> oi.IdpConfig:
    """A syntactically real (never actually contacted) IdP config -- this
    suite never drives a real OIDC round trip (test_org_mcp_e2e.py already
    covers that, without a browser); org-mode sessions here are minted
    directly via OrgSessionStore.create(), the same shortcut
    test_routes_org_approvals.py's own ``_signed_in`` takes."""
    return oi.IdpConfig(
        issuer=ISSUER, client_id="privacyfence", client_secret="s",
        authorization_endpoint=f"{ISSUER}/authorize",
        token_endpoint=f"{ISSUER}/token", jwks_uri=f"{ISSUER}/jwks",
    )


def _pdf_bytes() -> bytes:
    """A real, valid single-page PDF -- built with ``pypdf`` (already a
    runtime dependency, see pyproject.toml) rather than hand-crafted bytes,
    so a failure to render is never mistaken for a malformed-input bug."""
    import io

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# --------------------------------------------------------------------- #
# Browser/server fixtures
# --------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def browser():
    """Module-, not session-, scoped: ``sync_playwright()``'s own greenlet-
    based dispatcher must be fully torn down before any *other* test
    module's ``asyncio``-based test runs in this same process, or
    ``asyncio.Runner``/``get_event_loop()`` state corrupts across the
    board -- confirmed by hand (a session-scoped instance here made every
    unrelated async test elsewhere in the suite fail with "Cannot run the
    event loop while another loop is running", pytest-asyncio's own
    ``asyncio.Runner`` teardown colliding with Playwright's still-open
    one). Module scope still reuses one browser across every test in this
    file (this module has no ``async def`` test of its own to interleave
    with), without holding it open past this module's own last test."""
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except PlaywrightError as exc:
            pytest.skip(f"Chromium not available for Playwright ({exc}) -- run `playwright install chromium`")
            return
        yield b
        b.close()


@pytest.fixture
def context(browser):
    ctx = browser.new_context(ignore_https_errors=True)
    yield ctx
    ctx.close()


@pytest.fixture
def page(context):
    pg = context.new_page()
    # Buffered here (not read live) so a failing test's own teardown
    # (``_capture_failure_artifacts`` below) has the full transcript to
    # write out, regardless of which point in the test the failure actually
    # happened at -- a listener attached only after a failure would have
    # missed everything already printed by then.
    pg.pf_console_log: list[str] = []  # type: ignore[attr-defined]
    pg.on("console", lambda msg: pg.pf_console_log.append(f"[console:{msg.type}] {msg.text}"))
    pg.on("pageerror", lambda exc: pg.pf_console_log.append(f"[pageerror] {exc}"))
    yield pg
    pg.close()


# Phase 4 item 4.5 (docs/automated-test-strategy-plan.md): "systematic
# failure-artifact capture (screenshot/console/DOM/daemon log)" -- checked
# against what this module and .github/workflows/tests.yml's own Playwright
# step already had before this landed (neither did anything beyond pytest's
# own default traceback/stdout capture), so this is new, not a duplicate of
# an existing mechanism.
_ARTIFACTS_DIR = Path(__file__).resolve().parents[2] / "test-results" / "browser-smoke"


@pytest.fixture(autouse=True)
def _capture_failure_artifacts(request, page, caplog):
    """On any failing test in this file, write four things next to each
    other under ``test-results/browser-smoke/<test id>.*`` (created lazily,
    only once a test actually fails -- nothing written on a passing run):
    a full-page screenshot, the served page's own current DOM
    (``page.content()``), the buffered browser console/pageerror transcript
    (``page``'s own ``pf_console_log`` above), and this suite's "daemon"
    log -- there's no separate OS process here (``local_server``/
    ``org_server`` run ``WebServer`` in-process on a background thread, see
    those fixtures), so the closest equivalent is whatever
    ``privacyfence.*`` actually logged during the test, which ``caplog``
    already captures once its level is lowered enough to catch it.

    Depends on ``page`` directly (every test in this module already takes
    it) rather than reaching for it via ``request.getfixturevalue`` -- this
    fixture's own teardown must run *after* the test body but *before*
    ``page``'s (``pg.close()``), while the underlying page/server are still
    alive to screenshot/read from; normal fixture teardown ordering
    (reverse of setup) guarantees exactly that here since this fixture is
    requested after ``page`` on every test's own parameter list.
    """
    caplog.set_level(logging.INFO)
    yield
    rep_call = getattr(request.node, "rep_call", None)
    if rep_call is None or not rep_call.failed:
        return
    _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    base = _ARTIFACTS_DIR / re.sub(r"[^\w.-]+", "_", request.node.nodeid)
    try:
        page.screenshot(path=f"{base}.png", full_page=True)
    except Exception as exc:  # pragma: no cover -- best-effort diagnostics
        (base.with_name(base.name + ".screenshot-error.txt")).write_text(str(exc), encoding="utf-8")
    try:
        (base.with_name(base.name + ".dom.html")).write_text(page.content(), encoding="utf-8")
    except Exception:  # pragma: no cover -- best-effort diagnostics
        pass
    console_log = getattr(page, "pf_console_log", [])
    if console_log:
        (base.with_name(base.name + ".console.log")).write_text("\n".join(console_log), encoding="utf-8")
    if caplog.text:
        (base.with_name(base.name + ".daemon.log")).write_text(caplog.text, encoding="utf-8")


@pytest.fixture
def pf_home(tmp_path, monkeypatch):
    """An isolated HOME so web_token/mcp_token/webauthn credentials land
    under a throwaway directory, not the real ``~/.privacyfence`` -- same
    posture as test_mcp_daemon_contract.py's own ``mcp_home`` fixture."""
    home = tmp_path / f"pf-home-{uuid.uuid4().hex[:8]}"
    (home / ".privacyfence").mkdir(parents=True)
    monkeypatch.setattr(paths_module, "data_dir", lambda: home / ".privacyfence")
    return home


@pytest.fixture
def local_server(pf_home):
    web_ui = WebApprovalUI()
    port = _free_port()
    server = WebServer(web_ui, host="localhost", port=port)
    server.start()
    try:
        _wait_until_connectable("localhost", port)
        yield server, web_ui
    finally:
        server.stop()


@pytest.fixture
def org_server(pf_home, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "privacyfence.web.oauth_provider._clients_file_path", lambda: str(tmp_path / "oauth_clients.json"),
    )
    port = _free_port()
    issuer_url = f"http://localhost:{port}"
    idp = _idp()
    provider = OrgOAuthProvider(idp, idp_callback_url=f"{issuer_url}/oauth/idp/callback")
    sessions = OrgSessionStore()
    org = OrgAuth(
        provider=provider, sessions=sessions, idp=idp, issuer_url=issuer_url,
        # step_up.enabled doesn't need to be True for this suite -- only
        # /security (webauthn_stepup.py's enrollment ceremony) is under
        # test here, not the write-approval step-up gate itself.
        org_config={"step_up": {"enabled": True}},
    )
    server = WebServer(WebApprovalUI(), host="localhost", port=port, org=org)
    server.start()
    try:
        _wait_until_connectable("localhost", port)
        yield server, sessions
    finally:
        server.stop()


def _register_card(web_ui: WebApprovalUI, **kwargs) -> tuple[threading.Thread, object]:
    """Starts a blocking show_read_popup()/show_popup() call on a
    background (daemon) thread and waits for its card to register -- same
    pattern as test_routes_approvals.py's own ``_pending_card``, reused
    here rather than imported: this module has no other dependency on
    tests/unit/web/, and the two suites are meant to stay independently
    runnable (one needs a browser, the other doesn't)."""
    read = kwargs.pop("read", False)
    box: dict = {}

    def run():
        fn = web_ui.show_read_popup if read else web_ui.show_popup
        args = ("Send email", {"To": "a@b.com"}, "body text") if not read else (
            "Shared document", {"From": "a@b.com"}, "body text", None,
        )
        box["result"] = fn(*args, **kwargs)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    deadline = time.monotonic() + 5
    while web_ui.current() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    card = web_ui.current()
    assert card is not None, "card never registered"
    # Stashed on the thread itself (rather than returned as a third tuple
    # element) so every existing two-value `thread, card = _register_card(...)`
    # call site keeps working unchanged -- only tests that actually care what
    # the blocked show_popup()/show_read_popup() call resolved to need reach
    # for it, via `thread.result_box.get("result")` once `thread.join()`
    # confirms it's populated.
    t.result_box = box
    return t, card


def _register_confirm(web_ui: WebApprovalUI, categories: list[str]) -> tuple[threading.Thread, object]:
    """Same pattern as ``_register_card`` above, but for the PII/rule
    confirmation dialog shape (``show_pii_confirmation_popup``) -- a bare
    ``bool``, not a ``(result, choice)`` tuple, is what the blocked call
    resolves to; stashed on the thread the same way (``thread.result_box``)
    so a caller that cares can confirm which of Proceed/Cancel actually won."""
    box: dict = {}

    def run():
        box["result"] = web_ui.show_pii_confirmation_popup(categories)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    deadline = time.monotonic() + 5
    while web_ui.current() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    card = web_ui.current()
    assert card is not None, "confirmation dialog never registered"
    t.result_box = box
    return t, card


# --------------------------------------------------------------------- #
# Bootstrap + login (SEC-06)
# --------------------------------------------------------------------- #


class TestBootstrapLogin:
    def test_bootstrap_link_signs_in_and_leaves_no_credential_in_the_url(self, page, local_server):
        """The single-use ``?bootstrap=<code>`` link (web/server.py's
        ``_BootstrapMiddleware``) actually signs a real browser in, *and*
        the resulting page URL -- what a real browser bar, history entry,
        and Referer header would carry -- contains neither the bootstrap
        code nor any other credential, per SEC-06's whole point (see
        web/session_auth.py's module docstring). ASGI TestClient assertions
        already cover the redirect response's own Location header (test_
        server.py) -- this proves a real browser actually lands there with
        nothing left in its own address bar, not just that the server sent
        the right header."""
        server, _web_ui = local_server
        url = server.mint_bootstrap_url("/approvals")
        assert "bootstrap=" in url  # sanity: the link under test does carry one

        page.goto(url)
        page.wait_for_load_state("load")

        assert page.url == f"{server.base_url}/approvals"
        assert "bootstrap=" not in page.url
        assert "token=" not in page.url
        assert page.get_by_text("Nothing is waiting.").is_visible()

    def test_bootstrap_code_is_single_use(self, page, context, local_server):
        server, _web_ui = local_server
        url = server.mint_bootstrap_url("/approvals")
        page.goto(url)
        page.wait_for_load_state("load")

        # Same code again, fresh browser context (no session cookie this
        # time) -- BootstrapStore.consume() already deletes on first use
        # (unit-tested in test_session_auth.py); this proves a real browser
        # actually gets turned away, not just the store's own bookkeeping.
        other = context.browser.new_context()
        try:
            fresh_page = other.new_page()
            fresh_page.goto(url)
            fresh_page.wait_for_load_state("load")
            assert fresh_page.get_by_text("Not authorized").is_visible()
        finally:
            other.close()

    def test_unauthenticated_visitor_sees_the_sign_in_message(self, page, local_server):
        server, _web_ui = local_server
        page.goto(f"{server.base_url}/approvals")
        page.wait_for_load_state("load")
        assert page.get_by_text("Not authorized").is_visible()


# --------------------------------------------------------------------- #
# Allow/Deny, CSRF rejection, live list refresh
# --------------------------------------------------------------------- #


def _sign_in_local(page, server) -> None:
    page.goto(server.mint_bootstrap_url("/approvals"))
    page.wait_for_load_state("load")


def _sign_in_org(context, server, sessions: OrgSessionStore, *, principal: Principal) -> None:
    session_id = sessions.create(principal)
    context.add_cookies([{
        "name": org_session.SESSION_COOKIE, "value": session_id, "url": server.base_url,
        "httpOnly": True, "secure": True, "sameSite": "Strict",
    }])


class TestApprovalDecisionFlow:
    def test_allow_once_from_the_card_resolves_the_pending_call(self, page, local_server):
        """The regression test for the bug test_routes_approvals.py's
        ``TestInjectShim`` documents finding by hand: clicking the real
        "Allow once" button on a real served card must actually post a
        decision and unblock gate.py's caller -- not silently no-op because
        the injected bridge shim landed inside a `<style>` comment instead
        of `<body>`."""
        server, web_ui = local_server
        _sign_in_local(page, server)
        thread, card = _register_card(web_ui)
        try:
            page.goto(f"{server.base_url}/approvals/{card.id}")
            page.wait_for_load_state("load")
            page.locator('[data-pf-action="accept"]').click()
            page.wait_for_url(f"{server.base_url}/approvals")
            thread.join(timeout=5)
            assert not thread.is_alive()
        finally:
            if thread.is_alive():
                web_ui.resolve(card.id, "deny")
                thread.join(timeout=5)

    def test_deny_from_the_list_row_resolves_the_pending_call_without_opening_the_card(self, page, local_server):
        """§2.2's asymmetry (approval_list_html.py's own docstring): Deny is
        on the row, Allow never is. Exercises that row-level POST -- a
        separate JS path from the card page's own bridge shim above -- from
        a real click, with no navigation to the card at all."""
        server, web_ui = local_server
        _sign_in_local(page, server)
        thread, card = _register_card(web_ui)
        try:
            page.goto(f"{server.base_url}/approvals")
            page.wait_for_selector(f'[data-approval-id="{card.id}"]')
            page.locator(f'[data-approval-id="{card.id}"] [data-deny]').click()
            page.wait_for_selector(f'[data-approval-id="{card.id}"]', state="detached")
            thread.join(timeout=5)
            assert not thread.is_alive()
        finally:
            if thread.is_alive():
                web_ui.resolve(card.id, "deny")
                thread.join(timeout=5)

    def test_wrong_csrf_value_is_rejected(self, page, local_server):
        """A same-origin ``fetch()`` -- the real session cookie attached
        automatically, exactly as a forged request from any same-site page
        would carry it too -- but a wrong body ``csrf`` value: the
        double-submit check (session_auth.check_csrf) must still reject it.
        Proven with a real ``fetch()``/cookie jar, not TestClient's
        stand-in for one."""
        server, web_ui = local_server
        _sign_in_local(page, server)
        thread, card = _register_card(web_ui)
        try:
            status = page.evaluate(
                """async (url) => {
                    const r = await fetch(url, {
                        method: 'POST', credentials: 'same-origin',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({result: 'accept', csrf: 'not-the-real-csrf-value'}),
                    });
                    return r.status;
                }""",
                f"{server.base_url}/api/approvals/{card.id}/decide",
            )
            assert status == 401
            assert not card.event.is_set()
        finally:
            web_ui.resolve(card.id, "deny")
            thread.join(timeout=5)

    def test_cross_site_request_cannot_carry_the_session_cookie(self, page, context, local_server):
        """The defense-in-depth layer only a real browser can prove: the
        ``pf_session`` cookie is ``SameSite=Strict`` (session_auth.
        set_session_cookie), so a page served from a *different* origin
        attempting the exact same decide POST -- even with
        ``credentials: 'include'`` -- never gets the cookie attached at
        all, regardless of what csrf value it guesses. A second, unrelated
        loopback HTTP server stands in for "attacker-controlled site"."""
        server, web_ui = local_server
        _sign_in_local(page, server)
        thread, card = _register_card(web_ui)

        attacker_port = _free_port()
        decide_url = f"{server.base_url}/api/approvals/{card.id}/decide"
        html = (
            "<!doctype html><html><body><script>"
            f"fetch({decide_url!r}, {{method:'POST', credentials:'include', mode:'no-cors',"
            "headers:{'Content-Type':'text/plain'},"
            f"body: JSON.stringify({{result:'accept', csrf:'guess'}})}});"
            "</script></body></html>"
        )

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # noqa: D401
                pass

        httpd = http.server.HTTPServer(("127.0.0.1", attacker_port), _Handler)
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()
        try:
            attacker_page = context.new_page()
            attacker_page.goto(f"http://127.0.0.1:{attacker_port}/")
            attacker_page.wait_for_timeout(500)  # let the fire-and-forget fetch land
            attacker_page.close()

            # The forged, cookie-less request must have been rejected --
            # the approval is still genuinely pending, provable from the
            # legitimate, signed-in first-party page.
            page.goto(f"{server.base_url}/approvals/{card.id}")
            page.wait_for_load_state("load")
            assert page.locator('[data-pf-action="accept"]').count() == 1
            assert not card.event.is_set()
        finally:
            httpd.shutdown()
            httpd.server_close()
            web_ui.resolve(card.id, "deny")
            thread.join(timeout=5)

    def test_list_refreshes_live_without_a_manual_reload(self, page, local_server):
        """docs/approval-list-ui-ux.md's ``/api/approvals/stream`` SSE
        push, driven by a real ``EventSource`` in a real page -- a new
        approval registered *after* the page already loaded must appear
        with no navigation/reload, within the stream's own ~1s poll
        interval (web/state_stream.py's ``_APPROVALS_POLL_SECONDS``)."""
        server, web_ui = local_server
        _sign_in_local(page, server)
        assert page.get_by_text("Nothing is waiting.").is_visible()

        thread, card = _register_card(web_ui)
        try:
            page.wait_for_selector(f'[data-approval-id="{card.id}"]', timeout=5000)
        finally:
            web_ui.resolve(card.id, "deny")
            thread.join(timeout=5)


# --------------------------------------------------------------------- #
# Approval-list behaviors beyond one Allow/Deny round trip (Phase 4 item
# 4.1, docs/automated-test-strategy-plan.md) -- everything that section's
# own "Already in this repo" bullet lists as still missing from
# TestApprovalDecisionFlow above: the empty state on its own, "Always
# allow"'s (result, choice) round trip, the post-decision toast surviving
# more than one decision, live SSE refresh with several cards pending at
# once, and double-submit idempotency.
# --------------------------------------------------------------------- #


class TestApprovalListBehavior:
    def test_empty_list_shows_nothing_is_waiting(self, page, local_server):
        server, _web_ui = local_server
        _sign_in_local(page, server)
        page.goto(f"{server.base_url}/approvals")
        page.wait_for_load_state("load")
        assert page.get_by_text("Nothing is waiting.").is_visible()
        assert page.get_by_text("PrivacyFence is watching.").is_visible()

    def test_always_allow_resolves_with_the_picked_candidate_and_returns_to_list(self, page, local_server):
        """Clicking the card's own "Always allow" button must post
        ``data-pf-choice``'s index alongside ``data-pf-action="accept_all"``
        (approval_window_html.py's ``_button_row_html``) -- the signal
        gate.py's caller uses to know *which* matching candidate rule to
        actually propose (test_gate.py's own "Accept all" classes cover
        that side unit-tested; this is the browser-observable half: the
        right ``(result, choice)`` tuple actually reaches the blocked
        ``show_popup()`` call, and the card still returns to the list with
        its normal "decided" toast like any other decision)."""
        server, web_ui = local_server
        _sign_in_local(page, server)
        thread, card = _register_card(
            web_ui, accept_all_choices=[("Always allow — gmail.com", "gmail.com")],
        )
        try:
            page.goto(f"{server.base_url}/approvals/{card.id}")
            page.wait_for_load_state("load")
            page.locator('[data-pf-action="accept_all"]').click()
            page.wait_for_url(f"{server.base_url}/approvals")
            thread.join(timeout=5)
            assert not thread.is_alive()
            assert thread.result_box.get("result") == ("accept_all", 0)
            toast = page.locator("#pf-shell-toast")
            assert toast.text_content() == _DECIDED_MESSAGE
            assert "shown" in (toast.get_attribute("class") or "")
        finally:
            if thread.is_alive():
                web_ui.resolve(card.id, "deny")
                thread.join(timeout=5)

    def test_toast_message_reflects_each_decision_across_successive_round_trips(self, page, local_server):
        """The sessionStorage-relayed post-decision toast (_bridge_shim's
        own isDeny branch) set fresh for *each* card decided from a
        separate round trip -- not just once, and not left showing the
        first decision's message on the second. Allow, then Deny, in
        sequence on the same already-open list page."""
        server, web_ui = local_server
        _sign_in_local(page, server)

        thread_a, card_a = _register_card(web_ui)
        try:
            page.goto(f"{server.base_url}/approvals/{card_a.id}")
            page.wait_for_load_state("load")
            page.locator('[data-pf-action="accept"]').click()
            page.wait_for_url(f"{server.base_url}/approvals")
            thread_a.join(timeout=5)
            assert page.locator("#pf-shell-toast").text_content() == _DECIDED_MESSAGE
        finally:
            if thread_a.is_alive():
                web_ui.resolve(card_a.id, "deny")
                thread_a.join(timeout=5)

        thread_b, card_b = _register_card(web_ui)
        try:
            page.goto(f"{server.base_url}/approvals/{card_b.id}")
            page.wait_for_load_state("load")
            page.locator('[data-pf-action="deny"]').click()
            page.wait_for_url(f"{server.base_url}/approvals")
            thread_b.join(timeout=5)
            assert page.locator("#pf-shell-toast").text_content() == _DENIED_MESSAGE
        finally:
            if thread_b.is_alive():
                web_ui.resolve(card_b.id, "deny")
                thread_b.join(timeout=5)

    def test_sse_refreshes_the_list_live_with_multiple_pending_cards(self, page, local_server):
        """Two cards pending at once (possible since gate.py's
        ``_popup_lock`` removal, per this module's own routes_approvals.py
        docstring) -- resolving one must drop only that row from an
        already-open list page via the SSE stream, live, leaving the other
        one in place, then drop the second down to the empty state too."""
        server, web_ui = local_server
        _sign_in_local(page, server)
        thread_a, card_a = _register_card(web_ui)
        thread_b, card_b = _register_card(web_ui)
        try:
            page.goto(f"{server.base_url}/approvals")
            page.wait_for_selector(f'[data-approval-id="{card_a.id}"]')
            page.wait_for_selector(f'[data-approval-id="{card_b.id}"]')

            # Resolved out-of-band (as a second tab, or a native path,
            # would) -- this tab's own SSE stream must pick it up with no
            # reload.
            web_ui.resolve(card_a.id, "deny")
            thread_a.join(timeout=5)
            page.wait_for_selector(f'[data-approval-id="{card_a.id}"]', state="detached", timeout=5000)
            # approval_list_html.py's render() does one atomic innerHTML swap per
            # "approvals" SSE event using whatever rows snapshot the server just
            # polled (state_stream.py's _APPROVALS_POLL_SECONDS) -- the snapshot
            # that first drops card_a isn't guaranteed to be the same snapshot
            # that still carries card_b, if two poll ticks land close together.
            # Wait for card_b's row explicitly (same as card_a's own detached
            # wait above) instead of asserting immediately, so a transient
            # empty-then-repopulated render doesn't read as a real assertion
            # failure.
            page.wait_for_selector(f'[data-approval-id="{card_b.id}"]', timeout=5000)
            assert page.locator(f'[data-approval-id="{card_b.id}"]').count() == 1

            web_ui.resolve(card_b.id, "deny")
            thread_b.join(timeout=5)
            page.wait_for_selector(f'[data-approval-id="{card_b.id}"]', state="detached", timeout=5000)
            assert page.get_by_text("Nothing is waiting.").is_visible()
        finally:
            for thread, card in ((thread_a, card_a), (thread_b, card_b)):
                if thread.is_alive():
                    web_ui.resolve(card.id, "deny")
                    thread.join(timeout=5)

    def test_double_submitting_the_same_decision_is_idempotent(self, page, context, local_server):
        """A slow-network retry (or an over-eager double click) posting the
        exact same decision twice must resolve the pending call exactly
        once: the first POST wins (200), the second is turned away as
        ``already_decided`` (409, §7.1's own idempotency guarantee) rather
        than raising or double-resolving the already-unblocked
        ``show_popup()`` call. Driven with a raw ``fetch()`` (same approach
        as ``test_wrong_csrf_value_is_rejected`` above) since the real
        button click navigates away after the first response, leaving
        nothing on screen to click a genuine second time."""
        server, web_ui = local_server
        _sign_in_local(page, server)
        thread, card = _register_card(web_ui)
        csrf = next(c["value"] for c in context.cookies() if c["name"] == _LOCAL_SESSION_COOKIE)
        decide_url = f"{server.base_url}/api/approvals/{card.id}/decide"
        try:
            statuses = page.evaluate(
                """async (args) => {
                    const body = JSON.stringify({result: 'accept', csrf: args.csrf});
                    const opts = {method: 'POST', credentials: 'same-origin',
                        headers: {'Content-Type': 'application/json'}, body: body};
                    const r1 = await fetch(args.url, opts);
                    const r2 = await fetch(args.url, opts);
                    return [r1.status, r2.status];
                }""",
                {"url": decide_url, "csrf": csrf},
            )
            assert statuses == [200, 409]
            thread.join(timeout=5)
            assert not thread.is_alive()
            assert thread.result_box.get("result") == ("accept", None)
        finally:
            if thread.is_alive():
                web_ui.resolve(card.id, "deny")
                thread.join(timeout=5)


# --------------------------------------------------------------------- #
# PII behavior (Phase 4 item 4.2, docs/automated-test-strategy-plan.md):
# deterministic synthetic PII triggers the banner/tint on a review-gate
# card, an unrelated operation never gets it, and the separate PII/rule
# confirmation dialog (show_pii_confirmation_popup, dialog_window_html.py's
# build_confirmation_html) actually resolves Proceed/Cancel to the right
# boolean from a real click -- the same "does the (result, choice) tuple
# gate.py's caller needs actually reach the blocked call" concern
# TestApprovalListBehavior's own "Always allow" test raised, for this
# dialog's own bool-shaped contract instead.
# --------------------------------------------------------------------- #


class TestPiiApprovalUi:
    def test_pii_banner_lists_the_matched_categories_on_a_review_gate_card(self, page, local_server):
        """approval_window_html.py's ``_risk_section_html``: a read-gate
        card carrying ``pii_categories`` renders the "Possible PII
        detected" risk card, with every matched category rendered as its
        own visible tag -- not just present in the HTML source, but
        actually laid out and visible in a real browser."""
        server, web_ui = local_server
        _sign_in_local(page, server)
        thread, card = _register_card(
            web_ui, read=True, pii_categories=["Email address", "Phone number"],
        )
        try:
            page.goto(f"{server.base_url}/approvals/{card.id}")
            page.wait_for_load_state("load")
            assert page.get_by_text("Possible PII detected").is_visible()
            assert page.get_by_text("Email address").is_visible()
            assert page.get_by_text("Phone number").is_visible()
        finally:
            web_ui.resolve(card.id, "deny")
            thread.join(timeout=5)

    def test_an_unrelated_operation_never_shows_the_pii_banner(self, page, local_server):
        """The negative case only means something next to the positive one
        above: an ordinary card with no PII match at all (a plain write-gate
        popup, same shape ``TestApprovalDecisionFlow`` already exercises)
        must never render the risk card -- ``_risk_section_html`` returns
        empty for an empty category list, and this proves that reaches the
        real served page, not just the Python-level HTML string."""
        server, web_ui = local_server
        _sign_in_local(page, server)
        thread, card = _register_card(web_ui)
        try:
            page.goto(f"{server.base_url}/approvals/{card.id}")
            page.wait_for_load_state("load")
            assert page.get_by_text("Possible PII detected").count() == 0
        finally:
            web_ui.resolve(card.id, "deny")
            thread.join(timeout=5)

    def test_proceed_on_the_pii_confirmation_dialog_resolves_true(self, page, local_server):
        server, web_ui = local_server
        _sign_in_local(page, server)
        thread, card = _register_confirm(web_ui, ["Email address"])
        try:
            page.goto(f"{server.base_url}/approvals/{card.id}")
            page.wait_for_load_state("load")
            assert page.get_by_text("PrivacyFence — Possible PII Detected").is_visible()
            page.locator('[data-pf-action="confirm"]').click()
            page.wait_for_url(f"{server.base_url}/approvals")
            thread.join(timeout=5)
            assert not thread.is_alive()
            assert thread.result_box.get("result") is True
            assert page.locator("#pf-shell-toast").text_content() == _DECIDED_MESSAGE
        finally:
            if thread.is_alive():
                web_ui.resolve(card.id, "cancel")
                thread.join(timeout=5)

    def test_cancel_on_the_pii_confirmation_dialog_resolves_false(self, page, local_server):
        server, web_ui = local_server
        _sign_in_local(page, server)
        thread, card = _register_confirm(web_ui, ["Email address"])
        try:
            page.goto(f"{server.base_url}/approvals/{card.id}")
            page.wait_for_load_state("load")
            page.locator('[data-pf-action="cancel"]').click()
            page.wait_for_url(f"{server.base_url}/approvals")
            thread.join(timeout=5)
            assert not thread.is_alive()
            assert thread.result_box.get("result") is False
            assert page.locator("#pf-shell-toast").text_content() == _DENIED_MESSAGE
        finally:
            if thread.is_alive():
                web_ui.resolve(card.id, "cancel")
                thread.join(timeout=5)


# --------------------------------------------------------------------- #
# Responsive layout (Phase 4 item 4.3, docs/automated-test-strategy-plan.md):
# named viewports (phone/tablet/desktop) -- no horizontal page scroll, the
# WIDE layout's two-column split actually stacks below approval_window_
# html.py's own 700px breakpoint, primary actions stay reachable, and the
# separate (native-window-shaped) confirmation dialog fits a phone viewport
# too, not just the card documents.
# --------------------------------------------------------------------- #

_VIEWPORTS = {
    "phone": {"width": 375, "height": 812},
    "tablet": {"width": 768, "height": 1024},
    "desktop": {"width": 1280, "height": 800},
}


def _assert_no_horizontal_overflow(page) -> None:
    scroll_width = page.evaluate("document.documentElement.scrollWidth")
    client_width = page.evaluate("document.documentElement.clientWidth")
    assert scroll_width <= client_width, f"page scrolls horizontally: {scroll_width} > {client_width}"


class TestResponsiveLayout:
    @pytest.mark.parametrize("viewport_name", sorted(_VIEWPORTS))
    def test_approval_list_has_no_horizontal_overflow(self, page, local_server, viewport_name):
        server, _web_ui = local_server
        page.set_viewport_size(_VIEWPORTS[viewport_name])
        _sign_in_local(page, server)
        page.goto(f"{server.base_url}/approvals")
        page.wait_for_load_state("load")
        _assert_no_horizontal_overflow(page)

    @pytest.mark.parametrize("viewport_name", sorted(_VIEWPORTS))
    def test_pending_card_has_no_overflow_and_primary_actions_stay_visible(
        self, page, local_server, viewport_name,
    ):
        server, web_ui = local_server
        page.set_viewport_size(_VIEWPORTS[viewport_name])
        _sign_in_local(page, server)
        # WIDE -- the two-column layout with the most to go wrong at a
        # narrow width (see the stacking test below); NARROW has no second
        # column to overflow in the first place.
        thread, card = _register_card(web_ui, read=True, layout="wide")
        try:
            page.goto(f"{server.base_url}/approvals/{card.id}")
            page.wait_for_load_state("load")
            _assert_no_horizontal_overflow(page)
            assert page.locator('[data-pf-action="accept"]').is_visible()
            assert page.locator('[data-pf-action="deny"]').is_visible()
        finally:
            web_ui.resolve(card.id, "deny")
            thread.join(timeout=5)

    def test_wide_layout_columns_stack_at_phone_width_but_sit_side_by_side_on_desktop(
        self, page, local_server,
    ):
        """styles.css's ``.pf-wide-row``/``@media (max-width: 700px)``
        block: two columns (row) above the breakpoint, stacked (column)
        below it -- a real computed-style assertion, not just "the CSS rule
        exists in the source", the same posture ``TestColorScheme`` below
        takes for the dark-mode tokens."""
        server, web_ui = local_server
        _sign_in_local(page, server)
        thread, card = _register_card(web_ui, read=True, layout="wide")
        try:
            page.set_viewport_size(_VIEWPORTS["phone"])
            page.goto(f"{server.base_url}/approvals/{card.id}")
            page.wait_for_load_state("load")
            assert page.evaluate(
                "getComputedStyle(document.querySelector('.pf-wide-row')).flexDirection"
            ) == "column"

            page.set_viewport_size(_VIEWPORTS["desktop"])
            page.reload()
            page.wait_for_load_state("load")
            assert page.evaluate(
                "getComputedStyle(document.querySelector('.pf-wide-row')).flexDirection"
            ) == "row"
        finally:
            web_ui.resolve(card.id, "deny")
            thread.join(timeout=5)

    def test_pii_confirmation_dialog_fits_a_phone_viewport(self, page, local_server):
        """The regression test for the bug this check found: dialog_window_
        html.py's ``_document()`` used to give the confirmation/choice
        dialogs a bare fixed ``width: {width}px`` -- correct for the native
        host, which sizes its own window frame to exactly that width, but
        an unconditional overflow once the exact same document is served
        into an ordinary (narrower) browser tab/phone viewport, as
        web_approval_ui.py's ``show_pii_confirmation_popup`` does. Fixed to
        ``width: min({width}px, 100%)``, the same responsive shape
        approval_window_html.py's own card documents already used."""
        server, web_ui = local_server
        _sign_in_local(page, server)
        page.set_viewport_size(_VIEWPORTS["phone"])
        thread, card = _register_confirm(web_ui, ["Email address"])
        try:
            page.goto(f"{server.base_url}/approvals/{card.id}")
            page.wait_for_load_state("load")
            _assert_no_horizontal_overflow(page)
            assert page.locator('[data-pf-action="confirm"]').is_visible()
            assert page.locator('[data-pf-action="cancel"]').is_visible()
        finally:
            web_ui.resolve(card.id, "cancel")
            thread.join(timeout=5)


# --------------------------------------------------------------------- #
# Light/dark mode (Phase 4 item 4.4, docs/automated-test-strategy-plan.md):
# structural assertions only (element presence, and that the dark-mode
# design tokens actually took effect on a real computed style) -- not pixel
# comparison, per that item's own text; subjective visual quality
# (contrast, "does this look right") stays manual (docs/testing-policy.md's
# governing rule on what stays manual).
# --------------------------------------------------------------------- #


class TestColorScheme:
    @pytest.mark.parametrize("color_scheme", ["light", "dark"])
    def test_approval_list_renders_in_both_color_schemes(self, page, local_server, color_scheme):
        server, _web_ui = local_server
        page.emulate_media(color_scheme=color_scheme)
        _sign_in_local(page, server)
        page.goto(f"{server.base_url}/approvals")
        page.wait_for_load_state("load")
        assert page.get_by_text("Nothing is waiting.").is_visible()
        assert page.get_by_text("PrivacyFence is watching.").is_visible()

    @pytest.mark.parametrize("color_scheme", ["light", "dark"])
    def test_pending_card_renders_in_both_color_schemes(self, page, local_server, color_scheme):
        server, web_ui = local_server
        page.emulate_media(color_scheme=color_scheme)
        _sign_in_local(page, server)
        thread, card = _register_card(
            web_ui, read=True, pii_categories=["Email address"], layout="wide",
        )
        try:
            page.goto(f"{server.base_url}/approvals/{card.id}")
            page.wait_for_load_state("load")
            assert page.get_by_text("Possible PII detected").is_visible()
            assert page.locator('[data-pf-action="accept"]').is_visible()
            assert page.locator('[data-pf-action="deny"]').is_visible()
        finally:
            web_ui.resolve(card.id, "deny")
            thread.join(timeout=5)

    def test_the_dark_tokens_actually_take_effect_not_just_the_light_ones_twice(self, page, local_server):
        """The positive-and-negative pairing every other structural check in
        this class needs to mean anything: both parametrized tests above
        pass even if ``prefers-color-scheme: dark`` silently fell back to
        the light palette (element presence alone can't tell the
        difference) -- this proves the page's own background color, driven
        by styles.css's ``@media (prefers-color-scheme: dark)`` block,
        genuinely differs between the two, on the same page, in the same
        browser context."""
        server, _web_ui = local_server
        _sign_in_local(page, server)
        page.emulate_media(color_scheme="light")
        page.goto(f"{server.base_url}/approvals")
        page.wait_for_load_state("load")
        light_bg = page.evaluate("getComputedStyle(document.body).backgroundColor")

        page.emulate_media(color_scheme="dark")
        page.reload()
        page.wait_for_load_state("load")
        dark_bg = page.evaluate("getComputedStyle(document.body).backgroundColor")

        assert light_bg != dark_bg


# --------------------------------------------------------------------- #
# PDF preview (part of TST-06's own list) -- see module docstring for why
# this is xfail, not a plain pass/fail assertion.
# --------------------------------------------------------------------- #


class TestPdfPreview:
    def test_pdf_embed_is_not_blocked_by_csp(self, page, local_server):
        server, web_ui = local_server
        _sign_in_local(page, server)
        # layout="wide": approval_window_html.build_preview_body_html's own
        # docstring -- "NARROW has no preview at all... callers never need
        # this for a narrow-shape tool" -- the PDF <embed> only ever
        # renders in the WIDE layout's right-hand preview pane.
        thread, card = _register_card(web_ui, read=True, pdf_bytes=_pdf_bytes(), layout="wide")
        try:
            page.add_init_script(
                """
                window.__pfCspViolations = [];
                document.addEventListener('securitypolicyviolation', function (e) {
                    window.__pfCspViolations.push({directive: e.violatedDirective, blockedURI: e.blockedURI});
                });
                """
            )
            page.goto(f"{server.base_url}/approvals/{card.id}")
            page.wait_for_load_state("load")
            embed = page.locator('embed[type="application/pdf"]')
            assert embed.count() == 1, "the card should render a PDF <embed> at all"
            violations = page.evaluate("window.__pfCspViolations")
            assert violations == [], f"PDF <embed> blocked by CSP: {violations}"
        finally:
            web_ui.resolve(card.id, "deny")
            thread.join(timeout=5)


# --------------------------------------------------------------------- #
# CSP: no-inline-script (SEC-08/Phase 3.1)
# --------------------------------------------------------------------- #


class TestSecurityHeadersCsp:
    def test_script_src_and_style_src_carry_no_bare_unsafe_inline(self, page, local_server):
        """web/csp.py's build_csp(): script-src and style-src-elem take a
        per-response nonce, not 'unsafe-inline' -- style-src-attr is the
        one deliberate exception (see that module's own docstring for why),
        so this checks the *directive name* 'style-src' exactly, not any
        occurrence of the substring, to avoid a false pass/fail on
        style-src-attr's own value."""
        server, web_ui = local_server
        _sign_in_local(page, server)
        response = page.goto(f"{server.base_url}/approvals")
        csp = response.headers.get("content-security-policy", "")
        directives = dict(
            part.strip().split(" ", 1) for part in csp.split(";") if part.strip()
        )
        assert "script-src" in directives, csp
        assert "unsafe-inline" not in directives["script-src"]
        assert "'nonce-" in directives["script-src"]
        assert "style-src-elem" in directives, csp
        assert "unsafe-inline" not in directives["style-src-elem"]
        assert "'nonce-" in directives["style-src-elem"]

    def test_inline_script_injected_via_dom_never_executes(self, page, local_server):
        """A script element with no ``nonce`` attribute at all (what any
        HTML-injection bug would actually produce -- an attacker has no way
        to know the response's own nonce value) must never run, under the
        real CSP enforcement only an actual browser applies -- see this
        module's own docstring on why TestClient can't stand in here."""
        server, web_ui = local_server
        _sign_in_local(page, server)
        page.goto(f"{server.base_url}/approvals")
        page.evaluate(
            """() => {
                window.__pfInjectedRan = false;
                var s = document.createElement('script');
                s.textContent = 'window.__pfInjectedRan = true;';
                document.body.appendChild(s);
            }"""
        )
        # No API waits on a script *not* running -- give the (blocked)
        # element a moment it would need if it were somehow going to run.
        page.wait_for_timeout(200)
        assert page.evaluate("window.__pfInjectedRan") is False

    def test_correctly_nonced_inline_script_still_runs(self, page, local_server):
        """The negative check above only means something next to a positive
        one: the CSP isn't simply blocking every inline <script> outright
        (which would also break the real page) -- a <script> carrying this
        exact response's own nonce is allowed to run, same as
        approval_list_html.py's/web_shell.py's own inline scripts already
        do on every real page load."""
        server, web_ui = local_server
        _sign_in_local(page, server)
        response = page.goto(f"{server.base_url}/approvals")
        csp = response.headers.get("content-security-policy", "")
        nonce = next(
            part.split("'nonce-", 1)[1].rstrip("'")
            for part in csp.split(";")
            if part.strip().startswith("script-src")
        )
        page.evaluate(
            """(nonce) => {
                window.__pfNoncedRan = false;
                var s = document.createElement('script');
                s.nonce = nonce;
                s.textContent = 'window.__pfNoncedRan = true;';
                document.body.appendChild(s);
            }""",
            nonce,
        )
        page.wait_for_timeout(200)
        assert page.evaluate("window.__pfNoncedRan") is True


# --------------------------------------------------------------------- #
# Org mode: WebAuthn UI error handling with a mock (failing) authenticator
# --------------------------------------------------------------------- #


class TestOrgModeWebAuthnUi:
    def test_authenticator_failure_surfaces_an_inline_error_not_a_silent_no_op(self, page, context, org_server):
        """A mock authenticator that always rejects (stands in for a user
        cancelling the platform prompt, or a browser with no authenticator
        at all) -- proves web/routes_security.py's own catch() path
        actually reaches the DOM (``#pf-passkey-status``), the button is
        re-enabled rather than left stuck disabled, and nothing throws an
        uncaught error into the console instead."""
        server, sessions = org_server
        principal = Principal(id="alice", email="alice@example.com", display_name="Alice")
        _sign_in_org(context, server, sessions, principal=principal)

        page.add_init_script(
            """
            navigator.credentials.create = function () {
                return Promise.reject(new DOMException('User declined the request.', 'NotAllowedError'));
            };
            """
        )
        console_errors: list[str] = []
        page.on("pageerror", lambda exc: console_errors.append(str(exc)))

        page.goto(f"{server.base_url}/security")
        page.wait_for_load_state("load")
        page.locator("#pf-add-passkey").click()
        status = page.locator("#pf-passkey-status")
        page.wait_for_function(
            "() => { var el = document.getElementById('pf-passkey-status');"
            " return !!el && el.textContent.indexOf('Could not add a passkey') === 0; }"
        )
        assert "NotAllowedError" in status.text_content() or "declined" in status.text_content()
        assert page.locator("#pf-add-passkey").is_enabled()
        assert console_errors == []

    def test_security_page_renders_for_a_signed_in_principal(self, page, context, org_server):
        server, sessions = org_server
        principal = Principal(id="bob", email="bob@example.com", display_name="Bob")
        _sign_in_org(context, server, sessions, principal=principal)
        page.goto(f"{server.base_url}/security")
        page.wait_for_load_state("load")
        assert page.get_by_text("No passkeys added yet.").is_visible()
