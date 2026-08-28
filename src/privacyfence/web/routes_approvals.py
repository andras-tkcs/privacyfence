"""The web approval surface: WebApprovalUI (web_approval_ui.py) registers a
pending card or confirmation, and these routes are what let a human actually
see and decide it from a browser instead of a native dialog.

P1 scope only -- see web_approval_ui.py's own module docstring: at most one
approval is ever pending at a time (gate.py's ``_popup_lock`` still
serializes every popup-gate/review-gate call), so ``GET /approvals`` shows
zero or one row rather than the real per-principal list §7.1 of
docs/https-connector-refactor-plan.md describes; that list, SSE updates, and
concurrent decisions are P3's ``_popup_lock`` retirement, not this module's.

The one JS change to approval_window_html.py's/dialog_window_html.py's
otherwise-untouched documents (§7.1's own wording, and P0's own validated
approach, §11 of that document): a small shim script, injected here rather
than editing either module, defines ``window.webkit.messageHandlers.pf.
postMessage`` as a ``fetch()`` POST to this module's own decide endpoint --
the two shipped documents never need to know whether they're running in a
WKWebView or a browser tab.
"""
from __future__ import annotations

import hmac
import logging
from html import escape as _html_escape

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from ..web_approval_ui import WebApprovalUI

logger = logging.getLogger(__name__)

# Session cookie carrying the shared local token (see server.py's docstring
# for why this is the whole local-mode auth model in P1 -- the same
# "possession of the token is the authority" posture ~/.privacyfence/
# ipc_token already has for the bridge). SameSite=Strict + HttpOnly: never
# sent cross-site, never readable from page JS.
_SESSION_COOKIE = "pf_session"


_DECIDED_MESSAGE = "Decision recorded — you can close this tab."
_FAILED_MESSAGE = "Could not record this decision — please reload and try again."


def _bridge_shim(*, decide_url: str, csrf: str) -> str:
    """Runtime shim swapping approval_window_html.py's/dialog_window_html.py's
    own ``window.webkit.messageHandlers.pf.postMessage(payload)`` call for a
    ``fetch()`` POST here -- see module docstring. ``csrf`` is folded into
    every posted payload (double-submit: the same value also has to match
    the session cookie server-side, see _check_csrf below) rather than
    trusted from the cookie alone.
    """
    return (
        "<script>(function(){"
        "window.webkit = window.webkit || {};"
        "window.webkit.messageHandlers = window.webkit.messageHandlers || {};"
        "window.webkit.messageHandlers.pf = {postMessage: function(payload) {"
        f"var body = Object.assign({{}}, payload, {{csrf: {csrf!r}}});"
        f"fetch({decide_url!r}, {{method:'POST', credentials:'same-origin',"
        "headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)})"
        f".then(function(r){{ document.body.innerHTML = r.ok ? {_DECIDED_MESSAGE!r} : {_FAILED_MESSAGE!r}; }})"
        f".catch(function(){{ document.body.innerHTML = {_FAILED_MESSAGE!r}; }});"
        "}};"
        "})();</script>"
    )


def _inject_shim(html: str, shim: str) -> str:
    """Insert ``shim`` as the first child of the document's real ``<body>``
    tag. First child, not appended at the end -- it must define
    window.webkit before approval_window_html.py's/dialog_window_html.py's
    own <script> (also a direct child of <body>, added after body_html)
    runs its DOMContentLoaded handler; script tags execute in document
    order as parsed, so this ordering alone is enough, no defer/async
    needed.

    Searches for ``<body>`` only *after* ``</head>`` closes, not from the
    start of the document -- a plain ``html.replace("<body>", ..., 1)``
    finds whichever "<body>" comes first in the raw string, and the
    embedded stylesheet's own CSS comments genuinely contain that literal
    substring (e.g. styles.css: "the same-colored rail on <body>",
    "racing it on source order across two separate <style> blocks") well
    before the real tag, inside the document's one <style> block. Landing
    the shim there instead of in the real body makes the browser parse it
    as inert CSS text, not a script element -- it silently never runs, so
    window.webkit stays undefined and the button-row JS's own
    ``if (window.webkit && ...)`` guard just no-ops on every click. Found
    by actually driving a served card in headless Chromium and clicking
    Allow -- see the regression test below.
    """
    head_end = html.index("</head>")
    body_start = html.index("<body>", head_end) + len("<body>")
    return html[:body_start] + shim + html[body_start:]


def _check_csrf(request: Request, csrf: str | None) -> bool:
    cookie = request.cookies.get(_SESSION_COOKIE, "")
    if not cookie or not csrf:
        return False
    # constant-time compare -- same posture ipc_server.py's own token check
    # takes for ~/.privacyfence/ipc_token.
    return hmac.compare_digest(cookie, csrf)


