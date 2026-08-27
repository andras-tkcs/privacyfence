"""Tests for mobile_relay_client.py (issue #55).

Focus: the two things Phase 0's spike explicitly didn't do -- payload
confidentiality (AES-256-GCM under a key) and decision authenticity (HMAC
tag binding a decision to one request_id, so a device that doesn't hold the
key can't forge one, and a captured tag can't be replayed onto a different
request) -- plus relay_url_from_org_config()'s fail-closed parsing of
org_config.json's mobile_relay section, and the pairing-handshake-only
raw request/decision methods mobile_relay_pairing.py builds on.
"""
from __future__ import annotations

import base64
import json

import pytest
import requests

from privacyfence.mobile_relay_client import (
    MobileRelayClient,
    MobileRelayClientError,
    MobileRelayConfig,
    compute_auth_tag,
    decrypt_payload,
    encrypt_payload,
    pwa_release_public_key_from_org_config,
    relay_url_from_org_config,
    request_new_mailbox,
    verify_auth_tag,
)

VALID_KEY = b"0" * 32
VALID_KEY_B64 = base64.b64encode(VALID_KEY).decode("ascii")


def make_config(**overrides) -> MobileRelayConfig:
    defaults = dict(
        relay_url="https://relay.example.org:8765", mailbox_id="mbox1", token="tok1", shared_key=VALID_KEY,
    )
    defaults.update(overrides)
    return MobileRelayConfig(**defaults)


class _FakeResponse:
    def __init__(self, status_code: int, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text

    def json(self):
        return self._json_data


class _FakeSession:
    """Records every post()/get() call; returns responses/raises from queues."""

    def __init__(self, post_responses=None, get_responses=None):
        self.post_responses = list(post_responses or [])
        self.get_responses = list(get_responses or [])
        self.post_calls: list[dict] = []
        self.get_calls: list[dict] = []

    def post(self, url, params=None, json=None, timeout=None):
        self.post_calls.append({"url": url, "params": params, "json": json, "timeout": timeout})
        item = self.post_responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, params=None, timeout=None):
        self.get_calls.append({"url": url, "params": params, "timeout": timeout})
        # Repeat the last queued item once exhausted, rather than raising --
        # several tests below intentionally let the poll loop run past a
        # single response (e.g. until should_abandon()/the overall timeout
        # fires), and shouldn't need to pad the queue with N identical copies
        # just to survive that many iterations.
        item = self.get_responses.pop(0) if len(self.get_responses) > 1 else self.get_responses[0]
        if isinstance(item, Exception):
            raise item
        return item


# ---------------------------------------------------------------------------- #
# relay_url_from_org_config
# ---------------------------------------------------------------------------- #

class TestRelayUrlFromOrgConfig:
    def test_missing_section_returns_none(self):
        assert relay_url_from_org_config({}) is None

    def test_disabled_section_returns_none(self):
        org_config = {"mobile_relay": {"enabled": False, "relay_url": "https://r"}}
        assert relay_url_from_org_config(org_config) is None

    def test_missing_url_returns_none(self):
        org_config = {"mobile_relay": {"enabled": True}}
        assert relay_url_from_org_config(org_config) is None

    def test_valid_section_returns_url_with_trailing_slash_stripped(self):
        org_config = {"mobile_relay": {"enabled": True, "relay_url": "https://relay.example.org:8765/"}}
        assert relay_url_from_org_config(org_config) == "https://relay.example.org:8765"

    def test_does_not_read_mailbox_id_token_or_shared_key(self):
        """Regression guard for the Phase 1 design this replaced: those are
        per-device secrets now (mobile_relay_pairing.PairingStore), never
        org-wide config -- see relay_url_from_org_config's own docstring."""
        org_config = {"mobile_relay": {
            "enabled": True, "relay_url": "https://r",
            "mailbox_id": "should-be-ignored", "token": "should-be-ignored",
            "shared_key_base64": "should-be-ignored",
        }}
        assert relay_url_from_org_config(org_config) == "https://r"


# ---------------------------------------------------------------------------- #
# pwa_release_public_key_from_org_config
# ---------------------------------------------------------------------------- #

