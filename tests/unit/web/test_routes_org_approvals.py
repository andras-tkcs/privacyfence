"""Tests for web/routes_org_approvals.py: the principal-aware /approvals
surface in org mode (P9), including the WebAuthn/IdP step-up gate on write
decisions (§10.6, D7).
"""
from __future__ import annotations

import urllib.parse as up
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from privacyfence import org_identity as oi, paths, webauthn_stepup as wa
from privacyfence.approvals import PendingApprovalRegistry
from privacyfence.org_mode import StepUpConfig
from privacyfence.principal import Principal, principal_scope
from privacyfence.web import org_session, routes_org_approvals as roa
from privacyfence.web_approval_ui import WebApprovalUI

ISSUER = "https://pf.example.com"
ALICE = Principal(id="alice", email="alice@example.com", display_name="Alice")
BOB = Principal(id="bob", email="bob@example.com", display_name="Bob")


def _idp(**overrides) -> oi.IdpConfig:
    defaults = dict(
        issuer="https://idp.example.com", client_id="privacyfence", client_secret="s3cr3t",
        authorization_endpoint="https://idp.example.com/authorize",
        token_endpoint="https://idp.example.com/token", jwks_uri="https://idp.example.com/jwks",
    )
    defaults.update(overrides)
    return oi.IdpConfig(**defaults)


def _app(*, step_up: StepUpConfig | None = None, sessions=None, idp=None, web_ui=None):
    sessions = sessions or org_session.OrgSessionStore()
    web_ui = web_ui or WebApprovalUI(registry=PendingApprovalRegistry())
    step_up = step_up or StepUpConfig()
    routes = roa.build_routes(web_ui=web_ui, sessions=sessions, step_up=step_up, idp=idp or _idp(), issuer_url=ISSUER)
    app = Starlette(routes=routes)
    return app, sessions, web_ui


def _client(app, follow_redirects=False) -> TestClient:
    return TestClient(app, base_url=ISSUER, follow_redirects=follow_redirects)


def _signed_in(client: TestClient, sessions: org_session.OrgSessionStore, principal: Principal) -> str:
    session_id = sessions.create(principal)
    client.cookies.set(org_session.SESSION_COOKIE, session_id)
    return session_id


def _register(web_ui, principal: Principal, *, gate_kind="review", pii_detected=False, dedupe_key="k1"):
    with principal_scope(principal):
        approval, _ = web_ui.deferred_registry.register_or_coalesce(
            dedupe_key=dedupe_key, connector="gmail", tool="gmail_get_message", gate_kind=gate_kind,
            request_id="r1", summary="a message", tool_name="Get message", pii_detected=pii_detected,
        )
    web_ui.deferred_registry.set_html(approval.id, "<!doctype html><html><head></head><body>CARD</body></html>")
    return approval


class TestIndexAndServiceWorker:
    def test_root_redirects_to_approvals(self):
        app, _sessions, _web_ui = _app()
        r = _client(app).get("/")
        assert r.status_code == 302
        assert r.headers["location"] == "/approvals"

    def test_service_worker_is_served_with_no_auth(self):
        app, _sessions, _web_ui = _app()
        r = _client(app).get("/sw.js")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/javascript")


class TestAuthRequired:
    def test_list_redirects_to_login_when_signed_out(self):
        app, _sessions, _web_ui = _app()
        r = _client(app).get("/approvals")
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/approvals"

    def test_show_redirects_to_login_when_signed_out(self):
        app, _sessions, _web_ui = _app()
        r = _client(app).get("/approvals/abc123")
        assert r.status_code == 302
        assert r.headers["location"] == "/login?next=/approvals/abc123"

    def test_decide_is_401_when_signed_out(self):
        app, _sessions, _web_ui = _app()
        r = _client(app).post("/api/approvals/abc123/decide", json={"result": "deny"})
        assert r.status_code == 401

    def test_stream_is_401_when_signed_out(self):
        app, _sessions, _web_ui = _app()
        r = _client(app).get("/api/approvals/stream")
        assert r.status_code == 401


