"""privacyfence.settings_controller -- the domain/business logic behind the
webview settings window (issue #120).

This module's coverage was moved here from test_menu_bar.py's pre-#120 rule/
grant/PII/privacy/connector/audit/org-config tests (see git history) --
same behavior, now exercised through SettingsController's methods instead of
PrivacyFenceMenuBar's. Native-picker-specific tests (_osascript_pick-driven
rule/policy selection, the old int-value/list-value rumps.Window prompts)
were dropped rather than ported, since the Auto-accept Rules page now edits
rule_type/value as plain text (see settings_controller.py's own docstring on
why) -- there is no picker left to test.

Also covers the cross-thread AppHelper.callAfter marshaling contract
(_run_async/on_change) that used to live in test_menu_bar.py's "P6" module
docstring -- see TestRunAsyncMarshaling and TestOnChangeMarshaling below for
why that still matters here.

One follow-up feature rebuilt after the initial #120 pass per user
direction (see PR history): Telegram's in-webview multi-step sign-in
(TestTelegramStartAuth/TestTelegramSubmitCode/TestTelegramSubmit2FA/
TestTelegramCancelAuth, replacing the native rumps.Window-based flow --
telethon is mocked via MagicMock/AsyncMock, the same house style
test_telegram_client.py's own tests already use, rather than the
hand-rolled fake class the deleted native-prompt tests used).

Suggestion-priority reordering (the Rules page's old "Always-allow
Suggestion Order" section: move up/down, exclude/re-include) was one such
restored feature but is gone again as of issue #151 -- every auto-accept
rule that plausibly matches an item now gets its own "Always allow" button
in the popup, so there's nothing left to prioritize or exclude. See
git history for the removed TestSuggestionPriorityState/
TestSuggestionPriorityMutators coverage.
"""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from privacyfence import auto_accept, daemon_main, resource_names, settings_controller as sc, update_checker


def wait_until(predicate, timeout=2.0, interval=0.005) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _drain_run_async(recorded) -> None:
    while recorded:
        func, args, kwargs = recorded.pop(0)
        func(*args, **kwargs)


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

    ipc_calls = []
    ipc_server = SimpleNamespace(
        set_connectors=lambda conns: ipc_calls.append(conns),
        set_unattended_changed_listener=lambda callback: None,
    )

    ctrl = sc.SettingsController(str(config_path), connectors=[], ipc_server=ipc_server)
    ctrl._ipc_calls = ipc_calls
    return ctrl


class TestRunAsyncMarshaling:
    """_run_async is the mechanism every threaded flow in this module funnels
    through. If it ever regresses to invoking on_done directly on the worker
    thread, the AppKit-not-thread-safe invariant every mutation depends on
    breaks silently."""

    def test_success_result_never_delivered_directly_on_worker_thread(self, monkeypatch):
        recorded = []
        monkeypatch.setattr(sc, "AppHelper", SimpleNamespace(callAfter=lambda f, *a, **k: recorded.append((f, a, k))))

        done_calls = []
        work_thread = {}

        def work():
            work_thread["thread"] = threading.current_thread()
            return "alice@example.com"

        def done(ok, result):
            done_calls.append((ok, result))

        sc._run_async(work, done)

        assert wait_until(lambda: recorded)
        assert work_thread["thread"] is not threading.current_thread()
        assert done_calls == []

        func, args, kwargs = recorded[0]
        assert args == (True, "alice@example.com")
        func(*args, **kwargs)
        assert done_calls == [(True, "alice@example.com")]

    def test_exception_in_work_is_also_marshaled_not_raised_on_worker_thread(self, monkeypatch):
        recorded = []
        monkeypatch.setattr(sc, "AppHelper", SimpleNamespace(callAfter=lambda f, *a, **k: recorded.append((f, a, k))))
        boom = ValueError("auth failed")

        def work():
            raise boom

        done_calls = []
        sc._run_async(work, lambda ok, result: done_calls.append((ok, result)))

        assert wait_until(lambda: recorded)
        func, args, kwargs = recorded[0]
        assert args == (False, boom)
        func(*args, **kwargs)
        assert done_calls == [(False, boom)]


class TestOnChangeMarshaling:
    """Rule changes from the IPC server's own thread (e.g. an "Always allow"
    confirmation from the approval popup) must marshal onto the main thread
    via AppHelper.callAfter before touching on_change (which may drive
    AppKit/the webview)."""

    def test_reload_from_background_thread_schedules_but_does_not_push_inline(self, controller, monkeypatch):
        recorded = []
        monkeypatch.setattr(sc, "AppHelper", SimpleNamespace(
            callAfter=lambda f, *a, **k: recorded.append((f, a, k, threading.current_thread()))
        ))
        pushed = []
        controller.on_change = lambda state: pushed.append(threading.current_thread())

        bg_done = threading.Event()

        def ipc_server_thread_body():
            auto_accept.reload_rules({"gmail.read_message": [{"rule": "i_am_sender"}]})
            bg_done.set()

        t = threading.Thread(target=ipc_server_thread_body)
        t.start()
        t.join(timeout=2)
        assert bg_done.is_set()

        assert pushed == []
        assert len(recorded) == 1
        func, args, kwargs, calling_thread = recorded[0]
        assert calling_thread is not threading.current_thread()

        func(*args, **kwargs)
        assert pushed == [threading.current_thread()]


