"""Salesforce connector: wraps SalesforceClient + gated_call."""

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
                description="List Salesforce reports accessible to the user. Always allowed.",
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
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "salesforce_list_reports":
            return await self._list_reports()
        if tool == "salesforce_get_record":
            return await self._get_record(**args)
        if tool == "salesforce_run_report":
            return await self._run_report(**args)
        raise ValueError(f"Unknown Salesforce tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Always-allowed
    # ------------------------------------------------------------------ #

    async def _list_reports(self) -> Any:
        t0 = time.time()
        try:
            reports = await asyncio.to_thread(self._sf.list_reports)
        except SalesforceClientError as exc:
            raise RuntimeError(str(exc)) from exc
        result = [asdict(r) for r in reports]
        self._auto_audit(
            "salesforce_list_reports", "List Salesforce Reports",
            "List all reports", f"{len(reports)} report(s)", t0,
        )
        return result

    # ------------------------------------------------------------------ #
    # Gated
    # ------------------------------------------------------------------ #

    async def _get_record(self, object_type: str, record_id: str) -> Any:
        try:
            record = await asyncio.to_thread(self._sf.get_record, object_type, record_id)
        except SalesforceClientError as exc:
            raise RuntimeError(str(exc)) from exc
        filtered_data = asdict(record)
        return await gated_call(
            connector=self.name,
            tool="salesforce_get_record",
            tool_name="Read Salesforce Record",
            summary=f"Read {object_type} record",
            sender=object_type,
            raw_data=record,
            filtered_data=filtered_data,
            my_email=self.my_email,
            args={"object_type": object_type, "record_id": record_id},
        )

    async def _run_report(self, report_id: str) -> Any:
        try:
            result = await asyncio.to_thread(self._sf.run_report, report_id)
        except SalesforceClientError as exc:
            raise RuntimeError(str(exc)) from exc
        return await gated_call(
            connector=self.name,
            tool="salesforce_run_report",
            tool_name="Run Salesforce Report",
            summary=f"Run report {report_id[:20]}{'…' if len(report_id) > 20 else ''}",
            sender="Salesforce",
            raw_data=result,
            filtered_data=result,
            my_email=self.my_email,
            args={"report_id": report_id},
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
                auto_accept_rule="always_allowed",
                latency_seconds=time.time() - created_at,
            ))
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)
