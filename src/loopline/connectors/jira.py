"""Jira connector."""
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
from ..jira_client import JiraClient, JiraClientError

logger = logging.getLogger(__name__)


class JiraConnector(Connector):
    def __init__(self, client: JiraClient) -> None:
        self._jira = client
        self.my_email: str = ""

    @property
    def name(self) -> str:
        return "jira"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="jira_list_projects",
                description="List Jira projects accessible to the user (key, name, type, lead). Auto-approved.",
                params=[ToolParam("max_results", "int", required=False, default=50)],
                read_only=True,
            ),
            ToolSpec(
                name="jira_search_issues",
                description=(
                    "Search Jira issues using JQL. Returns summary info for matching issues. Auto-approved."
                ),
                params=[
                    ToolParam("jql", "str", description="e.g. 'project = MYPROJ AND status = Open'"),
                    ToolParam("max_results", "int", required=False, default=20),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="jira_get_issue",
                description=(
                    "Fetch full details of a Jira issue by key (e.g. PROJ-123), "
                    "including description and comments. Requires user approval."
                ),
                params=[ToolParam("issue_key", "str", description="e.g. PROJ-123")],
                read_only=True,
            ),
            ToolSpec(
                name="jira_create_issue",
                description="Create a new Jira issue. Requires user approval.",
                params=[
                    ToolParam("project_key", "str", description="e.g. MYPROJ"),
                    ToolParam("summary", "str"),
                    ToolParam("issue_type", "str", required=False, default="Task",
                              description="e.g. Task, Bug, Story"),
                    ToolParam("description", "str", required=False, default=""),
                    ToolParam("priority", "str", required=False, default="",
                              description="e.g. High, Medium, Low"),
                ],
            ),
            ToolSpec(
                name="jira_add_comment",
                description="Add a comment to an existing Jira issue. Requires user approval.",
                params=[
                    ToolParam("issue_key", "str", description="e.g. PROJ-123"),
                    ToolParam("body", "str", description="Comment text (plain text)"),
                ],
            ),
            ToolSpec(
                name="jira_update_issue",
                description=(
                    "Update fields on an existing Jira issue "
                    "(summary, description, priority). Requires user approval."
                ),
                params=[
                    ToolParam("issue_key", "str"),
                    ToolParam("summary", "str", required=False, default=""),
                    ToolParam("description", "str", required=False, default=""),
                    ToolParam("priority", "str", required=False, default=""),
                ],
            ),
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "jira_list_projects":
            return await self._list_projects(**args)
        if tool == "jira_search_issues":
            return await self._search_issues(**args)
        if tool == "jira_get_issue":
            return await self._get_issue(**args)
        if tool == "jira_create_issue":
            return await self._create_issue(**args)
        if tool == "jira_add_comment":
            return await self._add_comment(**args)
        if tool == "jira_update_issue":
            return await self._update_issue(**args)
        raise ValueError(f"Unknown Jira tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Auto
    # ------------------------------------------------------------------ #

    async def _list_projects(self, max_results: int = 50) -> Any:
        t0 = time.time()
        projects = await self._fetch(self._jira.list_projects, max_results)
        data = [asdict(p) for p in projects]
        self._auto_audit("jira_list_projects", "List Jira Projects",
                         f"List projects (max {max_results})", f"{len(projects)} project(s)", t0)
        return data

    async def _search_issues(self, jql: str, max_results: int = 20) -> Any:
        t0 = time.time()
        issues = await self._fetch(self._jira.search_issues, jql, max_results)
        data = [asdict(i) for i in issues]
        self._auto_audit("jira_search_issues", "Search Jira Issues",
                         f"Search: {jql[:80]}", f"{len(issues)} issue(s)", t0)
        return data

    # ------------------------------------------------------------------ #
    # Review gate (reads)
    # ------------------------------------------------------------------ #

    async def _get_issue(self, issue_key: str) -> Any:
        issue = await self._fetch(self._jira.get_issue, issue_key)
        comments = await self._fetch(self._jira.get_issue_comments, issue_key)
        result = {**asdict(issue), "comments": [asdict(c) for c in comments]}
        preview = {
            "Project": getattr(issue, "project_name", "") or issue_key.split("-")[0],
            "Key": issue.key,
            "Summary": (issue.summary[:80] + "…") if len(issue.summary) > 80 else issue.summary,
            "Status": getattr(issue, "status", "") or "",
            "Assignee": getattr(issue, "assignee", "") or "(unassigned)",
        }
        import json as _json
        details = (
            f"Key: {issue.key}\n"
            f"Summary: {issue.summary}\n"
            f"Status: {getattr(issue, 'status', '')}\n"
            f"Assignee: {getattr(issue, 'assignee', '')}\n"
            f"Reporter: {getattr(issue, 'reporter', '')}\n\n"
            f"Description:\n{getattr(issue, 'description', '') or '(none)'}\n\n"
            f"Comments ({len(comments)}):\n" +
            "\n---\n".join(
                f"{getattr(c, 'author', 'unknown')} [{getattr(c, 'created', '')}]:\n{getattr(c, 'body', '')}"
                for c in comments
            )
        )
        return await gated_call(
            connector=self.name,
            tool="jira_get_issue",
            tool_name="Read Jira Issue",
            summary=f"{issue.key}: {issue.summary[:60]}",
            sender=getattr(issue, "reporter", "") or getattr(issue, "assignee", "") or issue_key,
            raw_data=result,
            filtered_data=result,
            gate="review",
            preview=preview,
            details_text=details,
            my_email=self.my_email,
            args={"issue_key": issue_key},
        )

    # ------------------------------------------------------------------ #
    # Popup gate (writes)
    # ------------------------------------------------------------------ #

    async def _create_issue(
        self,
        project_key: str,
        summary: str,
        issue_type: str = "Task",
        description: str = "",
        priority: str = "",
    ) -> Any:
        details_lines = [
            f"Project: {project_key}",
            f"Type: {issue_type}",
            f"Summary: {summary}",
        ]
        if priority:
            details_lines.append(f"Priority: {priority}")
        if description:
            details_lines += ["", f"Description:\n{description}"]
        preview = {
            "project_key": project_key, "summary": summary,
            "issue_type": issue_type, "description": description, "priority": priority,
        }
        await gated_call(
            connector=self.name,
            tool="jira_create_issue",
            tool_name="Create Jira Issue",
            summary=f"Create {issue_type} in {project_key}: {summary[:60]}",
            sender=f"project={project_key}",
            raw_data=preview,
            filtered_data=None,
            gate="popup",
            details_text="\n".join(details_lines),
            my_email=self.my_email,
            args=preview,
        )
        issue = await self._fetch(
            self._jira.create_issue, project_key, summary, issue_type, description, priority,
        )
        return asdict(issue)

    async def _add_comment(self, issue_key: str, body: str) -> Any:
        issue = await self._fetch(self._jira.get_issue, issue_key)
        details = f"Issue: {issue.key} — {issue.summary}\n\nComment:\n{body}"
        await gated_call(
            connector=self.name,
            tool="jira_add_comment",
            tool_name="Add Jira Comment",
            summary=f"Comment on {issue_key}: {body[:80]}",
            sender=f"issue={issue_key}",
            raw_data={"issue_key": issue_key, "body": body},
            filtered_data=None,
            gate="popup",
            details_text=details,
            my_email=self.my_email,
            args={"issue_key": issue_key, "body": body},
        )
        comment = await self._fetch(self._jira.add_comment, issue_key, body)
        return asdict(comment)

    async def _update_issue(
        self,
        issue_key: str,
        summary: str = "",
        description: str = "",
        priority: str = "",
    ) -> Any:
        issue = await self._fetch(self._jira.get_issue, issue_key)
        fields: dict[str, Any] = {}
        changes: list[str] = []
        if summary:
            fields["summary"] = summary
            changes.append(f"  Summary: {issue.summary} → {summary}")
        if description:
            fields["description"] = {
                "type": "doc", "version": 1,
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": description}]}],
            }
            changes.append("  Description: (updated)")
        if priority:
            fields["priority"] = {"name": priority}
            changes.append(f"  Priority: → {priority}")
        if not fields:
            raise ValueError("update_issue: at least one field must be provided")
        details = f"Issue: {issue.key} — {issue.summary}\n\nChanges:\n" + "\n".join(changes)
        await gated_call(
            connector=self.name,
            tool="jira_update_issue",
            tool_name="Update Jira Issue",
            summary=f"Update {issue_key}: {', '.join(fields.keys())}",
            sender=f"issue={issue_key}",
            raw_data={"issue_key": issue_key, "fields": fields},
            filtered_data=None,
            gate="popup",
            details_text=details,
            my_email=self.my_email,
            args={"issue_key": issue_key},
        )
        updated = await self._fetch(self._jira.update_issue, issue_key, fields)
        return asdict(updated)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _fetch(self, func, *args) -> Any:
        try:
            return await asyncio.to_thread(func, *args)
        except JiraClientError as exc:
            logger.error("Jira fetch failed: %s", exc)
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
            ))
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)
