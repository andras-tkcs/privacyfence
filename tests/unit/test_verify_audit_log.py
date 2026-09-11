"""Tests for scripts/verify_audit_log.py -- SEC-23's chain-verification CLI
(docs/security-remediation-plan.md, Phase 3 item 3.6).

Imported by file path (importlib), same as test_build_org_bundle.py, since
scripts/ isn't part of the installed ``privacyfence`` distribution.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from privacyfence.audit_log import AuditEntry, AuditLogger

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "verify_audit_log.py"
_spec = importlib.util.spec_from_file_location("verify_audit_log", _SCRIPT_PATH)
verify_audit_log = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify_audit_log)


def _record_entries(log_dir: Path, week: str, count: int) -> None:
    logger = AuditLogger(str(log_dir))
    for i in range(count):
        logger.record(AuditEntry(
            timestamp="2026-07-06T12:00:00+00:00", week=week, request_id=str(i),
            connector="gmail", tool="gmail_get_message", tool_name="Read Gmail message",
            summary=f"entry {i}", sender="a@example.com", decision="approved",
            auto_accept_rule="", latency_seconds=1.0,
        ))


class TestMain:
    def test_missing_directory_returns_2(self, tmp_path, capsys):
        rc = verify_audit_log.main([str(tmp_path / "nonexistent")])
        assert rc == 2
        assert "is not a directory" in capsys.readouterr().err

    def test_empty_directory_returns_1(self, tmp_path, capsys):
        rc = verify_audit_log.main([str(tmp_path)])
        assert rc == 1
        assert "nothing to verify" in capsys.readouterr().out

    def test_missing_chain_key_returns_2(self, tmp_path, capsys):
        (tmp_path / "2026-W28.jsonl").write_text("{}\n", encoding="utf-8")
        rc = verify_audit_log.main([str(tmp_path)])
        assert rc == 2
        assert "no chain key" in capsys.readouterr().err

    def test_intact_chain_returns_0(self, tmp_path, capsys):
        _record_entries(tmp_path, "2026-W28", 3)
        rc = verify_audit_log.main([str(tmp_path)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "2026-W28: OK (3 entries checked)" in out

    def test_tampered_chain_returns_1(self, tmp_path, capsys):
        _record_entries(tmp_path, "2026-W28", 2)
        week_file = tmp_path / "2026-W28.jsonl"
        lines = week_file.read_text(encoding="utf-8").splitlines()
        tampered = json.loads(lines[0])
        tampered["summary"] = "an attacker changed this"
        lines[0] = json.dumps(tampered)
        week_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        rc = verify_audit_log.main([str(tmp_path)])
        out = capsys.readouterr().out
        assert rc == 1
        assert "FAILED" in out
        assert "first broken entry: line 1" in out

    def test_week_filter_checks_only_named_weeks(self, tmp_path, capsys):
        _record_entries(tmp_path, "2026-W28", 1)
        _record_entries(tmp_path, "2026-W29", 1)

        rc = verify_audit_log.main([str(tmp_path), "--week", "2026-W28"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "2026-W28" in out
        assert "2026-W29" not in out

    def test_multiple_weeks_all_checked_and_reported(self, tmp_path, capsys):
        _record_entries(tmp_path, "2026-W28", 1)
        _record_entries(tmp_path, "2026-W29", 1)

        rc = verify_audit_log.main([str(tmp_path)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "2026-W28" in out
        assert "2026-W29" in out

    def test_one_bad_week_among_several_still_reports_all_and_fails_overall(self, tmp_path, capsys):
        _record_entries(tmp_path, "2026-W28", 1)
        _record_entries(tmp_path, "2026-W29", 1)
        week_file = tmp_path / "2026-W28.jsonl"
        with open(week_file, "a", encoding="utf-8") as fh:
            fh.write("not valid json\n")

        rc = verify_audit_log.main([str(tmp_path)])
        out = capsys.readouterr().out
        assert rc == 1
        assert "2026-W28: FAILED" in out
        assert "2026-W29: OK" in out

    def test_expands_user_home_in_path(self, tmp_path, monkeypatch, capsys):
        _record_entries(tmp_path, "2026-W28", 1)
        # os.path.expanduser() reads $HOME on POSIX but $USERPROFILE on
        # Windows -- set both so this test controls "home" the same way
        # regardless of which platform it runs on.
        monkeypatch.setenv("HOME", str(tmp_path.parent))
        monkeypatch.setenv("USERPROFILE", str(tmp_path.parent))
        rc = verify_audit_log.main([f"~/{tmp_path.name}"])
        assert rc == 0