def create_app(web_ui: WebApprovalUI, *, token: str) -> Starlette:
    """Build the Starlette app serving the approval surface. ``token`` is
    the shared local-mode secret (see server.py) -- this function takes it
    as a plain argument rather than reading paths.py itself, so tests can
    construct an app against an isolated WebApprovalUI/token pair with no
    filesystem or global-singleton dependency.
    """

    def _authenticated(request: Request) -> bool:
        cookie = request.cookies.get(_SESSION_COOKIE, "")
        if cookie and hmac.compare_digest(cookie, token):
            return True
        return hmac.compare_digest(request.query_params.get("token", ""), token)

    def _unauthorized() -> Response:
        return HTMLResponse(
            "<!DOCTYPE html><html><body style=\"font:15px sans-serif;padding:40px\">"
            "Not authorized. Open the link PrivacyFence gave you, including its "
            "<code>?token=</code> parameter.</body></html>",
            status_code=401,
        )

    def _set_session_cookie(response: Response) -> None:
        response.set_cookie(
            _SESSION_COOKIE, token, httponly=True, samesite="strict", path="/",
        )

    async def index(request: Request) -> Response:
        return RedirectResponse(f"/approvals?token={token}" if "token" not in request.query_params else "/approvals")

    async def list_approvals(request: Request) -> Response:
        if not _authenticated(request):
            return _unauthorized()
        card = web_ui.current()
        if card is None:
            body = "<p>No approvals are currently pending.</p>"
        else:
            label = "Approval" if card.kind == "card" else "Confirmation"
            body = f'<p><a href="/approvals/{card.id}">{_html_escape(label)} pending — click to review</a></p>'
        response = HTMLResponse(
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
            "<title>PrivacyFence — Approvals</title></head>"
            f"<body style=\"font:15px -apple-system,system-ui,sans-serif;padding:40px\">{body}</body></html>",
            headers={"Cache-Control": "no-store"},
        )
        _set_session_cookie(response)
        return response

    async def show_approval(request: Request) -> Response:
        if not _authenticated(request):
            return _unauthorized()
        approval_id = request.path_params["id"]
        card = web_ui.current()
        if card is None or card.id != approval_id:
            # Covers both "never existed" and "already decided" -- see
            # web_approval_ui.py's resolve(): a decided card is cleared from
            # current() immediately, not left around in a resolved state,
            # so there's nothing here to distinguish between the two. Says
            # so rather than 404-ing (docs/https-connector-refactor-plan.md
            # §7.1).
            return HTMLResponse(
                "<!DOCTYPE html><html><body style=\"font:15px sans-serif;padding:40px\">"
                "This approval is no longer pending — it may already have been decided, "
                "or the link has expired.</body></html>",
                status_code=200,
                headers={"Cache-Control": "no-store"},
            )
        shim = _bridge_shim(decide_url=f"/api/approvals/{card.id}/decide", csrf=token)
        response = HTMLResponse(_inject_shim(card.html, shim), headers={"Cache-Control": "no-store"})
        _set_session_cookie(response)
        return response

    async def decide(request: Request) -> Response:
        approval_id = request.path_params["id"]
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(payload, dict) or not _check_csrf(request, payload.get("csrf")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        # Origin check on top of the double-submit token above -- the two
        # are independent defenses (see docs/https-connector-refactor-plan.md
        # §10.5's CSRF row): a same-site page couldn't forge the cookie
        # value into its own request body, but this also stops a
        # same-origin-cookie-jar edge case from ever mattering.
        origin = request.headers.get("origin")
        if origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}":
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        result = payload.get("result")
        choice = payload.get("choice")
        choice = int(choice) if isinstance(choice, (int, float)) else None
        if not isinstance(result, str):
            return JSONResponse({"error": "missing result"}, status_code=400)
        accepted = web_ui.resolve(approval_id, result, choice)
        if not accepted:
            # Idempotent by design (§7.1): the first accepted decision for
            # an id wins, any later one -- including a genuine double-submit
            # from a slow network retry -- is rejected here, not treated as
            # an error worth alarming over.
            return JSONResponse({"status": "already_decided"}, status_code=409)
        return JSONResponse({"status": "ok"})

    routes = [
        Route("/", index),
        Route("/approvals", list_approvals),
        Route("/approvals/{id}", show_approval),
        Route("/api/approvals/{id}/decide", decide, methods=["POST"]),
    ]
    return Starlette(routes=routes)