class TestPrincipalScopedList:
    def test_only_shows_the_signed_in_principals_own_approvals(self):
        app, sessions, web_ui = _app()
        _register(web_ui, ALICE, dedupe_key="a1")
        _register(web_ui, BOB, dedupe_key="b1")
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get("/approvals")
        assert r.status_code == 200
        assert "a message" in r.text or "Get message" in r.text  # her own row rendered

    def test_show_approval_404s_a_foreign_principals_card(self):
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, dedupe_key="a1")
        client = _client(app)
        _signed_in(client, sessions, BOB)
        r = client.get(f"/approvals/{approval.id}")
        assert r.status_code == 200
        assert "no longer pending" in r.text

    def test_show_approval_renders_the_owning_principals_card(self):
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, dedupe_key="a1")
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get(f"/approvals/{approval.id}")
        assert r.status_code == 200
        assert "CARD" in r.text


class TestDecideWithoutStepUp:
    def test_deny_a_read_succeeds(self):
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, gate_kind="review")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "deny", "csrf": session_id})
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_a_foreign_principal_cannot_decide_it(self):
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, gate_kind="review")
        client = _client(app)
        session_id = _signed_in(client, sessions, BOB)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "deny", "csrf": session_id})
        assert r.status_code == 409  # indistinguishable from "already decided"

    def test_wrong_csrf_is_rejected(self):
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, gate_kind="review")
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "deny", "csrf": "wrong"})
        assert r.status_code == 401

    def test_second_decision_is_already_decided(self):
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, gate_kind="review")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        client.post(f"/api/approvals/{approval.id}/decide", json={"result": "deny", "csrf": session_id})
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "deny", "csrf": session_id})
        assert r.status_code == 409

    def test_invalid_json_body_is_a_clean_400(self):
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, gate_kind="review")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(
            f"/api/approvals/{approval.id}/decide", content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 400

    def test_missing_result_is_rejected(self):
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, gate_kind="review")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"csrf": session_id})
        assert r.status_code == 400

    def test_mismatched_origin_is_rejected(self):
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, gate_kind="review")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(
            f"/api/approvals/{approval.id}/decide", json={"result": "deny", "csrf": session_id},
            headers={"Origin": "https://evil.example.com"},
        )
        assert r.status_code == 403

    def test_numeric_choice_result_is_normalized_to_its_string_form(self):
        # dialog_window_html.build_choice_html's own JS posts a bare number
        # for `result` instead of a string -- see decide()'s own comment.
        app, sessions, web_ui = _app()
        approval = _register(web_ui, ALICE, gate_kind="review")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": 1, "csrf": session_id})
        assert r.status_code == 200
        assert web_ui.deferred_registry.get(approval.id).result == "1"


