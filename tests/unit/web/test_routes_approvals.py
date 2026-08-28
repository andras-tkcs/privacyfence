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
        assert "No approvals are currently pending" in r.text

    def test_one_pending_card_links_to_it(self, client, web_ui):
        t, card, box = _pending_card(web_ui)
        r = client.get(f"/approvals?token={TOKEN}")
        assert f"/approvals/{card.id}" in r.text
        web_ui.resolve(card.id, "deny")
        t.join(timeout=2)

    def test_response_is_never_cached(self, client):
        r = client.get(f"/approvals?token={TOKEN}")
        assert r.headers.get("cache-control") == "no-store"


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
