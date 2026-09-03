"""Tests for web/routes_settings.py -- settings on the web
(docs/https-connector-refactor-plan.md §16, W3/W4): the allowlisted action
dispatcher, CSRF/Origin checks, per-action argument validation, the org
config upload, the audit log download, and quit_app's confirmation gate.
"""
from __future__ import annotations

import subprocess
import time
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from privacyfence import daemon_main, resource_names, settings_controller as sc, update_checker
from privacyfence.web.routes_settings import _ALLOWED_ACTIONS, create_app

TOKEN = "test-token-0123456789"


def wait_until(predicate, timeout=2.0, interval=0.005) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def controller(tmp_path, monkeypatch):
    monkeypatch.setattr(resource_names, "_cache_file", lambda: tmp_path / "resource_name_cache.json")
    monkeypatch.setattr(update_checker, "_cache_file", lambda: tmp_path / "update_check_cache.json")
    monkeypatch.setattr(sc, "check_for_update", lambda **kw: None)
    monkeypatch.setattr(daemon_main, "load_org_config", lambda: {})

    org_dir_path = tmp_path / "org"
    org_dir_path.mkdir()
    monkeypatch.setattr(sc, "org_dir", lambda: org_dir_path)
    data_dir_path = tmp_path / "data"
    data_dir_path.mkdir()
    monkeypatch.setattr(sc, "data_dir", lambda: data_dir_path)

    config_path = tmp_path / "settings.yaml"
    config_path.write_text("auto_accept_rules: {}\nconnectors: {}\n", encoding="utf-8")

    connector_host = SimpleNamespace(set_connectors=lambda conns: None)
    return sc.SettingsController(str(config_path), connectors=[], connector_host=connector_host)


@pytest.fixture
def client(controller):
    app = create_app(controller, token=TOKEN)
    return TestClient(app, base_url="http://localhost")


def _authed(client):
    client.cookies.set("pf_session", TOKEN)


class TestSettingsPage:
    def test_unauthenticated_is_rejected(self, client):
        r = client.get("/settings")
        assert r.status_code == 401

    def test_authenticated_renders_the_shell_and_the_settings_document(self, client):
        _authed(client)
        r = client.get("/settings")
        assert r.status_code == 200
        assert "PrivacyFence — Settings" in r.text
        assert "pf-shell-nav" in r.text
        assert "__pfInitialState" in r.text

    def test_response_is_never_cached(self, client):
        _authed(client)
        r = client.get("/settings")
        assert r.headers.get("cache-control") == "no-store"

    def test_notifications_enabled_config_reaches_the_page(self, controller):
        # settings_page reads this off the controller's own live snapshot
        # (general.notifications_enabled), not the notifications_enabled=
        # closure arg below -- that arg is only ever the daemon-startup
        # default a snapshot's own general dict doesn't have yet, which
        # never actually happens once a controller is wired (see
        # test_notifications_detail_reflects_a_live_config_edit_without_
        # restart below for why the two must agree). So this test edits
        # the same config file the controller reads, the same way a real
        # `web.notifications.enabled: false` in settings.yaml would.
        cfg = controller._load_config()
        cfg.setdefault("web", {}).setdefault("notifications", {})["enabled"] = False
        controller._save_config(cfg)
        app = create_app(controller, token=TOKEN, notifications_enabled=True)
        c = TestClient(app, base_url="http://localhost")
        c.cookies.set("pf_session", TOKEN)
        r = c.get("/settings")
        assert "NOTIFICATIONS_ENABLED = false" in r.text

    def test_notifications_detail_reflects_a_live_config_edit_without_restart(self, controller):
        # settings_page reads notifications_enabled/detail off this
        # request's own fresh snapshot, not the notifications_detail=
        # "minimal" default create_app was built with (server.py's
        # daemon-startup value) -- so a set_notifications_detail() call in
        # between two GETs must change what the *second* GET renders, with
        # no server restart and no create_app() rebuild.
        app = create_app(controller, token=TOKEN, notifications_detail="minimal")
        c = TestClient(app, base_url="http://localhost")
        c.cookies.set("pf_session", TOKEN)
        before = c.get("/settings")
        assert 'NOTIFICATIONS_DETAIL = "minimal"' in before.text

        controller.set_notifications_detail("detailed")

        after = c.get("/settings")
        assert 'NOTIFICATIONS_DETAIL = "detailed"' in after.text