class TestPwaReleasePublicKeyFromOrgConfig:
    def test_missing_key_returns_none(self):
        assert pwa_release_public_key_from_org_config({"mobile_relay": {"enabled": True}}) is None

    def test_missing_section_returns_none(self):
        assert pwa_release_public_key_from_org_config({}) is None

    def test_invalid_base64_returns_none(self):
        org_config = {"mobile_relay": {"pwa_release_public_key_base64": "not valid base64!!!"}}
        assert pwa_release_public_key_from_org_config(org_config) is None

    def test_valid_key_decodes(self):
        raw_key = b"\x04" + b"\x01" * 64  # uncompressed-point-shaped, doesn't need to be a real key here
        org_config = {"mobile_relay": {
            "pwa_release_public_key_base64": base64.b64encode(raw_key).decode("ascii"),
        }}
        assert pwa_release_public_key_from_org_config(org_config) == raw_key


# ---------------------------------------------------------------------------- #
# encrypt_payload / decrypt_payload / compute_auth_tag / verify_auth_tag
# ---------------------------------------------------------------------------- #

class TestCryptoHelpers:
    def test_encrypt_then_decrypt_round_trips(self):
        payload = {"tool": "gmail.send_message", "preview": {"To": "alice@example.com"}}
        ciphertext_b64 = encrypt_payload(VALID_KEY, payload)
        assert decrypt_payload(VALID_KEY, ciphertext_b64) == payload

    def test_decrypt_with_wrong_key_fails(self):
        ciphertext_b64 = encrypt_payload(VALID_KEY, {"a": 1})
        wrong_key = b"1" * 32
        with pytest.raises(Exception):  # cryptography's InvalidTag
            decrypt_payload(wrong_key, ciphertext_b64)

    def test_verify_auth_tag_accepts_a_correctly_computed_tag(self):
        tag = compute_auth_tag(VALID_KEY, "req1", "approved")
        assert verify_auth_tag(VALID_KEY, "req1", "approved", tag) is True

    def test_verify_auth_tag_rejects_wrong_key(self):
        tag = compute_auth_tag(VALID_KEY, "req1", "approved")
        assert verify_auth_tag(b"1" * 32, "req1", "approved", tag) is False

    def test_verify_auth_tag_rejects_empty_string(self):
        assert verify_auth_tag(VALID_KEY, "req1", "approved", "") is False


# ---------------------------------------------------------------------------- #
# request_new_mailbox
# ---------------------------------------------------------------------------- #

class TestRequestNewMailbox:
    def test_returns_mailbox_id_and_token(self):
        session = _FakeSession(post_responses=[
            _FakeResponse(201, {"mailbox_id": "mbox1", "token": "tok1"}),
        ])

        mailbox_id, token = request_new_mailbox("https://relay.example.org:8765", session=session)

        assert (mailbox_id, token) == ("mbox1", "tok1")
        assert session.post_calls[0]["url"] == "https://relay.example.org:8765/pair"

    def test_strips_trailing_slash_from_relay_url(self):
        session = _FakeSession(post_responses=[_FakeResponse(201, {"mailbox_id": "m", "token": "t"})])

        request_new_mailbox("https://relay.example.org:8765/", session=session)

        assert session.post_calls[0]["url"] == "https://relay.example.org:8765/pair"

    def test_network_error_raises_client_error(self):
        session = _FakeSession(post_responses=[requests.ConnectionError("down")])
        with pytest.raises(MobileRelayClientError):
            request_new_mailbox("https://r", session=session)

    def test_non_201_status_raises_client_error(self):
        session = _FakeSession(post_responses=[_FakeResponse(500, text="oops")])
        with pytest.raises(MobileRelayClientError):
            request_new_mailbox("https://r", session=session)

    def test_malformed_response_raises_client_error(self):
        session = _FakeSession(post_responses=[_FakeResponse(201, {"unexpected": "shape"})])
        with pytest.raises(MobileRelayClientError):
            request_new_mailbox("https://r", session=session)


# ---------------------------------------------------------------------------- #
# post_request
# ---------------------------------------------------------------------------- #

