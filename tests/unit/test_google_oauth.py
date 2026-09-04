"""Tests for the shared Google OAuth 2.0 server-redirect helper (P8, docs/
https-connector-refactor-plan.md §9.3) -- used only by web/routes_connect.py's
org-mode routes. Local mode's own InstalledAppFlow-based authorize_interactive
methods on GmailClient/DriveClient/etc. are untouched by this module and keep
their own existing test coverage.

``google_auth_oauthlib.flow.Flow``'s own network calls (``fetch_token``) are
mocked at the ``requests``/session layer it ultimately uses via
``requests_oauthlib.OAuth2Session`` -- the same "mock the transport, run the
real client logic" posture test_atlassian_oauth.py/test_slack_client.py/
test_salesforce_client.py already take for their own OAuth exchanges.
"""
from __future__ import annotations

import json
import stat
from unittest.mock import MagicMock, patch

from privacyfence import google_oauth

_CLIENT_CONFIG = {
    "client_id": "cid",
    "client_secret": "csecret",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "redirect_uris": ["https://pf.example.com/oauth/callback/gmail"],
}

REDIRECT_URI = "https://pf.example.com/oauth/callback/gmail"


class TestWebClientConfig:
    def test_missing_client_id_or_secret_is_empty(self):
        assert google_oauth.web_client_config({}) == {}
        assert google_oauth.web_client_config({"client_id": "x"}) == {}

    def test_missing_auth_or_token_uri_is_empty(self):
        assert google_oauth.web_client_config({"client_id": "x", "client_secret": "y"}) == {}

    def test_wraps_flat_section_under_web_key(self):
        wrapped = google_oauth.web_client_config(_CLIENT_CONFIG)
        assert wrapped == {"web": _CLIENT_CONFIG}


class TestAuthorizeUrl:
    def test_builds_a_real_google_authorize_url_with_pkce_and_state(self):
        wrapped = google_oauth.web_client_config(_CLIENT_CONFIG)
        url, code_verifier = google_oauth.authorize_url(wrapped, ["scope-a"], REDIRECT_URI, "state-123")

        assert url.startswith("https://accounts.google.com/o/oauth2/auth?")
        assert "state=state-123" in url
        assert "code_challenge=" in url
        assert "code_challenge_method=S256" in url
        assert "access_type=offline" in url
        assert "prompt=consent" in url
        assert len(code_verifier) >= 43  # RFC 7636's own minimum verifier length

    def test_each_call_gets_a_fresh_verifier(self):
        wrapped = google_oauth.web_client_config(_CLIENT_CONFIG)
        _, verifier1 = google_oauth.authorize_url(wrapped, ["s"], REDIRECT_URI, "state-1")
        _, verifier2 = google_oauth.authorize_url(wrapped, ["s"], REDIRECT_URI, "state-2")
        assert verifier1 != verifier2


def _fake_fetch_token(token: dict):
    """A fetch_token replacement that also does what the real
    OAuth2Session.fetch_token does as a side effect: populate
    self.oauth2session.token, which credentials_from_session (google_
    auth_oauthlib.helpers) requires to be set before Flow.credentials can
    build a real Credentials object. A bare return_value= mock skips that
    side effect entirely and credentials_from_session raises instead."""

    def fetch_token(self, **kwargs):
        self.oauth2session.token = token
        return token

    return fetch_token


class TestExchangeCode:
    def test_successful_exchange_returns_credentials_with_the_verifier_used(self):
        wrapped = google_oauth.web_client_config(_CLIENT_CONFIG)
        token = {
            "access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600,
            "expires_at": 9999999999, "scope": "scope-a", "token_type": "Bearer",
        }
        with patch("google_auth_oauthlib.flow.Flow.fetch_token", _fake_fetch_token(token)) as fetch:
            creds = google_oauth.exchange_code(wrapped, ["scope-a"], REDIRECT_URI, "auth-code", "verifier-abc")

        assert creds.token == "at-1"
        assert creds.refresh_token == "rt-1"

    def test_provider_failure_becomes_google_oauth_error(self):
        wrapped = google_oauth.web_client_config(_CLIENT_CONFIG)
        with patch("google_auth_oauthlib.flow.Flow.fetch_token", side_effect=RuntimeError("invalid_grant")):
            try:
                google_oauth.exchange_code(wrapped, ["scope-a"], REDIRECT_URI, "auth-code", "verifier-abc")
            except google_oauth.GoogleOAuthError as exc:
                assert "invalid_grant" in str(exc)
            else:
                raise AssertionError("expected GoogleOAuthError")

    def test_uses_the_persisted_code_verifier_not_a_fresh_one(self):
        # exchange_code must construct its Flow with autogenerate_code_verifier=False
        # and the caller-supplied verifier -- otherwise the PKCE check on Google's
        # own token endpoint would fail for every real exchange (the verifier used
        # at fetch_token time must match the challenge sent at authorize time).
        wrapped = google_oauth.web_client_config(_CLIENT_CONFIG)
        fake_token = {"access_token": "at-1", "expires_at": 9999999999}
        with patch("google_auth_oauthlib.flow.Flow.fetch_token", _fake_fetch_token(fake_token)):
            with patch.object(google_oauth.Flow, "from_client_config", wraps=google_oauth.Flow.from_client_config) as ctor:
                google_oauth.exchange_code(wrapped, ["scope-a"], REDIRECT_URI, "auth-code", "verifier-abc")
        _, kwargs = ctor.call_args
        assert kwargs["code_verifier"] == "verifier-abc"
        assert kwargs["autogenerate_code_verifier"] is False


class TestSaveCredentials:
    def test_writes_to_json_output_with_restricted_permissions(self, tmp_path):
        creds = MagicMock()
        creds.to_json.return_value = json.dumps({"token": "at-1", "refresh_token": "rt-1"})
        token_file = tmp_path / "nested" / "token.json"

        google_oauth.save_credentials(str(token_file), creds)

        saved = json.loads(token_file.read_text(encoding="utf-8"))
        assert saved == {"token": "at-1", "refresh_token": "rt-1"}
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600

    def test_chmod_failure_is_non_fatal(self, tmp_path, monkeypatch):
        creds = MagicMock()
        creds.to_json.return_value = "{}"

        def raise_chmod(*a, **kw):
            raise OSError("no chmod here")

        monkeypatch.setattr(google_oauth.os, "chmod", raise_chmod)
        google_oauth.save_credentials(str(tmp_path / "token.json"), creds)  # must not raise
