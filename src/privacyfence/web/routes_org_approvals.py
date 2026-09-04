"""The web approval surface, for org mode (P9, docs/https-connector-
refactor-plan.md's own P9 section). Not mounted through P8 -- web/server.py's
own module docstring explained why: the local-mode surface
(web/routes_approvals.py) authenticates with one shared secret and lists
*every* pending approval with no principal filtering, so exposing it as-is
under org mode would leak every principal's pending approvals to whoever
holds any valid token. This module is the principal-aware replacement P7's
own writeup named as real, scoped follow-up work: ``GET /approvals``,
``GET /approvals/{id}``, ``POST /api/approvals/{id}/decide`` and
``GET /api/approvals/stream`` all authorize against
``org_session.authenticated()`` and filter/authorize every read and write
through ``current_principal()`` (approvals.PendingApprovalRegistry's own P9
principal dimension -- see that module's docstring), exactly §10.5's
"every approval ... read is authorized against current_principal()".

Reuses web/routes_approvals.py's own ``_inject_shim``/``_SW_JS`` and its
decided-message vocabulary rather than duplicating them -- the card
document itself (approval_window_html.py's output) and the service worker
are principal-agnostic; only the *auth model* around them differs between
local and org mode, same as web/routes_connect.py's own relationship to
routes_settings.py.

**Step-up (§10.6, D7)** is the one piece with no local-mode analogue at
all: before releasing a *write* decision (or, with ``step_up.scope ==
"writes_and_pii_reads"``, a PII-flagged read too), the decide endpoint
demands proof of a fresh WebAuthn platform-authenticator assertion --
webauthn_stepup.py's own module docstring covers the ceremony and the
decision-fingerprint binding; this module is only the HTTP protocol
wrapping it: a first decide attempt with no ``webauthn_assertion`` gets a
``428`` carrying fresh assertion options (when a passkey is enrolled) and
an IdP re-auth link (always, as D7's own fallback), and a second attempt
carrying the completed assertion is verified and, on success, treated as
the original decision. **Deny needs no step-up** -- denying leaks nothing
(the same reasoning approval_list_html.py's own module docstring gives for
letting Deny live on the list row with no card at all), so step-up is
scoped to the two approving results (``accept``/``accept_all``) only.

The IdP re-auth path (``GET /api/approvals/{id}/stepup/idp`` ->
``GET /oauth/stepup/callback``) mirrors web/routes_org_identity.py's own
``/login`` flow almost exactly (same org_identity.py functions, same
single-use ``state``-keyed pending-attempt store) with one addition: the
callback must re-derive the *same* principal the step-up was started for,
not merely *a* signed-in principal -- otherwise a second IdP account
signing in through a leaked step-up link could authorize someone else's
pending decision. See ``_StepUpAuthAttemptStore``/``stepup_callback``
below.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from .. import approval_list_html, org_identity, webauthn_stepup
from ..org_identity import IdpConfig
from ..org_mode import StepUpConfig
from ..principal import Principal
from ..webauthn_stepup import StepUpChallengeStore, WebAuthnError
from ..web_approval_ui import WebApprovalUI
from . import org_session
from .org_session import OrgSessionStore
from .routes_approvals import _DECIDED_MESSAGE, _DENIED_MESSAGE, _ALREADY_DECIDED_MESSAGE, _FAILED_MESSAGE
from .routes_approvals import _inject_shim, _SW_JS
from .routes_security import PF_WEBAUTHN_JS

logger = logging.getLogger(__name__)

_STREAM_POLL_SECONDS = 1.0
_STEP_UP_ATTEMPT_TTL_SECONDS = 10 * 60

# Only an *approving* decision needs step-up -- see module docstring.
_STEP_UP_RESULTS = ("accept", "accept_all")


# --------------------------------------------------------------------- #
# IdP re-auth attempt state -- mirrors web/routes_org_identity.py's own
# _LoginAttemptStore / web/routes_connect.py's own _PendingAuthStore.
# --------------------------------------------------------------------- #

@dataclass
class _StepUpAuthAttempt:
    principal_id: str
    approval_id: str
    result: str
    choice: int | None
    nonce: str
    code_verifier: str
    created_at: float = field(default_factory=time.time)


class _StepUpAuthAttemptStore:
    def __init__(self, ttl: float = _STEP_UP_ATTEMPT_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._pending: dict[str, _StepUpAuthAttempt] = {}

    def put(self, state: str, attempt: _StepUpAuthAttempt) -> None:
        self._prune()
        with self._lock:
            self._pending[state] = attempt

    def pop(self, state: str) -> _StepUpAuthAttempt | None:
        with self._lock:
            return self._pending.pop(state, None)

    def _prune(self) -> None:
        now = time.time()
        with self._lock:
            stale = [s for s, a in self._pending.items() if (now - a.created_at) > self._ttl]
            for s in stale:
                del self._pending[s]


def _org_bridge_shim(*, decide_url: str, csrf: str, stepup_options_url: str) -> str:
    """The org-mode counterpart of web/routes_approvals.py's own
    ``_bridge_shim`` -- same ``window.webkit.messageHandlers.pf.postMessage``
    swap, plus the step-up branch a ``428`` response triggers (see module
    docstring). ``stepup_options_url`` is unused by the JS below directly
    (the ``428`` body already carries fresh options inline, see
    ``decide()``) but is threaded through so a future retry-without-a-body
    variant has somewhere to fetch a fresh challenge from without a second
    server-side endpoint to design; today's flow never needs it because the
    first ``428`` already includes everything the client needs.
    """
    del stepup_options_url  # reserved -- see docstring
    return (
        PF_WEBAUTHN_JS
        + "<script>(function(){"
        "window.webkit = window.webkit || {};"
        "window.webkit.messageHandlers = window.webkit.messageHandlers || {};"
        "function pfDecide(body){"
        f"return fetch({decide_url!r}, {{method:'POST', credentials:'same-origin',"
        "headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});"
        "}"
        "window.webkit.messageHandlers.pf = {postMessage: function(payload) {"
        f"var body = Object.assign({{}}, payload, {{csrf: {csrf!r}}});"
        "var isDeny = payload.result === 'deny' || payload.result === 'cancel';"
        "pfDecide(body).then(function(r){"
        "  if (r.status === 428) {"
        "    return r.json().then(function(data){"
        "      if (data.webauthn_options && window.PublicKeyCredential) {"
        "        return pfWebauthnGet(JSON.stringify(data.webauthn_options)).then(function(assertion){"
        "          var retryBody = Object.assign({}, body, {webauthn_assertion: assertion});"
        "          return pfDecide(retryBody);"
        "        }).catch(function(err){"
        "          if (data.idp_stepup_url) {"
        "            document.body.innerHTML = 'This approval needs extra verification. "
        "<a href=\"' + data.idp_stepup_url + '\">Verify by signing in again</a>';"
        "          } else {"
        f"            document.body.innerHTML = {_FAILED_MESSAGE!r} + ' (' + err.message + ')';"
        "          }"
        "          return null;"
        "        });"
        "      }"
        "      if (data.idp_stepup_url) {"
        "        document.body.innerHTML = 'This approval needs extra verification. "
        "<a href=\"' + data.idp_stepup_url + '\">Verify by signing in again</a>';"
        "        return null;"
        "      }"
        f"      document.body.innerHTML = {_FAILED_MESSAGE!r};"
        "      return null;"
        "    });"
        "  }"
        "  return r;"
        "}).then(function(r){"
        "  if (r === null) { return; }"
        "  var msg = null;"
        f"  if (r.ok) {{ msg = isDeny ? {_DENIED_MESSAGE!r} : {_DECIDED_MESSAGE!r}; }}"
        f"  else if (r.status === 409) {{ msg = {_ALREADY_DECIDED_MESSAGE!r}; }}"
        "  if (msg !== null) {"
        "    try { sessionStorage.setItem('pf_toast', JSON.stringify({msg: msg})); } catch (e) {}"
        "    window.location.replace('/approvals');"
        "    return;"
        "  }"
        f"  document.body.innerHTML = {_FAILED_MESSAGE!r};"
        "})"
        f".catch(function(){{ document.body.innerHTML = {_FAILED_MESSAGE!r}; }});"
        "}};"
        "})();</script>"
    )


_TOKENS_CSS = None  # lazily loaded -- see _tokens_css()


def _tokens_css() -> str:
    global _TOKENS_CSS
    if _TOKENS_CSS is None:
        from pathlib import Path

        _TOKENS_CSS = (Path(__file__).parent.parent / "resources" / "tokens.css").read_text(encoding="utf-8")
    return _TOKENS_CSS


def _render_list_page(rows: list, *, csrf: str) -> str:
    body = approval_list_html.build_list_html(rows, csrf=csrf)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PrivacyFence -- Approvals</title>
<style>{_tokens_css()}body{{background:var(--color-bg);color:var(--color-text);margin:0;
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}</style></head>
<body>{body}
<p style="text-align:center"><a href="/connect">Connections</a> &middot; <a href="/security">Passkeys</a></p>
</body></html>"""


