"""Tests for webview_bridge_windows.py: the pywebview JS-bridge polyfill
shared by approval_window_windows.py/dialog_window_windows.py/
settings_window_windows.py (issue #121). Pure Python/string logic, no
pywebview import at all -- runs on every platform, same as the *_html.py
modules it exists to keep unmodified.
"""
from __future__ import annotations

from privacyfence.webview_bridge_windows import BridgeApi, inject_bridge_polyfill


class TestInjectBridgePolyfill:
    def test_inserts_right_after_head_tag(self):
        html = "<html>\n<head>\n<style>x</style>\n</head>\n<body>hi</body>\n</html>"

        result = inject_bridge_polyfill(html)

        assert result.index("<head>") < result.index("window.pywebview.api.pf_message")
        assert "<style>x</style>" in result
        assert result.count("<head>") == 1

    def test_prepends_when_no_head_tag(self):
        html = "<div>fragment, no head element</div>"

        result = inject_bridge_polyfill(html)

        assert result.startswith("\n<script>") or result.lstrip().startswith("<script>")
        assert result.endswith(html)

    def test_polyfill_defines_webkit_message_handler_shape(self):
        result = inject_bridge_polyfill("<html><head></head><body></body></html>")

        assert "window.webkit.messageHandlers.pf" in result
        assert "pywebviewready" in result


class TestBridgeApi:
    def test_valid_json_dict_is_forwarded_to_on_message(self):
        received = []
        api = BridgeApi(on_message=received.append)

        api.pf_message('{"action": "resolve", "result": "accept"}')

        assert received == [{"action": "resolve", "result": "accept"}]

    def test_malformed_json_is_silently_dropped(self):
        received = []
        api = BridgeApi(on_message=received.append)

        api.pf_message("not json")

        assert received == []

    def test_non_dict_json_is_silently_dropped(self):
        received = []
        api = BridgeApi(on_message=received.append)

        api.pf_message('["a", "list", "not", "a", "dict"]')

        assert received == []