class TestConfigHelpers:
    def test_load_config_round_trips_yaml(self, controller, tmp_path):
        config_path = tmp_path / "other.yaml"
        config_path.write_text("connectors:\n  gmail:\n    enabled: false\n", encoding="utf-8")
        controller._config_path = str(config_path)

        assert controller._load_config() == {"connectors": {"gmail": {"enabled": False}}}

    def test_load_config_missing_file_returns_empty_dict(self, controller, tmp_path):
        controller._config_path = str(tmp_path / "does-not-exist.yaml")
        assert controller._load_config() == {}

    def test_load_config_malformed_yaml_returns_empty_dict_not_raise(self, controller, tmp_path):
        config_path = tmp_path / "bad.yaml"
        config_path.write_text(":\n  - not: [valid yaml", encoding="utf-8")
        controller._config_path = str(config_path)

        assert controller._load_config() == {}

    def test_save_config_writes_yaml_readable_back(self, controller):
        controller._save_config({"connectors": {"slack": {"enabled": True}}})

        assert controller._load_config() == {"connectors": {"slack": {"enabled": True}}}

    def test_save_config_write_failure_is_logged_not_raised(self, controller, monkeypatch):
        monkeypatch.setattr(sc, "open", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")), raising=False)

        controller._save_config({"a": 1})  # must not raise

    def test_save_and_reload_persists_and_triggers_rule_reload(self, controller, monkeypatch):
        reload_calls = []
        monkeypatch.setattr(sc, "reload_rules", lambda rules: reload_calls.append(rules))

        controller._save_and_reload({"auto_accept_rules": {"gmail.read_message": [{"rule": "i_am_sender"}]}})

        assert reload_calls == [{"gmail.read_message": [{"rule": "i_am_sender"}]}]
        assert controller._load_config()["auto_accept_rules"] == {"gmail.read_message": [{"rule": "i_am_sender"}]}

    def test_save_and_reload_swallows_reload_failures(self, controller, monkeypatch):
        monkeypatch.setattr(sc, "reload_rules", lambda rules: (_ for _ in ()).throw(RuntimeError("boom")))

        controller._save_and_reload({})  # must not raise


class TestExtractDriveId:
    def test_bare_id_is_accepted_as_is(self):
        assert sc._extract_drive_id("FOLDER1") == "FOLDER1"

    def test_folder_url_extracts_the_id(self):
        url = "https://drive.google.com/drive/folders/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms?usp=sharing"
        assert sc._extract_drive_id(url) == "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"

    def test_file_url_extracts_the_id(self):
        url = "https://docs.google.com/spreadsheets/d/1AbCdEf12345/edit#gid=0"
        assert sc._extract_drive_id(url) == "1AbCdEf12345"

    def test_unparseable_text_returns_empty_string(self):
        assert sc._extract_drive_id("not a url or id, has spaces") == ""


class TestShortId:
    def test_short_id_passes_through_unchanged(self):
        assert sc._short_id("F1") == "F1"

    def test_long_id_is_truncated_with_ellipsis(self):
        long_id = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
        result = sc._short_id(long_id)
        assert result.startswith("1BxiMVs0")
        assert result.endswith("E2upms")
        assert "…" in result


class TestParseFormatRuleValue:
    def test_list_value_rule_splits_on_comma(self):
        assert sc._parse_rule_value("trusted_sender_domain", "a.com, b.com") == ["a.com", "b.com"]

    def test_int_value_rule_parses_integer(self):
        assert sc._parse_rule_value("age_threshold_days", "30") == 30

    def test_int_value_rule_non_numeric_kept_as_typed(self):
        assert sc._parse_rule_value("age_threshold_days", "not-a-number") == "not-a-number"

    def test_unknown_rule_kept_as_plain_string(self):
        assert sc._parse_rule_value("some_future_rule", "hello") == "hello"

    def test_empty_text_is_none(self):
        assert sc._parse_rule_value("trusted_sender_domain", "   ") is None

    def test_format_round_trips_list(self):
        assert sc._format_rule_value("trusted_sender_domain", ["a.com", "b.com"]) == "a.com, b.com"

    def test_format_none_is_empty_string(self):
        assert sc._format_rule_value("i_am_sender", None) == ""


class TestPiiDetection:
    def test_toggle_flips_and_saves(self, controller):
        controller.toggle_pii_detection()
        assert controller._load_config()["pii_detection"]["enabled"] is False

    def test_toggling_twice_re_enables(self, controller):
        controller.toggle_pii_detection()
        controller.toggle_pii_detection()
        assert controller._load_config()["pii_detection"]["enabled"] is True

    def test_hot_reloads_live_detector_state(self, controller):
        from privacyfence import pii_detector

        assert pii_detector.is_pii_detection_enabled() is True
        controller.toggle_pii_detection()
        assert pii_detector.is_pii_detection_enabled() is False

    def test_category_flips_and_saves(self, controller):
        controller.toggle_pii_category("detect_ip_addresses")
        assert controller._load_config()["pii_detection"]["detect_ip_addresses"] is False

    def test_category_toggle_is_a_no_op_while_master_switch_off(self, controller):
        controller.toggle_pii_detection()  # now disabled
        before = controller._load_config()

        controller.toggle_pii_category("detect_ip_addresses")

        assert controller._load_config() == before

    def test_categories_toggle_independently(self, controller):
        controller.toggle_pii_category("detect_ip_addresses")

        cfg = controller._load_config()
        assert cfg["pii_detection"]["detect_ip_addresses"] is False
        assert cfg["pii_detection"].get("detect_financial_figures", True) is True


class TestUpdateCheck:
    def test_toggle_enabled_flips_and_saves(self, controller):
        controller.toggle_update_check()
        assert controller._load_config()["update_check"]["enabled"] is False

    def test_toggle_beta_flips_saves_and_checks_immediately(self, controller, monkeypatch):
        calls = []
        monkeypatch.setattr(controller, "check_for_updates_now", lambda: calls.append(1) or controller.snapshot())

        controller.toggle_update_check_beta()

        assert controller._load_config()["update_check"]["include_beta"] is True
        assert calls == [1]

    def test_timer_disabled_never_checks(self, controller, monkeypatch):
        controller._save_config({"update_check": {"enabled": False}})
        calls = []
        monkeypatch.setattr(controller, "check_for_updates_now", lambda: calls.append(1))

        controller.on_update_check_timer()

        assert calls == []

    def test_timer_enabled_checks(self, controller, monkeypatch):
        calls = []
        monkeypatch.setattr(controller, "check_for_updates_now", lambda: calls.append(1))

        controller.on_update_check_timer()

        assert calls == [1]

    def test_check_for_updates_now_passes_include_beta(self, controller, monkeypatch):
        controller._save_config({"update_check": {"enabled": True, "include_beta": True}})
        captured = {}

        def fake_run_async(work, on_done):
            captured["include_beta"] = work()

        monkeypatch.setattr(sc, "_run_async", fake_run_async)
        monkeypatch.setattr(sc, "check_for_update", lambda **kw: kw.get("include_beta"))

        controller.check_for_updates_now()

        assert captured["include_beta"] is True

    def test_update_check_done_failure_does_not_touch_latest_update(self, controller):
        controller._latest_update = None
        controller._on_update_check_done(False, RuntimeError("boom"))
        assert controller._latest_update is None

    def test_update_check_done_success_pushes_state(self, controller, monkeypatch):
        pushed = []
        controller.on_change = lambda state: pushed.append(state)
        monkeypatch.setattr(sc, "should_notify_now", lambda: False)

        result = update_checker.UpdateCheckResult(
            latest_version="v2.2.0", release_url="https://x", is_beta=False, is_update_available=False,
        )
        controller._on_update_check_done(True, result)

        assert controller._latest_update is result
        assert len(pushed) == 1

    def test_update_available_and_should_notify_shows_alert(self, controller, monkeypatch):
        monkeypatch.setattr(sc, "should_notify_now", lambda: True)
        alerts = []
        monkeypatch.setattr(sc, "rumps", SimpleNamespace(alert=lambda **kw: alerts.append(kw) or 0))
        controller.on_change = lambda state: None

        result = update_checker.UpdateCheckResult(
            latest_version="v2.2.0", release_url="https://x/tag/v2.2.0", is_beta=False, is_update_available=True,
        )
        controller._on_update_check_done(True, result)

        assert len(alerts) == 1
        assert "2.2.0" in alerts[0]["message"]

    def test_show_update_available_alert_download_opens_url(self, controller, monkeypatch):
        monkeypatch.setattr(sc, "rumps", SimpleNamespace(alert=lambda **kw: 1))
        open_calls = []
        monkeypatch.setattr(sc.subprocess, "run", lambda args, **kw: open_calls.append(args))

        result = update_checker.UpdateCheckResult(
            latest_version="v2.2.0", release_url="https://github.com/x/releases/tag/v2.2.0",
            is_beta=False, is_update_available=True,
        )
        controller._show_update_available_alert(result)

        assert open_calls == [["open", "https://github.com/x/releases/tag/v2.2.0"]]

    def test_show_update_available_alert_skip_marks_skipped(self, controller, monkeypatch):
        monkeypatch.setattr(sc, "rumps", SimpleNamespace(alert=lambda **kw: 0))
        skipped = []
        monkeypatch.setattr(sc, "mark_skipped", lambda version: skipped.append(version))

        result = update_checker.UpdateCheckResult(
            latest_version="v2.2.0", release_url="https://x", is_beta=False, is_update_available=True,
        )
        controller._show_update_available_alert(result)

        assert skipped == ["v2.2.0"]

    def test_show_update_available_alert_remind_later(self, controller, monkeypatch):
        monkeypatch.setattr(sc, "rumps", SimpleNamespace(alert=lambda **kw: -1))
        remind_calls = []
        monkeypatch.setattr(sc, "mark_remind_later", lambda: remind_calls.append(1))

        result = update_checker.UpdateCheckResult(
            latest_version="v2.2.0", release_url="https://x", is_beta=False, is_update_available=True,
        )
        controller._show_update_available_alert(result)

        assert remind_calls == [1]


class TestOrgConfigInstall:
    def _fake_picker(self, monkeypatch, path):
        monkeypatch.setattr(sc.subprocess, "run", lambda cmd, **kw: SimpleNamespace(stdout=(path or "")))

    def test_cancelled_picker_makes_no_change(self, controller, monkeypatch):
        self._fake_picker(monkeypatch, None)

        controller.install_org_config()

        assert not (sc.org_dir() / "org_config.json").exists()

    def test_non_json_file_sets_error(self, controller, monkeypatch, tmp_path):
        src = tmp_path / "bad.json"
        src.write_text("not valid json", encoding="utf-8")
        self._fake_picker(monkeypatch, str(src))

        controller.install_org_config()

        assert controller.error
        assert not (sc.org_dir() / "org_config.json").exists()

    def test_json_without_version_field_is_rejected(self, controller, monkeypatch, tmp_path):
        src = tmp_path / "bundle.json"
        src.write_text(json.dumps({"google": {"client_id": "x"}}), encoding="utf-8")
        self._fake_picker(monkeypatch, str(src))

        controller.install_org_config()

        assert controller.error
        assert not (sc.org_dir() / "org_config.json").exists()

    def test_valid_bundle_is_installed(self, controller, monkeypatch, tmp_path):
        src = tmp_path / "bundle.json"
        bundle = {"version": 1, "org_name": "Acme", "google": {"client_id": "x", "client_secret": "y"}}
        src.write_text(json.dumps(bundle), encoding="utf-8")
        self._fake_picker(monkeypatch, str(src))

        controller.install_org_config()

        installed = json.loads((sc.org_dir() / "org_config.json").read_text(encoding="utf-8"))
        assert installed == bundle
        assert controller.error == ""

    def test_snapshot_reflects_installed_state(self, controller, monkeypatch, tmp_path):
        src = tmp_path / "bundle.json"
        src.write_text(json.dumps({"version": 1}), encoding="utf-8")
        self._fake_picker(monkeypatch, str(src))

        state = controller.install_org_config()

        assert state["general"]["org_installed"] is True
        assert state["general"]["org_button_label"] == "Install/Update Organization Config…"

    def test_snapshot_not_installed_label(self, controller):
        state = controller.snapshot()
        assert state["general"]["org_installed"] is False
        assert state["general"]["org_button_label"] == "Install Organization Config…"


class TestToggleConnector:
    def test_flips_enabled_flag_and_refreshes(self, controller, monkeypatch):
        refresh_calls = []
        monkeypatch.setattr(controller, "refresh_connectors", lambda: refresh_calls.append(1) or controller.snapshot())

        controller.toggle_connector("gmail")

        cfg = controller._load_config()
        assert cfg["connectors"]["gmail"]["enabled"] is False
        assert refresh_calls == [1]

    def test_toggling_twice_re_enables(self, controller, monkeypatch):
        monkeypatch.setattr(controller, "refresh_connectors", lambda: controller.snapshot())

        controller.toggle_connector("gmail")
        controller.toggle_connector("gmail")

        assert controller._load_config()["connectors"]["gmail"]["enabled"] is True


class TestRefreshConnectors:
    def test_updates_connectors_and_pushes_to_ipc_server_after_drain(self, controller, monkeypatch):
        recorded = []
        monkeypatch.setattr(sc, "AppHelper", SimpleNamespace(callAfter=lambda f, *a, **k: recorded.append((f, a, k))))
        monkeypatch.setattr(daemon_main, "build_connectors", lambda cfg, org: [SimpleNamespace(name="drive")])

        controller.refresh_connectors()

        assert wait_until(lambda: len(recorded) == 1)
        func, args, kwargs = recorded[0]
        func(*args, **kwargs)

        assert controller._connectors == ["drive"]
        assert controller._ipc_calls == [[SimpleNamespace(name="drive")]]


class TestAuthenticateDispatch:
    @pytest.mark.parametrize("cname,method", [
        ("gmail", "_authenticate_google"), ("drive", "_authenticate_google"),
        ("contacts", "_authenticate_google"), ("calendar", "_authenticate_google"),
        ("tasks", "_authenticate_google"), ("slack", "_authenticate_slack"),
        ("salesforce", "_authenticate_salesforce"), ("jira", "_authenticate_atlassian"),
        ("confluence", "_authenticate_atlassian"),
    ])
    def test_dispatches_to_the_right_per_service_method(self, controller, monkeypatch, cname, method):
        calls = []
        monkeypatch.setattr(controller, method, lambda *a: calls.append((cname, a)))

        controller.authenticate_connector(cname)

        assert len(calls) == 1

    def test_telegram_is_not_routed_through_the_generic_dispatch(self, controller, monkeypatch):
        # Telegram's phone/code/2FA flow is routed client-side into its own
        # modal instead (see settings_window_html.py) -- production JS never
        # posts authenticate_connector for it, but a stray call must still be
        # a harmless no-op rather than an error.
        run_async_calls = []
        monkeypatch.setattr(sc, "_run_async", lambda *a: run_async_calls.append(a))

        controller.authenticate_connector("telegram")  # must not raise

        assert run_async_calls == []


class TestAuthenticateGoogle:
    def test_missing_org_config_sets_error_without_running_flow(self, controller, monkeypatch):
        run_async_calls = []
        monkeypatch.setattr(sc, "_run_async", lambda *a: run_async_calls.append(a))

        controller._authenticate_google("gmail", {})

        assert controller.error
        assert run_async_calls == []

    def test_runs_authorize_and_check_connection_marks_busy_then_refreshes(self, controller, monkeypatch):
        recorded = []
        monkeypatch.setattr(sc, "AppHelper", SimpleNamespace(callAfter=lambda f, *a, **k: recorded.append((f, a, k))))
        calls = []

        class FakeGmailClient:
            def __init__(self, client_config, token_file):
                calls.append(("init", client_config))

            def authorize_interactive(self):
                calls.append(("authorize",))

            def check_connection(self):
                calls.append(("check",))
                return "me@example.com"

        monkeypatch.setitem(sc._GOOGLE_CLIENTS, "gmail", FakeGmailClient)
        refresh_calls = []
        # refresh_connectors() itself runs a second _run_async hop -- stubbed
        # out here (TestRefreshConnectors below covers that hop directly) so
        # this test isn't racing a second background thread's append into
        # `recorded` against _drain_run_async's synchronous while-loop.
        monkeypatch.setattr(controller, "refresh_connectors", lambda: refresh_calls.append(1))

        controller._authenticate_google("gmail", {"google": {"client_id": "i", "client_secret": "s"}})

        assert "gmail" in controller._busy_connectors
        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)

        assert calls == [("init", {"installed": {"client_id": "i", "client_secret": "s"}}), ("authorize",), ("check",)]
        assert "gmail" not in controller._busy_connectors
        assert refresh_calls == [1]

    def test_failed_auth_sets_error_and_clears_busy_without_refreshing(self, controller, monkeypatch):
        recorded = []
        monkeypatch.setattr(sc, "AppHelper", SimpleNamespace(callAfter=lambda f, *a, **k: recorded.append((f, a, k))))
        refresh_calls = []
        monkeypatch.setattr(controller, "refresh_connectors", lambda: refresh_calls.append(1))

        class FailingGmailClient:
            def __init__(self, client_config, token_file):
                pass

            def authorize_interactive(self):
                raise RuntimeError("user closed browser")

        monkeypatch.setitem(sc._GOOGLE_CLIENTS, "gmail", FailingGmailClient)

        controller._authenticate_google("gmail", {"google": {"client_id": "i", "client_secret": "s"}})

        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)

        assert "user closed browser" in controller.error
        assert refresh_calls == []
        assert "gmail" not in controller._busy_connectors


