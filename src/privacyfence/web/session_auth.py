"""Shared local-mode session/CSRF/bootstrap helpers for every route in the
combined web app (docs/https-connector-refactor-plan.md §16.3: "/approvals
and /settings are one application: one header, one nav, one palette, one
session, links both ways") -- factored out of web/routes_approvals.py,
which owned this logic alone before web/routes_settings.py needed the exact
same posture on a second set of routes sharing the same session and the
same ``pf_session`` cookie.

**SEC-06 (docs/security-remediation-plan.md, Phase 1 item 1.2).** Through
v4.0.0a12 this module's whole model was "the cookie's own value is the one
shared secret everyone in the install has" -- the persistent, never-
rotated ``web_token`` itself, carried in the ``?token=`` query string the
daemon logged on every startup and reused, unchanged, across restarts
forever. That meant a token that leaked once (a log file, shell history, a
shared screen) stayed valid until someone manually deleted the token file
and restarted the daemon: no expiry, no single-use exchange, no way to
revoke just that one exposure.

This module now splits that one secret into two purpose-built pieces,
mirroring web/org_session.py's own real-session model (minus the
per-principal identity org mode needs and local mode doesn't):

- ``BootstrapStore`` mints short-lived, single-use codes -- the only thing
  ever carried in a URL (``?bootstrap=<code>``) or written to a log line
  (see daemon_main.py's own startup logging and web/server.py's
  ``_BootstrapMiddleware``). A code is consumed -- deleted -- the instant
  it's checked, valid or not, so it is exactly one attempt at establishing
  a session, never a standing credential.
- ``LocalSessionStore`` holds the real, server-side sessions a bootstrap
  exchange mints: an independent random id, a sliding idle timeout, and a
  hard absolute timeout from creation regardless of activity -- the
  "cookie with idle + absolute expiry" SEC-06 asks for. Nothing about a
  session id is ever derived from, or comparable to, the long-lived local
  secret (``web/server.py``'s ``load_or_create_token()``) any more; that
  secret's only remaining job is authorizing ``POST /api/bootstrap`` (see
  server.py) to mint a fresh bootstrap code on demand, via an
  ``Authorization`` header, never a query string -- so a human who still
  has filesystem access to this machine (the same trust boundary this
  secret has always drawn) can get back in without restarting the daemon
  once a session has expired, but nothing about that recovery path ever
  touches a URL, browser history, or a log file again either.
"""
from __future__ import annotations

import hmac
import secrets
import threading
import time
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

SESSION_COOKIE = "pf_session"
BOOTSTRAP_QUERY_PARAM = "bootstrap"

# Short-lived: long enough for a human to actually click the link the
# daemon just logged, short enough that a leaked log line/terminal
# scrollback stops being a usable credential quickly. Single-use (see
# BootstrapStore.consume) matters far more than the exact figure here --
# this is defense in depth on top of that, not the primary control.
BOOTSTRAP_TTL_SECONDS = 10 * 60

# Sliding idle timeout -- same posture and same figure as
# org_session.py's own DEFAULT_IDLE_TIMEOUT_SECONDS (§9.4: "short idle
# timeout"), renewed on every authenticated request so an active user is
# never logged out mid-task; an abandoned tab is.
DEFAULT_IDLE_TIMEOUT_SECONDS = 30 * 60

# Absolute cap from creation, regardless of activity -- SEC-06's own
# "cookie with idle + absolute expiry". Long enough that a background
# daemon someone actively uses through a workday doesn't force a
# re-bootstrap mid-task; short enough that a session -- unlike the
# previous design's literally-forever token -- has a real ceiling. Once it
# lapses, the answer to "how do I get back in" is a fresh bootstrap code:
# minted automatically on the next daemon restart, or on demand via
# ``POST /api/bootstrap`` (see this module's own docstring).
DEFAULT_ABSOLUTE_TIMEOUT_SECONDS = 24 * 60 * 60


@dataclass
class _Session:
    created_at: float
    last_seen_at: float


class LocalSessionStore:
    """Server-side session store backing the local-mode ``pf_session``
    cookie (SEC-06) -- the direct local-mode counterpart of
    web/org_session.py's ``OrgSessionStore``, minus the ``Principal`` each
    org session carries (local mode has exactly one identity, the same
    ``LOCAL_PRINCIPAL`` it always resolves to; see web/server.py's
    ``_default_principal``)."""

    def __init__(
        self,
        *,
        idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
        absolute_timeout_seconds: float = DEFAULT_ABSOLUTE_TIMEOUT_SECONDS,
    ) -> None:
        self._idle_timeout_seconds = idle_timeout_seconds
        self._absolute_timeout_seconds = absolute_timeout_seconds
        self._lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}

    def create(self) -> str:
        session_id = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._sessions[session_id] = _Session(created_at=now, last_seen_at=now)
        return session_id

    def touch(self, session_id: str) -> bool:
        """True, and refreshes ``last_seen_at``, iff ``session_id`` is live
        and neither idle- nor absolute-expired -- an expired session found
        here is evicted on the spot, the same "just not authenticated, not
        a 500" posture ``OrgSessionStore.get()`` takes for an unknown or
        stale cookie."""
        now = time.time()
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            if (now - session.last_seen_at) > self._idle_timeout_seconds:
                del self._sessions[session_id]
                return False
            if (now - session.created_at) > self._absolute_timeout_seconds:
                del self._sessions[session_id]
                return False
            session.last_seen_at = now
            return True

    def destroy(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    @property
    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)


