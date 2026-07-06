"""Security tests for privacyfence.approval_popup's AppleScript string builder.

_as_str()/_build_message() embed untrusted external content (email subjects,
Slack messages, Drive file names, ...) into an AppleScript string that is
then executed via `osascript`. If the escaping is wrong, a crafted piece of
content could break out of the string literal and run arbitrary AppleScript
(including `do shell script`). These tests round-trip real content through
osascript itself rather than just inspecting the built string, so they catch
any escaping mistake that would actually be exploitable.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from privacyfence.approval_popup import _as_str, _build_message

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="requires osascript (macOS only, matches project's macOS-only runtime)"
)


def _eval_applescript(expr: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", f"return {expr}"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.rstrip("\n")


class TestAsStrRoundTrip:
    @pytest.mark.parametrize(
        "raw",
        [
            "plain text",
            "",
            'has "one" quote pair',
            'trailing quote"',
            '"leading quote',
            '""""',
            "line\nwith\nnewlines",
            "unicode: café — 你好",
        ],
    )
    def test_round_trips_exactly(self, raw):
        assert _eval_applescript(_as_str(raw)) == raw

    def test_injection_attempt_does_not_execute_shell_command(self, tmp_path):
        marker = tmp_path / "pwned"
        payload = f'x" & do shell script "touch {marker}" & "'
        output = _eval_applescript(_as_str(payload))
        assert output == payload
        assert not marker.exists()

    def test_injection_attempt_with_quote_and_return_keywords(self):
        payload = 'x" & return & "injected'
        assert _eval_applescript(_as_str(payload)) == payload


class TestBuildMessage:
    def test_empty_lines_list(self):
        assert _build_message([]) == '""'

    def test_single_line_round_trips(self):
        assert _eval_applescript(_build_message(["hello world"])) == "hello world"

    def test_joins_lines_with_applescript_return(self):
        # osascript's stdout renders the AppleScript `return` constant as "\n".
        lines = ["first line", 'second "quoted" line', "third"]
        assert _eval_applescript(_build_message(lines)) == "\n".join(lines)

    def test_injection_attempt_in_one_of_several_lines(self, tmp_path):
        marker = tmp_path / "pwned"
        lines = ["normal preview line", f'evil" & do shell script "touch {marker}" & "']
        output = _eval_applescript(_build_message(lines))
        assert output == "\n".join(lines)
        assert not marker.exists()
