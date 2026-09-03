"""Browser login for org mode (P7, docs/https-connector-refactor-plan.md
§9.4) -- PrivacyFence acting as its own OIDC relying party against the
org's IdP, for a human visiting the web approval/settings surface
directly. This is a *separate* IdP-facing redirect_uri from web/
oauth_provider.py's ``OrgOAuthProvider`` (which does the same IdP dance,
against the same IdP registration, but on Claude's behalf as part of
PrivacyFence's own OAuth 2.1 authorization-server role) -- both go through
org_identity.py's identical claims-to-Principal mapping (see that module's
own docstring on why that sharing matters), but they're two distinct flows
with two distinct redirect URIs an org admin registers once with the IdP:

- ``/oauth/idp/login-callback`` -- this module, a browser's own sign-in.
- ``/oauth/idp/callback`` -- web/oauth_provider.py, on behalf of a pending
  Claude/MCP-client authorization.

Not wired into routes_approvals.py's or routes_settings.py's own auth
checks in this phase -- see org_session.py's module docstring for what
that means and doesn't mean.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from .. import org_identity
from ..org_identity import IdpConfig
from . import org_session

logger = logging.getLogger(__name__)

LOGIN_CALLBACK_PATH = "/oauth/idp/login-callback"
DEFAULT_NEXT_PATH = "/approvals"

# A login attempt outlives one browser round trip to the IdP and back --
# generous but bounded, so an abandoned /login tab doesn't leak the
# in-memory store forever.
_LOGIN_ATTEMPT_TTL_SECONDS = 5 * 60


@dataclass
class _LoginAttempt:
    nonce: str
    code_verifier: str
    next_path: str
    created_at: float


class _LoginAttemptStore:
    """In-memory, short-lived -- same "a daemon restart invalidates it"
    posture as OrgSessionStore (see that module's own docstring): losing an
    in-flight login attempt on restart just means starting over at
    ``/login``, not a security gap."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._attempts: dict[str, _LoginAttempt] = {}

    def create(self, *, next_path: str) -> tuple[str, _LoginAttempt, str]:
        """Returns ``(state, attempt, code_challenge)`` -- ``state`` is the
        dict key (and the OAuth ``state`` param sent to the IdP);
        ``code_challenge`` is derived from the attempt's own
        ``code_verifier`` and handed back so the caller can build the
        authorization URL without generating PKCE material twice."""
        self._prune()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(16)
        code_verifier, code_challenge = org_identity.generate_pkce_pair()
        attempt = _LoginAttempt(
            nonce=nonce, code_verifier=code_verifier, next_path=next_path, created_at=time.time(),
        )
        with self._lock:
            self._attempts[state] = attempt
        return state, attempt, code_challenge

    def pop(self, state: str) -> _LoginAttempt | None:
        """Single-use: a state value is consumed the moment it's looked up,
        successfully or not, so the same IdP redirect can never be replayed
        to mint a second session."""
        with self._lock:
            return self._attempts.pop(state, None)

    def _prune(self) -> None:
        now = time.time()
        with self._lock:
            stale = [s for s, a in self._attempts.items() if (now - a.created_at) > _LOGIN_ATTEMPT_TTL_SECONDS]
            for s in stale:
                del self._attempts[s]


def _safe_next_path(raw: str | None) -> str:
    """Open-redirect defense: ``next`` must be a same-origin relative path
    (a leading ``/`` that isn't a scheme-relative ``//host/...``), or the
    default is used instead. Never trusts ``raw`` far enough to redirect
    to it verbatim.

    Checked on a backslash-normalized copy, not the raw string: browsers
    treat a leading backslash the same as a forward slash when resolving a
    redirect target (a well-known open-redirect bypass), so ``"/\\evil.
    example.com"`` -- which reads as an ordinary same-origin path to
    Python's own string/URL handling -- must still be rejected, because a
    real browser turns it into ``//evil.example.com``, a protocol-relative
    redirect off-site, before it ever gets there.
    """
    if not raw or not raw.startswith("/"):
        return DEFAULT_NEXT_PATH
    if raw.replace("\\", "/").startswith("//"):
        return DEFAULT_NEXT_PATH
    return raw


def build_routes(
    *, idp: IdpConfig, sessions: org_session.OrgSessionStore, base_url: str,
) -> list[Route]:
    """``base_url`` is this daemon's own externally-reachable origin (org
    mode's configured issuer/server URL) -- the redirect_uri PrivacyFence
    presents to the IdP has to be this fixed, pre-registered value, never
    derived from a request's own (spoofable) Host header.
    """
    attempts = _LoginAttemptStore()
    redirect_uri = f"{base_url.rstrip('/')}{LOGIN_CALLBACK_PATH}"

    async def login(request: Request) -> Response:
        next_path = _safe_next_path(request.query_params.get("next"))
        state, attempt, code_challenge = attempts.create(next_path=next_path)
        url = org_identity.build_authorization_url(
            idp, redirect_uri=redirect_uri, state=state, code_challenge=code_challenge, nonce=attempt.nonce,
        )
        return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})

    async def login_callback(request: Request) -> Response:
        idp_error = request.query_params.get("error")
        if idp_error:
            logger.info("Org sign-in declined by IdP: %s", idp_error)
            return PlainTextResponse("Sign-in was not completed.", status_code=400)
        state = request.query_params.get("state", "")
        code = request.query_params.get("code", "")
        attempt = attempts.pop(state) if state else None
        if attempt is None or not code:
            return PlainTextResponse("Sign-in failed: invalid or expired login attempt.", status_code=400)
        try:
            tokens = await asyncio.to_thread(
                org_identity.exchange_code_for_tokens,
                idp, code=code, redirect_uri=redirect_uri, code_verifier=attempt.code_verifier,
            )
            id_token = tokens.get("id_token")
            if not id_token:
                raise ValueError("IdP token response carried no id_token")
            claims = await asyncio.to_thread(org_identity.verify_id_token, idp, id_token, nonce=attempt.nonce)
            principal = org_identity.principal_from_claims(claims, idp)
        except Exception as exc:  # noqa: BLE001 -- any IdP-side failure ends the same way: sign-in didn't complete
            logger.warning("Org sign-in failed: %s", exc)
            return PlainTextResponse("Sign-in failed. Please try again.", status_code=400)
        session_id = sessions.create(principal)
        response = RedirectResponse(attempt.next_path, status_code=302, headers={"Cache-Control": "no-store"})
        org_session.set_session_cookie(response, session_id)
        return response

    async def logout(request: Request) -> Response:
        # POST-only, no CSRF token required: the worst a forged cross-site
        # POST here can do is log the victim out, not disclose or change
        # anything -- a widely-accepted trade-off for a logout endpoint
        # specifically (unlike every state-changing route routes_
        # approvals.py/routes_settings.py protect with a real CSRF check).
        session_id = request.cookies.get(org_session.SESSION_COOKIE, "")
        if session_id:
            sessions.destroy(session_id)
        response = RedirectResponse("/login", status_code=302, headers={"Cache-Control": "no-store"})
        org_session.clear_session_cookie(response)
        return response

    return [
        Route("/login", login),
        Route(LOGIN_CALLBACK_PATH, login_callback),
        Route("/logout", logout, methods=["POST"]),
    ]


__all__ = ["DEFAULT_NEXT_PATH", "LOGIN_CALLBACK_PATH", "build_routes"]