class TestActionDispatch:
    def test_unlisted_action_is_404_before_any_getattr(self, client, controller, monkeypatch):
        _authed(client)
        called = []
        monkeypatch.setattr(sc.SettingsController, "__getattribute__", lambda self, name: (
            called.append(name) or object.__getattribute__(self, name)
        ))
        r = client.post("/api/settings/_load_config", json={"csrf": TOKEN})
        assert r.status_code == 404
        assert "_load_config" not in called

    def test_dunder_and_snapshot_are_rejected(self, client):
        _authed(client)
        for action in ("snapshot", "__init__", "_save_config", "on_change"):
            r = client.post(f"/api/settings/{action}", json={"csrf": TOKEN})
            assert r.status_code == 404, action

    def test_every_allowed_action_actually_exists_on_the_controller(self, controller):
        for action in _ALLOWED_ACTIONS:
            assert callable(getattr(controller, action, None)), action

    def test_mechanical_action_returns_a_fresh_snapshot(self, client):
        _authed(client)
        r = client.post("/api/settings/toggle_pii_detection", json={"csrf": TOKEN})
        assert r.status_code == 200
        assert r.json()["general"]["pii_enabled"] is False

    def test_action_with_arguments_coerces_idx_to_int(self, client, controller):
        controller.add_rule_row("gmail.read_message")
        _authed(client)
        r = client.post(
            "/api/settings/update_rule_row",
            json={"op_key": "gmail.read_message", "idx": "0", "field": "rule_type", "value": "i_am_sender", "csrf": TOKEN},
        )
        assert r.status_code == 200
        rows = r.json()["rules"]["sections_by_connector"]["gmail"][0]["rows"]
        assert rows[0]["rule_type"] == "i_am_sender"

    def test_bad_idx_type_is_400_not_500(self, client):
        _authed(client)
        r = client.post(
            "/api/settings/update_rule_row",
            json={"op_key": "gmail.read_message", "idx": "not-a-number", "field": "value", "value": "x", "csrf": TOKEN},
        )
        assert r.status_code == 400

    def test_missing_required_argument_is_400(self, client):
        _authed(client)
        r = client.post("/api/settings/update_rule_row", json={"csrf": TOKEN})
        assert r.status_code == 400

    def test_connector_icons_are_augmented(self, client):
        _authed(client)
        r = client.post("/api/settings/refresh_connectors", json={"csrf": TOKEN})
        connectors = r.json()["connectors"]
        assert any("icon_data_uri" in c for c in connectors)

    def test_missing_csrf_is_401(self, client):
        _authed(client)
        r = client.post("/api/settings/toggle_pii_detection", json={})
        assert r.status_code == 401

    def test_wrong_csrf_is_401(self, client):
        _authed(client)
        r = client.post("/api/settings/toggle_pii_detection", json={"csrf": "wrong"})
        assert r.status_code == 401

    def test_cross_origin_is_403(self, client):
        _authed(client)
        r = client.post(
            "/api/settings/toggle_pii_detection", json={"csrf": TOKEN},
            headers={"Origin": "https://evil.example.com"},
        )
        assert r.status_code == 403

    def test_unauthenticated_is_401(self, client):
        r = client.post("/api/settings/toggle_pii_detection", json={"csrf": TOKEN})
        assert r.status_code == 401

    def test_malformed_json_is_400(self, client):
        _authed(client)
        r = client.post(
            "/api/settings/toggle_pii_detection", content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 400

    def test_set_notifications_detail_persists_and_returns_it_in_general(self, client):
        _authed(client)
        r = client.post("/api/settings/set_notifications_detail", json={"level": "detailed", "csrf": TOKEN})
        assert r.status_code == 200
        assert r.json()["general"]["notifications_detail"] == "detailed"

    def test_set_notifications_detail_rejects_an_unknown_level(self, client, controller):
        _authed(client)
        client.post("/api/settings/set_notifications_detail", json={"level": "standard", "csrf": TOKEN})
        r = client.post("/api/settings/set_notifications_detail", json={"level": "bogus", "csrf": TOKEN})
        assert r.status_code == 200
        # Same shape as set_log_level's own bad-value handling -- an
        # invalid value is a silent no-op snapshot, not a 400 (the
        # segmented control only ever sends its own three literals).
        assert r.json()["general"]["notifications_detail"] == "standard"


class TestConnectorAuthenticationEndToEnd:
    """§16.5's W6 "Done when": a connector can be authenticated from a
    browser, start to finish, with the page reflecting each step -- proven
    here for Slack (representative of the single-click OAuth connectors;
    Atlassian's own picker flow is covered end-to-end in
    test_settings_controller.py's TestPickResourceIndexWebMode, Telegram's
    multi-step flow in its own TestTelegramStartAuth/... classes there)."""

    def test_authenticate_connector_reflects_busy_then_connected(self, client, controller, monkeypatch):
        import threading

        release = threading.Event()

        def fake_authorize(**kwargs):
            release.wait(timeout=2)
            return {"access_token": "tok"}

        # _authenticate_slack's background thread marshals its `done`
        # callback back via call_on_main, which prefers a real AppKit run
        # loop (AppHelper.callAfter) whenever pyobjc is importable -- true
        # on this repo's own macOS CI runner, not just on a real Mac with
        # the native window actually open. Nothing here ever pumps that
        # run loop, so the real callAfter would never actually deliver
        # `done` and this test would hang until wait_until's own timeout.
        # Same fake-callAfter-runs-inline pattern test_settings_controller.py's
        # own TestPickResourceIndexWebMode/TestAuthenticateAtlassian use for
        # exactly this reason.
        monkeypatch.setattr(sc, "AppHelper", SimpleNamespace(callAfter=lambda f, *a, **k: f(*a, **k)))
        monkeypatch.setattr(daemon_main, "load_org_config", lambda: {"slack": {"client_id": "cid"}})
        monkeypatch.setattr(sc, "slack_authorize_interactive", fake_authorize)
        monkeypatch.setattr(controller, "refresh_connectors", lambda: controller._push_snapshot())
        _authed(client)

        r = client.post(
            "/api/settings/authenticate_connector", json={"connector": "slack", "csrf": TOKEN},
        )
        assert r.status_code == 200
        assert any(c["key"] == "slack" and c["busy"] for c in r.json()["connectors"])

        release.set()
        assert wait_until(lambda: "slack" not in controller._busy_connectors)

    def test_missing_org_config_surfaces_an_error_not_a_500(self, client):
        _authed(client)
        r = client.post(
            "/api/settings/authenticate_connector", json={"connector": "slack", "csrf": TOKEN},
        )
        assert r.status_code == 200
        assert r.json()["error"]


class TestOrgConfigUpload:
    def test_valid_bundle_is_installed(self, client, controller, tmp_path):
        _authed(client)
        r = client.post(
            "/api/settings/org_config/upload",
            data={"csrf": TOKEN},
            files={"file": ("org_config.json", b'{"version": 1, "google": {}}', "application/json")},
        )
        assert r.status_code == 200
        assert (sc.org_dir() / "org_config.json").exists()
        assert r.json()["error"] == ""

    def test_non_json_is_rejected_with_an_error_not_installed(self, client):
        _authed(client)
        r = client.post(
            "/api/settings/org_config/upload",
            data={"csrf": TOKEN},
            files={"file": ("x.json", b"not json at all", "application/json")},
        )
        assert r.status_code == 200
        assert r.json()["error"]
        assert not (sc.org_dir() / "org_config.json").exists()

    def test_json_without_version_key_is_rejected(self, client):
        _authed(client)
        r = client.post(
            "/api/settings/org_config/upload",
            data={"csrf": TOKEN},
            files={"file": ("x.json", b'{"google": {}}', "application/json")},
        )
        assert r.json()["error"]
        assert not (sc.org_dir() / "org_config.json").exists()

    def test_installed_file_is_0600(self, client):
        _authed(client)
        client.post(
            "/api/settings/org_config/upload",
            data={"csrf": TOKEN},
            files={"file": ("org_config.json", b'{"version": 1}', "application/json")},
        )
        mode = (sc.org_dir() / "org_config.json").stat().st_mode & 0o777
        assert mode == 0o600

    def test_oversized_upload_is_rejected(self, client, monkeypatch):
        from privacyfence.web import routes_settings
        monkeypatch.setattr(routes_settings, "MAX_ORG_CONFIG_BYTES", 10)
        _authed(client)
        r = client.post(
            "/api/settings/org_config/upload",
            data={"csrf": TOKEN},
            files={"file": ("x.json", b'{"version": 1, "padding": "xxxxxxxxxxxxxxxxxxxx"}', "application/json")},
        )
        assert r.status_code == 400

    def test_missing_csrf_is_401(self, client):
        _authed(client)
        r = client.post(
            "/api/settings/org_config/upload",
            files={"file": ("x.json", b'{"version": 1}', "application/json")},
        )
        assert r.status_code == 401


class TestAuditLogDownload:
    def test_nothing_to_export_is_404(self, client):
        _authed(client)
        r = client.get("/api/settings/audit_log/download")
        assert r.status_code == 404

    def test_current_week_activity_downloads_with_content_disposition(self, client, controller):
        from privacyfence.audit_log import AuditEntry, AuditLogger, current_week

        log_dir = sc.data_dir() / "logs" / "audit"
        log_dir.mkdir(parents=True)
        week = current_week()
        AuditLogger(str(log_dir)).record(AuditEntry(
            timestamp="2026-07-06T12:00:00+00:00", week=week, request_id="",
            connector="gmail", tool="gmail_get_message", tool_name="Read Gmail message",
            summary="s", sender="a@x.com", decision="approved", auto_accept_rule="", latency_seconds=1.0,
        ))
        _authed(client)
        r = client.get("/api/settings/audit_log/download")
        assert r.status_code == 200
        assert "attachment" in r.headers.get("content-disposition", "")
        assert r.headers.get("cache-control") == "no-store"

    def test_unauthenticated_is_401(self, client):
        r = client.get("/api/settings/audit_log/download")
        assert r.status_code == 401


class TestQuitApp:
    def test_unconfirmed_is_rejected(self, client, monkeypatch):
        called = []
        monkeypatch.setattr(sc, "rumps", SimpleNamespace(quit_application=lambda: called.append(True)))
        _authed(client)
        r = client.post("/api/settings/quit_app", json={"csrf": TOKEN})
        assert r.status_code == 400
        assert called == []

    def test_confirmed_calls_quit(self, client, monkeypatch):
        called = []
        monkeypatch.setattr(sc, "rumps", SimpleNamespace(quit_application=lambda: called.append(True)))
        _authed(client)
        r = client.post("/api/settings/quit_app", json={"csrf": TOKEN, "confirmed": True})
        assert r.status_code == 200
        assert called == [True]

    def test_disabled_by_allow_quit_config(self, controller, monkeypatch):
        from privacyfence.web.routes_settings import create_app as _create_app
        called = []
        monkeypatch.setattr(sc, "rumps", SimpleNamespace(quit_application=lambda: called.append(True)))
        app = _create_app(controller, token=TOKEN, allow_quit=False)
        client = TestClient(app, base_url="http://localhost")
        _authed(client)
        r = client.post("/api/settings/quit_app", json={"csrf": TOKEN, "confirmed": True})
        assert r.status_code == 403
        assert called == []

    def test_generic_dispatch_never_reaches_quit_app(self, client):
        # quit_app is deliberately absent from _ALLOWED_ACTIONS -- it only
        # has its own dedicated route (above), which carries the
        # confirmation gate the generic dispatcher doesn't know about.
        assert "quit_app" not in _ALLOWED_ACTIONS


class TestNoSubprocessFromHttp:
    """§16.2.4's standing rule: no route in this module ever shells out.
    Patches subprocess.run to explode, then exercises every route that used
    to (or plausibly could) reach one."""

    def test_no_route_here_calls_subprocess(self, client, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError(f"subprocess.run reached from an HTTP route: {a!r}")

        monkeypatch.setattr(subprocess, "run", _boom)
        _authed(client)

        client.get("/settings")
        client.post("/api/settings/toggle_pii_detection", json={"csrf": TOKEN})
        client.post(
            "/api/settings/org_config/upload", data={"csrf": TOKEN},
            files={"file": ("x.json", b'{"version": 1}', "application/json")},
        )
        client.get("/api/settings/audit_log/download")
