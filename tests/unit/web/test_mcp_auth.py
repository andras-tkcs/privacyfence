"""Unit tests for web/mcp_auth.py's principal_from_access_token (P6, docs/
https-connector-refactor-plan.md §9.1) -- see test_routes_mcp_principal.py
for the wire-level proof that routes_mcp.py actually calls this per
request.
"""
from __future__ import annotations

from mcp.server.auth.provider import AccessToken

from privacyfence.principal import LOCAL_PRINCIPAL, Principal
from privacyfence.web.mcp_auth import principal_from_access_token


class TestPrincipalFromAccessToken:
    def test_none_token_resolves_to_local_principal(self):
        assert principal_from_access_token(None) == LOCAL_PRINCIPAL

    def test_local_client_id_resolves_to_local_principal(self):
        token = AccessToken(token="t", client_id="local", scopes=[])
        assert principal_from_access_token(token) == LOCAL_PRINCIPAL

    def test_other_client_id_resolves_to_a_matching_principal(self):
        # Unreachable today -- StaticTokenVerifier only ever mints
        # client_id="local" -- but this is the seam P7's real OAuth 2.1
        # authorization server plugs into, so it's worth proving this
        # function itself doesn't hardcode "local" as a fallback for
        # anything but a literally-local client_id.
        token = AccessToken(token="t", client_id="alice", scopes=[])
        assert principal_from_access_token(token) == Principal(id="alice")
