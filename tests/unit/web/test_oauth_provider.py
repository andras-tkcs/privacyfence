"""Tests for web/oauth_provider.py's OrgOAuthProvider -- PrivacyFence's own
minimal OAuth 2.1 authorization server (P7).

The IdP leg itself (org_identity.exchange_code_for_tokens/verify_id_token)
is monkeypatched throughout -- it has its own thorough tests
(test_org_identity.py); what's under test here is the provider's own
bookkeeping: client registration/persistence, the pending-authorization ->
IdP-callback -> issued-code dance, and code/token/refresh/revoke lifecycle.
"""
from __future__ import annotations

import pytest
from mcp.server.auth.provider import AuthorizationParams, RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from privacyfence import org_identity as oi
from privacyfence.web import oauth_provider as op

IDP_CALLBACK_URL = "https://pf.example.com/oauth/idp/callback"


def _idp() -> oi.IdpConfig:
    return oi.IdpConfig(
        issuer="https://idp.example.com", client_id="privacyfence", client_secret="s3cr3t",
        authorization_endpoint="https://idp.example.com/authorize",
        token_endpoint="https://idp.example.com/token", jwks_uri="https://idp.example.com/jwks",
    )


def _client_info(client_id="claude", redirect_uri="https://claude.example.com/callback") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id, redirect_uris=[AnyUrl(redirect_uri)],
        token_endpoint_auth_method="none",
    )


def _params(*, state="orig-state", redirect_uri="https://claude.example.com/callback") -> AuthorizationParams:
    return AuthorizationParams(
        state=state, scopes=["mcp"], code_challenge="claude-challenge",
        redirect_uri=AnyUrl(redirect_uri), redirect_uri_provided_explicitly=True, resource=None,
    )


def _provider(tmp_path, monkeypatch) -> op.OrgOAuthProvider:
    monkeypatch.setattr(op, "_clients_file_path", lambda: str(tmp_path / "oauth_clients.json"))
    return op.OrgOAuthProvider(_idp(), idp_callback_url=IDP_CALLBACK_URL)


def _patch_idp_exchange(monkeypatch, *, claims: dict):
    monkeypatch.setattr(op.org_identity, "exchange_code_for_tokens", lambda *a, **kw: {"id_token": "opaque"})
    monkeypatch.setattr(
        op.org_identity, "verify_id_token", lambda idp, token, *, nonce: {**claims, "nonce": nonce},
    )


async def _drive_full_flow(provider: op.OrgOAuthProvider, monkeypatch, client, *, claims: dict, params=None):
    """register -> authorize -> IdP callback -> exchange code -> OAuthToken."""
    await provider.register_client(client)
    params = params or _params()
    _patch_idp_exchange(monkeypatch, claims=claims)
    auth_url = await provider.authorize(client, params)
    import urllib.parse as up
    state = dict(up.parse_qsl(up.urlparse(auth_url).query))["state"]
    redirect_url = await provider.handle_idp_callback(state=state, code="idp-code")
    qs = dict(up.parse_qsl(up.urlparse(redirect_url).query))
    auth_code = await provider.load_authorization_code(client, qs["code"])
    tokens = await provider.exchange_authorization_code(client, auth_code)
    return qs, auth_code, tokens


