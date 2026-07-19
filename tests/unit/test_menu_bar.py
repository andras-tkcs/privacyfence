"""Race-condition tests for privacyfence.menu_bar.

rumps/AppKit is not thread-safe: every long-running flow (OAuth, the IPC
server's own thread) does its work off the main thread and is required to
hand the result back via ``PyObjCTools.AppHelper.callAfter`` before touching
the menu. These tests target two shipped bugs where that contract was
violated and the menu bar went stale until the app was restarted:

  * "Always Allow" rules added from the approval popup (which runs on the
    IPC server's thread) never appeared in the menu until restart, because
    only in-process menu edits triggered a rebuild.
  * "Authenticate…" for a connector completed successfully but the connector
    still showed as not connected until restart, because the result crossed
    threads without being marshaled onto the main thread first.

Rather than asserting on the final menu bitmap (fragile, and rumps has no
headless render target worth inspecting), these tests intercept
``AppHelper.callAfter`` itself: they prove that (a) the state-mutating
callback is *never* invoked directly on the background thread that produced
the result, and (b) once the callback is actually drained (simulating the
main run loop pumping it), the state update that the user was missing
without a restart does in fact happen.
"""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from privacyfence import auto_accept, menu_bar


def wait_until(predicate, timeout=2.0, interval=0.005) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(menu_bar, "_find_icon", lambda: None)
    monkeypatch.setattr(menu_bar, "load_org_config", lambda: {})
    monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: None)

    config_path = tmp_path / "settings.yaml"
    config_path.write_text("auto_accept_rules: {}\nconnectors: {}\n", encoding="utf-8")

    ipc_calls = []
    ipc_server = SimpleNamespace(
        set_connectors=lambda conns: ipc_calls.append(conns),
        unattended_session_count=lambda: 0,
        set_unattended_changed_listener=lambda callback: None,
    )

    instance = menu_bar.PrivacyFenceMenuBar(str(config_path), connectors=[], ipc_server=ipc_server)
    instance._ipc_calls = ipc_calls  # test-only hook
    return instance


class TestRunAsyncMarshaling:
    """_run_async is the mechanism every threaded flow in this module funnels
    through. If it ever regresses to invoking on_done directly on the worker
    thread, every one of the bugs described in the module docstring comes
    back at once.
    """

    def test_success_result_never_delivered_directly_on_worker_thread(self, app, monkeypatch):
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append((f, a, k)))

        done_calls = []
        work_thread = {}

        def work():
            work_thread["thread"] = threading.current_thread()
            return "alice@example.com"

        def done(ok, result):
            done_calls.append((ok, result))

        app._run_async(work, done)

        assert wait_until(lambda: recorded), "work() never handed its result to AppHelper.callAfter"
        # work() really did run off the calling thread...
        assert work_thread["thread"] is not threading.current_thread()
        # ...and `done` must not have been invoked yet: it may only run once
        # something actually pumps the main run loop's queued callback.
        assert done_calls == []

        func, args, kwargs = recorded[0]
        assert args == (True, "alice@example.com")

        func(*args, **kwargs)
        assert done_calls == [(True, "alice@example.com")]

    def test_exception_in_work_is_also_marshaled_not_raised_on_worker_thread(self, app, monkeypatch):
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append((f, a, k)))

        boom = ValueError("auth failed")

        def work():
            raise boom

        done_calls = []

        def done(ok, result):
            done_calls.append((ok, result))

        app._run_async(work, done)

        assert wait_until(lambda: recorded)
        assert done_calls == []
        func, args, kwargs = recorded[0]
        assert args == (False, boom)
        func(*args, **kwargs)
        assert done_calls == [(False, boom)]


class TestAuthenticateDoesNotRequireRestart:
    """Regression for: 'authenticate a service was not displayed in the
    menubar until restart'. Simulates a successful Google OAuth flow end to
    end through the two chained background hops (_authenticate_google's
    `done` calls `_refresh_connectors`, which itself runs a second
    background hop) and asserts the menu-visible state only ever changes
    once the main thread actually drains each callback -- and that it does
    change, without any extra manual rebuild.
    """

    def test_successful_auth_updates_connectors_only_after_callafter_drains(self, app, monkeypatch):
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append((f, a, k)))

        class FakeGmailClient:
            def __init__(self, client_config, token_file):
                pass

            def authorize_interactive(self):
                pass

            def check_connection(self):
                return "alice@example.com"

        monkeypatch.setitem(menu_bar._GOOGLE_CLIENTS, "gmail", FakeGmailClient)
        monkeypatch.setattr(
            menu_bar, "build_connectors", lambda cfg, org: [SimpleNamespace(name="gmail")]
        )

        org_config = {"google": {"client_id": "id", "client_secret": "secret"}}
        app._authenticate_google("gmail", org_config)

        # Hop 1: the OAuth flow finished on a background thread. Its result
        # must be sitting in the callAfter queue, not yet applied.
        assert wait_until(lambda: len(recorded) == 1)
        assert app._connectors == []  # still stale -- this is the "not shown" symptom pre-drain
        assert app._ipc_calls == []

        hop1_func, hop1_args, hop1_kwargs = recorded.pop(0)
        hop1_func(*hop1_args, **hop1_kwargs)  # simulate the main run loop draining it

        # That drained callback (`done`) calls _refresh_connectors(), which
        # kicks off a *second* background hop rather than mutating state
        # inline -- so the connectors list is still stale until this second
        # hop is also drained.
        assert wait_until(lambda: len(recorded) == 1)
        assert app._connectors == []

        hop2_func, hop2_args, hop2_kwargs = recorded.pop(0)
        hop2_func(*hop2_args, **hop2_kwargs)

        # Only now -- after both hops drained, exactly as a pumped run loop
        # would do automatically -- does the menu bar's connected state
        # reflect the finished authentication. No restart, no extra
        # explicit rebuild call from the test was needed.
        assert app._connectors == ["gmail"]
        assert app._ipc_calls == [[SimpleNamespace(name="gmail")]]

    def test_failed_auth_rebuilds_without_marking_connected(self, app, monkeypatch):
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append((f, a, k)))

        class FailingGmailClient:
            def __init__(self, client_config, token_file):
                pass

            def authorize_interactive(self):
                raise RuntimeError("user cancelled")

            def check_connection(self):
                raise AssertionError("should not be reached")

        monkeypatch.setitem(menu_bar._GOOGLE_CLIENTS, "gmail", FailingGmailClient)
        rebuild_calls = []
        monkeypatch.setattr(app, "_rebuild", lambda: rebuild_calls.append(1))

        app._authenticate_google("gmail", {"google": {"client_id": "id", "client_secret": "s"}})

        assert wait_until(lambda: len(recorded) == 1)
        assert rebuild_calls == []
        func, args, kwargs = recorded[0]
        assert args[0] is False
        func(*args, **kwargs)
        assert rebuild_calls == [1]
        assert app._connectors == []  # never marked connected


class TestRulesChangedMarshaling:
    """Regression for: 'always allow did not add the new rule to the menu
    ... until restart'. auto_accept.reload_rules() is called from the IPC
    server's own thread when a rule is created via Always allow. The listener
    it fires must schedule the rebuild through AppHelper.callAfter -- if a
    regression calls self._rebuild() directly instead, this test catches it
    because the rebuild would show up on the background thread before any
    drain happens.
    """

    def test_reload_from_background_thread_schedules_rebuild_but_does_not_run_it_inline(self, app, monkeypatch):
        recorded = []
        monkeypatch.setattr(
            menu_bar.AppHelper,
            "callAfter",
            lambda f, *a, **k: recorded.append((f, a, k, threading.current_thread())),
        )
        rebuild_calls = []
        monkeypatch.setattr(app, "_rebuild", lambda: rebuild_calls.append(threading.current_thread()))

        bg_done = threading.Event()

        def ipc_server_thread_body():
            # Mirrors what happens inside gate.py's add_auto_accept_rule ->
            # reload_rules() after an Always allow confirmation, called from
            # whatever thread is running the IPC server's request handling.
            auto_accept.reload_rules({"gmail.read_message": [{"rule": "i_am_sender"}]})
            bg_done.set()

        t = threading.Thread(target=ipc_server_thread_body)
        t.start()
        t.join(timeout=2)
        assert bg_done.is_set()

        # The rule reload completed, but the menu must not have rebuilt yet
        # on that thread -- only *scheduled* to, via callAfter.
        assert rebuild_calls == []
        assert len(recorded) == 1
        func, args, kwargs, calling_thread = recorded[0]
        assert calling_thread is not threading.current_thread()

        func(*args, **kwargs)
        assert rebuild_calls == [threading.current_thread()]


class TestConcurrentAuthAndRuleChangeDoNotLoseUpdates:
    """Two independent background flows finishing close together (an
    Authenticate… completing and a rule being added via Always allow) each
    queue their own callAfter callback. Draining them in either order must
    not let one clobber the other's state -- the two update disjoint pieces
    of state ( _connectors / ipc_server vs the rules submenu), but they share
    the same _rebuild() plumbing, so a naive implementation could lose one
    rebuild if it coalesced callbacks incorrectly.
    """

    def test_both_updates_are_visible_regardless_of_drain_order(self, app, monkeypatch):
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append((f, a, k)))
        monkeypatch.setattr(menu_bar, "build_connectors", lambda cfg, org: [SimpleNamespace(name="slack")])

        class FakeSlackAuth:
            def __call__(self, **kwargs):
                return {"team_name": "Acme"}

        monkeypatch.setattr(menu_bar, "slack_authorize_interactive", FakeSlackAuth())

        # Flow 1: successful Slack auth.
        app._authenticate_slack({"slack": {"client_id": "id", "client_secret": "s"}})
        assert wait_until(lambda: len(recorded) == 1)

        # Flow 2: a rule gets added from another thread while flow 1's
        # result is still sitting undrained in the queue.
        bg_done = threading.Event()

        def add_rule_from_bg_thread():
            auto_accept.reload_rules({"gmail.read_message": [{"rule": "i_am_sender"}]})
            bg_done.set()

        threading.Thread(target=add_rule_from_bg_thread).start()
        assert wait_until(lambda: bg_done.is_set())
        assert wait_until(lambda: len(recorded) == 2)

        # Drain in arrival order: auth's `done` first (spawns a second hop
        # via _refresh_connectors), then the rules-changed rebuild.
        auth_func, auth_args, auth_kwargs = recorded.pop(0)
        auth_func(*auth_args, **auth_kwargs)
        assert wait_until(lambda: len(recorded) == 2)  # refresh_connectors' own hop queued

        rules_func, rules_args, rules_kwargs = recorded.pop(0)
        rules_func(*rules_args, **rules_kwargs)

        refresh_func, refresh_args, refresh_kwargs = recorded.pop(0)
        refresh_func(*refresh_args, **refresh_kwargs)

        assert app._connectors == ["slack"]
        assert app._ipc_calls == [[SimpleNamespace(name="slack")]]


