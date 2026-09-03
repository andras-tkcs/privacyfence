"""Local-mode bearer-token auth for ``/mcp``.

A ``TokenVerifier`` (the official SDK's protocol,
``mcp.server.auth.provider.TokenVerifier``) checking a single shared secret
-- the same "possession of this file is the authority" posture
``~/.privacyfence/ipc_token`` already has for the bridge (see ipc.py's
module docstring) and ``web_token`` has for the approval surface (see
server.py's module docstring). Not real OAuth 2.1: that's org mode (D5 in
docs/https-connector-refactor-plan.md §15, P7+). Using the SDK's own
``TokenVerifier``/``BearerAuthBackend``/``RequireAuthMiddleware`` here
anyway -- rather than a hand-rolled header check -- means P7 only has to
swap this one class for a real verifier; routes_mcp.py's own wiring doesn't
change.

This token is deliberately a **separate secret from web_token**
(server.py's approval-surface token): §10.3's audience separation --
"the MCP access token must never be accepted on approval-decision
endpoints, and the browser session cookie must never be accepted on
/mcp" -- has to hold even if someone reuses one file's contents by hand, so
the two are generated independently and never compared against each other
anywhere in this codebase. See web/test_routes_mcp.py's audience-separation
test, which is the one required to fail loudly if that ever changes.
"""
from __future__ import annotations

import hmac
import secrets

from mcp.server.auth.provider import AccessToken, TokenVerifier

from .. import paths

MCP_TOKEN_FILE_NAME = "mcp_token"


def load_or_create_mcp_token() -> str:
    """Reused across daemon restarts (same file), same posture as
    web/server.py's ``load_or_create_token``."""
    path = paths.data_dir() / MCP_TOKEN_FILE_NAME
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_hex(32)
    path.write_text(token, encoding="utf-8")
    path.chmod(0o600)
    return token


class StaticTokenVerifier(TokenVerifier):
    """Verifies a bearer token against one fixed shared secret -- see module
    docstring. ``client_id`` is always ``"local"``: there is exactly one
    principal in local mode (§9.2), so there's nothing else it could be."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or not hmac.compare_digest(token, self._token):
            return None
        return AccessToken(token=token, client_id="local", scopes=[])
