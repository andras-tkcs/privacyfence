"""Tests for webauthn_stepup.py (P9, docs/https-connector-refactor-plan.md
§10.6, §15 D7).

The ``webauthn`` package's own CBOR/COSE attestation parsing and signature
verification is not re-tested here (D2's own reasoning: it's a maintained
library, not this repo's code) -- ``verify_registration_response``/
``verify_authentication_response`` are mocked at the module boundary so
these tests cover what this module is actually responsible for: credential
storage, challenge binding/single-use/TTL, the decision fingerprint, and
the writes-vs-reads scoping rule. ``begin_registration``/``begin_assertion``
are exercised for real (no mocking) since they only build options, never
verify anything.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from webauthn.helpers import bytes_to_base64url

from privacyfence import paths, webauthn_stepup as wa
from privacyfence.principal import Principal

ALICE = Principal(id="alice", email="alice@example.com", display_name="Alice")
BOB = Principal(id="bob", email="bob@example.com", display_name="Bob")


@pytest.fixture(autouse=True)
def _fake_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    return tmp_path


def _b64u(raw: bytes) -> str:
    return bytes_to_base64url(raw)


# credential_id must be real base64url (webauthn_stepup.py round-trips it
# through base64url_to_bytes for begin_assertion's own exclude/allow lists),
# not an arbitrary test label -- "cred1"/"cred2" below are byte payloads
# encoded the same way a real credential id would be.
CRED1_ID = _b64u(b"cred-1-raw-id")


def _credential(credential_id: str = CRED1_ID, **overrides) -> wa.WebAuthnCredential:
    defaults = dict(
        credential_id=credential_id, public_key="pk", sign_count=0,
        device_type="single_device", backed_up=False, label="Passkey",
    )
    defaults.update(overrides)
    return wa.WebAuthnCredential(**defaults)


class TestCredentialStorage:
    def test_no_file_means_no_credentials(self):
        assert wa.list_credentials(ALICE) == []
        assert not wa.has_credentials(ALICE)

    def test_add_then_list_round_trips(self):
        wa.add_credential(ALICE, _credential())
        creds = wa.list_credentials(ALICE)
        assert len(creds) == 1
        assert creds[0].credential_id == CRED1_ID
        assert wa.has_credentials(ALICE)

    def test_adding_same_id_twice_replaces_not_duplicates(self):
        wa.add_credential(ALICE, _credential(label="First"))
        wa.add_credential(ALICE, _credential(label="Second"))
        creds = wa.list_credentials(ALICE)
        assert len(creds) == 1
        assert creds[0].label == "Second"

    def test_remove_credential(self):
        wa.add_credential(ALICE, _credential())
        assert wa.remove_credential(ALICE, CRED1_ID) is True
        assert wa.list_credentials(ALICE) == []

    def test_remove_unknown_credential_returns_false(self):
        assert wa.remove_credential(ALICE, "nope") is False

    def test_credentials_are_isolated_per_principal(self):
        wa.add_credential(ALICE, _credential(credential_id="alice-cred"))
        wa.add_credential(BOB, _credential(credential_id="bob-cred"))
        assert [c.credential_id for c in wa.list_credentials(ALICE)] == ["alice-cred"]
        assert [c.credential_id for c in wa.list_credentials(BOB)] == ["bob-cred"]

    def test_credentials_file_is_0600(self):
        wa.add_credential(ALICE, _credential())
        path = paths.user_dir(ALICE) / wa.CREDENTIALS_FILE_NAME
        assert path.exists()
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_corrupt_credentials_file_reads_as_empty_not_a_crash(self):
        path = paths.user_dir(ALICE) / wa.CREDENTIALS_FILE_NAME
        path.write_text("not json", encoding="utf-8")
        assert wa.list_credentials(ALICE) == []


class TestBeginRegistration:
    def test_options_carry_the_configured_rp(self):
        options_json, challenge = wa.begin_registration(ALICE, rp_id="pf.example.com", rp_name="PrivacyFence")
        assert isinstance(challenge, bytes) and len(challenge) >= 16
        assert '"id": "pf.example.com"' in options_json or '"id":"pf.example.com"' in options_json
        assert "PrivacyFence" in options_json

    def test_existing_credentials_are_excluded(self):
        wa.add_credential(ALICE, _credential(credential_id=CRED1_ID))
        options_json, _ = wa.begin_registration(ALICE, rp_id="pf.example.com", rp_name="PrivacyFence")
        assert "excludeCredentials" in options_json


class TestFinishRegistration:
    def test_verified_registration_is_stored(self):
        fake_verified = type("V", (), {
            "credential_id": b"raw-id", "credential_public_key": b"pub-key", "sign_count": 0,
            "credential_device_type": type("D", (), {"value": "single_device"})(),
            "credential_backed_up": False,
        })()
        with patch.object(wa.webauthn, "verify_registration_response", return_value=fake_verified) as mocked:
            cred = wa.finish_registration(
                ALICE, {"id": "x"}, expected_challenge=b"chal", rp_id="pf.example.com",
                origin="https://pf.example.com", label="My Phone",
            )
        mocked.assert_called_once()
        assert mocked.call_args.kwargs["require_user_verification"] is True
        assert cred.label == "My Phone"
        assert wa.list_credentials(ALICE) == [cred]

    def test_verification_failure_raises_webauthn_error_not_the_libs_own(self):
        with patch.object(wa.webauthn, "verify_registration_response", side_effect=ValueError("bad signature")):
            with pytest.raises(wa.WebAuthnError):
                wa.finish_registration(
                    ALICE, {"id": "x"}, expected_challenge=b"chal", rp_id="pf.example.com",
                    origin="https://pf.example.com",
                )
        assert wa.list_credentials(ALICE) == []


class TestBeginAssertion:
    def test_none_when_no_credentials_enrolled(self):
        assert wa.begin_assertion(ALICE, rp_id="pf.example.com") is None

    def test_options_allow_the_enrolled_credential(self):
        wa.add_credential(ALICE, _credential(credential_id=CRED1_ID))
        result = wa.begin_assertion(ALICE, rp_id="pf.example.com")
        assert result is not None
        options_json, challenge = result
        assert isinstance(challenge, bytes)
        assert "allowCredentials" in options_json


class TestVerifyAssertion:
    def test_unknown_credential_id_raises(self):
        with pytest.raises(wa.WebAuthnError):
            wa.verify_assertion(
                ALICE, {"id": "unknown"}, expected_challenge=b"chal", rp_id="pf.example.com",
                origin="https://pf.example.com",
            )

    def test_successful_assertion_updates_sign_count(self):
        wa.add_credential(ALICE, _credential(credential_id="cred1", sign_count=5))
        fake_verified = type("V", (), {"new_sign_count": 6, "credential_device_type": None, "credential_backed_up": False})()
        with patch.object(wa.webauthn, "verify_authentication_response", return_value=fake_verified) as mocked:
            wa.verify_assertion(
                ALICE, {"id": "cred1"}, expected_challenge=b"chal", rp_id="pf.example.com",
                origin="https://pf.example.com",
            )
        assert mocked.call_args.kwargs["require_user_verification"] is True
        assert mocked.call_args.kwargs["credential_current_sign_count"] == 5
        assert wa.list_credentials(ALICE)[0].sign_count == 6

    def test_verification_failure_raises_webauthn_error(self):
        wa.add_credential(ALICE, _credential(credential_id="cred1"))
        with patch.object(wa.webauthn, "verify_authentication_response", side_effect=ValueError("nope")):
            with pytest.raises(wa.WebAuthnError):
                wa.verify_assertion(
                    ALICE, {"id": "cred1"}, expected_challenge=b"chal", rp_id="pf.example.com",
                    origin="https://pf.example.com",
                )
        # A failed verification must not silently bump the stored sign count.
        assert wa.list_credentials(ALICE)[0].sign_count == 0


class TestDecisionFingerprint:
    def test_stable_for_the_same_inputs(self):
        a = wa.decision_fingerprint(approval_id="a1", principal_id="alice", result="accept", choice=None)
        b = wa.decision_fingerprint(approval_id="a1", principal_id="alice", result="accept", choice=None)
        assert a == b

    @pytest.mark.parametrize("field, value", [
        ("approval_id", "a2"), ("principal_id", "bob"), ("result", "deny"),
    ])
    def test_changes_when_any_bound_field_changes(self, field, value):
        base = dict(approval_id="a1", principal_id="alice", result="accept", choice=None)
        overridden = dict(base, **{field: value})
        assert wa.decision_fingerprint(**base) != wa.decision_fingerprint(**overridden)

    def test_choice_none_differs_from_choice_zero(self):
        assert (
            wa.decision_fingerprint(approval_id="a1", principal_id="alice", result="accept_all", choice=None)
            != wa.decision_fingerprint(approval_id="a1", principal_id="alice", result="accept_all", choice=0)
        )


class TestStepUpChallengeStore:
    def test_put_then_pop_returns_the_entry(self):
        store = wa.StepUpChallengeStore()
        store.put("alice", "a1", challenge=b"chal", fingerprint="fp")
        entry = store.pop("alice", "a1")
        assert entry is not None
        assert entry.challenge == b"chal"
        assert entry.fingerprint == "fp"

    def test_pop_is_single_use(self):
        store = wa.StepUpChallengeStore()
        store.put("alice", "a1", challenge=b"chal", fingerprint="fp")
        store.pop("alice", "a1")
        assert store.pop("alice", "a1") is None

    def test_missing_entry_returns_none(self):
        assert wa.StepUpChallengeStore().pop("alice", "a1") is None

    def test_expired_entry_returns_none(self):
        store = wa.StepUpChallengeStore(ttl=0.01)
        store.put("alice", "a1", challenge=b"chal", fingerprint="fp")
        time.sleep(0.02)
        assert store.pop("alice", "a1") is None

    def test_scoped_per_principal_and_approval(self):
        store = wa.StepUpChallengeStore()
        store.put("alice", "a1", challenge=b"chal", fingerprint="fp")
        assert store.pop("bob", "a1") is None
        assert store.pop("alice", "a2") is None
        assert store.pop("alice", "a1") is not None


class TestRegistrationChallengeStore:
    def test_put_then_pop(self):
        store = wa.RegistrationChallengeStore()
        store.put("alice", b"chal")
        assert store.pop("alice") == b"chal"

    def test_pop_is_single_use(self):
        store = wa.RegistrationChallengeStore()
        store.put("alice", b"chal")
        store.pop("alice")
        assert store.pop("alice") is None

    def test_a_second_put_replaces_the_first(self):
        store = wa.RegistrationChallengeStore()
        store.put("alice", b"first")
        store.put("alice", b"second")
        assert store.pop("alice") == b"second"


class TestIsStepUpRequired:
    def test_write_always_requires_it(self):
        assert wa.is_step_up_required(gate_kind="popup", pii_detected=False, scope="writes") is True
        assert wa.is_step_up_required(gate_kind="popup", pii_detected=False, scope="writes_and_pii_reads") is True

    def test_plain_read_never_requires_it(self):
        assert wa.is_step_up_required(gate_kind="review", pii_detected=False, scope="writes") is False
        assert wa.is_step_up_required(gate_kind="review", pii_detected=False, scope="writes_and_pii_reads") is False

    def test_pii_read_requires_it_only_in_the_wider_scope(self):
        assert wa.is_step_up_required(gate_kind="review", pii_detected=True, scope="writes") is False
        assert wa.is_step_up_required(gate_kind="review", pii_detected=True, scope="writes_and_pii_reads") is True
