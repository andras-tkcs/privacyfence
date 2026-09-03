"""Tests for web/routes_approvals.py -- the approval list/card/decide
routes, exercised against an in-process ASGI test client (no real socket).
See docs/https-connector-refactor-plan.md §13: "routes tested against an
in-process ASGI/HTTP test client... Auth middleware, CSRF, Host/Origin
policy... each get explicit negative tests."
"""
from __future__ import annotations

import threading
import time

import pytest
from starlette.testclient import TestClient

from privacyfence.web.routes_approvals import _inject_shim, create_app
from privacyfence.web_approval_ui import WebApprovalUI

TOKEN = "test-token-0123456789"


@pytest.fixture
def web_ui():
    return WebApprovalUI()


@pytest.fixture
def client(web_ui):
    app = create_app(web_ui, token=TOKEN)
    return TestClient(app, base_url="http://localhost")


def _pending_card(web_ui, **kwargs):
    """Starts a blocking show_popup() call on a background thread and
    waits for its card to register -- returns (thread, card). daemon=True:
    if a test's own assertion fails before it resolves the card (or calls
    t.join()), this thread would otherwise block forever on the unresolved
    card's Event -- a non-daemon thread left running like that hangs the
    whole pytest process at interpreter exit, not just fails the one test."""
    box = {}

    def run():
        box["result"] = web_ui.show_popup("Send email", {"To": "a@b.com"}, "body text", **kwargs)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    deadline = time.monotonic() + 2
    while web_ui.current() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    card = web_ui.current()
    assert card is not None, "card never registered"
    return t, card, box


class TestInjectShim:
    """Regression coverage for a real bug found by actually clicking Allow
    in headless Chromium: a naive html.replace("<body>", ..., 1) lands the
    shim wherever "<body>" first appears in the raw string -- and
    styles.css's own CSS comments genuinely contain that literal substring
    (e.g. "the same-colored rail on <body>") inside the document's one
    <style> block, well before the real tag. The browser then parses the
    injected <script> as inert CSS text, not a script element: it silently
    never runs, so window.webkit stays undefined and clicking any button
    has no effect at all -- no console error, no failed request, nothing.
    _inject_shim must only ever match the real <body>, after </head>
    closes.
    """

    def test_a_body_like_string_inside_a_style_comment_before_head_close_is_not_matched(self):
        html = (
            "<html><head><style>/* mentions <body> in a comment */</style></head>"
            "<body>REAL CONTENT</body></html>"
        )
        shimmed = _inject_shim(html, "<script>SHIM</script>")
        assert shimmed == (
            "<html><head><style>/* mentions <body> in a comment */</style></head>"
            "<body><script>SHIM</script>REAL CONTENT</body></html>"
        )

    def test_lands_after_head_close_and_before_the_real_body_content(self):
        from privacyfence.card_builder import build_card_html

        html = build_card_html(title="t", preview={}, details_text="d", is_read=True, layout="narrow")
        shimmed = _inject_shim(html, "<script>SHIM_MARKER</script>")
        head_close = shimmed.index("</head>")
        style_close = shimmed.index("</style>")
        # Deliberately searched from head_close on, not shimmed.index("<body>")
        # from the start -- the real card template's own styles.css comments
        # contain the literal substring "<body>" earlier in the document
        # (inside the <style> block), which is exactly the bug this test
        # exists to catch; a naive search here would hide it, not prove it.
        body_open = shimmed.index("<body>", head_close)
        shim_pos = shimmed.index("SHIM_MARKER")
        assert style_close < head_close < body_open < shim_pos


class TestAuthentication:
    def test_unauthenticated_list_is_rejected(self, client):
        r = client.get("/approvals")
        assert r.status_code == 401

    def test_token_query_param_authenticates_and_sets_a_session_cookie(self, client):
        r = client.get(f"/approvals?token={TOKEN}")
        assert r.status_code == 200
        assert "pf_session" in r.headers.get("set-cookie", "")

    def test_wrong_token_is_rejected(self, client):
        r = client.get("/approvals?token=not-the-real-token")
        assert r.status_code == 401

    def test_session_cookie_alone_authenticates_a_later_request(self, client):
        client.cookies.set("pf_session", TOKEN)
        r = client.get("/approvals")
        assert r.status_code == 200