def build_routes(
    *, web_ui: WebApprovalUI, sessions: OrgSessionStore, step_up: StepUpConfig, idp: IdpConfig, issuer_url: str,
) -> list[Route]:
    challenges = StepUpChallengeStore()
    stepup_attempts = _StepUpAuthAttemptStore()
    origin = issuer_url.rstrip("/")
    registry = web_ui.deferred_registry

    def _current_principal(request: Request) -> Principal | None:
        return org_session.authenticated(request, sessions)

    async def index(request: Request) -> Response:
        return RedirectResponse("/approvals", status_code=302, headers={"Cache-Control": "no-store"})

    async def list_approvals(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return RedirectResponse("/login?next=/approvals", status_code=302, headers={"Cache-Control": "no-store"})
        session_id = request.cookies.get(org_session.SESSION_COOKIE, "")
        rows = [approval_list_html.row_from_approval(card) for card in registry.list_pending(principal.id)]
        html = _render_list_page(rows, csrf=session_id)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    async def show_approval(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return RedirectResponse(
                f"/login?next=/approvals/{request.path_params['id']}", status_code=302,
                headers={"Cache-Control": "no-store"},
            )
        approval_id = request.path_params["id"]
        card = registry.get(approval_id, principal_id=principal.id)
        if card is None or card.event.is_set():
            return HTMLResponse(
                "<!DOCTYPE html><html><body style=\"font:15px sans-serif;padding:40px\">"
                "This approval is no longer pending — it may already have been decided, "
                "or the link has expired. <a href=\"/approvals\">Back to approvals</a>"
                "</body></html>",
                status_code=200,
                headers={"Cache-Control": "no-store"},
            )
        session_id = request.cookies.get(org_session.SESSION_COOKIE, "")
        shim = _org_bridge_shim(
            decide_url=f"/api/approvals/{card.id}/decide", csrf=session_id,
            stepup_options_url=f"/api/approvals/{card.id}/stepup/idp",
        )
        return HTMLResponse(_inject_shim(card.html, shim), headers={"Cache-Control": "no-store"})

    async def approvals_stream(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        async def event_source():
            last_ids: tuple[str, ...] | None = None
            while True:
                if await request.is_disconnected():
                    break
                ids = tuple(card.id for card in registry.list_pending(principal.id))
                if ids != last_ids:
                    last_ids = ids
                    yield f"data: {json.dumps(list(ids))}\n\n"
                await asyncio.sleep(_STREAM_POLL_SECONDS)

        return StreamingResponse(
            event_source(), media_type="text/event-stream", headers={"Cache-Control": "no-store"},
        )

    def _step_up_response(principal: Principal, approval_id: str, *, result: str, choice: int | None) -> JSONResponse:
        body: dict = {"error": "step_up_required"}
        if step_up.rp_id:
            begun = webauthn_stepup.begin_assertion(principal, rp_id=step_up.rp_id)
            if begun is not None:
                options_json, challenge = begun
                fingerprint = webauthn_stepup.decision_fingerprint(
                    approval_id=approval_id, principal_id=principal.id, result=result, choice=choice,
                )
                challenges.put(principal.id, approval_id, challenge=challenge, fingerprint=fingerprint)
                body["webauthn_options"] = json.loads(options_json)
        choice_q = "" if choice is None else str(int(choice))
        body["idp_stepup_url"] = (
            f"/api/approvals/{quote(approval_id)}/stepup/idp?result={quote(result)}&choice={quote(choice_q)}"
        )
        return JSONResponse(body, status_code=428)

    async def decide(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        approval_id = request.path_params["id"]
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(payload, dict) or not org_session.check_csrf(request, payload.get("csrf")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not org_session.check_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        result = payload.get("result")
        choice = payload.get("choice")
        choice = int(choice) if isinstance(choice, (int, float)) else None
        if isinstance(result, bool) or not isinstance(result, (str, int, float)):
            return JSONResponse({"error": "missing result"}, status_code=400)
        if not isinstance(result, str):
            result = str(int(result))

        if step_up.enabled and result in _STEP_UP_RESULTS:
            approval = registry.get(approval_id, principal_id=principal.id)
            if approval is not None and webauthn_stepup.is_step_up_required(
                gate_kind=approval.gate_kind, pii_detected=approval.pii_detected, scope=step_up.scope,
            ):
                assertion = payload.get("webauthn_assertion")
                if not isinstance(assertion, dict):
                    return _step_up_response(principal, approval_id, result=result, choice=choice)
                pending = challenges.pop(principal.id, approval_id)
                expected_fp = webauthn_stepup.decision_fingerprint(
                    approval_id=approval_id, principal_id=principal.id, result=result, choice=choice,
                )
                if pending is None or pending.fingerprint != expected_fp:
                    return JSONResponse({"error": "step_up_expired"}, status_code=400)
                try:
                    webauthn_stepup.verify_assertion(
                        principal, assertion, expected_challenge=pending.challenge,
                        rp_id=step_up.rp_id, origin=origin,
                    )
                except WebAuthnError as exc:
                    return JSONResponse({"error": str(exc)}, status_code=401)

        accepted = web_ui.resolve(approval_id, result, choice, principal_id=principal.id)
        if not accepted:
            return JSONResponse({"status": "already_decided"}, status_code=409)
        return JSONResponse({"status": "ok"})

    async def stepup_idp_start(request: Request) -> Response:
        """A same-site navigation the user's own click on the failed
        card's "Verify by signing in again" link makes -- the session
        cookie *is* present here (unlike the eventual callback, see module
        docstring)."""
        principal = _current_principal(request)
        if principal is None:
            return RedirectResponse(
                f"/login?next=/approvals/{quote(request.path_params['id'])}", status_code=302,
                headers={"Cache-Control": "no-store"},
            )
        approval_id = request.path_params["id"]
        result = request.query_params.get("result", "")
        choice_raw = request.query_params.get("choice", "")
        choice = int(choice_raw) if choice_raw.strip().lstrip("-").isdigit() else None
        if result not in _STEP_UP_RESULTS:
            return PlainTextResponse("Invalid step-up request.", status_code=400)

        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(16)
        code_verifier, code_challenge = org_identity.generate_pkce_pair()
        stepup_attempts.put(state, _StepUpAuthAttempt(
            principal_id=principal.id, approval_id=approval_id, result=result, choice=choice,
            nonce=nonce, code_verifier=code_verifier,
        ))
        redirect_uri = f"{origin}/oauth/stepup/callback"
        extra_params = {"prompt": "login", "max_age": "0"}
        if idp.step_up_acr_values:
            extra_params["acr_values"] = " ".join(idp.step_up_acr_values)
        url = org_identity.build_authorization_url(
            idp, redirect_uri=redirect_uri, state=state, code_challenge=code_challenge, nonce=nonce,
            extra_params=extra_params,
        )
        return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})

    async def stepup_callback(request: Request) -> Response:
        # Deliberately does not read the session cookie -- see module
        # docstring's own note (same reasoning as web/routes_connect.py's
        # own callback).
        idp_error = request.query_params.get("error")
        state = request.query_params.get("state", "")
        code = request.query_params.get("code", "")
        attempt = stepup_attempts.pop(state) if state else None
        if attempt is None:
            return PlainTextResponse("Step-up verification failed: invalid or expired attempt.", status_code=400)
        if idp_error or not code:
            return RedirectResponse(
                f"/approvals/{quote(attempt.approval_id)}?stepup=error", status_code=302,
                headers={"Cache-Control": "no-store"},
            )
        redirect_uri = f"{origin}/oauth/stepup/callback"
        try:
            tokens = await asyncio.to_thread(
                org_identity.exchange_code_for_tokens,
                idp, code=code, redirect_uri=redirect_uri, code_verifier=attempt.code_verifier,
            )
            id_token = tokens.get("id_token")
            if not id_token:
                raise ValueError("IdP token response carried no id_token")
            claims = await asyncio.to_thread(org_identity.verify_id_token, idp, id_token, nonce=attempt.nonce)
            reauthed = org_identity.principal_from_claims(claims, idp)
        except Exception as exc:  # noqa: BLE001 -- any IdP-side failure ends the same way
            logger.warning("Step-up re-authentication failed: %s", exc)
            return RedirectResponse(
                f"/approvals/{quote(attempt.approval_id)}?stepup=error", status_code=302,
                headers={"Cache-Control": "no-store"},
            )
        # The human who just re-authenticated must be the *same* one this
        # step-up was started for -- otherwise a leaked step-up link (the
        # approval URL itself is not a secret, per §10.4) could be
        # completed by signing in as someone else entirely. See module
        # docstring's own note on why this check exists.
        if reauthed.id != attempt.principal_id:
            logger.warning(
                "Step-up re-authentication resolved to a different principal (%s != %s) -- rejecting",
                reauthed.id, attempt.principal_id,
            )
            return RedirectResponse(
                f"/approvals/{quote(attempt.approval_id)}?stepup=error", status_code=302,
                headers={"Cache-Control": "no-store"},
            )
        if idp.step_up_acr_values and claims.get("acr") not in idp.step_up_acr_values:
            logger.warning("Step-up re-authentication did not satisfy the configured acr_values -- rejecting")
            return RedirectResponse(
                f"/approvals/{quote(attempt.approval_id)}?stepup=error", status_code=302,
                headers={"Cache-Control": "no-store"},
            )

        accepted = web_ui.resolve(attempt.approval_id, attempt.result, attempt.choice, principal_id=attempt.principal_id)
        status = "ok" if accepted else "already_decided"
        return RedirectResponse(
            f"/approvals?stepup={status}", status_code=302, headers={"Cache-Control": "no-store"},
        )

    async def service_worker(request: Request) -> Response:
        return PlainTextResponse(
            _SW_JS, media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
        )

    return [
        Route("/", index),
        Route("/approvals", list_approvals),
        Route("/approvals/{id}", show_approval),
        Route("/api/approvals/{id}/decide", decide, methods=["POST"]),
        Route("/api/approvals/stream", approvals_stream),
        Route("/api/approvals/{id}/stepup/idp", stepup_idp_start),
        Route("/oauth/stepup/callback", stepup_callback),
        Route("/sw.js", service_worker),
    ]


__all__ = ["build_routes"]
