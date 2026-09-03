"""Unit tests for web/mcp_auth.py's principal_from_access_token (P6/P7,
docs/https-connector-refactor-plan.md §9.1) -- see
test_routes_mcp_principal.py for the wire-level proof that routes_mcp.py
actually calls this per request, and web/test_oauth_provider.py for the
org-mode ``OrgOAuthProvider`` tokens this function is actually built to
read (``subject``/``claims``), end to end.
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

    def test_client_id_is_a_fallback_only_when_no_subject_is_present(self):
        # A hand-rolled/future verifier that doesn't populate subject --
        # StaticTokenVerifier's own local-mode case is handled above
        # already; OrgOAuthProvider (P7) always sets subject (see the next
        # test), so this branch exists for robustness, not as the org-mode
        # path itself.
        token = AccessToken(token="t", client_id="some-oauth-client-id", scopes=[])
        assert principal_from_access_token(token) == Principal(id="some-oauth-client-id")

    def test_subject_is_preferred_over_client_id(self):
        # client_id identifies *which Claude installation* registered via
        # DCR, not *which human* is using it -- subject is the org IdP's
        # own resolved identity (OrgOAuthProvider._mint_tokens).
        token = AccessToken(token="t", client_id="claude-desktop-install-1", scopes=[], subject="alice")
        assert principal_from_access_token(token).id == "alice"

    def test_claims_populate_email_display_name_and_is_admin(self):
        token = AccessToken(
            token="t", client_id="c", scopes=[], subject="alice",
            claims={"email": "alice@example.com", "display_name": "Alice A.", "is_admin": True},
        )
        principal = principal_from_access_token(token)
        assert principal == Principal(id="alice", email="alice@example.com", display_name="Alice A.", is_admin=True)

    def test_missing_claims_default_to_empty_not_admin(self):
        token = AccessToken(token="t", client_id="c", scopes=[], subject="alice")
        principal = principal_from_access_token(token)
        assert principal == Principal(id="alice")
