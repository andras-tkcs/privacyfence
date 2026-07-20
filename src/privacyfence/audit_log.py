"""Audit log: records every accept/deny/auto_accept decision.

Entries are appended to JSON-lines files in logs/audit/YYYY-WNN.jsonl
(one file per ISO week). A weekly Excel export (openpyxl) is generated
at daemon startup for any week that has a .jsonl but no .xlsx yet.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Decisions where the AI actually received the data or the write went
# through -- used by AuditLogger.recent_matches() below to count how many
# times a request has already been let through, not merely asked about.
_APPROVED_LIKE_DECISIONS = frozenset({
    "approved", "auto_accepted", "accepted_via_accept_all", "accepted_via_temp_session",
})


@dataclass
class AuditEntry:
    timestamp: str
    week: str
    request_id: str
    connector: str
    tool: str
    tool_name: str
    summary: str
    sender: str
    decision: str           # "approved" | "rejected" | "auto_accepted" | "accepted_via_accept_all" |
                            # "accepted_via_temp_session" | "denied_unattended" | "policy_check" |
                            # "rules_listed" |
                            # "unattended_session_started" | "unattended_session_ended" |
                            # "rule_changed_via_bridge_proposal" | "rule_removed_via_bridge_proposal" |
                            # "grant_changed_via_bridge_proposal" | "grant_removed_via_bridge_proposal" |
                            # "error"
                            # ("error": gate.py's gated_call exited without reaching a normal decision
                            #  branch -- a fallback so an unanticipated failure still leaves a trail)
                            # ("denied_unattended": gate.py denied the call without ever prompting,
                            #  because the connection was in an unattended session and no auto-accept
                            #  rule matched -- distinct from "rejected", which is a human's own Deny.
                            #  Also used by gate.py's propose_rule_change() for the same reason)
                            # ("policy_check": ipc_server.py's check_policy handler -- a preflight
                            #  question, not a real decision; recorded for pattern-spotting only)
                            # ("rules_listed": ipc_server.py's list_rules handler -- not a decision
                            #  either, but the full current rule/grant set was disclosed, worth its
                            #  own record for the same pattern-spotting reason as "policy_check")
                            # ("unattended_session_started"/"_ended": ipc_server.py's begin/end_
                            #  unattended_session handlers, and the same on disconnect cleanup --
                            #  this connection's gate posture changed, which is worth a record of
                            #  its own even though no specific tool call was involved)
                            # ("rule_changed_via_bridge_proposal"/"rule_removed_via_bridge_proposal"/
                            #  "grant_changed_via_bridge_proposal"/"grant_removed_via_bridge_proposal":
                            #  gate.py's propose_rule_change() -- a bridge-initiated auto_accept_rules/
                            #  auto_accept_grants edit that a human confirmed via the same
                            #  show_rule_confirmation_popup() the "Always allow" flow uses. "rejected"
                            #  is reused, not a new value, when the human declines instead)
    auto_accept_rule: str   # rule name if auto_accepted, else ""
    latency_seconds: float
    pii_detected: bool = False  # True if pii_detector.py flagged the content before this decision
    claude_reason: str = ""  # Claude's self-reported reason for the call, from the mandatory
                              # "reason" ToolSpec param every gated/auto tool now declares (see
                              # gate.py's reason_scope), or the "reason" param on the three
                              # privacyfence_* meta-tools for "policy_check"/
                              # "unattended_session_started"/"_ended" entries, which have no
                              # underlying gated tool call to take it from otherwise (see
                              # ipc_server.py's _audit_policy_check/_audit_unattended_session_event).
                              # Self-reported and unverified -- never treated as fact. Empty for
                              # the automatic session-end-on-disconnect path, which has no reason
                              # to attribute.


class AuditLogger:
    def __init__(self, log_dir: str) -> None:
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, entry: AuditEntry) -> None:
        week_file = self._log_dir / f"{entry.week}.jsonl"
        line = json.dumps(asdict(entry)) + "\n"
        with self._lock:
            with open(week_file, "a", encoding="utf-8") as fh:
                fh.write(line)
        logger.debug("Audit: %s %s/%s", entry.decision, entry.connector, entry.tool)

    def export_week_to_excel(self, week: str) -> str | None:
        """Export one week's .jsonl to .xlsx, overwriting any existing file.

        Callers that only want to fill in weeks that have never been
        exported should use export_all_pending() instead.
        """
        try:
            import openpyxl
            from openpyxl.styles import Alignment, Font, PatternFill
            from openpyxl.utils import get_column_letter
        except ImportError:
            logger.warning("openpyxl not installed — skipping Excel audit export")
            return None

        week_file = self._log_dir / f"{week}.jsonl"
        if not week_file.exists():
            return None

        entries: list[AuditEntry] = []
        with open(week_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        entries.append(AuditEntry(**json.loads(line)))
                    except Exception:
                        pass
        if not entries:
            return None

        output_path = str(self._log_dir / f"{week}.xlsx")
        wb = openpyxl.Workbook()

        # ── Main sheet ────────────────────────────────────────────────────
        ws = wb.active
        ws.title = "Decisions"

        HEADERS = [
            "Timestamp", "Week", "Connector", "Tool", "Human-Readable Name",
            "Summary", "Sender / Context", "Decision", "Auto-Accept Rule", "Latency (s)",
            "PII Detected", "Claude's Reason (unverified)",
        ]
        COL_WIDTHS = [22, 10, 12, 30, 22, 55, 30, 14, 22, 12, 12, 55]

        hdr_font  = Font(bold=True, color="FFFFFF")
        hdr_fill  = PatternFill("solid", fgColor="2D4A6B")
        hdr_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

        decision_fills = {
            "approved":              PatternFill("solid", fgColor="E8F5E9"),
            "auto_accepted":         PatternFill("solid", fgColor="E3F2FD"),
            "accepted_via_accept_all": PatternFill("solid", fgColor="FFF3CD"),
            "accepted_via_temp_session": PatternFill("solid", fgColor="FFF3CD"),
            "rejected":              PatternFill("solid", fgColor="FFEBEE"),
            "denied_unattended":     PatternFill("solid", fgColor="FFD8A8"),
            "policy_check":          PatternFill("solid", fgColor="F1F3F5"),
            "rules_listed":          PatternFill("solid", fgColor="F1F3F5"),
            "rule_changed_via_bridge_proposal":   PatternFill("solid", fgColor="FFF3CD"),
            "rule_removed_via_bridge_proposal":   PatternFill("solid", fgColor="FFF3CD"),
            "grant_changed_via_bridge_proposal":  PatternFill("solid", fgColor="FFF3CD"),
            "grant_removed_via_bridge_proposal":  PatternFill("solid", fgColor="FFF3CD"),
            "error":                 PatternFill("solid", fgColor="FF6B6B"),
        }

        ws.append(HEADERS)
        for col, _ in enumerate(HEADERS, 1):
            c = ws.cell(row=1, column=col)
            c.font = hdr_font
            c.fill = hdr_fill
            c.alignment = hdr_align

        for entry in entries:
            ws.append([
                entry.timestamp, entry.week, entry.connector, entry.tool,
                entry.tool_name, entry.summary, entry.sender, entry.decision,
                entry.auto_accept_rule or "", round(entry.latency_seconds, 2),
                "Yes" if entry.pii_detected else "", entry.claude_reason or "",
            ])
            fill = decision_fills.get(entry.decision, PatternFill())
            for col in range(1, len(HEADERS) + 1):
                ws.cell(row=ws.max_row, column=col).fill = fill

        for col, width in enumerate(COL_WIDTHS, 1):
            ws.column_dimensions[get_column_letter(col)].width = width

        ws.auto_filter.ref = ws.dimensions
        ws.freeze_panes = "A2"

        # ── Summary sheet ─────────────────────────────────────────────────
        ws2 = wb.create_sheet("Summary")
        ws2.append(["Metric", "Value"])
        ws2.append(["Week", week])
        ws2.append(["Total decisions", len(entries)])
        counts = Counter(e.decision for e in entries)
        ws2.append(["Approved (manual)", counts.get("approved", 0)])
        ws2.append(["Auto-accepted", counts.get("auto_accepted", 0)])
        ws2.append(["Accepted via Always allow (new rule)", counts.get("accepted_via_accept_all", 0)])
        ws2.append(["Accepted via \"Allow for 5 min\"", counts.get("accepted_via_temp_session", 0)])
        ws2.append(["Rejected", counts.get("rejected", 0)])
        ws2.append(["Denied unattended (no human asked)", counts.get("denied_unattended", 0)])
        ws2.append(["Preflight checks (privacyfence_check_policy)", counts.get("policy_check", 0)])
        ws2.append(["PII flagged (any decision)", sum(1 for e in entries if e.pii_detected)])
        ws2.append([])
        ws2.append(["By connector", ""])
        for connector, cnt in sorted(Counter(e.connector for e in entries).items()):
            ws2.append([connector, cnt])
        ws2.column_dimensions["A"].width = 24
        ws2.column_dimensions["B"].width = 14

        wb.save(output_path)
        logger.info("Audit Excel exported: %s (%d entries)", output_path, len(entries))
        return output_path

    def export_all_pending(self) -> None:
        """Export any week that has .jsonl but no .xlsx."""
        for jsonl in sorted(self._log_dir.glob("*.jsonl")):
            week = jsonl.stem
            xlsx = self._log_dir / f"{week}.xlsx"
            if not xlsx.exists():
                self.export_week_to_excel(week)

    def recent_matches(self, connector: str, tool: str, summary: str, *, week: str | None = None) -> int:
        """Count prior approved-like decisions (see _APPROVED_LIKE_DECISIONS)
        for the same (connector, tool, summary) in one week's log --
        defaults to the current week. The request-fingerprint feature:
        "you've approved this exact request N times this week," so a
        reviewer can spot an unusually novel request versus a routine
        repeat at a glance.

        (connector, tool, summary) is a practical proxy for "the same
        request" -- AuditEntry carries neither an operation_key nor the
        full preview dict, and summary already names the specific resource
        for most tools (e.g. "Read email: Confidential Q3 numbers", 'Read
        "Budget.xlsx"'). A coarser or finer fingerprint can replace this
        later without changing the caller-facing count semantics.
        """
        week_file = self._log_dir / f"{week or current_week()}.jsonl"
        if not week_file.exists():
            return 0
        count = 0
        with open(week_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    data.get("connector") == connector
                    and data.get("tool") == tool
                    and data.get("summary") == summary
                    and data.get("decision") in _APPROVED_LIKE_DECISIONS
                ):
                    count += 1
        return count


def current_week() -> str:
    iso = datetime.now(timezone.utc).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


_INSTANCE: AuditLogger | None = None
_LOCK = threading.Lock()


def get_audit_logger() -> AuditLogger:
    global _INSTANCE
    if _INSTANCE is None:
        with _LOCK:
            if _INSTANCE is None:
                fallback = os.path.join(os.path.expanduser("~"), ".privacyfence", "audit")
                _INSTANCE = AuditLogger(fallback)
    return _INSTANCE


def init_audit_logger(log_dir: str) -> AuditLogger:
    global _INSTANCE
    with _LOCK:
        _INSTANCE = AuditLogger(log_dir)
    return _INSTANCE