# ============================================================================ #
# P6: the rest of menu_bar.py -- rule CRUD, connector lifecycle, menu
# building, and per-service authenticate flows. Interactive AppKit dialogs
# (rumps.alert / rumps.Window / _osascript_pick) are mocked at the module
# boundary rather than driven for real, matching every gate.py/approval_*
# test's approach of not popping up real modal dialogs in a test run.
# ============================================================================ #

class TestFormatPairLine:
    def test_with_tab(self):
        assert menu_bar._format_pair_line({"spreadsheet_id": "sheet1", "tab": "Sheet1"}) == "sheet1:Sheet1"

    def test_without_tab(self):
        assert menu_bar._format_pair_line({"spreadsheet_id": "sheet1"}) == "sheet1"

    def test_non_dict_falls_back_to_str(self):
        assert menu_bar._format_pair_line("sheet1") == "sheet1"


class TestParsePairLines:
    def test_parses_id_only_lines(self):
        assert menu_bar._parse_pair_lines("sheet1\nsheet2") == [
            {"spreadsheet_id": "sheet1"}, {"spreadsheet_id": "sheet2"},
        ]

    def test_parses_id_colon_tab_lines(self):
        assert menu_bar._parse_pair_lines("sheet1:Sheet1\nsheet2:My Tab") == [
            {"spreadsheet_id": "sheet1", "tab": "Sheet1"},
            {"spreadsheet_id": "sheet2", "tab": "My Tab"},
        ]

    def test_strips_whitespace_around_id_and_tab(self):
        assert menu_bar._parse_pair_lines("  sheet1 : Sheet1  ") == [{"spreadsheet_id": "sheet1", "tab": "Sheet1"}]

    def test_blank_lines_are_skipped(self):
        assert menu_bar._parse_pair_lines("sheet1\n\n\nsheet2") == [
            {"spreadsheet_id": "sheet1"}, {"spreadsheet_id": "sheet2"},
        ]

    def test_trailing_colon_with_empty_tab_omits_tab_key(self):
        assert menu_bar._parse_pair_lines("sheet1:") == [{"spreadsheet_id": "sheet1"}]

    def test_empty_text_yields_empty_list(self):
        assert menu_bar._parse_pair_lines("") == []


class TestBind:
    def test_forwards_bound_args_and_sender(self):
        calls = []
        def fn(*args):
            calls.append(args)
        cb = menu_bar._bind(fn, "op_key", 3)
        cb("the-sender")
        assert calls == [("op_key", 3, "the-sender")]


class TestFindIcon:
    def test_returns_first_existing_candidate(self, monkeypatch, tmp_path):
        # _find_icon() derives its search dir from __file__'s parent /
        # "resources", so relocate __file__ to make tmp_path that parent.
        resources = tmp_path / "resources"
        resources.mkdir()
        (resources / "icon_32.png").write_bytes(b"")
        (resources / "icon_64.png").write_bytes(b"")
        monkeypatch.setattr(menu_bar, "__file__", str(tmp_path / "fake_menu_bar.py"))

        assert menu_bar._find_icon() == str(resources / "icon_32.png")

    def test_prefers_menubar_icon_over_others_when_both_exist(self, monkeypatch, tmp_path):
        resources = tmp_path / "resources"
        resources.mkdir()
        (resources / "icon_32.png").write_bytes(b"")
        (resources / "icon_menubar.png").write_bytes(b"")
        monkeypatch.setattr(menu_bar, "__file__", str(tmp_path / "fake_menu_bar.py"))

        assert menu_bar._find_icon() == str(resources / "icon_menubar.png")

    def test_returns_none_when_nothing_found(self, monkeypatch, tmp_path):
        monkeypatch.setattr(menu_bar, "__file__", str(tmp_path / "fake_menu_bar.py"))
        assert menu_bar._find_icon() is None


class TestGoogleClientConfig:
    def test_empty_without_both_fields(self):
        assert menu_bar._google_client_config({}) == {}
        assert menu_bar._google_client_config({"google": {"client_id": "i"}}) == {}

    def test_wraps_when_both_present(self):
        cfg = {"google": {"client_id": "i", "client_secret": "s"}}
        assert menu_bar._google_client_config(cfg) == {"installed": {"client_id": "i", "client_secret": "s"}}


class TestOsascriptPick:
    def test_builds_script_with_title_prompt_and_options(self, monkeypatch):
        captured = {}
        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return SimpleNamespace(stdout="chosen-value\n")
        monkeypatch.setattr(menu_bar.subprocess, "run", fake_run)

        result = menu_bar._osascript_pick("My Title", "Pick one:", ["a", "b"])

        assert result == "chosen-value"
        script = captured["cmd"][2]
        assert "My Title" in script
        assert "Pick one:" in script
        assert '"a"' in script and '"b"' in script

    def test_empty_output_returns_none(self, monkeypatch):
        monkeypatch.setattr(menu_bar.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=""))
        assert menu_bar._osascript_pick("T", "P", ["a"]) is None


