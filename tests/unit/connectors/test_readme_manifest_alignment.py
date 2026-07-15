"""Drift detection between the docs privacy matrix and the actual code.

The docs document, per tool, whether it's a read or a write and what
gate it goes through -- this is PrivacyFence's public privacy promise.
Nothing enforces that the table stays in sync with the connectors as they
evolve, so this test parses the docs tables and cross-checks them
against each connector's real ToolSpec.read_only flag, and against
auto_accept.TOOL_TO_GATE (the static "auto"/"review"/"popup" registry a
preflight caller relies on -- see privacyfence_check_policy). It caught two
real gaps when written: gmail_reply_draft/gmail_reply_all_draft and
drive_upload_file existed in code but were undocumented in the README (the
privacy matrix has since moved to docs/TECHNICAL_REFERENCE.md).
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from privacyfence.auto_accept import TOOL_TO_GATE
from privacyfence.connectors.calendar import CalendarConnector
from privacyfence.connectors.confluence import ConfluenceConnector
from privacyfence.connectors.contacts import ContactsConnector
from privacyfence.connectors.drive import DriveConnector
from privacyfence.connectors.gmail import GmailConnector
from privacyfence.connectors.jira import JiraConnector
from privacyfence.connectors.salesforce import SalesforceConnector
from privacyfence.connectors.slack import SlackConnector
from privacyfence.connectors.tasks import TasksConnector
from privacyfence.connectors.telegram import TelegramConnector

CONNECTOR_CLASSES = [
    GmailConnector, DriveConnector, SlackConnector, CalendarConnector,
    ContactsConnector, SalesforceConnector, JiraConnector, ConfluenceConnector,
    TasksConnector, TelegramConnector,
]

README_PATH = Path(__file__).resolve().parents[3] / "docs" / "TECHNICAL_REFERENCE.md"

# Matches only rows of the 5-column privacy-matrix tables
# (`Tool | Dir | Gate | Cowork preview | Details popup`), not the 2-column
# auto-accept-rules tables (`Rule | Matches when...`) further down the file.
_ROW_RE = re.compile(r"^\|\s*`([a-z0-9_]+)`\s*\|\s*(read|write)\s*\|\s*(auto|review|popup)\s*\|", re.MULTILINE)

# Matches a tool="..." kwarg followed (non-greedily, across the rest of that
# same gated_call(...) invocation) by its gate="..." kwarg -- the two are
# always kwargs of one call, so the nearest gate= after a given tool= is that
# tool's own gate. Tools with no match here never call gated_call() at all,
# i.e. they're unconditionally "auto".
_SRC_TOOL_GATE_RE = re.compile(r'tool="(?P<tool>\w+)",.*?gate="(?P<gate>review|popup)"', re.DOTALL)


def _readme_privacy_matrix() -> dict[str, tuple[str, str]]:
    text = README_PATH.read_text(encoding="utf-8")
    start = text.index("## Connectors & privacy matrix")
    end = text.index("## Auto-accept rules")
    section = text[start:end]
    return {tool: (direction, gate) for tool, direction, gate in _ROW_RE.findall(section)}


def _all_code_tools() -> dict[str, bool]:
    """Return {tool_name: read_only} across every registered connector."""
    tools: dict[str, bool] = {}
    for cls in CONNECTOR_CLASSES:
        connector = cls(MagicMock())
        for spec in connector.tool_specs():
            tools[spec.name] = spec.read_only
    return tools


def _source_declared_gates() -> dict[str, str]:
    """Parse connectors/*.py for the gate= each tool actually passes to gated_call()."""
    gates: dict[str, str] = {}
    seen_files: set[Path] = set()
    for cls in CONNECTOR_CLASSES:
        path = Path(inspect.getfile(cls))
        if path in seen_files:
            continue
        seen_files.add(path)
        text = path.read_text(encoding="utf-8")
        for match in _SRC_TOOL_GATE_RE.finditer(text):
            gates[match.group("tool")] = match.group("gate")
    return gates


@pytest.fixture(scope="module")
def readme_matrix():
    matrix = _readme_privacy_matrix()
    assert len(matrix) > 30, "README parser found suspiciously few rows -- check _ROW_RE / section markers"
    return matrix


@pytest.fixture(scope="module")
def code_tools():
    return _all_code_tools()


def test_every_code_tool_is_documented_in_readme(readme_matrix, code_tools):
    undocumented = sorted(set(code_tools) - set(readme_matrix))
    assert undocumented == [], (
        f"Tools exist in connector code but are missing from docs/TECHNICAL_REFERENCE.md's privacy matrix: {undocumented}"
    )


def test_every_readme_tool_still_exists_in_code(readme_matrix, code_tools):
    stale = sorted(set(readme_matrix) - set(code_tools))
    assert stale == [], (
        f"docs/TECHNICAL_REFERENCE.md documents tools that no longer exist in any connector: {stale}"
    )


@pytest.mark.parametrize("tool", sorted(_all_code_tools()))
def test_read_only_flag_matches_documented_direction(tool, readme_matrix, code_tools):
    if tool not in readme_matrix:
        pytest.skip(f"{tool} undocumented in docs (see test_every_code_tool_is_documented_in_readme)")
    direction, _gate = readme_matrix[tool]
    expected_read_only = direction == "read"
    assert code_tools[tool] == expected_read_only, (
        f"{tool}: docs say dir={direction!r} (expects read_only={expected_read_only}) "
        f"but ToolSpec.read_only={code_tools[tool]!r}"
    )


def test_every_code_tool_is_in_tool_to_gate(code_tools):
    missing = sorted(set(code_tools) - set(TOOL_TO_GATE))
    assert missing == [], (
        f"Tools exist in connector code but are missing from auto_accept.TOOL_TO_GATE: {missing}"
    )


def test_tool_to_gate_has_no_stale_entries(code_tools):
    stale = sorted(set(TOOL_TO_GATE) - set(code_tools))
    assert stale == [], (
        f"auto_accept.TOOL_TO_GATE lists tools that no longer exist in any connector: {stale}"
    )


@pytest.mark.parametrize("tool", sorted(TOOL_TO_GATE))
def test_tool_to_gate_matches_documented_gate(tool, readme_matrix):
    if tool not in readme_matrix:
        pytest.skip(f"{tool} undocumented in docs (see test_every_code_tool_is_documented_in_readme)")
    _direction, documented_gate = readme_matrix[tool]
    assert TOOL_TO_GATE[tool] == documented_gate, (
        f"{tool}: docs say gate={documented_gate!r} but "
        f"auto_accept.TOOL_TO_GATE[{tool!r}]={TOOL_TO_GATE[tool]!r}"
    )


@pytest.mark.parametrize("tool", sorted(_all_code_tools()))
def test_tool_to_gate_matches_gated_call_source(tool, code_tools):
    """TOOL_TO_GATE must match what connectors/*.py actually passes to gated_call() --
    the docs can drift, but this checks the real call sites directly."""
    source_gates = _source_declared_gates()
    expected = source_gates.get(tool, "auto")
    assert TOOL_TO_GATE.get(tool) == expected, (
        f"{tool}: connectors/*.py passes gate={expected!r} to gated_call() (or never calls it, "
        f"implying \"auto\") but auto_accept.TOOL_TO_GATE[{tool!r}]={TOOL_TO_GATE.get(tool)!r}"
    )
