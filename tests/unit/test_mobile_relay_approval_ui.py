"""Tests for mobile_relay_approval_ui.py (issue #55, Phase 1).

Stubs MobileRelayClient entirely (its own contract is covered by
test_mobile_relay_client.py) -- these tests are about the ApprovalUI mapping
layer: which payload shape each of the four methods builds, how a relay
decision maps back to (decision, chosen_index)/bool, and that a relay
failure or timeout degrades to "denied" rather than raising or hanging.
"""
from __future__ import annotations

import threading

from privacyfence.mobile_relay_approval_ui import MobileRelayApprovalUI
from privacyfence.mobile_relay_client import MobileRelayClientError


class _FakeClient:
    def __init__(self, decision="approved", post_error=None):
        self.decision = decision
        self.post_error = post_error
        self.posted: list[dict] = []
        self.poll_calls: list[dict] = []

    def post_request(self, request_id, payload, *, ttl_seconds):
        self.posted.append({"request_id": request_id, "payload": payload, "ttl_seconds": ttl_seconds})
        if self.post_error is not None:
            raise self.post_error

    def poll_decision(self, request_id, *, overall_timeout_seconds, should_abandon=None, long_poll_seconds=25.0):
        self.poll_calls.append({"request_id": request_id, "should_abandon": should_abandon})
        return self.decision


class TestShowPopup:
    def test_approved_maps_to_accept_with_no_chosen_index(self):
        client = _FakeClient(decision="approved")
        ui = MobileRelayApprovalUI(client)

        result = ui.show_popup("Title", {"f": "v"}, "details", write_content_flags=["Email"])

        assert result == ("accept", None)

    def test_denied_maps_to_deny(self):
        ui = MobileRelayApprovalUI(_FakeClient(decision="denied"))

        assert ui.show_popup("Title", {}, "details") == ("deny", None)

    def test_payload_carries_pii_flag_and_categories_for_parity(self):
        client = _FakeClient()
        ui = MobileRelayApprovalUI(client)

        ui.show_popup("Title", {}, "details", write_content_flags=["Phone number"])

        payload = client.posted[0]["payload"]
        assert payload["kind"] == "popup"
        assert payload["pii_flagged"] is True
        assert payload["pii_categories"] == ["Phone number"]

    def test_no_content_flags_means_not_pii_flagged(self):
        client = _FakeClient()
        ui = MobileRelayApprovalUI(client)

        ui.show_popup("Title", {}, "details")

        assert client.posted[0]["payload"]["pii_flagged"] is False


class TestShowReadPopup:
    def test_approved_maps_to_accept(self):
        ui = MobileRelayApprovalUI(_FakeClient(decision="approved"))

        result = ui.show_read_popup("Title", {}, "details", None, pii_categories=["Email"])

        assert result == ("accept", None)

    def test_pii_categories_carried_for_red_banner_parity(self):
        """Issue #55 requirement 4: the PII warning must render on mobile too."""
        client = _FakeClient()
        ui = MobileRelayApprovalUI(client)

        ui.show_read_popup("Title", {}, "full body text", None, pii_categories=["IBAN"])

        payload = client.posted[0]["payload"]
        assert payload["kind"] == "read_popup"
        assert payload["pii_flagged"] is True
        assert payload["pii_categories"] == ["IBAN"]
        assert payload["details_text"] == "full body text"


class TestConfirmationPopups:
    def test_show_pii_confirmation_popup_approved_is_true(self):
        ui = MobileRelayApprovalUI(_FakeClient(decision="approved"))
        assert ui.show_pii_confirmation_popup(["Email"]) is True

    def test_show_pii_confirmation_popup_denied_is_false(self):
        ui = MobileRelayApprovalUI(_FakeClient(decision="denied"))
        assert ui.show_pii_confirmation_popup(["Email"]) is False

    def test_show_rule_confirmation_popup_approved_is_true(self):
        ui = MobileRelayApprovalUI(_FakeClient(decision="approved"))
        assert ui.show_rule_confirmation_popup("some rule") is True

    def test_show_rule_confirmation_popup_denied_is_false(self):
        ui = MobileRelayApprovalUI(_FakeClient(decision="denied"))
        assert ui.show_rule_confirmation_popup("some rule") is False


class TestFailClosedBehavior:
    def test_relay_unreachable_on_post_denies_rather_than_raising(self):
        client = _FakeClient(post_error=MobileRelayClientError("relay down"))
        ui = MobileRelayApprovalUI(client)

        # Must not raise -- a phone that can't be reached must never fail
        # a call the native popup (raced alongside this) can still answer.
        assert ui.show_popup("Title", {}, "details") == ("deny", None)

    def test_no_trustworthy_decision_denies(self):
        """poll_decision returning None (timeout, or every candidate decision
        failed auth) must degrade to deny, never hang or raise."""
        client = _FakeClient(decision=None)
        ui = MobileRelayApprovalUI(client)

        assert ui.show_read_popup("Title", {}, "details", None) == ("deny", None)


class TestAbandonEventPropagation:
    def test_abandon_event_is_forwarded_as_should_abandon(self):
        client = _FakeClient()
        ui = MobileRelayApprovalUI(client)
        event = threading.Event()

        ui.show_popup("Title", {}, "details", abandon_event=event)

        should_abandon = client.poll_calls[0]["should_abandon"]
        assert should_abandon() is False
        event.set()
        assert should_abandon() is True

    def test_no_abandon_event_still_works(self):
        client = _FakeClient()
        ui = MobileRelayApprovalUI(client)

        result = ui.show_popup("Title", {}, "details")

        assert result == ("accept", None)
        assert client.poll_calls[0]["should_abandon"]() is False


class TestRequestIdIsUniquePerCall:
    def test_two_calls_use_different_request_ids(self):
        client = _FakeClient()
        ui = MobileRelayApprovalUI(client)

        ui.show_popup("Title", {}, "details")
        ui.show_popup("Title", {}, "details")

        assert client.posted[0]["request_id"] != client.posted[1]["request_id"]
