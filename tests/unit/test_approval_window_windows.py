"""Tests for approval_window_windows.py: the pywebview-hosted approval
window (issue #121), same contract as test_approval_window.py's coverage of
the AppKit one -- show_native_approval() must block until a decision is made
and return (decision, chosen_index).

No real pywebview is installed in this sandbox (or on macOS/Linux dev
machines generally -- it's a Windows-only dependency, see pyproject.toml's
sys_platform markers), so every test here monkeypatches
approval_window_windows.webview with a small fake standing in for the one
surface this module actually calls: create_window(), .events.closed/.loaded,
.evaluate_js(), .destroy(). This mirrors how test_approval_ui.py mocks at
the approval_popup boundary rather than exercising a real dialog.
"""
from __future__ import annotations

import json

import pytest

from privacyfence import approval_window_windows as aww


class _FakeEventSlot:
    """Stands in for a pywebview window.events.<name> slot. auto_fire lets a
    test simulate "the window was already closed by the time the handler
    was registered" deterministically, without threads."""

    def __init__(self, auto_fire: bool = False):
        self.callbacks: list = []
        self.auto_fire = auto_fire

    def __iadd__(self, callback):
        self.callbacks.append(callback)
        if self.auto_fire:
            callback()
        return self


class _FakeWindow:
    def __init__(self, closed_auto_fire: bool = False):
        self.events = type("Events", (), {})()
        self.events.closed = _FakeEventSlot(auto_fire=closed_auto_fire)
        self.events.loaded = _FakeEventSlot()
        self.evaluate_js_calls: list[str] = []
        self.destroyed = False

    def evaluate_js(self, js: str) -> None:
        self.evaluate_js_calls.append(js)

    def destroy(self) -> None:
        self.destroyed = True


class _FakeWebview:
    """resolve_payload, when set, is delivered through the bridge
    synchronously inside create_window() -- before it returns -- since the
    js_api passed to create_window() is already fully wired by then (its
    on_message closure exists the moment BridgeApi() is constructed). That
    means show_native_approval()'s done.wait() (reached only after
    create_window() returns) sees an already-set event and returns
    immediately, no background thread required. closed_auto_fire models
    the other resolution path -- the window being closed without a button
    click -- by firing the moment show_native_approval() registers its own
    events.closed handler (a `window.events.closed += ...` statement)."""

    def __init__(self, resolve_payload: dict | None = None, closed_auto_fire: bool = False):
        self.resolve_payload = resolve_payload
        self.closed_auto_fire = closed_auto_fire
        self.create_window_calls: list[dict] = []

    def create_window(self, **kwargs):
        self.create_window_calls.append(kwargs)
        if self.resolve_payload is not None:
            kwargs["js_api"].pf_message(json.dumps(self.resolve_payload))
        return _FakeWindow(closed_auto_fire=self.closed_auto_fire)


class TestShowNativeApproval:
    def test_accept_resolves_with_no_chosen_index(self, monkeypatch):
        monkeypatch.setattr(aww, "webview", _FakeWebview(resolve_payload={"action": "resolve", "result": "accept"}))

        decision, chosen_index = aww.show_native_approval(
            title="Read Gmail message", preview={"From": "a@b.com"}, details_text="body",
        )

        assert decision == "accept"
        assert chosen_index is None

    def test_deny_resolves(self, monkeypatch):
        monkeypatch.setattr(aww, "webview", _FakeWebview(resolve_payload={"action": "resolve", "result": "deny"}))

        decision, chosen_index = aww.show_native_approval(title="t", preview={}, details_text="d")

        assert decision == "deny"
        assert chosen_index is None

    def test_accept_all_carries_chosen_index(self, monkeypatch):
        monkeypatch.setattr(
            aww, "webview",
            _FakeWebview(resolve_payload={"action": "resolve", "result": "accept_all", "choice": 1}),
        )

        decision, chosen_index = aww.show_native_approval(
            title="t", preview={}, details_text="d",
            accept_all_choices=[("rule_a", "this folder"), ("rule_b", "if I'm sender")],
        )

        assert decision == "accept_all"
        assert chosen_index == 1

    def test_unrecognized_result_defaults_to_deny(self, monkeypatch):
        monkeypatch.setattr(
            aww, "webview", _FakeWebview(resolve_payload={"action": "resolve", "result": "something-else"}),
        )

        decision, chosen_index = aww.show_native_approval(title="t", preview={}, details_text="d")

        assert decision == "deny"
        assert chosen_index is None

    def test_window_closed_without_a_decision_defaults_to_deny(self, monkeypatch):
        monkeypatch.setattr(aww, "webview", _FakeWebview(closed_auto_fire=True))

        decision, chosen_index = aww.show_native_approval(title="t", preview={}, details_text="d")

        assert decision == "deny"
        assert chosen_index is None

    def test_missing_pywebview_defaults_to_deny(self, monkeypatch):
        monkeypatch.setattr(aww, "webview", None)

        decision, chosen_index = aww.show_native_approval(title="t", preview={}, details_text="d")

        assert decision == "deny"
        assert chosen_index is None

    def test_window_width_matches_layout_content_width(self, monkeypatch):
        fake = _FakeWebview(resolve_payload={"action": "resolve", "result": "deny"})
        monkeypatch.setattr(aww, "webview", fake)

        aww.show_native_approval(title="t", preview={}, details_text="d", layout="wide")

        from privacyfence import approval_window_html
        assert fake.create_window_calls[0]["width"] == int(approval_window_html.CONTENT_WIDTH["wide"])

    def test_window_is_destroyed_after_resolving(self, monkeypatch):
        fake = _FakeWebview(resolve_payload={"action": "resolve", "result": "accept"})
        monkeypatch.setattr(aww, "webview", fake)
        created = []
        real_create = fake.create_window

        def _tracking_create_window(**kwargs):
            window = real_create(**kwargs)
            created.append(window)
            return window

        monkeypatch.setattr(fake, "create_window", _tracking_create_window)

        aww.show_native_approval(title="t", preview={}, details_text="d")

        assert created[0].destroyed is True

    def test_bridge_html_includes_the_polyfill(self, monkeypatch):
        fake = _FakeWebview(resolve_payload={"action": "resolve", "result": "deny"})
        monkeypatch.setattr(aww, "webview", fake)

        aww.show_native_approval(title="t", preview={}, details_text="d")

        assert "window.pywebview.api.pf_message" in fake.create_window_calls[0]["html"]


class TestIconAndReadingTimeHelpers:
    def test_reading_time_label_short_text_is_seconds(self):
        assert aww._reading_time_label("just a few words") == "~1 sec read"

    def test_reading_time_label_long_text_is_minutes(self):
        label = aww._reading_time_label(" ".join(["word"] * 400))
        assert "min read" in label

    def test_connector_icon_path_none_for_empty_connector(self):
        assert aww._connector_icon_path("") is None

    def test_connector_icon_path_none_for_unknown_connector(self):
        assert aww._connector_icon_path("not-a-real-connector") is None

    def test_icon_data_uri_empty_for_missing_path(self):
        assert aww._icon_data_uri(None) == ""
        assert aww._icon_data_uri("") == ""
