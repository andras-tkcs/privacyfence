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
from ..principal import LOCAL_PRINCIPAL, Principal

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


def principal_from_access_token(token: AccessToken | None) -> Principal:
    """The ``/mcp`` endpoint's principal_scope() entry point (P6, docs/
    https-connector-refactor-plan.md §9.1: "entered once per HTTP request,
    in exactly one place per surface") -- routes_mcp.py calls this once per
    tool call, wrapping dispatch in ``principal_scope(...)`` around it.

    Today ``StaticTokenVerifier`` above only ever mints ``client_id="local"``
    (see its own docstring), so this always resolves to ``LOCAL_PRINCIPAL``
    -- there is no real per-user identity until P7's OAuth 2.1 authorization
    server exists to hand out a token whose ``client_id`` means something
    else. This function is the one place that has to change when it does;
    nothing downstream of principal_scope() does.
    """
    if token is None or token.client_id == LOCAL_PRINCIPAL.id:
        return LOCAL_PRINCIPAL
    return Principal(id=token.client_id)