class TestConfigHelpers:
    def test_load_config_round_trips_yaml(self, app, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text("connectors:\n  gmail:\n    enabled: false\n", encoding="utf-8")
        app._config_path = str(config_path)

        assert app._load_config() == {"connectors": {"gmail": {"enabled": False}}}

    def test_load_config_missing_file_returns_empty_dict(self, app, tmp_path):
        app._config_path = str(tmp_path / "does-not-exist.yaml")
        assert app._load_config() == {}

    def test_load_config_malformed_yaml_returns_empty_dict_not_raise(self, app, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(":\n  - not: [valid yaml", encoding="utf-8")
        app._config_path = str(config_path)

        assert app._load_config() == {}

    def test_save_config_writes_yaml_readable_back(self, app, tmp_path):
        config_path = tmp_path / "settings.yaml"
        app._config_path = str(config_path)

        app._save_config({"connectors": {"slack": {"enabled": True}}})

        assert app._load_config() == {"connectors": {"slack": {"enabled": True}}}

    def test_save_and_reload_persists_and_triggers_rule_reload(self, app, tmp_path, monkeypatch):
        config_path = tmp_path / "settings.yaml"
        app._config_path = str(config_path)
        reload_calls = []
        monkeypatch.setattr(menu_bar, "reload_rules", lambda rules: reload_calls.append(rules))

        app._save_and_reload({"auto_accept_rules": {"gmail.read_message": [{"rule": "i_am_sender"}]}})

        assert reload_calls == [{"gmail.read_message": [{"rule": "i_am_sender"}]}]
        assert app._load_config()["auto_accept_rules"] == {"gmail.read_message": [{"rule": "i_am_sender"}]}

    def test_save_and_reload_falls_back_to_rebuild_when_reload_rules_raises(self, app, tmp_path, monkeypatch):
        config_path = tmp_path / "settings.yaml"
        app._config_path = str(config_path)
        monkeypatch.setattr(menu_bar, "reload_rules", lambda rules: (_ for _ in ()).throw(RuntimeError("boom")))
        rebuild_calls = []
        monkeypatch.setattr(app, "_rebuild", lambda: rebuild_calls.append(1))

        app._save_and_reload({})  # must not raise

        assert rebuild_calls == [1]


class TestBuildRulesMenu:
    def test_every_connector_gets_a_top_level_entry(self, app):
        rules_parent = app._build_rules_menu({})
        titles = {item.title for item in rules_parent.values()}
        for cname in menu_bar.ALL_CONNECTORS:
            assert cname.capitalize() in titles

    def test_sheets_gets_its_own_top_level_entry_distinct_from_drive(self, app):
        # Regression: sheets.* operation keys were bucketed under a "sheets"
        # group by _build_rules_menu's own connector-prefix grouping, but
        # "sheets" was never iterated (ALL_CONNECTORS has no such entry --
        # it's not a real connector), so the whole bucket was silently
        # dropped and no Sheets rules ever appeared in the menu at all.
        rules_parent = app._build_rules_menu({})
        titles = {item.title for item in rules_parent.values()}
        assert "Sheets" in titles

        # Sheets has no grantable resource type of its own (drive.folders/
        # sandbox_folders/spreadsheets grants live under "Drive" -- see
        # resource_grants.py), so its top-level entry is Filters-only.
        sheets_item = rules_parent["Sheets"]
        assert {i.title for i in sheets_item.values()} == {"Filters"}
        sub_titles = {i.title for i in sheets_item["Filters"].values()}
        assert sub_titles == {
            "Read values", "Write range", "Add tab", "Rename tab", "Format range",
        }

        drive_item = rules_parent["Drive"]
        drive_filter_titles = {i.title for i in drive_item["Filters"].values()}
        assert not (drive_filter_titles & sub_titles)  # Sheets ops aren't duplicated under Drive

    def test_connector_with_no_configurable_ops_shows_placeholder(self, app, monkeypatch):
        # Every real connector has at least one entry in OPERATION_LABELS
        # now, so this exercises the placeholder branch with a synthetic
        # connector that deliberately has none.
        monkeypatch.setattr(menu_bar, "RULES_MENU_GROUPS", menu_bar.RULES_MENU_GROUPS + ["nullconnector"])
        rules_parent = app._build_rules_menu({})
        placeholder_item = rules_parent["Nullconnector"]
        sub_titles = [i.title for i in placeholder_item.values()]
        assert any("always auto-approved" in t for t in sub_titles)

    def test_tasks_shows_its_write_operations(self, app):
        rules_parent = app._build_rules_menu({})
        tasks_item = rules_parent["Tasks"]
        # Tasks also gets a "Trusted Task Lists" grant submenu now, alongside
        # its Filters submenu -- see resource_grants.py's tasks.task_lists.
        assert {i.title for i in tasks_item.values()} == {"Trusted Task Lists", "Filters"}
        sub_titles = {i.title for i in tasks_item["Filters"].values()}
        assert sub_titles == {
            "Create task", "Update task", "Complete task",
            "Uncomplete task", "Move task",
        }

    def test_existing_rule_appears_with_toggle_and_remove(self, app):
        cfg = {"auto_accept_rules": {"gmail.read_message": [{"rule": "i_am_sender"}]}}
        rules_parent = app._build_rules_menu(cfg)
        gmail_item = rules_parent["Gmail"]
        read_message_item = gmail_item["Filters"]["Read message"]
        sub_titles = [i.title for i in read_message_item.values()]
        # Boolean rules (no value) render as a single click-to-remove row --
        # no separate toggle/remove pair, see _build_rule_row.
        assert any("i_am_sender" in t for t in sub_titles)

    def test_rule_with_list_value_shows_each_entry_and_add_value(self, app):
        # List-value rules no longer open a shared multi-line "Edit…" box --
        # each value is its own row (with its own Remove), plus a single
        # "+ Add value…" row to add another one at a time.
        cfg = {"auto_accept_rules": {"gmail.read_message": [{"rule": "trusted_sender_domain", "value": ["a.com"]}]}}
        rules_parent = app._build_rules_menu(cfg)
        read_message_item = rules_parent["Gmail"]["Filters"]["Read message"]
        rule_row = read_message_item["  trusted_sender_domain"]
        sub_titles = [i.title for i in rule_row.values()]
        assert any("a.com" in t for t in sub_titles)
        assert any("Add value" in t for t in sub_titles)
        assert not any("Edit…" in t for t in sub_titles)

    def test_rule_without_value_has_no_edit_entry(self, app):
        cfg = {"auto_accept_rules": {"gmail.read_message": [{"rule": "i_am_sender"}]}}
        rules_parent = app._build_rules_menu(cfg)
        read_message_item = rules_parent["Gmail"]["Filters"]["Read message"]
        sub_titles = [i.title for i in read_message_item.values()]
        assert not any("Edit…" in t for t in sub_titles)


class TestBuildOrgMenu:
    def test_no_org_config_shows_not_installed(self, app):
        org_parent = app._build_org_menu({})
        titles = [i.title for i in org_parent.values() if hasattr(i, 'title')]
        assert any("No organization config installed" in t for t in titles)
        assert any("Install Organization Config" in t for t in titles)

    def test_installed_org_config_shows_name_and_services(self, app):
        org_parent = app._build_org_menu({
            "org_name": "Acme Corp", "google": {"client_id": "x"}, "slack": {"client_id": "y"},
        })
        titles = [i.title for i in org_parent.values() if hasattr(i, 'title')]
        assert any("Acme Corp" in t for t in titles)
        assert any("google" in t and "slack" in t for t in titles)
        assert any("Install/Update Organization Config" in t for t in titles)

    def test_installed_without_org_name_shows_generic_header(self, app):
        org_parent = app._build_org_menu({"google": {"client_id": "x"}})
        titles = [i.title for i in org_parent.values() if hasattr(i, 'title')]
        assert any(t == "Installed" for t in titles)


class TestBuildConnectorsMenu:
    def test_connected_connector_shows_connected_status(self, app):
        app._connectors = ["gmail"]
        connectors_parent = app._build_connectors_menu({"google": {"client_id": "x"}}, {})
        gmail_title = next(t for t in connectors_parent.keys() if "Gmail" in t)
        assert gmail_title.startswith("●")

    def test_disabled_connector_shows_disabled_status(self, app):
        app._connectors = []
        connectors_parent = app._build_connectors_menu(
            {"google": {"client_id": "x"}}, {"gmail": {"enabled": False}},
        )
        gmail_title = next(t for t in connectors_parent.keys() if "Gmail" in t)
        assert gmail_title.startswith("✕")

    def test_missing_org_config_shows_missing_status_and_message(self, app):
        app._connectors = []
        connectors_parent = app._build_connectors_menu({}, {})
        gmail_title = next(t for t in connectors_parent.keys() if "Gmail" in t)
        assert gmail_title.startswith("○")
        gmail_item = connectors_parent[gmail_title]
        sub_titles = [i.title for i in gmail_item.values() if hasattr(i, 'title')]
        assert any("Organization config missing" in t for t in sub_titles)

    def test_org_present_not_authenticated_shows_needs_auth_status(self, app):
        app._connectors = []
        connectors_parent = app._build_connectors_menu({"google": {"client_id": "x"}}, {})
        gmail_title = next(t for t in connectors_parent.keys() if "Gmail" in t)
        assert gmail_title.startswith("◐")
        gmail_item = connectors_parent[gmail_title]
        sub_titles = [i.title for i in gmail_item.values() if hasattr(i, 'title')]
        assert any("Authenticate…" in t for t in sub_titles)

    def test_connected_connector_shows_reconnect_not_authenticate(self, app):
        app._connectors = ["gmail"]
        connectors_parent = app._build_connectors_menu({"google": {"client_id": "x"}}, {})
        gmail_title = next(t for t in connectors_parent.keys() if "Gmail" in t)
        gmail_item = connectors_parent[gmail_title]
        sub_titles = [i.title for i in gmail_item.values() if hasattr(i, 'title')]
        assert any("Reconnect…" in t for t in sub_titles)

    def test_telegram_checks_app_credentials_not_org_config(self, app, monkeypatch):
        monkeypatch.setattr(menu_bar, "telegram_app_credentials", lambda: (123, "hash"))
        app._connectors = []
        connectors_parent = app._build_connectors_menu({}, {})  # empty org_config
        telegram_title = next(t for t in connectors_parent.keys() if "Telegram" in t)
        assert telegram_title.startswith("◐")  # needs auth, not missing org config

    def test_telegram_missing_credentials_shows_build_missing_message(self, app, monkeypatch):
        monkeypatch.setattr(menu_bar, "telegram_app_credentials", lambda: None)
        app._connectors = []
        connectors_parent = app._build_connectors_menu({}, {})
        telegram_title = next(t for t in connectors_parent.keys() if "Telegram" in t)
        telegram_item = connectors_parent[telegram_title]
        sub_titles = [i.title for i in telegram_item.values() if hasattr(i, 'title')]
        assert any("App credentials missing" in t for t in sub_titles)


class _FakeWindowResponse:
    def __init__(self, clicked: bool, text: str = ""):
        self.clicked = clicked
        self.text = text


def _fake_window(clicked: bool, text: str = ""):
    """Returns a stand-in for rumps.Window: .run() gives the canned response,
    regardless of what title/message/default_text kwargs it was built with."""
    class _FakeWindow:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
        def run(self):
            return _FakeWindowResponse(clicked, text)
    return _FakeWindow


class TestAddRule:
    def test_no_configurable_rules_alerts_and_returns(self, app, monkeypatch):
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: alerts.append((a, k)))

        app._add_rule("tasks.anything")  # not a real operation key -- no entry in RULES_BY_OPERATION

        assert len(alerts) == 1

    def test_cancelled_picker_makes_no_change(self, app, monkeypatch):
        monkeypatch.setattr(menu_bar, "_osascript_pick", lambda **kw: None)
        before = app._load_config()

        app._add_rule("gmail.read_message")

        assert app._load_config() == before

    def test_rule_without_value_is_added_directly(self, app, monkeypatch):
        monkeypatch.setattr(menu_bar, "_osascript_pick", lambda **kw: "i_am_sender")

        app._add_rule("gmail.read_message")

        cfg = app._load_config()
        assert cfg["auto_accept_rules"]["gmail.read_message"] == [{"rule": "i_am_sender"}]

    def test_rule_with_list_value_starts_empty_no_prompt(self, app, monkeypatch):
        # List-value rules no longer prompt immediately for a (multi-line)
        # value -- they're created empty and populated one value at a time
        # via "+ Add value..." on the new row (see TestAddRuleValue).
        monkeypatch.setattr(menu_bar, "_osascript_pick", lambda **kw: "trusted_sender_domain")
        window_calls = []
        monkeypatch.setattr(menu_bar.rumps, "Window", lambda **kw: window_calls.append(kw))

        app._add_rule("gmail.read_message")

        assert window_calls == []
        cfg = app._load_config()
        assert cfg["auto_accept_rules"]["gmail.read_message"] == [
            {"rule": "trusted_sender_domain", "value": []}
        ]

    def test_rule_with_int_value_parses_integer(self, app, monkeypatch):
        monkeypatch.setattr(menu_bar, "_osascript_pick", lambda **kw: "age_threshold_days")
        monkeypatch.setattr(menu_bar.rumps, "Window", _fake_window(clicked=True, text="30"))

        app._add_rule("gmail.read_message")

        cfg = app._load_config()
        assert cfg["auto_accept_rules"]["gmail.read_message"] == [{"rule": "age_threshold_days", "value": 30}]

    def test_int_value_non_numeric_alerts_and_does_not_add(self, app, monkeypatch):
        monkeypatch.setattr(menu_bar, "_osascript_pick", lambda **kw: "age_threshold_days")
        monkeypatch.setattr(menu_bar.rumps, "Window", _fake_window(clicked=True, text="not-a-number"))
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: alerts.append((a, k)))

        app._add_rule("gmail.read_message")

        assert len(alerts) == 1
        assert app._load_config().get("auto_accept_rules", {}) == {}

    def test_pair_value_rule_starts_empty_no_prompt(self, app, monkeypatch):
        # approved_spreadsheet is normally offered via a grant now (see
        # resource_grants.py's drive.spreadsheets); this exercises _add_rule's
        # generic pair-value handling regardless of what the picker returned.
        monkeypatch.setattr(menu_bar, "_osascript_pick", lambda **kw: "approved_spreadsheet")
        window_calls = []
        monkeypatch.setattr(menu_bar.rumps, "Window", lambda **kw: window_calls.append(kw))

        app._add_rule("sheets.read_values")

        assert window_calls == []
        cfg = app._load_config()
        assert cfg["auto_accept_rules"]["sheets.read_values"] == [
            {"rule": "approved_spreadsheet", "value": []}
        ]

    def test_add_rule_int_prompt_starts_empty_not_prefilled_with_hint(self, app, monkeypatch):
        # Regression: the "Add rule" dialog used to pre-fill the text field with
        # the RULE_HINTS example value, so the first line looked like garbage
        # data the user had to delete before typing their real value. The
        # example belongs in the message text, not in the editable field's
        # initial content. (List/pair-value rules no longer prompt at all in
        # _add_rule -- see test_rule_with_list_value_starts_empty_no_prompt --
        # so the int-value case is the only one left that still opens a Window
        # here.)
        monkeypatch.setattr(menu_bar, "_osascript_pick", lambda **kw: "age_threshold_days")
        captured = {}
        class _CapturingWindow:
            def __init__(self, **kwargs):
                captured.update(kwargs)
            def run(self):
                return _FakeWindowResponse(clicked=False)
        monkeypatch.setattr(menu_bar.rumps, "Window", _CapturingWindow)

        app._add_rule("gmail.read_message")

        assert captured["default_text"] == ""
        assert "Example:" in captured["message"]
        assert menu_bar.RULE_HINTS["age_threshold_days"] in captured["message"]

    def test_grant_covered_rules_are_not_offered(self, app, monkeypatch):
        # approved_sandbox_folder is grant-managed now (drive.sandbox_folders
        # -- see resource_grants.py); rename_sheet/format_range used to have
        # no folder-scoped rule offered at all, then gained one as a raw rule
        # -- now it's offered as a grant capability instead, so it must NOT
        # appear in _add_rule's own picker options anymore (there's no longer
        # a second, more tedious way to do the same thing).
        captured = {}
        monkeypatch.setattr(menu_bar, "_osascript_pick", lambda **kw: captured.update(kw) or None)

        app._add_rule("sheets.rename_sheet")
        assert "approved_sandbox_folder" not in captured["options"]

        captured.clear()
        app._add_rule("sheets.format_range")
        assert "approved_sandbox_folder" not in captured["options"]


