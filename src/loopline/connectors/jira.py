"""Jira connector: wraps JiraClient with MCP tool definitions and gating."""

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
                description=(
                    "List Jira projects accessible to the user "
                    "(key, name, type, lead). Auto-approved."
                ),
                params=[
                    ToolParam("max_results", "int", required=False, default=50),
                ],
            ),
            ToolSpec(
                name="jira_search_issues",
                description=(
                    "Search Jira issues using JQL (Jira Query Language). "
                    "Returns summary info for matching issues. Auto-approved."
                ),
                params=[
                    ToolParam("jql", "str", description="e.g. 'project = MYPROJ AND status = Open'"),
                    ToolParam("max_results", "int", required=False, default=20),
                ],
            ),
            ToolSpec(
                name="jira_get_issue",
                description=(
                    "Fetch full details of a Jira issue by key (e.g. PROJ-123), "
                    "including description and comments. Requires user approval."
                ),
                params=[
                    ToolParam("issue_key", "str", description="e.g. PROJ-123"),
                ],
            ),
            ToolSpec(
                name="jira_create_issue",
                description=(
                    "Create a new Jira issue. Requires user approval."
                ),
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
                description=(
                    "Add a comment to an existing Jira issue. Requires user approval."
                ),
                params=[
                    ToolParam("issue_key", "str", description="e.g. PROJ-123"),
                    ToolParam("body", "str", description="Comment text (plain text)"),
                ],
            ),
            ToolSpec(
                name="jira_update_issue",
                description=(
                    "Update fields on an existing Jira issue (summary, description, "
                    "priority, assignee account ID, labels). Requires user approval."
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
    # Always-allowed
    # ------------------------------------------------------------------ #

    async def _list_projects(self, max_results: int = 50) -> Any:
        t0 = time.time()
        projects = await self._fetch(self._jira.list_projects, max_results)
        data = [asdict(p) for p in projects]
        self._auto_audit(
            "jira_list_projects", "List Jira Projects",
            f"List projects (max {max_results})", f"{len(projects)} project(s)", t0,
        )
        return data

    async def _search_issues(self, jql: str, max_results: int = 20) -> Any:
        t0 = time.time()
        issues = await self._fetch(self._jira.search_issues, jql, max_results)
        data = [asdict(i) for i in issues]
        self._auto_audit(
            "jira_search_issues", "Search Jira Issues",
            f"Search: {jql[:80]}", f"{len(issues)} issue(s)", t0,
        )
        return data

    # ------------------------------------------------------------------ #
    # Gated
    # ------------------------------------------------------------------ #

    async def _get_issue(self, issue_key: str) -> Any:
        issue = await self._fetch(self._jira.get_issue, issue_key)
        comments = await self._fetch(self._jira.get_issue_comments, issue_key)
        result = {**asdict(issue), "comments": [asdict(c) for c in comments]}
        return await gated_call(
            connector=self.name,
            tool="jira_get_issue",
            tool_name="Read Jira Issue",
            summary=f"{issue.key}: {issue.summary[:80]}{'…' if len(issue.summary) > 80 else ''}",
            sender=issue.reporter or issue.assignee or issue_key,
            raw_data=result,
            filtered_data=result,
            my_email=self.my_email,
            args={"issue_key": issue_key},
        )

    async def _create_issue(
        self,
        project_key: str,
        summary: str,
        issue_type: str = "Task",
        description: str = "",
        priority: str = "",
    ) -> Any:
        preview = {
            "project_key": project_key,
            "summary": summary,
            "issue_type": issue_type,
            "description": description,
            "priority": priority,
        }
        approved = await gated_call(
            connector=self.name,
            tool="jira_create_issue",
            tool_name="Create Jira Issue",
            summary=f"Create {issue_type} in {project_key}: {summary[:60]}",
            sender=f"project={project_key}",
            raw_data=preview,
            filtered_data=preview,
            my_email=self.my_email,
            args=preview,
        )
        # After approval the gate returns the args — execute the actual write.
        issue = await self._fetch(
            self._jira.create_issue,
            project_key, summary, issue_type, description, priority,
        )
        return asdict(issue)

    async def _add_comment(self, issue_key: str, body: str) -> Any:
        preview = {"issue_key": issue_key, "body": body}
        await gated_call(
            connector=self.name,
            tool="jira_add_comment",
            tool_name="Add Jira Comment",
            summary=f"Comment on {issue_key}: {body[:80]}",
            sender=f"issue={issue_key}",
            raw_data=preview,
            filtered_data=preview,
            my_email=self.my_email,
            args=preview,
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
        fields: dict[str, Any] = {}
        if summary:
            fields["summary"] = summary
        if description:
            fields["description"] = {
                "type": "doc",
                "version": 1,
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": description}]}],
            }
        if priority:
            fields["priority"] = {"name": priority}
        if not fields:
            raise ValueError("update_issue: at least one field must be provided")
        preview = {"issue_key": issue_key, "fields": fields}
        await gated_call(
            connector=self.name,
            tool="jira_update_issue",
            tool_name="Update Jira Issue",
            summary=f"Update {issue_key}: {', '.join(fields.keys())}",
            sender=f"issue={issue_key}",
            raw_data=preview,
            filtered_data=preview,
            my_email=self.my_email,
            args={"issue_key": issue_key},
        )
        issue = await self._fetch(self._jira.update_issue, issue_key, fields)
        return asdict(issue)

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
                auto_accept_rule="always_allowed",
                latency_seconds=time.time() - created_at,
            ))
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)
