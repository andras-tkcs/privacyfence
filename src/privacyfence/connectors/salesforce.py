"""Salesforce connector."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..gate import gated_call
from ..salesforce_client import SalesforceClient, SalesforceClientError

logger = logging.getLogger(__name__)


def _format_flat_fields(fields: dict[str, Any]) -> str:
    """Render a Salesforce record's (flat, scalar-valued) fields dict as one
    'Field: value' line per field, alphabetized, skipping unset ones."""
    lines = [
        f"{key}: {value}" for key, value in sorted(fields.items())
        if value not in (None, "")
    ]
    return "\n".join(lines) if lines else "(no populated fields)"


def _report_column_labels(result_dict: dict) -> list[str]:
    """Friendly column labels for detail rows, falling back to the raw API
    field names when no extended metadata is present."""
    detail_columns = ((result_dict.get("reportMetadata") or {}).get("detailColumns")) or []
    column_info = ((result_dict.get("reportExtendedMetadata") or {}).get("detailColumnInfo")) or {}
    return [(column_info.get(col) or {}).get("label", col) for col in detail_columns]


def _grouping_label(groupings: list[dict], key: str) -> str | None:
    """Walk a groupingsDown/groupingsAcross tree to resolve the label for a
    (possibly compound, e.g. '0_1' for a nested sub-group) grouping key."""
    label = None
    nodes = groupings
    for part in key.split("_"):
        match = next((g for g in nodes if str(g.get("key")) == part), None)
        if match is None:
            return None
        label = match.get("label") or match.get("value") or part
        nodes = match.get("groupings") or []
    return label


def _report_group_label(result_dict: dict, fact_key: str) -> str:
    """Best-effort human label for a factMap group key like '0!1' (a
    grouping/sub-grouping combination) or 'T!T' (tabular, no grouping)."""
    down_key, _, across_key = fact_key.partition("!")
    parts = []
    if down_key != "T":
        label = _grouping_label(((result_dict.get("groupingsDown") or {}).get("groupings")) or [], down_key)
        if label:
            parts.append(label)
    if across_key != "T":
        label = _grouping_label(((result_dict.get("groupingsAcross") or {}).get("groupings")) or [], across_key)
        if label:
            parts.append(label)
    return " / ".join(parts) if parts else fact_key


def _format_report_rows(rows: list[dict], limit: int = 50) -> str:
    lines = [
        " | ".join(str(cell.get("label", "")) for cell in (row.get("dataCells") or []))
        for row in rows[:limit]
    ]
    text = "\n".join(lines)
    if len(rows) > limit:
        text += f"\n… and {len(rows) - limit} more row(s)"
    return text


def _format_report_details(result_dict: dict) -> str:
    """Render a Salesforce report-run result as plain text tables instead of
    raw JSON. The envelope shape (factMap/groupingsDown/groupingsAcross) is
    Salesforce's documented Analytics REST API response format; if a
    particular report doesn't match these assumptions, falls back to a
    short plain-language summary rather than a technical dump. Report name/id
    are shown in the preview, not repeated here."""
    try:
        fact_map = result_dict.get("factMap") if isinstance(result_dict, dict) else None
        if not isinstance(fact_map, dict) or not fact_map:
            return "No data returned."
        columns = _report_column_labels(result_dict)
        sections = []
        for fact_key in sorted(fact_map.keys()):
            group = fact_map[fact_key] or {}
            body_lines = []
            if columns and fact_key == "T!T":
                body_lines.append(" | ".join(columns))
            body_lines.append(_format_report_rows(group.get("rows") or []))
            aggregates = group.get("aggregates") or []
            if aggregates:
                body_lines.append("Total: " + " | ".join(str(a.get("label", "")) for a in aggregates))
            body = "\n".join(body_lines)
            sections.append(body if fact_key == "T!T" else f"{_report_group_label(result_dict, fact_key)}\n{body}")
        return "\n\n".join(sections)
    except Exception:
        group_count = len(result_dict.get("factMap") or {}) if isinstance(result_dict, dict) else 0
        return (
            f"Report ran successfully — {group_count} data group(s). "
            "Structure too complex to preview here; open in Salesforce to view."
        )


class SalesforceConnector(Connector):
    def __init__(self, client: SalesforceClient) -> None:
        self._sf = client
        self.my_email: str = ""

    @property
    def name(self) -> str:
        return "salesforce"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="salesforce_list_reports",
                description="List Salesforce reports accessible to the user. Auto-approved.",
                params=[],
                read_only=True,
            ),
            ToolSpec(
                name="salesforce_get_record",
                description="Fetch a Salesforce record by object type and id. Requires user approval.",
                params=[
                    ToolParam("object_type", "str", description="e.g. Account, Contact, Opportunity"),
                    ToolParam("record_id", "str"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="salesforce_run_report",
                description="Run a Salesforce report by id and return the results. Requires user approval.",
                params=[ToolParam("report_id", "str")],
                read_only=True,
            ),
            ToolSpec(
                name="salesforce_search",
                description=(
                    "Search Salesforce by name or id across one or more object types — "
                    "the same mechanism as the search bar at the top of the Salesforce "
                    "UI. Returns lightweight Id/Name matches per object type; call "
                    "salesforce_get_record for full field details on a match. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("search_term", "str", description="Name, partial name, or id to search for"),
                    ToolParam(
                        "object_types", "str", required=False, default="",
                        description=(
                            "Comma-separated Salesforce object API names to restrict the "
                            "search to, e.g. 'Opportunity,Contact'. Leave empty to search "
                            "Salesforce's default globally-searchable objects."
                        ),
                    ),
                    ToolParam(
                        "account_id", "str", required=False, default="",
                        description=(
                            "Scope results to this Account's related records (e.g. its "
                            "Opportunities). Requires object_types to be set, since not "
                            "every object has an AccountId field."
                        ),
                    ),
                    ToolParam("max_results", "int", required=False, default=20),
                ],
                read_only=True,
            ),
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "salesforce_list_reports":
            return await self._list_reports()
        if tool == "salesforce_get_record":
            return await self._get_record(**args)
        if tool == "salesforce_run_report":
            return await self._run_report(**args)
        if tool == "salesforce_search":
            return await self._search(**args)
        raise ValueError(f"Unknown Salesforce tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Auto
    # ------------------------------------------------------------------ #

    async def _list_reports(self) -> Any:
        t0 = time.time()
        try:
            reports = await asyncio.to_thread(self._sf.list_reports)
        except SalesforceClientError as exc:
            raise RuntimeError(str(exc)) from exc
        result = [asdict(r) for r in reports]
        self._auto_audit("salesforce_list_reports", "List Salesforce Reports",
                         "List all reports", f"{len(reports)} report(s)", t0)
        return result

    # ------------------------------------------------------------------ #
    # Review gate (reads)
    # ------------------------------------------------------------------ #

    async def _get_record(self, object_type: str, record_id: str) -> Any:
        try:
            record = await asyncio.to_thread(self._sf.get_record, object_type, record_id)
        except SalesforceClientError as exc:
            raise RuntimeError(str(exc)) from exc
        record_dict = asdict(record)
        record_fields = record_dict.get("fields", {})
        name = record_fields.get("Name") or record_fields.get("name") or record_id
        preview = {
            "Object type": object_type,
            "Name": str(name),
            "Record ID": record_id,
        }
        details = f"Fields:\n{_format_flat_fields(record_fields)}"
        return await gated_call(
            connector=self.name,
            tool="salesforce_get_record",
            tool_name="Read Salesforce Record",
            summary=f"Read {object_type}: {name}",
            sender=object_type,
            raw_data=record,
            filtered_data=record_dict,
            gate="review",
            preview=preview,
            details_text=details,
            my_email=self.my_email,
            args={"object_type": object_type, "record_id": record_id},
        )

    async def _run_report(self, report_id: str) -> Any:
        try:
            result = await asyncio.to_thread(self._sf.run_report, report_id)
        except SalesforceClientError as exc:
            raise RuntimeError(str(exc)) from exc
        result_dict = asdict(result) if hasattr(result, "__dataclass_fields__") else result
        # Salesforce's report-run response nests the report's name under
        # reportMetadata.name, not at the top level -- report_dict.get("name")
        # is always None for a real API response.
        report_metadata = result_dict.get("reportMetadata") or {} if isinstance(result_dict, dict) else {}
        report_name = (
            report_metadata.get("name")
            or (result_dict.get("name") or result_dict.get("reportName") if isinstance(result_dict, dict) else None)
            or report_id
        )
        preview = {
            "Report": str(report_name),
            "Report ID": report_id,
        }
        details = _format_report_details(result_dict)
        return await gated_call(
            connector=self.name,
            tool="salesforce_run_report",
            tool_name="Run Salesforce Report",
            summary=f"Run report: {report_name}",
            sender="Salesforce",
            raw_data=result,
            filtered_data=result_dict,
            gate="review",
            preview=preview,
            details_text=details,
            my_email=self.my_email,
            args={"report_id": report_id},
        )

    async def _search(
        self, search_term: str, object_types: str = "", account_id: str = "", max_results: int = 20,
    ) -> Any:
        # Validate before gating, not after -- same reasoning as
        # drive_sheets_insert_dimensions's early dimension check: a doomed
        # call shouldn't cost the user an unnecessary approval decision.
        if account_id and not object_types.strip():
            raise ValueError("salesforce_search: account_id requires object_types to be specified")
        try:
            records = await asyncio.to_thread(
                self._sf.search, search_term, object_types, account_id, max_results
            )
        except SalesforceClientError as exc:
            raise RuntimeError(str(exc)) from exc
        result = [asdict(r) for r in records]
        preview = {
            "Search term": search_term,
            "Object types": object_types or "(default)",
            "Results": str(len(records)),
        }
        if account_id:
            preview["Account ID"] = account_id
        details = "\n".join(
            f"{r.object_type} — {r.fields.get('Name', '(no name)')} (id={r.id})" for r in records
        ) or "(no matches)"
        return await gated_call(
            connector=self.name,
            tool="salesforce_search",
            tool_name="Search Salesforce",
            summary=f"Search: {search_term[:80]}",
            sender="Salesforce",
            raw_data=records,
            filtered_data=result,
            gate="review",
            preview=preview,
            details_text=details,
            my_email=self.my_email,
            args={"search_term": search_term, "object_types": object_types, "account_id": account_id},
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _auto_audit(
        self, tool: str, tool_name: str, summary: str, sender: str, created_at: float
    ) -> None:
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id="",
                connector=self.name,
                tool=tool,
                tool_name=tool_name,
                summary=summary,
                sender=sender,
                decision="auto_accepted",
                auto_accept_rule="auto",
                latency_seconds=time.time() - created_at,
            ))
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)