class TestEditRuleValue:
    """_edit_rule_value is only reachable for RULES_INT_VALUE rows now --
    list/pair values are edited one at a time via _add_rule_value/
    _remove_rule_value instead (see TestAddRuleValue/TestRemoveRuleValue)."""

    def _seed(self, app, op_key, rules):
        cfg = app._load_config()
        cfg.setdefault("auto_accept_rules", {})[op_key] = rules
        app._save_config(cfg)

    def test_edits_int_value_in_place(self, app, monkeypatch):
        self._seed(app, "gmail.read_message", [{"rule": "age_threshold_days", "value": 10}])
        monkeypatch.setattr(menu_bar.rumps, "Window", _fake_window(clicked=True, text="99"))

        app._edit_rule_value("gmail.read_message", 0)

        rules = app._load_config()["auto_accept_rules"]["gmail.read_message"]
        assert rules[0]["value"] == 99

    def test_prefills_current_int_value_as_default_text(self, app, monkeypatch):
        self._seed(app, "gmail.read_message", [{"rule": "age_threshold_days", "value": 10}])
        captured = {}
        class _CapturingWindow:
            def __init__(self, **kwargs):
                captured.update(kwargs)
            def run(self):
                return _FakeWindowResponse(clicked=False)
        monkeypatch.setattr(menu_bar.rumps, "Window", _CapturingWindow)

        app._edit_rule_value("gmail.read_message", 0)

        assert captured["default_text"] == "10"

    def test_cancelled_edit_makes_no_change(self, app, monkeypatch):
        self._seed(app, "gmail.read_message", [{"rule": "trusted_sender_domain", "value": ["a.com"]}])
        monkeypatch.setattr(menu_bar.rumps, "Window", _fake_window(clicked=False))

        app._edit_rule_value("gmail.read_message", 0)

        rules = app._load_config()["auto_accept_rules"]["gmail.read_message"]
        assert rules[0]["value"] == ["a.com"]

    def test_empty_text_after_strip_makes_no_change(self, app, monkeypatch):
        self._seed(app, "gmail.read_message", [{"rule": "trusted_sender_domain", "value": ["a.com"]}])
        monkeypatch.setattr(menu_bar.rumps, "Window", _fake_window(clicked=True, text="   "))

        app._edit_rule_value("gmail.read_message", 0)

        rules = app._load_config()["auto_accept_rules"]["gmail.read_message"]
        assert rules[0]["value"] == ["a.com"]

    def test_invalid_int_alerts_and_does_not_change_value(self, app, monkeypatch):
        self._seed(app, "gmail.read_message", [{"rule": "age_threshold_days", "value": 10}])
        monkeypatch.setattr(menu_bar.rumps, "Window", _fake_window(clicked=True, text="nope"))
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: alerts.append((a, k)))

        app._edit_rule_value("gmail.read_message", 0)

        assert len(alerts) == 1
        rules = app._load_config()["auto_accept_rules"]["gmail.read_message"]
        assert rules[0]["value"] == 10

    def test_out_of_range_index_is_a_no_op(self, app):
        self._seed(app, "gmail.read_message", [{"rule": "i_am_sender"}])
        before = app._load_config()

        app._edit_rule_value("gmail.read_message", 9)

        assert app._load_config() == before


class TestRemoveRule:
    def _seed(self, app, op_key, rules):
        cfg = app._load_config()
        cfg.setdefault("auto_accept_rules", {})[op_key] = rules
        app._save_config(cfg)

    def test_confirmed_removal_removes_the_rule(self, app, monkeypatch):
        self._seed(app, "gmail.read_message", [{"rule": "i_am_sender"}, {"rule": "trusted_sender_domain"}])
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda **kw: 1)

        app._remove_rule("gmail.read_message", 0)

        rules = app._load_config()["auto_accept_rules"]["gmail.read_message"]
        assert rules == [{"rule": "trusted_sender_domain"}]

    def test_cancelled_removal_makes_no_change(self, app, monkeypatch):
        self._seed(app, "gmail.read_message", [{"rule": "i_am_sender"}])
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda **kw: 0)

        app._remove_rule("gmail.read_message", 0)

        rules = app._load_config()["auto_accept_rules"]["gmail.read_message"]
        assert rules == [{"rule": "i_am_sender"}]

    def test_removing_last_rule_drops_the_operation_key(self, app, monkeypatch):
        self._seed(app, "gmail.read_message", [{"rule": "i_am_sender"}])
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda **kw: 1)

        app._remove_rule("gmail.read_message", 0)

        assert "gmail.read_message" not in app._load_config().get("auto_accept_rules", {})

    def test_out_of_range_index_is_a_no_op(self, app, monkeypatch):
        self._seed(app, "gmail.read_message", [{"rule": "i_am_sender"}])
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda **kw: 1)
        before = app._load_config()

        app._remove_rule("gmail.read_message", 9)

        assert app._load_config() == before


class TestAddRuleValue:
    def _seed(self, app, op_key, rules):
        cfg = app._load_config()
        cfg.setdefault("auto_accept_rules", {})[op_key] = rules
        app._save_config(cfg)

    def test_appends_a_value_to_an_existing_list_rule(self, app, monkeypatch):
        self._seed(app, "gmail.read_message", [{"rule": "trusted_sender_domain", "value": ["a.com"]}])
        monkeypatch.setattr(menu_bar.rumps, "Window", _fake_window(clicked=True, text="b.com"))

        app._add_rule_value("gmail.read_message", 0)

        rules = app._load_config()["auto_accept_rules"]["gmail.read_message"]
        assert rules[0]["value"] == ["a.com", "b.com"]

    def test_does_not_duplicate_an_existing_value(self, app, monkeypatch):
        self._seed(app, "gmail.read_message", [{"rule": "trusted_sender_domain", "value": ["a.com"]}])
        monkeypatch.setattr(menu_bar.rumps, "Window", _fake_window(clicked=True, text="a.com"))

        app._add_rule_value("gmail.read_message", 0)

        rules = app._load_config()["auto_accept_rules"]["gmail.read_message"]
        assert rules[0]["value"] == ["a.com"]

    def test_hint_shown_in_message_not_prefilled(self, app, monkeypatch):
        # Same fix as _add_rule's int-value prompt -- see
        # test_add_rule_int_prompt_starts_empty_not_prefilled_with_hint.
        self._seed(app, "gmail.read_message", [{"rule": "trusted_sender_domain", "value": []}])
        captured = {}
        class _CapturingWindow:
            def __init__(self, **kwargs):
                captured.update(kwargs)
            def run(self):
                return _FakeWindowResponse(clicked=False)
        monkeypatch.setattr(menu_bar.rumps, "Window", _CapturingWindow)

        app._add_rule_value("gmail.read_message", 0)

        assert captured["default_text"] == ""
        assert "Example:" in captured["message"]

    def test_pair_value_parses_id_and_id_colon_tab(self, app, monkeypatch):
        self._seed(app, "sheets.read_values", [{"rule": "approved_spreadsheet", "value": []}])
        monkeypatch.setattr(menu_bar.rumps, "Window", _fake_window(clicked=True, text="sheet1:Sheet1"))

        app._add_rule_value("sheets.read_values", 0)

        rules = app._load_config()["auto_accept_rules"]["sheets.read_values"]
        assert rules[0]["value"] == [{"spreadsheet_id": "sheet1", "tab": "Sheet1"}]

    def test_cancelled_prompt_makes_no_change(self, app, monkeypatch):
        self._seed(app, "gmail.read_message", [{"rule": "trusted_sender_domain", "value": ["a.com"]}])
        monkeypatch.setattr(menu_bar.rumps, "Window", _fake_window(clicked=False))

        app._add_rule_value("gmail.read_message", 0)

        rules = app._load_config()["auto_accept_rules"]["gmail.read_message"]
        assert rules[0]["value"] == ["a.com"]

    def test_out_of_range_index_is_a_no_op(self, app):
        self._seed(app, "gmail.read_message", [{"rule": "trusted_sender_domain", "value": ["a.com"]}])
        before = app._load_config()

        app._add_rule_value("gmail.read_message", 9)

        assert app._load_config() == before


class TestRemoveRuleValue:
    def _seed(self, app, op_key, rules):
        cfg = app._load_config()
        cfg.setdefault("auto_accept_rules", {})[op_key] = rules
        app._save_config(cfg)

    def test_removes_one_value_keeps_the_rest(self, app):
        self._seed(app, "gmail.read_message", [{"rule": "trusted_sender_domain", "value": ["a.com", "b.com"]}])

        app._remove_rule_value("gmail.read_message", 0, 0)

        rules = app._load_config()["auto_accept_rules"]["gmail.read_message"]
        assert rules[0]["value"] == ["b.com"]

    def test_removing_the_last_value_drops_the_whole_rule(self, app):
        self._seed(app, "gmail.read_message", [{"rule": "trusted_sender_domain", "value": ["a.com"]}])

        app._remove_rule_value("gmail.read_message", 0, 0)

        assert "gmail.read_message" not in app._load_config().get("auto_accept_rules", {})

    def test_removing_last_value_of_one_rule_keeps_sibling_rules(self, app):
        self._seed(app, "gmail.read_message", [
            {"rule": "trusted_sender_domain", "value": ["a.com"]},
            {"rule": "i_am_sender"},
        ])

        app._remove_rule_value("gmail.read_message", 0, 0)

        rules = app._load_config()["auto_accept_rules"]["gmail.read_message"]
        assert rules == [{"rule": "i_am_sender"}]

    def test_out_of_range_value_index_is_a_no_op(self, app):
        self._seed(app, "gmail.read_message", [{"rule": "trusted_sender_domain", "value": ["a.com"]}])
        before = app._load_config()

        app._remove_rule_value("gmail.read_message", 0, 9)

        assert app._load_config() == before


