"""Tests for the shared browser-loopback OAuth helper (run_browser_oauth).

Since the local redirect handler is a real http.server.HTTPServer bound to
127.0.0.1, these tests exercise it for real: the injectable `open_browser`
callback -- instead of actually opening a browser -- makes a real HTTP GET
request straight to the local redirect_uri with whatever query string the
test scenario needs (valid code+state, wrong state, provider error, no
code), simulating exactly what a real browser would deliver after the user
completes the provider's consent screen. This is the same trust boundary
that matters most here: CSRF state verification and PKCE generation.
"""
from __future__ import annotations

import base64
import hashlib
import socket

import pytest
import requests

from privacyfence.oauth_loopback import OAuthLoopbackError, _make_pkce_pair, run_browser_oauth

# A loopback redirect must never be routed through an HTTP(S)_PROXY that
# happens to be set in the environment (common on hosted CI runners) --
# requests/urllib3 honor those env vars by default, and a stalled proxy-
# tunnel negotiation to an unreachable proxy isn't reliably bounded by the
# per-call `timeout=` argument, which previously caused this file's tests to
# hang until pytest-timeout killed them rather than failing fast. A real
# browser wouldn't proxy a localhost redirect either, so this also matches
# reality, not just test convenience.
_NO_PROXY_SESSION = requests.Session()
_NO_PROXY_SESSION.trust_env = False


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Flow:
    """Builds the build_authorize_url/exchange pair and captures everything
    they were called with, so assertions can inspect state/PKCE/redirect_uri
    without the test needing to parse the authorize URL itself."""

    def __init__(self):
        self.captured: dict = {}
        self.exchange_result: dict = {"access_token": "tok-123"}
        self.exchange_error: Exception | None = None

    def build_authorize_url(self, redirect_uri: str, state: str, code_challenge: str) -> str:
        self.captured["redirect_uri"] = redirect_uri
        self.captured["state"] = state
        self.captured["code_challenge"] = code_challenge
        return f"https://provider.example/authorize?state={state}"

    def exchange(self, code: str, redirect_uri: str, code_verifier: str) -> dict:
        self.captured["exchanged_code"] = code
        self.captured["exchanged_redirect_uri"] = redirect_uri
        self.captured["code_verifier"] = code_verifier
        if self.exchange_error:
            raise self.exchange_error
        return self.exchange_result

    def opener_for(self, query: dict | None = None, respond: bool = True):
        """An open_browser stand-in that hits our own local redirect_uri with
        the given query params (or the correct code+state if None)."""
        def opener(url: str) -> bool:
            if not respond:
                return False
            params = query if query is not None else {"code": "auth-code-123", "state": self.captured["state"]}
            _NO_PROXY_SESSION.get(self.captured["redirect_uri"], params=params, timeout=5)
            return True
        return opener


# ---------------------------------------------------------------------------- #
# _make_pkce_pair
# ---------------------------------------------------------------------------- #

class TestMakePkcePair:
    def test_verifier_is_url_safe_and_within_length_bounds(self):
        verifier, _ = _make_pkce_pair()
        assert 43 <= len(verifier) <= 128
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        assert set(verifier) <= allowed

    def test_challenge_is_sha256_of_verifier_base64url_no_padding(self):
        verifier, challenge = _make_pkce_pair()
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
        assert challenge == expected

    def test_successive_pairs_are_unique(self):
        pairs = {_make_pkce_pair() for _ in range(5)}
        assert len(pairs) == 5


# ---------------------------------------------------------------------------- #
# run_browser_oauth: success path + what gets passed to exchange()
# ---------------------------------------------------------------------------- #

class TestRunBrowserOauthSuccess:
    def test_successful_flow_returns_exchange_result(self):
        flow = _Flow()
        result = run_browser_oauth(
            flow.build_authorize_url, flow.exchange, port=free_port(),
            open_browser=flow.opener_for(),
        )
        assert result == {"access_token": "tok-123"}

    def test_exchange_receives_the_code_and_redirect_uri_and_verifier(self):
        flow = _Flow()
        run_browser_oauth(
            flow.build_authorize_url, flow.exchange, port=free_port(),
            open_browser=flow.opener_for(),
        )
        assert flow.captured["exchanged_code"] == "auth-code-123"
        assert flow.captured["exchanged_redirect_uri"] == flow.captured["redirect_uri"]
        assert flow.captured["code_verifier"]

    def test_redirect_uri_defaults_to_127_0_0_1(self):
        flow = _Flow()
        port = free_port()
        run_browser_oauth(flow.build_authorize_url, flow.exchange, port=port, open_browser=flow.opener_for())
        assert flow.captured["redirect_uri"] == f"http://127.0.0.1:{port}/callback"

    def test_redirect_host_override_used_in_redirect_uri(self):
        flow = _Flow()
        port = free_port()
        run_browser_oauth(
            flow.build_authorize_url, flow.exchange, port=port,
            open_browser=flow.opener_for(), redirect_host="localhost",
        )
        assert flow.captured["redirect_uri"] == f"http://localhost:{port}/callback"

    def test_custom_path_used_for_the_callback(self):
        flow = _Flow()
        port = free_port()
        run_browser_oauth(
            flow.build_authorize_url, flow.exchange, port=port,
            path="/oauth/done", open_browser=flow.opener_for(),
        )
        assert flow.captured["redirect_uri"].endswith("/oauth/done")

    def test_pkce_challenge_matches_verifier_passed_to_exchange(self):
        flow = _Flow()
        run_browser_oauth(
            flow.build_authorize_url, flow.exchange, port=free_port(),
            open_browser=flow.opener_for(),
        )
        expected_challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(flow.captured["code_verifier"].encode()).digest())
            .rstrip(b"=").decode()
        )
        assert flow.captured["code_challenge"] == expected_challenge


