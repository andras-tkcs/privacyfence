"""Passkey enrollment for org mode (P9, docs/https-connector-refactor-plan.md
§10.6, §15 D7): ``GET /security`` lets a signed-in principal see and manage
their own enrolled WebAuthn credentials, and the ``/api/security/webauthn/*``
routes drive the two ceremonies webauthn_stepup.py implements. web/routes_
org_approvals.py's decide endpoint is the other, later consumer of an
enrolled credential (the actual step-up check on a write approval) -- this
module only ever registers or removes one.

Same posture as web/routes_connect.py (not a port of routes_settings.py's
whole surface, org mode's own session-cookie CSRF model via org_session.
check_csrf/check_origin, not web_shell.wrap()'d) -- see that module's own
docstring for the reasoning, which applies here unchanged.

``PF_WEBAUTHN_JS`` (the base64url <-> ArrayBuffer conversions and the two
``navigator.credentials`` wrapper calls) is defined here and imported by
web/routes_org_approvals.py's own step-up shim rather than duplicated --
this module owns it only because enrollment is where the ceremony's shape
first has to exist; there is nothing enrollment-specific about the helpers
themselves.
"""
from __future__ import annotations

import json
import logging
from html import escape as _esc
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .. import webauthn_stepup
from ..org_mode import StepUpConfig
from ..principal import Principal
from ..webauthn_stepup import RegistrationChallengeStore, WebAuthnError
from . import org_session
from .org_session import OrgSessionStore

logger = logging.getLogger(__name__)

# Shared with web/routes_org_approvals.py's decide-time step-up shim -- see
# module docstring. Defines window.pfWebauthnCreate(optionsJson) and
# window.pfWebauthnGet(optionsJson), each returning a Promise of the plain
# JSON-shaped credential object webauthn_stepup.py's finish_registration()/
# verify_assertion() expect (the same camelCase field names @simplewebauthn/
# browser uses, since py_webauthn's own JSON parsing matches that shape --
# see webauthn_stepup.py's module docstring on why hand-rolling the
# verification side, but not this encode/decode plumbing, would be a
# mistake). Written by hand, not loaded from a CDN: web/server.py's CSP
# (script-src 'unsafe-inline', no external host) allows no external script
# on any page this daemon serves -- see docs/https-connector-refactor-
# plan.md §10.5's CSP row.
PF_WEBAUTHN_JS = """
function pfB64uToBuf(s) {
  var b64 = s.replace(/-/g, '+').replace(/_/g, '/');
  while (b64.length % 4) { b64 += '='; }
  var bin = atob(b64);
  var buf = new Uint8Array(bin.length);
  for (var i = 0; i < bin.length; i++) { buf[i] = bin.charCodeAt(i); }
  return buf.buffer;
}
function pfBufToB64u(buf) {
  var bytes = new Uint8Array(buf);
  var bin = '';
  for (var i = 0; i < bytes.length; i++) { bin += String.fromCharCode(bytes[i]); }
  return btoa(bin).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '');
}
function pfWebauthnCreate(optionsJson) {
  var o = JSON.parse(optionsJson);
  var publicKey = {
    rp: o.rp,
    user: { id: pfB64uToBuf(o.user.id), name: o.user.name, displayName: o.user.displayName },
    challenge: pfB64uToBuf(o.challenge),
    pubKeyCredParams: o.pubKeyCredParams,
    timeout: o.timeout,
    excludeCredentials: (o.excludeCredentials || []).map(function (c) {
      return { id: pfB64uToBuf(c.id), type: c.type };
    }),
    authenticatorSelection: o.authenticatorSelection,
    attestation: o.attestation
  };
  return navigator.credentials.create({ publicKey: publicKey }).then(function (cred) {
    return {
      id: cred.id, rawId: pfBufToB64u(cred.rawId), type: cred.type,
      response: {
        clientDataJSON: pfBufToB64u(cred.response.clientDataJSON),
        attestationObject: pfBufToB64u(cred.response.attestationObject)
      }
    };
  });
}
function pfWebauthnGet(optionsJson) {
  var o = JSON.parse(optionsJson);
  var publicKey = {
    rpId: o.rpId,
    challenge: pfB64uToBuf(o.challenge),
    timeout: o.timeout,
    allowCredentials: (o.allowCredentials || []).map(function (c) {
      return { id: pfB64uToBuf(c.id), type: c.type };
    }),
    userVerification: o.userVerification
  };
  return navigator.credentials.get({ publicKey: publicKey }).then(function (cred) {
    return {
      id: cred.id, rawId: pfBufToB64u(cred.rawId), type: cred.type,
      response: {
        clientDataJSON: pfBufToB64u(cred.response.clientDataJSON),
        authenticatorData: pfBufToB64u(cred.response.authenticatorData),
        signature: pfBufToB64u(cred.response.signature),
        userHandle: cred.response.userHandle ? pfBufToB64u(cred.response.userHandle) : null
      }
    };
  });
}
"""