class TestClientFor:
    def test_returns_the_connected_connector_s_client(self, app):
        fake_client = object()
        app._connector_objs = {"drive": SimpleNamespace(client=fake_client)}

        assert app._client_for("drive") is fake_client

    def test_returns_none_for_an_unconnected_connector(self, app):
        app._connector_objs = {}

        assert app._client_for("drive") is None


class TestBuildGrantResourceMenu:
    def test_new_grant_defaults_every_capability_off(self, app):
        cfg = {"auto_accept_grants": {"drive": {"sandbox_folders": [{"id": "F1"}]}}}
        rt = menu_bar.grant_resource_type("drive", "sandbox_folders")
        grants_cfg = cfg["auto_accept_grants"]

        group_item = app._build_grant_resource_menu(rt, grants_cfg)

        folder_row = group_item.values()[0]
        assert any("☑" in c.title for c in folder_row.values()) is False

    def test_resolved_name_is_shown_instead_of_raw_id(self, app):
        rt = menu_bar.grant_resource_type("drive", "sandbox_folders")
        grants_cfg = {"drive": {"sandbox_folders": [{"id": "F1", "name": "Scratch", "write": True}]}}

        group_item = app._build_grant_resource_menu(rt, grants_cfg)

        folder_row = group_item.values()[0]
        assert "Scratch" in folder_row.title
        assert "F1" not in folder_row.title

    def test_no_name_falls_back_to_short_id(self, app):
        rt = menu_bar.grant_resource_type("drive", "sandbox_folders")
        long_id = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
        grants_cfg = {"drive": {"sandbox_folders": [{"id": long_id, "write": True}]}}

        group_item = app._build_grant_resource_menu(rt, grants_cfg)

        folder_row = group_item.values()[0]
        assert menu_bar._short_id(long_id) in folder_row.title

    def test_add_item_is_always_present(self, app):
        rt = menu_bar.grant_resource_type("drive", "sandbox_folders")

        group_item = app._build_grant_resource_menu(rt, {})

        assert any("Add folder" in i.title for i in group_item.values())


class TestToggleGrantCapability:
    def test_toggles_capability_on(self, app):
        cfg = {"auto_accept_grants": {"drive": {"sandbox_folders": [{"id": "F1", "write": False}]}}}
        app._save_config(cfg)

        app._toggle_grant_capability("drive", "sandbox_folders", 0, "write")

        entries = app._load_config()["auto_accept_grants"]["drive"]["sandbox_folders"]
        assert entries[0]["write"] is True

    def test_toggles_capability_off(self, app):
        cfg = {"auto_accept_grants": {"drive": {"sandbox_folders": [{"id": "F1", "write": True}]}}}
        app._save_config(cfg)

        app._toggle_grant_capability("drive", "sandbox_folders", 0, "write")

        entries = app._load_config()["auto_accept_grants"]["drive"]["sandbox_folders"]
        assert entries[0]["write"] is False

    def test_unknown_resource_type_is_a_no_op(self, app):
        before = app._load_config()

        app._toggle_grant_capability("nope", "nope", 0, "write")

        assert app._load_config() == before

    def test_out_of_range_index_is_a_no_op(self, app):
        cfg = {"auto_accept_grants": {"drive": {"sandbox_folders": [{"id": "F1", "write": True}]}}}
        app._save_config(cfg)
        before = app._load_config()

        app._toggle_grant_capability("drive", "sandbox_folders", 9, "write")

        assert app._load_config() == before


class TestRemoveGrant:
    def test_confirmed_removal_removes_the_entry(self, app, monkeypatch):
        cfg = {"auto_accept_grants": {"drive": {"sandbox_folders": [{"id": "F1", "write": True}]}}}
        app._save_config(cfg)
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda **kw: 1)

        app._remove_grant("drive", "sandbox_folders", 0)

        assert "auto_accept_grants" not in app._load_config() or not app._load_config().get(
            "auto_accept_grants", {}
        ).get("drive", {}).get("sandbox_folders")

    def test_cancelled_removal_makes_no_change(self, app, monkeypatch):
        cfg = {"auto_accept_grants": {"drive": {"sandbox_folders": [{"id": "F1", "write": True}]}}}
        app._save_config(cfg)
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda **kw: 0)

        app._remove_grant("drive", "sandbox_folders", 0)

        entries = app._load_config()["auto_accept_grants"]["drive"]["sandbox_folders"]
        assert entries == [{"id": "F1", "write": True}]

    def test_out_of_range_index_is_a_no_op(self, app, monkeypatch):
        cfg = {"auto_accept_grants": {"drive": {"sandbox_folders": [{"id": "F1", "write": True}]}}}
        app._save_config(cfg)
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda **kw: 1)
        before = app._load_config()

        app._remove_grant("drive", "sandbox_folders", 9)

        assert app._load_config() == before


class TestConfirmAndSaveGrant:
    def test_confirmed_add_creates_a_grant_with_every_capability_off(self, app, monkeypatch):
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda **kw: 1)
        rt = menu_bar.grant_resource_type("drive", "sandbox_folders")

        app._confirm_and_save_grant(rt, "F1", "Scratch", "")

        entries = app._load_config()["auto_accept_grants"]["drive"]["sandbox_folders"]
        assert entries == [{"id": "F1", "name": "Scratch"}]

    def test_cancelled_confirmation_saves_nothing(self, app, monkeypatch):
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda **kw: 0)
        rt = menu_bar.grant_resource_type("drive", "sandbox_folders")

        app._confirm_and_save_grant(rt, "F1", "Scratch", "")

        assert app._load_config().get("auto_accept_grants", {}) == {}

    def test_duplicate_resource_id_is_rejected(self, app, monkeypatch):
        cfg = {"auto_accept_grants": {"drive": {"sandbox_folders": [{"id": "F1", "write": True}]}}}
        app._save_config(cfg)
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **kw: alerts.append((a, kw)) or 1)
        rt = menu_bar.grant_resource_type("drive", "sandbox_folders")

        app._confirm_and_save_grant(rt, "F1", "Scratch", "")

        entries = app._load_config()["auto_accept_grants"]["drive"]["sandbox_folders"]
        assert entries == [{"id": "F1", "write": True}]  # unchanged, not duplicated
        assert len(alerts) == 2  # the "already trusted" alert, on top of the initial confirmation

    def test_spreadsheet_tab_is_stored_on_the_entry(self, app, monkeypatch):
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda **kw: 1)
        rt = menu_bar.grant_resource_type("drive", "spreadsheets")

        app._confirm_and_save_grant(rt, "S1", "Budget Sheet", "Q3")

        entries = app._load_config()["auto_accept_grants"]["drive"]["spreadsheets"]
        assert entries == [{"id": "S1", "name": "Budget Sheet", "tab": "Q3"}]


class TestOnCandidatesListed:
    def test_no_candidates_alerts_and_saves_nothing(self, app, monkeypatch):
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **kw: alerts.append((a, kw)) or 1)
        rt = menu_bar.grant_resource_type("tasks", "task_lists")

        app._on_candidates_listed(rt, [])

        assert len(alerts) == 1
        assert app._load_config().get("auto_accept_grants", {}) == {}

    def test_picked_candidate_is_saved_with_its_name(self, app, monkeypatch):
        monkeypatch.setattr(menu_bar, "_osascript_pick", lambda **kw: "Work (LIST2)")
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda **kw: 1)
        rt = menu_bar.grant_resource_type("tasks", "task_lists")

        app._on_candidates_listed(rt, [("LIST1", "Personal"), ("LIST2", "Work")])

        entries = app._load_config()["auto_accept_grants"]["tasks"]["task_lists"]
        assert entries == [{"id": "LIST2", "name": "Work"}]

    def test_cancelled_picker_saves_nothing(self, app, monkeypatch):
        monkeypatch.setattr(menu_bar, "_osascript_pick", lambda **kw: None)
        rt = menu_bar.grant_resource_type("tasks", "task_lists")

        app._on_candidates_listed(rt, [("LIST1", "Personal")])

        assert app._load_config().get("auto_accept_grants", {}) == {}


class TestAddGrant:
    def test_enumerable_resource_lists_candidates_via_the_live_client(self, app, monkeypatch):
        monkeypatch.setattr(menu_bar, "_osascript_pick", lambda **kw: None)  # cancel the picker
        listed = []

        class _FakeTaskList:
            def __init__(self, id, title):
                self.id = id
                self.title = title

        class _FakeTasksClient:
            def list_task_lists(self):
                listed.append(True)
                return [_FakeTaskList("LIST1", "Personal")]

        app._connector_objs = {"tasks": SimpleNamespace(client=_FakeTasksClient())}

        app._add_grant("tasks", "task_lists")

        assert listed == [True]

    def test_enumerable_resource_with_no_connected_client_alerts(self, app, monkeypatch):
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **kw: alerts.append((a, kw)) or 1)
        app._connector_objs = {}

        app._add_grant("tasks", "task_lists")

        assert len(alerts) == 1
        assert app._load_config().get("auto_accept_grants", {}) == {}

    def test_paste_id_resource_extracts_id_from_pasted_url_and_confirms(self, app, monkeypatch):
        monkeypatch.setattr(
            menu_bar.rumps, "Window",
            _fake_window(clicked=True, text="https://drive.google.com/drive/folders/FOLDER9"),
        )
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda **kw: 1)
        app._connector_objs = {}  # no client -- confirm-by-ID-only path

        app._add_grant("drive", "sandbox_folders")

        entries = app._load_config()["auto_accept_grants"]["drive"]["sandbox_folders"]
        assert entries == [{"id": "FOLDER9"}]

    def test_paste_id_resource_cancelled_prompt_saves_nothing(self, app, monkeypatch):
        monkeypatch.setattr(menu_bar.rumps, "Window", _fake_window(clicked=False))

        app._add_grant("drive", "sandbox_folders")

        assert app._load_config().get("auto_accept_grants", {}) == {}

    def test_paste_id_resource_with_unparseable_text_alerts(self, app, monkeypatch):
        monkeypatch.setattr(menu_bar.rumps, "Window", _fake_window(clicked=True, text="not a url or id"))
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **kw: alerts.append((a, kw)) or 1)

        app._add_grant("drive", "sandbox_folders")

        assert len(alerts) == 1
        assert app._load_config().get("auto_accept_grants", {}) == {}


