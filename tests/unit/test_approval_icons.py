"""Tests for approval_icons.py -- shared icon-asset loading for both the
native (AppKit) and web approval hosts. Pure filesystem + base64, no AppKit
-- importable and testable on any platform.
"""
from __future__ import annotations

from privacyfence import approval_icons


class TestShieldIcon:
    def test_shield_icon_path_resolves_to_a_real_bundled_file(self):
        path = approval_icons.shield_icon_path()
        assert path is not None
        assert path.endswith(".png")

    def test_shield_icon_data_uri_is_a_base64_png(self):
        uri = approval_icons.icon_data_uri(approval_icons.shield_icon_path())
        assert uri.startswith("data:image/png;base64,")


class TestConnectorIcon:
    def test_unknown_connector_returns_no_path(self):
        assert approval_icons.connector_icon_path("not-a-real-connector") is None

    def test_empty_connector_returns_no_path(self):
        assert approval_icons.connector_icon_path("") is None

    def test_gmail_connector_icon_resolves(self):
        # gmail.png ships in resources/connector_icons/ -- see that
        # directory's README.
        path = approval_icons.connector_icon_path("gmail")
        assert path is not None
        assert path.endswith("gmail.png")


class TestIconDataUri:
    def test_missing_path_is_empty_string_not_an_error(self):
        assert approval_icons.icon_data_uri(None) == ""

    def test_repeated_calls_are_cached_and_return_the_same_value(self):
        path = approval_icons.shield_icon_path()
        first = approval_icons.icon_data_uri(path)
        second = approval_icons.icon_data_uri(path)
        assert first == second
        assert path in approval_icons._icon_data_uri_cache