class TestAuthenticateSlack:
    def test_missing_org_config_sets_error(self, controller, monkeypatch):
        run_async_calls = []
        monkeypatch.setattr(sc, "_run_async", lambda *a: run_async_calls.append(a))

        controller._authenticate_slack({})

        assert controller.error
        assert run_async_calls == []

    def test_success_refreshes(self, controller, monkeypatch):
        recorded = []
        monkeypatch.setattr(sc, "AppHelper", SimpleNamespace(callAfter=lambda f, *a, **k: recorded.append((f, a, k))))
        monkeypatch.setattr(sc, "slack_authorize_interactive", lambda **kw: {"team_name": "Acme"})
        refresh_calls = []
        monkeypatch.setattr(controller, "refresh_connectors", lambda: refresh_calls.append(1))

        controller._authenticate_slack({"slack": {"client_id": "id", "client_secret": "s"}})

        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)

        assert refresh_calls == [1]
        assert controller.error == ""


class TestAuthenticateSalesforce:
    def test_missing_org_config_sets_error(self, controller, monkeypatch):
        run_async_calls = []
        monkeypatch.setattr(sc, "_run_async", lambda *a: run_async_calls.append(a))

        controller._authenticate_salesforce({})

        assert controller.error
        assert run_async_calls == []

    def test_success_refreshes(self, controller, monkeypatch):
        recorded = []
        monkeypatch.setattr(sc, "AppHelper", SimpleNamespace(callAfter=lambda f, *a, **k: recorded.append((f, a, k))))
        monkeypatch.setattr(sc, "salesforce_authorize_interactive", lambda **kw: {"instance_url": "https://x.salesforce.com"})
        refresh_calls = []
        monkeypatch.setattr(controller, "refresh_connectors", lambda: refresh_calls.append(1))

        controller._authenticate_salesforce({"salesforce": {"consumer_key": "ck"}})

        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)

        assert refresh_calls == [1]


