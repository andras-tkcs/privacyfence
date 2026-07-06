"""Drift detection between README.md's privacy matrix and the actual code.

The README documents, per tool, whether it's a read or a write and what
gate it goes through -- this is PrivacyFence's public privacy promise.
Nothing enforces that the table stays in sync with the connectors as they
evolve, so this test parses the README tables and cross-checks them
against each connector's real ToolSpec.read_only flag. It caught two real
gaps when written: gmail_reply_draft/gmail_reply_all_draft and
drive_upload_file existed in code but were undocumented in README.md.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

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

README_PATH = Path(__file__).resolve().parents[3] / "README.md"

# Matches only rows of the 5-column privacy-matrix tables
# (`Tool | Dir | Gate | Cowork preview | Details popup`), not the 2-column
# auto-accept-rules tables (`Rule | Matches when...`) further down the file.
_ROW_RE = re.compile(r"^\|\s*`([a-z0-9_]+)`\s*\|\s*(read|write)\s*\|\s*(auto|review|popup)\s*\|", re.MULTILINE)


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
        f"Tools exist in connector code but are missing from README.md's privacy matrix: {undocumented}"
    )


def test_every_readme_tool_still_exists_in_code(readme_matrix, code_tools):
    stale = sorted(set(readme_matrix) - set(code_tools))
    assert stale == [], (
        f"README.md documents tools that no longer exist in any connector: {stale}"
    )


@pytest.mark.parametrize("tool", sorted(_all_code_tools()))
def test_read_only_flag_matches_documented_direction(tool, readme_matrix, code_tools):
    if tool not in readme_matrix:
        pytest.skip(f"{tool} undocumented in README (see test_every_code_tool_is_documented_in_readme)")
    direction, _gate = readme_matrix[tool]
    expected_read_only = direction == "read"
    assert code_tools[tool] == expected_read_only, (
        f"{tool}: README says dir={direction!r} (expects read_only={expected_read_only}) "
        f"but ToolSpec.read_only={code_tools[tool]!r}"
    )