# ---------------------------------------------------------------------------- #
# CSRF state verification
# ---------------------------------------------------------------------------- #

class TestStateVerification:
    def test_mismatched_state_raises_without_calling_exchange(self):
        flow = _Flow()
        with pytest.raises(OAuthLoopbackError, match="state mismatch"):
            run_browser_oauth(
                flow.build_authorize_url, flow.exchange, port=free_port(),
                open_browser=flow.opener_for(query={"code": "auth-code-123", "state": "attacker-supplied"}),
            )
        assert "exchanged_code" not in flow.captured

    def test_missing_state_raises(self):
        flow = _Flow()
        with pytest.raises(OAuthLoopbackError, match="state mismatch"):
            run_browser_oauth(
                flow.build_authorize_url, flow.exchange, port=free_port(),
                open_browser=flow.opener_for(query={"code": "auth-code-123"}),
            )


# ---------------------------------------------------------------------------- #
# Provider errors / missing code
# ---------------------------------------------------------------------------- #

class TestCallbackErrorHandling:
    def test_provider_error_description_surfaced(self):
        flow = _Flow()
        with pytest.raises(OAuthLoopbackError, match="user cancelled"):
            run_browser_oauth(
                flow.build_authorize_url, flow.exchange, port=free_port(),
                open_browser=flow.opener_for(query={"error": "access_denied", "error_description": "user cancelled"}),
            )

    def test_provider_error_without_description_falls_back_to_error_code(self):
        flow = _Flow()
        with pytest.raises(OAuthLoopbackError, match="access_denied"):
            run_browser_oauth(
                flow.build_authorize_url, flow.exchange, port=free_port(),
                open_browser=flow.opener_for(query={"error": "access_denied"}),
            )

    def test_missing_code_without_error_raises(self):
        flow = _Flow()
        def opener(url):
            _NO_PROXY_SESSION.get(flow.captured["redirect_uri"], params={"state": flow.captured["state"]}, timeout=5)
            return True
        with pytest.raises(OAuthLoopbackError, match="no authorization code"):
            run_browser_oauth(flow.build_authorize_url, flow.exchange, port=free_port(), open_browser=opener)

    def test_unknown_path_on_local_server_gets_404_and_flow_still_times_out(self):
        flow = _Flow()
        def opener(url):
            base = flow.captured["redirect_uri"].rsplit("/", 1)[0]
            resp = _NO_PROXY_SESSION.get(f"{base}/not-the-callback-path", timeout=5)
            assert resp.status_code == 404
            return True
        with pytest.raises(OAuthLoopbackError, match="Timed out"):
            run_browser_oauth(
                flow.build_authorize_url, flow.exchange, port=free_port(),
                open_browser=opener, timeout=0.3,
            )


# ---------------------------------------------------------------------------- #
# Browser-open failure / timeout / port conflict
# ---------------------------------------------------------------------------- #

class TestOpenBrowserAndTimeoutFailures:
    def test_browser_fails_to_open_raises_with_manual_url(self):
        flow = _Flow()
        with pytest.raises(OAuthLoopbackError, match="Could not open a browser"):
            run_browser_oauth(
                flow.build_authorize_url, flow.exchange, port=free_port(),
                open_browser=flow.opener_for(respond=False),
            )

    def test_timeout_when_browser_never_completes_the_flow(self):
        flow = _Flow()
        with pytest.raises(OAuthLoopbackError, match="Timed out"):
            run_browser_oauth(
                flow.build_authorize_url, flow.exchange, port=free_port(),
                open_browser=lambda url: True,  # "opens" but never hits the callback
                timeout=0.2,
            )

    def test_port_already_in_use_raises_actionable_error(self):
        port = free_port()
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        try:
            flow = _Flow()
            with pytest.raises(OAuthLoopbackError, match="already in progress"):
                run_browser_oauth(
                    flow.build_authorize_url, flow.exchange, port=port,
                    open_browser=flow.opener_for(),
                )
        finally:
            blocker.close()


# ---------------------------------------------------------------------------- #
# Exchange failures propagate (not swallowed by the loopback helper)
# ---------------------------------------------------------------------------- #

class TestExchangeFailurePropagates:
    def test_exchange_raising_is_not_caught_here(self):
        flow = _Flow()
        flow.exchange_error = ValueError("token endpoint returned 500")
        with pytest.raises(ValueError, match="token endpoint returned 500"):
            run_browser_oauth(
                flow.build_authorize_url, flow.exchange, port=free_port(),
                open_browser=flow.opener_for(),
            )
