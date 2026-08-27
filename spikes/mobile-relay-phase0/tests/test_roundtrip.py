"""Proves the Phase 0 relay's pair -> wake -> decide round trip end to end.

Stands in for both real endpoints this spike doesn't build:
- "the daemon" is played by plain HTTP calls posting a fake pending
  approval and polling for its decision -- issue #55's Phase 0 is explicit
  that no real daemon code is involved yet.
- "the phone" is played by plain HTTP calls polling the mailbox and posting
  a decision -- standing in for the PWA's `app.js`, which drives the exact
  same three endpoints from a browser (`fetch`) instead of `requests`.

Run directly (not part of the main `pytest tests/` suite -- this lives
outside `src/privacyfence` and testpaths in pyproject.toml, on purpose:
see this spike's top-level README):

    pip install pytest requests
    pytest spikes/mobile-relay-phase0/tests/test_roundtrip.py -v
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator

import pytest
import requests

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "relay"))

from relay_server import DEFAULT_REQUEST_TTL_SECONDS, make_server  # noqa: E402


@pytest.fixture()
def relay_url() -> Iterator[str]:
    server = make_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def pair(relay_url: str) -> tuple[str, str]:
    resp = requests.post(f"{relay_url}/pair")
    assert resp.status_code == 201
    body = resp.json()
    return body["mailbox_id"], body["token"]


class TestPairWakeDecideRoundTrip:
    """The one scenario Phase 0 exists to prove is possible at all."""

    def test_full_round_trip_approve(self, relay_url: str) -> None:
        mailbox_id, token = pair(relay_url)
        request_id = str(uuid.uuid4())

        # "Daemon" posts a fake pending approval -- this is the opaque
        # payload a real deployment would encrypt to the phone's key
        # (Phase 1/2); the relay never looks inside it.
        post_resp = requests.post(
            f"{relay_url}/mailbox/{mailbox_id}",
            params={"token": token},
            json={
                "request_id": request_id,
                "payload": {"tool": "gmail.send_message", "preview": "fake PII banner content"},
            },
        )
        assert post_resp.status_code == 201

        # "Phone" wakes (long-polls) and sees the pending request.
        get_resp = requests.get(
            f"{relay_url}/mailbox/{mailbox_id}", params={"token": token, "wait": 2}
        )
        assert get_resp.status_code == 200
        assert get_resp.json() == {
            "request_id": request_id,
            "payload": {"tool": "gmail.send_message", "preview": "fake PII banner content"},
        }

        # "Phone" taps Approve.
        decide_resp = requests.post(
            f"{relay_url}/mailbox/{mailbox_id}/decision",
            params={"token": token},
            json={"request_id": request_id, "decision": "approved"},
        )
        assert decide_resp.status_code == 200

        # "Daemon" polls and gets the decision back.
        daemon_poll = requests.get(
            f"{relay_url}/mailbox/{mailbox_id}/decision",
            params={"token": token, "request_id": request_id, "wait": 2},
        )
        assert daemon_poll.status_code == 200
        assert daemon_poll.json() == {"request_id": request_id, "decision": "approved"}

    def test_deny_round_trips_too(self, relay_url: str) -> None:
        mailbox_id, token = pair(relay_url)
        request_id = str(uuid.uuid4())
        requests.post(
            f"{relay_url}/mailbox/{mailbox_id}",
            params={"token": token},
            json={"request_id": request_id, "payload": {}},
        )
        requests.post(
            f"{relay_url}/mailbox/{mailbox_id}/decision",
            params={"token": token},
            json={"request_id": request_id, "decision": "denied"},
        )
        resp = requests.get(
            f"{relay_url}/mailbox/{mailbox_id}/decision",
            params={"token": token, "request_id": request_id, "wait": 1},
        )
        assert resp.json()["decision"] == "denied"


class TestDecidedRequestStopsBeingPending:
    """Regression: a real browser-driven run of the PWA against this relay
    found that once a request was decided, the phone's *next* long-poll
    still got it back as if it were newly pending -- because the relay kept
    `mailbox.pending` set (so the daemon could still poll the decision) but
    the phone-facing GET didn't filter out requests that already had a
    decision recorded. The PWA re-rendered the already-answered request as a
    fresh Approve/Deny prompt right after the user had just answered it."""

    def test_phone_poll_does_not_resurface_a_decided_request(self, relay_url: str) -> None:
        mailbox_id, token = pair(relay_url)
        request_id = str(uuid.uuid4())
        requests.post(
            f"{relay_url}/mailbox/{mailbox_id}",
            params={"token": token},
            json={"request_id": request_id, "payload": {}},
        )
        requests.post(
            f"{relay_url}/mailbox/{mailbox_id}/decision",
            params={"token": token},
            json={"request_id": request_id, "decision": "approved"},
        )

        resp = requests.get(
            f"{relay_url}/mailbox/{mailbox_id}", params={"token": token, "wait": 0}
        )
        assert resp.status_code == 204

    def test_daemon_can_still_read_the_decision_after_that(self, relay_url: str) -> None:
        mailbox_id, token = pair(relay_url)
        request_id = str(uuid.uuid4())
        requests.post(
            f"{relay_url}/mailbox/{mailbox_id}",
            params={"token": token},
            json={"request_id": request_id, "payload": {}},
        )
        requests.post(
            f"{relay_url}/mailbox/{mailbox_id}/decision",
            params={"token": token},
            json={"request_id": request_id, "decision": "denied"},
        )
        # The phone no longer sees it (checked above), but the daemon must
        # still be able to collect the decision it's been waiting on.
        resp = requests.get(
            f"{relay_url}/mailbox/{mailbox_id}/decision",
            params={"token": token, "request_id": request_id, "wait": 0},
        )
        assert resp.status_code == 200
        assert resp.json()["decision"] == "denied"


class TestFailClosed:
    """Requirement 1 in #55: no response must ever be silently treated as approval."""

    def test_wrong_token_is_rejected_not_leaked(self, relay_url: str) -> None:
        mailbox_id, _token = pair(relay_url)
        resp = requests.get(
            f"{relay_url}/mailbox/{mailbox_id}", params={"token": "wrong-token"}
        )
        assert resp.status_code == 403

    def test_unknown_mailbox_is_rejected(self, relay_url: str) -> None:
        resp = requests.get(
            f"{relay_url}/mailbox/does-not-exist", params={"token": "anything"}
        )
        assert resp.status_code == 403

    def test_no_pending_request_never_fabricates_an_approval(self, relay_url: str) -> None:
        mailbox_id, token = pair(relay_url)
        resp = requests.get(
            f"{relay_url}/mailbox/{mailbox_id}", params={"token": token, "wait": 0}
        )
        assert resp.status_code == 204

    def test_daemon_poll_before_any_decision_is_not_an_approval(self, relay_url: str) -> None:
        mailbox_id, token = pair(relay_url)
        request_id = str(uuid.uuid4())
        requests.post(
            f"{relay_url}/mailbox/{mailbox_id}",
            params={"token": token},
            json={"request_id": request_id, "payload": {}},
        )
        resp = requests.get(
            f"{relay_url}/mailbox/{mailbox_id}/decision",
            params={"token": token, "request_id": request_id, "wait": 0},
        )
        # 204 carries no body (HTTP semantics) -- absence of a decision is
        # the status code alone, never a fabricated "decision": null body.
        assert resp.status_code == 204

    def test_expired_request_disappears_rather_than_auto_approving(self, relay_url: str) -> None:
        mailbox_id, token = pair(relay_url)
        request_id = str(uuid.uuid4())
        requests.post(
            f"{relay_url}/mailbox/{mailbox_id}",
            params={"token": token},
            json={"request_id": request_id, "payload": {}, "ttl_seconds": 0.1},
        )
        time.sleep(0.3)

        resp = requests.get(
            f"{relay_url}/mailbox/{mailbox_id}", params={"token": token, "wait": 0}
        )
        assert resp.status_code == 204

    def test_default_ttl_is_a_finite_bound_not_forever(self) -> None:
        assert 0 < DEFAULT_REQUEST_TTL_SECONDS < 3600