class TestPostRequest:
    def test_posts_encrypted_payload_to_correct_url(self):
        session = _FakeSession(post_responses=[_FakeResponse(201)])
        client = MobileRelayClient(make_config(), session=session)

        client.post_request("req1", {"tool": "gmail.send_message"}, ttl_seconds=120)

        call = session.post_calls[0]
        assert call["url"] == "https://relay.example.org:8765/mailbox/mbox1"
        assert call["params"] == {"token": "tok1"}
        assert call["json"]["request_id"] == "req1"
        assert call["json"]["ttl_seconds"] == 120
        # The relay must never see plaintext -- the payload it was given is
        # ciphertext, not anything resembling the original dict.
        posted_payload = call["json"]["payload"]
        assert posted_payload["v"] == 1
        assert "tool" not in json.dumps(posted_payload)

    def test_ciphertext_actually_decrypts_back_to_the_original_payload(self):
        session = _FakeSession(post_responses=[_FakeResponse(201)])
        client = MobileRelayClient(make_config(), session=session)
        original = {"tool": "gmail.send_message", "preview": {"To": "alice@example.com"}}

        client.post_request("req1", original, ttl_seconds=120)

        ciphertext_b64 = session.post_calls[0]["json"]["payload"]["ciphertext"]
        assert decrypt_payload(VALID_KEY, ciphertext_b64) == original

    def test_two_calls_use_different_nonces(self):
        session = _FakeSession(post_responses=[_FakeResponse(201), _FakeResponse(201)])
        client = MobileRelayClient(make_config(), session=session)

        client.post_request("req1", {"a": 1}, ttl_seconds=10)
        client.post_request("req2", {"a": 1}, ttl_seconds=10)

        ct1 = session.post_calls[0]["json"]["payload"]["ciphertext"]
        ct2 = session.post_calls[1]["json"]["payload"]["ciphertext"]
        assert ct1 != ct2

    def test_network_error_raises_client_error(self):
        session = _FakeSession(post_responses=[requests.ConnectionError("down")])
        client = MobileRelayClient(make_config(), session=session)

        with pytest.raises(MobileRelayClientError):
            client.post_request("req1", {}, ttl_seconds=10)

    def test_non_201_status_raises_client_error(self):
        session = _FakeSession(post_responses=[_FakeResponse(403, text="forbidden")])
        client = MobileRelayClient(make_config(), session=session)

        with pytest.raises(MobileRelayClientError):
            client.post_request("req1", {}, ttl_seconds=10)


# ---------------------------------------------------------------------------- #
# poll_decision
# ---------------------------------------------------------------------------- #

def valid_auth(config: MobileRelayConfig, request_id: str, decision: str) -> str:
    return compute_auth_tag(config.shared_key, request_id, decision)


class TestPollDecision:
    def test_returns_decision_with_valid_auth_tag(self):
        config = make_config()
        auth = valid_auth(config, "req1", "approved")
        session = _FakeSession(get_responses=[
            _FakeResponse(200, {"request_id": "req1", "decision": "approved", "auth": auth}),
        ])
        client = MobileRelayClient(config, session=session)

        result = client.poll_decision("req1", overall_timeout_seconds=5)

        assert result == "approved"

    def test_missing_auth_tag_is_discarded_not_applied(self):
        """A prior real bug class this guards against: without this check, an
        unauthenticated relay response (or a compromised/misbehaving relay)
        could apply a decision nobody with the shared key actually made."""
        config = make_config()
        session = _FakeSession(get_responses=[
            _FakeResponse(200, {"request_id": "req1", "decision": "approved", "auth": ""}),
        ])
        client = MobileRelayClient(config, session=session)

        result = client.poll_decision("req1", overall_timeout_seconds=0.3, long_poll_seconds=0.1)

        assert result is None

    def test_wrong_auth_tag_is_discarded(self):
        config = make_config()
        wrong_auth = valid_auth(config, "req1", "denied")  # tag for a *different* decision
        session = _FakeSession(get_responses=[
            _FakeResponse(200, {"request_id": "req1", "decision": "approved", "auth": wrong_auth}),
        ])
        client = MobileRelayClient(config, session=session)

        result = client.poll_decision("req1", overall_timeout_seconds=0.3, long_poll_seconds=0.1)

        assert result is None

    def test_auth_tag_does_not_transfer_to_a_different_request_id(self):
        """Replay-protection check: a valid tag for req1 must not verify for req2."""
        config = make_config()
        auth_for_req1 = valid_auth(config, "req1", "approved")
        session = _FakeSession(get_responses=[
            _FakeResponse(200, {"request_id": "req2", "decision": "approved", "auth": auth_for_req1}),
        ])
        client = MobileRelayClient(config, session=session)

        result = client.poll_decision("req2", overall_timeout_seconds=0.3, long_poll_seconds=0.1)

        assert result is None

    def test_decision_outside_approved_denied_is_ignored(self):
        config = make_config()
        session = _FakeSession(get_responses=[
            _FakeResponse(200, {"request_id": "req1", "decision": "maybe", "auth": "whatever"}),
        ])
        client = MobileRelayClient(config, session=session)

        result = client.poll_decision("req1", overall_timeout_seconds=0.3, long_poll_seconds=0.1)

        assert result is None

    def test_204_keeps_polling_until_timeout_then_returns_none(self):
        config = make_config()
        session = _FakeSession(get_responses=[_FakeResponse(204), _FakeResponse(204), _FakeResponse(204)])
        client = MobileRelayClient(config, session=session)

        result = client.poll_decision("req1", overall_timeout_seconds=0.3, long_poll_seconds=0.1)

        assert result is None
        assert len(session.get_calls) >= 2

    def test_should_abandon_stops_polling_immediately(self):
        config = make_config()
        session = _FakeSession(get_responses=[])  # would raise IndexError if ever polled
        client = MobileRelayClient(config, session=session)

        result = client.poll_decision(
            "req1", overall_timeout_seconds=5, should_abandon=lambda: True,
        )

        assert result is None
        assert session.get_calls == []

    def test_network_error_is_retried_not_fatal(self):
        config = make_config()
        auth = valid_auth(config, "req1", "denied")
        session = _FakeSession(get_responses=[
            requests.ConnectionError("blip"),
            _FakeResponse(200, {"request_id": "req1", "decision": "denied", "auth": auth}),
        ])
        client = MobileRelayClient(config, session=session)

        result = client.poll_decision("req1", overall_timeout_seconds=5, long_poll_seconds=0.1)

        assert result == "denied"
        assert len(session.get_calls) == 2


