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

import json
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
.pf-shell-notif-prompt {
  bottom: 72px; display: flex; align-items: center; gap: 10px; pointer-events: auto;
}
.pf-shell-notif-enable {
  background: var(--color-accent); color: #fff; border: none; border-radius: var(--radius-md);
  font-size: 12.5px; font-weight: 600; padding: 5px 10px; cursor: pointer; flex-shrink: 0;
}
.pf-sr-only {
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden;
  clip: rect(0,0,0,0); white-space: nowrap; border: 0;
}
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
#
# Notifications (docs/approval-list-ui-ux.md §4), tiers 0-1 only -- no
# push, no VAPID, nothing leaving the machine (tier 2 is org mode/P7+):
#   - tier 0: the document title gains a "(N) " badge and a visually
#     hidden aria-live region announces the count, whenever the approvals
#     event's row count changes -- works with no permission at all.
#   - tier 1: registration.showNotification() via resources/sw.js, fired
#     only when the tab is not focused, rate-limited to one per 5s (§4.2),
#     grouped into a single "N approvals pending" notification rather than
#     one per row. **Content invariant (§4.3), enforced by construction,
#     via notificationBody()'s own per-level field allowlist (P5)**: at
#     `minimal`, or whenever more than one approval fired at once (there is
#     no "several different named things" copy -- §4.2's own grouping
#     case), the body is always the bare count -- "N approval(s) pending".
#     `standard` adds exactly two fields off the single pending row's own
#     summary dict -- connector and gate_kind (read/write direction), both
#     safe by construction: gate_kind names a category, never gated
#     content, and connector is the same bare connector key
#     approval_list_html.py's own row kicker already capitalizes and shows
#     unescaped, nothing this function invents access to. `detailed` adds
#     one more field: the row's own `summary` -- the one field that
#     genuinely can carry gated content (an event title, a contact name, a
#     document title -- see approvals.PendingApproval's own docstring on
#     it), which is exactly why it's the one gated behind the highest
#     level rather than shown at `standard` or below.
#   - the permission pre-prompt only ever fires from window.__pfNotifPrompt,
#     called by approval_list_html.py's own script right after a decision
#     is made (never on page load -- §4.4: "never on page load, a cold
#     Notification.requestPermission() is what browsers now penalize").
_STREAM_JS = """
(function () {
  // web.notifications.enabled (settings.yaml.example) -- config wiring for
  // tiers 0-1 together, per that config block's own comment. false turns
  // off the title badge, the aria-live announcement, service worker
  // registration, and the permission pre-prompt alike; the live-connection
  // indicator above is unaffected (it isn't a notification).
  var NOTIFICATIONS_ENABLED = %(notifications_enabled)s;
  // web.notifications.detail -- "minimal" | "standard" | "detailed" (P5,
  // docs/approval-list-ui-ux.md §4.3). See notificationBody() below for
  // what each level is allowed to read off a pending-approval row.
  var NOTIFICATIONS_DETAIL = %(notifications_detail)s;
  // Exposed globally so the settings page's own notifications card
  // (settings_window_html.py's renderNotificationsCard) can read the same
  // config flags -- that module's JS is shared with the native settings
  // window, which never loads this script at all, so it treats a missing
  // flag as "on" (feature-detecting Notification support instead) rather
  // than assuming these variables exist; __pfNotificationsDetail missing
  // is that same "no such config surface here" case, so the card omits
  // the detail-level line entirely on native rather than guessing.
  window.__pfNotificationsEnabled = NOTIFICATIONS_ENABLED;
  window.__pfNotificationsDetail = NOTIFICATIONS_DETAIL;

  var dot = document.getElementById('pf-shell-live-dot');
  var label = document.getElementById('pf-shell-live-label');
  var announcer = document.getElementById('pf-shell-announcer');
  var baseTitle = document.title;
  var lastCount = null;
  var lastNotifyAt = 0;

  function setState(state, text) {
    if (dot) { dot.className = 'pf-shell-live-dot ' + state; }
    if (label) { label.textContent = text; }
  }

  function updateBadge(count) {
    if (!NOTIFICATIONS_ENABLED) { return; }
    document.title = count > 0 ? '(' + count + ') ' + baseTitle : baseTitle;
    if (announcer) {
      announcer.textContent = count > 0
        ? count + ' approval' + (count === 1 ? '' : 's') + ' pending'
        : 'No approvals pending';
    }
  }

  function countBody(count) {
    return count === 1 ? '1 approval pending' : count + ' approvals pending';
  }

  // The notification-detail allowlist (P5, docs/approval-list-ui-ux.md
  // §4.3) -- see this file's own comment above _STREAM_JS for what each
  // level is allowed to read off `row`. Only ever called with exactly the
  // one row a count-increase-to-1 identifies unambiguously (see
  // maybeNotify below); anything else falls back to the plain count, which
  // is always safe at any level.
  function notificationBody(count, rows) {
    if (NOTIFICATIONS_DETAIL === 'minimal' || count !== 1 || !rows || rows.length !== 1) {
      return countBody(count);
    }
    var row = rows[0];
    var connector = row.connector
      ? row.connector.charAt(0).toUpperCase() + row.connector.slice(1)
      : '';
    var direction = row.gate_kind === 'review' ? 'read' : row.gate_kind === 'popup' ? 'write' : '';
    var head = [connector, row.tool_name || ''].filter(function (s) { return !!s; }).join(' — ');
    if (direction) { head += ' · ' + direction; }
    if (!head) { return countBody(count); }
    if (NOTIFICATIONS_DETAIL === 'detailed' && row.summary) { head += '\\n' + row.summary; }
    return head;
  }

  function maybeNotify(count, rows) {
    if (!NOTIFICATIONS_ENABLED) { return; }
    if (count === 0 || lastCount === null || count <= lastCount) { return; }
    if (typeof document.hasFocus === 'function' && document.hasFocus()) { return; }
    var now = Date.now();
    if (now - lastNotifyAt < 5000) { return; }
    lastNotifyAt = now;
    if (!('Notification' in window) || Notification.permission !== 'granted') { return; }
    if (!('serviceWorker' in navigator)) { return; }
    var body = notificationBody(count, rows);
    navigator.serviceWorker.ready.then(function (reg) {
      reg.showNotification('PrivacyFence', { body: body, tag: 'pf-approvals', renotify: false });
    }).catch(function () {});
  }

  function onApprovalsEvent(rows) {
    var count = (rows || []).length;
    updateBadge(count);
    maybeNotify(count, rows);
    lastCount = count;
  }

  if (NOTIFICATIONS_ENABLED && 'serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(function () {});
  }

  window.__pfNotifPrompt = function () {
    if (!NOTIFICATIONS_ENABLED) { return; }
    if (!('Notification' in window) || Notification.permission !== 'default') { return; }
    var already;
    try { already = localStorage.getItem('pf_notif_prompted'); } catch (e) { already = null; }
    if (already) { return; }
    try { localStorage.setItem('pf_notif_prompted', '1'); } catch (e) {}
    var bar = document.createElement('div');
    bar.className = 'pf-shell-toast pf-shell-notif-prompt shown';
    bar.setAttribute('role', 'status');
    var text = document.createElement('span');
    text.textContent = 'Want PrivacyFence to notify you when Claude needs approval?';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'pf-shell-notif-enable';
    btn.textContent = 'Enable';
    btn.addEventListener('click', function () {
      Notification.requestPermission();
      bar.remove();
    });
    bar.appendChild(text);
    bar.appendChild(btn);
    document.body.appendChild(bar);
    setTimeout(function () { if (bar.parentNode) { bar.remove(); } }, 10000);
  };

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
    var rows = JSON.parse(e.data);
    if (window.__pfRenderApprovals) { window.__pfRenderApprovals(rows); }
    onApprovalsEvent(rows);
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


def wrap(
    body_html: str, *, title: str, active: str,
    notifications_enabled: bool = True, notifications_detail: str = "minimal",
) -> str:
    """Full ``<!DOCTYPE html>`` document: tokens.css + the shell's own CSS,
    the header (brand, nav between Approvals/Settings, live indicator), and
    ``body_html`` dropped into ``<main>`` unescaped -- callers own their own
    content's escaping, same convention web/routes_approvals.py's existing
    HTML-building routes already follow throughout this codebase.

    ``active`` is one of ``"approvals"``/``"settings"`` -- which nav item
    renders as current. ``notifications_enabled`` is settings.yaml.
    example's ``web.notifications.enabled`` (default true) -- false turns
    off tiers 0 and 1 together (title badge, aria-live announcement,
    service worker registration, the permission pre-prompt), per that
    config key's own comment. ``notifications_detail`` is that same config
    block's ``detail`` (``"minimal"``/``"standard"``/``"detailed"``, P5 --
    docs/approval-list-ui-ux.md §4.3) -- see _STREAM_JS's own
    notificationBody() for exactly what each level is allowed to say.
    """
    stream_js = _STREAM_JS % {
        "notifications_enabled": "true" if notifications_enabled else "false",
        "notifications_detail": json.dumps(notifications_detail),
    }
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
<div class="pf-sr-only" id="pf-shell-announcer" aria-live="polite"></div>
<script>{stream_js}</script>
</body>
</html>
"""
