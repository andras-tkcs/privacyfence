"""Shared browser-loopback OAuth 2.0 helper.

Used by the Slack, Salesforce, and Atlassian authorize flows (Google keeps using
``google-auth-oauthlib``'s own loopback implementation via ``InstalledAppFlow``).
Handles the parts every Authorization Code + PKCE flow needs: a short-lived local
HTTP server to catch the redirect, CSRF ``state`` verification, and PKCE
``code_verifier``/``code_challenge`` generation.

Slack/Salesforce/Atlassian all require an exact-match redirect URI in their app's
allow-list, so callers must pass a fixed port (unlike Google's "Desktop app" OAuth
clients, which accept any loopback port).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

_SUCCESS_HTML = b"""<!doctype html><html><head><title>PrivacyFence</title></head>
<body style="font-family: -apple-system, sans-serif; text-align: center; padding-top: 4em;">
<h2>You're connected.</h2><p>You can close this window and return to PrivacyFence.</p>
</body></html>"""

_ERROR_HTML = b"""<!doctype html><html><head><title>PrivacyFence</title></head>
<body style="font-family: -apple-system, sans-serif; text-align: center; padding-top: 4em;">
<h2>Something went wrong.</h2><p>Close this window and try again from PrivacyFence.</p>
</body></html>"""


class OAuthLoopbackError(Exception):
    """Raised when the loopback OAuth flow fails (timeout, state mismatch, provider error)."""


@dataclass
class _CallbackResult:
    code: str | None = None
    error: str | None = None


def _make_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE with the S256 method."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def run_browser_oauth(
    build_authorize_url: Callable[[str, str, str], str],
    exchange: Callable[[str, str, str], dict[str, Any]],
    port: int,
    path: str = "/callback",
    timeout: float = 180.0,
    open_browser: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Run a browser-based Authorization Code + PKCE flow via a loopback redirect.

    ``build_authorize_url(redirect_uri, state, code_challenge)`` returns the full
    authorize URL to open in the browser.

    ``exchange(code, redirect_uri, code_verifier)`` trades the authorization code
    for tokens and returns the provider's token response.

    Binds ``127.0.0.1:port`` only for the duration of this call — the server is
    torn down as soon as the callback is received (or the timeout expires).
    """
    state = secrets.token_urlsafe(24)
    code_verifier, code_challenge = _make_pkce_pair()
    redirect_uri = f"http://127.0.0.1:{port}{path}"

    result = _CallbackResult()
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # silence default access log
            pass

        def do_GET(self) -> None:  # noqa: N802 - required stdlib handler method name
            parsed = urlparse(self.path)
            if parsed.path != path:
                self.send_response(404)
                self.end_headers()
                return

            qs = parse_qs(parsed.query)
            got_state = (qs.get("state") or [""])[0]
            error = (qs.get("error_description") or qs.get("error") or [""])[0]
            code = (qs.get("code") or [""])[0]

            if error:
                result.error = error
            elif got_state != state:
                result.error = "state mismatch (possible CSRF) — please retry"
            elif not code:
                result.error = "no authorization code in callback"
            else:
                result.code = code

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_ERROR_HTML if result.error else _SUCCESS_HTML)
            done.set()

    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        raise OAuthLoopbackError(
            f"Could not bind 127.0.0.1:{port} for the OAuth redirect — is another "
            f"PrivacyFence sign-in already in progress? ({exc})"
        ) from exc

    server_thread = threading.Thread(target=server.serve_forever, daemon=True, name="oauth-loopback")
    server_thread.start()

    try:
        authorize_url = build_authorize_url(redirect_uri, state, code_challenge)
        logger.info("Opening browser for OAuth authorization (redirect_uri=%s)", redirect_uri)
        opener = open_browser
        if opener is None:
            import webbrowser as _webbrowser

            opener = _webbrowser.open
        if not opener(authorize_url):
            raise OAuthLoopbackError(f"Could not open a browser. Visit manually: {authorize_url}")

        if not done.wait(timeout=timeout):
            raise OAuthLoopbackError("Timed out waiting for sign-in to complete in the browser.")
    finally:
        server.shutdown()
        server_thread.join(timeout=5)
        server.server_close()

    if result.error:
        raise OAuthLoopbackError(result.error)
    assert result.code is not None

    return exchange(result.code, redirect_uri, code_verifier)