class TestExtractDriveId:
    def test_bare_id_is_accepted_as_is(self):
        assert menu_bar._extract_drive_id("FOLDER1") == "FOLDER1"

    def test_folder_url_extracts_the_id(self):
        url = "https://drive.google.com/drive/folders/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms?usp=sharing"
        assert menu_bar._extract_drive_id(url) == "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"

    def test_file_url_extracts_the_id(self):
        url = "https://docs.google.com/spreadsheets/d/1AbCdEf12345/edit#gid=0"
        assert menu_bar._extract_drive_id(url) == "1AbCdEf12345"

    def test_unparseable_text_returns_empty_string(self):
        assert menu_bar._extract_drive_id("not a url or id, has spaces") == ""

    def test_empty_text_returns_empty_string(self):
        assert menu_bar._extract_drive_id("") == ""


class TestShortId:
    def test_short_id_returned_unchanged(self):
        assert menu_bar._short_id("FOLDER1") == "FOLDER1"

    def test_long_id_truncated_with_ellipsis(self):
        long_id = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
        result = menu_bar._short_id(long_id)
        assert result.startswith("1BxiMVs0")
        assert result.endswith(long_id[-6:])
        assert "…" in result


class TestInstallOrgConfig:
    def _fake_picker(self, monkeypatch, path: str | None):
        def fake_run(cmd, **kwargs):
            return SimpleNamespace(stdout=(path or ""))
        monkeypatch.setattr(menu_bar.subprocess, "run", fake_run)

    def test_cancelled_file_picker_makes_no_change(self, app, monkeypatch, tmp_path):
        self._fake_picker(monkeypatch, None)
        monkeypatch.setattr(menu_bar, "org_dir", lambda: tmp_path)

        app._install_org_config()

        assert not (tmp_path / "org_config.json").exists()

    def test_non_json_file_alerts_and_does_not_install(self, app, monkeypatch, tmp_path):
        src = tmp_path / "bad.json"
        src.write_text("not valid json", encoding="utf-8")
        self._fake_picker(monkeypatch, str(src))
        monkeypatch.setattr(menu_bar, "org_dir", lambda: tmp_path)
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: alerts.append((a, k)))

        app._install_org_config()

        assert len(alerts) == 1
        assert not (tmp_path / "org_config.json").exists()

    def test_json_without_version_field_is_rejected(self, app, monkeypatch, tmp_path):
        src = tmp_path / "bundle.json"
        src.write_text(json.dumps({"google": {"client_id": "x"}}), encoding="utf-8")
        self._fake_picker(monkeypatch, str(src))
        monkeypatch.setattr(menu_bar, "org_dir", lambda: tmp_path)
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: alerts.append((a, k)))

        app._install_org_config()

        assert len(alerts) == 1
        assert not (tmp_path / "org_config.json").exists()

    def test_valid_bundle_is_installed_and_confirmed(self, app, monkeypatch, tmp_path):
        dest_dir = tmp_path / "org"
        dest_dir.mkdir()
        src = tmp_path / "bundle.json"
        bundle = {"version": 1, "org_name": "Acme", "google": {"client_id": "x", "client_secret": "y"}}
        src.write_text(json.dumps(bundle), encoding="utf-8")
        self._fake_picker(monkeypatch, str(src))
        monkeypatch.setattr(menu_bar, "org_dir", lambda: dest_dir)
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: alerts.append((a, k)))

        app._install_org_config()

        installed = json.loads((dest_dir / "org_config.json").read_text(encoding="utf-8"))
        assert installed == bundle
        assert len(alerts) == 1
        assert "Acme" in str(alerts[0])


class TestToggleConnector:
    def test_flips_enabled_flag_and_refreshes(self, app, monkeypatch):
        refresh_calls = []
        monkeypatch.setattr(app, "_refresh_connectors", lambda: refresh_calls.append(1))

        app._toggle_connector("gmail")

        cfg = app._load_config()
        assert cfg["connectors"]["gmail"]["enabled"] is False
        assert refresh_calls == [1]

    def test_toggling_twice_re_enables(self, app, monkeypatch):
        monkeypatch.setattr(app, "_refresh_connectors", lambda: None)

        app._toggle_connector("gmail")
        app._toggle_connector("gmail")

        assert app._load_config()["connectors"]["gmail"]["enabled"] is True


class TestTogglePiiDetection:
    def test_flips_enabled_flag_and_saves(self, app):
        app._toggle_pii_detection()

        cfg = app._load_config()
        assert cfg["pii_detection"]["enabled"] is False

    def test_toggling_twice_re_enables(self, app):
        app._toggle_pii_detection()
        app._toggle_pii_detection()

        assert app._load_config()["pii_detection"]["enabled"] is True

    def test_defaults_to_enabled_when_unset(self, app):
        # No pii_detection section in config yet -> treated as enabled, so
        # the first toggle should turn it off.
        assert "pii_detection" not in app._load_config()

        app._toggle_pii_detection()

        assert app._load_config()["pii_detection"]["enabled"] is False

    def test_hot_reloads_live_detector_state(self, app):
        from privacyfence import pii_detector

        assert pii_detector.is_pii_detection_enabled() is True

        app._toggle_pii_detection()

        assert pii_detector.is_pii_detection_enabled() is False

    def test_menu_item_state_reflects_config(self, app):
        app._toggle_pii_detection()  # now disabled

        item = app.menu["PII Detection Gate"]
        assert bool(item.state) is False


class TestRefreshConnectors:
    def test_updates_connectors_and_pushes_to_ipc_server_after_drain(self, app, monkeypatch):
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append((f, a, k)))
        monkeypatch.setattr(menu_bar, "build_connectors", lambda cfg, org: [SimpleNamespace(name="drive")])

        app._refresh_connectors()

        assert wait_until(lambda: len(recorded) == 1)
        func, args, kwargs = recorded[0]
        func(*args, **kwargs)

        assert app._connectors == ["drive"]
        assert app._ipc_calls == [[SimpleNamespace(name="drive")]]


class TestAuthenticateDispatch:
    @pytest.mark.parametrize("cname,method", [
        ("gmail", "_authenticate_google"),
        ("drive", "_authenticate_google"),
        ("contacts", "_authenticate_google"),
        ("calendar", "_authenticate_google"),
        ("tasks", "_authenticate_google"),
        ("slack", "_authenticate_slack"),
        ("salesforce", "_authenticate_salesforce"),
        ("jira", "_authenticate_atlassian"),
        ("confluence", "_authenticate_atlassian"),
        ("telegram", "_authenticate_telegram"),
    ])
    def test_dispatches_to_the_right_per_service_method(self, app, monkeypatch, cname, method):
        calls = []
        if method == "_authenticate_telegram":
            # _authenticate_telegram takes no args beyond self; since
            # monkeypatch.setattr replaces it as a plain instance attribute
            # (not a bound method), _authenticate's `self._authenticate_telegram()`
            # call passes no positional args at all.
            monkeypatch.setattr(app, method, lambda: calls.append(cname))
        else:
            # The others are called as self._authenticate_X(cname_or_none, org_config)
            # depending on which branch -- Google's takes (cname, org_config),
            # Slack/Salesforce/Atlassian take just (org_config). Accept either shape.
            monkeypatch.setattr(app, method, lambda *a, _m=method: calls.append((cname, _m, a)))
        monkeypatch.setattr(menu_bar, "load_org_config", lambda: {})

        app._authenticate(cname)

        assert len(calls) == 1


class TestAuthenticateGoogle:
    def test_missing_org_config_alerts_without_running_flow(self, app, monkeypatch):
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: alerts.append((a, k)))
        run_async_calls = []
        monkeypatch.setattr(app, "_run_async", lambda *a: run_async_calls.append(a))

        app._authenticate_google("gmail", {})

        assert len(alerts) == 1
        assert run_async_calls == []

    def test_runs_authorize_and_check_connection_on_background_thread(self, app, monkeypatch):
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append((f, a, k)))
        calls = []

        class FakeGmailClient:
            def __init__(self, client_config, token_file):
                calls.append(("init", client_config, token_file))
            def authorize_interactive(self):
                calls.append(("authorize",))
            def check_connection(self):
                calls.append(("check",))
                return "me@example.com"

        monkeypatch.setitem(menu_bar._GOOGLE_CLIENTS, "gmail", FakeGmailClient)

        app._authenticate_google("gmail", {"google": {"client_id": "i", "client_secret": "s"}})

        assert wait_until(lambda: recorded)
        assert calls == [
            ("init", {"installed": {"client_id": "i", "client_secret": "s"}}, calls[0][2]),
            ("authorize",), ("check",),
        ]

    def test_failed_auth_alerts_and_rebuilds_without_refreshing_connectors(self, app, monkeypatch):
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append((f, a, k)))
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: alerts.append((a, k)))
        rebuild_calls = []
        monkeypatch.setattr(app, "_rebuild", lambda: rebuild_calls.append(1))
        refresh_calls = []
        monkeypatch.setattr(app, "_refresh_connectors", lambda: refresh_calls.append(1))

        class FailingGmailClient:
            def __init__(self, client_config, token_file):
                pass
            def authorize_interactive(self):
                raise RuntimeError("user closed browser")

        monkeypatch.setitem(menu_bar._GOOGLE_CLIENTS, "gmail", FailingGmailClient)

        app._authenticate_google("gmail", {"google": {"client_id": "i", "client_secret": "s"}})

        assert wait_until(lambda: recorded)
        func, args, kwargs = recorded[0]
        func(*args, **kwargs)

        assert len(alerts) == 1
        assert rebuild_calls == [1]
        assert refresh_calls == []


class TestAuthenticateSlack:
    def test_missing_org_config_alerts_without_running_flow(self, app, monkeypatch):
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: alerts.append((a, k)))
        run_async_calls = []
        monkeypatch.setattr(app, "_run_async", lambda *a: run_async_calls.append(a))

        app._authenticate_slack({})

        assert len(alerts) == 1
        assert run_async_calls == []

    def test_success_shows_team_name_and_refreshes(self, app, monkeypatch):
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append((f, a, k)))
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: alerts.append((a, k)))
        monkeypatch.setattr(menu_bar, "slack_authorize_interactive", lambda **kw: {"team_name": "Acme"})
        refresh_calls = []
        monkeypatch.setattr(app, "_refresh_connectors", lambda: refresh_calls.append(1))

        app._authenticate_slack({"slack": {"client_id": "id", "client_secret": "s"}})

        assert wait_until(lambda: recorded)
        func, args, kwargs = recorded[0]
        func(*args, **kwargs)

        assert any("Acme" in str(a) for a in alerts)
        assert refresh_calls == [1]