# ---------------------------------------------------------------------------- #
# poll_for_request_raw / post_decision_raw (pairing-handshake-only methods)
# ---------------------------------------------------------------------------- #

class TestPollForRequestRaw:
    def test_returns_request_id_and_raw_payload_undecrypted(self):
        config = make_config()
        session = _FakeSession(get_responses=[
            _FakeResponse(200, {"request_id": "__pairing__", "payload": {"ciphertext": "opaque-blob"}}),
        ])
        client = MobileRelayClient(config, session=session)

        result = client.poll_for_request_raw(overall_timeout_seconds=5)

        assert result == ("__pairing__", {"ciphertext": "opaque-blob"})

    def test_204_keeps_polling_until_timeout_then_returns_none(self):
        config = make_config()
        session = _FakeSession(get_responses=[_FakeResponse(204)])
        client = MobileRelayClient(config, session=session)

        result = client.poll_for_request_raw(overall_timeout_seconds=0.3, long_poll_seconds=0.1)

        assert result is None

    def test_network_error_is_retried_not_fatal(self):
        config = make_config()
        session = _FakeSession(get_responses=[
            requests.ConnectionError("blip"),
            _FakeResponse(200, {"request_id": "r1", "payload": {"x": 1}}),
        ])
        client = MobileRelayClient(config, session=session)

        result = client.poll_for_request_raw(overall_timeout_seconds=5, long_poll_seconds=0.1)

        assert result == ("r1", {"x": 1})


class TestPostDecisionRaw:
    def test_posts_decision_with_caller_supplied_auth(self):
        session = _FakeSession(post_responses=[_FakeResponse(200)])
        client = MobileRelayClient(make_config(), session=session)

        client.post_decision_raw("__pairing__", "approved", auth="custom-tag")

        call = session.post_calls[0]
        assert call["url"] == "https://relay.example.org:8765/mailbox/mbox1/decision"
        assert call["json"] == {"request_id": "__pairing__", "decision": "approved", "auth": "custom-tag"}

    def test_network_error_raises_client_error(self):
        session = _FakeSession(post_responses=[requests.ConnectionError("down")])
        client = MobileRelayClient(make_config(), session=session)
        with pytest.raises(MobileRelayClientError):
            client.post_decision_raw("r1", "approved", auth="tag")

    def test_non_200_status_raises_client_error(self):
        session = _FakeSession(post_responses=[_FakeResponse(409, text="already decided")])
        client = MobileRelayClient(make_config(), session=session)
        with pytest.raises(MobileRelayClientError):
            client.post_decision_raw("r1", "approved", auth="tag")
