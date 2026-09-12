"""Embedded HTTP(S) server lifecycle: bind policy, security headers, and
starting/stopping the ASGI app (uvicorn) on its own thread -- see daemon_
main.py's own module docstring's "Threading model" section (its "Web
thread") for how this fits alongside the daemon's other threads. Before
P5 retired it, the bridge socket ran on its own dedicated thread the same
way (``IPCServerThread``, since deleted along with the rest of ``ipc_
server.py``).

**Local mode**: D1's decision applies -- loopback HTTP, bound to
``localhost`` (not a bare ``127.0.0.1``/``0.0.0.0``) so ``http://localhost``
stays a secure context for whatever this surface needs (WebAuthn, P9)
without anyone having to move the bind address. Auth is deliberately the
simplest thing that's still a real control, not sessions/OIDC -- but since
SEC-06 it is no
longer "possession of one never-expiring, URL-carried token is the
authority" the way it was through v4.0.0a12 (the same posture
``~/.privacyfence/ipc_token`` had for the bridge, before P5 retired both --
this surface, reachable from a browser rather than only a local process,
needed more). ``load_or_create_token()``'s random
secret is generated once, written 0600 under paths.data_dir(), rotated
whenever the installed version changes, and now used only to authorize
minting a bootstrap code on demand (``POST /api/bootstrap``, see
``_bootstrap_mint_route`` below) -- never sent to a browser or written to a
URL/log line itself. What a browser actually presents is a short-lived,
single-use bootstrap code (``?bootstrap=<code>``, see
``_BootstrapMiddleware``), exchanged exactly once for an independent,
random session id (web/session_auth.py's ``LocalSessionStore``) carrying
its own idle and absolute expiry -- web/routes_approvals.py's own docstring
covers the CSRF double-submit that session id also backs.

**Org mode** (P7): a
configurable bind host/port, optional TLS termination, optional
``X-Forwarded-*`` trust for a small explicit set of reverse-proxy
addresses (never by default), and ``/mcp`` authenticated by
``web/oauth_provider.py``'s real OAuth 2.1 authorization server instead of
one shared secret. Passed to ``build_app``/``WebServer`` as one ``OrgAuth``
bundle (see that class) rather than four separate parameters, so a caller
either opts into the whole org-mode picture or none of it.

**`/approvals` in org mode** (P9, web/routes_org_approvals.py) is *not*
``routes_approvals.create_app``'s local-mode surface -- that one still
authenticates with one shared secret and lists *every* pending approval
with no principal filtering, which is exactly why it was never mounted
here through P8 (exposing it as-is under org mode, where many principals
share one daemon, would leak every principal's pending approvals to
whoever holds any valid token). ``/approvals``/``/security`` here are a
separate, principal-aware route set: every read and write is authorized
against ``current_principal()`` via ``org_session``, and a *write*
decision additionally demands a fresh WebAuthn step-up when
``org_config.json``'s ``step_up.enabled`` is set (§10.6, D7) --
web/routes_org_approvals.py's own module docstring covers both.

**Still deliberately not mounted in org mode**: ``/settings``
(``routes_settings.py``'s ~30-action surface) -- generalizing its CSRF
model (today "the one shared token doubles as the session cookie value and
the per-page CSRF token") to org mode's per-session cookie is real, scoped
follow-up work no phase through P9 has needed yet (``/connect``, P8, and
now ``/approvals``/``/security``, P9, are each a small, purpose-built page
rather than a port of that surface -- see routes_connect.py's own module
docstring for why that shape was chosen over porting it).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

import uvicorn
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from .. import __version__, paths
from ..connector_registry import ConnectorRegistry
from ..org_identity import IdpConfig
from ..principal import ANONYMOUS_PRINCIPAL, LOCAL_PRINCIPAL, Principal, principal_scope
from ..secure_files import atomic_write_text
from ..settings_controller import SettingsController, set_main_dispatcher
from ..web_approval_ui import WebApprovalUI
from . import org_session
from . import routes_connect
from . import routes_downloads
from . import routes_org_identity
from . import state_stream as _state_stream
from .csp import build_csp
from .csp import new_nonce as _new_csp_nonce
from .mcp_auth import load_or_create_mcp_token
from .mcp_dispatch import McpDispatcher
from .oauth_provider import OrgOAuthProvider
from .org_session import OrgSessionStore
from .routes_approvals import create_app as create_approvals_app
from .routes_mcp import MCP_PATH, mcp_lifespan, mount_mcp, mount_org_oauth, protected_resource_metadata_url
from .routes_settings import build_routes as build_settings_routes
from .session_auth import BOOTSTRAP_QUERY_PARAM, BootstrapStore, LocalSessionStore
from .session_auth import authenticated as _session_authenticated
from .session_auth import set_session_cookie as _set_session_cookie
from .session_auth import unauthorized_html as _unauthorized_response
from .session_auth import verify_bearer_secret
from .state_stream import StateStream

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8765
TOKEN_FILE_NAME = "web_token"
# SEC-06: rotation-tracking sibling of TOKEN_FILE_NAME -- records which
# installed version last (re)generated it, so an upgrade rotates a token
# that was minted (and possibly already leaked, logged, or bookmarked
# from a URL) by an older, pre-SEC-06 build, rather than trusting it
# forever just because the file itself was never deleted. See
# load_or_create_token()'s own docstring.
TOKEN_VERSION_FILE_NAME = "web_token_version"
MCP_URL_FILE_NAME = "mcp_url"

# Content-Security-Policy: see web/csp.py's own module docstring for the
# full policy and the reasoning behind each directive (SEC-08 -- this
# replaced a blanket 'unsafe-inline' grant on both script-src and
# style-src, which is what
# this comment described through v4.0.0a12; that description had grown
# actively inaccurate, since the code below it granted exactly the
# "blanket 'unsafe-inline'" the comment said this policy avoided). Built
# per-response, from that request's own nonce (_SecurityHeadersMiddleware
# below), not a fixed module-level constant any more.

# SEC-18: every
# browser feature this app never uses, denied outright -- the same "narrow,
# explicit exceptions for exactly what these pages actually use" posture
# web/csp.py's build_csp() already takes. ``publickey-credentials-get``/
# ``-create`` are the one exception left at their browser default (``self``)
# rather than denied: web/routes_security.py's step-up flow calls
# ``navigator.credentials.get``/``.create`` from this same origin, and
# Permissions-Policy's own default allowlist for both directives is
# already ``self`` -- naming them explicitly here just documents that on
# purpose instead of leaving a future reader to wonder whether they were
# overlooked.
_PERMISSIONS_POLICY = (
    "accelerometer=(), autoplay=(), camera=(), display-capture=(), encrypted-media=(), "
    "fullscreen=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), "
    "midi=(), payment=(), picture-in-picture=(), publickey-credentials-create=(self), "
    "publickey-credentials-get=(self), screen-wake-lock=(), usb=(), xr-spatial-tracking=()"
)

# SEC-18: one year, subdomains included -- the usual conservative starting
# point (see e.g. OWASP's HSTS cheat sheet) short of the two-year "preload"
# submission length, which this app has no business requesting: preload is
# a permanent, browser-vendor-controlled commitment keyed on the exact
# public hostname, wrong for a value that varies per self-hosted org
# deployment (``org.issuer_url``'s host) rather than being one fixed domain
# this project itself controls. Only ever added in org mode -- see
# _SecurityHeadersMiddleware's own docstring for why local mode never sends
# this header at all.
_HSTS = "max-age=31536000; includeSubDomains"


def load_or_create_token() -> str:
    """The persistent local-mode secret -- see module docstring. Reused
    across daemon restarts within the same installed version (same file,
    same posture as ipc_token), but rotated whenever ``__version__``
    changes -- including the very first startup after upgrading to this
    SEC-06 fix, which rotates whatever token an older build already wrote,
    dead-ending any ``?token=`` link, log line, or shell-history entry that
    named it. A missing/unreadable version marker counts as "changed" too,
    so every pre-SEC-06 install (which never wrote one) rotates exactly
    once on first startup under this version."""
    path = paths.data_dir() / TOKEN_FILE_NAME
    version_path = paths.data_dir() / TOKEN_VERSION_FILE_NAME
    current_version = __version__
    if path.exists() and version_path.exists():
        try:
            same_version = version_path.read_text(encoding="utf-8").strip() == current_version
        except OSError:
            same_version = False
        if same_version:
            token = path.read_text(encoding="utf-8").strip()
            if token:
                return token
    token = secrets.token_hex(32)
    atomic_write_text(path, token)
    atomic_write_text(version_path, current_version)
    return token


def _write_mcp_url_file(url: str) -> None:
    """The direct successor of ipc.py's PORT_FILE for a client that talks to
    /mcp instead of the old IPC socket -- see mcpb/shim/src/protocol.ts's
    module docstring, which reads this same file (D11): "WebServer.start()
    writes ~/.privacyfence/mcp_url when it binds, and clears it on
    shutdown -- the only new daemon-side surface P4b needs." 0600 for the
    same reason web_token/mcp_token are: not a secret itself, but written
    alongside them under the same directory."""
    path = paths.data_dir() / MCP_URL_FILE_NAME
    atomic_write_text(path, url)


def _clear_mcp_url_file() -> None:
    """Called on WebServer.stop() so a shim launched after this daemon exits
    finds no file rather than a stale, now-dead URL -- the same reasoning
    ipc_server.py's own shutdown has for not leaving a dangling PORT_FILE
    behind."""
    (paths.data_dir() / MCP_URL_FILE_NAME).unlink(missing_ok=True)


class _SecurityHeadersMiddleware:
    """Plain ASGI middleware (not starlette.middleware.base.
    BaseHTTPMiddleware, which buffers the whole response) adding the fixed
    header set every response from this app needs -- see, for the three
    added by SEC-18, the module-level ``_PERMISSIONS_POLICY``/``_HSTS``
    constants' own comments.
    Cache-Control: no-store is also set per-route (web/routes_approvals.py
    and friends) for the routes that actually carry sensitive content,
    since a static blanket no-store here would be redundant with, not a
    replacement for, being deliberate about it at the route that matters --
    see test_server.py's ``TestCacheControlOnSensitivePages`` for the sweep
    that checks every one of those routes actually does.

    ``hsts`` gates ``Strict-Transport-Security`` alone: local mode (``org``
    is ``None`` in build_app()) binds to plain ``http://localhost`` by
    design (this module's own docstring), where the header is at best
    inert and at worst actively wrong -- a browser that honored it would
    start refusing plain-HTTP connections to ``localhost`` for every *other*
    app on the machine too, since HSTS is scoped by hostname, not by port
    or origin. Org mode is the one caller that passes ``hsts=True``: its
    ``issuer_url`` is a real, single-purpose public hostname reached over
    HTTPS (TLS terminated here or, just as often, at a reverse proxy in
    front of this daemon -- either way the browser's own connection is
    HTTPS, which is what HSTS actually governs), so pinning that hostname
    to HTTPS-only is exactly the SEC-18 win intended, with none of local
    mode's collateral risk.

    **CSP nonce (SEC-08, Phase 3.1).** A fresh nonce is minted for every
    HTTP request and placed on ``scope["state"]`` (readable downstream as
    ``request.state.csp_nonce``, see web/csp.py's ``nonce_for``) *before*
    the wrapped app runs -- every route that builds its document fresh
    per-request (``/approvals``, ``/settings``, the org-mode browser pages)
    just uses that value as-is. A route serving a document that was
    rendered once, well before this response existed (an approval card --
    see approval_window_html.py's own module docstring), overrides it via
    ``web/csp.py``'s ``set_nonce`` to whatever nonce is already baked into
    that specific document. Either way, the actual header value below is
    read from ``scope["state"]`` at send time, *after* the app has already
    run and had a chance to override it -- not the value minted up front --
    so the header always matches whichever nonce actually ended up in the
    response body.

    **Replace, not extend (SEC-08, Phase 3.1).** Previously this appended
    the fixed header set onto whatever the wrapped app already sent, which
    would silently emit *two* headers of the same name -- ambiguous at
    best, and for Content-Security-Policy specifically, most browsers
    intersect multiple CSP headers into their most-restrictive combination,
    which is not the same thing as "the value this middleware computed"
    and isn't something any caller here actually wants. Using
    ``MutableHeaders`` instead makes each of these headers authoritative --
    set exactly once, overriding rather than accumulating alongside
    anything a route handler already added under the same name.
    """

    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        self._app = app
        self._hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        state.setdefault("csp_nonce", _new_csp_nonce())

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                nonce = scope.get("state", {}).get("csp_nonce") or _new_csp_nonce()
                headers = MutableHeaders(raw=message.setdefault("headers", []))
                headers["x-frame-options"] = "DENY"
                headers["x-content-type-options"] = "nosniff"
                headers["referrer-policy"] = "no-referrer"
                headers["content-security-policy"] = build_csp(nonce)
                headers["permissions-policy"] = _PERMISSIONS_POLICY
                headers["cross-origin-opener-policy"] = "same-origin"
                if self._hsts:
                    headers["strict-transport-security"] = _HSTS
            await send(message)

        await self._app(scope, receive, send_with_headers)


@dataclass(frozen=True)
class OrgAuth:
    """Everything build_app()/WebServer need to run org mode (P7) -- one
    bundle instead of four separate parameters, so a caller either opts
    into the whole org-mode picture (a real OAuth 2.1 AS, real sessions,
    the IdP config both go through) or passes ``org=None`` and gets local
    mode's own unchanged behavior. See this module's own docstring for
    what org mode does and deliberately does not mount yet.
    """

    provider: OrgOAuthProvider
    sessions: OrgSessionStore
    idp: IdpConfig
    issuer_url: str
    # P8: per-user service
    # authorization (Google/Slack/Salesforce/Atlassian/Telegram) and the
    # /connect page that drives it. Both default to None/{} so every
    # existing caller of OrgAuth (this module's own tests included) keeps
    # constructing one without them -- _build_org_app below simply doesn't
    # mount web/routes_connect.py's routes when connector_registry is
    # absent, the same "additive, opt-in" posture every other new surface
    # in this codebase takes. daemon_main.py's real _start_org_web_server
    # always supplies both.
    connector_registry: ConnectorRegistry | None = None
    org_config: dict = field(default_factory=dict)


def _default_principal(_request: Request) -> Principal:
    """Local mode's own resolver: there is no logged-in multi-user session
    to resolve a real one from -- possessing the shared token *is* the
    identity, exactly as this module's own docstring describes."""
    return LOCAL_PRINCIPAL


def _org_principal_resolver(sessions: OrgSessionStore) -> Callable[[Request], Principal]:
    """Org mode's default resolver (P7): a valid ``pf_org_session`` cookie
    (web/org_session.py) resolves to the human who signed in via
    ``/login``; anything else -- no cookie, an unknown or expired one --
    resolves to ``ANONYMOUS_PRINCIPAL``, never ``LOCAL_PRINCIPAL`` (see
    that constant's own docstring for why conflating "not authenticated"
    with "the local single-user principal" would be actively misleading in
    a multi-user deployment). The route itself is what actually rejects an
    unauthenticated request; this only decides what current_principal()
    resolves to in the brief window before that check runs.
    """

    def resolve(request: Request) -> Principal:
        return org_session.authenticated(request, sessions) or ANONYMOUS_PRINCIPAL

    return resolve


class _PrincipalScopeMiddleware:
    """The browser surface's principal_scope() entry point (P6: "entered
    once per HTTP request, in exactly one place per surface") -- the MCP
    endpoint's own entry point is routes_mcp.py's handle_call_tool. Every
    per-principal registry
    downstream (auto_accept.py, audit_log.py, pii_detector.py,
    privacy_filter.py, resource_names.py) resolves against whatever
    ``resolve`` returns for the rest of the request.

    ``resolve`` defaults to _default_principal (always LOCAL_PRINCIPAL) in
    local mode, or _org_principal_resolver in org mode (P7) --
    parameterized rather than hardcoded so a test can inject a resolver
    that varies by request to prove two principals stay isolated all the
    way through the real HTTP routes, not just via principal_scope()
    called directly.
    """

    def __init__(self, app: ASGIApp, resolve: Callable[[Request], Principal]) -> None:
        self._app = app
        self._resolve = resolve

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        principal = self._resolve(Request(scope))
        with principal_scope(principal):
            await self._app(scope, receive, send)


def _parse_host_header(raw: str) -> str | None:
    """Standards-aware ``Host`` header -> bare hostname (SEC-17). The manual
    ``split(":", 1)[0]`` this replaced assumed the first colon always
    separates host from port, which is only true for a bare name or IPv4
    address -- an IPv6 literal has colons *in* the host itself
    (``[::1]:8443``, or even a port-less ``[::1]``), so that split just
    returned the literal ``"["`` -- every IPv6 request either got rejected
    outright, or wrongly let through by whatever else happened to compare
    equal to ``"["``.

    Delegating to ``urlsplit`` on a synthesized ``//<raw>`` authority
    gets RFC 3986's bracket-aware host/port grammar for free -- its
    ``.hostname`` is already lowercased and has the brackets stripped, so
    ``"[::1]"`` and ``"[::1]:8443"`` both normalize to ``"::1"``, matching
    the plain ``urlsplit(...).hostname`` used for the org issuer host
    below.

    Returns ``None`` -- treat exactly like a disallowed host -- for
    anything that isn't a clean ``host[:port]`` authority: an unparsable
    port, userinfo (``user@host``, never valid in a Host header), or a
    stray path/query/fragment component smuggled in behind the host.
    """
    if not raw:
        return None
    try:
        parsed = urlsplit(f"//{raw}")
        parsed.port  # noqa: B018 -- validated lazily; unparsable ports raise here, not on urlsplit()
    except ValueError:
        return None
    if parsed.username is not None or parsed.path or parsed.query or parsed.fragment:
        return None
    return parsed.hostname


class _HostAllowlistMiddleware:
    """DNS-rebinding defense: reject any request whose Host header isn't in the configured
    allowlist, *before* it reaches any route -- a page served from a
    malicious domain that gets a victim's browser to send a request to
    ``http://localhost:PORT`` with a forged Host header is exactly what
    this stops from resolving as a "same server" request routing-wise.
    """

    def __init__(self, app: ASGIApp, allowed_hosts: frozenset[str]) -> None:
        self._app = app
        self._allowed_hosts = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request = Request(scope)
        host = _parse_host_header(request.headers.get("host") or "")
        if host is None or host not in self._allowed_hosts:
            response = PlainTextResponse("Invalid Host header", status_code=400)
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


class _BootstrapMiddleware:
    """SEC-06: the
    single place a ``?bootstrap=<code>`` query string is ever honored,
    ahead of every route in the local-mode app. A live, unexpired code is
    consumed (so it can never be replayed -- successful exchange or not)
    and exchanged for a brand-new, independent session id, set as the
    ``pf_session`` cookie; either way the response is a redirect to the
    *same path* with no query string at all, so neither the code nor the
    session id it minted ever appears in a URL, browser history, or a
    Referer header the way the old ``?token=`` link (itself the literal
    session-cookie value) did. A request that carries no ``bootstrap``
    param passes straight through untouched -- this only ever intercepts
    the one-time exchange, never ordinary authenticated traffic, which
    keeps proving itself out via the ``pf_session`` cookie exactly as
    before.
    """

    def __init__(self, app: ASGIApp, *, bootstrap: BootstrapStore, sessions: LocalSessionStore) -> None:
        self._app = app
        self._bootstrap = bootstrap
        self._sessions = sessions

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request = Request(scope)
        code = request.query_params.get(BOOTSTRAP_QUERY_PARAM, "")
        if not code:
            await self._app(scope, receive, send)
            return
        response = RedirectResponse(request.url.path, status_code=303)
        if self._bootstrap.consume(code):
            _set_session_cookie(response, self._sessions.create())
        await response(scope, receive, send)


def _bootstrap_mint_route(*, token: str, bootstrap: BootstrapStore) -> Route:
    """``POST /api/bootstrap`` (SEC-06) -- mints a fresh one-time bootstrap
    code on demand, for a human who still has filesystem access to this
    machine (``load_or_create_token()``'s persistent secret, read from
    ``~/.privacyfence/web_token``) but whose previous session or bootstrap
    link has already expired, without needing to restart the daemon. The
    secret is presented as an ``Authorization: Bearer`` header, never a
    query string -- see verify_bearer_secret's own docstring for why."""

    async def handler(request: Request) -> Response:
        if not verify_bearer_secret(request, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"bootstrap": bootstrap.mint()}, headers={"Cache-Control": "no-store"})

    return Route("/api/bootstrap", handler, methods=["POST"])


def _state_stream_route(stream: StateStream, *, sessions: LocalSessionStore) -> Route:
    """``GET /api/state/stream`` (§16.3) -- the one interface this phase
    and P3 share; see web/state_stream.py's own module docstring for what
    it carries. Built here (not in state_stream.py itself) purely because
    every other route factory in this module already lives beside
    _HostAllowlistMiddleware/build_app -- state_stream.py stays focused on
    the stream's own state and SSE-formatting logic."""

    async def handler(request: Request) -> Response:
        if not _session_authenticated(request, sessions):
            return _unauthorized_response(request)
        return StreamingResponse(
            stream.subscribe(request.is_disconnected),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store"},
        )

    return Route("/api/state/stream", handler)


@contextlib.asynccontextmanager
async def _combined_lifespan(managers: list) -> AsyncIterator[None]:
    """Compose however many of {mcp_lifespan, the state-stream loop-capture
    below} this build actually needs into the one ``lifespan`` Starlette
    accepts -- build_app() constructs `managers` from whichever of
    mcp_dispatcher/state_stream were actually passed in, so a caller that
    passes neither (every pre-P2 test in this repo) gets an empty list and
    this is a no-op context manager, unchanged."""
    async with contextlib.AsyncExitStack() as stack:
        for cm in managers:
            await stack.enter_async_context(cm)
        yield


@contextlib.asynccontextmanager
async def _state_stream_loop_lifespan(ready_event: threading.Event | None = None) -> AsyncIterator[None]:
    """Captures this ASGI app's own running event loop into
    web/state_stream.py's module-level ``_loop`` for the app's whole
    lifetime -- settings_controller.call_on_main's fallback dispatcher
    (§16.2.1) needs it to marshal a background-thread callback onto this
    loop rather than running inline. Cleared on shutdown so a stale loop
    reference from a previous server instance (e.g. across daemon restarts
    in a single test process) is never mistaken for a live one.

    ``ready_event``, when given, is set right after the loop is captured --
    WebServer's own ``wait_until_ready`` is what a synchronous caller on
    another thread (daemon_main.py's run_app(), the direct successor of the
    old IPCServerThread's own ``_ready`` Event) blocks on to learn this
    loop, the one every connector call now actually runs on (P5)."""
    loop = asyncio.get_running_loop()
    _state_stream.set_loop(loop)
    if ready_event is not None:
        ready_event.set()
    try:
        yield
    finally:
        _state_stream.set_loop(None)


def build_app(
    web_ui: WebApprovalUI,
    *,
    token: str | None = None,
    sessions: LocalSessionStore | None = None,
    bootstrap: BootstrapStore | None = None,
    allowed_hosts: frozenset[str] = frozenset({"localhost", "127.0.0.1"}),
    mcp_dispatcher: McpDispatcher | None = None,
    mcp_token: str | None = None,
    controller: SettingsController | None = None,
    allow_quit: bool = True,
    state_stream: StateStream | None = None,
    notifications_enabled: bool = True,
    notifications_detail: str = "minimal",
    loop_ready: threading.Event | None = None,
    principal_resolver: Callable[[Request], Principal] | None = None,
    org: OrgAuth | None = None,
) -> ASGIApp:
    """The approval routes, wrapped with the Host allowlist and security
    headers every real deployment needs -- routes_approvals.create_app()
    alone (no wrapping) is what tests reach for when they want to exercise
    the routes without also exercising this middleware stack.

    ``org`` (P7) switches this into org mode: ``token``/``sessions``/
    ``bootstrap``/``mcp_token``/``controller``/``state_stream`` are all
    ignored (org mode doesn't mount the local-session-authenticated
    approval/settings surface at all -- see this module's own docstring for
    why), ``/mcp`` is authenticated by ``org.provider`` instead of a shared
    secret, and the OAuth 2.1 authorization-server + browser-login routes
    are mounted alongside it. Local mode (``org=None``, the default) is
    entirely unchanged from before this phase.

    ``principal_resolver`` defaults to _default_principal (local mode,
    always LOCAL_PRINCIPAL) or _org_principal_resolver (org mode) --
    pass an explicit one only to prove per-principal isolation over real
    HTTP in a test.

    ``mcp_dispatcher`` (P2) folds
    the ``/mcp`` Streamable HTTP endpoint into this same app. In local
    mode it's authenticated by its own ``mcp_token`` -- a secret
    independent of ``sessions`` (the approval surface's own session/CSRF
    store, SEC-06), which is what makes the audience separation ("the
    MCP access token must never be accepted on approval-decision endpoints,
    and the browser session cookie must never be accepted on /mcp") hold
    structurally rather than by convention. In org mode the same
    separation holds because ``org.provider``'s tokens and
    ``org.sessions``' cookies are two entirely different stores with
    nothing that compares one against the other (see
    web/test_org_mcp_e2e.py's own audience-separation tests).

    ``controller`` (P4, §16) folds ``/settings`` and its ``/api/settings/*``
    actions into the same app, on the *same* ``sessions`` store -- unlike
    MCP, the settings surface shares the approval surface's own session/
    CSRF cookie by design (§16.1's exit criterion: "/approvals and
    /settings are one application: one header, one nav, one palette, one
    session"). ``state_stream`` (built by WebServer when either
    ``controller`` or ``web_ui`` needs the push channel) backs
    ``GET /api/state/stream`` either way. ``token`` (SEC-06,
    ``load_or_create_token()``) is only used to authorize
    ``POST /api/bootstrap``; ``sessions``/``bootstrap`` default to a fresh
    store each when omitted, since every real (non-test) local-mode caller
    is ``WebServer``, which always constructs and shares one pair for its
    whole lifetime. Every new parameter defaults to ``None``/unchanged
    behavior, so every existing caller (including this module's own
    pre-P4 tests) is unaffected.
    """
    if org is not None:
        return _build_org_app(
            org, web_ui=web_ui, mcp_dispatcher=mcp_dispatcher, allowed_hosts=allowed_hosts,
            principal_resolver=principal_resolver,
        )

    if not token:
        raise ValueError("token is required in local mode (org=None)")

    sessions = sessions or LocalSessionStore()
    bootstrap = bootstrap or BootstrapStore()

    extra_routes = [_bootstrap_mint_route(token=token, bootstrap=bootstrap)]
    lifespans = []
    if mcp_dispatcher is not None:
        if not mcp_token:
            raise ValueError("mcp_token is required when mcp_dispatcher is given")
        mcp_route, session_manager = mount_mcp(mcp_dispatcher, token=mcp_token)
        extra_routes.append(mcp_route)
        lifespans.append(mcp_lifespan(session_manager))

    if controller is not None:
        extra_routes.extend(build_settings_routes(
            controller, sessions=sessions, allow_quit=allow_quit, notifications_enabled=notifications_enabled,
            notifications_detail=notifications_detail,
        ))

    if state_stream is not None:
        extra_routes.append(_state_stream_route(state_stream, sessions=sessions))
        lifespans.append(_state_stream_loop_lifespan(loop_ready))
        set_main_dispatcher(_state_stream.call_soon_threadsafe)

    lifespan = None
    if lifespans:
        @contextlib.asynccontextmanager
        async def lifespan(_app) -> AsyncIterator[None]:  # noqa: ANN001
            async with _combined_lifespan(lifespans):
                yield

    app = create_approvals_app(
        web_ui, sessions=sessions, extra_routes=extra_routes, lifespan=lifespan,
        notifications_enabled=notifications_enabled, notifications_detail=notifications_detail,
    )
    bootstrapped: ASGIApp = _BootstrapMiddleware(app, bootstrap=bootstrap, sessions=sessions)
    scoped: ASGIApp = _PrincipalScopeMiddleware(bootstrapped, principal_resolver or _default_principal)
    wrapped: ASGIApp = _HostAllowlistMiddleware(scoped, allowed_hosts)
    return _SecurityHeadersMiddleware(wrapped)


def _build_org_app(
    org: OrgAuth, *, web_ui: WebApprovalUI, mcp_dispatcher: McpDispatcher | None, allowed_hosts: frozenset[str],
    principal_resolver: Callable[[Request], Principal] | None,
) -> ASGIApp:
    """org mode's own route set -- see build_app()'s and this module's own
    docstrings for what's deliberately absent (the local-token settings
    surface, still). ``/approvals`` and ``/security`` (P9,
    web/routes_org_approvals.py/web/routes_security.py) are mounted
    unconditionally here -- unlike ``/connect`` (below), they need nothing
    from ``org.connector_registry``, only ``web_ui`` (already a required
    parameter of build_app() in both modes) and ``org.org_config`` for
    ``StepUpConfig``."""
    from urllib.parse import urlparse

    from ..org_mode import AuthzPolicyConfig, StepUpConfig
    from . import routes_org_approvals, routes_security

    extra_routes: list[Route] = []
    lifespans = []
    if mcp_dispatcher is not None:
        mcp_route, session_manager = mount_mcp(
            mcp_dispatcher, verifier=org.provider,
            resource_metadata_url=protected_resource_metadata_url(org.issuer_url),
        )
        extra_routes.append(mcp_route)
        lifespans.append(mcp_lifespan(session_manager))

    extra_routes.extend(mount_org_oauth(org.provider, issuer_url=org.issuer_url))
    # Mounted unconditionally here (every _build_org_app call is already
    # org mode) -- needs nothing from org.connector_registry, only
    # org.sessions, same reasoning /approvals'/security's own unconditional
    # mount below gives for needing only web_ui/org.org_config.
    extra_routes.extend(routes_downloads.build_routes(sessions=org.sessions))
    # P8: only mounted once a
    # real ConnectorRegistry exists to evict on a successful authorization
    # -- see OrgAuth's own docstring. daemon_main.py's real org-mode boot
    # path always supplies one; a hand-built OrgAuth in a test that only
    # cares about the OAuth-AS/session-login surface can omit it and get
    # exactly P7's own route set, with /connect and /oauth/start|callback
    # left out (see test_server_org_mode.py's TestConnectSurfaceOrgMode).
    default_next_path = routes_org_identity.DEFAULT_NEXT_PATH
    if org.connector_registry is not None:
        extra_routes.extend(routes_connect.build_routes(
            sessions=org.sessions, connector_registry=org.connector_registry,
            org_config=org.org_config, issuer_url=org.issuer_url,
        ))
        default_next_path = "/connect"
    extra_routes.extend(routes_org_identity.build_routes(
        idp=org.idp, sessions=org.sessions, base_url=org.issuer_url, default_next_path=default_next_path,
        # SEC-22: derived from org.org_config directly here, the same
        # "self-contained, cheap re-parse" pattern StepUpConfig below
        # already uses -- see that call's own comment.
        policy=AuthzPolicyConfig.from_org_config(org.org_config),
    ))

    issuer_host = urlparse(org.issuer_url).hostname or ""
    step_up = StepUpConfig.from_org_config(org.org_config, default_rp_id=issuer_host)
    extra_routes.extend(routes_org_approvals.build_routes(
        web_ui=web_ui, sessions=org.sessions, step_up=step_up, idp=org.idp, issuer_url=org.issuer_url,
    ))
    if step_up.rp_id:
        extra_routes.extend(routes_security.build_routes(
            sessions=org.sessions, step_up=step_up, issuer_url=org.issuer_url,
        ))

    lifespan = None
    if lifespans:
        @contextlib.asynccontextmanager
        async def lifespan(_app) -> AsyncIterator[None]:  # noqa: ANN001
            async with _combined_lifespan(lifespans):
                yield

    app: ASGIApp = Starlette(routes=extra_routes, lifespan=lifespan)
    resolver = principal_resolver or _org_principal_resolver(org.sessions)
    scoped: ASGIApp = _PrincipalScopeMiddleware(app, resolver)
    wrapped: ASGIApp = _HostAllowlistMiddleware(scoped, allowed_hosts)
    return _SecurityHeadersMiddleware(wrapped, hsts=True)


class WebServer:
    """Runs the embedded HTTP server on its own daemon thread -- always
    started in local mode since P10 (daemon_main.py's own
    ``_maybe_start_web_server``), since the web approval UI is the only one
    there is. Through P9 this started only when ``web.approval_ui: web`` was
    configured; see approval_ui.py's ``init_approval_ui`` seam, which was
    the switch between this and the native popup (docs/https-connector-
    refactor-plan.md §12, decision D6)."""

    def __init__(
        self,
        web_ui: WebApprovalUI,
        *,
        host: str = "localhost",
        port: int = DEFAULT_PORT,
        token: str | None = None,
        mcp_dispatcher: McpDispatcher | None = None,
        mcp_token: str | None = None,
        controller: SettingsController | None = None,
        allow_quit: bool = True,
        notifications_enabled: bool = True,
        notifications_detail: str = "minimal",
        principal_resolver: Callable[[Request], Principal] | None = None,
        org: OrgAuth | None = None,
        ssl_certfile: str | None = None,
        ssl_keyfile: str | None = None,
        trusted_proxies: tuple[str, ...] = (),
    ) -> None:
        """``org``, ``ssl_certfile``/``ssl_keyfile`` and ``trusted_proxies``
        are org mode's own additions (P7, §10.2) -- every local-mode caller
        (every one before this phase) leaves them unset and gets exactly
        today's behavior. ``ssl_certfile``/``ssl_keyfile`` (both required
        together, or neither) terminate TLS directly in uvicorn; leave both
        unset when a reverse proxy in front of this daemon terminates TLS
        instead. ``trusted_proxies`` is the explicit allowlist §10.2
        requires before ``X-Forwarded-For``/``X-Forwarded-Proto`` are
        honored at all -- empty (the default) means never, regardless of
        mode.
        """
        self.host = host
        self.port = port
        self.org = org
        self.token = None if org is not None else (token or load_or_create_token())
        # SEC-06: local mode's real session/bootstrap-code stores, built
        # once here and shared with build_app() below -- None in org mode,
        # which has its own OrgSessionStore (org.sessions) and no bootstrap
        # concept at all (org mode's entry point is /login, not a one-time
        # link). See mint_bootstrap_url() for how a human actually gets a
        # code out of self.bootstrap.
        self.sessions = None if org is not None else LocalSessionStore()
        self.bootstrap = None if org is not None else BootstrapStore()
        self.mcp_dispatcher = mcp_dispatcher
        self.mcp_token = (
            None if org is not None
            else ((mcp_token or load_or_create_mcp_token()) if mcp_dispatcher is not None else None)
        )
        self.controller = controller
        self.allow_quit = allow_quit
        self.notifications_enabled = notifications_enabled
        self.notifications_detail = notifications_detail
        self.principal_resolver = principal_resolver
        # The state-push channel (§16.3) backs both /settings (async
        # outcomes reaching an open tab) and /approvals (P3's own list, via
        # the same "approvals" event) -- built whenever either surface is
        # actually being served, not gated on mcp_dispatcher, which has
        # nothing to do with either page. Not built in org mode: neither
        # surface is mounted there yet (see module docstring), and nothing
        # in this class reads it in that case either.
        self.state_stream: StateStream | None = None
        if org is None and (controller is not None or web_ui is not None):
            self.state_stream = StateStream(
                settings_snapshot=(controller.snapshot if controller is not None else lambda: None),
                list_pending=web_ui.deferred_registry.list_pending,
            )
            if controller is not None:
                controller.add_change_listener(self.state_stream.push_settings)
        # Set once this server's own ASGI event loop is captured (see
        # _state_stream_loop_lifespan) -- wait_until_ready() below is what a
        # synchronous caller on another thread (daemon_main.py's run_app(),
        # the direct successor of the old IPCServerThread's own ``_ready``
        # Event) blocks on to learn that loop.
        self._loop_ready = threading.Event()
        # "::1" (not the bracketed "[::1]" a Host header would spell it
        # as) -- _parse_host_header normalizes every incoming Host header
        # the same way urlsplit's own .hostname does below, brackets
        # stripped, so the allowlist has to speak that same bare form.
        allowed_hosts = frozenset({host, "127.0.0.1", "::1"})
        if org is not None:
            issuer_host = urlsplit(org.issuer_url).hostname
            if issuer_host:
                allowed_hosts = allowed_hosts | {issuer_host}
        wrapped = build_app(
            web_ui,
            token=self.token,
            sessions=self.sessions,
            bootstrap=self.bootstrap,
            allowed_hosts=allowed_hosts,
            mcp_dispatcher=mcp_dispatcher,
            mcp_token=self.mcp_token,
            controller=controller,
            allow_quit=allow_quit,
            state_stream=self.state_stream,
            notifications_enabled=notifications_enabled,
            notifications_detail=notifications_detail,
            loop_ready=self._loop_ready,
            principal_resolver=principal_resolver,
            org=org,
        )
        if trusted_proxies:
            # §10.2: honored only when this explicit list is non-empty --
            # never by default, in either mode.
            wrapped = ProxyHeadersMiddleware(wrapped, trusted_hosts=list(trusted_proxies))
        config = uvicorn.Config(
            wrapped, host=host, port=port, log_level="warning",
            ssl_certfile=ssl_certfile, ssl_keyfile=ssl_keyfile,
        )
        self._server = uvicorn.Server(config)
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        # Org mode's issuer_url is the authoritative externally-reachable
        # origin (it may differ from this process's own bind host/port
        # entirely, e.g. behind a reverse proxy or load balancer) -- local
        # mode has no such indirection, so it keeps computing this from
        # what it's actually bound to.
        if self.org is not None:
            return self.org.issuer_url.rstrip("/")
        return f"http://{self.host}:{self.port}"

    def wait_until_ready(self, timeout: float = 5.0) -> asyncio.AbstractEventLoop | None:
        """Blocks (from any thread) until this server's own ASGI event loop
        has been captured, or ``timeout`` elapses -- the direct successor of
        the old IPCServerThread's own ``_ready.wait(timeout=5)`` pattern,
        for the same reason: daemon_main.py's cache-warming needs a real,
        already-running loop to schedule Telegram's async warm on (see
        daemon_main._warm_connector_caches), not just "the thread has
        started". Returns ``None`` on a timeout, or if this server's own
        state_stream was never built (nothing to wait for -- see __init__:
        that only happens when ``web_ui`` is falsy, which no real caller
        passes)."""
        if self.state_stream is None:
            return None
        self._loop_ready.wait(timeout=timeout)
        return _state_stream.get_loop()

    @property
    def mcp_url(self) -> str | None:
        """``None`` unless this server was built with ``mcp_dispatcher`` --
        the URL to configure in a Streamable HTTP MCP client, e.g.
        ``claude mcp add --transport http privacyfence <mcp_url> --header
        "Authorization: Bearer <mcp_token>"``."""
        if self.mcp_dispatcher is None:
            return None
        return f"{self.base_url}{MCP_PATH}"

    def mint_bootstrap_url(self, path: str) -> str | None:
        """SEC-06: a fresh, single-use ``?bootstrap=`` link for ``path``
        (e.g. ``/approvals``, ``/settings``) -- ``None`` in org mode, which
        has no bootstrap concept (its entry point is ``/login``). Call this
        once per link actually handed to a human -- daemon_main.py's own
        startup log lines are the only caller today -- never reuse the
        result: each call mints a brand-new code, and the previous one (if
        any) is simply left to expire on its own rather than being
        invalidated early."""
        if self.bootstrap is None:
            return None
        return f"{self.base_url}{path}?bootstrap={self.bootstrap.mint()}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.run, name="web-server", daemon=True)
        self._thread.start()
        logger.info("Web approval server listening on %s", self.base_url)
        if self.mcp_url is not None:
            _write_mcp_url_file(self.mcp_url)

    def stop(self) -> None:
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self.mcp_url is not None:
            _clear_mcp_url_file()
