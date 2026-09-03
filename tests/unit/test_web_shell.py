"""web_shell.py -- the shared header/nav/live-indicator chrome wrapping
/approvals and /settings (docs/https-connector-refactor-plan.md §16.2.3/
§16.3)."""
from __future__ import annotations

from pathlib import Path

from privacyfence import web_shell
from privacyfence.approval_window_html import _STYLES_CSS


class TestWrap:
    def test_returns_one_well_formed_document(self):
        html = web_shell.wrap("<p>hello</p>", title="Test Page", active="approvals")
        assert html.startswith("<!DOCTYPE html>")
        assert "<title>Test Page</title>" in html
        assert "<p>hello</p>" in html

    def test_title_is_escaped(self):
        html = web_shell.wrap("<p>x</p>", title="<script>evil()</script>", active="approvals")
        assert "<script>evil()</script>" not in html
        assert "&lt;script&gt;evil()&lt;/script&gt;" in html

    def test_active_nav_item_is_marked(self):
        html = web_shell.wrap("", title="t", active="settings")
        assert 'class="pf-shell-nav-item active" href="/settings"' in html
        assert 'class="pf-shell-nav-item" href="/approvals"' in html

    def test_embeds_the_shared_tokens(self):
        html = web_shell.wrap("", title="t", active="approvals")
        assert "--color-accent:" in html
        assert "prefers-color-scheme: dark" in html

    def test_wires_the_state_stream_and_render_dispatch(self):
        html = web_shell.wrap("", title="t", active="approvals")
        assert "new EventSource('/api/state/stream')" in html
        assert "__pfRender" in html
        assert "__pfRenderApprovals" in html

    def test_body_html_is_not_escaped(self):
        # Callers own their own content's escaping (same convention as
        # web/routes_approvals.py's existing HTML-building routes) -- the
        # shell must not double-escape real markup handed to it.
        html = web_shell.wrap('<div id="mine">x</div>', title="t", active="approvals")
        assert '<div id="mine">x</div>' in html


class TestNotifications:
    """W8, docs/approval-list-ui-ux.md §4: tiers 0-1 only."""

    def test_registers_the_service_worker(self):
        html = web_shell.wrap("", title="t", active="approvals")
        assert "navigator.serviceWorker.register('/sw.js')" in html

    def test_title_badge_and_aria_live_announcer_present(self):
        html = web_shell.wrap("", title="t", active="approvals")
        assert "document.title = count > 0" in html
        assert 'id="pf-shell-announcer"' in html
        assert 'aria-live="polite"' in html

    def test_never_asks_for_permission_on_page_load(self):
        # §4.4: only window.__pfNotifPrompt, called by approval_list_html.py
        # after a decision -- never invoked unconditionally by this script.
        html = web_shell.wrap("", title="t", active="approvals")
        assert "Notification.requestPermission()" in html
        # The only unconditional call in this document is the *definition*
        # of __pfNotifPrompt, not an invocation of it.
        assert "__pfNotifPrompt();" not in html

    def test_notification_body_is_never_built_from_a_connector_or_tool_name(self):
        # §4.3's hard invariant, enforced by construction here: the only
        # string this script ever passes as a notification body is the
        # bare pending count -- never rows[i].connector/tool/summary/
        # tool_name, any of which can carry real gated content
        # (approvals.PendingApproval.summary -- see gate.py's call sites).
        html = web_shell.wrap("", title="t", active="approvals")
        start = html.index("function maybeNotify")
        end = html.index("function onApprovalsEvent")
        notify_fn_body = html[start:end]
        assert "showNotification" in notify_fn_body
        for forbidden in (".connector", ".tool_name", ".summary", "rows[", "row."):
            assert forbidden not in notify_fn_body, forbidden
        assert "var body = count === 1" in html

    def test_rate_limited_to_one_notification_per_five_seconds(self):
        html = web_shell.wrap("", title="t", active="approvals")
        assert "5000" in html

    def test_no_notification_while_the_tab_is_focused(self):
        html = web_shell.wrap("", title="t", active="approvals")
        assert "document.hasFocus()" in html


class TestTokensCssStaysInSyncWithTheApprovalCard:
    """resources/tokens.css is a manual export of approval_window/styles.css's
    own :root block (see that file's docstring for why it isn't a single
    shared @import source) -- this guards against the two silently drifting
    apart the next time either palette changes."""

    def test_every_token_value_in_tokens_css_appears_in_the_approval_stylesheet(self):
        tokens_css = (Path(web_shell.__file__).parent / "resources" / "tokens.css").read_text()
        for line in tokens_css.splitlines():
            line = line.strip()
            if not line.startswith("--") or ":" not in line:
                continue
            value = line.split(":", 1)[1].strip().rstrip(";")
            assert value in _STYLES_CSS, f"tokens.css value {value!r} ({line!r}) not found in approval styles.css"
