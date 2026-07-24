"""Google Tasks connector.

Reads (list task lists, list tasks, get a task) are low-sensitivity metadata
and stay auto-approved, like every other connector's read-only listing calls.
The one exception is a task's free-text `notes` field, which is filtered
through tasks_privacy's "notes" category (see privacy_filter.py) before
being returned -- unlike title/due/status, notes can carry arbitrary
personal content.
Writes (create/update/complete/uncomplete/move) go through the popup gate,
same as every other connector's writes — this connector used to auto-approve
everything, including writes, which was the one connector whose behavior
didn't match the documented default ("writes require review/popup").
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..gate import current_reason, gated_call
from ..privacy_filter import apply_text
from ..tasks_client import TasksClient, TasksClientError

logger = logging.getLogger(__name__)


class TasksConnector(Connector):
    def __init__(self, client: TasksClient) -> None:
        self._tasks = client
        self._list_name_cache: dict[str, str] = {}

    @property
    def client(self) -> TasksClient:
        return self._tasks

    @property
    def name(self) -> str:
        return "tasks"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="tasks_list_task_lists",
                description="List all Google Task lists. Auto-approved.",
                params=[ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?")],
                read_only=True,
            ),
            ToolSpec(
                name="tasks_list_tasks",
                description="List tasks in a task list. Auto-approved.",
                params=[
                    ToolParam("task_list_id", "str"),
                    ToolParam("show_completed", "bool", required=False, default=False),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="tasks_get_task",
                description="Fetch a single task by id. Auto-approved.",
                params=[
                    ToolParam("task_list_id", "str"),
                    ToolParam("task_id", "str"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="tasks_create_task",
                description="Create a new task. Requires user approval.",
                params=[
                    ToolParam("task_list_id", "str"),
                    ToolParam("title", "str"),
                    ToolParam("notes", "str", required=False, default=""),
                    ToolParam("due", "str", required=False, default="",
                              description="Due date in RFC 3339 format"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="tasks_update_task",
                description="Update a task's title, notes, or due date. Requires user approval.",
                params=[
                    ToolParam("task_list_id", "str"),
                    ToolParam("task_id", "str"),
                    ToolParam("title", "str", required=False, default=""),
                    ToolParam("notes", "str", required=False, default=""),
                    ToolParam("due", "str", required=False, default=""),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="tasks_complete_task",
                description="Mark a task as completed. Requires user approval.",
                params=[
                    ToolParam("task_list_id", "str"),
                    ToolParam("task_id", "str"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="tasks_uncomplete_task",
                description="Mark a task as not completed. Requires user approval.",
                params=[
                    ToolParam("task_list_id", "str"),
                    ToolParam("task_id", "str"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="tasks_move_task",
                description="Move a task from one list to another. Requires user approval.",
                params=[
                    ToolParam("source_list_id", "str"),
                    ToolParam("task_id", "str"),
                    ToolParam("destination_list_id", "str"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "tasks_list_task_lists":
            return await self._run("tasks_list_task_lists", "List Task Lists", "List all task lists", self._tasks.list_task_lists)
        if tool == "tasks_list_tasks":
            return await self._run("tasks_list_tasks", "List Tasks", f"List tasks in {args.get('task_list_id', '')}", self._tasks.list_tasks, args.get("task_list_id", ""), bool(args.get("show_completed", False)))
        if tool == "tasks_get_task":
            return await self._run("tasks_get_task", "Get Task", f"Get task {args.get('task_id', '')}", self._tasks.get_task, args.get("task_list_id", ""), args.get("task_id", ""))
        if tool == "tasks_create_task":
            return await self._create_task(**args)
        if tool == "tasks_update_task":
            return await self._update_task(**args)
        if tool == "tasks_complete_task":
            return await self._complete_task(**args)
        if tool == "tasks_uncomplete_task":
            return await self._uncomplete_task(**args)
        if tool == "tasks_move_task":
            return await self._move_task(**args)
        raise ValueError(f"Unknown Tasks tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Auto (no gate) — read-only metadata
    # ------------------------------------------------------------------ #

    async def _run(self, tool: str, tool_name: str, summary: str, func, *func_args) -> Any:
        t0 = time.time()
        result = await self._fetch(func, *func_args)
        self._auto_audit(tool, tool_name, summary, t0)
        # Only the read path -- a write's result dict echoes back notes
        # Claude just wrote itself, so there's nothing to redact there.
        return _redact_notes(self._serialize(result))

    # ------------------------------------------------------------------ #
    # Popup gate (writes)
    # ------------------------------------------------------------------ #

    async def _create_task(
        self, task_list_id: str, title: str, notes: str = "", due: str = ""
    ) -> Any:
        preview = {"Task list": await self._list_name_for(task_list_id), "Title": title}
        if due:
            preview["Due"] = due
        await gated_call(
            connector=self.name,
            tool="tasks_create_task",
            tool_name="Create Task",
            summary=f"Create task: {title}",
            sender="",
            raw_data={"task_list_id": task_list_id, "title": title, "notes": notes, "due": due},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=notes or "No notes provided; see preview for task details.",
            args={"task_list_id": task_list_id, "title": title},
        )
        result = await self._fetch(self._tasks.create_task, task_list_id, title, notes, due)
        return self._serialize(result)

    async def _update_task(
        self, task_list_id: str, task_id: str, title: str = "", notes: str = "", due: str = ""
    ) -> Any:
        existing = await self._fetch(self._tasks.get_task, task_list_id, task_id)
        preview = {"Task list": await self._list_name_for(task_list_id), "Task": existing.title}
        if title:
            preview["New title"] = title
        if due:
            preview["New due"] = due
        if notes and notes != existing.notes:
            details_text = notes
        else:
            changed_fields = ", ".join(k for k in preview if k not in ("Task list", "Task")) or "no fields"
            details_text = f"{changed_fields} will be updated; notes unchanged."
        await gated_call(
            connector=self.name,
            tool="tasks_update_task",
            tool_name="Update Task",
            summary=f"Update task: {existing.title}",
            sender="",
            raw_data=existing,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=details_text,
            args={"task_list_id": task_list_id, "task_id": task_id},
        )
        result = await self._fetch(
            self._tasks.update_task, task_list_id, task_id,
            title or None, notes or None, due or None,
        )
        return self._serialize(result)

    async def _complete_task(self, task_list_id: str, task_id: str) -> Any:
        existing = await self._fetch(self._tasks.get_task, task_list_id, task_id)
        preview = {"Task list": await self._list_name_for(task_list_id), "Task": existing.title}
        await gated_call(
            connector=self.name,
            tool="tasks_complete_task",
            tool_name="Complete Task",
            summary=f"Complete task: {existing.title}",
            sender="",
            raw_data=existing,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text="Task will be marked as completed; title and notes are unchanged.",
            args={"task_list_id": task_list_id, "task_id": task_id},
        )
        result = await self._fetch(self._tasks.complete_task, task_list_id, task_id)
        return self._serialize(result)

    async def _uncomplete_task(self, task_list_id: str, task_id: str) -> Any:
        existing = await self._fetch(self._tasks.get_task, task_list_id, task_id)
        preview = {"Task list": await self._list_name_for(task_list_id), "Task": existing.title}
        await gated_call(
            connector=self.name,
            tool="tasks_uncomplete_task",
            tool_name="Uncomplete Task",
            summary=f"Uncomplete task: {existing.title}",
            sender="",
            raw_data=existing,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text="Task will be marked as not completed; title and notes are unchanged.",
            args={"task_list_id": task_list_id, "task_id": task_id},
        )
        result = await self._fetch(self._tasks.uncomplete_task, task_list_id, task_id)
        return self._serialize(result)

    async def _move_task(
        self, source_list_id: str, task_id: str, destination_list_id: str
    ) -> Any:
        existing = await self._fetch(self._tasks.get_task, source_list_id, task_id)
        preview = {
            "Task": existing.title,
            "From list": await self._list_name_for(source_list_id),
            "To list": await self._list_name_for(destination_list_id),
        }
        await gated_call(
            connector=self.name,
            tool="tasks_move_task",
            tool_name="Move Task",
            summary=f"Move task: {existing.title}",
            sender="",
            raw_data=existing,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text="Task will be moved to the new list; title and notes are unchanged.",
            args={
                "source_list_id": source_list_id,
                "task_id": task_id,
                "destination_list_id": destination_list_id,
            },
        )
        result = await self._fetch(self._tasks.move_task, source_list_id, task_id, destination_list_id)
        return self._serialize(result)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _fetch(self, func, *args) -> Any:
        try:
            return await asyncio.to_thread(func, *args)
        except TasksClientError as exc:
            logger.error("Tasks call failed: %s", exc)
            raise RuntimeError(str(exc)) from exc

    async def _list_name_for(self, task_list_id: str) -> str:
        """Best-effort, cached task-list title lookup; falls back to the raw
        id (e.g. the list was deleted, or a permissions edge case) rather
        than blocking the popup on a lookup that can't succeed."""
        if task_list_id in self._list_name_cache:
            return self._list_name_cache[task_list_id]
        try:
            task_list = await self._fetch(self._tasks.get_task_list, task_list_id)
            name = task_list.title or task_list_id
        except RuntimeError:
            name = task_list_id
        self._list_name_cache[task_list_id] = name
        return name

    @staticmethod
    def _serialize(result: Any) -> Any:
        if isinstance(result, list):
            return [asdict(r) for r in result]
        if hasattr(result, "__dataclass_fields__"):
            return asdict(result)
        return result

    def _auto_audit(self, tool: str, tool_name: str, summary: str, created_at: float) -> None:
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id="",
                connector=self.name,
                tool=tool,
                tool_name=tool_name,
                summary=summary,
                sender="",
                decision="auto_accepted",
                auto_accept_rule="auto",
                latency_seconds=time.time() - created_at,
                claude_reason=current_reason(),
            ))
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)


def _redact_notes(value: Any) -> Any:
    """Apply tasks_privacy's "notes" category to a serialized Task's
    free-text notes field -- the one field on a task that can carry
    arbitrary personal content, unlike title/due/status. A TaskList dict has
    no "notes" key and passes through untouched; a list of either is handled
    recursively."""
    if isinstance(value, list):
        return [_redact_notes(v) for v in value]
    if isinstance(value, dict) and "notes" in value:
        value["notes"] = apply_text("tasks_privacy", "notes", value.get("notes", "") or "")
    return value
