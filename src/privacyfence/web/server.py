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

import logging
import secrets
import threading

import uvicorn
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .. import paths
from ..web_approval_ui import WebApprovalUI
from .routes_approvals import create_app as create_approvals_app

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8765
TOKEN_FILE_NAME = "web_token"

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
    web_ui: WebApprovalUI, *, token: str, allowed_hosts: frozenset[str] = frozenset({"localhost", "127.0.0.1"}),
) -> ASGIApp:
    """The approval routes, wrapped with the Host allowlist and security
    headers every real deployment needs -- routes_approvals.create_app()
    alone (no wrapping) is what tests reach for when they want to exercise
    the routes without also exercising this middleware stack."""
    app = create_approvals_app(web_ui, token=token)
    wrapped: ASGIApp = _HostAllowlistMiddleware(app, allowed_hosts)
    return _SecurityHeadersMiddleware(wrapped)


class WebServer:
    """Runs the embedded HTTP server on its own daemon thread -- started
    only when ``web.approval_ui: web`` is configured (daemon_main.py); see
    approval_ui.py's ``init_approval_ui`` seam, which is the actual switch
    between this and the native popup (rollback lever, per
    docs/https-connector-refactor-plan.md §12)."""

    def __init__(
        self, web_ui: WebApprovalUI, *, host: str = "localhost", port: int = DEFAULT_PORT, token: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.token = token or load_or_create_token()
        wrapped = build_app(
            web_ui, token=self.token, allowed_hosts=frozenset({host, "127.0.0.1", "[::1]"}),
        )
        config = uvicorn.Config(wrapped, host=host, port=port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.run, name="web-server", daemon=True)
        self._thread.start()
        logger.info("Web approval server listening on %s", self.base_url)

    def stop(self) -> None:
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
