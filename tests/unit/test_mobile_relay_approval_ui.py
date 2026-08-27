"""Tests for mobile_relay_approval_ui.py (issue #55).

Runs against a real relay (the Phase 0 spike's relay_server.py, same
fixture pattern as test_mobile_relay_pairing.py) rather than mocking HTTP:
MobileRelayApprovalUI constructs one MobileRelayClient per paired device
internally, at call time, so there's no seam to inject a fake session at
this layer -- and a real relay gives much stronger confidence for the
multi-device racing behavior this module exists to implement (Phase 2)
than a mock ever could. "The phone(s)" are played by plain HTTP calls,
same convention as every other test file that stands in for one.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest
import requests

from privacyfence.mobile_relay_approval_ui import MobileRelayApprovalUI
from privacyfence.mobile_relay_pairing import PairedDevice, PairingStore

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "spikes" / "mobile-relay-phase0" / "relay"))
from relay_server import make_server  # noqa: E402


@pytest.fixture()
def relay_url():
    server = make_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def pair_a_device(relay_url: str, device_name: str = "Test Phone") -> PairedDevice:
    """Provisions a real mailbox on the relay and returns a PairedDevice
    pointing at it, with a fixed shared_key this test controls -- standing
    in for a device that's already been through mobile_relay_pairing.py's
    real handshake (covered by test_mobile_relay_pairing.py; this file is
    about what happens *after* pairing, not the handshake itself)."""
    resp = requests.post(f"{relay_url}/pair")
    assert resp.status_code == 201
    body = resp.json()
    return PairedDevice(
        device_id=f"dev-{device_name}", device_name=device_name, device_public_key=b"\x00" * 32,
        mailbox_id=body["mailbox_id"], token=body["token"], shared_key=b"1" * 32, paired_at=time.time(),
    )


def phone_answers(device: PairedDevice, relay_url: str, decision: str, *, delay: float = 0.0) -> None:
    """Plays one phone: waits for the pending request, then answers it with
    a correctly-authenticated decision. Runs synchronously -- callers that
    want this to happen concurrently with the daemon's own post/poll should
    run it in a thread."""
    from privacyfence.mobile_relay_client import compute_auth_tag

    deadline = time.time() + 5
    request_id = None
    while time.time() < deadline:
        resp = requests.get(
            f"{relay_url}/mailbox/{device.mailbox_id}", params={"token": device.token, "wait": 1},
        )
        if resp.status_code == 200:
            request_id = resp.json()["request_id"]
            break
    assert request_id is not None, "phone never saw a pending request"

    if delay:
        time.sleep(delay)

    auth = compute_auth_tag(device.shared_key, request_id, decision)
    resp = requests.post(
        f"{relay_url}/mailbox/{device.mailbox_id}/decision",
        params={"token": device.token},
        json={"request_id": request_id, "decision": decision, "auth": auth},
    )
    assert resp.status_code == 200, resp.text


def start_phone(device: PairedDevice, relay_url: str, decision: str, *, delay: float = 0.0) -> threading.Thread:
    thread = threading.Thread(target=phone_answers, args=(device, relay_url, decision), kwargs={"delay": delay})
    thread.start()
    return thread


class TestNoActiveDevices:
    def test_no_paired_devices_denies_immediately(self, tmp_path, relay_url):
        store = PairingStore(str(tmp_path / "store.json"))
        ui = MobileRelayApprovalUI(relay_url, store, request_timeout_seconds=2)

        result = ui.show_popup("Title", {}, "details")

        assert result == ("deny", None)

    def test_only_revoked_devices_denies_immediately(self, tmp_path, relay_url):
        store = PairingStore(str(tmp_path / "store.json"))
        device = pair_a_device(relay_url)
        store.add_device(device)
        store.revoke(device.device_id)

        ui = MobileRelayApprovalUI(relay_url, store, request_timeout_seconds=2)
        result = ui.show_popup("Title", {}, "details")

        assert result == ("deny", None)


class TestSingleDevice:
    def test_approved_maps_to_accept(self, tmp_path, relay_url):
        store = PairingStore(str(tmp_path / "store.json"))
        device = pair_a_device(relay_url)
        store.add_device(device)
        ui = MobileRelayApprovalUI(relay_url, store, request_timeout_seconds=10)

        phone = start_phone(device, relay_url, "approved")
        result = ui.show_popup("Title", {}, "details")
        phone.join(timeout=5)

        assert result == ("accept", None)
        assert ui.last_answered_device_name == device.device_name

    def test_denied_maps_to_deny(self, tmp_path, relay_url):
        store = PairingStore(str(tmp_path / "store.json"))
        device = pair_a_device(relay_url)
        store.add_device(device)
        ui = MobileRelayApprovalUI(relay_url, store, request_timeout_seconds=10)

        phone = start_phone(device, relay_url, "denied")
        result = ui.show_read_popup("Title", {}, "details", None)
        phone.join(timeout=5)

        assert result == ("deny", None)

    def test_no_answer_within_timeout_denies(self, tmp_path, relay_url):
        store = PairingStore(str(tmp_path / "store.json"))
        store.add_device(pair_a_device(relay_url))
        ui = MobileRelayApprovalUI(relay_url, store, request_timeout_seconds=0.5)

        result = ui.show_popup("Title", {}, "details")

        assert result == ("deny", None)
        assert ui.last_answered_device_name == ""

    def test_unreachable_relay_url_denies_rather_than_raising(self, tmp_path):
        store = PairingStore(str(tmp_path / "store.json"))
        store.add_device(PairedDevice(
            device_id="dev1", device_name="Unreachable", device_public_key=b"\x00" * 32,
            mailbox_id="mbox1", token="tok1", shared_key=b"1" * 32, paired_at=time.time(),
        ))
        ui = MobileRelayApprovalUI("http://127.0.0.1:1", store, request_timeout_seconds=2)

        result = ui.show_popup("Title", {}, "details")

        assert result == ("deny", None)


class TestConfirmationMethods:
    def test_show_pii_confirmation_popup(self, tmp_path, relay_url):
        store = PairingStore(str(tmp_path / "store.json"))
        device = pair_a_device(relay_url)
        store.add_device(device)
        ui = MobileRelayApprovalUI(relay_url, store, request_timeout_seconds=10)

        phone = start_phone(device, relay_url, "approved")
        result = ui.show_pii_confirmation_popup(["Email"])
        phone.join(timeout=5)

        assert result is True

    def test_show_rule_confirmation_popup(self, tmp_path, relay_url):
        store = PairingStore(str(tmp_path / "store.json"))
        device = pair_a_device(relay_url)
        store.add_device(device)
        ui = MobileRelayApprovalUI(relay_url, store, request_timeout_seconds=10)

        phone = start_phone(device, relay_url, "denied")
        result = ui.show_rule_confirmation_popup("some rule")
        phone.join(timeout=5)

        assert result is False


class TestMultiDeviceRacing:
    def test_first_device_to_answer_wins(self, tmp_path, relay_url):
        store = PairingStore(str(tmp_path / "store.json"))
        fast_device = pair_a_device(relay_url, "Fast Phone")
        slow_device = pair_a_device(relay_url, "Slow Phone")
        store.add_device(fast_device)
        store.add_device(slow_device)
        ui = MobileRelayApprovalUI(relay_url, store, request_timeout_seconds=10)

        fast_phone = start_phone(fast_device, relay_url, "approved", delay=0.05)
        slow_phone = start_phone(slow_device, relay_url, "denied", delay=3.0)

        started = time.monotonic()
        result = ui.show_popup("Title", {}, "details")
        elapsed = time.monotonic() - started

        assert result == ("accept", None)
        assert ui.last_answered_device_name == "Fast Phone"
        # Must not have waited for the slow device -- see this module's own
        # docstring on why the race returns as soon as one device answers.
        assert elapsed < 2.0
        fast_phone.join(timeout=5)
        slow_phone.join(timeout=5)

    def test_one_unreachable_device_does_not_block_another_from_answering(self, tmp_path, relay_url):
        store = PairingStore(str(tmp_path / "store.json"))
        real_device = pair_a_device(relay_url, "Real Phone")
        broken_device = PairedDevice(
            device_id="dev-broken", device_name="Broken", device_public_key=b"\x00" * 32,
            mailbox_id="does-not-exist", token="wrong-token", shared_key=b"1" * 32, paired_at=time.time(),
        )
        store.add_device(real_device)
        store.add_device(broken_device)
        ui = MobileRelayApprovalUI(relay_url, store, request_timeout_seconds=10)

        phone = start_phone(real_device, relay_url, "approved", delay=0.2)
        result = ui.show_popup("Title", {}, "details")
        phone.join(timeout=5)

        assert result == ("accept", None)
        assert ui.last_answered_device_name == "Real Phone"

    def test_all_devices_failing_denies(self, tmp_path):
        store = PairingStore(str(tmp_path / "store.json"))
        store.add_device(PairedDevice(
            device_id="dev1", device_name="Broken1", device_public_key=b"\x00" * 32,
            mailbox_id="m1", token="t1", shared_key=b"1" * 32, paired_at=time.time(),
        ))
        store.add_device(PairedDevice(
            device_id="dev2", device_name="Broken2", device_public_key=b"\x00" * 32,
            mailbox_id="m2", token="t2", shared_key=b"1" * 32, paired_at=time.time(),
        ))
        ui = MobileRelayApprovalUI("http://127.0.0.1:1", store, request_timeout_seconds=2)

        result = ui.show_popup("Title", {}, "details")

        assert result == ("deny", None)


class TestAbandonEventStopsPolling:
    def test_outer_abandon_event_stops_the_race_early(self, tmp_path, relay_url):
        store = PairingStore(str(tmp_path / "store.json"))
        store.add_device(pair_a_device(relay_url))
        ui = MobileRelayApprovalUI(relay_url, store, request_timeout_seconds=10)
        abandon_event = threading.Event()
        abandon_event.set()  # native already won, per CompositeApprovalUI's own contract

        started = time.monotonic()
        result = ui.show_popup("Title", {}, "details", abandon_event=abandon_event)
        elapsed = time.monotonic() - started

        assert result == ("deny", None)
        assert elapsed < 2.0
