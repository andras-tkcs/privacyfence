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

import contextlib
import logging
import secrets
import threading
from collections.abc import AsyncIterator

import uvicorn
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .. import paths
from ..web_approval_ui import WebApprovalUI
from .mcp_auth import load_or_create_mcp_token
from .mcp_dispatch import McpDispatcher
from .routes_approvals import create_app as create_approvals_app
from .routes_mcp import MCP_PATH, mcp_lifespan, mount_mcp

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


def build_app(
    web_ui: WebApprovalUI,
    *,
    token: str,
    allowed_hosts: frozenset[str] = frozenset({"localhost", "127.0.0.1"}),
    mcp_dispatcher: McpDispatcher | None = None,
    mcp_token: str | None = None,
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
    be accepted on /mcp") hold structurally rather than by convention. Omit
    both to get exactly P1's app, unchanged.
    """
    extra_routes = []
    lifespan = None
    if mcp_dispatcher is not None:
        if not mcp_token:
            raise ValueError("mcp_token is required when mcp_dispatcher is given")
        mcp_route, session_manager = mount_mcp(mcp_dispatcher, token=mcp_token)
        extra_routes.append(mcp_route)

        @contextlib.asynccontextmanager
        async def lifespan(_app) -> AsyncIterator[None]:  # noqa: ANN001
            async with mcp_lifespan(session_manager):
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
    ) -> None:
        self.host = host
        self.port = port
        self.token = token or load_or_create_token()
        self.mcp_dispatcher = mcp_dispatcher
        self.mcp_token = (mcp_token or load_or_create_mcp_token()) if mcp_dispatcher is not None else None
        wrapped = build_app(
            web_ui,
            token=self.token,
            allowed_hosts=frozenset({host, "127.0.0.1", "[::1]"}),
            mcp_dispatcher=mcp_dispatcher,
            mcp_token=self.mcp_token,
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
