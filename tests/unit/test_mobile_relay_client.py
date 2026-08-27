"""Tests for mobile_relay_client.py (issue #55, Phase 1).

Focus: the two things Phase 0's spike explicitly didn't do -- payload
confidentiality (AES-256-GCM under a shared key) and decision authenticity
(HMAC tag binding a decision to one request_id, so a device that doesn't
hold the shared key can't forge one, and a captured tag can't be replayed
onto a different request) -- plus MobileRelayConfig's fail-closed parsing of
org_config.json's mobile_relay section.
"""
from __future__ import annotations

import base64
import json

import pytest
import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from privacyfence.mobile_relay_client import (
    MobileRelayClient,
    MobileRelayClientError,
    MobileRelayConfig,
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
# MobileRelayConfig.from_org_config
# ---------------------------------------------------------------------------- #

class TestMobileRelayConfigFromOrgConfig:
    def test_missing_section_returns_none(self):
        assert MobileRelayConfig.from_org_config({}) is None

    def test_disabled_section_returns_none(self):
        org_config = {"mobile_relay": {
            "enabled": False, "relay_url": "https://r", "mailbox_id": "m", "token": "t",
            "shared_key_base64": VALID_KEY_B64,
        }}
        assert MobileRelayConfig.from_org_config(org_config) is None

    @pytest.mark.parametrize("missing_field", ["relay_url", "mailbox_id", "token", "shared_key_base64"])
    def test_missing_required_field_returns_none(self, missing_field):
        section = {
            "enabled": True, "relay_url": "https://r", "mailbox_id": "m", "token": "t",
            "shared_key_base64": VALID_KEY_B64,
        }
        del section[missing_field]
        assert MobileRelayConfig.from_org_config({"mobile_relay": section}) is None

    def test_invalid_base64_returns_none(self):
        section = {
            "enabled": True, "relay_url": "https://r", "mailbox_id": "m", "token": "t",
            "shared_key_base64": "not-valid-base64!!!",
        }
        assert MobileRelayConfig.from_org_config({"mobile_relay": section}) is None

    def test_wrong_key_length_returns_none(self):
        short_key_b64 = base64.b64encode(b"too-short").decode("ascii")
        section = {
            "enabled": True, "relay_url": "https://r", "mailbox_id": "m", "token": "t",
            "shared_key_base64": short_key_b64,
        }
        assert MobileRelayConfig.from_org_config({"mobile_relay": section}) is None

    def test_valid_section_builds_config(self):
        section = {
            "enabled": True, "relay_url": "https://relay.example.org:8765/", "mailbox_id": "mbox1",
            "token": "tok1", "shared_key_base64": VALID_KEY_B64,
        }
        config = MobileRelayConfig.from_org_config({"mobile_relay": section})
        assert config is not None
        assert config.relay_url == "https://relay.example.org:8765"  # trailing slash stripped
        assert config.mailbox_id == "mbox1"
        assert config.token == "tok1"
        assert config.shared_key == VALID_KEY


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
        blob = base64.b64decode(ciphertext_b64)
        nonce, ciphertext = blob[:12], blob[12:]
        plaintext = AESGCM(VALID_KEY).decrypt(nonce, ciphertext, None)
        assert json.loads(plaintext) == original

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
    import hashlib
    import hmac as hmac_module

    digest = hmac_module.new(
        config.shared_key, f"{request_id}:{decision}".encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("ascii")


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