class TestAuthenticateSalesforce:
    def test_missing_org_config_alerts_without_running_flow(self, app, monkeypatch):
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: alerts.append((a, k)))
        run_async_calls = []
        monkeypatch.setattr(app, "_run_async", lambda *a: run_async_calls.append(a))

        app._authenticate_salesforce({})

        assert len(alerts) == 1
        assert run_async_calls == []

    def test_success_shows_instance_url_and_refreshes(self, app, monkeypatch):
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append((f, a, k)))
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: alerts.append((a, k)))
        monkeypatch.setattr(
            menu_bar, "salesforce_authorize_interactive", lambda **kw: {"instance_url": "https://x.salesforce.com"}
        )
        refresh_calls = []
        monkeypatch.setattr(app, "_refresh_connectors", lambda: refresh_calls.append(1))

        app._authenticate_salesforce({"salesforce": {"consumer_key": "ck"}})

        assert wait_until(lambda: recorded)
        func, args, kwargs = recorded[0]
        func(*args, **kwargs)

        assert any("x.salesforce.com" in str(a) for a in alerts)
        assert refresh_calls == [1]


class TestAuthenticateAtlassian:
    def test_missing_org_config_alerts_without_running_flow(self, app, monkeypatch):
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: alerts.append((a, k)))
        run_async_calls = []
        monkeypatch.setattr(app, "_run_async", lambda *a: run_async_calls.append(a))

        app._authenticate_atlassian({})

        assert len(alerts) == 1
        assert run_async_calls == []

    def test_success_mentions_both_jira_and_confluence(self, app, monkeypatch):
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append((f, a, k)))
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: alerts.append((a, k)))
        monkeypatch.setattr(
            menu_bar, "atlassian_authorize_interactive", lambda **kw: {"site_url": "https://acme.atlassian.net"}
        )
        refresh_calls = []
        monkeypatch.setattr(app, "_refresh_connectors", lambda: refresh_calls.append(1))

        app._authenticate_atlassian({"atlassian": {"client_id": "ci"}})

        assert wait_until(lambda: recorded)
        func, args, kwargs = recorded[0]
        func(*args, **kwargs)

        assert any("Jira and Confluence" in str(a) for a in alerts)
        assert refresh_calls == [1]

    def test_multiple_sites_pick_resource_uses_osascript_picker(self, app, monkeypatch):
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append((f, a, k)))
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: None)
        monkeypatch.setattr(menu_bar, "_osascript_pick", lambda **kw: "https://b.atlassian.net")

        captured_pick_resource = {}
        def fake_authorize(**kwargs):
            captured_pick_resource["fn"] = kwargs["pick_resource"]
            resources = [
                {"url": "https://a.atlassian.net", "id": "a"},
                {"url": "https://b.atlassian.net", "id": "b"},
            ]
            chosen = kwargs["pick_resource"](resources)
            return {"site_url": chosen["url"]}
        monkeypatch.setattr(menu_bar, "atlassian_authorize_interactive", fake_authorize)

        app._authenticate_atlassian({"atlassian": {"client_id": "ci"}})

        assert wait_until(lambda: recorded)
        func, args, kwargs = recorded[0]
        func(*args, **kwargs)
        # The work() ran on the background thread already, synchronously
        # calling pick_resource -- verify it picked "b" per the osascript stub.
        assert args[1]["site_url"] == "https://b.atlassian.net"


class TestPrompt:
    def test_shows_window_on_main_thread_and_returns_response(self, app, monkeypatch):
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: f(*a, **k))
        monkeypatch.setattr(menu_bar.rumps, "Window", _fake_window(clicked=True, text="hello"))

        clicked, text = app._prompt(title="T", message="M")

        assert clicked is True
        assert text == "hello"

    def test_cancelled_window_returns_false_and_empty_text(self, app, monkeypatch):
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: f(*a, **k))
        monkeypatch.setattr(menu_bar.rumps, "Window", _fake_window(clicked=False, text=""))

        clicked, text = app._prompt(title="T", message="M")

        assert clicked is False
        assert text == ""


class TestUnattendedIndicator:
    """The top menu item's live count of connections currently in an
    unattended session (see docs/TECHNICAL_REFERENCE.md's "Scheduled /
    unattended Cowork tasks" section) -- ipc_server.py fires
    set_unattended_changed_listener from its own
    asyncio thread, so this must marshal through AppHelper.callAfter the
    same way _on_rules_changed does (see TestRunAsyncMarshaling's module
    docstring for why that matters)."""

    def test_status_label_no_unattended_sessions(self, app):
        app._ipc_server.unattended_session_count = lambda: 0
        assert app._status_label() == "PrivacyFence is running"

    def test_status_label_singular(self, app):
        app._ipc_server.unattended_session_count = lambda: 1
        assert app._status_label() == "PrivacyFence is running — 1 unattended session active"

    def test_status_label_plural(self, app):
        app._ipc_server.unattended_session_count = lambda: 3
        assert app._status_label() == "PrivacyFence is running — 3 unattended sessions active"

    def test_registers_a_listener_with_the_ipc_server_on_init(self, tmp_path, monkeypatch):
        monkeypatch.setattr(menu_bar, "_find_icon", lambda: None)
        monkeypatch.setattr(menu_bar, "load_org_config", lambda: {})
        config_path = tmp_path / "settings.yaml"
        config_path.write_text("auto_accept_rules: {}\nconnectors: {}\n", encoding="utf-8")

        registered = []
        ipc_server = SimpleNamespace(
            set_connectors=lambda conns: None,
            unattended_session_count=lambda: 0,
            set_unattended_changed_listener=lambda callback: registered.append(callback),
        )

        instance = menu_bar.PrivacyFenceMenuBar(str(config_path), connectors=[], ipc_server=ipc_server)

        assert registered == [instance._on_unattended_changed]

    def test_on_unattended_changed_marshals_rebuild_through_app_helper(self, app, monkeypatch):
        # Rather than asserting on the rendered menu (rumps has no headless
        # render target worth inspecting -- see this module's docstring),
        # confirm the rebuild is handed to AppHelper.callAfter rather than
        # invoked directly, which is what protects the main thread from a
        # callback fired off ipc_server.py's own asyncio thread.
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append(f))

        app._on_unattended_changed()

        assert recorded == [app._rebuild]


class TestMiscActions:
    def test_open_audit_log_opens_existing_dir(self, app, monkeypatch, tmp_path):
        log_dir = tmp_path / "logs" / "audit"
        log_dir.mkdir(parents=True)
        monkeypatch.setattr(menu_bar, "data_dir", lambda: tmp_path)
        run_calls = []
        monkeypatch.setattr(menu_bar.subprocess, "run", lambda *a, **k: run_calls.append(a))

        app.open_audit_log()

        assert run_calls == [(["open", str(log_dir)],)]

    def test_open_audit_log_missing_dir_alerts_instead(self, app, monkeypatch, tmp_path):
        monkeypatch.setattr(menu_bar, "data_dir", lambda: tmp_path)
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: alerts.append((a, k)))

        app.open_audit_log()

        assert len(alerts) == 1

    def test_open_audit_log_refreshes_current_week_excel_and_opens_it(self, app, monkeypatch, tmp_path):
        from privacyfence.audit_log import AuditEntry

        log_dir = tmp_path / "logs" / "audit"
        log_dir.mkdir(parents=True)
        monkeypatch.setattr(menu_bar, "data_dir", lambda: tmp_path)

        week = menu_bar.current_week()
        entry = AuditEntry(
            timestamp="2026-07-06T12:00:00+00:00", week=week, request_id="",
            connector="gmail", tool="gmail_get_message", tool_name="Read Gmail message",
            summary="s", sender="a@x.com", decision="approved", auto_accept_rule="", latency_seconds=1.0,
        )
        menu_bar.AuditLogger(str(log_dir)).record(entry)

        run_calls = []
        monkeypatch.setattr(menu_bar.subprocess, "run", lambda *a, **k: run_calls.append(a))

        app.open_audit_log()

        expected_xlsx = log_dir / f"{week}.xlsx"
        assert expected_xlsx.exists()
        assert run_calls == [(["open", str(expected_xlsx)],)]

    def test_open_audit_log_falls_back_to_folder_when_nothing_logged_this_week(self, app, monkeypatch, tmp_path):
        log_dir = tmp_path / "logs" / "audit"
        log_dir.mkdir(parents=True)
        monkeypatch.setattr(menu_bar, "data_dir", lambda: tmp_path)
        run_calls = []
        monkeypatch.setattr(menu_bar.subprocess, "run", lambda *a, **k: run_calls.append(a))

        app.open_audit_log()

        assert run_calls == [(["open", str(log_dir)],)]

    def test_open_audit_log_falls_back_to_folder_when_export_returns_none(self, app, monkeypatch, tmp_path):
        # e.g. openpyxl not installed -- export_week_to_excel returns None.
        log_dir = tmp_path / "logs" / "audit"
        log_dir.mkdir(parents=True)
        monkeypatch.setattr(menu_bar, "data_dir", lambda: tmp_path)
        week = menu_bar.current_week()
        (log_dir / f"{week}.jsonl").write_text("")
        monkeypatch.setattr(menu_bar.AuditLogger, "export_week_to_excel", lambda self, w: None)
        run_calls = []
        monkeypatch.setattr(menu_bar.subprocess, "run", lambda *a, **k: run_calls.append(a))

        app.open_audit_log()

        assert run_calls == [(["open", str(log_dir)],)]

    def test_show_about_opens_github_on_first_button(self, app, monkeypatch):
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda **kw: 1)
        run_calls = []
        monkeypatch.setattr(menu_bar.subprocess, "run", lambda *a, **k: run_calls.append(a))

        app.show_about()

        assert run_calls == [([ "open", menu_bar.REPO_URL],)]

    def test_show_about_does_not_open_github_on_close(self, app, monkeypatch):
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda **kw: 0)
        run_calls = []
        monkeypatch.setattr(menu_bar.subprocess, "run", lambda *a, **k: run_calls.append(a))

        app.show_about()

        assert run_calls == []

    def test_quit_app_calls_rumps_quit(self, app, monkeypatch):
        calls = []
        monkeypatch.setattr(menu_bar.rumps, "quit_application", lambda: calls.append(1))

        app.quit_app()

        assert calls == [1]


