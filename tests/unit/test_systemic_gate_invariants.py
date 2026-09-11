"""Systemic per-call-site invariants (TST-13, docs/security-remediation-plan.md
Phase 3.12): extends tests/unit/connectors/test_readme_manifest_alignment.py's
own parameterized, source-scanning pattern -- one assertion per tool/site,
generated from the real code rather than a hand-maintained list -- to three
more properties the review names, none of which had a mechanical regression
guard before this module existed:

- Every gated tool (``auto_accept.TOOL_TO_GATE[tool] != "auto"``) declares a
  required ``"reason"`` ``ToolParam`` -- gate.py's own module comment: "Every
  gated tool's ToolSpec declares a required reason param so Claude['s]..."
  (see gate.py's ``reason_scope``/``current_reason`` docstrings). Without
  this param, ``web/mcp_dispatch.py``'s ``McpDispatcher.call()`` still runs
  fine (``args.pop("reason", "")`` defaults to ``""``), so a missing
  declaration is a silent capability loss (no reason ever recorded for that
  tool's audit trail), not a crash -- exactly the kind of gap a parametrized
  test catches and an ad hoc code read doesn't.
- Every ``gate="review"`` ``gated_call()`` site passes ``pii_scan_text`` --
  gate.py's module docstring: content-only text so the real PII scan isn't
  run against unavoidable structural envelope metadata (an email's From/To,
  a chat message's channel/sender, a page's author) present on *every*
  single read, which "will otherwise make the PII gate fire on essentially
  every read." A short, individually-justified exemption list
  (``_PII_SCAN_TEXT_EXEMPT`` below) covers the few tools whose
  ``details_text`` is already pure record/content with no separate envelope
  layer to strip -- documented rather than silently excluded, per this
  repo's own "where the codebase is inconsistent, call it out explicitly"
  convention (docs/coding-and-testing-guidelines.md).
- All ten remaining credential/token-file writers across the
  ``*_client.py``/``*_oauth.py`` modules go through ``secure_files.py``'s
  ``atomic_write_text``/``atomic_write_json`` rather than a hand-rolled
  ``open()``/``.write()`` -- the SEC-09 invariant (secure_files.py's own
  module docstring) checked per call site here, rather than trusted to have
  been done once at each site and stay that way. (Eleven at the time the
  review was written; see ``TOKEN_WRITE_SITES``'s own comment for why
  ``room_directory_client.py``'s site doesn't count against this specific
  check anymore -- it's still covered, just separately.)
"""
from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from privacyfence.auto_accept import TOOL_TO_GATE
from privacyfence.connector import ToolSpec
from privacyfence.connectors.apps_script import AppsScriptConnector
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

# docs/automated-test-strategy-plan.md Phase 0: source-scanning assertions
# over real code, no I/O -- unit per testing-policy.md's seven-layer taxonomy.
pytestmark = pytest.mark.unit

CONNECTOR_CLASSES = [
    GmailConnector, DriveConnector, SlackConnector, CalendarConnector,
    ContactsConnector, SalesforceConnector, JiraConnector, ConfluenceConnector,
    TasksConnector, TelegramConnector, AppsScriptConnector,
]

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "privacyfence"


def _all_tool_specs() -> dict[str, ToolSpec]:
    """{tool_name: ToolSpec} across every registered connector -- same
    construction test_readme_manifest_alignment.py's own _all_code_tools()
    uses, kept here as a separate copy (not imported from that module) so
    this file has no test-to-test dependency on it."""
    specs: dict[str, ToolSpec] = {}
    for cls in CONNECTOR_CLASSES:
        connector = cls(MagicMock())
        for spec in connector.tool_specs():
            specs[spec.name] = spec
    return specs


def _gated_call_blocks(path: Path) -> list[str]:
    """Every ``gated_call(...)`` invocation's full source text from
    ``path``, extracted by counting balanced parens from each ``gated_call(``
    -- unlike test_readme_manifest_alignment.py's own single-regex
    ``_SRC_TOOL_GATE_RE`` (which only needs to find the *nearest* gate= after
    a tool=, fine for a one-fact check), this needs the whole call's text so
    a later check can look for pii_scan_text= appearing anywhere within it,
    however far from tool=/gate= it happens to be written.
    """
    text = path.read_text(encoding="utf-8")
    blocks: list[str] = []
    i = 0
    needle = "gated_call("
    while True:
        start = text.find(needle, i)
        if start == -1:
            break
        depth = 0
        j = start + len(needle) - 1
        while True:
            char = text[j]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        blocks.append(text[start:j + 1])
        i = j + 1
    return blocks


