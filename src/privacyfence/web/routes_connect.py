"""Per-user service authorization on the web (P8, docs/https-connector-
refactor-plan.md §9.3) -- the org-mode surface that finally makes P6/P7's
per-principal machinery reach somewhere: ``GET /connect`` lets a signed-in
principal see which of Google/Slack/Salesforce/Atlassian/Telegram they've
authorized and connect the rest, and ``GET /oauth/start/{service}``/
``GET /oauth/callback/{service}`` are the server-side redirect endpoints
that make it possible from a phone with no desktop in the loop --
``oauth_loopback.py``'s local ``webbrowser.open()``/loopback-listener flow
only ever worked when the browser and the daemon were the same machine.

**Not** a port of settings_window_html.py/routes_settings.py's ~30-action
surface into org mode -- that's real, separate follow-up work (see web/
server.py's own module docstring, and the "Deliberately out of scope for
P7" paragraph at the top of the plan document): this page does exactly one
thing, authorizing a principal's own connectors, using org mode's session
cookie (``pf_org_session``) for its own small CSRF model (``org_session.
check_csrf``/``check_origin``) rather than reusing local mode's shared-
secret-token one.

**Google** gets five separate authorize buttons (gmail/drive/calendar/
contacts/tasks), not one "Connect Google" -- see settings_controller.py's
own ``GOOGLE_CONNECTORS``/``_GOOGLE_CLIENTS``, which already draws this
same distinction for local mode's own menu-bar flow: each is a distinct
OAuth grant with its own scopes and its own token file.

**The one load-bearing subtlety this module exists to get right**: the
``pf_org_session`` cookie is ``SameSite=Strict`` (org_session.py's own
``set_session_cookie``), so it is *not* sent on the browser's GET that
lands back on ``/oauth/callback/{service}`` after a redirect from Google/
Slack/Salesforce/Atlassian -- that request is a cross-site-initiated
top-level navigation from the provider's own domain, exactly the case
``SameSite=Strict`` is designed to omit the cookie on. The callback route
therefore never reads ``current_principal()``/the session cookie; it
resolves the principal entirely from the single-use ``state`` value
``_PendingAuthStore`` recorded at ``/oauth/start/{service}`` time -- where
the cookie *is* present, since that request is a same-site navigation the
user's own click on ``/connect`` made. This mirrors how ``routes_org_
identity.py``'s own IdP callback needs no session check (there's no
session yet at that point); this is the first flow in this codebase where
an *already-authenticated* session has to survive a real third-party
redirect round trip.

**Atlassian's multi-site accounts** are handled with one deliberate
simplification versus local mode: if the signed-in account can reach more
than one Atlassian site, the first one returned is used automatically
rather than prompting for a choice (local mode's own native/web picker has
nowhere to block inside a one-shot HTTP callback -- see atlassian_oauth.
resolve_resource_and_save's own ``pick_resource`` parameter). Anyone who
needs a different site can still get one via local mode's menu bar, or by
disconnecting and asking IT to scope the account down to one site. Worth
flagging in review, not hidden in a comment only.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field, replace as _dc_replace
from html import escape as _esc
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from .. import atlassian_oauth, google_oauth, org_identity, paths, salesforce_client, slack_client, telegram_auth
from ..app_credentials import telegram_app_credentials
from ..calendar_client import SCOPES as _CALENDAR_SCOPES
from ..connector_registry import ConnectorRegistry
from ..contacts_client import SCOPES as _CONTACTS_SCOPES
from ..drive_client import SCOPES as _DRIVE_SCOPES
from ..gmail_client import SCOPES as _GMAIL_SCOPES
from ..principal import Principal, principal_scope
from ..tasks_client import SCOPES as _TASKS_SCOPES
from . import org_session
from .org_session import OrgSessionStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------- #
# Service registry
# ---------------------------------------------------------------------------- #

GOOGLE_SCOPES: dict[str, list[str]] = {
    "gmail": _GMAIL_SCOPES, "drive": _DRIVE_SCOPES, "calendar": _CALENDAR_SCOPES,
    "contacts": _CONTACTS_SCOPES, "tasks": _TASKS_SCOPES,
}
GOOGLE_SERVICES = frozenset(GOOGLE_SCOPES)
ATLASSIAN_SERVICES = frozenset({"jira", "confluence"})
OAUTH_SERVICES = GOOGLE_SERVICES | ATLASSIAN_SERVICES | frozenset({"slack", "salesforce"})

# service -> daemon_main.TOKEN_FILES key (jira/confluence share one Atlassian grant).
_TOKEN_FILE_KEY: dict[str, str] = {s: s for s in GOOGLE_SERVICES}
_TOKEN_FILE_KEY.update({"slack": "slack", "salesforce": "salesforce", "jira": "atlassian", "confluence": "atlassian"})

SERVICE_LABELS: dict[str, str] = {
    "gmail": "Gmail", "drive": "Drive", "calendar": "Calendar", "contacts": "Contacts", "tasks": "Tasks",
    "slack": "Slack", "salesforce": "Salesforce", "jira": "Jira", "confluence": "Confluence", "telegram": "Telegram",
}

# service -> org_config.json section name.
_ORG_CONFIG_SECTION: dict[str, str] = {s: "google" for s in GOOGLE_SERVICES}
_ORG_CONFIG_SECTION.update({"slack": "slack", "salesforce": "salesforce", "jira": "atlassian", "confluence": "atlassian"})


def _token_files() -> dict[str, str]:
    from ..daemon_main import TOKEN_FILES  # lazy: daemon_main.py is the top of this codebase's import graph
    return TOKEN_FILES


def _token_file_path(principal: Principal, service: str) -> str:
    return str(paths.user_dir(principal) / _token_files()[_TOKEN_FILE_KEY[service]])


def _is_connected(principal: Principal, service: str) -> bool:
    if service == "telegram":
        session_file = str(paths.user_dir(principal) / _token_files()["telegram"])
        return os.path.exists(session_file) or os.path.exists(session_file + ".session")
    return os.path.exists(_token_file_path(principal, service))


def _is_configured(org_config: dict[str, Any], service: str) -> bool:
    if service == "telegram":
        return telegram_app_credentials() is not None
    section = org_config.get(_ORG_CONFIG_SECTION[service]) or {}
    if service in GOOGLE_SERVICES:
        return bool(google_oauth.web_client_config(section))
    if service == "slack":
        return bool(section.get("client_id"))
    if service == "salesforce":
        return bool(section.get("consumer_key"))
    return bool(section.get("client_id"))  # jira/confluence -> atlassian


# ---------------------------------------------------------------------------- #
# In-memory, short-lived flow state -- same "a daemon restart invalidates
# it, and that's fine" posture as routes_org_identity.py's own
# _LoginAttemptStore/org_session.OrgSessionStore.
# ---------------------------------------------------------------------------- #

_PENDING_TTL_SECONDS = 10 * 60


@dataclass
class _PendingAuth:
    principal_id: str
    service: str
    code_verifier: str = ""
    created_at: float = field(default_factory=time.time)


class _PendingAuthStore:
    """State -> principal/service/PKCE-verifier, single-use. See module
    docstring's own note on why the callback route trusts *this*, not the
    session cookie, to know who is signing in."""

    def __init__(self, ttl: float = _PENDING_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._pending: dict[str, _PendingAuth] = {}

    def put(self, *, state: str, principal_id: str, service: str, code_verifier: str = "") -> None:
        self._prune()
        with self._lock:
            self._pending[state] = _PendingAuth(principal_id=principal_id, service=service, code_verifier=code_verifier)

    def pop(self, state: str) -> _PendingAuth | None:
        with self._lock:
            return self._pending.pop(state, None)

    def _prune(self) -> None:
        now = time.time()
        with self._lock:
            stale = [s for s, p in self._pending.items() if (now - p.created_at) > self._ttl]
            for s in stale:
                del self._pending[s]


@dataclass
class _TelegramState:
    step: str | None = None  # None | "phone" | "code" | "password"
    phone: str = ""
    phone_code_hash: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)


class _TelegramAuthStore:
    """Principal-keyed, one in-progress sign-in attempt at a time per
    principal -- the org-mode counterpart of settings_controller.py's own
    single ``self._telegram_auth`` slot, which is correct only for local
    mode's one user."""

    def __init__(self, ttl: float = _PENDING_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._states: dict[str, _TelegramState] = {}

    def get(self, principal_id: str) -> _TelegramState:
        with self._lock:
            state = self._states.get(principal_id)
            if state is None or (time.time() - state.created_at) > self._ttl:
                state = _TelegramState()
                self._states[principal_id] = state
            return state

    def set(self, principal_id: str, state: _TelegramState) -> None:
        with self._lock:
            self._states[principal_id] = state

    def clear(self, principal_id: str) -> None:
        with self._lock:
            self._states.pop(principal_id, None)


# ---------------------------------------------------------------------------- #
# Provider dispatch: build the authorize URL (start) / exchange the code and
# save a token (callback), per service. Each of these delegates to the
# hoisted, provider-specific functions in slack_client.py/salesforce_client.
# py/atlassian_oauth.py/google_oauth.py -- see those modules for the actual
# HTTP/PKCE mechanics.
# ---------------------------------------------------------------------------- #

class _NotConfigured(Exception):
    """Raised when org_config.json has no (usable) section for a service."""


def _build_authorize_url(service: str, org_config: dict[str, Any], redirect_uri: str, state: str) -> tuple[str, str]:
    """Returns ``(authorize_url, code_verifier)`` -- ``code_verifier`` is
    ``""`` for Slack (no PKCE, see slack_client.build_authorize_url's own
    docstring). Raises ``_NotConfigured`` if org_config.json has no usable
    section for ``service``."""
    if service in GOOGLE_SERVICES:
        client_config = google_oauth.web_client_config(org_config.get("google") or {})
        if not client_config:
            raise _NotConfigured(service)
        return google_oauth.authorize_url(client_config, GOOGLE_SCOPES[service], redirect_uri, state)

    if service == "slack":
        slack_org = org_config.get("slack") or {}
        if not slack_org.get("client_id"):
            raise _NotConfigured(service)
        return slack_client.build_authorize_url(slack_org["client_id"], redirect_uri, state), ""

    if service == "salesforce":
        sf_org = org_config.get("salesforce") or {}
        if not sf_org.get("consumer_key"):
            raise _NotConfigured(service)
        verifier, challenge = org_identity.generate_pkce_pair()
        url = salesforce_client.build_authorize_url(
            sf_org["consumer_key"], redirect_uri, state, challenge,
            sf_org.get("login_url", salesforce_client.DEFAULT_LOGIN_URL),
        )
        return url, verifier

    if service in ATLASSIAN_SERVICES:
        atlassian_org = org_config.get("atlassian") or {}
        if not atlassian_org.get("client_id"):
            raise _NotConfigured(service)
        verifier, challenge = org_identity.generate_pkce_pair()
        return atlassian_oauth.build_authorize_url(atlassian_org["client_id"], redirect_uri, state, challenge), verifier

    raise _NotConfigured(service)


def _first_resource(resources: list[dict[str, Any]]) -> dict[str, Any]:
    # See module docstring's own note on this simplification.
    return resources[0]


def _exchange_and_save(
    service: str, org_config: dict[str, Any], redirect_uri: str, code: str, code_verifier: str, principal: Principal,
) -> None:
    token_file = _token_file_path(principal, service)

    if service in GOOGLE_SERVICES:
        client_config = google_oauth.web_client_config(org_config.get("google") or {})
        creds = google_oauth.exchange_code(client_config, GOOGLE_SCOPES[service], redirect_uri, code, code_verifier)
        google_oauth.save_credentials(token_file, creds)
        return

    if service == "slack":
        slack_org = org_config.get("slack") or {}
        token_record = slack_client.exchange_code(slack_org["client_id"], slack_org.get("client_secret", ""), code, redirect_uri)
        slack_client.save_token_record(token_file, token_record)
        return

    if service == "salesforce":
        sf_org = org_config.get("salesforce") or {}
        token_record = salesforce_client.exchange_code(
            sf_org["consumer_key"], sf_org.get("consumer_secret", ""), code, redirect_uri, code_verifier,
            sf_org.get("login_url", salesforce_client.DEFAULT_LOGIN_URL),
        )
        salesforce_client.save_token_file(token_file, token_record)
        return

    if service in ATLASSIAN_SERVICES:
        atlassian_org = org_config.get("atlassian") or {}
        response = atlassian_oauth.exchange_code(
            atlassian_org["client_id"], atlassian_org.get("client_secret", ""), code, redirect_uri, code_verifier,
        )
        atlassian_oauth.resolve_resource_and_save(
            token_file, response["access_token"], response.get("refresh_token", ""), _first_resource,
        )
        return

    raise _NotConfigured(service)


# ---------------------------------------------------------------------------- #
# Routes
# ---------------------------------------------------------------------------- #

def build_routes(
    *, sessions: OrgSessionStore, connector_registry: ConnectorRegistry, org_config: dict[str, Any], issuer_url: str,
) -> list[Route]:
    attempts = _PendingAuthStore()
    telegram_states = _TelegramAuthStore()
    base_url = issuer_url.rstrip("/")

    def _current_principal(request: Request) -> Principal | None:
        return org_session.authenticated(request, sessions)

    async def start(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return RedirectResponse("/login?next=/connect", status_code=302, headers={"Cache-Control": "no-store"})
        service = request.path_params["service"]
        if service not in OAUTH_SERVICES:
            return PlainTextResponse("Unknown service.", status_code=404)

        redirect_uri = f"{base_url}/oauth/callback/{service}"
        state = secrets.token_urlsafe(32)
        try:
            authorize_url, code_verifier = _build_authorize_url(service, org_config, redirect_uri, state)
        except _NotConfigured:
            return RedirectResponse(f"/connect?error={service}", status_code=302, headers={"Cache-Control": "no-store"})
        attempts.put(state=state, principal_id=principal.id, service=service, code_verifier=code_verifier)
        return RedirectResponse(authorize_url, status_code=302, headers={"Cache-Control": "no-store"})

    async def callback(request: Request) -> Response:
        # Deliberately does not read org_session.authenticated()/the
        # session cookie -- see module docstring's own note on why it
        # can't be relied on here. Trust is rooted entirely in the
        # single-use `state` value, popped exactly once.
        service = request.path_params["service"]
        provider_error = request.query_params.get("error")
        state = request.query_params.get("state", "")
        code = request.query_params.get("code", "")
        pending = attempts.pop(state) if state else None
        if pending is None or pending.service != service:
            return PlainTextResponse("Invalid or expired sign-in attempt. Return to /connect and try again.", status_code=400)
        if provider_error or not code:
            return RedirectResponse(f"/connect?error={service}", status_code=302, headers={"Cache-Control": "no-store"})

        principal = Principal(id=pending.principal_id)
        redirect_uri = f"{base_url}/oauth/callback/{service}"
        try:
            with principal_scope(principal):
                _exchange_and_save(service, org_config, redirect_uri, code, pending.code_verifier, principal)
        except Exception as exc:  # noqa: BLE001 -- any provider/exchange failure ends the same way
            logger.warning("Service authorization failed for %s (%s): %s", principal.id, service, exc)
            return RedirectResponse(f"/connect?error={service}", status_code=302, headers={"Cache-Control": "no-store"})

        connector_registry.evict(principal.id)
        return RedirectResponse(f"/connect?connected={service}", status_code=302, headers={"Cache-Control": "no-store"})

    async def connect_page(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return RedirectResponse("/login?next=/connect", status_code=302, headers={"Cache-Control": "no-store"})
        session_id = request.cookies.get(org_session.SESSION_COOKIE, "")
        telegram_state = telegram_states.get(principal.id)
        html = _render_connect_page(
            principal=principal, org_config=org_config, telegram_state=telegram_state,
            flash_connected=request.query_params.get("connected", ""),
            flash_error=request.query_params.get("error", ""),
            csrf=session_id,
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    def _check_telegram_post(request: Request, form) -> Response | None:
        if not org_session.check_csrf(request, form.get("csrf")):
            return PlainTextResponse("Unauthorized.", status_code=401)
        if not org_session.check_origin(request):
            return PlainTextResponse("Cross-origin request rejected.", status_code=403)
        return None

    async def telegram_start(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return RedirectResponse("/login?next=/connect", status_code=302, headers={"Cache-Control": "no-store"})
        form = await request.form()
        rejected = _check_telegram_post(request, form)
        if rejected is not None:
            return rejected

        creds = telegram_app_credentials()
        phone = str(form.get("phone", "")).strip()
        if creds is None:
            telegram_states.set(principal.id, _TelegramState(step="phone", error="Telegram isn't available on this install."))
        elif not phone:
            telegram_states.set(principal.id, _TelegramState(step="phone", error="Enter a phone number."))
        else:
            api_id, api_hash = creds
            session_file = str(paths.user_dir(principal) / _token_files()["telegram"])
            try:
                phone_code_hash = await telegram_auth.send_code(phone, session_file, api_id, api_hash)
            except Exception as exc:  # noqa: BLE001 -- provider-side failure, shown to the user
                telegram_states.set(principal.id, _TelegramState(step="phone", error=str(exc)))
            else:
                telegram_states.set(
                    principal.id, _TelegramState(step="code", phone=phone, phone_code_hash=phone_code_hash),
                )
        return RedirectResponse("/connect", status_code=303, headers={"Cache-Control": "no-store"})

    async def telegram_code(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return RedirectResponse("/login?next=/connect", status_code=302, headers={"Cache-Control": "no-store"})
        form = await request.form()
        rejected = _check_telegram_post(request, form)
        if rejected is not None:
            return rejected

        state = telegram_states.get(principal.id)
        code = str(form.get("code", "")).strip()
        if state.step != "code":
            return RedirectResponse("/connect", status_code=303, headers={"Cache-Control": "no-store"})
        if not code:
            telegram_states.set(principal.id, _dc_replace(state, error="Enter the verification code."))
            return RedirectResponse("/connect", status_code=303, headers={"Cache-Control": "no-store"})

        creds = telegram_app_credentials()
        if creds is None:
            telegram_states.set(principal.id, _dc_replace(state, error="Telegram isn't available on this install."))
            return RedirectResponse("/connect", status_code=303, headers={"Cache-Control": "no-store"})
        api_id, api_hash = creds
        session_file = str(paths.user_dir(principal) / _token_files()["telegram"])
        try:
            result = await telegram_auth.sign_in(state.phone, code, state.phone_code_hash, session_file, api_id, api_hash)
        except Exception as exc:  # noqa: BLE001 -- provider-side failure, shown to the user
            telegram_states.set(principal.id, _TelegramState(step="code", phone=state.phone, phone_code_hash=state.phone_code_hash, error=str(exc)))
        else:
            if result == telegram_auth.NEEDS_2FA:
                telegram_states.set(principal.id, _TelegramState(step="password", phone=state.phone))
            else:
                telegram_states.clear(principal.id)
                connector_registry.evict(principal.id)
        return RedirectResponse("/connect", status_code=303, headers={"Cache-Control": "no-store"})

    async def telegram_2fa(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return RedirectResponse("/login?next=/connect", status_code=302, headers={"Cache-Control": "no-store"})
        form = await request.form()
        rejected = _check_telegram_post(request, form)
        if rejected is not None:
            return rejected

        state = telegram_states.get(principal.id)
        password = str(form.get("password", "")).strip()
        if state.step != "password":
            return RedirectResponse("/connect", status_code=303, headers={"Cache-Control": "no-store"})
        if not password:
            telegram_states.set(principal.id, _TelegramState(step="password", phone=state.phone, error="Enter your two-step verification password."))
            return RedirectResponse("/connect", status_code=303, headers={"Cache-Control": "no-store"})

        creds = telegram_app_credentials()
        if creds is None:
            telegram_states.set(principal.id, _dc_replace(state, error="Telegram isn't available on this install."))
            return RedirectResponse("/connect", status_code=303, headers={"Cache-Control": "no-store"})
        api_id, api_hash = creds
        session_file = str(paths.user_dir(principal) / _token_files()["telegram"])
        try:
            await telegram_auth.sign_in_2fa(password, session_file, api_id, api_hash)
        except Exception as exc:  # noqa: BLE001
            telegram_states.set(principal.id, _TelegramState(step="password", phone=state.phone, error=str(exc)))
        else:
            telegram_states.clear(principal.id)
            connector_registry.evict(principal.id)
        return RedirectResponse("/connect", status_code=303, headers={"Cache-Control": "no-store"})

    async def telegram_cancel(request: Request) -> Response:
        principal = _current_principal(request)
        if principal is None:
            return RedirectResponse("/login?next=/connect", status_code=302, headers={"Cache-Control": "no-store"})
        form = await request.form()
        rejected = _check_telegram_post(request, form)
        if rejected is not None:
            return rejected
        telegram_states.clear(principal.id)
        return RedirectResponse("/connect", status_code=303, headers={"Cache-Control": "no-store"})

    return [
        Route("/oauth/start/{service}", start),
        Route("/oauth/callback/{service}", callback),
        Route("/connect", connect_page),
        Route("/connect/telegram/start", telegram_start, methods=["POST"]),
        Route("/connect/telegram/code", telegram_code, methods=["POST"]),
        Route("/connect/telegram/2fa", telegram_2fa, methods=["POST"]),
        Route("/connect/telegram/cancel", telegram_cancel, methods=["POST"]),
    ]


# ---------------------------------------------------------------------------- #
# Page rendering -- a small, self-contained document, deliberately not
# web_shell.wrap()'d (see module docstring: that shell's live indicator
# needs GET /api/state/stream, which org mode doesn't mount) and not built
# from settings_window_html.py (which is a pure function of SettingsController.
# snapshot()'s whole ~30-action surface, not a per-service connect list).
# ---------------------------------------------------------------------------- #

_STYLE = """
body{font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:640px;margin:0 auto;
  padding:32px 20px 64px;color:#1b1b1f;background:#fff}
h1{font-size:20px;margin:0 0 4px}
p.lead{color:#555;margin-top:0}
.flash{border-radius:8px;padding:10px 14px;margin:16px 0;font-size:14px}
.flash.ok{background:#e6f4ea;color:#1e7e34}
.flash.err{background:#fdecea;color:#a02a2a}
ul.services{list-style:none;padding:0;margin:24px 0}
li.service{display:flex;align-items:center;justify-content:space-between;padding:12px 0;border-bottom:1px solid #eee}
li.service:last-child{border-bottom:none}
.name{font-weight:600}
.badge{font-size:12px;padding:2px 8px;border-radius:999px;margin-left:8px}
.badge.connected{background:#e6f4ea;color:#1e7e34}
.badge.not-configured{background:#f1f1f3;color:#888}
a.connect-link{color:#fff;background:#2451c9;padding:6px 14px;border-radius:6px;text-decoration:none;font-size:14px}
a.connect-link.reconnect{background:#555}
.telegram-box{margin-top:8px;padding:14px;border:1px solid #eee;border-radius:8px}
.telegram-box input[type=text],.telegram-box input[type=password]{width:100%;box-sizing:border-box;padding:8px;
  margin:6px 0;border:1px solid #ccc;border-radius:6px;font-size:14px}
.telegram-box button{padding:8px 16px;border:none;border-radius:6px;background:#2451c9;color:#fff;font-size:14px}
.telegram-box .cancel{background:none;color:#888;text-decoration:underline;border:none;padding:0;margin-left:10px;
  font-size:13px;cursor:pointer}
.telegram-box .error{color:#a02a2a;font-size:13px;margin:4px 0}
"""


def _flash_html(flash_connected: str, flash_error: str) -> str:
    parts = []
    if flash_connected and flash_connected in SERVICE_LABELS:
        parts.append(f'<div class="flash ok">{_esc(SERVICE_LABELS[flash_connected])} connected.</div>')
    if flash_error and flash_error in SERVICE_LABELS:
        parts.append(
            f'<div class="flash err">Could not connect {_esc(SERVICE_LABELS[flash_error])} -- '
            "either sign-in was declined, or your organization hasn't configured it yet.</div>"
        )
    return "".join(parts)


def _service_row_html(principal: Principal, org_config: dict[str, Any], service: str) -> str:
    label = SERVICE_LABELS[service]
    connected = _is_connected(principal, service)
    configured = _is_configured(org_config, service)
    if not configured:
        badge = '<span class="badge not-configured">Not set up by your organization</span>'
        action = ""
    elif connected:
        badge = '<span class="badge connected">Connected</span>'
        action = f'<a class="connect-link reconnect" href="/oauth/start/{service}">Reconnect</a>'
    else:
        badge = ""
        action = f'<a class="connect-link" href="/oauth/start/{service}">Connect</a>'
    return f'<li class="service"><span><span class="name">{_esc(label)}</span>{badge}</span>{action}</li>'


def _telegram_box_html(principal: Principal, org_config: dict[str, Any], telegram_state: _TelegramState, csrf: str) -> str:
    configured = _is_configured(org_config, "telegram")
    connected = _is_connected(principal, "telegram")
    if not configured:
        return _service_row_html(principal, org_config, "telegram")

    header_badge = '<span class="badge connected">Connected</span>' if connected else ""
    error_html = f'<div class="error">{_esc(telegram_state.error)}</div>' if telegram_state.error else ""
    csrf_field = f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'

    if telegram_state.step == "code":
        body = (
            f"{error_html}"
            f'<form method="post" action="/connect/telegram/code">{csrf_field}'
            '<input type="text" name="code" placeholder="Verification code" autocomplete="one-time-code" required>'
            '<button type="submit">Confirm code</button></form>'
            f'<form method="post" action="/connect/telegram/cancel" style="display:inline">{csrf_field}'
            '<button type="submit" class="cancel">Cancel</button></form>'
        )
    elif telegram_state.step == "password":
        body = (
            f"{error_html}"
            f'<form method="post" action="/connect/telegram/2fa">{csrf_field}'
            '<input type="password" name="password" placeholder="Two-step verification password" required>'
            '<button type="submit">Confirm</button></form>'
            f'<form method="post" action="/connect/telegram/cancel" style="display:inline">{csrf_field}'
            '<button type="submit" class="cancel">Cancel</button></form>'
        )
    else:
        body = (
            f"{error_html}"
            f'<form method="post" action="/connect/telegram/start">{csrf_field}'
            '<input type="text" name="phone" placeholder="+1 555 0100" autocomplete="tel" required>'
            f'<button type="submit">{"Reconnect" if connected else "Connect"} Telegram</button></form>'
        )

    return (
        f'<li class="service"><div style="width:100%">'
        f'<div style="display:flex;justify-content:space-between;align-items:center">'
        f'<span class="name">Telegram</span>{header_badge}</div>'
        f'<div class="telegram-box">{body}</div></div></li>'
    )


def _render_connect_page(
    *, principal: Principal, org_config: dict[str, Any], telegram_state: _TelegramState,
    flash_connected: str, flash_error: str, csrf: str,
) -> str:
    google_rows = "".join(_service_row_html(principal, org_config, s) for s in ("gmail", "drive", "calendar", "contacts", "tasks"))
    other_rows = "".join(_service_row_html(principal, org_config, s) for s in ("slack", "salesforce", "jira", "confluence"))
    telegram_row = _telegram_box_html(principal, org_config, telegram_state, csrf)
    who = _esc(principal.email or principal.display_name or principal.id)

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PrivacyFence -- Connect your accounts</title>
<style>{_STYLE}</style></head>
<body>
<h1>Connect your accounts</h1>
<p class="lead">Signed in as {who}. Connecting a service lets PrivacyFence act on it for you, still gated by
the same approval rules as everything else.</p>
{_flash_html(flash_connected, flash_error)}
<ul class="services">{google_rows}{other_rows}{telegram_row}</ul>
<form method="post" action="/logout"><button type="submit" class="cancel" style="cursor:pointer">Sign out</button></form>
</body></html>"""


__all__ = [
    "ATLASSIAN_SERVICES",
    "GOOGLE_SCOPES",
    "GOOGLE_SERVICES",
    "OAUTH_SERVICES",
    "SERVICE_LABELS",
    "build_routes",
]
