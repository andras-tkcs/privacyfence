"""Unit tests for privacyfence.audit_log — the append-only decision trail."""
from __future__ import annotations

import json

import pytest
from freezegun import freeze_time

from privacyfence.audit_log import (
    AuditEntry,
    AuditLogger,
    current_week,
    get_audit_logger,
    init_audit_logger,
)


def make_entry(**overrides) -> AuditEntry:
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


class TestPiiDetectedField:
    def test_defaults_to_false(self):
        assert make_entry().pii_detected is False

    def test_round_trips_through_jsonl(self, tmp_path):
        logger = AuditLogger(str(tmp_path))
        logger.record(make_entry(pii_detected=True))

        line = (tmp_path / "2026-W28.jsonl").read_text(encoding="utf-8").splitlines()[0]
        assert json.loads(line)["pii_detected"] is True

    def test_old_jsonl_lines_without_the_field_still_parse(self):
        # Entries written before this field existed have no "pii_detected"
        # key at all; export_week_to_excel reconstructs AuditEntry(**line),
        # so the field needs a default rather than being required.
        legacy = dict(
            timestamp="2026-07-06T12:00:00+00:00", week="2026-W28", request_id="",
            connector="gmail", tool="gmail_get_message", tool_name="Read Gmail message",
            summary="s", sender="a@example.com", decision="approved",
            auto_accept_rule="", latency_seconds=1.0,
        )
        entry = AuditEntry(**legacy)
        assert entry.pii_detected is False


class TestClaudeReasonField:
    def test_defaults_to_empty_string(self):
        assert make_entry().claude_reason == ""

    def test_round_trips_through_jsonl(self, tmp_path):
        logger = AuditLogger(str(tmp_path))
        logger.record(make_entry(claude_reason="Summarizing the Q3 budget for the user."))

        line = (tmp_path / "2026-W28.jsonl").read_text(encoding="utf-8").splitlines()[0]
        assert json.loads(line)["claude_reason"] == "Summarizing the Q3 budget for the user."

    def test_old_jsonl_lines_without_the_field_still_parse(self):
        # Same backward-compatibility need as pii_detected above -- entries
        # written before this field existed have no "claude_reason" key.
        legacy = dict(
            timestamp="2026-07-06T12:00:00+00:00", week="2026-W28", request_id="",
            connector="gmail", tool="gmail_get_message", tool_name="Read Gmail message",
            summary="s", sender="a@example.com", decision="approved",
            auto_accept_rule="", latency_seconds=1.0,
        )
        entry = AuditEntry(**legacy)
        assert entry.claude_reason == ""


class TestCurrentWeek:
    @freeze_time("2026-07-06")  # a Monday, ISO week 28 of 2026
    def test_format(self):
        assert current_week() == "2026-W28"

    @freeze_time("2026-01-01")  # ISO week boundary: this date is ISO week 1 of 2026
    def test_iso_week_boundary(self):
        assert current_week() == "2026-W01"


