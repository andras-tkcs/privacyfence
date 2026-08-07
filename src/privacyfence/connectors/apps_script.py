"""Google Apps Script connector: read/write a script project's *source*, and
read back the result of a run the user triggered themselves.

Running a script is deliberately **out of scope** -- there is no
``apps_script_run``/execute tool here, and there never should be one added
without its own threat-model discussion. Apps Script's own runtime is opaque
to PrivacyFence once a script starts running server-side (no per-call
visibility into what Drive/other APIs it touches), so a "run this script"
popup could only ever be a blank-check approval, unlike every other gated
tool in this codebase, which shows the actual object/content being touched.
The user runs the script themselves in the Apps Script editor (or via its
own triggers), under their own Google account, through Apps Script's own
separate one-time consent screen -- untouched by PrivacyFence, same as
today. See issue #154's "Non-goals" for the full reasoning.

``apps_script_list_projects`` is auto-approved metadata only (id/name/
modified time), mirroring drive_list_shared_drives. ``apps_script_get_content``
and ``apps_script_get_execution_log`` are review-gated reads (script source
can embed sensitive constants/URLs; execution results came from a run the
user triggered, not from Claude). ``apps_script_write_content`` is a
popup-gated write with no configurable auto-accept rule yet (Allow-once-only
at first cut -- see issue #154 open question 2), matching how most new write
tools have shipped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from ..apps_script_client import AppsScriptClient, AppsScriptClientError, ScriptFile
from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..gate import current_reason, gated_call

logger = logging.getLogger(__name__)


def _parse_files_json(value: str) -> list[dict] | None:
    """Parse a JSON array of {"name", "type", "source"} file objects for
    apps_script_write_content, or None if the value isn't valid JSON or
    doesn't have that shape -- checked here so a malformed call fails with a
    clear ValueError before ever reaching the approval popup, same reasoning
    as drive.py's _parse_json_2d_list/_sheets_format_range's early range
    check."""
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, list) or not parsed:
        return None
    for item in parsed:
        if not isinstance(item, dict) or not item.get("name") or not item.get("type"):
            return None
    return parsed


def _format_files(files: list[ScriptFile] | list[dict]) -> str:
    """Render a script project's files as readable text, each preceded by a
    '=== name (type) ===' header -- shared by apps_script_get_content's
    review-gate details_text and apps_script_write_content's popup-gate
    details_text, so both tools show the same shape for the same data.
    Accepts either ScriptFile dataclass instances (get_content's return
    shape) or plain dicts (write_content's parsed argument shape)."""
    parts = []
    for f in files:
        is_dataclass = isinstance(f, ScriptFile)
        name = f.name if is_dataclass else f.get("name", "")
        file_type = f.type if is_dataclass else f.get("type", "")
        source = f.source if is_dataclass else f.get("source", "")
        parts.append(f"=== {name} ({file_type}) ===\n{source}")
    return "\n\n".join(parts)


class AppsScriptConnector(Connector):
    def __init__(self, client: AppsScriptClient) -> None:
        self._apps_script = client

    @property
    def name(self) -> str:
        return "apps_script"

    @property
    def client(self) -> AppsScriptClient:
        return self._apps_script

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="apps_script_list_projects",
                description=(
                    "List standalone Google Apps Script projects visible to "
                    "the user (id, name, last-modified time). Container-bound "
                    "scripts attached to a Sheet/Doc/Form are not returned. "
                    "Auto-approved."
                ),
                params=[
                    ToolParam("max_results", "int", required=False, default=50),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="apps_script_get_content",
                description=(
                    "Fetch the full source of a Google Apps Script project -- "
                    "every file (.gs/.html) plus the appsscript.json manifest. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("script_id", "str"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="apps_script_write_content",
                description=(
                    "Write new source to a Google Apps Script project. "
                    "Replaces the project's entire file set -- there is no "
                    "single-file/partial update, so always pass every file "
                    "the project should have afterward, not just the ones "
                    "you changed (fetch the current set with "
                    "apps_script_get_content first if you need to preserve "
                    "files you aren't touching). This only writes source -- "
                    "PrivacyFence never runs the script; the user runs it "
                    "themselves in the Apps Script editor once this write is "
                    "approved. Requires user approval."
                ),
                params=[
                    ToolParam("script_id", "str"),
                    ToolParam(
                        "files", "str",
                        description=(
                            'JSON array of {"name": str, "type": "SERVER_JS"|'
                            '"HTML"|"JSON", "source": str}, one entry per file '
                            '(a JSON manifest file is named "appsscript" with '
                            'type "JSON").'
                        ),
                    ),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="apps_script_get_execution_log",
                description=(
                    "Read the result of the most recent run(s) of a script "
                    "that the user triggered themselves outside PrivacyFence "
                    "(status, duration, which function ran) -- not a live "
                    "console.log transcript. Requires user approval."
                ),
                params=[
                    ToolParam("script_id", "str"),
                    ToolParam("max_results", "int", required=False, default=10),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "apps_script_list_projects":
            return await self._list_projects(**args)
        if tool == "apps_script_get_content":
            return await self._get_content(**args)
        if tool == "apps_script_write_content":
            return await self._write_content(**args)
        if tool == "apps_script_get_execution_log":
            return await self._get_execution_log(**args)
        raise ValueError(f"Unknown Apps Script tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Auto
    # ------------------------------------------------------------------ #

    async def _list_projects(self, max_results: int = 50) -> Any:
        t0 = time.time()
        projects = await self._fetch(self._apps_script.list_projects, max_results)
        self._auto_audit(
            "apps_script_list_projects", "List Apps Script Projects",
            f"List projects (max {max_results})", f"{len(projects)} project(s)", t0,
        )
        return [asdict(p) for p in projects]

    # ------------------------------------------------------------------ #
    # Review gate (reads)
    # ------------------------------------------------------------------ #

    async def _get_content(self, script_id: str) -> Any:
        content = await self._fetch(self._apps_script.get_content, script_id)
        metadata = await self._fetch(self._apps_script.get_project_metadata, script_id)
        name = metadata.name or script_id
        rendered = _format_files(content.files)
        preview = {"Project": name, "Files": f"{len(content.files)} file(s)"}
        filtered = {"script_id": script_id, "files": [asdict(f) for f in content.files]}
        return await gated_call(
            connector=self.name,
            tool="apps_script_get_content",
            tool_name="Read Apps Script Project",
            summary=f"Read \"{name}\"",
            sender="",
            raw_data=content,
            filtered_data=filtered,
            gate="review",
            preview=preview,
            details_text=rendered,
            pii_scan_text=rendered[:2000],
            args={"script_id": script_id},
        )

    async def _get_execution_log(self, script_id: str, max_results: int = 10) -> Any:
        metadata = await self._fetch(self._apps_script.get_project_metadata, script_id)
        name = metadata.name or script_id
        executions = await self._fetch(self._apps_script.get_execution_log, script_id, max_results)
        preview = {"Project": name, "Executions": f"{len(executions)} run(s)"}
        # v2's right pane: a real table (Function/Status/Start time/Duration)
        # instead of a comma-joined text dump -- same treatment
        # drive_sheets_get_values's own table gets. table_only since
        # details_text (kept for legacy display and the PII scan's default
        # fallback) would otherwise show the exact same rows twice.
        table = {
            "headers": ["Function", "Status", "Start time", "Duration"],
            "rows": [[e.function_name, e.status, e.start_time, e.duration] for e in executions],
        }
        rendered = "\n".join(
            f"{e.function_name}: {e.status} (started {e.start_time}, took {e.duration})"
            for e in executions
        ) or "No executions found."
        filtered = [asdict(e) for e in executions]
        return await gated_call(
            connector=self.name,
            tool="apps_script_get_execution_log",
            tool_name="Read Apps Script Execution Log",
            summary=f"Read execution log for \"{name}\"",
            sender="",
            raw_data=executions,
            filtered_data=filtered,
            gate="review",
            preview=preview,
            details_text=rendered,
            pii_scan_text=rendered[:2000],
            preview_tables=[table] if executions else [],
            table_only=True,
            args={"script_id": script_id, "max_results": max_results},
        )

    # ------------------------------------------------------------------ #
    # Popup gate (writes)
    # ------------------------------------------------------------------ #

    async def _write_content(self, script_id: str, files: str) -> Any:
        parsed = _parse_files_json(files)
        if parsed is None:
            raise ValueError(
                "apps_script_write_content: 'files' must be a JSON array of "
                '{"name","type","source"} objects, e.g. '
                '[{"name":"Code","type":"SERVER_JS","source":"function foo() {}"}]'
            )
        metadata = await self._fetch(self._apps_script.get_project_metadata, script_id)
        name = metadata.name or script_id
        preview = {
            "Project": name,
            "Files": ", ".join(f"{f.get('name', '')} ({f.get('type', '')})" for f in parsed),
        }
        rendered = _format_files(parsed)
        await gated_call(
            connector=self.name,
            tool="apps_script_write_content",
            tool_name="Write Apps Script Project",
            summary=f"Write new source to \"{name}\"",
            sender="",
            raw_data={"script_id": script_id, "files_preview": rendered[:200]},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=rendered,
            args={"script_id": script_id},
        )
        return await self._fetch(self._apps_script.write_content, script_id, parsed)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _fetch(self, func, *args) -> Any:
        try:
            return await asyncio.to_thread(func, *args)
        except AppsScriptClientError as exc:
            logger.error("Apps Script fetch failed: %s", exc)
            raise RuntimeError(str(exc)) from exc

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
                claude_reason=current_reason(),
            ))
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)
