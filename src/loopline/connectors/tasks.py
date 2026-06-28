"""Google Tasks connector: all tools are auto-approved."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..tasks_client import TasksClient, TasksClientError

logger = logging.getLogger(__name__)


class TasksConnector(Connector):
    def __init__(self, client: TasksClient) -> None:
        self._tasks = client

    @property
    def name(self) -> str:
        return "tasks"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="tasks_list_task_lists",
                description="List all Google Task lists. Always allowed.",
                params=[],
            read_only=True,
            ),
            ToolSpec(
                name="tasks_list_tasks",
                description="List tasks in a task list. Always allowed.",
                params=[
                    ToolParam("task_list_id", "str"),
                    ToolParam("show_completed", "bool", required=False, default=False),
                ],
            read_only=True,
            ),
            ToolSpec(
                name="tasks_get_task",
                description="Fetch a single task by id. Always allowed.",
                params=[
                    ToolParam("task_list_id", "str"),
                    ToolParam("task_id", "str"),
                ],
            read_only=True,
            ),
            ToolSpec(
                name="tasks_create_task",
                description="Create a new task. Always allowed.",
                params=[
                    ToolParam("task_list_id", "str"),
                    ToolParam("title", "str"),
                    ToolParam("notes", "str", required=False, default=""),
                    ToolParam("due", "str", required=False, default="",
                              description="Due date in RFC 3339 format"),
                ],
            ),
            ToolSpec(
                name="tasks_update_task",
                description="Update a task's title, notes, or due date. Always allowed.",
                params=[
                    ToolParam("task_list_id", "str"),
                    ToolParam("task_id", "str"),
                    ToolParam("title", "str", required=False, default=""),
                    ToolParam("notes", "str", required=False, default=""),
                    ToolParam("due", "str", required=False, default=""),
                ],
            ),
            ToolSpec(
                name="tasks_complete_task",
                description="Mark a task as completed. Always allowed.",
                params=[
                    ToolParam("task_list_id", "str"),
                    ToolParam("task_id", "str"),
                ],
            ),
            ToolSpec(
                name="tasks_uncomplete_task",
                description="Mark a task as not completed. Always allowed.",
                params=[
                    ToolParam("task_list_id", "str"),
                    ToolParam("task_id", "str"),
                ],
            ),
            ToolSpec(
                name="tasks_move_task",
                description="Move a task from one list to another. Always allowed.",
                params=[
                    ToolParam("source_list_id", "str"),
                    ToolParam("task_id", "str"),
                    ToolParam("destination_list_id", "str"),
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
            return await self._run("tasks_create_task", "Create Task", f"Create task: {args.get('title', '')}", self._tasks.create_task, args.get("task_list_id", ""), args.get("title", ""), args.get("notes", ""), args.get("due", ""))
        if tool == "tasks_update_task":
            title = args.get("title") or None
            notes = args.get("notes") or None
            due = args.get("due") or None
            return await self._run("tasks_update_task", "Update Task", f"Update task {args.get('task_id', '')}", self._tasks.update_task, args.get("task_list_id", ""), args.get("task_id", ""), title, notes, due)
        if tool == "tasks_complete_task":
            return await self._run("tasks_complete_task", "Complete Task", f"Complete task {args.get('task_id', '')}", self._tasks.complete_task, args.get("task_list_id", ""), args.get("task_id", ""))
        if tool == "tasks_uncomplete_task":
            return await self._run("tasks_uncomplete_task", "Uncomplete Task", f"Uncomplete task {args.get('task_id', '')}", self._tasks.uncomplete_task, args.get("task_list_id", ""), args.get("task_id", ""))
        if tool == "tasks_move_task":
            return await self._run("tasks_move_task", "Move Task", f"Move task {args.get('task_id', '')}", self._tasks.move_task, args.get("source_list_id", ""), args.get("task_id", ""), args.get("destination_list_id", ""))
        raise ValueError(f"Unknown Tasks tool: {tool!r}")

    async def _run(self, tool: str, tool_name: str, summary: str, func, *func_args) -> Any:
        t0 = time.time()
        try:
            result = await asyncio.to_thread(func, *func_args)
        except TasksClientError as exc:
            logger.error("Tasks call failed: %s", exc)
            raise RuntimeError(str(exc)) from exc

        # Serialize dataclasses
        if isinstance(result, list):
            serialized = [asdict(r) for r in result]
        elif hasattr(result, "__dataclass_fields__"):
            serialized = asdict(result)
        else:
            serialized = result

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
                latency_seconds=time.time() - t0,
            ))
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)

        logger.info("%s auto-approved", tool)
        return serialized
