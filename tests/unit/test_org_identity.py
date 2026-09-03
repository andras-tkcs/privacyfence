"""Tests for org_identity.py: the OIDC relying-party helpers shared by
web/oauth_provider.py (Claude's own OAuth 2.1 dance) and web/routes_org_
identity.py (a browser's /login) -- see that module's own docstring for why
they share this code rather than each reimplementing claims-to-Principal
mapping.
"""
from __future__ import annotations

from types import SimpleNamespace

import jwt
import pytest
import requests
from cryptography.hazmat.primitives.asymmetric import rsa

from privacyfence import org_identity as oi
from privacyfence.principal import Principal


@pytest.fixture(scope="module")
def rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _idp(**overrides) -> oi.IdpConfig:
    defaults = dict(
        issuer="https://idp.example.com",
        client_id="privacyfence",
        client_secret="s3cr3t",
        authorization_endpoint="https://idp.example.com/authorize",
        token_endpoint="https://idp.example.com/token",
        jwks_uri="https://idp.example.com/jwks",
    )
    defaults.update(overrides)
    return oi.IdpConfig(**defaults)


def _sign(claims: dict, private_key) -> str:
    return jwt.encode(claims, private_key, algorithm="RS256")


class FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._json


class TestDiscoverIdp:
    def test_fetches_the_well_known_document(self, monkeypatch):
        calls = []

        def fake_get(url, timeout):
            calls.append((url, timeout))
            return FakeResponse({"authorization_endpoint": "a", "token_endpoint": "t", "jwks_uri": "j"})

        monkeypatch.setattr(oi.requests, "get", fake_get)
        result = oi.discover_idp("https://idp.example.com")

        assert calls == [("https://idp.example.com/.well-known/openid-configuration", oi._HTTP_TIMEOUT_SECONDS)]
        assert result["jwks_uri"] == "j"

    def test_strips_a_trailing_slash_on_the_issuer(self, monkeypatch):
        calls = []
        monkeypatch.setattr(oi.requests, "get", lambda url, timeout: calls.append(url) or FakeResponse({}))
        oi.discover_idp("https://idp.example.com/")
        assert calls == ["https://idp.example.com/.well-known/openid-configuration"]

    def test_raises_on_an_error_status(self, monkeypatch):
        monkeypatch.setattr(oi.requests, "get", lambda url, timeout: FakeResponse({}, status_code=500))
        with pytest.raises(requests.HTTPError):
            oi.discover_idp("https://idp.example.com")


class TestIdpConfigFromOrgConfig:
    def test_none_when_no_idp_section(self, monkeypatch):
        assert oi.IdpConfig.from_org_config({}) is None

    def test_none_when_issuer_missing(self, monkeypatch):
        assert oi.IdpConfig.from_org_config({"idp": {"client_id": "x"}}) is None

    def test_none_when_client_id_missing(self, monkeypatch):
        assert oi.IdpConfig.from_org_config({"idp": {"issuer": "https://idp.example.com"}}) is None

    def test_builds_config_via_discovery(self, monkeypatch):
        monkeypatch.setattr(
            oi, "discover_idp",
            lambda issuer: {"authorization_endpoint": "a", "token_endpoint": "t", "jwks_uri": "j"},
        )
        config = oi.IdpConfig.from_org_config({
            "idp": {
                "issuer": "https://idp.example.com", "client_id": "cid", "client_secret": "sec",
                "admin_group_claim": "groups", "admin_group_values": ["admins"],
            },
        })
        assert config == oi.IdpConfig(
            issuer="https://idp.example.com", client_id="cid", client_secret="sec",
            authorization_endpoint="a", token_endpoint="t", jwks_uri="j",
            admin_group_claim="groups", admin_group_values=("admins",),
        )

    def test_admin_group_defaults_are_fail_closed(self, monkeypatch):
        monkeypatch.setattr(
            oi, "discover_idp",
            lambda issuer: {"authorization_endpoint": "a", "token_endpoint": "t", "jwks_uri": "j"},
        )
        config = oi.IdpConfig.from_org_config({"idp": {"issuer": "https://idp.example.com", "client_id": "cid"}})
        assert config.admin_group_claim == ""
        assert config.admin_group_values == ()


class TestGeneratePkcePair:
    def test_challenge_is_derived_from_the_verifier(self):
        verifier, challenge = oi.generate_pkce_pair()
        assert verifier and challenge
        assert verifier != challenge

    def test_two_calls_produce_different_pairs(self):
        v1, c1 = oi.generate_pkce_pair()
        v2, c2 = oi.generate_pkce_pair()
        assert v1 != v2
        assert c1 != c2


