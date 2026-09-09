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

Two of the checks the plan's own TST-06 row lists are deliberately *not*
asserted as passing here, each for a different reason -- see
``TestSecurityHeadersCsp``/``TestPdfPreview`` below:

- "no-inline-script CSP check" is written but skipped: it needs SEC-08's
  nonce migration (Phase 3.1, still unimplemented as of this plan item),
  not just a test. ``script-src``/``style-src`` are ``'unsafe-inline'`` by
  design until then (web/server.py's own ``_CSP`` docstring).
- "PDF preview actually renders" is written as a real assertion, and is
  ``xfail`` against today's ``_CSP``: it has no ``object-src`` exception,
  so it inherits ``default-src 'none'`` and blocks the card's own
  ``<embed>`` -- exactly the "currently likely broken -- no test catches
  this" the plan's Phase 3 table flags for SEC-08/3.1 to fix. Recorded
  here, not silently fixed here -- see this plan's own "Notes on scope not
  in the source review": widening this phase's scope to also patch the CSP
  is exactly what that note says not to do.
"""
from __future__ import annotations

import http.server
import socket
import threading
import time
import uuid

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
from privacyfence.web.server import OrgAuth, WebServer  # noqa: E402
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
    yield pg
    pg.close()


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
# PDF preview (part of TST-06's own list) -- see module docstring for why
# this is xfail, not a plain pass/fail assertion.
# --------------------------------------------------------------------- #


class TestPdfPreview:
    @pytest.mark.xfail(
        reason=(
            "SEC-08/Phase 3.1 (docs/security-remediation-plan.md) hasn't landed: web/server.py's _CSP has no "
            "object-src exception, so it inherits default-src 'none' and blocks the card's own <embed "
            "type=\"application/pdf\">. This is the 'currently likely broken' PDF-preview bug the plan's Phase 3 "
            "table already names 3.1 to fix -- flagged here, not fixed here (see this module's own docstring)."
        ),
        strict=False,
    )
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
# CSP: no-inline-script (SEC-08/Phase 3.1 dependency)
# --------------------------------------------------------------------- #


class TestSecurityHeadersCsp:
    @pytest.mark.skip(
        reason=(
            "Not applicable until SEC-08/Phase 3.1 lands: web/server.py's own _CSP docstring documents "
            "script-src/style-src 'unsafe-inline' as a deliberate, current choice (approval_window_html.py's/"
            "approval_list_html.py's inline <script> tags), not a bug -- there is no nonce-based CSP yet for this "
            "check to assert against. Un-skip once 3.1 migrates both directives off 'unsafe-inline'."
        ),
    )
    def test_inline_scripts_would_be_blocked_under_a_nonce_based_csp(self, page, local_server):
        """Deliberately unimplemented -- see the class-level skip reason
        above. Once SEC-08 lands (a per-response nonce on script-src/
        style-src, no bare 'unsafe-inline'), this should assert the
        Content-Security-Policy response header carries no 'unsafe-inline'
        on either directive, and that an inline <script> injected via
        page.evaluate never executes."""
        raise NotImplementedError("un-skip and implement once SEC-08/Phase 3.1 lands")


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
