"""Tests for platform_open.py: the cross-platform "open this in the default
app" helper that replaced three ``subprocess.run(["open", ...])`` call sites
in settings_window.py/settings_controller.py (see that module's own
docstring for why -- macOS's ``open`` command has no direct Windows
equivalent)."""
from __future__ import annotations

from privacyfence import platform_open


class TestOpenPathOrUrl:
    def test_macos_shells_out_to_open(self, monkeypatch):
        monkeypatch.setattr(platform_open.sys, "platform", "darwin")
        calls = []
        monkeypatch.setattr(
            platform_open.subprocess, "run", lambda args, **kw: calls.append((args, kw))
        )

        platform_open.open_path_or_url("https://example.com")

        assert calls == [(["open", "https://example.com"], {"check": False})]

    def test_windows_calls_os_startfile(self, monkeypatch):
        monkeypatch.setattr(platform_open.sys, "platform", "win32")
        calls = []
        # os.startfile doesn't exist on non-Windows Python at all -- set it
        # as a fresh attribute for the duration of this test rather than
        # relying on it already being there (this suite also runs on Linux/
        # macOS dev machines with no such attribute to patch).
        monkeypatch.setattr(platform_open.os, "startfile", lambda target: calls.append(target), raising=False)

        platform_open.open_path_or_url(r"C:\Users\me\PrivacyFence\logs")

        assert calls == [r"C:\Users\me\PrivacyFence\logs"]

    def test_windows_swallows_a_missing_default_handler(self, monkeypatch):
        monkeypatch.setattr(platform_open.sys, "platform", "win32")

        def _raise(target):
            raise OSError("no application is associated")

        monkeypatch.setattr(platform_open.os, "startfile", _raise, raising=False)

        platform_open.open_path_or_url("something.unknownext")  # must not raise

    def test_other_platforms_fall_back_to_webbrowser(self, monkeypatch):
        monkeypatch.setattr(platform_open.sys, "platform", "linux")
        calls = []
        monkeypatch.setattr(platform_open.webbrowser, "open", lambda target: calls.append(target))

        platform_open.open_path_or_url("https://example.com")

        assert calls == ["https://example.com"]
