"""The Windows autostart task template, checked on every PR, on any OS.

``installer/privacyfence-task.xml.tmpl`` is the entire Windows autostart
mechanism, and until now nothing in the suite looked at it at all: its bugs
were found one at a time by real runs of the scheduled, Windows-only
``windows-graphical-session.yml`` workflow -- several of them the kind a
parser can see instantly (a prolog encoding declaration ``schtasks`` rejects
outright, a missing schema version, a principal bound to no actions). These
tests are the cheap half of that coverage: they cannot prove Task Scheduler
*accepts* the document, which still needs a real Windows run, but they hold
every property the real runs had to discover the hard way.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.windows_task_contract import EXEC_PATH_PLACEHOLDER, assert_task_xml_matches_autostart_contract

TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "installer" / "privacyfence-task.xml.tmpl"
INSTALLED_EXEC_PATH = r"C:\Program Files\PrivacyFence\privacyfence-app.exe"


@pytest.fixture
def template_text() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def test_template_substituted_at_install_time_matches_the_autostart_contract(template_text):
    assert EXEC_PATH_PLACEHOLDER in template_text, (
        f"{TEMPLATE_PATH.name} no longer carries the placeholder installer/privacyfence.iss's "
        f"RegisterAutostartTask substitutes"
    )
    assert_task_xml_matches_autostart_contract(
        template_text.replace(EXEC_PATH_PLACEHOLDER, INSTALLED_EXEC_PATH),
        exec_path=INSTALLED_EXEC_PATH,
    )


def test_template_prolog_declares_no_encoding(template_text):
    """The bug that silently broke every install for several runs.

    ``schtasks.exe`` reads this file and hands it to MSXML as a Unicode
    stream, so a prolog claiming an encoding contradicts the stream the
    parser is already on and the whole registration is rejected
    (``(1,40)::ERROR: unable to switch the encoding``). The content is pure
    ASCII, so declaring nothing is correct however schtasks decides to read
    it.
    """
    prolog = template_text.splitlines()[0]
    assert prolog.startswith("<?xml "), f"unexpected first line: {prolog!r}"
    assert "encoding" not in prolog, (
        f"the XML prolog declares an encoding again ({prolog!r}) -- schtasks rejects the whole "
        f"registration when it does"
    )
    assert template_text.isascii(), (
        "the definition is no longer pure ASCII, which is the only reason declaring no encoding "
        "is safe -- see this test's own docstring"
    )


def test_template_comment_never_contains_a_double_hyphen(template_text):
    """XML comments may not contain ``--`` anywhere in their body, and this
    file's header comment is long enough (and rewritten often enough) for
    that to be a live hazard -- it says so itself. A violation makes the
    document unparseable, which is a registration failure on a real
    install."""
    for comment in _comment_bodies(template_text):
        assert "--" not in comment, f"XML comment contains a double hyphen:\n{comment}"


def _comment_bodies(xml_text: str) -> list[str]:
    bodies: list[str] = []
    rest = xml_text
    while True:
        start = rest.find("<!--")
        if start < 0:
            return bodies
        end = rest.find("-->", start + 4)
        assert end > 0, "unterminated XML comment"
        bodies.append(rest[start + 4:end])
        rest = rest[end + 3:]