class TestAuditLoggerRecord:
    def test_record_appends_jsonl_line(self, tmp_path):
        logger = AuditLogger(str(tmp_path))
        logger.record(make_entry())

        week_file = tmp_path / "2026-W28.jsonl"
        assert week_file.exists()
        lines = week_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["connector"] == "gmail"
        assert data["decision"] == "approved"

    def test_record_appends_multiple_entries_to_same_week(self, tmp_path):
        logger = AuditLogger(str(tmp_path))
        logger.record(make_entry(decision="approved"))
        logger.record(make_entry(decision="rejected"))

        lines = (tmp_path / "2026-W28.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["decision"] == "approved"
        assert json.loads(lines[1])["decision"] == "rejected"

    def test_record_separates_different_weeks(self, tmp_path):
        logger = AuditLogger(str(tmp_path))
        logger.record(make_entry(week="2026-W01"))
        logger.record(make_entry(week="2026-W28"))

        assert (tmp_path / "2026-W01.jsonl").exists()
        assert (tmp_path / "2026-W28.jsonl").exists()

    def test_log_dir_created_if_missing(self, tmp_path):
        target = tmp_path / "nested" / "audit"
        AuditLogger(str(target))
        assert target.is_dir()


class TestExportWeekToExcel:
    def test_export_returns_none_when_no_jsonl_exists(self, tmp_path):
        logger = AuditLogger(str(tmp_path))
        assert logger.export_week_to_excel("2026-W28") is None

    def test_export_produces_workbook_with_expected_content(self, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")

        logger = AuditLogger(str(tmp_path))
        logger.record(make_entry(decision="approved", pii_detected=True, claude_reason="Summarizing for the user."))
        logger.record(make_entry(decision="auto_accepted", auto_accept_rule="i_am_sender"))
        logger.record(make_entry(decision="rejected"))

        output = logger.export_week_to_excel("2026-W28")
        assert output == str(tmp_path / "2026-W28.xlsx")

        wb = openpyxl.load_workbook(output)
        ws = wb["Decisions"]
        assert ws.cell(row=1, column=1).value == "Timestamp"
        # 3 data rows + 1 header row
        assert ws.max_row == 4
        decisions_col = [ws.cell(row=r, column=8).value for r in range(2, 5)]
        assert decisions_col == ["approved", "auto_accepted", "rejected"]

        assert ws.cell(row=1, column=11).value == "PII Detected"
        pii_col = [ws.cell(row=r, column=11).value for r in range(2, 5)]
        assert pii_col == ["Yes", None, None]  # openpyxl reads back "" cells as None

        assert ws.cell(row=1, column=12).value == "Claude's Reason (unverified)"
        reason_col = [ws.cell(row=r, column=12).value for r in range(2, 5)]
        assert reason_col == ["Summarizing for the user.", None, None]

        summary = wb["Summary"]
        summary_rows = {row[0].value: row[1].value for row in summary.iter_rows(min_row=2) if row[0].value}
        assert summary_rows["Total decisions"] == 3
        assert summary_rows["Approved (manual)"] == 1
        assert summary_rows["Auto-accepted"] == 1
        assert summary_rows["Rejected"] == 1
        assert summary_rows["PII flagged (any decision)"] == 1

    def test_export_skips_malformed_lines(self, tmp_path):
        pytest.importorskip("openpyxl")
        logger = AuditLogger(str(tmp_path))
        logger.record(make_entry())
        week_file = tmp_path / "2026-W28.jsonl"
        with open(week_file, "a", encoding="utf-8") as fh:
            fh.write("not valid json\n")
            fh.write("\n")  # blank line

        output = logger.export_week_to_excel("2026-W28")
        assert output is not None

    def test_export_empty_file_returns_none(self, tmp_path):
        pytest.importorskip("openpyxl")
        logger = AuditLogger(str(tmp_path))
        week_file = tmp_path / "2026-W28.jsonl"
        week_file.write_text("", encoding="utf-8")
        assert logger.export_week_to_excel("2026-W28") is None


class TestExportAllPending:
    def test_exports_only_weeks_missing_xlsx(self, tmp_path):
        pytest.importorskip("openpyxl")
        logger = AuditLogger(str(tmp_path))
        logger.record(make_entry(week="2026-W01"))
        logger.record(make_entry(week="2026-W02"))
        # Pre-create an xlsx for W01 so it should be skipped.
        (tmp_path / "2026-W01.xlsx").write_text("stub", encoding="utf-8")

        logger.export_all_pending()

        # W01's stub should be untouched (not a real workbook, so if it were
        # regenerated openpyxl would have overwritten it with valid content).
        assert (tmp_path / "2026-W01.xlsx").read_text(encoding="utf-8") == "stub"
        assert (tmp_path / "2026-W02.xlsx").exists()


class TestSingletonAccess:
    def test_init_audit_logger_sets_singleton(self, tmp_path):
        logger = init_audit_logger(str(tmp_path))
        assert get_audit_logger() is logger

    def test_get_audit_logger_lazily_creates_fallback(self, monkeypatch, tmp_path):
        fallback_home = tmp_path / "home"
        monkeypatch.setattr("os.path.expanduser", lambda p: str(fallback_home))
        logger = get_audit_logger()
        assert isinstance(logger, AuditLogger)
        assert str(logger._log_dir) == str(fallback_home / ".privacyfence" / "audit")