class TestAuthenticateAtlassian:
    def test_missing_org_config_sets_error(self, controller, monkeypatch):
        run_async_calls = []
        monkeypatch.setattr(sc, "_run_async", lambda *a: run_async_calls.append(a))

        controller._authenticate_atlassian({})

        assert controller.error
        assert run_async_calls == []

    def test_success_marks_busy_for_both_jira_and_confluence(self, controller, monkeypatch):
        recorded = []
        monkeypatch.setattr(sc, "AppHelper", SimpleNamespace(callAfter=lambda f, *a, **k: recorded.append((f, a, k))))
        monkeypatch.setattr(sc, "atlassian_authorize_interactive", lambda **kw: {"site_url": "https://acme.atlassian.net"})
        refresh_calls = []
        monkeypatch.setattr(controller, "refresh_connectors", lambda: refresh_calls.append(1))

        controller._authenticate_atlassian({"atlassian": {"client_id": "ci"}})

        assert {"jira", "confluence"} <= controller._busy_connectors
        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)

        assert refresh_calls == [1]
        assert "jira" not in controller._busy_connectors
        assert "confluence" not in controller._busy_connectors

    def test_multiple_sites_pick_resource_uses_osascript_picker(self, controller, monkeypatch):
        recorded = []
        monkeypatch.setattr(sc, "AppHelper", SimpleNamespace(callAfter=lambda f, *a, **k: recorded.append((f, a, k))))
        monkeypatch.setattr(sc, "_osascript_pick", lambda **kw: "https://b.atlassian.net")
        monkeypatch.setattr(controller, "refresh_connectors", lambda: None)

        captured = {}

        def fake_authorize(**kwargs):
            resources = [
                {"url": "https://a.atlassian.net", "id": "a"},
                {"url": "https://b.atlassian.net", "id": "b"},
            ]
            chosen = kwargs["pick_resource"](resources)
            captured["site_url"] = chosen["url"]
            return {"site_url": chosen["url"]}

        monkeypatch.setattr(sc, "atlassian_authorize_interactive", fake_authorize)

        controller._authenticate_atlassian({"atlassian": {"client_id": "ci"}})

        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)
        assert captured["site_url"] == "https://b.atlassian.net"