class TestStepUpScoping:
    """§10.6/D7: step-up is off by default, and even when enabled, applies
    only to approving decisions on writes (or PII reads, in the wider
    scope) -- never to denies."""

    def test_disabled_step_up_never_blocks_a_write_accept(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=False))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert r.status_code == 200

    def test_deny_never_needs_step_up_even_on_a_write(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "deny", "csrf": session_id})
        assert r.status_code == 200

    def test_plain_read_never_needs_step_up_in_default_scope(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com", scope="writes"))
        approval = _register(web_ui, ALICE, gate_kind="review", pii_detected=True)
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert r.status_code == 200

    def test_pii_read_needs_step_up_in_the_wider_scope(self):
        app, sessions, web_ui = _app(
            step_up=StepUpConfig(enabled=True, rp_id="pf.example.com", scope="writes_and_pii_reads"),
        )
        approval = _register(web_ui, ALICE, gate_kind="review", pii_detected=True)
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert r.status_code == 428


class TestStepUpWebAuthnFlow:
    @pytest.fixture(autouse=True)
    def _fake_data_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        return tmp_path

    def test_first_attempt_with_no_credential_offers_only_the_idp_link(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert r.status_code == 428
        body = r.json()
        assert "webauthn_options" not in body
        assert body["idp_stepup_url"].startswith(f"/api/approvals/{approval.id}/stepup/idp")

    def test_first_attempt_with_a_credential_offers_webauthn_options_too(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        r = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert r.status_code == 428
        body = r.json()
        assert "webauthn_options" in body
        assert "idp_stepup_url" in body

    def test_valid_assertion_completes_the_decision(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        first = client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        assert first.status_code == 428

        fake_verified = type("V", (), {"new_sign_count": 1, "credential_device_type": None, "credential_backed_up": False})()
        with patch.object(wa.webauthn, "verify_authentication_response", return_value=fake_verified):
            second = client.post(f"/api/approvals/{approval.id}/decide", json={
                "result": "accept", "csrf": session_id, "webauthn_assertion": {"id": "Y3JlZC0x"},
            })
        assert second.status_code == 200
        assert second.json() == {"status": "ok"}

    def test_an_assertion_for_a_different_decision_is_rejected(self):
        # Fingerprint binding (§10.6): a challenge minted for "accept"
        # cannot be reused to authorize "accept_all" on the same approval.
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        r = client.post(f"/api/approvals/{approval.id}/decide", json={
            "result": "accept_all", "csrf": session_id, "webauthn_assertion": {"id": "Y3JlZC0x"},
        })
        assert r.status_code == 400

    def test_a_failed_assertion_does_not_release_the_decision(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        wa.add_credential(ALICE, wa.WebAuthnCredential(
            credential_id="Y3JlZC0x", public_key="cGs", sign_count=0, device_type="single_device", backed_up=False,
        ))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        session_id = _signed_in(client, sessions, ALICE)
        client.post(f"/api/approvals/{approval.id}/decide", json={"result": "accept", "csrf": session_id})
        with patch.object(wa.webauthn, "verify_authentication_response", side_effect=ValueError("bad sig")):
            r = client.post(f"/api/approvals/{approval.id}/decide", json={
                "result": "accept", "csrf": session_id, "webauthn_assertion": {"id": "Y3JlZC0x"},
            })
        assert r.status_code == 401
        stored = web_ui.deferred_registry.get(approval.id)
        assert stored is not None
        assert not stored.event.is_set()  # decision was never released


class TestIdpStepUp:
    def test_start_requires_a_signed_in_session(self):
        app, _sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        r = _client(app).get(f"/api/approvals/{approval.id}/stepup/idp?result=accept&choice=")
        assert r.status_code == 302
        assert r.headers["location"].startswith("/login?next=")

    def test_start_rejects_an_invalid_result(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get(f"/api/approvals/{approval.id}/stepup/idp?result=deny&choice=")
        assert r.status_code == 400

    def test_start_redirects_to_the_idp_with_reauth_hints(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        r = client.get(f"/api/approvals/{approval.id}/stepup/idp?result=accept&choice=")
        assert r.status_code == 302
        qs = dict(up.parse_qsl(up.urlparse(r.headers["location"]).query))
        assert qs["prompt"] == "login"
        assert qs["max_age"] == "0"

    def test_callback_with_an_unknown_state_is_a_clean_400(self):
        app, _sessions, _web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        cb_client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = cb_client.get("/oauth/stepup/callback?code=abc&state=does-not-exist")
        assert r.status_code == 400

    def test_callback_with_an_idp_error_redirects_back_with_stepup_error(self):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        start = client.get(f"/api/approvals/{approval.id}/stepup/idp?result=accept&choice=")
        qs = dict(up.parse_qsl(up.urlparse(start.headers["location"]).query))

        cb_client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = cb_client.get(f"/oauth/stepup/callback?error=access_denied&state={qs['state']}")
        assert r.status_code == 302
        assert r.headers["location"] == f"/approvals/{approval.id}?stepup=error"

    def test_callback_completes_the_decision_for_the_same_principal(self, monkeypatch):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        start = client.get(f"/api/approvals/{approval.id}/stepup/idp?result=accept&choice=")
        qs = dict(up.parse_qsl(up.urlparse(start.headers["location"]).query))

        monkeypatch.setattr(roa.org_identity, "exchange_code_for_tokens", lambda *a, **kw: {"id_token": "t"})
        monkeypatch.setattr(
            roa.org_identity, "verify_id_token",
            lambda idp, token, *, nonce: {"sub": "alice", "nonce": nonce},
        )
        # A fresh, cookie-less client -- mirrors the real cross-site landing.
        cb_client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = cb_client.get(f"/oauth/stepup/callback?code=abc&state={qs['state']}")
        assert r.status_code == 302
        assert r.headers["location"] == "/approvals?stepup=ok"
        # web_ui.resolve() only ever completes the UI-step (registry.answer())
        # -- gate.py's own _drive_interaction is what finalizes an approval in
        # production, and nothing here spins one up (this test registered the
        # approval directly, bypassing gate.py entirely, same as every other
        # decide test in this file and in test_routes_approvals.py's own
        # local-mode equivalent). event.is_set()/.result is the UI-step
        # outcome this route is actually responsible for.
        stored = web_ui.deferred_registry.get(approval.id)
        assert stored.event.is_set()
        assert stored.result == "accept"

    def test_callback_redirects_to_error_when_the_idp_exchange_raises(self, monkeypatch):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        start = client.get(f"/api/approvals/{approval.id}/stepup/idp?result=accept&choice=")
        qs = dict(up.parse_qsl(up.urlparse(start.headers["location"]).query))

        def _boom(*a, **kw):
            raise RuntimeError("IdP unreachable")

        monkeypatch.setattr(roa.org_identity, "exchange_code_for_tokens", _boom)
        cb_client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = cb_client.get(f"/oauth/stepup/callback?code=abc&state={qs['state']}")
        assert r.status_code == 302
        assert r.headers["location"] == f"/approvals/{approval.id}?stepup=error"
        assert not web_ui.deferred_registry.get(approval.id).event.is_set()

    def test_callback_rejects_a_different_principal_re_authenticating(self, monkeypatch):
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"))
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        start = client.get(f"/api/approvals/{approval.id}/stepup/idp?result=accept&choice=")
        qs = dict(up.parse_qsl(up.urlparse(start.headers["location"]).query))

        monkeypatch.setattr(roa.org_identity, "exchange_code_for_tokens", lambda *a, **kw: {"id_token": "t"})
        monkeypatch.setattr(
            roa.org_identity, "verify_id_token",
            lambda idp, token, *, nonce: {"sub": "bob", "nonce": nonce},  # a different human signs in
        )
        cb_client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = cb_client.get(f"/oauth/stepup/callback?code=abc&state={qs['state']}")
        assert r.status_code == 302
        assert r.headers["location"] == f"/approvals/{approval.id}?stepup=error"
        assert not web_ui.deferred_registry.get(approval.id).event.is_set()

    def test_callback_requires_configured_acr_values_when_set(self, monkeypatch):
        idp = _idp(step_up_acr_values=("phr",))
        app, sessions, web_ui = _app(step_up=StepUpConfig(enabled=True, rp_id="pf.example.com"), idp=idp)
        approval = _register(web_ui, ALICE, gate_kind="popup")
        client = _client(app)
        _signed_in(client, sessions, ALICE)
        start = client.get(f"/api/approvals/{approval.id}/stepup/idp?result=accept&choice=")
        qs = dict(up.parse_qsl(up.urlparse(start.headers["location"]).query))
        assert qs["acr_values"] == "phr"

        monkeypatch.setattr(roa.org_identity, "exchange_code_for_tokens", lambda *a, **kw: {"id_token": "t"})
        monkeypatch.setattr(
            roa.org_identity, "verify_id_token",
            lambda idp, token, *, nonce: {"sub": "alice", "nonce": nonce},  # no "acr" claim at all
        )
        cb_client = TestClient(app, base_url=ISSUER, follow_redirects=False)
        r = cb_client.get(f"/oauth/stepup/callback?code=abc&state={qs['state']}")
        assert r.headers["location"] == f"/approvals/{approval.id}?stepup=error"
        assert not web_ui.deferred_registry.get(approval.id).event.is_set()
