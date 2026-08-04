"""privacyfence.menu_bar -- the two-item tray icon (issue #120).

Everything menu_bar.py used to own directly (rule/grant/PII/privacy/
connector/audit/org-config mutation, and the crash-avoidance machinery
around mutating a live NSMenu while it's open) moved to
settings_controller.py/settings_window.py -- see test_settings_controller.py
and test_settings_window.py for that coverage. What's left here is just:
construction wires a SettingsController + lazy SettingsWindowController, and
the two menu items ("Settings…"/"Quit PrivacyFence") dispatch to
the right place.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from privacyfence import menu_bar


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(menu_bar, "_find_icon", lambda: None)
    # SettingsController.__init__ registers a rules-changed listener and
    # (if given one) an unattended-changed listener on the ipc_server --
    # neither must explode during construction.
    ipc_server = SimpleNamespace(
        set_connectors=lambda conns: None,
        set_unattended_changed_listener=lambda callback: None,
    )
    config_path = tmp_path / "settings.yaml"
    config_path.write_text("auto_accept_rules: {}\nconnectors: {}\n", encoding="utf-8")

    instance = menu_bar.PrivacyFenceMenuBar(str(config_path), connectors=[], ipc_server=ipc_server)
    return instance


class TestFindIcon:
    def test_returns_first_existing_candidate(self, monkeypatch, tmp_path):
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


class TestConstruction:
    def test_builds_a_two_item_menu(self, app):
        titles = [item.title for item in app.menu.values() if hasattr(item, "title")]
        assert titles == ["Settings…", "Quit PrivacyFence"]

    def test_holds_a_settings_controller(self, app):
        from privacyfence.settings_controller import SettingsController

        assert isinstance(app.controller, SettingsController)

    def test_settings_window_not_created_until_opened(self, app):
        assert app._settings_window is None

    def test_starts_the_update_check_timer(self, app):
        assert app._update_check_timer is not None

    def test_fires_an_initial_update_check_pulse(self, tmp_path, monkeypatch):
        # __init__ calls _on_update_check_timer() once immediately, which
        # must reach the controller (not silently no-op) -- verified by
        # intercepting the controller method rather than letting a real
        # network call happen.
        monkeypatch.setattr(menu_bar, "_find_icon", lambda: None)
        ipc_server = SimpleNamespace(set_connectors=lambda c: None, set_unattended_changed_listener=lambda cb: None)
        config_path = tmp_path / "settings.yaml"
        config_path.write_text("auto_accept_rules: {}\nconnectors: {}\n", encoding="utf-8")

        calls = []
        from privacyfence.settings_controller import SettingsController
        monkeypatch.setattr(SettingsController, "on_update_check_timer", lambda self: calls.append(1))

        menu_bar.PrivacyFenceMenuBar(str(config_path), connectors=[], ipc_server=ipc_server)

        assert calls == [1]


class TestOpenSettingsWindow:
    def test_lazily_creates_and_shows_the_window(self, app, monkeypatch):
        created = []

        class _FakeWindowController:
            def configure(self, controller):
                created.append(controller)
                self.controller = controller

            def show_window(self):
                created.append("shown")

        monkeypatch.setattr(menu_bar, "SettingsWindowController", SimpleNamespace(alloc=lambda: SimpleNamespace(init=lambda: _FakeWindowController())))

        assert app._settings_window is None
        app._open_settings_window()

        assert app._settings_window is not None
        assert created == [app.controller, "shown"]

    def test_reopening_reuses_the_same_controller(self, app, monkeypatch):
        class _FakeWindowController:
            def __init__(self):
                self.show_calls = 0

            def configure(self, controller):
                self.controller = controller

            def show_window(self):
                self.show_calls += 1

        monkeypatch.setattr(menu_bar, "SettingsWindowController", SimpleNamespace(alloc=lambda: SimpleNamespace(init=lambda: _FakeWindowController())))

        app._open_settings_window()
        first = app._settings_window
        app._open_settings_window()

        assert app._settings_window is first
        assert first.show_calls == 2


class TestQuit:
    def test_quit_delegates_to_the_controller(self, app, monkeypatch):
        calls = []
        monkeypatch.setattr(app.controller, "quit_app", lambda: calls.append(1))

        app._quit()

        assert calls == [1]
