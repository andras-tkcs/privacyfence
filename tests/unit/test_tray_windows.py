"""Tests for tray_windows.py: the pystray-based tray icon (issue #121), same
role as test_menu_bar.py's coverage of the rumps one -- two menu items
("Settings…"/"Quit PrivacyFence"), lazily-created settings window, holds one
SettingsController for the app's whole lifetime. Same fake-dependency
approach as the other Windows backend tests -- pystray/pywebview/Pillow
aren't installed in this sandbox (Windows-only per pyproject.toml), so every
test monkeypatches tray_windows.pystray/Image/webview with small fakes.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from privacyfence import tray_windows


class _FakeMenuItem:
    def __init__(self, label, callback):
        self.label = label
        self.callback = callback


class _FakeMenu:
    SEPARATOR = "---"

    def __init__(self, *items):
        self.items = items


class _FakeIcon:
    def __init__(self, name, image, title, menu):
        self.name = name
        self.image = image
        self.title = title
        self.menu = menu
        self.ran_detached = False
        self.stopped = False

    def run_detached(self):
        self.ran_detached = True

    def stop(self):
        self.stopped = True


class _FakePystray:
    Menu = _FakeMenu
    MenuItem = _FakeMenuItem
    Icon = _FakeIcon


class _FakeImage:
    @staticmethod
    def open(path):
        return f"image:{path}"

    @staticmethod
    def new(mode, size, color):
        return f"blank:{mode}:{size}:{color}"


class _FakeWebview:
    def __init__(self):
        self.started = False
        self.destroyed = False

    def start(self):
        self.started = True

    def destroy_window(self):
        self.destroyed = True


@pytest.fixture
def ipc_server():
    return SimpleNamespace(
        set_connectors=lambda conns: None,
        set_unattended_changed_listener=lambda callback: None,
    )


@pytest.fixture
def tray(tmp_path, monkeypatch, ipc_server):
    monkeypatch.setattr(tray_windows, "pystray", _FakePystray())
    monkeypatch.setattr(tray_windows, "Image", _FakeImage())
    monkeypatch.setattr(tray_windows, "webview", _FakeWebview())
    monkeypatch.setattr(tray_windows, "_find_icon", lambda: None)

    config_path = tmp_path / "settings.yaml"
    config_path.write_text("auto_accept_rules: {}\nconnectors: {}\n", encoding="utf-8")

    return tray_windows.PrivacyFenceTray(str(config_path), connectors=[], ipc_server=ipc_server)


class TestConstruction:
    def test_holds_a_settings_controller(self, tray):
        from privacyfence.settings_controller import SettingsController
        assert isinstance(tray.controller, SettingsController)

    def test_settings_window_not_created_until_opened(self, tray):
        assert tray._settings_window is None


class TestBuildIcon:
    def test_builds_a_two_item_menu_with_a_separator(self, tray):
        icon = tray._build_icon()

        assert [item.label for item in icon.menu.items if isinstance(item, _FakeMenuItem)] == [
            "Settings…", "Quit PrivacyFence",
        ]

    def test_falls_back_to_a_blank_image_when_no_icon_file_is_found(self, tray):
        icon = tray._build_icon()
        assert icon.image.startswith("blank:")


class TestOpenSettingsWindow:
    def test_lazily_creates_and_shows_the_window(self, tray, monkeypatch):
        created = []

        class _FakeWindowController:
            def configure(self, controller):
                created.append(controller)

            def show_window(self):
                created.append("shown")

        monkeypatch.setattr(tray_windows, "SettingsWindowController", _FakeWindowController)

        assert tray._settings_window is None
        tray._open_settings_window()

        assert tray._settings_window is not None
        assert created == [tray.controller, "shown"]

    def test_reopening_reuses_the_same_controller(self, tray, monkeypatch):
        class _FakeWindowController:
            def __init__(self):
                self.show_calls = 0

            def configure(self, controller):
                pass

            def show_window(self):
                self.show_calls += 1

        monkeypatch.setattr(tray_windows, "SettingsWindowController", _FakeWindowController)

        tray._open_settings_window()
        first = tray._settings_window
        tray._open_settings_window()

        assert tray._settings_window is first
        assert first.show_calls == 2


class TestQuit:
    def test_quit_delegates_to_the_controller_and_stops_the_icon(self, tray, monkeypatch):
        calls = []
        monkeypatch.setattr(tray.controller, "quit_app", lambda: calls.append(1))
        tray._icon = tray._build_icon()

        tray._quit()

        assert calls == [1]
        assert tray._icon.stopped is True
        assert tray_windows.webview.destroyed is True


class TestRun:
    def test_runs_the_tray_detached_and_starts_the_webview_loop(self, tray):
        tray.run()

        assert tray._icon.ran_detached is True
        assert tray_windows.webview.started is True

    def test_missing_dependencies_logs_and_does_not_raise(self, tray, monkeypatch):
        monkeypatch.setattr(tray_windows, "pystray", None)

        tray.run()  # must not raise