class TestTelegramStartAuth:
    """telethon is mocked the same way test_telegram_client.py's own tests
    do -- MagicMock() with AsyncMock() for the awaited methods, rather than
    a hand-rolled fake class (the pattern the pre-#120 native-prompt flow's
    tests used) -- see that file's TestCheckConnection etc. for the house
    style this follows."""

    def test_missing_credentials_sets_error_without_running_flow(self, controller, monkeypatch):
        monkeypatch.setattr(sc, "telegram_app_credentials", lambda: None)
        run_async_calls = []
        monkeypatch.setattr(sc, "_run_async", lambda *a: run_async_calls.append(a))

        controller.telegram_start_auth("+123456789")

        assert controller.error
        assert run_async_calls == []

    def test_empty_phone_sets_a_field_error_without_running_flow(self, controller, monkeypatch):
        monkeypatch.setattr(sc, "telegram_app_credentials", lambda: (123, "hash"))
        run_async_calls = []
        monkeypatch.setattr(sc, "_run_async", lambda *a: run_async_calls.append(a))

        controller.telegram_start_auth("   ")

        assert controller._telegram_auth == {"step": "phone", "error": "Enter a phone number."}
        assert run_async_calls == []

    def test_happy_path_stores_phone_code_hash_and_advances_to_code_step(self, controller, monkeypatch):
        monkeypatch.setattr(sc, "telegram_app_credentials", lambda: (123, "hash"))
        recorded = []
        monkeypatch.setattr(sc, "AppHelper", SimpleNamespace(callAfter=lambda f, *a, **k: recorded.append((f, a, k))))

        fake_client = MagicMock()
        fake_client.connect = AsyncMock()
        fake_client.disconnect = AsyncMock()
        fake_client.send_code_request = AsyncMock(return_value=SimpleNamespace(phone_code_hash="hash-123"))
        monkeypatch.setattr("telethon.TelegramClient", lambda *a, **kw: fake_client)

        state = controller.telegram_start_auth("+123456789")

        assert "telegram" in controller._busy_connectors
        assert state["telegram_auth"] == {"step": "phone", "error": ""}

        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)

        assert controller._telegram_auth == {
            "step": "code", "phone": "+123456789", "phone_code_hash": "hash-123", "error": "",
        }
        assert "telegram" not in controller._busy_connectors
        fake_client.send_code_request.assert_awaited_once_with("+123456789")
        fake_client.disconnect.assert_awaited_once()

    def test_connect_failure_keeps_phone_step_with_error(self, controller, monkeypatch):
        monkeypatch.setattr(sc, "telegram_app_credentials", lambda: (123, "hash"))
        recorded = []
        monkeypatch.setattr(sc, "AppHelper", SimpleNamespace(callAfter=lambda f, *a, **k: recorded.append((f, a, k))))

        fake_client = MagicMock()
        fake_client.connect = AsyncMock(side_effect=RuntimeError("network down"))
        fake_client.disconnect = AsyncMock()
        monkeypatch.setattr("telethon.TelegramClient", lambda *a, **kw: fake_client)

        controller.telegram_start_auth("+123456789")

        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)

        assert controller._telegram_auth["step"] == "phone"
        assert "network down" in controller._telegram_auth["error"]


class TestTelegramSubmitCode:
    def test_no_flow_in_progress_is_a_no_op(self, controller, monkeypatch):
        run_async_calls = []
        monkeypatch.setattr(sc, "_run_async", lambda *a: run_async_calls.append(a))

        controller.telegram_submit_code("12345")

        assert run_async_calls == []

    def test_wrong_step_is_a_no_op(self, controller, monkeypatch):
        controller._telegram_auth = {"step": "password", "error": ""}
        run_async_calls = []
        monkeypatch.setattr(sc, "_run_async", lambda *a: run_async_calls.append(a))

        controller.telegram_submit_code("12345")

        assert run_async_calls == []

    def test_empty_code_sets_a_field_error(self, controller):
        controller._telegram_auth = {"step": "code", "phone": "+1", "phone_code_hash": "h", "error": ""}

        controller.telegram_submit_code("   ")

        assert controller._telegram_auth["error"] == "Enter the verification code."

    def test_happy_path_signs_in_and_refreshes(self, controller, monkeypatch):
        monkeypatch.setattr(sc, "telegram_app_credentials", lambda: (123, "hash"))
        controller._telegram_auth = {
            "step": "code", "phone": "+123456789", "phone_code_hash": "hash-123", "error": "",
        }
        recorded = []
        monkeypatch.setattr(sc, "AppHelper", SimpleNamespace(callAfter=lambda f, *a, **k: recorded.append((f, a, k))))
        refresh_calls = []
        monkeypatch.setattr(controller, "refresh_connectors", lambda: refresh_calls.append(1))

        fake_client = MagicMock()
        fake_client.connect = AsyncMock()
        fake_client.disconnect = AsyncMock()
        fake_client.sign_in = AsyncMock()
        fake_client.get_me = AsyncMock(return_value=SimpleNamespace(first_name="Jane", last_name="Doe"))
        monkeypatch.setattr("telethon.TelegramClient", lambda *a, **kw: fake_client)

        controller.telegram_submit_code("12345")

        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)

        fake_client.sign_in.assert_awaited_once_with("+123456789", "12345", phone_code_hash="hash-123")
        assert controller._telegram_auth is None
        assert controller.error == ""
        assert refresh_calls == [1]

    def test_2fa_required_advances_to_password_step_without_error(self, controller, monkeypatch):
        monkeypatch.setattr(sc, "telegram_app_credentials", lambda: (123, "hash"))
        controller._telegram_auth = {"step": "code", "phone": "+1", "phone_code_hash": "h", "error": ""}
        recorded = []
        monkeypatch.setattr(sc, "AppHelper", SimpleNamespace(callAfter=lambda f, *a, **k: recorded.append((f, a, k))))

        from telethon.errors import SessionPasswordNeededError

        fake_client = MagicMock()
        fake_client.connect = AsyncMock()
        fake_client.disconnect = AsyncMock()
        fake_client.sign_in = AsyncMock(side_effect=SessionPasswordNeededError(request=None))
        monkeypatch.setattr("telethon.TelegramClient", lambda *a, **kw: fake_client)

        controller.telegram_submit_code("12345")

        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)

        assert controller._telegram_auth["step"] == "password"
        assert controller._telegram_auth["error"] == ""

    def test_wrong_code_sets_error_and_stays_on_code_step(self, controller, monkeypatch):
        monkeypatch.setattr(sc, "telegram_app_credentials", lambda: (123, "hash"))
        controller._telegram_auth = {"step": "code", "phone": "+1", "phone_code_hash": "h", "error": ""}
        recorded = []
        monkeypatch.setattr(sc, "AppHelper", SimpleNamespace(callAfter=lambda f, *a, **k: recorded.append((f, a, k))))

        fake_client = MagicMock()
        fake_client.connect = AsyncMock()
        fake_client.disconnect = AsyncMock()
        fake_client.sign_in = AsyncMock(side_effect=RuntimeError("invalid code"))
        monkeypatch.setattr("telethon.TelegramClient", lambda *a, **kw: fake_client)

        controller.telegram_submit_code("00000")

        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)

        assert controller._telegram_auth["step"] == "code"
        assert "invalid code" in controller._telegram_auth["error"]


