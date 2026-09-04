"""Org-mode browser sessions (P7, docs/https-connector-refactor-plan.md
§9.4: "Session cookie: Secure, HttpOnly, SameSite=Strict, short idle
timeout"). A server-side session store mapping an opaque, unguessable
session id to the ``Principal`` that authenticated it via web/routes_org_
identity.py's ``/login`` -- deliberately not the local-mode ``session_
auth.py`` model of "the cookie's own value is the one shared secret
everyone in the install has": org mode has more than one user, so each
session has to carry its own, distinct identity.

Not wired into routes_approvals.py/routes_settings.py's own auth checks in
this phase -- those stay local-mode-only for now (a documented follow-up,
see docs/https-connector-refactor-plan.md's P7 section). What this module
and routes_org_identity.py deliver is the session mechanism itself, real
and tested end to end, plus web/server.py's ``_PrincipalScopeMiddleware``
resolving ``current_principal()`` from it (P6's own seam) -- so any route
built against it from here on gets per-principal scoping for free.
"""
from __future__ import annotations

import hmac
import secrets
import threading
import time
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import Response

from ..principal import Principal

SESSION_COOKIE = "pf_org_session"

# §9.4's own words: "short idle timeout". 30 minutes -- renewed on every
# authenticated request (see get() below), so an active user is never
# logged out mid-task; an abandoned tab is.
DEFAULT_IDLE_TIMEOUT_SECONDS = 30 * 60


@dataclass
class OrgSession:
    principal: Principal
    created_at: float
    last_seen_at: float


class OrgSessionStore:
    """In-memory -- a session dying with the daemon process is an accepted
    cost (the same one local mode's own web_token/mcp_token files avoid
    only because there's nothing to distribute across a restart there
    either way), not a design gap: persisting live sessions across a
    restart would mean persisting session ids in plaintext somewhere, which
    is a bigger new risk than "sign in again after a restart.\""""

    def __init__(self, *, idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS) -> None:
        self._idle_timeout_seconds = idle_timeout_seconds
        self._lock = threading.Lock()
        self._sessions: dict[str, OrgSession] = {}

    def create(self, principal: Principal) -> str:
        session_id = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._sessions[session_id] = OrgSession(principal=principal, created_at=now, last_seen_at=now)
        return session_id

    def get(self, session_id: str) -> Principal | None:
        """The session's ``Principal`` if ``session_id`` is live and not
        idle-expired -- touches ``last_seen_at`` as a side effect (the
        sliding idle timeout §9.4 asks for). ``None`` for an unknown or
        expired session; never raises, so a forged or stale cookie is just
        "not authenticated," not a 500."""
        now = time.time()
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if (now - session.last_seen_at) > self._idle_timeout_seconds:
                del self._sessions[session_id]
                return None
            session.last_seen_at = now
            return session.principal

    def destroy(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def destroy_all_for(self, principal_id: str) -> int:
        """Revokes every live session belonging to one principal (e.g. a
        future "sign out everywhere" action). Returns how many were
        removed."""
        with self._lock:
            stale = [sid for sid, s in self._sessions.items() if s.principal.id == principal_id]
            for sid in stale:
                del self._sessions[sid]
            return len(stale)

    @property
    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)


def authenticated(request: Request, store: OrgSessionStore) -> Principal | None:
    session_id = request.cookies.get(SESSION_COOKIE, "")
    if not session_id:
        return None
    return store.get(session_id)


def set_session_cookie(response: Response, session_id: str) -> None:
    # secure=True (unlike session_auth.py's local-mode cookie): org mode is
    # HTTPS-mandatory (§10.2), so a Secure cookie is never silently dropped
    # here the way it would be forced to be over local mode's deliberate
    # plain-HTTP loopback transport (D1, §15).
    response.set_cookie(SESSION_COOKIE, session_id, httponly=True, samesite="strict", secure=True, path="/")


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def check_csrf(request: Request, csrf: str | None) -> bool:
    """Double-submit check for org-mode mutations (P8, docs/https-connector-
    refactor-plan.md §9.3), mirroring web/session_auth.py's own
    ``check_csrf`` exactly -- but against ``pf_org_session`` instead of
    local mode's ``pf_session``. The session id itself doubles as the CSRF
    token here for the same reason it does in local mode: it's server-set,
    HttpOnly (page JS can never read it out of the cookie jar), and only
    reachable by a page this server itself rendered baking the same value
    in -- and unlike local mode's *shared* token, each org session already
    has its own unguessable id (``OrgSessionStore.create()``), so no
    separate per-session CSRF value needs to be minted and tracked."""
    cookie = request.cookies.get(SESSION_COOKIE, "")
    if not cookie or not csrf:
        return False
    return hmac.compare_digest(cookie, csrf)


def check_origin(request: Request) -> bool:
    """Defense in depth on top of the double-submit token above -- see
    web/session_auth.py's identical function for the full rationale.
    ``None`` (no Origin header at all) is accepted; only a *mismatched*
    Origin is rejected."""
    origin = request.headers.get("origin")
    if origin is None:
        return True
    return origin == f"{request.url.scheme}://{request.url.netloc}"


__all__ = [
    "DEFAULT_IDLE_TIMEOUT_SECONDS",
    "SESSION_COOKIE",
    "OrgSession",
    "OrgSessionStore",
    "authenticated",
    "check_csrf",
    "check_origin",
    "clear_session_cookie",
    "set_session_cookie",
]