class TestListApprovals:
    def test_nothing_pending(self, client):
        r = client.get(f"/approvals?token={TOKEN}")
        assert "Nothing is waiting" in r.text

    def test_one_pending_card_links_to_it(self, client, web_ui):
        t, card, box = _pending_card(web_ui)
        r = client.get(f"/approvals?token={TOKEN}")
        assert f"/approvals/{card.id}" in r.text
        web_ui.resolve(card.id, "deny")
        t.join(timeout=2)

    def test_response_is_never_cached(self, client):
        r = client.get(f"/approvals?token={TOKEN}")
        assert r.headers.get("cache-control") == "no-store"

    def test_wrapped_in_the_shared_shell(self, client):
        r = client.get(f"/approvals?token={TOKEN}")
        assert "pf-shell-nav" in r.text
        assert 'class="pf-shell-nav-item active" href="/approvals"' in r.text

    def test_notifications_enabled_config_reaches_the_page(self, web_ui):
        from privacyfence.web.routes_approvals import create_app

        app = create_app(web_ui, token=TOKEN, notifications_enabled=False)
        c = TestClient(app, base_url="http://localhost")
        r = c.get(f"/approvals?token={TOKEN}")
        assert "NOTIFICATIONS_ENABLED = false" in r.text

    def test_pending_card_row_has_a_deny_button_and_review_link(self, client, web_ui):
        t, card, box = _pending_card(web_ui)
        r = client.get(f"/approvals?token={TOKEN}")
        assert f'data-deny="{card.id}"' in r.text
        assert f'href="/approvals/{card.id}"' in r.text
        assert ">Allow<" not in r.text
        web_ui.resolve(card.id, "deny")
        t.join(timeout=2)

    def test_two_pending_cards_both_link(self, client, web_ui):
        # P3: several approvals can be pending at once (§6's retirement of
        # _popup_lock's "one dialog at a time") -- the list must show all of
        # them, not just the most recent.
        t1, card1, box1 = _pending_card(web_ui)
        t2, card2, box2 = _pending_card(web_ui)
        r = client.get(f"/approvals?token={TOKEN}")
        assert f"/approvals/{card1.id}" in r.text
        assert f"/approvals/{card2.id}" in r.text
        web_ui.resolve(card1.id, "deny")
        web_ui.resolve(card2.id, "deny")
        t1.join(timeout=2)
        t2.join(timeout=2)


class TestServiceWorker:
    """W8 (tier 0/1 notifications, docs/approval-list-ui-ux.md §4): served
    at the origin root with no auth required, so registration never fails
    on a session that hasn't authenticated yet."""

    def test_served_with_no_auth_required(self, client):
        r = client.get("/sw.js")
        assert r.status_code == 200

    def test_correct_content_type_and_scope_header(self, client):
        r = client.get("/sw.js")
        assert "javascript" in r.headers.get("content-type", "")
        assert r.headers.get("service-worker-allowed") == "/"

    def test_no_push_handler(self, client):
        # This phase is tier 0/1 only -- no `push` event handler (that's
        # tier 2, VAPID, org mode/P7+).
        r = client.get("/sw.js")
        assert "addEventListener(\"push\"" not in r.text
        assert "addEventListener('push'" not in r.text


class TestApprovalsStream:
    """GET /api/approvals/stream -- §7.1's SSE counterpart to the list page,
    so it updates live without polling. Only the auth boundary is exercised
    here: the handler's own generator loops until the client disconnects,
    which starlette.testclient.TestClient's synchronous, fully-buffering
    ``stream()`` (it drains an ASGI response before yielding control back,
    even under ``with client.stream(...)``) can't drive without hanging --
    a real streaming HTTP client is what this endpoint actually needs to be
    exercised end-to-end, which is what P0/P1's own manual Chromium checks
    (§11 of docs/https-connector-refactor-plan.md) already cover the
    pattern for, not this test client."""

    def test_requires_auth(self, client):
        r = client.get("/api/approvals/stream")
        assert r.status_code == 401