class TestTelegramSubmit2FA:
    def test_wrong_step_is_a_no_op(self, controller, monkeypatch):
        controller._telegram_auth = {"step": "code", "error": ""}
        run_async_calls = []
        monkeypatch.setattr(sc, "_run_async", lambda *a: run_async_calls.append(a))

        controller.telegram_submit_2fa("pw")

        assert run_async_calls == []

    def test_empty_password_sets_a_field_error(self, controller):
        controller._telegram_auth = {"step": "password", "error": ""}

        controller.telegram_submit_2fa("   ")

        assert controller._telegram_auth["error"]

    def test_happy_path_signs_in_and_refreshes(self, controller, monkeypatch):
        monkeypatch.setattr(sc, "telegram_app_credentials", lambda: (123, "hash"))
        controller._telegram_auth = {"step": "password", "error": ""}
        recorded = []
        monkeypatch.setattr(sc, "AppHelper", SimpleNamespace(callAfter=lambda f, *a, **k: recorded.append((f, a, k))))
        refresh_calls = []
        monkeypatch.setattr(controller, "refresh_connectors", lambda: refresh_calls.append(1))

        fake_client = MagicMock()
        fake_client.connect = AsyncMock()
        fake_client.disconnect = AsyncMock()
        fake_client.sign_in = AsyncMock()
        fake_client.get_me = AsyncMock(return_value=SimpleNamespace(first_name="Jane", last_name="Doe"))
        monkeypatch.setattr("telethon.TelegramClient", lambda *a, **kw: fake_client)

        controller.telegram_submit_2fa("my-2fa-password")

        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)

        fake_client.sign_in.assert_awaited_once_with(password="my-2fa-password")
        assert controller._telegram_auth is None
        assert refresh_calls == [1]

    def test_wrong_password_sets_error_and_stays_on_password_step(self, controller, monkeypatch):
        monkeypatch.setattr(sc, "telegram_app_credentials", lambda: (123, "hash"))
        controller._telegram_auth = {"step": "password", "error": ""}
        recorded = []
        monkeypatch.setattr(sc, "AppHelper", SimpleNamespace(callAfter=lambda f, *a, **k: recorded.append((f, a, k))))

        fake_client = MagicMock()
        fake_client.connect = AsyncMock()
        fake_client.disconnect = AsyncMock()
        fake_client.sign_in = AsyncMock(side_effect=RuntimeError("wrong password"))
        monkeypatch.setattr("telethon.TelegramClient", lambda *a, **kw: fake_client)

        controller.telegram_submit_2fa("nope")

        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)

        assert controller._telegram_auth["step"] == "password"
        assert "wrong password" in controller._telegram_auth["error"]


class TestTelegramCancelAuth:
    def test_resets_to_no_active_flow(self, controller):
        controller._telegram_auth = {"step": "code", "error": ""}

        controller.telegram_cancel_auth()

        assert controller._telegram_auth is None

    def test_no_op_when_nothing_in_progress(self, controller):
        controller.telegram_cancel_auth()  # must not raise
        assert controller._telegram_auth is None


class TestTelegramAuthSnapshotState:
    def test_default_state_is_no_step_no_error(self, controller):
        state = controller.snapshot()
        assert state["telegram_auth"] == {"step": None, "error": ""}

    def test_reflects_in_progress_flow(self, controller):
        controller._telegram_auth = {"step": "code", "phone": "+1", "phone_code_hash": "h", "error": "oops"}

        state = controller.snapshot()

        assert state["telegram_auth"] == {"step": "code", "error": "oops"}



class TestRuleRows:
    def _seed(self, controller, op_key, rules):
        cfg = controller._load_config()
        cfg.setdefault("auto_accept_rules", {})[op_key] = rules
        controller._save_config(cfg)

    def test_add_rule_row_appends_an_empty_row(self, controller):
        controller.add_rule_row("gmail.read_message")

        rules = controller._load_config()["auto_accept_rules"]["gmail.read_message"]
        assert rules == [{"rule": ""}]

    def test_update_rule_type_field(self, controller):
        self._seed(controller, "gmail.read_message", [{"rule": ""}])

        controller.update_rule_row("gmail.read_message", 0, "rule_type", "i_am_sender")

        rules = controller._load_config()["auto_accept_rules"]["gmail.read_message"]
        assert rules[0]["rule"] == "i_am_sender"

    def test_update_value_field_for_list_rule_splits_on_comma(self, controller):
        self._seed(controller, "gmail.read_message", [{"rule": "trusted_sender_domain"}])

        controller.update_rule_row("gmail.read_message", 0, "value", "a.com, b.com")

        rules = controller._load_config()["auto_accept_rules"]["gmail.read_message"]
        assert rules[0]["value"] == ["a.com", "b.com"]

    def test_update_value_field_for_int_rule_parses_integer(self, controller):
        self._seed(controller, "gmail.read_message", [{"rule": "age_threshold_days"}])

        controller.update_rule_row("gmail.read_message", 0, "value", "30")

        rules = controller._load_config()["auto_accept_rules"]["gmail.read_message"]
        assert rules[0]["value"] == 30

    def test_clearing_value_field_removes_the_value_key(self, controller):
        self._seed(controller, "gmail.read_message", [{"rule": "age_threshold_days", "value": 30}])

        controller.update_rule_row("gmail.read_message", 0, "value", "")

        rules = controller._load_config()["auto_accept_rules"]["gmail.read_message"]
        assert "value" not in rules[0]

    def test_update_out_of_range_index_is_a_no_op(self, controller):
        self._seed(controller, "gmail.read_message", [{"rule": "i_am_sender"}])
        before = controller._load_config()

        controller.update_rule_row("gmail.read_message", 9, "rule_type", "x")

        assert controller._load_config() == before

    def test_remove_rule_row(self, controller):
        self._seed(controller, "gmail.read_message", [{"rule": "i_am_sender"}, {"rule": "trusted_sender_domain"}])

        controller.remove_rule_row("gmail.read_message", 0)

        rules = controller._load_config()["auto_accept_rules"]["gmail.read_message"]
        assert rules == [{"rule": "trusted_sender_domain"}]

    def test_removing_last_rule_drops_the_operation_key(self, controller):
        self._seed(controller, "gmail.read_message", [{"rule": "i_am_sender"}])

        controller.remove_rule_row("gmail.read_message", 0)

        assert "gmail.read_message" not in controller._load_config().get("auto_accept_rules", {})

    def test_remove_out_of_range_index_is_a_no_op(self, controller):
        self._seed(controller, "gmail.read_message", [{"rule": "i_am_sender"}])
        before = controller._load_config()

        controller.remove_rule_row("gmail.read_message", 9)

        assert controller._load_config() == before

    def test_grant_compiled_entries_are_excluded_from_rows(self, controller):
        self._seed(controller, "drive.read_file_contents", [{"rule": "approved_folder", "_grant": True}])

        state = controller._rules_state(controller._load_config())
        read_file = next(s for s in state["sections_by_connector"]["drive"] if s["op_key"] == "drive.read_file_contents")
        assert read_file["rows"] == []