class TestStaleDecisionGuard:
    """The gap flagged in the issue comments: a stale/duplicate decision for
    an already-resolved request must never be applied by either channel."""

    def test_second_decision_for_same_request_is_rejected(self, relay_url: str) -> None:
        mailbox_id, token = pair(relay_url)
        request_id = str(uuid.uuid4())
        requests.post(
            f"{relay_url}/mailbox/{mailbox_id}",
            params={"token": token},
            json={"request_id": request_id, "payload": {}},
        )

        first = requests.post(
            f"{relay_url}/mailbox/{mailbox_id}/decision",
            params={"token": token},
            json={"request_id": request_id, "decision": "approved"},
        )
        assert first.status_code == 200

        # Simulates the stale desktop popup click arriving after the phone
        # already answered (or vice versa) -- must not overturn the first
        # decision, even to a conflicting one.
        second = requests.post(
            f"{relay_url}/mailbox/{mailbox_id}/decision",
            params={"token": token},
            json={"request_id": request_id, "decision": "denied"},
        )
        assert second.status_code == 409

        # The daemon must still see the first, real decision -- not the
        # rejected second one, and not a fabricated result of the conflict.
        daemon_poll = requests.get(
            f"{relay_url}/mailbox/{mailbox_id}/decision",
            params={"token": token, "request_id": request_id, "wait": 0},
        )
        assert daemon_poll.json()["decision"] == "approved"

    def test_decision_for_unknown_request_id_is_rejected(self, relay_url: str) -> None:
        mailbox_id, token = pair(relay_url)
        requests.post(
            f"{relay_url}/mailbox/{mailbox_id}",
            params={"token": token},
            json={"request_id": "real-request", "payload": {}},
        )
        resp = requests.post(
            f"{relay_url}/mailbox/{mailbox_id}/decision",
            params={"token": token},
            json={"request_id": "some-other-stale-request", "decision": "approved"},
        )
        assert resp.status_code == 410
