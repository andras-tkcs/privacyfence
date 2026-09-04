"""Embedded HTTP(S) server lifecycle: bind policy, security headers, and
starting/stopping the ASGI app (uvicorn) on its own thread -- the same
"runs on its own dedicated thread, daemon still starting for IPC either
way" posture daemon_main.py's IPCServerThread already established for the
bridge socket.

**Local mode** (unchanged since P1): D1's decision applies -- loopback
HTTP, bound to ``localhost`` (not a bare ``127.0.0.1``/``0.0.0.0``) so
``http://localhost`` stays a secure context for whatever this surface needs
(WebAuthn, P9) without anyone having to move the bind address. Auth is
deliberately the simplest thing that's still a real control -- the same
"possession of a local secret is the authority" posture
``~/.privacyfence/ipc_token`` already has for the bridge (see ipc.py's
module docstring), not sessions/OIDC. A random token is generated once,
written 0600 under paths.data_dir(), and required (as a session cookie once
presented, or a ``?token=`` query param the first time) by every route --
web/routes_approvals.py's own docstring covers the CSRF double-submit this
token also backs.

**Org mode** (P7, docs/https-connector-refactor-plan.md §10.2): a
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

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from .. import paths
from ..connector_registry import ConnectorRegistry
from ..org_identity import IdpConfig
from ..principal import ANONYMOUS_PRINCIPAL, LOCAL_PRINCIPAL, Principal, principal_scope
from ..settings_controller import SettingsController, set_main_dispatcher
from ..web_approval_ui import WebApprovalUI
from . import org_session
from . import routes_connect
from . import routes_org_identity
from . import state_stream as _state_stream
from .mcp_auth import load_or_create_mcp_token
from .mcp_dispatch import McpDispatcher
from .oauth_provider import OrgOAuthProvider
from .org_session import OrgSessionStore
from .routes_approvals import create_app as create_approvals_app
from .routes_mcp import MCP_PATH, mcp_lifespan, mount_mcp, mount_org_oauth, protected_resource_metadata_url
from .routes_settings import build_routes as build_settings_routes
from .session_auth import authenticated as _token_authenticated
from .session_auth import unauthorized_html as _unauthorized_response
from .state_stream import StateStream

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8765
TOKEN_FILE_NAME = "web_token"
MCP_URL_FILE_NAME = "mcp_url"

# Content-Security-Policy for a fully self-contained document (see
# approval_window_html.py's own module docstring: fonts/icons are base64
# data URIs, never a network fetch) -- default-src 'none' with narrow,
# explicit exceptions for exactly what these pages actually use, not a
# blanket 'unsafe-inline' grant. See docs/https-connector-refactor-plan.md
# §10.5. worker-src 'self' (P4/W8) is the one addition since P1: without
# it, registering resources/sw.js for tier-0/1 notifications
# (web_shell.py's own script) is blocked by the same default-src 'none'
# every other unlisted fetch type already is -- 'self' only, same-origin,
# nothing external.
_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "img-src data:; font-src data:; connect-src 'self'; worker-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def load_or_create_token() -> str:
    """The shared local-mode secret -- see module docstring. Reused across
    daemon restarts (same file, same posture as ipc_token) so a
    previously-bookmarked ``?token=`` link or session cookie keeps working."""
    path = paths.data_dir() / TOKEN_FILE_NAME
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_hex(32)
    path.write_text(token, encoding="utf-8")
    path.chmod(0o600)
    return token


def _write_mcp_url_file(url: str) -> None:
    """The direct successor of ipc.py's PORT_FILE for a client that talks to
    /mcp instead of the old IPC socket -- see mcpb/shim/src/protocol.ts's
    module docstring, which reads this same file (D11 in
    docs/https-connector-refactor-plan.md §12: "WebServer.start() writes
    ~/.privacyfence/mcp_url when it binds, and clears it on shutdown -- the
    only new daemon-side surface P4b needs."). 0600 for the same reason
    web_token/mcp_token are: not a secret itself, but written alongside them
    under the same directory."""
    path = paths.data_dir() / MCP_URL_FILE_NAME
    path.write_text(url, encoding="utf-8")
    path.chmod(0o600)


def _clear_mcp_url_file() -> None:
    """Called on WebServer.stop() so a shim launched after this daemon exits
    finds no file rather than a stale, now-dead URL -- the same reasoning
    ipc_server.py's own shutdown has for not leaving a dangling PORT_FILE
    behind."""
    (paths.data_dir() / MCP_URL_FILE_NAME).unlink(missing_ok=True)


class _SecurityHeadersMiddleware:
    """Plain ASGI middleware (not starlette.middleware.base.
    BaseHTTPMiddleware, which buffers the whole response) adding the fixed
    header set every response from this app needs -- see
    docs/https-connector-refactor-plan.md §10.5. Cache-Control: no-store is
    also set per-route (web/routes_approvals.py) for the routes that
    actually carry approval content, since a static blanket no-store here
    would be redundant with, not a replacement for, being deliberate about
    it at the route that matters.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend([
                    (b"x-frame-options", b"DENY"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"content-security-policy", _CSP.encode("ascii")),
                ])
                message = {**message, "headers": headers}
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
    # P8 (docs/https-connector-refactor-plan.md §9.3): per-user service
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
    """The browser surface's principal_scope() entry point (P6, docs/
    https-connector-refactor-plan.md §9.1: "entered once per HTTP request,
    in exactly one place per surface") -- the MCP endpoint's own entry point
    is routes_mcp.py's handle_call_tool. Every per-principal registry
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


class _HostAllowlistMiddleware:
    """DNS-rebinding defense (docs/https-connector-refactor-plan.md §10.5,
    §9.4): reject any request whose Host header isn't in the configured
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
        host = (request.headers.get("host") or "").split(":", 1)[0].lower()
        if host not in self._allowed_hosts:
            response = PlainTextResponse("Invalid Host header", status_code=400)
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


def _state_stream_route(stream: StateStream, *, token: str) -> Route:
    """``GET /api/state/stream`` (§16.3) -- the one interface this phase
    and P3 share; see web/state_stream.py's own module docstring for what
    it carries. Built here (not in state_stream.py itself) purely because
    every other route factory in this module already lives beside
    _HostAllowlistMiddleware/build_app -- state_stream.py stays focused on
    the stream's own state and SSE-formatting logic."""

    async def handler(request: Request) -> Response:
        if not _token_authenticated(request, token):
            return _unauthorized_response()
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
    loop, the one every connector call now actually runs on (P5,
    docs/https-connector-refactor-plan.md §12)."""
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

    ``org`` (P7, §9.4) switches this into org mode: ``token``/
    ``mcp_token``/``controller``/``state_stream`` are all ignored (org mode
    doesn't mount the local-token-authenticated approval/settings surface
    at all -- see this module's own docstring for why), ``/mcp`` is
    authenticated by ``org.provider`` instead of a shared secret, and the
    OAuth 2.1 authorization-server + browser-login routes are mounted
    alongside it. Local mode (``org=None``, the default) is entirely
    unchanged from before this phase.

    ``principal_resolver`` defaults to _default_principal (local mode,
    always LOCAL_PRINCIPAL) or _org_principal_resolver (org mode) --
    pass an explicit one only to prove per-principal isolation over real
    HTTP in a test.

    ``mcp_dispatcher`` (P2, docs/https-connector-refactor-plan.md §8) folds
    the ``/mcp`` Streamable HTTP endpoint into this same app. In local
    mode it's authenticated by its own ``mcp_token`` -- a secret
    independent of ``token`` (the approval surface's own session/CSRF
    secret), which is what makes §10.3's audience separation ("the MCP
    access token must never be accepted on approval-decision endpoints,
    and the browser session cookie must never be accepted on /mcp") hold
    structurally rather than by convention. In org mode the same
    separation holds because ``org.provider``'s tokens and
    ``org.sessions``' cookies are two entirely different stores with
    nothing that compares one against the other (see
    web/test_org_mcp_e2e.py's own audience-separation tests).

    ``controller`` (P4, §16) folds ``/settings`` and its ``/api/settings/*``
    actions into the same app, on the *same* ``token`` -- unlike MCP, the
    settings surface shares the approval surface's own session/CSRF secret
    and cookie by design (§16.1's exit criterion: "/approvals and /settings
    are one application: one header, one nav, one palette, one session").
    ``state_stream`` (built by WebServer when either ``controller`` or
    ``web_ui`` needs the push channel) backs ``GET /api/state/stream``
    either way. Every new parameter defaults to ``None``/unchanged
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

    extra_routes = []
    lifespans = []
    if mcp_dispatcher is not None:
        if not mcp_token:
            raise ValueError("mcp_token is required when mcp_dispatcher is given")
        mcp_route, session_manager = mount_mcp(mcp_dispatcher, token=mcp_token)
        extra_routes.append(mcp_route)
        lifespans.append(mcp_lifespan(session_manager))

    if controller is not None:
        extra_routes.extend(build_settings_routes(
            controller, token=token, allow_quit=allow_quit, notifications_enabled=notifications_enabled,
            notifications_detail=notifications_detail,
        ))

    if state_stream is not None:
        extra_routes.append(_state_stream_route(state_stream, token=token))
        lifespans.append(_state_stream_loop_lifespan(loop_ready))
        set_main_dispatcher(_state_stream.call_soon_threadsafe)

    lifespan = None
    if lifespans:
        @contextlib.asynccontextmanager
        async def lifespan(_app) -> AsyncIterator[None]:  # noqa: ANN001
            async with _combined_lifespan(lifespans):
                yield

    app = create_approvals_app(
        web_ui, token=token, extra_routes=extra_routes, lifespan=lifespan,
        notifications_enabled=notifications_enabled, notifications_detail=notifications_detail,
    )
    scoped: ASGIApp = _PrincipalScopeMiddleware(app, principal_resolver or _default_principal)
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

    from ..org_mode import StepUpConfig
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
    # P8 (docs/https-connector-refactor-plan.md §9.3): only mounted once a
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
    return _SecurityHeadersMiddleware(wrapped)


class WebServer:
    """Runs the embedded HTTP server on its own daemon thread -- started
    only when ``web.approval_ui: web`` is configured (daemon_main.py); see
    approval_ui.py's ``init_approval_ui`` seam, which is the actual switch
    between this and the native popup (rollback lever, per
    docs/https-connector-refactor-plan.md §12)."""

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
        allowed_hosts = frozenset({host, "127.0.0.1", "[::1]"})
        if org is not None:
            from urllib.parse import urlparse

            issuer_host = urlparse(org.issuer_url).hostname
            if issuer_host:
                allowed_hosts = allowed_hosts | {issuer_host}
        wrapped = build_app(
            web_ui,
            token=self.token,
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