class TestGrantRows:
    def test_add_grant_row_appends_an_empty_entry(self, controller):
        controller.add_grant_row("drive", "sandbox_folders")

        entries = controller._load_config()["auto_accept_grants"]["drive"]["sandbox_folders"]
        assert entries == [{"id": ""}]

    def test_update_id_field_extracts_from_pasted_url(self, controller):
        controller.add_grant_row("drive", "sandbox_folders")

        controller.update_grant_row(
            "drive", "sandbox_folders", 0, "id",
            "https://drive.google.com/drive/folders/FOLDER9",
        )

        entries = controller._load_config()["auto_accept_grants"]["drive"]["sandbox_folders"]
        assert entries[0]["id"] == "FOLDER9"

    def test_update_name_field(self, controller):
        controller.add_grant_row("drive", "sandbox_folders")

        controller.update_grant_row("drive", "sandbox_folders", 0, "name", "Scratch")

        entries = controller._load_config()["auto_accept_grants"]["drive"]["sandbox_folders"]
        assert entries[0]["name"] == "Scratch"

    def test_duplicate_id_is_rejected(self, controller):
        controller._save_config({"auto_accept_grants": {"drive": {"sandbox_folders": [{"id": "F1"}]}}})
        controller.add_grant_row("drive", "sandbox_folders")

        controller.update_grant_row("drive", "sandbox_folders", 1, "id", "F1")

        entries = controller._load_config()["auto_accept_grants"]["drive"]["sandbox_folders"]
        assert entries[1]["id"] == ""
        assert controller.error

    def test_toggle_capability_on_and_off(self, controller):
        controller._save_config({"auto_accept_grants": {"drive": {"sandbox_folders": [{"id": "F1", "write": False}]}}})

        controller.toggle_grant_capability("drive", "sandbox_folders", 0, "write")
        assert controller._load_config()["auto_accept_grants"]["drive"]["sandbox_folders"][0]["write"] is True

        controller.toggle_grant_capability("drive", "sandbox_folders", 0, "write")
        assert controller._load_config()["auto_accept_grants"]["drive"]["sandbox_folders"][0]["write"] is False

    def test_toggle_capability_unknown_resource_type_is_a_no_op(self, controller):
        before = controller._load_config()

        controller.toggle_grant_capability("nope", "nope", 0, "write")

        assert controller._load_config() == before

    def test_remove_grant_row(self, controller):
        controller._save_config({"auto_accept_grants": {"drive": {"sandbox_folders": [{"id": "F1"}]}}})

        controller.remove_grant_row("drive", "sandbox_folders", 0)

        assert not controller._load_config().get("auto_accept_grants", {}).get("drive", {}).get("sandbox_folders")

    def test_remove_out_of_range_index_is_a_no_op(self, controller):
        controller._save_config({"auto_accept_grants": {"drive": {"sandbox_folders": [{"id": "F1"}]}}})
        before = controller._load_config()

        controller.remove_grant_row("drive", "sandbox_folders", 9)

        assert controller._load_config() == before

    def test_resolve_names_async_kicks_off_when_client_available(self, controller, monkeypatch):
        recorded = []
        monkeypatch.setattr(sc, "AppHelper", SimpleNamespace(callAfter=lambda f, *a, **k: recorded.append((f, a, k))))
        client = SimpleNamespace(get_file_metadata=lambda file_id: SimpleNamespace(name="Scratch"))
        controller._connector_objs = {"drive": SimpleNamespace(client=client)}
        controller.add_grant_row("drive", "sandbox_folders")

        controller.update_grant_row("drive", "sandbox_folders", 0, "id", "F1")

        assert wait_until(lambda: recorded)
        _drain_run_async(recorded)

        assert controller._resolver.cached_name(sc.grant_resource_type("drive", "sandbox_folders"), "F1") == "Scratch"


class TestDriveGrantSummary:
    """Sheets and Docs aren't real connectors (see RULES_MENU_GROUPS' own
    comment in settings_controller.py) but silently ride Drive's Trusted/
    Sandbox Folder grants -- this read-only summary is how the Rules page
    surfaces that instead of leaving Sheets/Docs looking ungoverned."""

    def test_absent_for_non_sheets_docs_connectors(self, controller):
        state = controller._rules_state(controller._load_config())
        summary = state["drive_grant_summary_by_connector"]
        assert summary["drive"] is None
        assert summary["gmail"] is None

    def test_present_for_sheets_and_docs(self, controller):
        state = controller._rules_state(controller._load_config())
        summary = state["drive_grant_summary_by_connector"]
        for cname in ("sheets", "docs"):
            assert summary[cname]["title"] == "Governed by Drive"
            labels = [row["label"] for row in summary[cname]["rows"]]
            assert labels == ["Trusted Folders — read auto-accept", "Sandbox Folders — write auto-accept"]

    def test_shows_none_configured_when_no_grants(self, controller):
        state = controller._rules_state(controller._load_config())
        rows = state["drive_grant_summary_by_connector"]["sheets"]["rows"]
        assert [row["value"] for row in rows] == ["(none configured)", "(none configured)"]

    def test_shows_granted_folder_name_falling_back_to_a_short_id(self, controller):
        controller._save_config({"auto_accept_grants": {"drive": {
            "folders": [{"id": "FOLDER_READ_1", "read": True}],
            "sandbox_folders": [{"id": "FOLDER_WRITE_1", "name": "Scratch", "write": True}],
        }}})

        state = controller._rules_state(controller._load_config())
        rows = state["drive_grant_summary_by_connector"]["docs"]["rows"]
        assert sc._short_id("FOLDER_READ_1") in rows[0]["value"]
        assert "Scratch" in rows[1]["value"]

    def test_sheets_and_docs_share_the_same_summary_data(self, controller):
        controller._save_config({"auto_accept_grants": {"drive": {"folders": [{"id": "F1", "read": True}]}}})

        state = controller._rules_state(controller._load_config())
        summary = state["drive_grant_summary_by_connector"]
        assert summary["sheets"]["rows"] == summary["docs"]["rows"]


class TestPrivacyFilter:
    def test_set_default_policy(self, controller):
        controller.set_default_policy("privacy", "block")

        assert controller._load_config()["privacy"]["default_policy"] == "block"
        from privacyfence import privacy_filter
        assert privacy_filter.category_policy("privacy", "body") == "block"

    def test_set_default_policy_invalid_value_is_a_no_op(self, controller):
        before = controller._load_config()

        controller.set_default_policy("privacy", "delete_everything")

        assert controller._load_config() == before

    def test_set_category_policy(self, controller):
        controller.set_category_policy("slack_privacy", "message_content", "block")

        cfg = controller._load_config()
        assert cfg["slack_privacy"]["categories"]["message_content"] == "block"

    def test_toggle_calendar_free_busy(self, controller, monkeypatch):
        monkeypatch.setattr(controller, "refresh_connectors", lambda: controller.snapshot())

        controller.toggle_calendar_free_busy()

        assert controller._load_config()["calendar"]["free_busy_full_event_details"] is False

    def test_toggle_calendar_free_busy_refreshes_connectors(self, controller, monkeypatch):
        calls = []
        monkeypatch.setattr(controller, "refresh_connectors", lambda: calls.append(1) or controller.snapshot())

        controller.toggle_calendar_free_busy()

        assert calls == [1]