def build_routes(*, sessions: OrgSessionStore, step_up: StepUpConfig, issuer_url: str) -> list[Route]:
    challenges = RegistrationChallengeStore()
    origin = issuer_url.rstrip("/")

    def _current_principal(request: Request) -> Principal | None:
        return org_session.authenticated(request, sessions)

    def _check_post(request: Request, csrf: Any) -> Response | None:
        if not org_session.check_csrf(request, csrf):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not org_session.check_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        return None

    async def security_page(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return RedirectResponse("/login?next=/security", status_code=302, headers={"Cache-Control": "no-store"})
        session_id = request.cookies.get(org_session.SESSION_COOKIE, "")
        creds = webauthn_stepup.list_credentials(principal)
        html = _render_security_page(principal=principal, creds=creds, csrf=session_id, step_up=step_up)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    async def register_options(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        rejected = _check_post(request, payload.get("csrf") if isinstance(payload, dict) else None)
        if rejected is not None:
            return rejected
        if not step_up.rp_id:
            return JSONResponse({"error": "WebAuthn is not configured on this server"}, status_code=400)
        options_json, challenge = webauthn_stepup.begin_registration(
            principal, rp_id=step_up.rp_id, rp_name=step_up.rp_name,
        )
        challenges.put(principal.id, challenge)
        return JSONResponse({"options": json.loads(options_json)})

    async def register_verify(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        rejected = _check_post(request, payload.get("csrf"))
        if rejected is not None:
            return rejected
        challenge = challenges.pop(principal.id)
        if challenge is None:
            return JSONResponse({"error": "Registration attempt expired -- try again."}, status_code=400)
        credential = payload.get("credential")
        label = str(payload.get("label") or "Passkey")[:64]
        if not isinstance(credential, dict):
            return JSONResponse({"error": "missing credential"}, status_code=400)
        try:
            saved = webauthn_stepup.finish_registration(
                principal, credential, expected_challenge=challenge,
                rp_id=step_up.rp_id, origin=origin, label=label,
            )
        except WebAuthnError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"status": "ok", "credential_id": saved.credential_id, "label": saved.label})

    async def delete_credential(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return RedirectResponse("/login?next=/security", status_code=302, headers={"Cache-Control": "no-store"})
        form = await request.form()
        rejected = _check_post(request, form.get("csrf"))
        if rejected is not None:
            return rejected
        webauthn_stepup.remove_credential(principal, request.path_params["credential_id"])
        return RedirectResponse("/security", status_code=303, headers={"Cache-Control": "no-store"})

    return [
        Route("/security", security_page),
        Route("/api/security/webauthn/register/options", register_options, methods=["POST"]),
        Route("/api/security/webauthn/register/verify", register_verify, methods=["POST"]),
        Route("/security/credentials/{credential_id}/delete", delete_credential, methods=["POST"]),
    ]


# --------------------------------------------------------------------- #
# Page rendering -- same small, self-contained-document style as
# web/routes_connect.py's own _render_connect_page (see that module's own
# note on why this isn't web_shell.wrap()'d).
# --------------------------------------------------------------------- #

_STYLE = """
body{font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:640px;margin:0 auto;
  padding:32px 20px 64px;color:#1b1b1f;background:#fff}
h1{font-size:20px;margin:0 0 4px}
p.lead{color:#555;margin-top:0}
.flash{border-radius:8px;padding:10px 14px;margin:16px 0;font-size:14px}
.flash.ok{background:#e6f4ea;color:#1e7e34}
.flash.err{background:#fdecea;color:#a02a2a}
ul.creds{list-style:none;padding:0;margin:24px 0}
li.cred{display:flex;align-items:center;justify-content:space-between;padding:12px 0;border-bottom:1px solid #eee}
li.cred:last-child{border-bottom:none}
.name{font-weight:600}
.meta{color:#888;font-size:12px}
.badge{font-size:12px;padding:2px 8px;border-radius:999px;margin-left:8px;background:#f1f1f3;color:#555}
button.add{padding:8px 16px;border:none;border-radius:6px;background:#2451c9;color:#fff;font-size:14px;cursor:pointer}
button.remove{background:none;color:#a02a2a;text-decoration:underline;border:none;padding:0;font-size:13px;cursor:pointer}
.empty{color:#888;padding:20px 0}
"""

_PAGE_JS = """
document.addEventListener('DOMContentLoaded', function () {
  var btn = document.getElementById('pf-add-passkey');
  var status = document.getElementById('pf-passkey-status');
  var csrf = document.body.getAttribute('data-csrf');
  if (!btn) { return; }
  if (!window.PublicKeyCredential) {
    btn.disabled = true;
    if (status) { status.textContent = 'This browser does not support passkeys.'; }
    return;
  }
  btn.addEventListener('click', function () {
    btn.disabled = true;
    if (status) { status.textContent = 'Follow your browser\\'s prompt...'; }
    fetch('/api/security/webauthn/register/options', {
      method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({csrf: csrf})
    }).then(function (r) { return r.json(); }).then(function (data) {
      if (data.error) { throw new Error(data.error); }
      return pfWebauthnCreate(JSON.stringify(data.options));
    }).then(function (credential) {
      return fetch('/api/security/webauthn/register/verify', {
        method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({csrf: csrf, credential: credential, label: 'Passkey'})
      });
    }).then(function (r) { return r.json(); }).then(function (data) {
      if (data.error) { throw new Error(data.error); }
      window.location.reload();
    }).catch(function (err) {
      btn.disabled = false;
      if (status) { status.textContent = 'Could not add a passkey: ' + err.message; }
    });
  });
});
""" + PF_WEBAUTHN_JS


def _credential_row_html(principal: Principal, cred, csrf: str) -> str:
    created = ""
    try:
        import datetime as _dt
        created = _dt.datetime.fromtimestamp(cred.created_at, tz=_dt.timezone.utc).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        pass
    synced = '<span class="badge">Synced</span>' if cred.backed_up else '<span class="badge">Device-bound</span>'
    return (
        '<li class="cred"><span>'
        f'<span class="name">{_esc(cred.label)}</span>{synced}'
        f'<div class="meta">Added {_esc(created)}</div></span>'
        f'<form method="post" action="/security/credentials/{_esc(cred.credential_id)}/delete">'
        f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
        '<button type="submit" class="remove">Remove</button></form></li>'
    )


def _render_security_page(*, principal: Principal, creds: list, csrf: str, step_up: StepUpConfig) -> str:
    who = _esc(principal.email or principal.display_name or principal.id)
    rows = "".join(_credential_row_html(principal, c, csrf) for c in creds)
    body = f'<ul class="creds">{rows}</ul>' if creds else '<div class="empty">No passkeys added yet.</div>'
    scope_note = (
        "Required to approve a write." if step_up.scope == "writes"
        else "Required to approve a write, or a read that detected personal data."
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PrivacyFence -- Security</title><style>{_STYLE}</style></head>
<body data-csrf="{_esc(csrf)}">
<h1>Passkeys</h1>
<p class="lead">Signed in as {who}. A passkey (Face ID, Touch ID, fingerprint, or Windows Hello) proves it's
really you before a write approval is released, even if someone else has your unlocked phone. {_esc(scope_note)}</p>
{body}
<p><button type="button" class="add" id="pf-add-passkey">Add a passkey</button>
<span id="pf-passkey-status" class="meta"></span></p>
<p><a href="/connect">Back to connections</a></p>
<script>{_PAGE_JS}</script>
</body></html>"""


__all__ = ["PF_WEBAUTHN_JS", "build_routes"]
