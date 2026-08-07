"""Tests for dialog_window_windows.py: the pywebview-hosted confirmation/
list-picker dialogs (issue #121), same contract as test_dialog_window.py's
coverage of the AppKit ones. Same fake-webview approach as
test_approval_window_windows.py -- see that file's own module docstring for
why (no real pywebview in this sandbox).
"""
from __future__ import annotations

import json

from privacyfence import dialog_window_windows as dww


class _FakeEventSlot:
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
        self.destroyed = False

    def evaluate_js(self, js: str) -> None:
        pass

    def destroy(self) -> None:
        self.destroyed = True


class _FakeWebview:
    def __init__(self, resolve_payload: dict | None = None, closed_auto_fire: bool = False):
        self.resolve_payload = resolve_payload
        self.closed_auto_fire = closed_auto_fire
        self.create_window_calls: list[dict] = []

    def create_window(self, **kwargs):
        self.create_window_calls.append(kwargs)
        if self.resolve_payload is not None:
            kwargs["js_api"].pf_message(json.dumps(self.resolve_payload))
        return _FakeWindow(closed_auto_fire=self.closed_auto_fire)


class TestShowConfirmationDialog:
    def test_confirm_returns_true(self, monkeypatch):
        monkeypatch.setattr(dww, "webview", _FakeWebview(resolve_payload={"action": "resolve", "result": "confirm"}))

        assert dww.show_confirmation_dialog(
            title="t", message_lines=["line"], cancel_label="Cancel", confirm_label="Confirm",
        ) is True

    def test_cancel_returns_false(self, monkeypatch):
        monkeypatch.setattr(dww, "webview", _FakeWebview(resolve_payload={"action": "resolve", "result": "cancel"}))

        assert dww.show_confirmation_dialog(
            title="t", message_lines=["line"], cancel_label="Cancel", confirm_label="Confirm",
        ) is False

    def test_window_closed_without_a_decision_returns_false(self, monkeypatch):
        monkeypatch.setattr(dww, "webview", _FakeWebview(closed_auto_fire=True))

        assert dww.show_confirmation_dialog(
            title="t", message_lines=["line"], cancel_label="Cancel", confirm_label="Confirm",
        ) is False

    def test_missing_pywebview_defaults_to_false(self, monkeypatch):
        monkeypatch.setattr(dww, "webview", None)

        assert dww.show_confirmation_dialog(
            title="t", message_lines=["line"], cancel_label="Cancel", confirm_label="Confirm",
        ) is False

    def test_uses_confirm_width(self, monkeypatch):
        fake = _FakeWebview(resolve_payload={"action": "resolve", "result": "cancel"})
        monkeypatch.setattr(dww, "webview", fake)

        dww.show_confirmation_dialog(title="t", message_lines=["l"], cancel_label="C", confirm_label="K")

        from privacyfence import dialog_window_html
        assert fake.create_window_calls[0]["width"] == int(dialog_window_html.CONFIRM_WIDTH)


class TestShowChoiceDialog:
    def test_returns_chosen_index(self, monkeypatch):
        monkeypatch.setattr(dww, "webview", _FakeWebview(resolve_payload={"action": "resolve", "result": "1"}))

        result = dww.show_choice_dialog(title="t", prompt="pick one", options=["a", "b", "c"])

        assert result == 1

    def test_cancel_returns_none(self, monkeypatch):
        monkeypatch.setattr(dww, "webview", _FakeWebview(resolve_payload={"action": "resolve", "result": "cancel"}))

        assert dww.show_choice_dialog(title="t", prompt="pick", options=["a", "b"]) is None

    def test_out_of_range_index_returns_none(self, monkeypatch):
        monkeypatch.setattr(dww, "webview", _FakeWebview(resolve_payload={"action": "resolve", "result": "99"}))

        assert dww.show_choice_dialog(title="t", prompt="pick", options=["a", "b"]) is None

    def test_non_numeric_result_returns_none(self, monkeypatch):
        monkeypatch.setattr(dww, "webview", _FakeWebview(resolve_payload={"action": "resolve", "result": "oops"}))

        assert dww.show_choice_dialog(title="t", prompt="pick", options=["a", "b"]) is None

    def test_window_closed_without_a_decision_returns_none(self, monkeypatch):
        monkeypatch.setattr(dww, "webview", _FakeWebview(closed_auto_fire=True))

        assert dww.show_choice_dialog(title="t", prompt="pick", options=["a", "b"]) is None

    def test_uses_picker_width(self, monkeypatch):
        fake = _FakeWebview(resolve_payload={"action": "resolve", "result": "cancel"})
        monkeypatch.setattr(dww, "webview", fake)

        dww.show_choice_dialog(title="t", prompt="p", options=["a"])

        from privacyfence import dialog_window_html
        assert fake.create_window_calls[0]["width"] == int(dialog_window_html.PICKER_WIDTH)