class BootstrapStore:
    """One-time codes (SEC-06) that exchange for exactly one
    ``LocalSessionStore`` session -- minted server-side, either by
    daemon_main.py at every startup (the direct replacement for logging
    the long-lived token itself) or on demand via ``POST /api/bootstrap``
    (web/server.py) once a previous code/session has already expired."""

    def __init__(self, *, ttl_seconds: float = BOOTSTRAP_TTL_SECONDS) -> None:
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._codes: dict[str, float] = {}  # code -> expires_at

    def mint(self) -> str:
        code = secrets.token_urlsafe(32)
        with self._lock:
            self._codes[code] = time.time() + self._ttl_seconds
        return code

    def consume(self, code: str) -> bool:
        """True iff ``code`` was live and unexpired -- always removes it
        first, so presenting it again (a slow double-click, a replayed
        request, an attacker who intercepted it after the fact) never gets
        a second attempt, successful exchange or not."""
        if not code:
            return False
        with self._lock:
            expires_at = self._codes.pop(code, None)
        return expires_at is not None and expires_at >= time.time()


def authenticated(request: Request, sessions: LocalSessionStore) -> bool:
    session_id = request.cookies.get(SESSION_COOKIE, "")
    if not session_id:
        return False
    return sessions.touch(session_id)


def set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(SESSION_COOKIE, session_id, httponly=True, samesite="strict", path="/")


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def unauthorized_html(request: Request) -> Response:
    """The page a human actually lands on with no valid ``pf_session``
    cookie -- most commonly a bootstrap link that's already been used (it's
    single-use by design, see ``BootstrapStore.consume``) or a session that
    idle-/absolute-timed out, reopened by something like a browser
    restoring a previously-open tab verbatim rather than a fresh click on a
    freshly-logged link. Restarting PrivacyFence is offered as the
    fallback of last resort, not the first-and-only answer: ``POST
    /api/bootstrap`` (this module's own docstring) exists precisely so a
    stale link/expired session doesn't require one, and this page spells
    out the actual command rather than just naming the endpoint -- a
    reader who's landed here from a dead link is exactly the audience that
    finding this self-explanatory matters most for. ``request`` supplies
    only this page's own origin (scheme+host+port), the same one the
    reader is already looking at, so the command below can be pasted
    as-is."""
    origin = f"{request.url.scheme}://{request.url.netloc}"
    return HTMLResponse(
        "<!DOCTYPE html><html><body style=\"font:15px sans-serif;padding:40px;max-width:640px\">"
        "<p>Not authorized — this link has expired, was already used, or your "
        "session timed out.</p>"
        "<p>Easiest fix: open the newest sign-in link PrivacyFence logged "
        "(<code>~/.privacyfence/logs/privacyfence.log</code>) — every daemon "
        "startup logs a fresh one.</p>"
        "<p>Don't want to restart PrivacyFence just for that? From a terminal "
        "on this machine, mint a new one on demand and open the link it "
        "returns:</p>"
        "<pre style=\"white-space:pre-wrap;background:#f0f0f0;padding:10px;"
        "border-radius:4px\">curl -s -X POST "
        "-H \"Authorization: Bearer $(cat ~/.privacyfence/web_token)\" "
        f"{origin}/api/bootstrap</pre>"
        "</body></html>",
        status_code=401,
    )


def check_csrf(request: Request, csrf: str | None) -> bool:
    """Double-submit check: the session cookie (HttpOnly, so page JS never
    reads it -- it can only have been set by this server's own
    set_session_cookie) must equal the csrf value the page's own bridge
    shim baked in at render time -- the session id itself doubles as the
    CSRF token, the same reasoning web/org_session.py's own check_csrf
    gives for why no separate per-session value needs to be minted and
    tracked. Constant-time compare -- same posture ipc_server.py's own
    token check took for ~/.privacyfence/ipc_token, before P5 deleted
    both."""
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


def verify_bearer_secret(request: Request, secret: str) -> bool:
    """Constant-time check of an ``Authorization: Bearer <secret>`` header
    against the persistent local install secret (``web/server.py``'s
    ``load_or_create_token()``) -- the one remaining use of that raw value
    (SEC-06): minting a fresh bootstrap code on demand, via
    ``POST /api/bootstrap``, without needing to restart the daemon just
    because the previous one-time link already expired. Deliberately a
    header, never a query parameter -- a query string is exactly what
    SEC-06 is getting this secret out of."""
    auth = request.headers.get("authorization", "")
    prefix = "Bearer "
    if not auth.startswith(prefix):
        return False
    return hmac.compare_digest(auth[len(prefix):], secret)


__all__ = [
    "BOOTSTRAP_QUERY_PARAM",
    "BOOTSTRAP_TTL_SECONDS",
    "DEFAULT_ABSOLUTE_TIMEOUT_SECONDS",
    "DEFAULT_IDLE_TIMEOUT_SECONDS",
    "SESSION_COOKIE",
    "BootstrapStore",
    "LocalSessionStore",
    "authenticated",
    "check_csrf",
    "check_origin",
    "clear_session_cookie",
    "set_session_cookie",
    "unauthorized_html",
    "verify_bearer_secret",
]