class TestAuditLog:
    def test_set_log_level_persists_and_hot_applies(self, controller, monkeypatch):
        applied = []
        monkeypatch.setattr(daemon_main, "setup_logging", lambda cfg: applied.append(cfg.get("logging")))

        controller.set_log_level("DEBUG")

        assert controller._load_config()["logging"]["level"] == "DEBUG"
        assert applied == [{"level": "DEBUG"}]

    def test_set_log_level_rejects_unknown_levels(self, controller):
        before = controller._load_config()

        controller.set_log_level("NOT_A_LEVEL")

        assert controller._load_config() == before

    def test_export_audit_log_missing_dir_sets_error(self, controller):
        controller.export_audit_log()
        assert controller.error

    def test_export_audit_log_opens_existing_dir(self, controller, monkeypatch):
        log_dir = sc.data_dir() / "logs" / "audit"
        log_dir.mkdir(parents=True)
        run_calls = []
        monkeypatch.setattr(sc.subprocess, "run", lambda *a, **k: run_calls.append(a))

        controller.export_audit_log()

        assert run_calls == [(["open", str(log_dir)],)]
        assert controller.error == ""

    def test_export_audit_log_exports_and_opens_the_current_week(self, controller, monkeypatch):
        from privacyfence.audit_log import AuditEntry, AuditLogger, current_week

        log_dir = sc.data_dir() / "logs" / "audit"
        log_dir.mkdir(parents=True)
        week = current_week()
        entry = AuditEntry(
            timestamp="2026-07-06T12:00:00+00:00", week=week, request_id="",
            connector="gmail", tool="gmail_get_message", tool_name="Read Gmail message",
            summary="s", sender="a@x.com", decision="approved", auto_accept_rule="", latency_seconds=1.0,
        )
        AuditLogger(str(log_dir)).record(entry)
        run_calls = []
        monkeypatch.setattr(sc.subprocess, "run", lambda *a, **k: run_calls.append(a))

        controller.export_audit_log()

        expected_xlsx = log_dir / f"{week}.xlsx"
        assert expected_xlsx.exists()
        assert run_calls == [(["open", str(expected_xlsx)],)]

    def test_snapshot_recent_entries_reflect_the_audit_log(self, controller):
        from privacyfence.audit_log import AuditEntry, AuditLogger, current_week

        log_dir = sc.data_dir() / "logs" / "audit"
        log_dir.mkdir(parents=True)
        entry = AuditEntry(
            timestamp="2026-07-06T12:00:00+00:00", week=current_week(), request_id="",
            connector="gmail", tool="gmail_get_message", tool_name="Read Gmail message",
            summary="s", sender="a@x.com", decision="auto_accepted", auto_accept_rule="i_am_sender",
            latency_seconds=1.0,
        )
        AuditLogger(str(log_dir)).record(entry)

        state = controller.snapshot()

        assert len(state["audit"]["recent"]) == 1
        assert state["audit"]["recent"][0]["tool"] == "Read Gmail message"
        assert state["audit"]["recent"][0]["decision"] == "auto_accepted"


class TestAbout:
    def test_quit_app_calls_rumps_quit(self, controller, monkeypatch):
        calls = []
        monkeypatch.setattr(sc, "rumps", SimpleNamespace(quit_application=lambda: calls.append(1)))

        controller.quit_app()

        assert calls == [1]


class TestSnapshotStructure:
    def test_snapshot_has_one_key_per_page(self, controller):
        state = controller.snapshot()
        assert set(state) == {
            "error", "general", "connectors", "telegram_auth", "rules", "privacy", "audit", "about",
        }

    def test_connectors_cover_all_connectors(self, controller):
        state = controller.snapshot()
        assert {c["key"] for c in state["connectors"]} == set(sc.ALL_CONNECTORS)

    def test_rules_connectors_cover_rules_menu_groups(self, controller):
        state = controller.snapshot()
        assert {c["key"] for c in state["rules"]["connectors"]} == set(sc.RULES_MENU_GROUPS)

    def test_privacy_groups_include_calendar_and_the_six_category_groups(self, controller):
        state = controller.snapshot()
        keys = {g["key"] for g in state["privacy"]["groups"]}
        assert keys == set(sc.PRIVACY_GROUP_LABELS) | {"calendar"}

    def test_sheets_and_docs_are_distinct_from_drive(self, controller):
        # Regression: same bug class as the pre-#120 menu -- "sheets"/"docs"
        # ride on Drive's OAuth grant but have their own operation-key
        # namespace and must not be silently dropped or merged into drive's
        # bucket.
        state = controller.snapshot()
        sheets_titles = {s["title"] for s in state["rules"]["sections_by_connector"]["sheets"]}
        docs_titles = {s["title"] for s in state["rules"]["sections_by_connector"]["docs"]}
        drive_titles = {s["title"] for s in state["rules"]["sections_by_connector"]["drive"]}
        assert sheets_titles and docs_titles
        assert not (drive_titles & sheets_titles)
        assert not (drive_titles & docs_titles)

    def test_tasks_gets_a_grant_section_and_its_write_operations(self, controller):
        state = controller.snapshot()
        grant_titles = {g["title"] for g in state["rules"]["grants_by_connector"]["tasks"]}
        rule_titles = {s["title"] for s in state["rules"]["sections_by_connector"]["tasks"]}
        assert "Trusted Task Lists" in grant_titles
        assert rule_titles == {"Create task", "Update task", "Complete task", "Uncomplete task", "Move task"}


class TestRuleUiCompleteness:
    """Structural checks tying the settings window's rule UI to auto_accept's
    rule engine -- see test_menu_bar.py's pre-#120 version of this class for
    the original regressions these caught (calendar.set_visibility/
    non_private_event never reachable from the UI, "docs" missing from
    RULES_MENU_GROUPS)."""

    @staticmethod
    def _all_rule_names() -> set[str]:
        return {
            name[len("_rule_"):]
            for name in vars(auto_accept.AutoAcceptEvaluator)
            if name.startswith("_rule_") and callable(getattr(auto_accept.AutoAcceptEvaluator, name))
        }

    @staticmethod
    def _rules_by_operation_names() -> set[str]:
        return {rule for rules in sc.RULES_BY_OPERATION.values() for rule in rules}

    def test_every_rule_is_reachable_from_some_operation(self):
        unreachable = self._all_rule_names() - self._rules_by_operation_names()
        assert unreachable == set()

    def test_no_stale_rule_names_in_rules_by_operation(self):
        stale = self._rules_by_operation_names() - self._all_rule_names()
        assert stale == set()

    def test_every_operation_label_is_a_real_operation_key(self):
        real_ops = set(auto_accept.TOOL_TO_OPERATION.values())
        fake = set(sc.OPERATION_LABELS) - real_ops
        assert fake == set()

    def test_every_rules_by_operation_key_has_a_label(self):
        unlabeled = set(sc.RULES_BY_OPERATION) - set(sc.OPERATION_LABELS)
        assert unlabeled == set()

    def test_every_operation_labels_connector_prefix_is_in_rules_menu_groups(self):
        prefixes = {op_key.split(".", 1)[0] for op_key in sc.OPERATION_LABELS}
        missing = prefixes - set(sc.RULES_MENU_GROUPS)
        assert missing == set()