class TestClientRegistration:
    async def test_register_then_get_round_trips(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        client = _client_info()
        await provider.register_client(client)
        got = await provider.get_client("claude")
        assert got.client_id == "claude"

    async def test_unknown_client_returns_none(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        assert await provider.get_client("nope") is None

    async def test_persists_to_disk(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        await provider.register_client(_client_info())
        assert (tmp_path / "oauth_clients.json").exists()

    async def test_a_fresh_provider_loads_clients_registered_by_a_previous_one(self, tmp_path, monkeypatch):
        first = _provider(tmp_path, monkeypatch)
        await first.register_client(_client_info())

        second = _provider(tmp_path, monkeypatch)
        got = await second.get_client("claude")
        assert got is not None
        assert got.client_id == "claude"

    async def test_corrupt_clients_file_is_tolerated(self, tmp_path, monkeypatch):
        (tmp_path / "oauth_clients.json").write_text("not valid json{{{")
        provider = _provider(tmp_path, monkeypatch)  # must not raise
        assert await provider.get_client("claude") is None


class TestAuthorizeAndIdpCallback:
    async def test_authorize_redirects_to_the_idp(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        client = _client_info()
        await provider.register_client(client)

        url = await provider.authorize(client, _params())

        assert url.startswith("https://idp.example.com/authorize?")

    async def test_authorize_url_carries_the_fixed_idp_callback_redirect_uri(self, tmp_path, monkeypatch):
        import urllib.parse as up
        provider = _provider(tmp_path, monkeypatch)
        client = _client_info()
        await provider.register_client(client)

        url = await provider.authorize(client, _params())
        qs = dict(up.parse_qsl(up.urlparse(url).query))
        assert qs["redirect_uri"] == IDP_CALLBACK_URL

    async def test_stale_pending_authorizations_are_pruned_on_the_next_authorize_call(self, tmp_path, monkeypatch):
        import urllib.parse as up
        provider = _provider(tmp_path, monkeypatch)
        client = _client_info()
        await provider.register_client(client)
        fake_now = [1000.0]
        monkeypatch.setattr(op.time, "time", lambda: fake_now[0])

        old_url = await provider.authorize(client, _params())
        old_state = dict(up.parse_qsl(up.urlparse(old_url).query))["state"]

        fake_now[0] += op._PENDING_AUTHORIZATION_TTL_SECONDS + 1
        await provider.authorize(client, _params())  # triggers _prune_pending()

        with pytest.raises(ValueError):
            await provider.handle_idp_callback(state=old_state, code="idp-code")

    async def test_idp_callback_redirects_to_the_original_clients_redirect_uri(self, tmp_path, monkeypatch):
        import urllib.parse as up
        provider = _provider(tmp_path, monkeypatch)
        client = _client_info(redirect_uri="https://claude.example.com/callback")
        await provider.register_client(client)
        _patch_idp_exchange(monkeypatch, claims={"sub": "alice"})
        auth_url = await provider.authorize(client, _params(state="claudes-own-state"))
        state = dict(up.parse_qsl(up.urlparse(auth_url).query))["state"]

        redirect_url = await provider.handle_idp_callback(state=state, code="idp-code")

        parsed = up.urlparse(redirect_url)
        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://claude.example.com/callback"
        qs = dict(up.parse_qsl(parsed.query))
        assert qs["state"] == "claudes-own-state"  # the ORIGINAL client's state, not PrivacyFence's own
        assert "code" in qs

    async def test_idp_callback_with_unknown_state_raises(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        with pytest.raises(ValueError):
            await provider.handle_idp_callback(state="never-issued", code="c")

    async def test_idp_callback_state_is_single_use(self, tmp_path, monkeypatch):
        import urllib.parse as up
        provider = _provider(tmp_path, monkeypatch)
        client = _client_info()
        await provider.register_client(client)
        _patch_idp_exchange(monkeypatch, claims={"sub": "alice"})
        auth_url = await provider.authorize(client, _params())
        state = dict(up.parse_qsl(up.urlparse(auth_url).query))["state"]

        await provider.handle_idp_callback(state=state, code="idp-code")
        with pytest.raises(ValueError):
            await provider.handle_idp_callback(state=state, code="idp-code")

    async def test_idp_callback_missing_id_token_raises(self, tmp_path, monkeypatch):
        import urllib.parse as up
        provider = _provider(tmp_path, monkeypatch)
        client = _client_info()
        await provider.register_client(client)
        monkeypatch.setattr(op.org_identity, "exchange_code_for_tokens", lambda *a, **kw: {})
        auth_url = await provider.authorize(client, _params())
        state = dict(up.parse_qsl(up.urlparse(auth_url).query))["state"]
        with pytest.raises(ValueError):
            await provider.handle_idp_callback(state=state, code="idp-code")


class TestAuthorizationCodeExchange:
    async def test_load_authorization_code_carries_the_resolved_principal_as_subject(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        client = _client_info()
        qs, auth_code, _tokens = await _drive_full_flow(provider, monkeypatch, client, claims={"sub": "alice"})
        assert auth_code.subject == "alice"

    async def test_load_authorization_code_for_a_different_client_id_returns_none(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        client = _client_info()
        await provider.register_client(client)
        other_client = _client_info(client_id="someone-else")
        _patch_idp_exchange(monkeypatch, claims={"sub": "alice"})
        auth_url = await provider.authorize(client, _params())
        import urllib.parse as up
        state = dict(up.parse_qsl(up.urlparse(auth_url).query))["state"]
        redirect_url = await provider.handle_idp_callback(state=state, code="idp-code")
        qs = dict(up.parse_qsl(up.urlparse(redirect_url).query))

        assert await provider.load_authorization_code(other_client, qs["code"]) is None

    async def test_exchange_authorization_code_mints_an_access_and_refresh_token(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        client = _client_info()
        _qs, _auth_code, tokens = await _drive_full_flow(provider, monkeypatch, client, claims={"sub": "alice"})
        assert tokens.access_token
        assert tokens.refresh_token
        assert tokens.token_type == "Bearer"

    async def test_exchanged_access_token_verifies_and_carries_the_principal(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        client = _client_info()
        _qs, _auth_code, tokens = await _drive_full_flow(
            provider, monkeypatch, client, claims={"sub": "alice", "email": "alice@example.com", "name": "Alice"},
        )
        verified = await provider.verify_token(tokens.access_token)
        assert verified is not None
        assert verified.subject == "alice"
        assert verified.claims["email"] == "alice@example.com"
        assert verified.claims["display_name"] == "Alice"

    async def test_exchange_is_single_use(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        client = _client_info()
        qs, auth_code, _tokens = await _drive_full_flow(provider, monkeypatch, client, claims={"sub": "alice"})
        with pytest.raises(TokenError):
            await provider.exchange_authorization_code(client, auth_code)

    async def test_admin_claim_flows_through_to_the_access_token(self, tmp_path, monkeypatch):
        idp = oi.IdpConfig(
            issuer="https://idp.example.com", client_id="privacyfence", client_secret="s",
            authorization_endpoint="https://idp.example.com/authorize",
            token_endpoint="https://idp.example.com/token", jwks_uri="https://idp.example.com/jwks",
            admin_group_claim="groups", admin_group_values=("admins",),
        )
        monkeypatch.setattr(op, "_clients_file_path", lambda: str(tmp_path / "oauth_clients.json"))
        provider = op.OrgOAuthProvider(idp, idp_callback_url=IDP_CALLBACK_URL)
        client = _client_info()
        _qs, _auth_code, tokens = await _drive_full_flow(
            provider, monkeypatch, client, claims={"sub": "alice", "groups": ["admins"]},
        )
        verified = await provider.verify_token(tokens.access_token)
        assert verified.claims["is_admin"] is True


class TestVerifyToken:
    async def test_unknown_token_returns_none(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        assert await provider.verify_token("not-a-real-token") is None

    async def test_expired_access_token_returns_none(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        client = _client_info()
        _qs, _auth_code, tokens = await _drive_full_flow(provider, monkeypatch, client, claims={"sub": "alice"})
        provider._access_tokens[tokens.access_token].expires_at = 1  # force expiry
        assert await provider.verify_token(tokens.access_token) is None

    async def test_verify_token_delegates_to_load_access_token(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        client = _client_info()
        _qs, _auth_code, tokens = await _drive_full_flow(provider, monkeypatch, client, claims={"sub": "alice"})
        via_verify = await provider.verify_token(tokens.access_token)
        via_load = await provider.load_access_token(tokens.access_token)
        assert via_verify == via_load


class TestRefreshToken:
    async def test_refresh_mints_new_tokens_carrying_the_same_principal(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        client = _client_info()
        _qs, _auth_code, tokens = await _drive_full_flow(
            provider, monkeypatch, client, claims={"sub": "alice", "email": "alice@example.com"},
        )
        loaded = await provider.load_refresh_token(client, tokens.refresh_token)
        new_tokens = await provider.exchange_refresh_token(client, loaded, [])

        assert new_tokens.access_token != tokens.access_token
        verified = await provider.verify_token(new_tokens.access_token)
        assert verified.subject == "alice"
        assert verified.claims["email"] == "alice@example.com"

    async def test_old_refresh_token_is_invalidated_after_rotation(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        client = _client_info()
        _qs, _auth_code, tokens = await _drive_full_flow(provider, monkeypatch, client, claims={"sub": "alice"})
        loaded = await provider.load_refresh_token(client, tokens.refresh_token)
        await provider.exchange_refresh_token(client, loaded, [])

        assert await provider.load_refresh_token(client, tokens.refresh_token) is None

    async def test_old_access_token_is_invalidated_after_rotation(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        client = _client_info()
        _qs, _auth_code, tokens = await _drive_full_flow(provider, monkeypatch, client, claims={"sub": "alice"})
        loaded = await provider.load_refresh_token(client, tokens.refresh_token)
        await provider.exchange_refresh_token(client, loaded, [])

        assert await provider.verify_token(tokens.access_token) is None

    async def test_refresh_token_for_a_different_client_returns_none(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        client = _client_info()
        _qs, _auth_code, tokens = await _drive_full_flow(provider, monkeypatch, client, claims={"sub": "alice"})
        other_client = _client_info(client_id="someone-else")
        assert await provider.load_refresh_token(other_client, tokens.refresh_token) is None


class TestRevokeToken:
    async def test_revoking_an_access_token_invalidates_it(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        client = _client_info()
        _qs, _auth_code, tokens = await _drive_full_flow(provider, monkeypatch, client, claims={"sub": "alice"})
        at = await provider.load_access_token(tokens.access_token)

        await provider.revoke_token(at)

        assert await provider.verify_token(tokens.access_token) is None

    async def test_revoking_an_access_token_also_revokes_its_paired_refresh_token(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        client = _client_info()
        _qs, _auth_code, tokens = await _drive_full_flow(provider, monkeypatch, client, claims={"sub": "alice"})
        at = await provider.load_access_token(tokens.access_token)

        await provider.revoke_token(at)

        assert await provider.load_refresh_token(client, tokens.refresh_token) is None

    async def test_revoking_a_refresh_token_also_revokes_its_paired_access_token(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        client = _client_info()
        _qs, _auth_code, tokens = await _drive_full_flow(provider, monkeypatch, client, claims={"sub": "alice"})
        rt = await provider.load_refresh_token(client, tokens.refresh_token)

        await provider.revoke_token(rt)

        assert await provider.verify_token(tokens.access_token) is None

    async def test_revoking_an_unknown_token_is_a_no_op(self, tmp_path, monkeypatch):
        provider = _provider(tmp_path, monkeypatch)
        await provider.revoke_token(RefreshToken(token="never-issued", client_id="c", scopes=[]))  # must not raise
