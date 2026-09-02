"""Shared local-mode session/CSRF helpers for every route in the combined
web app (docs/https-connector-refactor-plan.md §16.3: "/approvals and
/settings are one application: one header, one nav, one palette, one
session, links both ways") -- factored out of web/routes_approvals.py,
which owned this logic alone before web/routes_settings.py needed the exact
same "possession of web_token is the authority" posture (see web/server.py's
own module docstring) on a second set of routes sharing the same token and
the same ``pf_session`` cookie.

Nothing here changes routes_approvals.py's existing behavior or its own
tests -- see that module for the only caller of these functions before this
phase; this file is a pure extraction, not a redesign.
"""
from __future__ import annotations

import hmac

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

SESSION_COOKIE = "pf_session"


def authenticated(request: Request, token: str) -> bool:
    cookie = request.cookies.get(SESSION_COOKIE, "")
    if cookie and hmac.compare_digest(cookie, token):
        return True
    return hmac.compare_digest(request.query_params.get("token", ""), token)


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", path="/")


def unauthorized_html() -> Response:
    return HTMLResponse(
        "<!DOCTYPE html><html><body style=\"font:15px sans-serif;padding:40px\">"
        "Not authorized. Open the link PrivacyFence gave you, including its "
        "<code>?token=</code> parameter.</body></html>",
        status_code=401,
    )


def check_csrf(request: Request, csrf: str | None) -> bool:
    """Double-submit check: the session cookie (HttpOnly, so page JS never
    reads it -- it can only have been set by this server's own
    set_session_cookie) must equal the csrf value the page's own bridge
    shim baked in at render time. Constant-time compare -- same posture
    ipc_server.py's own token check takes for ~/.privacyfence/ipc_token."""
    cookie = request.cookies.get(SESSION_COOKIE, "")
    if not cookie or not csrf:
        return False
    return hmac.compare_digest(cookie, csrf)


def check_origin(request: Request) -> bool:
    """Defense in depth on top of the double-submit token above -- a
    same-site page couldn't forge the cookie value into its own request
    body, but this also stops a same-origin-cookie-jar edge case from ever
    mattering. ``None`` (no Origin header at all, e.g. a same-origin
    navigation in some browsers) is accepted -- only a *mismatched* Origin
    is rejected."""
    origin = request.headers.get("origin")
    if origin is None:
        return True
    return origin == f"{request.url.scheme}://{request.url.netloc}"
