"""Shared page chrome for the web surfaces (docs/https-connector-refactor-
plan.md §16.3: "one header, one nav, one palette, one session, links both
ways") -- one pure function, ``wrap()``, that both ``/approvals`` and
``/settings`` wrap themselves in at the route layer (web/routes_approvals.py,
web/routes_settings.py), so the two pages read as one application instead of
"two applications bolted together" (§16.2.3's own wording for the problem
this fixes).

Deliberately **not** used by:

- the native settings window (settings_window.py's WKWebView) or the native
  approval window (approval_window.py's) -- both load their own document's
  markup directly via ``loadHTMLString_baseURL_``, with no HTTP request and
  no other page to link to. Wrapping their shared documents
  (settings_window_html.build_html/approval_window_html.build_card_stack_
  html) in this shell would change what those two already-tested,
  geometry-tuned documents render, for a native host that has no use for a
  cross-page nav bar at all.
- an individual approval card's own page (``GET /approvals/{id}``) -- that
  page *is* the decision screen, full-window, same as the native dialog it
  replaces; the list it returns to (§3 of docs/approval-list-ui-ux.md) is
  where the shell belongs, not the card itself.

Owns the one thing every shell-wrapped page needs and none of them should
reimplement: the ``/api/state/stream`` SSE connection (web/state_stream.py)
that drives the live indicator and dispatches each event to whichever of
``window.__pfRender``/``window.__pfRenderApprovals`` the current page
happens to define -- settings_window_html.py's own bridge JS already
defines the former (unchanged, since it also serves the native-window
push path); approval_list_html.py defines the latter. Centralizing the
connection here, rather than duplicating an EventSource per page, is what
makes "one live indicator" true instead of aspirational.
"""
from __future__ import annotations

from html import escape as _html_escape
from pathlib import Path

_TOKENS_CSS = (Path(__file__).parent / "resources" / "tokens.css").read_text(encoding="utf-8")

_SHELL_CSS = """
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--color-bg); color: var(--color-text);
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", Helvetica, Arial, sans-serif;
  font-size: 14px; min-height: 100vh; display: flex; flex-direction: column;
}
.pf-shell-header {
  display: flex; align-items: center; gap: var(--space-4); flex-wrap: wrap;
  padding: 10px 20px; background: var(--color-surface);
  border-bottom: 1px solid var(--color-divider); flex-shrink: 0;
}
.pf-shell-brand { font-weight: 700; font-size: 14px; letter-spacing: -0.01em; }
.pf-shell-nav { display: flex; gap: 4px; flex: 1; }
.pf-shell-nav-item {
  padding: 5px 10px; border-radius: var(--radius-md); font-size: 13px;
  color: var(--color-text); text-decoration: none; opacity: .7;
}
.pf-shell-nav-item:hover { opacity: 1; background: var(--color-bg); }
.pf-shell-nav-item.active { opacity: 1; font-weight: 600; background: var(--color-bg); }
.pf-shell-live {
  display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--color-neutral-600);
  white-space: nowrap;
}
.pf-shell-live-dot {
  width: 7px; height: 7px; border-radius: 50%; background: var(--color-neutral-400); flex-shrink: 0;
}
.pf-shell-live-dot.live { background: #2fa84f; }
.pf-shell-live-dot.reconnecting { background: #d9a520; }
.pf-shell-live-dot.down { background: var(--color-danger); }
.pf-shell-main { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.pf-shell-toast {
  position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%);
  background: var(--color-neutral-900); color: var(--color-neutral-100);
  padding: 10px 16px; border-radius: var(--radius-lg); font-size: 13px;
  max-width: min(480px, calc(100vw - 32px)); box-shadow: 0 8px 24px rgba(0,0,0,.25);
  opacity: 0; pointer-events: none; transition: opacity .15s ease;
  z-index: 1000;
}
.pf-shell-toast.shown { opacity: 1; }
@media (max-width: 480px) {
  .pf-shell-header { padding: 8px 12px; gap: 10px; }
  .pf-shell-brand { display: none; }
}
"""

# EventSource is same-origin by construction (a relative URL), so it
# automatically carries the pf_session cookie web/routes_approvals.py's/
# web/routes_settings.py's own auth already set on the page load that
# reaches this script -- no separate token plumbing needed here. Dispatch,
# not rendering: this script never touches the DOM for either event's
# *content*, only the live indicator -- window.__pfRender/
# window.__pfRenderApprovals (whichever the current page defines) own their
# own page's markup entirely, same separation settings_window_html.py's own
# bridge already has between "receive a message" and "render it".
_STREAM_JS = """
(function () {
  var dot = document.getElementById('pf-shell-live-dot');
  var label = document.getElementById('pf-shell-live-label');
  function setState(state, text) {
    if (dot) { dot.className = 'pf-shell-live-dot ' + state; }
    if (label) { label.textContent = text; }
  }
  setState('reconnecting', 'connecting…');
  if (typeof EventSource === 'undefined') {
    setState('down', "can't reach PrivacyFence");
    return;
  }
  var es = new EventSource('/api/state/stream');
  es.onopen = function () { setState('live', 'live'); };
  es.onerror = function () { setState('reconnecting', 'reconnecting…'); };
  es.addEventListener('settings', function (e) {
    if (window.__pfRender) { window.__pfRender(JSON.parse(e.data)); }
  });
  es.addEventListener('approvals', function (e) {
    if (window.__pfRenderApprovals) { window.__pfRenderApprovals(JSON.parse(e.data)); }
  });
  window.__pfStateStream = es;
})();
"""

_NAV_ITEMS = (("approvals", "Approvals", "/approvals"), ("settings", "Settings", "/settings"))


def _nav_html(active: str) -> str:
    items = []
    for key, label, href in _NAV_ITEMS:
        cls = "pf-shell-nav-item active" if key == active else "pf-shell-nav-item"
        items.append(f'<a class="{cls}" href="{href}">{_html_escape(label)}</a>')
    return "".join(items)


def wrap(body_html: str, *, title: str, active: str) -> str:
    """Full ``<!DOCTYPE html>`` document: tokens.css + the shell's own CSS,
    the header (brand, nav between Approvals/Settings, live indicator), and
    ``body_html`` dropped into ``<main>`` unescaped -- callers own their own
    content's escaping, same convention web/routes_approvals.py's existing
    HTML-building routes already follow throughout this codebase.

    ``active`` is one of ``"approvals"``/``"settings"`` -- which nav item
    renders as current.
    """
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{_html_escape(title)}</title>
<style>{_TOKENS_CSS}{_SHELL_CSS}</style>
</head>
<body>
<header class="pf-shell-header">
<div class="pf-shell-brand">PrivacyFence</div>
<nav class="pf-shell-nav">{_nav_html(active)}</nav>
<div class="pf-shell-live" role="status" aria-live="polite">
<span class="pf-shell-live-dot" id="pf-shell-live-dot"></span>
<span id="pf-shell-live-label">connecting…</span>
</div>
</header>
<main class="pf-shell-main">{body_html}</main>
<div class="pf-shell-toast" id="pf-shell-toast" role="status"></div>
<script>{_STREAM_JS}</script>
</body>
</html>
"""
