#!/usr/bin/env python3
"""Manual environment check for §10.6 of docs/https-connector-refactor-plan.md: does a real
platform-authenticator (Face ID / Touch ID / Android fingerprint / Windows Hello) prompt actually
appear when a WebAuthn-capable link is opened from *inside a real Claude conversation*, on Desktop,
iOS, and Android?

§10.6 flags this as unknown after P0 and names it a blocking entry condition for P9 (step-up auth,
D7): the platform facts are settled (Chrome Custom Tabs and SFSafariViewController /
ASWebAuthenticationSession support platform WebAuthn fully; a bare embedded Android WebView does
not), but which component each of Claude's own apps actually uses to open an in-chat link is
app-specific behavior, not publicly documented, and can change between app versions. See
docs/webauthn-link-open-check.md for the full step-by-step procedure this script is one half of.

This is NOT a pytest test and NEVER runs in CI, for the same reason qa_popup_smoke.py and
qa_web_smoke.py don't:
  - It needs a human with real Claude Desktop/iOS/Android apps to tap a link and watch their own
    screen -- "did a biometric prompt appear" is not something any server can observe by itself.
  - It needs a public HTTPS URL with a real hostname (a secure context with a registrable RP ID --
    see §10.6's "RP-ID rule constrains D1" callout), which a CI runner has no business minting.

What this script *can* verify, and does: once a ceremony completes, it cryptographically confirms
what actually happened -- whether the browser's WebAuthn API was present at all (its total absence
is itself evidence of the bare-WebView failure mode this check exists to catch), whether the
server-side signature checks out, whether the UV (user-verified) flag in `authenticatorData` was
actually set (per §10.6: "verify user verification, not just the signature"), whether the
authenticator reported `platform` attachment (the mechanism D7 decided on, not a security key), and
whether the credential is backed up/synced (§10.6's "synced passkeys weaken 'this device'"). The one
thing it cannot see -- whether a human actually watched an OS biometric sheet appear on that specific
screen just now -- is the one question the page asks the tester to answer after each ceremony.

Requires `pip install webauthn` (not a project dependency -- this script never imports
`privacyfence` and is not part of the shipped package, same reasoning `qa_web_smoke.py`'s docstring
gives for `playwright`). Everything else is standard library. State (registered credentials, the
session log) lives in memory only and is gone the moment the process exits -- there is nothing to
clean up beyond stopping this process and whatever tunnel sits in front of it.

Usage:

    python3 scripts/webauthn_link_open_check.py [--host 127.0.0.1] [--port 8000]

Then put a tunnel in front of it (see docs/webauthn-link-open-check.md) and open the tunnel's
https:// URL from inside a real Claude conversation.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

logger = logging.getLogger("webauthn_link_open_check")

# --- In-memory state --- #
# Single-operator, single-process, never persisted -- see module docstring. A lock guards all three
# because ThreadingHTTPServer dispatches each request on its own thread.


@dataclass
class PendingChallenge:
    challenge: bytes
    created_at: float


@dataclass
class StoredCredential:
    credential_id: bytes
    public_key: bytes
    sign_count: int
    label: str
    device_type: str
    backed_up: bool
    attachment: str


@dataclass
class LogEntry:
    id: str
    time: str
    label: str
    action: str  # "register" | "login"
    user_verified: bool
    attachment: str
    backed_up: bool
    device_type: str
    saw_prompt: str | None = None


_LOCK = threading.Lock()
_PENDING: dict[str, PendingChallenge] = {}
_CREDENTIALS: dict[str, StoredCredential] = {}
_LOG: list[LogEntry] = []


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _origin_and_rp_id(handler: BaseHTTPRequestHandler) -> tuple[str, str]:
    # Deliberately dynamic, unlike D1's fixed-hostname production config (https-connector-refactor-
    # plan.md §10.2/§15 D1): this is a throwaway, single-operator tool meant to run behind whatever
    # tunnel hostname `cloudflared`/`ngrok` hands out that session, so the RP ID and origin are
    # derived from the incoming request instead of a config file. Never do this for a real relying
    # party -- trusting Host/X-Forwarded-Proto from the request is fine only because the tunnel in
    # front of this is the only thing that can reach it and is the one setting those headers.
    host_header = handler.headers.get("Host", "localhost")
    proto = handler.headers.get("X-Forwarded-Proto", "http")
    return f"{proto}://{host_header}", host_header.split(":")[0]


# --- HTTP plumbing --- #


def _json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", 0) or 0)
    raw = handler.rfile.read(length) if length else b"{}"
    return json.loads(raw or b"{}")


def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _send_text(handler: BaseHTTPRequestHandler, status: int, content_type: str, body: bytes) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


# --- Route handlers --- #


def serve_index(handler: BaseHTTPRequestHandler) -> None:
    _send_text(handler, 200, "text/html; charset=utf-8", HTML_PAGE.encode("utf-8"))


def handle_state(handler: BaseHTTPRequestHandler) -> None:
    with _LOCK:
        payload = {
            "log": [asdict(e) for e in _LOG],
            "credentials": [
                {
                    "label": c.label,
                    "device_type": c.device_type,
                    "backed_up": c.backed_up,
                    "attachment": c.attachment,
                }
                for c in _CREDENTIALS.values()
            ],
        }
    _send_json(handler, 200, payload)


def handle_report(handler: BaseHTTPRequestHandler) -> None:
    with _LOCK:
        rows = list(_LOG)
    lines = [
        "## WebAuthn link-open check (§10.6)",
        "",
        "| Time (UTC) | Device / app | Action | User-verified | Attachment | Backed up | Saw OS prompt? |",
        "|---|---|---|---|---|---|---|",
    ]
    for e in rows:
        lines.append(
            f"| {e.time} | {e.label} | {e.action} | {e.user_verified} | {e.attachment} "
            f"| {e.backed_up} | {e.saw_prompt or '(not recorded)'} |"
        )
    if not rows:
        lines.append("| _(no ceremonies run yet)_ | | | | | | |")
    _send_text(handler, 200, "text/plain; charset=utf-8", ("\n".join(lines) + "\n").encode("utf-8"))


def handle_register_options(handler: BaseHTTPRequestHandler) -> None:
    body = _json_body(handler)
    label = str(body.get("label") or "unlabeled device")[:200]
    origin, rp_id = _origin_and_rp_id(handler)
    user_id = secrets.token_bytes(16)
    options = generate_registration_options(
        rp_id=rp_id,
        rp_name="PrivacyFence WebAuthn link-open check",
        user_id=user_id,
        user_name=f"tester-{secrets.token_hex(4)}",
        user_display_name=label,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            # Matches the exact call shape D7 decided on (§10.6): platform attachment, UV required --
            # not a generic passkey demo that would also accept a security key or "preferred" UV.
            authenticator_attachment=AuthenticatorAttachment.PLATFORM,
            resident_key=ResidentKeyRequirement.DISCOURAGED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    state_token = secrets.token_hex(16)
    with _LOCK:
        _PENDING[state_token] = PendingChallenge(challenge=options.challenge, created_at=time.time())
    _send_json(handler, 200, {"state": state_token, "publicKey": json.loads(options_to_json(options))})


def handle_register_verify(handler: BaseHTTPRequestHandler) -> None:
    body = _json_body(handler)
    state_token = body.get("state")
    label = str(body.get("label") or "unlabeled device")[:200]
    origin, rp_id = _origin_and_rp_id(handler)
    with _LOCK:
        pending = _PENDING.pop(state_token, None)
    if pending is None:
        _send_json(handler, 400, {"error": "No pending registration for this state -- server restarted, or a double submit. Reload and try again."})
        return
    try:
        verification = verify_registration_response(
            credential=body["credential"],
            expected_challenge=pending.challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            require_user_verification=True,
        )
    except Exception as exc:
        _send_json(handler, 400, {"error": str(exc)})
        return
    cred_id_b64 = _b64url(verification.credential_id)
    attachment = body["credential"].get("authenticatorAttachment") or "unknown"
    with _LOCK:
        _CREDENTIALS[cred_id_b64] = StoredCredential(
            credential_id=verification.credential_id,
            public_key=verification.credential_public_key,
            sign_count=verification.sign_count,
            label=label,
            device_type=verification.credential_device_type.value,
            backed_up=verification.credential_backed_up,
            attachment=attachment,
        )
        entry = LogEntry(
            id=secrets.token_hex(8),
            time=_now(),
            label=label,
            action="register",
            user_verified=verification.user_verified,
            attachment=attachment,
            backed_up=verification.credential_backed_up,
            device_type=verification.credential_device_type.value,
        )
        _LOG.append(entry)
    _send_json(
        handler,
        200,
        {
            "credential_id": cred_id_b64,
            "user_verified": verification.user_verified,
            "attachment": attachment,
            "backed_up": verification.credential_backed_up,
            "device_type": verification.credential_device_type.value,
            "log_id": entry.id,
        },
    )


def handle_login_options(handler: BaseHTTPRequestHandler) -> None:
    body = _json_body(handler)
    credential_id = body.get("credential_id")
    origin, rp_id = _origin_and_rp_id(handler)
    with _LOCK:
        if credential_id and credential_id in _CREDENTIALS:
            allow = [PublicKeyCredentialDescriptor(id=_CREDENTIALS[credential_id].credential_id)]
        else:
            allow = [PublicKeyCredentialDescriptor(id=c.credential_id) for c in _CREDENTIALS.values()]
    options = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=allow or None,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    state_token = secrets.token_hex(16)
    with _LOCK:
        _PENDING[state_token] = PendingChallenge(challenge=options.challenge, created_at=time.time())
    _send_json(handler, 200, {"state": state_token, "publicKey": json.loads(options_to_json(options))})


def handle_login_verify(handler: BaseHTTPRequestHandler) -> None:
    body = _json_body(handler)
    state_token = body.get("state")
    origin, rp_id = _origin_and_rp_id(handler)
    cred_json = body["credential"]
    cred_id_b64 = cred_json["id"]
    with _LOCK:
        pending = _PENDING.pop(state_token, None)
        stored = _CREDENTIALS.get(cred_id_b64)
    if pending is None or stored is None:
        _send_json(handler, 400, {"error": "Unknown credential or expired state -- register on this device first."})
        return
    try:
        verification = verify_authentication_response(
            credential=cred_json,
            expected_challenge=pending.challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=stored.public_key,
            credential_current_sign_count=stored.sign_count,
            require_user_verification=True,
        )
    except Exception as exc:
        _send_json(handler, 400, {"error": str(exc)})
        return
    attachment = cred_json.get("authenticatorAttachment") or stored.attachment
    with _LOCK:
        stored.sign_count = verification.new_sign_count
        entry = LogEntry(
            id=secrets.token_hex(8),
            time=_now(),
            label=stored.label,
            action="login",
            user_verified=verification.user_verified,
            attachment=attachment,
            backed_up=verification.credential_backed_up,
            device_type=verification.credential_device_type.value,
        )
        _LOG.append(entry)
    _send_json(
        handler,
        200,
        {
            "user_verified": verification.user_verified,
            "attachment": attachment,
            "backed_up": verification.credential_backed_up,
            "device_type": verification.credential_device_type.value,
            "log_id": entry.id,
        },
    )


def handle_annotate(handler: BaseHTTPRequestHandler) -> None:
    body = _json_body(handler)
    log_id = body.get("log_id")
    saw_prompt = body.get("saw_prompt")
    with _LOCK:
        for e in _LOG:
            if e.id == log_id:
                e.saw_prompt = saw_prompt
                break
    _send_json(handler, 200, {"ok": True})


def handle_reset(handler: BaseHTTPRequestHandler) -> None:
    with _LOCK:
        _PENDING.clear()
        _CREDENTIALS.clear()
        _LOG.clear()
    _send_json(handler, 200, {"ok": True})


_ROUTES_GET = {"/": serve_index, "/api/state": handle_state, "/api/report": handle_report}
_ROUTES_POST = {
    "/api/register/options": handle_register_options,
    "/api/register/verify": handle_register_verify,
    "/api/login/options": handle_login_options,
    "/api/login/verify": handle_login_verify,
    "/api/log/annotate": handle_annotate,
    "/api/reset": handle_reset,
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # every response below sets Content-Length; keep-alive is cheap through a tunnel
    server_version = "webauthn-link-open-check/1.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002 -- matching BaseHTTPRequestHandler's signature
        logger.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's naming convention
        fn = _ROUTES_GET.get(urlsplit(self.path).path)
        if fn is None:
            self.send_error(404)
            return
        fn(self)

    def do_POST(self) -> None:  # noqa: N802
        fn = _ROUTES_POST.get(urlsplit(self.path).path)
        if fn is None:
            self.send_error(404)
            return
        try:
            fn(self)
        except Exception as exc:  # last-resort guard: one bad ceremony must not take the server down
            logger.exception("unhandled error in %s", self.path)
            _send_json(self, 500, {"error": str(exc)})


# --- The page --- #

HTML_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WebAuthn link-open check</title>
<style>
  :root { color-scheme: light; }
  body { margin: 0; padding: 1.25rem; font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #f7f7f8; color: #1a1a1a; max-width: 640px; margin-inline: auto; }
  h1 { font-size: 1.3rem; margin-bottom: 0.25rem; }
  .sub { color: #555; font-size: 0.9rem; margin-top: 0; }
  section { margin: 1.25rem 0; }
  label { display: block; font-weight: 600; margin-bottom: 0.35rem; }
  input[type=text], input:not([type]) { width: 100%; box-sizing: border-box; padding: 0.6rem; font-size: 1rem;
         border: 1px solid #ccc; border-radius: 8px; }
  .row { display: flex; gap: 0.6rem; margin-top: 0.75rem; flex-wrap: wrap; }
  button { font-size: 1rem; padding: 0.75rem 1.1rem; border-radius: 10px; border: 1px solid #333;
           background: #1a1a1a; color: #fff; cursor: pointer; }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  #annotate button { background: #fff; color: #1a1a1a; }
  pre#ceremony-log { background: #111; color: #d7ffd7; padding: 0.75rem; border-radius: 8px;
         font-size: 0.8rem; max-height: 8rem; overflow: auto; white-space: pre-wrap; }
  .banner { padding: 0.9rem; border-radius: 10px; margin-top: 0.5rem; font-size: 0.95rem; }
  .banner ul { margin: 0.4rem 0 0; padding-left: 1.2rem; }
  .banner.ok { background: #e3f7e6; border: 1px solid #38a169; }
  .banner.warn { background: #fff6e0; border: 1px solid #d9a406; }
  .banner.fail { background: #fde8e8; border: 1px solid #c0392b; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th, td { text-align: left; border-bottom: 1px solid #ddd; padding: 0.35rem 0.4rem; }
  textarea#report { width: 100%; box-sizing: border-box; font-family: ui-monospace, monospace;
         font-size: 0.8rem; }
</style>
</head>
<body>
  <h1>WebAuthn link-open check</h1>
  <p class="sub">PrivacyFence §10.6 &mdash; does a platform biometric prompt appear when this link is
  opened from inside a real Claude conversation? Full procedure:
  docs/webauthn-link-open-check.md.</p>

  <section id="support-banner"></section>

  <section>
    <label for="label">Label this device / app (e.g. "iPhone &middot; Claude iOS &middot; in-chat link")</label>
    <input id="label" placeholder="iPhone &middot; Claude iOS &middot; in-chat link">
    <div class="row">
      <button id="btn-register">1 &mdash; Register a passkey here</button>
      <button id="btn-login" disabled>2 &mdash; Verify with that passkey</button>
    </div>
    <pre id="ceremony-log" aria-live="polite"></pre>
  </section>

  <section id="verdict"></section>

  <section id="annotate" hidden>
    <p><strong>Did a Face ID / Touch ID / fingerprint / Windows Hello (or security-key) prompt
    actually appear on screen just now?</strong> This is the one thing only you can answer.</p>
    <div class="row">
      <button data-a="yes">Yes, it appeared</button>
      <button data-a="no">No prompt appeared</button>
      <button data-a="unsure">Unsure</button>
    </div>
  </section>

  <section>
    <h2 style="font-size:1.05rem">Session log</h2>
    <div style="overflow-x:auto"><table id="log-table"></table></div>

    <h2 style="font-size:1.05rem">Report (paste into your notes / the PR / the tracking issue)</h2>
    <textarea id="report" readonly rows="8"></textarea>
    <div class="row">
      <button id="btn-copy">Copy report</button>
      <button id="btn-reset">Reset all test data on this server</button>
    </div>
  </section>

<script>
function b64uToBuf(s) {
  s = s.replace(/-/g, '+').replace(/_/g, '/');
  const pad = s.length % 4 === 0 ? '' : '='.repeat(4 - (s.length % 4));
  const bin = atob(s + pad);
  const buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  return buf.buffer;
}
function bufToB64u(buf) {
  const bytes = new Uint8Array(buf);
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '');
}
function decodeCreationOptions(o) {
  o.challenge = b64uToBuf(o.challenge);
  o.user.id = b64uToBuf(o.user.id);
  if (o.excludeCredentials) o.excludeCredentials = o.excludeCredentials.map(c => Object.assign({}, c, {id: b64uToBuf(c.id)}));
  return o;
}
function decodeRequestOptions(o) {
  o.challenge = b64uToBuf(o.challenge);
  if (o.allowCredentials) o.allowCredentials = o.allowCredentials.map(c => Object.assign({}, c, {id: b64uToBuf(c.id)}));
  return o;
}
function encodeCredentialForServer(cred) {
  const out = {
    id: cred.id,
    rawId: bufToB64u(cred.rawId),
    type: cred.type,
    clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
    authenticatorAttachment: cred.authenticatorAttachment || null,
    response: { clientDataJSON: bufToB64u(cred.response.clientDataJSON) },
  };
  if (cred.response.attestationObject) {
    out.response.attestationObject = bufToB64u(cred.response.attestationObject);
    if (cred.response.getTransports) out.response.transports = cred.response.getTransports();
  }
  if (cred.response.authenticatorData) {
    out.response.authenticatorData = bufToB64u(cred.response.authenticatorData);
    out.response.signature = bufToB64u(cred.response.signature);
    if (cred.response.userHandle && cred.response.userHandle.byteLength) {
      out.response.userHandle = bufToB64u(cred.response.userHandle);
    }
  }
  return out;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
async function postJSON(url, body) {
  const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
  return data;
}
function log(line) {
  const pre = document.getElementById('ceremony-log');
  pre.textContent += line + '\\n';
  pre.scrollTop = pre.scrollHeight;
}

async function checkSupport() {
  const el = document.getElementById('support-banner');
  if (!window.PublicKeyCredential) {
    el.innerHTML = '<div class="banner fail">&#10060; <code>navigator.credentials</code> / WebAuthn is not available in this browser at all. ' +
      'If you reached this page by tapping the link inside a Claude conversation, this is very likely a bare embedded WebView with no ' +
      'platform-authenticator UI &mdash; exactly the failure mode &sect;10.6 is checking for. Try the same link in the system browser to compare.</div>';
    document.getElementById('btn-register').disabled = true;
    return;
  }
  let platformAvailable = 'unknown';
  try { platformAvailable = await PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable(); } catch (e) {}
  el.innerHTML = '<div class="banner ok">&#9989; WebAuthn API is present. Platform authenticator reported available: <b>' +
    platformAvailable + '</b>. This alone does not confirm a prompt will appear here &mdash; proceed to step 1.</div>';
}
checkSupport();

document.getElementById('btn-register').onclick = async () => {
  const label = document.getElementById('label').value || 'unlabeled device';
  try {
    log('Requesting registration options...');
    const opts = await postJSON('/api/register/options', {label});
    log('Calling navigator.credentials.create() -- watch the device now.');
    const cred = await navigator.credentials.create({publicKey: decodeCreationOptions(opts.publicKey)});
    log('Browser returned a credential. Verifying with the server...');
    const result = await postJSON('/api/register/verify', {state: opts.state, label, credential: encodeCredentialForServer(cred)});
    log('Server verified the registration.');
    renderVerdict('register', result);
    const loginBtn = document.getElementById('btn-login');
    loginBtn.disabled = false;
    loginBtn.dataset.credentialId = result.credential_id;
    promptAnnotation(result.log_id);
  } catch (e) {
    log('FAILED: ' + e.message);
    renderVerdict('register', {error: e.message});
  }
  refresh();
};

document.getElementById('btn-login').onclick = async () => {
  const credentialId = document.getElementById('btn-login').dataset.credentialId;
  try {
    log('Requesting authentication options...');
    const opts = await postJSON('/api/login/options', {credential_id: credentialId});
    log('Calling navigator.credentials.get() -- watch the device now.');
    const cred = await navigator.credentials.get({publicKey: decodeRequestOptions(opts.publicKey)});
    log('Browser returned an assertion. Verifying with the server...');
    const result = await postJSON('/api/login/verify', {state: opts.state, credential: encodeCredentialForServer(cred)});
    log('Server verified the assertion.');
    renderVerdict('login', result);
    promptAnnotation(result.log_id);
  } catch (e) {
    log('FAILED: ' + e.message);
    renderVerdict('login', {error: e.message});
  }
  refresh();
};

function renderVerdict(kind, result) {
  const el = document.getElementById('verdict');
  if (result.error) {
    el.innerHTML = '<div class="banner fail">&#10060; ' + kind + ' failed: ' + escapeHtml(result.error) + '</div>';
    return;
  }
  const uvOk = result.user_verified;
  const platformOk = result.attachment === 'platform';
  const cls = (uvOk && platformOk) ? 'ok' : 'warn';
  el.innerHTML = '<div class="banner ' + cls + '">' + (uvOk && platformOk ? '&#9989;' : '&#9888;&#65039;') +
    ' Server-verified facts for this ' + kind + ':<ul>' +
    '<li>userVerified (UV flag): <b>' + uvOk + '</b>' + (uvOk ? '' : ' &mdash; the OS did NOT report a real biometric/PIN check') + '</li>' +
    '<li>authenticatorAttachment: <b>' + escapeHtml(result.attachment) + '</b>' + (platformOk ? '' : ' &mdash; not a platform authenticator') + '</li>' +
    '<li>credential backed up / synced (BE/BS): <b>' + result.backed_up + '</b>' +
      (result.backed_up ? ' &mdash; a synced passkey, see &sect;10.6 &ldquo;Synced passkeys weaken this device&rdquo;' : '') + '</li>' +
    '<li>credential device type: <b>' + escapeHtml(result.device_type) + '</b></li>' +
    '</ul>These three facts are as far as this page can see on its own. Answer below for the one thing only you can confirm.</div>';
}

let pendingLogId = null;
function promptAnnotation(logId) {
  pendingLogId = logId;
  document.getElementById('annotate').hidden = false;
}
document.querySelectorAll('#annotate button').forEach(b => b.onclick = async () => {
  await postJSON('/api/log/annotate', {log_id: pendingLogId, saw_prompt: b.dataset.a});
  document.getElementById('annotate').hidden = true;
  refresh();
});

document.getElementById('btn-reset').onclick = async () => {
  if (!confirm('Clear all registered credentials and the session log on this server?')) return;
  await postJSON('/api/reset', {});
  document.getElementById('btn-login').disabled = true;
  document.getElementById('ceremony-log').textContent = '';
  document.getElementById('verdict').innerHTML = '';
  refresh();
};

document.getElementById('btn-copy').onclick = async () => {
  const ta = document.getElementById('report');
  try { await navigator.clipboard.writeText(ta.value); }
  catch (e) { ta.focus(); ta.select(); }
};

async function refresh() {
  const text = await (await fetch('/api/report')).text();
  document.getElementById('report').value = text;
  const s = await (await fetch('/api/state')).json();
  const tbl = document.getElementById('log-table');
  tbl.innerHTML = '<tr><th>Time</th><th>Device</th><th>Action</th><th>UV</th><th>Attachment</th><th>Backed up</th><th>Saw prompt?</th></tr>' +
    s.log.map(e => '<tr><td>' + e.time + '</td><td>' + escapeHtml(e.label) + '</td><td>' + e.action + '</td><td>' +
      e.user_verified + '</td><td>' + escapeHtml(e.attachment) + '</td><td>' + e.backed_up + '</td><td>' +
      (e.saw_prompt || '&mdash;') + '</td></tr>').join('');
  // The credential the server already has is what matters, not whether *this* page load is the
  // one that registered it -- some in-app browsers reload/rebuild the tab around the native
  // biometric sheet, wiping the "just registered" in-memory flag the click handler below sets.
  // Re-derive the button's enabled state from server truth on every load/refresh so that reload
  // doesn't strand the tester on a permanently grayed-out "Verify" button.
  const loginBtn = document.getElementById('btn-login');
  if (s.credentials.length > 0) {
    loginBtn.disabled = false;
    const labelField = document.getElementById('label');
    if (!labelField.value) labelField.value = s.credentials[s.credentials.length - 1].label;
  }
}
refresh();
</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address (default: 127.0.0.1 -- leave this alone and put a tunnel in front of it, "
        "don't expose it directly; see docs/webauthn-link-open-check.md)",
    )
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Serving the WebAuthn link-open check on http://{args.host}:{args.port}")
    print("Put a tunnel in front of this (see docs/webauthn-link-open-check.md) -- WebAuthn needs a")
    print("secure-context HTTPS origin with a real hostname; localhost/plain HTTP won't work from a")
    print("phone. Then paste the tunnel's https:// URL into a real Claude conversation, tap it there,")
    print("and watch the device. Ctrl+C to stop; nothing here persists past that.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
