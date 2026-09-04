"""Tests for web/routes_security.py: passkey enrollment (P9)."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from privacyfence import paths, webauthn_stepup as wa
from privacyfence.org_mode import StepUpConfig
from privacyfence.principal import Principal
from privacyfence.web import org_session, routes_security as rs

ISSUER = "https://pf.example.com"
ALICE = Principal(id="alice", email="alice@example.com", display_name="Alice")


@pytest.fixture(autouse=True)
def _fake_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    return tmp_path


def _app(*, step_up=None, sessions=None):
    sessions = sessions or org_session.OrgSessionStore()
    step_up = step_up or StepUpConfig(rp_id="pf.example.com", rp_name="PrivacyFence")
    routes = rs.build_routes(sessions=sessions, step_up=step_up, issuer_url=ISSUER)
    app = Starlette(routes=routes)
    return app, sessions


def _client(app, follow_redirects=False) -> TestClient:
    return TestClient(app, base_url=ISSUER, follow_redirects=follow_redirects)


def _signed_in(client, sessions, principal) -> str:
    session_id = sessions.create(principal)
    client.cookies.set(org_session.SESSION_COOKIE, session_id)
    return session_id


class TestAuthRequired:
    def test_page_redirects_when_signed_out(self):
        app, _sessions = _app()
        r = _client(app).get("/security")
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/security"

    def test_register_options_is_401_when_signed_out(self):
        app, _sessions = _app()
        r = _client(app).post("/api/security/webauthn/register/options", json={})
        assert r.status_code == 401


class TestSecurityPage:
    def test_lists_no_credentials_by_default(self):
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get("/security")
        assert r.status_code == 200
        assert "No passkeys added yet." in r.text

    def test_lists_an_enrolled_credential(self):
        app, sessions = _app()
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0,
            device_type="single_device", backed_up=False, label="My Phone",
        ))
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get("/security")
        assert "My Phone" in r.text


class TestRegisterOptions:
    def test_returns_options_bound_to_the_configured_rp(self):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post("/api/security/webauthn/register/options", json={"csrf": session_id})
        assert r.status_code == 200
        assert r.json()["options"]["rp"]["id"] == "pf.example.com"

    def test_wrong_csrf_is_rejected(self):
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.post("/api/security/webauthn/register/options", json={"csrf": "wrong"})
        assert r.status_code == 401

    def test_unconfigured_rp_id_is_a_clean_400(self):
        app, sessions = _app(step_up=StepUpConfig(rp_id=""))
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post("/api/security/webauthn/register/options", json={"csrf": session_id})
        assert r.status_code == 400

    def test_invalid_json_body_is_treated_as_unauthorized_not_a_crash(self):
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.post(
            "/api/security/webauthn/register/options", content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 401

    def test_mismatched_origin_is_rejected(self):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(
            "/api/security/webauthn/register/options", json={"csrf": session_id},
            headers={"Origin": "https://evil.example.com"},
        )
        assert r.status_code == 403


class TestRegisterVerify:
    def test_unauthenticated_is_401(self):
        app, _sessions = _app()
        r = _client(app).post("/api/security/webauthn/register/verify", json={"credential": {"id": "x"}})
        assert r.status_code == 401

    def test_invalid_json_body_is_a_clean_400(self):
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.post(
            "/api/security/webauthn/register/verify", content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 400

    def test_non_object_json_body_is_a_clean_400(self):
        app, sessions = _app()
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.post("/api/security/webauthn/register/verify", json="just a string")
        assert r.status_code == 400

    def test_missing_credential_is_rejected(self):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        client.post("/api/security/webauthn/register/options", json={"csrf": session_id})
        r = client.post("/api/security/webauthn/register/verify", json={"csrf": session_id})
        assert r.status_code == 400

    def test_a_webauthn_verification_failure_is_a_clean_400(self):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        client.post("/api/security/webauthn/register/options", json={"csrf": session_id})
        with patch.object(wa.webauthn, "verify_registration_response", side_effect=ValueError("bad sig")):
            r = client.post("/api/security/webauthn/register/verify", json={
                "csrf": session_id, "credential": {"id": "x"},
            })
        assert r.status_code == 400
        assert wa.list_credentials(ALICE) == []

    def test_verify_without_a_prior_options_call_fails(self):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post("/api/security/webauthn/register/verify", json={
            "csrf": session_id, "credential": {"id": "x"},
        })
        assert r.status_code == 400

    def test_successful_verification_stores_the_credential(self):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        client.post("/api/security/webauthn/register/options", json={"csrf": session_id})

        fake_verified = type("V", (), {
            "credential_id": b"raw-id", "credential_public_key": b"pub-key", "sign_count": 0,
            "credential_device_type": type("D", (), {"value": "single_device"})(),
            "credential_backed_up": False,
        })()
        with patch.object(wa.webauthn, "verify_registration_response", return_value=fake_verified):
            r = client.post("/api/security/webauthn/register/verify", json={
                "csrf": session_id, "credential": {"id": "x"}, "label": "My Laptop",
            })
        assert r.status_code == 200
        assert wa.list_credentials(ALICE)[0].label == "My Laptop"

    def test_the_challenge_is_single_use(self):
        app, sessions = _app()
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        client.post("/api/security/webauthn/register/options", json={"csrf": session_id})
        fake_verified = type("V", (), {
            "credential_id": b"raw-id", "credential_public_key": b"pub-key", "sign_count": 0,
            "credential_device_type": type("D", (), {"value": "single_device"})(),
            "credential_backed_up": False,
        })()
        with patch.object(wa.webauthn, "verify_registration_response", return_value=fake_verified):
            client.post("/api/security/webauthn/register/verify", json={"csrf": session_id, "credential": {"id": "x"}})
            second = client.post(
                "/api/security/webauthn/register/verify", json={"csrf": session_id, "credential": {"id": "x"}},
            )
        assert second.status_code == 400


class TestDeleteCredential:
    def test_unauthenticated_redirects_to_login(self):
        app, _sessions = _app()
        r = _client(app).post("/security/credentials/Y3JlZC0x/delete", data={"csrf": "x"})
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/security"

    def test_removes_the_credential(self):
        app, sessions = _app()
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(
            "/security/credentials/Y3JlZC0x/delete", data={"csrf": session_id},
        )
        assert r.status_code in (302, 303)
        assert wa.list_credentials(ALICE) == []

    def test_wrong_csrf_does_not_delete(self):
        app, sessions = _app()
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.post("/security/credentials/Y3JlZC0x/delete", data={"csrf": "wrong"})
        assert r.status_code == 401
        assert len(wa.list_credentials(ALICE)) == 1