class TestShowApproval:
    def test_unknown_id_says_no_longer_pending_not_404(self, client):
        r = client.get(f"/approvals/does-not-exist?token={TOKEN}")
        assert r.status_code == 200
        assert "no longer pending" in r.text

    def test_pending_card_renders_with_the_bridge_shim_injected(self, client, web_ui):
        t, card, box = _pending_card(web_ui, connector="gmail")
        r = client.get(f"/approvals/{card.id}?token={TOKEN}")
        assert "window.webkit.messageHandlers.pf" in r.text
        assert f"/api/approvals/{card.id}/decide" in r.text
        assert "Send email" in r.text
        web_ui.resolve(card.id, "deny")
        t.join(timeout=2)

    def test_unauthenticated_show_is_rejected(self, client, web_ui):
        t, card, box = _pending_card(web_ui)
        r = client.get(f"/approvals/{card.id}")
        assert r.status_code == 401
        web_ui.resolve(card.id, "deny")
        t.join(timeout=2)

    def test_no_longer_pending_page_links_back_to_the_list(self, client):
        r = client.get(f"/approvals/does-not-exist?token={TOKEN}")
        assert 'href="/approvals"' in r.text

    def test_shim_navigates_back_to_the_list_on_success_not_innerhtml(self, client, web_ui):
        # §3 of docs/approval-list-ui-ux.md: a decision navigates back to
        # /approvals via location.replace (so the back button can't walk
        # into a dead card) with a toast stashed in sessionStorage, instead
        # of rewriting the document body in place.
        t, card, box = _pending_card(web_ui)
        r = client.get(f"/approvals/{card.id}?token={TOKEN}")
        assert "location.replace('/approvals')" in r.text
        assert "sessionStorage.setItem('pf_toast'" in r.text
        assert "document.body.innerHTML = r.ok" not in r.text
        web_ui.resolve(card.id, "deny")
        t.join(timeout=2)

    def test_shim_handles_the_409_already_decided_case(self, client, web_ui):
        t, card, box = _pending_card(web_ui)
        r = client.get(f"/approvals/{card.id}?token={TOKEN}")
        assert "r.status === 409" in r.text
        assert "Already decided elsewhere" in r.text
        web_ui.resolve(card.id, "deny")
        t.join(timeout=2)


class TestDecide:
    def test_happy_path_releases_the_blocked_gate_call(self, client, web_ui):
        t, card, box = _pending_card(web_ui, accept_all_choices=[("always_allow", "")])
        client.cookies.set("pf_session", TOKEN)
        r = client.post(
            f"/api/approvals/{card.id}/decide",
            json={"action": "resolve", "result": "accept_all", "choice": 0, "csrf": TOKEN},
        )
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}
        t.join(timeout=2)
        assert box["result"] == ("accept_all", 0)

    def test_missing_csrf_is_rejected(self, client, web_ui):
        t, card, box = _pending_card(web_ui)
        r = client.post(f"/api/approvals/{card.id}/decide", json={"action": "resolve", "result": "deny"})
        assert r.status_code == 401
        web_ui.resolve(card.id, "deny")
        t.join(timeout=2)

    def test_csrf_not_matching_the_session_cookie_is_rejected(self, client, web_ui):
        t, card, box = _pending_card(web_ui)
        client.cookies.set("pf_session", TOKEN)
        r = client.post(
            f"/api/approvals/{card.id}/decide",
            json={"action": "resolve", "result": "deny", "csrf": "wrong-value"},
        )
        assert r.status_code == 401
        web_ui.resolve(card.id, "deny")
        t.join(timeout=2)

    def test_second_decide_for_the_same_id_is_rejected_not_silently_applied(self, client, web_ui):
        # Idempotent: the first accepted decision wins (§7.1). Also what
        # protects against a stale tab and a genuine double-submit.
        t, card, box = _pending_card(web_ui)
        client.cookies.set("pf_session", TOKEN)
        first = client.post(
            f"/api/approvals/{card.id}/decide",
            json={"action": "resolve", "result": "accept", "csrf": TOKEN},
        )
        assert first.status_code == 200
        second = client.post(
            f"/api/approvals/{card.id}/decide",
            json={"action": "resolve", "result": "deny", "csrf": TOKEN},
        )
        assert second.status_code == 409
        t.join(timeout=2)
        assert box["result"] == ("accept", None)  # the second POST never overturned the first

    def test_cross_origin_request_is_rejected(self, client, web_ui):
        t, card, box = _pending_card(web_ui)
        client.cookies.set("pf_session", TOKEN)
        r = client.post(
            f"/api/approvals/{card.id}/decide",
            json={"action": "resolve", "result": "deny", "csrf": TOKEN},
            headers={"Origin": "https://evil.example.com"},
        )
        assert r.status_code == 403
        web_ui.resolve(card.id, "deny")
        t.join(timeout=2)

    def test_unknown_id_is_rejected_without_touching_the_real_pending_card(self, client, web_ui):
        t, card, box = _pending_card(web_ui)
        client.cookies.set("pf_session", TOKEN)
        r = client.post(
            "/api/approvals/not-the-real-id/decide",
            json={"action": "resolve", "result": "deny", "csrf": TOKEN},
        )
        assert r.status_code == 409
        web_ui.resolve(card.id, "accept")
        t.join(timeout=2)
        assert box["result"] == ("accept", None)

    def test_malformed_json_body_is_rejected(self, client, web_ui):
        t, card, box = _pending_card(web_ui)
        client.cookies.set("pf_session", TOKEN)
        r = client.post(
            f"/api/approvals/{card.id}/decide",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 400
        web_ui.resolve(card.id, "deny")
        t.join(timeout=2)