def _review_gated_call_sites() -> dict[str, str]:
    """{tool_name: full gated_call(...) source text} for every gate="review"
    call site across every connector module gated_call() appears in."""
    sites: dict[str, str] = {}
    for path in sorted((SRC_ROOT / "connectors").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "gated_call(" not in text:
            continue
        for block in _gated_call_blocks(path):
            if 'gate="review"' not in block:
                continue
            tool_match = block.split('tool="', 1)
            if len(tool_match) < 2:
                continue
            tool = tool_match[1].split('"', 1)[0]
            sites[tool] = block
    return sites


# Salesforce's three review-gated reads (salesforce_get_record/_run_report/
# _search) build details_text directly from the record's/report's own
# field values (_format_flat_fields()/_report_tables()) -- unlike Gmail's
# From/To headers, Slack's channel/sender, Jira's reporter/assignee, or
# Confluence's author, there is no separate transport/system-added envelope
# layer wrapping a Salesforce record: the fields *are* the content, so
# pii_scan_text would just be a copy of details_text with nothing to strip
# out of it. See salesforce.py's own comment on _get_record's table/preview
# construction. Each entry here needs its own justification, not a blanket
# per-connector or per-gate exemption -- Salesforce's own list_reports is
# gate="auto" (not in this table at all), and every other connector's
# review-gated reads pass pii_scan_text (see
# test_every_pii_scan_text_exempt_tool_is_still_review_gated below, which
# fails loudly the day that stops being true for any of these three).
_PII_SCAN_TEXT_EXEMPT = {
    "salesforce_get_record",
    "salesforce_run_report",
    "salesforce_search",
}

# The credential/token-file write sites -- (module, class-or-None,
# function/method name) triples, one per site. See this module's own
# docstring for how that count was derived: every *_client.py's private
# Client._save_token(self, creds) *method*, plus the three module-level
# function helpers (atlassian_oauth.save_token_file, google_oauth.
# save_credentials, slack_client.save_token_record) and salesforce_client's
# real writer (_save_token_file -- save_token_file is a thin public wrapper
# around it, not a second independent write site).
#
# TST-13 named eleven at the time the review was written; room_directory_
# client.py's own _save_token (once counted here) was retired along with
# the rest of that module once scripts/sync_room_directory.py became its
# only caller -- that script now carries its own standalone copy of the
# atomic-write pattern (deliberately, same as build_org_bundle.py: it must
# not import the privacyfence package at all, see its own module
# docstring), so it was never a fair fit for *this* check (which is
# specifically "does it call the shared helper" -- a script that can't
# import that helper by design isn't a violation of the invariant). Ten
# real sites remain here; the standalone script's own copy is checked
# separately below for the pattern it stands in for, not for calling the
# helper it deliberately can't reach.
TOKEN_WRITE_SITES: tuple[tuple[str, str | None, str], ...] = (
    ("apps_script_client", "AppsScriptClient", "_save_token"),
    ("atlassian_oauth", None, "save_token_file"),
    ("calendar_client", "CalendarClient", "_save_token"),
    ("contacts_client", "ContactsClient", "_save_token"),
    ("drive_client", "DriveClient", "_save_token"),
    ("gmail_client", "GmailClient", "_save_token"),
    ("google_oauth", None, "save_credentials"),
    ("salesforce_client", None, "_save_token_file"),
    ("slack_client", None, "save_token_record"),
    ("tasks_client", "TasksClient", "_save_token"),
)

# scripts/sync_room_directory.py's own standalone _atomic_write_text -- see
# TOKEN_WRITE_SITES's own comment above for why it's checked separately
# rather than folded into that tuple/parametrize.
_SYNC_ROOM_DIRECTORY_PATH = SRC_ROOT.parent.parent / "scripts" / "sync_room_directory.py"


class TestReasonParamOnEveryGatedTool:
    @pytest.mark.parametrize(
        "tool", sorted(t for t, gate in TOOL_TO_GATE.items() if gate != "auto"),
    )
    def test_gated_tool_declares_a_required_reason_param(self, tool):
        specs = _all_tool_specs()
        assert tool in specs, f"{tool} is in TOOL_TO_GATE but not returned by any connector's tool_specs()"
        reason_params = [p for p in specs[tool].params if p.name == "reason"]
        assert reason_params, f"{tool} is gate={TOOL_TO_GATE[tool]!r} but declares no 'reason' ToolParam"
        assert reason_params[0].required, f"{tool}'s 'reason' ToolParam must be required=True"

    def test_auto_tools_are_not_silently_missing_from_this_check(self):
        # Sanity check on the parametrize expression itself: TOOL_TO_GATE
        # really does contain both kinds, so "gate != 'auto'" above is
        # actually filtering something, not accidentally selecting every
        # entry (or none).
        gates = set(TOOL_TO_GATE.values())
        assert "auto" in gates
        assert {"review", "popup"} & gates


class TestPiiScanTextOnEveryReviewGatedTool:
    @pytest.mark.parametrize("tool", sorted(_review_gated_call_sites()))
    def test_review_gated_call_passes_pii_scan_text(self, tool):
        if tool in _PII_SCAN_TEXT_EXEMPT:
            pytest.skip(f"{tool} is a documented pii_scan_text exemption -- see _PII_SCAN_TEXT_EXEMPT")
        block = _review_gated_call_sites()[tool]
        assert "pii_scan_text" in block, (
            f"{tool} is gate=\"review\" but its gated_call() doesn't pass pii_scan_text -- either add it "
            "(content-only text, no sender/author/channel envelope) or add a justified entry to "
            "_PII_SCAN_TEXT_EXEMPT in this test module explaining why not."
        )

    def test_every_pii_scan_text_exempt_tool_is_still_review_gated(self):
        # Guards the exemption list itself against drift: an exempted tool
        # that stops being gate="review" at all (or is renamed/removed)
        # should be caught here rather than the exemption silently applying
        # to nothing.
        sites = _review_gated_call_sites()
        missing = _PII_SCAN_TEXT_EXEMPT - set(sites)
        assert missing == set(), (
            f"_PII_SCAN_TEXT_EXEMPT names tool(s) no longer found as a gate=\"review\" gated_call() site: {missing}"
        )


class TestTokenSitesUseTheSharedSecureWriteHelper:
    def test_ten_token_write_sites_are_listed(self):
        # A token writer added or removed without updating TOKEN_WRITE_SITES
        # above is itself worth catching, not just silently checking
        # whatever's currently listed -- see that tuple's own comment for
        # why this is ten, not the review's original eleven.
        assert len(TOKEN_WRITE_SITES) == 10

    @pytest.mark.parametrize(
        "module_name, class_name, func_name", TOKEN_WRITE_SITES,
        ids=[f"{m}.{(c + '.') if c else ''}{f}" for m, c, f in TOKEN_WRITE_SITES],
    )
    def test_token_write_site_calls_the_shared_atomic_write_helper(self, module_name, class_name, func_name):
        module = importlib.import_module(f"privacyfence.{module_name}")
        owner = getattr(module, class_name) if class_name else module
        func = getattr(owner, func_name)
        source = inspect.getsource(func)
        site = f"{module_name}.{(class_name + '.') if class_name else ''}{func_name}"
        assert "atomic_write_text(" in source or "atomic_write_json(" in source, (
            f"{site} no longer calls secure_files.atomic_write_text/atomic_write_json -- "
            "SEC-09 (docs/security-remediation-plan.md Phase 1 item 1.4) requires every credential/token "
            "writer to go through the shared atomic, 0600-permissioned helper, not a hand-rolled open()/write()."
        )

    def test_sync_room_directory_script_still_carries_the_safe_atomic_write_pattern(self):
        # This standalone script (see TOKEN_WRITE_SITES's own comment)
        # deliberately can't import secure_files.atomic_write_text -- it
        # keeps its own copy of the same write-then-rename-plus-chmod core
        # instead. Checked here for that pattern's own hallmarks rather
        # than for calling a helper it can never call by design, so a
        # regression to a naive open()/write() still fails this test.
        assert _SYNC_ROOM_DIRECTORY_PATH.is_file(), (
            f"{_SYNC_ROOM_DIRECTORY_PATH} not found -- update this test (and TOKEN_WRITE_SITES's own "
            "comment) if it moved, was renamed, or its OAuth token write was retired entirely."
        )
        source = _SYNC_ROOM_DIRECTORY_PATH.read_text(encoding="utf-8")
        func_start = source.index("def _atomic_write_text(")
        func_source = source[func_start:source.index("\ndef ", func_start + 1)]
        assert "os.O_CREAT" in func_source and "os.O_EXCL" in func_source, (
            f"{_SYNC_ROOM_DIRECTORY_PATH}'s _atomic_write_text no longer creates its temp file with "
            "O_CREAT|O_EXCL -- that's the collision-safety half of the atomic-write pattern it exists to copy."
        )
        assert "os.replace(" in func_source, (
            f"{_SYNC_ROOM_DIRECTORY_PATH}'s _atomic_write_text no longer renames into place with os.replace -- "
            "that's the atomicity half of the pattern (a partial write must never be observable at the real path)."
        )
        assert "os.chmod(" in func_source or ", mode)" in func_source, (
            f"{_SYNC_ROOM_DIRECTORY_PATH}'s _atomic_write_text no longer sets restrictive permissions on the "
            "OAuth token file it writes."
        )
