"""Embedded HTTP(S) server lifecycle: bind policy, security headers, and
starting/stopping the ASGI app (uvicorn) on its own thread -- the same
"runs on its own dedicated thread, daemon still starting for IPC either
way" posture daemon_main.py's IPCServerThread already established for the
bridge socket.

**local mode only in P1** -- org mode's HTTPS/reverse-proxy/trusted-proxies
handling (docs/https-connector-refactor-plan.md §10.2) is P6+ work, once
principals exist at all. D1's decision already applies here: loopback HTTP,
bound to ``localhost`` (not a bare ``127.0.0.1``/``0.0.0.0``) so
``http://localhost`` stays a secure context for whatever this surface needs
later (WebAuthn, P9) without anyone having to move the bind address then.

Local-mode auth, in P1, is deliberately the simplest thing that's still a
real control -- the same "possession of a local secret is the authority"
posture ``~/.privacyfence/ipc_token`` already has for the bridge (see
ipc.py's module docstring), not sessions/OIDC (§9.4 of the refactor plan,
P4/P7+). A random token is generated once, written 0600 under
paths.data_dir(), and required (as a session cookie once presented, or a
``?token=`` query param the first time) by every route --
web/routes_approvals.py's own docstring covers the CSRF double-submit this
token also backs.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import threading
from collections.abc import AsyncIterator

import uvicorn
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .. import paths
from ..settings_controller import SettingsController, set_main_dispatcher
from ..web_approval_ui import WebApprovalUI
from . import state_stream as _state_stream
from .mcp_auth import load_or_create_mcp_token
from .mcp_dispatch import McpDispatcher
from .routes_approvals import create_app as create_approvals_app
from .routes_mcp import MCP_PATH, mcp_lifespan, mount_mcp
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
# §10.5.
_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "img-src data:; font-src data:; connect-src 'self'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
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
async def _state_stream_loop_lifespan() -> AsyncIterator[None]:
    """Captures this ASGI app's own running event loop into
    web/state_stream.py's module-level ``_loop`` for the app's whole
    lifetime -- settings_controller.call_on_main's fallback dispatcher
    (§16.2.1) needs it to marshal a background-thread callback onto this
    loop rather than running inline. Cleared on shutdown so a stale loop
    reference from a previous server instance (e.g. across daemon restarts
    in a single test process) is never mistaken for a live one."""
    loop = asyncio.get_running_loop()
    _state_stream.set_loop(loop)
    try:
        yield
    finally:
        _state_stream.set_loop(None)


def build_app(
    web_ui: WebApprovalUI,
    *,
    token: str,
    allowed_hosts: frozenset[str] = frozenset({"localhost", "127.0.0.1"}),
    mcp_dispatcher: McpDispatcher | None = None,
    mcp_token: str | None = None,
    controller: SettingsController | None = None,
    allow_quit: bool = True,
    state_stream: StateStream | None = None,
) -> ASGIApp:
    """The approval routes, wrapped with the Host allowlist and security
    headers every real deployment needs -- routes_approvals.create_app()
    alone (no wrapping) is what tests reach for when they want to exercise
    the routes without also exercising this middleware stack.

    ``mcp_dispatcher`` (P2, docs/https-connector-refactor-plan.md §8) folds
    the ``/mcp`` Streamable HTTP endpoint into this same app, on its own
    ``mcp_token`` -- a secret independent of ``token`` (the approval
    surface's own session/CSRF secret), which is what makes §10.3's
    audience separation ("the MCP access token must never be accepted on
    approval-decision endpoints, and the browser session cookie must never
    be accepted on /mcp") hold structurally rather than by convention.

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
    extra_routes = []
    lifespans = []
    if mcp_dispatcher is not None:
        if not mcp_token:
            raise ValueError("mcp_token is required when mcp_dispatcher is given")
        mcp_route, session_manager = mount_mcp(mcp_dispatcher, token=mcp_token)
        extra_routes.append(mcp_route)
        lifespans.append(mcp_lifespan(session_manager))

    if controller is not None:
        extra_routes.extend(build_settings_routes(controller, token=token, allow_quit=allow_quit))

    if state_stream is not None:
        extra_routes.append(_state_stream_route(state_stream, token=token))
        lifespans.append(_state_stream_loop_lifespan())
        set_main_dispatcher(_state_stream.call_soon_threadsafe)

    lifespan = None
    if lifespans:
        @contextlib.asynccontextmanager
        async def lifespan(_app) -> AsyncIterator[None]:  # noqa: ANN001
            async with _combined_lifespan(lifespans):
                yield

    app = create_approvals_app(web_ui, token=token, extra_routes=extra_routes, lifespan=lifespan)
    wrapped: ASGIApp = _HostAllowlistMiddleware(app, allowed_hosts)
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
    ) -> None:
        self.host = host
        self.port = port
        self.token = token or load_or_create_token()
        self.mcp_dispatcher = mcp_dispatcher
        self.mcp_token = (mcp_token or load_or_create_mcp_token()) if mcp_dispatcher is not None else None
        self.controller = controller
        self.allow_quit = allow_quit
        # The state-push channel (§16.3) backs both /settings (async
        # outcomes reaching an open tab) and /approvals (P3's own list, via
        # the same "approvals" event) -- built whenever either surface is
        # actually being served, not gated on mcp_dispatcher, which has
        # nothing to do with either page.
        self.state_stream: StateStream | None = None
        if controller is not None or web_ui is not None:
            self.state_stream = StateStream(
                settings_snapshot=(controller.snapshot if controller is not None else lambda: None),
                list_pending=web_ui.deferred_registry.list_pending,
            )
            if controller is not None:
                controller.add_change_listener(self.state_stream.push_settings)
        wrapped = build_app(
            web_ui,
            token=self.token,
            allowed_hosts=frozenset({host, "127.0.0.1", "[::1]"}),
            mcp_dispatcher=mcp_dispatcher,
            mcp_token=self.mcp_token,
            controller=controller,
            allow_quit=allow_quit,
            state_stream=self.state_stream,
        )
        config = uvicorn.Config(wrapped, host=host, port=port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

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
