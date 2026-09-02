"""Pure-function HTML for the ``/approvals`` list page
(docs/approval-list-ui-ux.md §2, the P1-compatible slice its own §6 says
can land ahead of P3's full design -- the row shape, the empty state, and
the central asymmetry of §2.2: **Deny is on the row; Allow is never on the
row.** Denying without reading the card cannot leak anything; approving
from a one-line summary is exactly the habituation failure the card exists
to prevent, so there is no "Allow" button here at all -- only "Review",
which opens the real card.

``build_list_html(rows)`` is the first paint (web/routes_approvals.py, given
``approvals.PendingApproval`` objects, each with a real connector icon --
see ``row_from_approval`` below); ``window.__pfRenderApprovals(state)`` is
the live re-render web_shell.py's SSE dispatch calls with
web/state_stream.py's own "approvals" event payload
(``PendingApproval.to_summary_dict()``, which carries no icon -- building
one needs approval_icons.py, a filesystem read this module deliberately
doesn't do on every SSE tick). A live-updated row therefore renders with a
plain connector-initial badge instead of the real icon; a decided row
leaves the list within one poll interval regardless (web/state_stream.py's
``_APPROVALS_POLL_SECONDS``), so the visual gap is real but short-lived --
a documented simplification of this phase's own P1-compatible scope, not
an oversight.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from html import escape as _html_escape
from typing import Any

from . import approval_icons

_EMPTY_STATE = (
    '<div class="pf-approvals-empty">'
    '<div class="pf-approvals-empty-title">Nothing is waiting.</div>'
    '<div class="pf-approvals-empty-sub">PrivacyFence is watching.</div>'
    "</div>"
)

_CSS = """
.pf-approvals-page { max-width: 720px; margin: 0 auto; padding: 24px 20px 60px; width: 100%; }
.pf-approvals-heading { font-size: 13px; color: var(--color-neutral-600); margin-bottom: 14px; }
.pf-approvals-empty {
  text-align: center; padding: 80px 20px; color: var(--color-neutral-600);
}
.pf-approvals-empty-title { font-size: 16px; font-weight: 600; color: var(--color-text); margin-bottom: 4px; }
.pf-approvals-empty-sub { font-size: 13px; }
.pf-approval-row {
  display: flex; align-items: center; gap: 14px; padding: 14px 16px;
  background: var(--color-surface); border-radius: var(--radius-lg); margin-bottom: 10px;
}
.pf-approval-icon {
  width: 28px; height: 28px; border-radius: var(--radius-md); flex-shrink: 0; object-fit: contain;
  background: var(--color-neutral-200);
}
.pf-approval-icon-fallback {
  display: flex; align-items: center; justify-content: center;
  font-size: 12px; font-weight: 700; color: var(--color-neutral-600);
}
.pf-approval-main { flex: 1; min-width: 0; }
.pf-approval-title {
  font-size: 14px; font-weight: 600; color: var(--color-text);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.pf-approval-kicker { font-size: 12px; color: var(--color-neutral-600); margin-top: 2px; }
.pf-approval-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.pf-btn-deny, .pf-btn-review {
  font-size: 12.5px; font-weight: 600; padding: 7px 12px; border-radius: var(--radius-md);
  border: none; cursor: pointer; text-decoration: none; white-space: nowrap;
}
.pf-btn-deny { background: transparent; color: var(--color-danger); border: 1px solid var(--color-divider); }
.pf-btn-review { background: var(--color-accent); color: #fff; }
"""

# Runtime dispatch: sessionStorage's pending toast (left by the card page's
# own shim right before it navigates back here -- see
# web/routes_approvals.py's _bridge_shim), the empty-state/row re-render on
# every "approvals" SSE event, and the row-level Deny button (a direct POST
# to the decide endpoint, no navigation to the card at all -- §2.2's own
# point: denying needs no context).
_JS = """
(function () {
  function relAge(iso) {
    if (!iso) return '';
    var then = new Date(iso).getTime();
    if (isNaN(then)) return '';
    var s = Math.max(0, Math.floor((Date.now() - then) / 1000));
    if (s < 60) return 'just now';
    var m = Math.floor(s / 60);
    if (m < 60) return m + 'm ago';
    var h = Math.floor(m / 60);
    if (h < 24) return h + 'h ago';
    return Math.floor(h / 24) + 'd ago';
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function rowHtml(row) {
    var title = esc(row.tool_name || row.summary || (row.kind === 'card' ? 'Approval' : 'Confirmation'));
    var kicker = [row.connector ? row.connector.charAt(0).toUpperCase() + row.connector.slice(1) : '',
      row.tool || '', relAge(row.created_at)].filter(Boolean).join(' · ');
    var initial = (row.connector || '?').charAt(0).toUpperCase();
    return '<div class="pf-approval-row" data-approval-id="' + esc(row.id) + '">' +
      '<div class="pf-approval-icon pf-approval-icon-fallback">' + esc(initial) + '</div>' +
      '<div class="pf-approval-main"><div class="pf-approval-title">' + title + '</div>' +
      '<div class="pf-approval-kicker">' + esc(kicker) + '</div></div>' +
      '<div class="pf-approval-actions">' +
      '<button type="button" class="pf-btn-deny" data-deny="' + esc(row.id) + '">Deny</button>' +
      '<a class="pf-btn-review" href="/approvals/' + esc(row.id) + '">Review →</a></div></div>';
  }

  function render(rows) {
    var container = document.getElementById('pf-approvals-list');
    if (!container) return;
    if (!rows || !rows.length) {
      container.innerHTML = %(empty)s;
      return;
    }
    container.innerHTML = rows.map(rowHtml).join('');
  }
  window.__pfRenderApprovals = render;

  function denyRow(id) {
    fetch('/api/approvals/' + encodeURIComponent(id) + '/decide', {
      method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({result: 'deny', csrf: %(csrf)s}),
    }).then(function () {
      var row = document.querySelector('[data-approval-id="' + id + '"]');
      if (row) { row.remove(); }
    });
  }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-deny]');
    if (btn) { denyRow(btn.getAttribute('data-deny')); }
  });

  // The card page's own return-to-list flow (web/routes_approvals.py's
  // _bridge_shim) stashes a toast message here right before navigating
  // back -- shown once, then cleared, so a page refresh never re-shows it.
  // Deferred to DOMContentLoaded: #pf-shell-toast and window.__pfNotifPrompt
  // are both defined by web_shell.py's own markup/script, which come
  // *after* this <script> in document order (this module's body_html is
  // wrapped inside <main>, ahead of the shell's own footer) -- running
  // this inline, synchronously, would find neither yet.
  document.addEventListener('DOMContentLoaded', function () {
    try {
      var raw = sessionStorage.getItem('pf_toast');
      if (raw) {
        sessionStorage.removeItem('pf_toast');
        var toast = JSON.parse(raw);
        var el = document.getElementById('pf-shell-toast');
        if (el && toast && toast.msg) {
          el.textContent = toast.msg;
          el.classList.add('shown');
          setTimeout(function () { el.classList.remove('shown'); }, 4000);
        }
        // §4.4: offer the notification permission pre-prompt right after
        // the first successful decision, when the value is concrete --
        // never on page load. window.__pfNotifPrompt (web_shell.py) itself
        // no-ops past the first time (localStorage) and past a non-default
        // permission state.
        if (window.__pfNotifPrompt) { window.__pfNotifPrompt(); }
      }
    } catch (e) { /* sessionStorage unavailable -- toast just doesn't show */ }
  });

  // §3 point 4: focus moves to the next pending row's Review control, not
  // into a re-opened card -- never auto-advance into a decision.
  var firstReview = document.querySelector('.pf-btn-review');
  if (firstReview) { firstReview.focus({preventScroll: true}); }
})();
"""


def row_from_approval(card: Any) -> dict[str, Any]:
    """``PendingApproval`` -> the same summary shape
    ``PendingApproval.to_summary_dict()`` already produces (approvals.py) --
    used for the server-rendered first paint here rather than calling that
    method directly, only so this module stays the one place that decides
    what a row needs to render (kept in sync with to_summary_dict() by the
    field names below, not by importing it, since the two shapes need to
    stay decoupled: an SSE payload field this module doesn't use yet
    shouldn't have to be added here too)."""
    return {
        "id": card.id,
        "kind": card.kind,
        "connector": card.connector,
        "tool": card.tool,
        "tool_name": card.tool_name,
        "summary": card.summary,
        "created_at": _iso(card.created_at),
    }


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _row_html(row: dict[str, Any]) -> str:
    label = "Confirmation" if row.get("kind") != "card" else "Approval"
    title = row.get("tool_name") or row.get("summary") or label
    connector = (row.get("connector") or "").capitalize()
    kicker = " · ".join(p for p in (connector, row.get("tool") or "", _relative_age(row.get("created_at", ""))) if p)
    rid = row["id"]
    icon_uri = approval_icons.icon_data_uri(approval_icons.connector_icon_path(row.get("connector", "")))
    icon_html = (
        f'<img class="pf-approval-icon" src="{_html_escape(icon_uri)}" alt="">' if icon_uri
        else f'<div class="pf-approval-icon pf-approval-icon-fallback">{_html_escape(connector[:1])}</div>'
    )
    return (
        f'<div class="pf-approval-row" data-approval-id="{_html_escape(rid)}">'
        f"{icon_html}"
        '<div class="pf-approval-main">'
        f'<div class="pf-approval-title">{_html_escape(title)}</div>'
        f'<div class="pf-approval-kicker">{_html_escape(kicker)}</div>'
        "</div>"
        '<div class="pf-approval-actions">'
        f'<button type="button" class="pf-btn-deny" data-deny="{_html_escape(rid)}">Deny</button>'
        f'<a class="pf-btn-review" href="/approvals/{_html_escape(rid)}">Review →</a>'
        "</div>"
        "</div>"
    )


def _relative_age(iso_ts: str) -> str:
    if not iso_ts:
        return ""
    try:
        dt = datetime.fromisoformat(iso_ts)
    except ValueError:
        return ""
    seconds = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def build_list_html(rows: list[dict[str, Any]], *, csrf: str) -> str:
    """The ``/approvals`` page body (dropped into web_shell.wrap's
    ``<main>``) -- ``rows`` is a list of row_from_approval()'s shape,
    newest first (same order approvals.PendingApprovalRegistry.
    list_pending() already returns)."""
    body = "".join(_row_html(r) for r in rows) if rows else _EMPTY_STATE
    heading = (
        f"{len(rows)} approval{'s' if len(rows) != 1 else ''} pending" if rows else ""
    )
    js = _JS % {"empty": json.dumps(_EMPTY_STATE), "csrf": json.dumps(csrf)}
    return (
        f"<style>{_CSS}</style>"
        '<div class="pf-approvals-page">'
        + (f'<div class="pf-approvals-heading">{_html_escape(heading)}</div>' if heading else "")
        + f'<div id="pf-approvals-list">{body}</div>'
        "</div>"
        f"<script>{js}</script>"
    )