class TestBuildAuthorizationUrl:
    def test_carries_every_required_param(self):
        url = oi.build_authorization_url(
            _idp(), redirect_uri="https://pf.example.com/oauth/idp/callback",
            state="st4te", code_challenge="ch4ll", nonce="n0nce",
        )
        assert url.startswith("https://idp.example.com/authorize?")
        for fragment in (
            "response_type=code", "client_id=privacyfence",
            "state=st4te", "code_challenge=ch4ll", "code_challenge_method=S256", "nonce=n0nce",
        ):
            assert fragment in url

    def test_appends_to_an_endpoint_that_already_has_a_query_string(self):
        url = oi.build_authorization_url(
            _idp(authorization_endpoint="https://idp.example.com/authorize?tenant=acme"),
            redirect_uri="https://pf.example.com/cb", state="s", code_challenge="c", nonce="n",
        )
        assert url.startswith("https://idp.example.com/authorize?tenant=acme&")


class TestExchangeCodeForTokens:
    def test_posts_the_expected_form_body(self, monkeypatch):
        captured = {}

        def fake_post(url, data, timeout):
            captured["url"] = url
            captured["data"] = data
            return FakeResponse({"id_token": "abc"})

        monkeypatch.setattr(oi.requests, "post", fake_post)
        result = oi.exchange_code_for_tokens(
            _idp(), code="c0de", redirect_uri="https://pf.example.com/cb", code_verifier="v3rifier",
        )

        assert result == {"id_token": "abc"}
        assert captured["url"] == "https://idp.example.com/token"
        assert captured["data"]["grant_type"] == "authorization_code"
        assert captured["data"]["code"] == "c0de"
        assert captured["data"]["code_verifier"] == "v3rifier"
        assert captured["data"]["client_secret"] == "s3cr3t"

    def test_omits_client_secret_when_the_idp_config_has_none(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(oi.requests, "post", lambda url, data, timeout: captured.update(data) or FakeResponse({}))
        oi.exchange_code_for_tokens(
            _idp(client_secret=""), code="c", redirect_uri="https://pf.example.com/cb", code_verifier="v",
        )
        assert "client_secret" not in captured

    def test_raises_on_an_error_status(self, monkeypatch):
        monkeypatch.setattr(oi.requests, "post", lambda url, data, timeout: FakeResponse({}, status_code=400))
        with pytest.raises(requests.HTTPError):
            oi.exchange_code_for_tokens(_idp(), code="c", redirect_uri="https://pf.example.com/cb", code_verifier="v")


class TestVerifyIdToken:
    def _patch_jwks(self, monkeypatch, public_key):
        monkeypatch.setattr(
            oi.PyJWKClient, "get_signing_key_from_jwt", lambda self, token: SimpleNamespace(key=public_key),
        )

    def test_valid_token_returns_claims(self, monkeypatch, rsa_keypair):
        private_key, public_key = rsa_keypair
        self._patch_jwks(monkeypatch, public_key)
        idp = _idp()
        token = _sign(
            {
                "sub": "alice", "email": "alice@example.com", "aud": idp.client_id, "iss": idp.issuer,
                "iat": 1, "exp": 9999999999, "nonce": "expected-nonce",
            },
            private_key,
        )

        claims = oi.verify_id_token(idp, token, nonce="expected-nonce")

        assert claims["sub"] == "alice"
        assert claims["email"] == "alice@example.com"

    def test_wrong_audience_is_rejected(self, monkeypatch, rsa_keypair):
        private_key, public_key = rsa_keypair
        self._patch_jwks(monkeypatch, public_key)
        idp = _idp()
        token = _sign(
            {"sub": "alice", "aud": "someone-else", "iss": idp.issuer, "iat": 1, "exp": 9999999999, "nonce": "n"},
            private_key,
        )
        with pytest.raises(jwt.InvalidAudienceError):
            oi.verify_id_token(idp, token, nonce="n")

    def test_wrong_issuer_is_rejected(self, monkeypatch, rsa_keypair):
        private_key, public_key = rsa_keypair
        self._patch_jwks(monkeypatch, public_key)
        idp = _idp()
        token = _sign(
            {"sub": "alice", "aud": idp.client_id, "iss": "https://not-the-idp.example.com",
             "iat": 1, "exp": 9999999999, "nonce": "n"},
            private_key,
        )
        with pytest.raises(jwt.InvalidIssuerError):
            oi.verify_id_token(idp, token, nonce="n")

    def test_expired_token_is_rejected(self, monkeypatch, rsa_keypair):
        private_key, public_key = rsa_keypair
        self._patch_jwks(monkeypatch, public_key)
        idp = _idp()
        token = _sign(
            {"sub": "alice", "aud": idp.client_id, "iss": idp.issuer, "iat": 1, "exp": 1, "nonce": "n"},
            private_key,
        )
        with pytest.raises(jwt.ExpiredSignatureError):
            oi.verify_id_token(idp, token, nonce="n")

    def test_mismatched_nonce_is_rejected(self, monkeypatch, rsa_keypair):
        private_key, public_key = rsa_keypair
        self._patch_jwks(monkeypatch, public_key)
        idp = _idp()
        token = _sign(
            {"sub": "alice", "aud": idp.client_id, "iss": idp.issuer, "iat": 1, "exp": 9999999999, "nonce": "actual"},
            private_key,
        )
        with pytest.raises(jwt.InvalidTokenError):
            oi.verify_id_token(idp, token, nonce="expected")

    def test_signature_forged_with_a_different_key_is_rejected(self, monkeypatch, rsa_keypair):
        _private_key, public_key = rsa_keypair
        other_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._patch_jwks(monkeypatch, public_key)
        idp = _idp()
        token = _sign(
            {"sub": "alice", "aud": idp.client_id, "iss": idp.issuer, "iat": 1, "exp": 9999999999, "nonce": "n"},
            other_private_key,
        )
        with pytest.raises(jwt.InvalidSignatureError):
            oi.verify_id_token(idp, token, nonce="n")

    def test_missing_sub_claim_is_rejected(self, monkeypatch, rsa_keypair):
        private_key, public_key = rsa_keypair
        self._patch_jwks(monkeypatch, public_key)
        idp = _idp()
        token = _sign(
            {"aud": idp.client_id, "iss": idp.issuer, "iat": 1, "exp": 9999999999, "nonce": "n"}, private_key,
        )
        with pytest.raises(jwt.MissingRequiredClaimError):
            oi.verify_id_token(idp, token, nonce="n")


class TestPrincipalFromClaims:
    def test_maps_sub_email_and_name(self):
        p = oi.principal_from_claims({"sub": "alice-id", "email": "alice@example.com", "name": "Alice A."}, _idp())
        assert p == Principal(id="alice-id", email="alice@example.com", display_name="Alice A.")

    def test_falls_back_to_preferred_username_then_email_then_sub_for_display_name(self):
        idp = _idp()
        assert oi.principal_from_claims({"sub": "s", "preferred_username": "aa"}, idp).display_name == "aa"
        assert oi.principal_from_claims({"sub": "s", "email": "a@x.com"}, idp).display_name == "a@x.com"
        assert oi.principal_from_claims({"sub": "s"}, idp).display_name == "s"

    def test_raises_without_a_sub_claim(self):
        with pytest.raises(ValueError):
            oi.principal_from_claims({"email": "a@x.com"}, _idp())

    def test_unsafe_sub_is_hashed_into_a_safe_principal_id(self):
        p = oi.principal_from_claims({"sub": "cn=alice,dc=example,dc=com"}, _idp())
        assert p.id.startswith("idp-")
        assert p.id != "cn=alice,dc=example,dc=com"

    def test_not_admin_when_no_admin_group_claim_configured(self):
        p = oi.principal_from_claims({"sub": "s", "groups": ["admins"]}, _idp())
        assert p.is_admin is False

    def test_admin_when_a_configured_group_is_present(self):
        idp = _idp(admin_group_claim="groups", admin_group_values=("admins", "it"))
        p = oi.principal_from_claims({"sub": "s", "groups": ["engineers", "admins"]}, idp)
        assert p.is_admin is True

    def test_not_admin_when_configured_group_is_absent(self):
        idp = _idp(admin_group_claim="groups", admin_group_values=("admins",))
        p = oi.principal_from_claims({"sub": "s", "groups": ["engineers"]}, idp)
        assert p.is_admin is False

    def test_admin_group_claim_as_a_bare_string_not_a_list(self):
        idp = _idp(admin_group_claim="role", admin_group_values=("admin",))
        p = oi.principal_from_claims({"sub": "s", "role": "admin"}, idp)
        assert p.is_admin is True
