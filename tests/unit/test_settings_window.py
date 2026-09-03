"""Construction-level tests for SettingsWindowController -- the native
window hosting the settings webview (settings_window.py).

Same pattern as test_approval_window.py: calls build_window() directly and
inspects the resulting real AppKit/WebKit view tree, never show_window()'s
makeKeyAndOrderFront_/activateIgnoringOtherApps_ and never anything modal
(this window isn't modal at all, unlike ApprovalWindowController's
runApproval_, but build_window() still keeps construction free of any
window-server side effect, so this can run in CI on macos-latest without a
real interactive session).
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="requires real AppKit/PyObjC/WebKit (macOS only, matches project's macOS-only runtime)"
)


def _make_controller(tmp_path, monkeypatch):
    from privacyfence import daemon_main, settings_controller as sc

    monkeypatch.setattr(daemon_main, "load_org_config", lambda: {})
    config_path = tmp_path / "settings.yaml"
    config_path.write_text("auto_accept_rules: {}\nconnectors: {}\n", encoding="utf-8")
    org_dir_path = tmp_path / "org"
    org_dir_path.mkdir()
    monkeypatch.setattr(sc, "org_dir", lambda: org_dir_path)
    data_dir_path = tmp_path / "data"
    data_dir_path.mkdir()
    monkeypatch.setattr(sc, "data_dir", lambda: data_dir_path)

    ipc_server = SimpleNamespace(set_connectors=lambda c: None, set_unattended_changed_listener=lambda cb: None)
    return sc.SettingsController(str(config_path), connectors=[], ipc_server=ipc_server)


class TestBuildWindow:
    def test_builds_a_titled_closable_miniaturizable_window(self, tmp_path, monkeypatch):
        from AppKit import NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskTitled
        from privacyfence.settings_window import SettingsWindowController, _WINDOW_HEIGHT, _WINDOW_WIDTH

        controller = _make_controller(tmp_path, monkeypatch)
        wc = SettingsWindowController.alloc().init()
        wc.configure(controller)

        window = wc.build_window()

        assert window.title() == "PrivacyFence Settings"
        mask = window.styleMask()
        assert mask & NSWindowStyleMaskTitled
        assert mask & NSWindowStyleMaskClosable
        assert mask & NSWindowStyleMaskMiniaturizable
        assert window.frame().size.width == _WINDOW_WIDTH
        assert window.frame().size.height == _WINDOW_HEIGHT
        assert window.isReleasedWhenClosed() is False

    def test_webview_fills_the_content_area(self, tmp_path, monkeypatch):
        from WebKit import WKWebView
        from privacyfence.settings_window import SettingsWindowController

        controller = _make_controller(tmp_path, monkeypatch)
        wc = SettingsWindowController.alloc().init()
        wc.configure(controller)

        window = wc.build_window()

        assert isinstance(window.contentView(), WKWebView)
        assert wc._webview is window.contentView()

    def test_registers_the_pf_message_handler(self, tmp_path, monkeypatch):
        from privacyfence.settings_window import SettingsWindowController, _MESSAGE_HANDLER_NAME

        controller = _make_controller(tmp_path, monkeypatch)
        wc = SettingsWindowController.alloc().init()
        wc.configure(controller)

        wc.build_window()

        assert wc._user_content_controller is not None
        assert _MESSAGE_HANDLER_NAME == "pf"

    def test_controller_on_change_wired_to_push_state(self, tmp_path, monkeypatch):
        from privacyfence.settings_window import SettingsWindowController

        controller = _make_controller(tmp_path, monkeypatch)
        wc = SettingsWindowController.alloc().init()
        wc.configure(controller)

        assert controller.on_change == wc._push_state


class TestWindowWillClose:
    def test_drops_references_and_removes_the_message_handler(self, tmp_path, monkeypatch):
        from privacyfence.settings_window import SettingsWindowController, _MESSAGE_HANDLER_NAME

        controller = _make_controller(tmp_path, monkeypatch)
        wc = SettingsWindowController.alloc().init()
        wc.configure(controller)
        wc.build_window()

        # A real WKUserContentController is a bridged Objective-C instance --
        # its methods aren't per-instance overridable Python attributes, so
        # swap in a plain mock rather than trying to patch the native object.
        mock_ucc = Mock()
        wc._user_content_controller = mock_ucc

        wc.windowWillClose_(None)

        mock_ucc.removeScriptMessageHandlerForName_.assert_called_once_with(_MESSAGE_HANDLER_NAME)
        assert wc.window is None
        assert wc._webview is None
        assert wc._user_content_controller is None


class TestDispatch:
    def test_known_action_calls_the_matching_controller_method(self, tmp_path, monkeypatch):
        from privacyfence.settings_window import SettingsWindowController

        controller = _make_controller(tmp_path, monkeypatch)
        calls = []
        controller.toggle_pii_detection = lambda: calls.append(1) or controller.snapshot()
        wc = SettingsWindowController.alloc().init()
        wc.configure(controller)
        wc.build_window()

        wc._dispatch("toggle_pii_detection", {})

        assert calls == [1]

    def test_idx_payload_is_coerced_to_int(self, tmp_path, monkeypatch):
        from privacyfence.settings_window import SettingsWindowController

        controller = _make_controller(tmp_path, monkeypatch)
        captured = {}

        def fake_remove(op_key, idx):
            captured["op_key"] = op_key
            captured["idx"] = idx
            captured["idx_type"] = type(idx)
            return controller.snapshot()

        controller.remove_rule_row = fake_remove
        wc = SettingsWindowController.alloc().init()
        wc.configure(controller)
        wc.build_window()

        wc._dispatch("remove_rule_row", {"op_key": "gmail.read_message", "idx": 2.0})

        assert captured["idx"] == 2
        assert captured["idx_type"] is int

    def test_string_payload_values_are_coerced_to_plain_str(self, tmp_path, monkeypatch):
        """message.body() bridges JS strings as objc.pyobjc_unicode (a str
        subclass), not plain str. A str subclass sitting inside a
        toggle_grant_capability-style call ends up as a dict key that later
        gets persisted to settings.yaml via yaml.dump/safe_dump -- neither
        has a representer for the subclass, so it either errors out (safe_dump)
        or writes a !!python/object/apply:builtins.str tag that yaml.safe_load
        can't read back on the next launch (dump). _dispatch must hand
        plain str to the controller so that never happens."""
        from privacyfence.settings_window import SettingsWindowController

        class _PseudoStr(str):
            pass

        controller = _make_controller(tmp_path, monkeypatch)
        captured = {}

        def fake_toggle(connector, config_key, idx, cap):
            captured["cap"] = cap
            captured["cap_type"] = type(cap)
            return controller.snapshot()

        controller.toggle_grant_capability = fake_toggle
        wc = SettingsWindowController.alloc().init()
        wc.configure(controller)
        wc.build_window()

        wc._dispatch(
            "toggle_grant_capability",
            {
                "connector": _PseudoStr("drive"),
                "config_key": _PseudoStr("folders"),
                "idx": 0,
                "cap": _PseudoStr("read"),
            },
        )

        assert captured["cap"] == "read"
        assert captured["cap_type"] is str

    def test_quit_app_dispatches_to_the_controller_without_a_state_push(self, tmp_path, monkeypatch):
        from privacyfence.settings_window import SettingsWindowController

        controller = _make_controller(tmp_path, monkeypatch)
        calls = []
        controller.quit_app = lambda: calls.append(1)
        wc = SettingsWindowController.alloc().init()
        wc.configure(controller)
        wc.build_window()
        pushed = []
        monkeypatch.setattr(wc, "_push_state", lambda state: pushed.append(state))

        wc._dispatch("quit_app", {})

        assert calls == [1]
        assert pushed == []

    def test_open_repo_shells_out_to_open(self, tmp_path, monkeypatch):
        import privacyfence.settings_window as sw

        controller = _make_controller(tmp_path, monkeypatch)
        wc = sw.SettingsWindowController.alloc().init()
        wc.configure(controller)
        wc.build_window()
        run_calls = []
        monkeypatch.setattr(sw.subprocess, "run", lambda args, **kw: run_calls.append(args))

        wc._dispatch("open_repo", {})

        assert run_calls == [["open", sw.REPO_URL]]

    def test_reveal_mcp_token_routes_to_its_own_js_callback_not_pfrender(self, tmp_path, monkeypatch):
        # P4c (docs/https-connector-refactor-plan.md §16.9): the result is
        # a bare {"mcp_token": ...}, not a snapshot, so it must never reach
        # _push_state()/window.__pfRender() -- that would wipe every other
        # rendered section of the page.
        from privacyfence.settings_window import SettingsWindowController

        controller = _make_controller(tmp_path, monkeypatch)
        controller.set_mcp_connection_info(url="http://localhost:8765/mcp", token="super-secret-token")
        wc = SettingsWindowController.alloc().init()
        wc.configure(controller)
        wc.build_window()
        pushed = []
        monkeypatch.setattr(wc, "_push_state", lambda state: pushed.append(state))
        captured = {}
        wc._webview = Mock()
        wc._webview.evaluateJavaScript_completionHandler_.side_effect = (
            lambda js, cb: captured.setdefault("js", js)
        )

        wc._dispatch("reveal_mcp_token", {})

        assert pushed == []
        assert "window.__pfRevealMcpToken" in captured["js"]
        assert "super-secret-token" in captured["js"]
        assert "window.__pfRender" not in captured["js"]

    def test_unknown_action_is_logged_and_ignored(self, tmp_path, monkeypatch):
        from privacyfence.settings_window import SettingsWindowController

        controller = _make_controller(tmp_path, monkeypatch)
        wc = SettingsWindowController.alloc().init()
        wc.configure(controller)
        wc.build_window()
        pushed = []
        monkeypatch.setattr(wc, "_push_state", lambda state: pushed.append(state))

        wc._dispatch("not_a_real_action", {})  # must not raise

        assert pushed == []


class TestPushState:
    def test_augments_connector_icons_before_evaluating_js(self, tmp_path, monkeypatch):
        from privacyfence.settings_window import SettingsWindowController

        controller = _make_controller(tmp_path, monkeypatch)
        wc = SettingsWindowController.alloc().init()
        wc.configure(controller)
        wc.build_window()

        # A real WKWebView is a bridged Objective-C instance -- its methods
        # aren't per-instance overridable Python attributes, so swap in a
        # plain mock rather than trying to patch the native object.
        captured = {}
        wc._webview = Mock()
        wc._webview.evaluateJavaScript_completionHandler_.side_effect = (
            lambda js, cb: captured.setdefault("js", js)
        )

        state = controller.snapshot()
        wc._push_state(state)

        assert "window.__pfRender(" in captured["js"]
        assert "icon_data_uri" in captured["js"]

    def test_no_op_before_a_window_exists(self, tmp_path, monkeypatch):
        from privacyfence.settings_window import SettingsWindowController

        controller = _make_controller(tmp_path, monkeypatch)
        wc = SettingsWindowController.alloc().init()
        wc.configure(controller)

        wc._push_state(controller.snapshot())  # must not raise
