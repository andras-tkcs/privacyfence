"""Shared pywebview JS-bridge plumbing for the three Windows webview hosts
(approval_window_windows.py, dialog_window_windows.py,
settings_window_windows.py).

macOS's bridge (see approval_window.py's own module docstring) is JS calling
``window.webkit.messageHandlers.pf.postMessage(msg)``, delivered to Python
via ``WKScriptMessageHandler``. That's a WKWebView-specific browser API --
it doesn't exist under pywebview's WebView2 backend at all, and pywebview
doesn't polyfill it either (it exposes its own bridge instead:
``window.pywebview.api.<method>()``, injected once the page's
``pywebviewready`` event fires).

Rather than touch the three ``*_html.py`` generators -- all already fully
AppKit/WebKit-Python-import-free, see their own module docstrings, and
exercised by tests that assert on their exact output -- this module defines
``window.webkit.messageHandlers.pf.postMessage`` as a small polyfill script
that forwards to pywebview's bridge instead. Every document's own
hand-written JS (button click handling, keyboard shortcuts, DOMContentLoaded
gating) runs completely unmodified; only this one script differs between the
two hosts.
"""
from __future__ import annotations

import json
from typing import Callable

# Queues the postMessage call until window.pywebview.api actually exists --
# pywebviewready fires once per window, generally very shortly after the
# document itself loads, but there's no ordering guarantee the two always
# race the same way across pywebview versions/backends, so this handles
# both "api already there" and "not yet" rather than assuming one.
BRIDGE_POLYFILL_JS = """
<script>
(function () {
  function send(msg) {
    window.pywebview.api.pf_message(JSON.stringify(msg));
  }
  window.webkit = window.webkit || {};
  window.webkit.messageHandlers = window.webkit.messageHandlers || {};
  window.webkit.messageHandlers.pf = {
    postMessage: function (msg) {
      if (window.pywebview && window.pywebview.api) {
        send(msg);
      } else {
        window.addEventListener("pywebviewready", function () { send(msg); }, { once: true });
      }
    }
  };
})();
</script>
"""


class BridgeApi:
    """pywebview ``js_api`` object -- exposes exactly one JS-callable
    method, ``pf_message``, mirroring the single ``"pf"``
    WKScriptMessageHandler name every ``*_html.py`` document's JS already
    posts to (see BRIDGE_POLYFILL_JS above). ``on_message`` receives the
    decoded dict payload, the same shape
    ``userContentController_didReceiveScriptMessage_`` gets on macOS."""

    def __init__(self, on_message: Callable[[dict], None]) -> None:
        self._on_message = on_message

    def pf_message(self, payload_json: str) -> None:
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError):
            return
        if isinstance(payload, dict):
            self._on_message(payload)


def inject_bridge_polyfill(html: str) -> str:
    """Insert BRIDGE_POLYFILL_JS into a document that already has a
    ``<head>`` element -- true of approval_window_html.py's/
    dialog_window_html.py's own ``build_*_html`` output (see this module's
    docstring). settings_window_html.py's ``build_html()`` returns a bare
    body fragment instead (no ``<head>`` at all -- it's designed to be
    loaded via macOS's ``loadHTMLString_baseURL_``, which tolerates that);
    settings_window_windows.py wraps that fragment in its own minimal
    ``<html>`` shell rather than calling this."""
    if "<head>" in html:
        return html.replace("<head>", f"<head>{BRIDGE_POLYFILL_JS}", 1)
    return BRIDGE_POLYFILL_JS + html
