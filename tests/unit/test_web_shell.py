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