class TestAuthenticateFailureBranches:
    """The three OAuth flows (Slack, Salesforce, Atlassian) each alert + fall
    back to a plain rebuild (not _refresh_connectors) on failure -- same
    contract as Google's, covered separately above."""

    def test_slack_failure_alerts_and_rebuilds(self, app, monkeypatch):
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append((f, a, k)))
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: alerts.append((a, k)))
        rebuild_calls = []
        monkeypatch.setattr(app, "_rebuild", lambda: rebuild_calls.append(1))
        refresh_calls = []
        monkeypatch.setattr(app, "_refresh_connectors", lambda: refresh_calls.append(1))

        def raiser(**kw):
            raise RuntimeError("bad redirect")
        monkeypatch.setattr(menu_bar, "slack_authorize_interactive", raiser)

        app._authenticate_slack({"slack": {"client_id": "id"}})

        assert wait_until(lambda: recorded)
        func, args, kwargs = recorded[0]
        func(*args, **kwargs)

        assert len(alerts) == 1
        assert rebuild_calls == [1]
        assert refresh_calls == []

    def test_salesforce_failure_alerts_and_rebuilds(self, app, monkeypatch):
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append((f, a, k)))
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: alerts.append((a, k)))
        rebuild_calls = []
        monkeypatch.setattr(app, "_rebuild", lambda: rebuild_calls.append(1))
        refresh_calls = []
        monkeypatch.setattr(app, "_refresh_connectors", lambda: refresh_calls.append(1))

        def raiser(**kw):
            raise RuntimeError("bad redirect")
        monkeypatch.setattr(menu_bar, "salesforce_authorize_interactive", raiser)

        app._authenticate_salesforce({"salesforce": {"consumer_key": "ck"}})

        assert wait_until(lambda: recorded)
        func, args, kwargs = recorded[0]
        func(*args, **kwargs)

        assert len(alerts) == 1
        assert rebuild_calls == [1]
        assert refresh_calls == []

    def test_atlassian_failure_alerts_and_rebuilds(self, app, monkeypatch):
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append((f, a, k)))
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: alerts.append((a, k)))
        rebuild_calls = []
        monkeypatch.setattr(app, "_rebuild", lambda: rebuild_calls.append(1))
        refresh_calls = []
        monkeypatch.setattr(app, "_refresh_connectors", lambda: refresh_calls.append(1))

        def raiser(**kw):
            raise RuntimeError("bad redirect")
        monkeypatch.setattr(menu_bar, "atlassian_authorize_interactive", raiser)

        app._authenticate_atlassian({"atlassian": {"client_id": "ci"}})

        assert wait_until(lambda: recorded)
        func, args, kwargs = recorded[0]
        func(*args, **kwargs)

        assert len(alerts) == 1
        assert rebuild_calls == [1]
        assert refresh_calls == []


class TestConfigSaveErrorSwallowed:
    def test_save_config_write_failure_is_logged_not_raised(self, app, monkeypatch):
        def raise_open(*a, **kw):
            raise OSError("disk full")
        monkeypatch.setattr(menu_bar, "open", raise_open, raising=False)

        app._save_config({"a": 1})  # must not raise


class TestInstallOrgConfigWriteFailure:
    def test_write_failure_alerts_instead_of_raising(self, app, monkeypatch, tmp_path):
        src = tmp_path / "bundle.json"
        src.write_text(json.dumps({"version": 1}), encoding="utf-8")
        monkeypatch.setattr(menu_bar.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=str(src)))
        # org_dir() returning a path whose directory doesn't exist makes the
        # write raise OSError (no such file or directory).
        monkeypatch.setattr(menu_bar, "org_dir", lambda: tmp_path / "nonexistent_subdir")
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: alerts.append((a, k)))

        app._install_org_config()  # must not raise

        assert len(alerts) == 1
        assert "Could not install" in str(alerts[0])


class _FakeTelegramClient:
    """Stand-in for telethon.TelegramClient. Class-level scenario knobs are
    set per test before _authenticate_telegram's flow() constructs instances
    of it (it always constructs a fresh client per phase, matching the real
    module's per-phase-reconnect design)."""

    needs_2fa = False
    sign_in_error: Exception | None = None
    me_first_name = "Jane"
    me_last_name = "Doe"

    def __init__(self, session_file, api_id, api_hash):
        self.session_file, self.api_id, self.api_hash = session_file, api_id, api_hash

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def send_code_request(self, phone):
        return SimpleNamespace(phone_code_hash="hash-123")

    async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
        if password is None and type(self).needs_2fa:
            from telethon.errors import SessionPasswordNeededError
            raise SessionPasswordNeededError(request=None)
        if type(self).sign_in_error is not None:
            raise type(self).sign_in_error

    async def get_me(self):
        return SimpleNamespace(first_name=type(self).me_first_name, last_name=type(self).me_last_name)


@pytest.fixture
def telegram_ready(monkeypatch):
    """Common setup for _authenticate_telegram tests: valid app credentials
    and the real telethon.TelegramClient replaced with our fake."""
    monkeypatch.setattr(menu_bar, "telegram_app_credentials", lambda: (123, "hash"))
    monkeypatch.setattr("telethon.TelegramClient", _FakeTelegramClient)
    _FakeTelegramClient.needs_2fa = False
    _FakeTelegramClient.sign_in_error = None
    yield
    _FakeTelegramClient.needs_2fa = False
    _FakeTelegramClient.sign_in_error = None


def _drain_run_async(recorded) -> None:
    """Drain every AppHelper.callAfter hop recorded so far, following newly
    queued ones (as a pumped main run loop would) until none remain."""
    while recorded:
        func, args, kwargs = recorded.pop(0)
        func(*args, **kwargs)


class TestAuthenticateTelegram:
    def test_missing_credentials_alerts_without_running_flow(self, app, monkeypatch):
        monkeypatch.setattr(menu_bar, "telegram_app_credentials", lambda: None)
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: alerts.append((a, k)))
        run_async_calls = []
        monkeypatch.setattr(app, "_run_async", lambda *a: run_async_calls.append(a))

        app._authenticate_telegram()

        assert len(alerts) == 1
        assert run_async_calls == []

    def test_happy_path_without_2fa_connects_and_refreshes(self, app, monkeypatch, telegram_ready):
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append((f, a, k)))
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: alerts.append((a, k)))
        refresh_calls = []
        monkeypatch.setattr(app, "_refresh_connectors", lambda: refresh_calls.append(1))

        prompts = iter([(True, "+123456789"), (True, "12345")])
        monkeypatch.setattr(app, "_prompt", lambda **kw: next(prompts))

        app._authenticate_telegram()

        assert wait_until(lambda: recorded, timeout=5)
        _drain_run_async(recorded)

        assert any("Jane Doe" in str(a) for a in alerts)
        assert refresh_calls == [1]

    def test_cancelling_at_phone_prompt_is_silently_ignored(self, app, monkeypatch, telegram_ready):
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append((f, a, k)))
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: alerts.append((a, k)))
        rebuild_calls = []
        monkeypatch.setattr(app, "_rebuild", lambda: rebuild_calls.append(1))
        monkeypatch.setattr(app, "_prompt", lambda **kw: (False, ""))

        app._authenticate_telegram()

        assert wait_until(lambda: recorded, timeout=5)
        _drain_run_async(recorded)

        assert alerts == []
        assert rebuild_calls == []

    def test_cancelling_at_code_prompt_is_silently_ignored(self, app, monkeypatch, telegram_ready):
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append((f, a, k)))
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: alerts.append((a, k)))

        prompts = iter([(True, "+123456789"), (False, "")])
        monkeypatch.setattr(app, "_prompt", lambda **kw: next(prompts))

        app._authenticate_telegram()

        assert wait_until(lambda: recorded, timeout=5)
        _drain_run_async(recorded)

        assert alerts == []

    def test_2fa_required_prompts_for_password_and_succeeds(self, app, monkeypatch, telegram_ready):
        _FakeTelegramClient.needs_2fa = True
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append((f, a, k)))
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: alerts.append((a, k)))
        refresh_calls = []
        monkeypatch.setattr(app, "_refresh_connectors", lambda: refresh_calls.append(1))

        prompts = iter([(True, "+123456789"), (True, "12345"), (True, "my-2fa-password")])
        monkeypatch.setattr(app, "_prompt", lambda **kw: next(prompts))

        app._authenticate_telegram()

        assert wait_until(lambda: recorded, timeout=5)
        _drain_run_async(recorded)

        assert any("Jane Doe" in str(a) for a in alerts)
        assert refresh_calls == [1]

    def test_cancelling_at_2fa_password_prompt_is_silently_ignored(self, app, monkeypatch, telegram_ready):
        _FakeTelegramClient.needs_2fa = True
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append((f, a, k)))
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: alerts.append((a, k)))

        prompts = iter([(True, "+123456789"), (True, "12345"), (False, "")])
        monkeypatch.setattr(app, "_prompt", lambda **kw: next(prompts))

        app._authenticate_telegram()

        assert wait_until(lambda: recorded, timeout=5)
        _drain_run_async(recorded)

        assert alerts == []

    def test_sign_in_failure_alerts_and_rebuilds(self, app, monkeypatch, telegram_ready):
        _FakeTelegramClient.sign_in_error = RuntimeError("invalid code")
        recorded = []
        monkeypatch.setattr(menu_bar.AppHelper, "callAfter", lambda f, *a, **k: recorded.append((f, a, k)))
        alerts = []
        monkeypatch.setattr(menu_bar.rumps, "alert", lambda *a, **k: alerts.append((a, k)))
        rebuild_calls = []
        monkeypatch.setattr(app, "_rebuild", lambda: rebuild_calls.append(1))
        refresh_calls = []
        monkeypatch.setattr(app, "_refresh_connectors", lambda: refresh_calls.append(1))

        prompts = iter([(True, "+123456789"), (True, "12345")])
        monkeypatch.setattr(app, "_prompt", lambda **kw: next(prompts))

        app._authenticate_telegram()

        assert wait_until(lambda: recorded, timeout=5)
        _drain_run_async(recorded)

        assert any("invalid code" in str(a) for a in alerts)
        assert rebuild_calls == [1]
        assert refresh_calls == []
