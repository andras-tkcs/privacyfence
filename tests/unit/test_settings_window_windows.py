"""Tests for settings_window_windows.py: the pywebview-hosted settings
window (issue #121), same contract as test_settings_window.py's coverage of
the AppKit one. Same fake-webview approach as test_approval_window_windows.py
-- see that file's own module docstring for why (no real pywebview in this
sandbox).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from privacyfence import settings_window_windows as sww
from privacyfence.settings_controller import SettingsController


class _FakeEventSlot:
    def __init__(self):
        self.callbacks: list = []

    def __iadd__(self, callback):
        self.callbacks.append(callback)
        return self


class _FakeWindow:
    def __init__(self, file_dialog_result=None):
        self.events = type("Events", (), {})()
        self.events.closed = _FakeEventSlot()
        self.evaluate_js_calls: list[str] = []
        self.restored = False
        self._file_dialog_result = file_dialog_result

    def evaluate_js(self, js: str) -> None:
        self.evaluate_js_calls.append(js)

    def restore(self) -> None:
        self.restored = True

    def create_file_dialog(self, dialog_type, file_types=()):
        return self._file_dialog_result


class _FakeWebview:
    OPEN_DIALOG = "open"

    def __init__(self, file_dialog_result=None):
        self.file_dialog_result = file_dialog_result
        self.create_window_calls: list[dict] = []
        self.last_window: _FakeWindow | None = None

    def create_window(self, **kwargs):
        self.create_window_calls.append(kwargs)
        window = _FakeWindow(file_dialog_result=self.file_dialog_result)
        self.last_window = window
        return window


@pytest.fixture
def controller(tmp_path, monkeypatch):
    config_path = tmp_path / "settings.yaml"
    config_path.write_text("auto_accept_rules: {}\nconnectors: {}\n", encoding="utf-8")
    ipc_server = SimpleNamespace(
        set_connectors=lambda conns: None,
        set_unattended_changed_listener=lambda callback: None,
    )
    return SettingsController(str(config_path), connectors=[], ipc_server=ipc_server)


@pytest.fixture
def wc(monkeypatch, controller):
    monkeypatch.setattr(sww, "webview", _FakeWebview())
    controller_window = sww.SettingsWindowController()
    controller_window.configure(controller)
    return controller_window


class TestConfigure:
    def test_wires_on_change_and_file_picker_hook(self, wc, controller):
        assert controller.on_change == wc._push_state
        assert controller.pick_org_config_file_hook == wc._pick_org_config_file


class TestShowWindow:
    def test_builds_and_shows_a_window_on_first_call(self, monkeypatch, controller):
        fake = _FakeWebview()
        monkeypatch.setattr(sww, "webview", fake)
        wc = sww.SettingsWindowController()
        wc.configure(controller)

        wc.show_window()

        assert len(fake.create_window_calls) == 1
        assert wc.window is not None

    def test_reopening_reuses_the_existing_window(self, monkeypatch, controller):
        fake = _FakeWebview()
        monkeypatch.setattr(sww, "webview", fake)
        wc = sww.SettingsWindowController()
        wc.configure(controller)

        wc.show_window()
        wc.show_window()

        assert len(fake.create_window_calls) == 1
        assert fake.last_window.restored is True

    def test_missing_pywebview_does_not_raise(self, monkeypatch, controller):
        monkeypatch.setattr(sww, "webview", None)
        wc = sww.SettingsWindowController()
        wc.configure(controller)

        wc.show_window()  # must not raise

        assert wc.window is None

    def test_closing_the_window_clears_the_reference(self, monkeypatch, controller):
        fake = _FakeWebview()
        monkeypatch.setattr(sww, "webview", fake)
        wc = sww.SettingsWindowController()
        wc.configure(controller)
        wc.show_window()

        for callback in fake.last_window.events.closed.callbacks:
            callback()

        assert wc.window is None


class TestDispatch:
    def test_quit_app_delegates_to_the_controller(self, wc, monkeypatch):
        calls = []
        monkeypatch.setattr(wc.controller, "quit_app", lambda: calls.append(1))

        wc._dispatch("quit_app", {})

        assert calls == [1]

    def test_open_repo_opens_the_repo_url(self, wc, monkeypatch):
        opened = []
        monkeypatch.setattr(sww, "open_path_or_url", lambda target: opened.append(target))

        wc._dispatch("open_repo", {})

        assert opened == [sww.REPO_URL]

    def test_unknown_action_is_logged_and_ignored(self, wc):
        wc._dispatch("not_a_real_action", {})  # must not raise

    def test_dict_result_from_a_controller_method_pushes_state(self, wc, monkeypatch):
        wc.show_window()
        pushed = []
        monkeypatch.setattr(wc, "_push_state", lambda state: pushed.append(state))
        monkeypatch.setattr(wc.controller, "toggle_pii_detection", lambda: {"pii_enabled": False})

        wc._dispatch("toggle_pii_detection", {})

        assert pushed == [{"pii_enabled": False}]


class TestOnMessage:
    def test_pops_action_and_dispatches(self, wc, monkeypatch):
        captured = []
        monkeypatch.setattr(wc, "_dispatch", lambda action, payload: captured.append((action, payload)))

        wc._on_message({"action": "toggle_pii_detection", "extra": "value"})

        assert captured == [("toggle_pii_detection", {"extra": "value"})]

    def test_missing_action_is_ignored(self, wc, monkeypatch):
        captured = []
        monkeypatch.setattr(wc, "_dispatch", lambda action, payload: captured.append(1))

        wc._on_message({})

        assert captured == []


class TestPushState:
    def test_no_op_when_no_window_exists(self, wc):
        wc._push_state({"anything": 1})  # must not raise

    def test_evaluates_js_with_the_state_once_a_window_exists(self, wc):
        wc.show_window()

        wc._push_state({"connectors": []})

        assert len(wc.window.evaluate_js_calls) == 1
        assert "window.__pfRender" in wc.window.evaluate_js_calls[0]


class TestPickOrgConfigFile:
    def test_returns_empty_string_without_a_window(self, wc):
        assert wc._pick_org_config_file() == ""

    def test_returns_the_first_selected_path(self, monkeypatch, controller):
        fake = _FakeWebview(file_dialog_result=("/path/to/org_config.json",))
        monkeypatch.setattr(sww, "webview", fake)
        wc = sww.SettingsWindowController()
        wc.configure(controller)
        wc.show_window()

        assert wc._pick_org_config_file() == "/path/to/org_config.json"

    def test_returns_empty_string_when_the_dialog_is_cancelled(self, monkeypatch, controller):
        fake = _FakeWebview(file_dialog_result=None)
        monkeypatch.setattr(sww, "webview", fake)
        wc = sww.SettingsWindowController()
        wc.configure(controller)
        wc.show_window()

        assert wc._pick_org_config_file() == ""
