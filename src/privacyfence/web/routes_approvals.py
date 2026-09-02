"""The web approval surface: WebApprovalUI (web_approval_ui.py) registers a
pending card or confirmation, and these routes are what let a human actually
see and decide it from a browser instead of a native dialog.

P3: ``GET /approvals`` lists every currently-unanswered card/confirmation
(approvals.PendingApprovalRegistry.list_pending()), not just one -- several
can genuinely be pending at once now that gate.py's ``_popup_lock`` is gone
(§6 of docs/https-connector-refactor-plan.md), each independently
decidable from its own ``/approvals/{id}`` link. ``GET
/api/approvals/stream`` is the SSE counterpart (§7.1) so the list page (or
whatever's showing it) updates live as approvals appear and get decided,
without polling.

The one JS change to approval_window_html.py's/dialog_window_html.py's
otherwise-untouched documents (§7.1's own wording, and P0's own validated
approach, §11 of that document): a small shim script, injected here rather
than editing either module, defines ``window.webkit.messageHandlers.pf.
postMessage`` as a ``fetch()`` POST to this module's own decide endpoint --
the two shipped documents never need to know whether they're running in a
WKWebView or a browser tab.
"""
from __future__ import annotations

import asyncio
import json
import logging
from html import escape as _html_escape

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Route

from ..web_approval_ui import WebApprovalUI
from .session_auth import SESSION_COOKIE as _SESSION_COOKIE
from .session_auth import authenticated as _token_authenticated
from .session_auth import check_csrf as _csrf_matches
from .session_auth import check_origin as _origin_ok
from .session_auth import set_session_cookie as _set_session_cookie_on
from .session_auth import unauthorized_html as _unauthorized_response

# How often the SSE stream below checks for a change in what's pending --
# not a hard real-time guarantee, just short enough that a human watching
# the list page doesn't perceive a lag. Polling the registry's own
# in-memory state (cheap) rather than adding a pub/sub mechanism.
_STREAM_POLL_SECONDS = 1.0

logger = logging.getLogger(__name__)

# _SESSION_COOKIE re-exported (see the session_auth import above) purely so
# this module's own docstring/history referencing "pf_session" as a local
# name still resolves -- session_auth.py is the actual definition now,
# shared with web/routes_settings.py. See that module's own docstring for
# why this is the whole local-mode auth model in P1: the same "possession
# of the token is the authority" posture ~/.privacyfence/ipc_token already
# has for the bridge. SameSite=Strict + HttpOnly: never sent cross-site,
# never readable from page JS.

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


def create_app(
    web_ui: WebApprovalUI, *, token: str, extra_routes: list[BaseRoute] | None = None, lifespan=None,
) -> Starlette:
    """Build the Starlette app serving the approval surface. ``token`` is
    the shared local-mode secret (see server.py) -- this function takes it
    as a plain argument rather than reading paths.py itself, so tests can
    construct an app against an isolated WebApprovalUI/token pair with no
    filesystem or global-singleton dependency.

    ``extra_routes``/``lifespan`` are how server.py folds the ``/mcp``
    endpoint (routes_mcp.py, P2) into this same combined app rather than
    running a second ASGI app/server on a second port -- one embedded HTTP
    server, per docs/https-connector-refactor-plan.md §3's target
    architecture. Both default to nothing so every existing caller
    (including this module's own tests) is unaffected.
    """

    def _authenticated(request: Request) -> bool:
        return _token_authenticated(request, token)

    def _unauthorized() -> Response:
        return _unauthorized_response()

    def _set_session_cookie(response: Response) -> None:
        _set_session_cookie_on(response, token)

    async def index(request: Request) -> Response:
        return RedirectResponse(f"/approvals?token={token}" if "token" not in request.query_params else "/approvals")

    def _list_rows() -> list:
        return web_ui.deferred_registry.list_pending()

    async def list_approvals(request: Request) -> Response:
        if not _authenticated(request):
            return _unauthorized()
        pending = _list_rows()
        if not pending:
            body = "<p>No approvals are currently pending.</p>"
        else:
            items = []
            for card in pending:
                label = "Approval" if card.kind == "card" else "Confirmation"
                title = _html_escape(card.tool_name or card.summary or label)
                items.append(f'<li><a href="/approvals/{card.id}">{_html_escape(label)}: {title}</a></li>')
            body = f"<ul>{''.join(items)}</ul>"
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
        card = web_ui.deferred_registry.get(approval_id)
        if card is None or card.event.is_set():
            # Covers both "never existed" and "already decided" -- an
            # answered card is left in the registry a while longer now (it
            # may still be feeding the decision ledger, see approvals.py),
            # but there's nothing left here for a human to decide, so this
            # says so rather than 404-ing (docs/https-connector-refactor-
            # plan.md §7.1).
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

    async def approvals_stream(request: Request) -> Response:
        if not _authenticated(request):
            return _unauthorized()

        async def event_source():
            last_ids: tuple[str, ...] | None = None
            while True:
                if await request.is_disconnected():
                    break
                ids = tuple(card.id for card in _list_rows())
                if ids != last_ids:
                    last_ids = ids
                    yield f"data: {json.dumps(list(ids))}\n\n"
                await asyncio.sleep(_STREAM_POLL_SECONDS)

        return StreamingResponse(
            event_source(), media_type="text/event-stream", headers={"Cache-Control": "no-store"},
        )

    async def decide(request: Request) -> Response:
        approval_id = request.path_params["id"]
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(payload, dict) or not _csrf_matches(request, payload.get("csrf")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        # Origin check on top of the double-submit token above -- the two
        # are independent defenses (see docs/https-connector-refactor-plan.md
        # §10.5's CSRF row): a same-site page couldn't forge the cookie
        # value into its own request body, but this also stops a
        # same-origin-cookie-jar edge case from ever mattering.
        if not _origin_ok(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        result = payload.get("result")
        choice = payload.get("choice")
        choice = int(choice) if isinstance(choice, (int, float)) else None
        # A card/confirm result is one of approvals.CARD_RESULTS/
        # CONFIRM_RESULTS -- always a string. A *choice* dialog
        # (dialog_window_html.build_choice_html, W5's web_prompt.py picker)
        # posts its selected option's index as a bare number instead (see
        # that module's own JS -- there is no separate "choice" field), so
        # an int/float here is accepted too and normalized to its string
        # form before being handed to web_ui.resolve(); web_prompt.py's own
        # reader parses it back with int().
        if isinstance(result, bool) or not isinstance(result, (str, int, float)):
            return JSONResponse({"error": "missing result"}, status_code=400)
        if not isinstance(result, str):
            result = str(int(result))
        accepted = web_ui.resolve(approval_id, result, choice)
        if not accepted:
            # Idempotent by design (§7.1): the first accepted decision for
            # an id wins, any later one -- including a genuine double-submit
            # from a slow network retry -- is rejected here, not treated as
            # an error worth alarming over.
            return JSONResponse({"status": "already_decided"}, status_code=409)
        return JSONResponse({"status": "ok"})

    routes: list[BaseRoute] = [
        Route("/", index),
        Route("/approvals", list_approvals),
        Route("/approvals/{id}", show_approval),
        Route("/api/approvals/{id}/decide", decide, methods=["POST"]),
        Route("/api/approvals/stream", approvals_stream),
    ]
    routes.extend(extra_routes or [])
    return Starlette(routes=routes, lifespan=lifespan)
