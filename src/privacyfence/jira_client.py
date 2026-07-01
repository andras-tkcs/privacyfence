"""Jira Cloud API client.

Uses Atlassian's API token authentication (email + token).
Create a token at https://id.atlassian.com/manage/api-tokens.

Required config keys:
  cloud_url   – e.g. https://yourcompany.atlassian.net
  email       – your Atlassian account email
  api_token   – personal API token
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from atlassian import Jira

logger = logging.getLogger(__name__)


class JiraClientError(Exception):
    """Raised for unrecoverable Jira client problems (auth, config, API)."""


@dataclass
class JiraProject:
    key: str
    name: str
    project_type: str = ""
    description: str = ""
    lead: str = ""

    def short_summary(self) -> str:
        return f"[{self.key}] {self.name}"


@dataclass
class JiraIssue:
    key: str
    summary: str
    status: str
    issue_type: str
    priority: str = ""
    assignee: str = ""
    reporter: str = ""
    description: str = ""
    labels: list[str] = field(default_factory=list)
    created: str = ""
    updated: str = ""
    url: str = ""

    def short_summary(self) -> str:
        snippet = self.summary[:60] + "…" if len(self.summary) > 60 else self.summary
        return f"{self.key} ({self.status}): {snippet}"


@dataclass
class JiraComment:
    id: str
    author: str
    body: str
    created: str = ""
    updated: str = ""


class JiraClient:
    """Jira Cloud client backed by email + API token (Basic auth)."""

    def __init__(self, config: dict[str, Any]) -> None:
        cloud_url = config.get("cloud_url", "").rstrip("/")
        email = config.get("email", "")
        api_token = config.get("api_token", "")

        if not cloud_url:
            raise JiraClientError("jira.cloud_url not configured")
        if not email:
            raise JiraClientError("jira.email not configured")
        if not api_token or api_token.startswith("your-"):
            raise JiraClientError("jira.api_token not configured")

        self._base_url = cloud_url
        try:
            self._client = Jira(
                url=cloud_url,
                username=email,
                password=api_token,
                cloud=True,
            )
        except Exception as exc:
            raise JiraClientError(f"Failed to initialise Jira client: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #

    def check_connection(self) -> str:
        """Verify credentials. Returns the site URL on success."""
        try:
            myself = self._client.myself()
            display = myself.get("displayName", "unknown user")
            logger.info("Connected to Jira at %s as %r", self._base_url, display)
            return f"{display} @ {self._base_url}"
        except Exception as exc:
            raise JiraClientError(f"Jira connection check failed: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Projects
    # ------------------------------------------------------------------ #

    def list_projects(self, max_results: int = 50) -> list[JiraProject]:
        max_results = max(1, min(max_results, 500))
        try:
            raw = self._client.projects(included_archived=None)
        except Exception as exc:
            raise JiraClientError(f"list_projects failed: {exc}") from exc
        projects = [self._parse_project(p) for p in (raw or [])][:max_results]
        logger.info("list_projects returned %d project(s)", len(projects))
        return projects

    # ------------------------------------------------------------------ #
    # Issues
    # ------------------------------------------------------------------ #

    def search_issues(self, jql: str, max_results: int = 20) -> list[JiraIssue]:
        if not jql:
            raise JiraClientError("search_issues requires a non-empty JQL query")
        max_results = max(1, min(max_results, 100))
        try:
            result = self._client.jql(jql, limit=max_results)
        except Exception as exc:
            raise JiraClientError(f"search_issues failed: {exc}") from exc
        issues = [self._parse_issue(i) for i in (result.get("issues") or [])]
        logger.info("search_issues jql=%r returned %d issue(s)", jql, len(issues))
        return issues

    def get_issue(self, issue_key: str) -> JiraIssue:
        if not issue_key:
            raise JiraClientError("get_issue requires an issue key")
        try:
            raw = self._client.issue(issue_key)
        except Exception as exc:
            raise JiraClientError(f"get_issue({issue_key!r}) failed: {exc}") from exc
        issue = self._parse_issue(raw, include_description=True)
        logger.info("get_issue %s: %s", issue_key, issue.short_summary())
        return issue

    def get_issue_comments(self, issue_key: str) -> list[JiraComment]:
        if not issue_key:
            raise JiraClientError("get_issue_comments requires an issue key")
        try:
            raw = self._client.issue(issue_key, fields="comment")
            comments_raw = (raw.get("fields", {}).get("comment") or {}).get("comments", [])
        except Exception as exc:
            raise JiraClientError(f"get_issue_comments({issue_key!r}) failed: {exc}") from exc
        comments = [self._parse_comment(c) for c in comments_raw]
        logger.info("get_issue_comments %s returned %d comment(s)", issue_key, len(comments))
        return comments

    def create_issue(
        self,
        project_key: str,
        summary: str,
        issue_type: str = "Task",
        description: str = "",
        priority: str = "",
        assignee_account_id: str = "",
        labels: list[str] | None = None,
    ) -> JiraIssue:
        if not project_key or not summary:
            raise JiraClientError("create_issue requires project_key and summary")
        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "summary": summary,
            "issuetype": {"name": issue_type},
        }
        if description:
            fields["description"] = {
                "type": "doc",
                "version": 1,
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": description}]}],
            }
        if priority:
            fields["priority"] = {"name": priority}
        if assignee_account_id:
            fields["assignee"] = {"accountId": assignee_account_id}
        if labels:
            fields["labels"] = labels
        try:
            raw = self._client.create_issue(fields=fields)
        except Exception as exc:
            raise JiraClientError(f"create_issue failed: {exc}") from exc
        key = raw.get("key", "")
        logger.info("create_issue created %s", key)
        return self.get_issue(key)

    def add_comment(self, issue_key: str, body: str) -> JiraComment:
        if not issue_key or not body:
            raise JiraClientError("add_comment requires issue_key and body")
        try:
            raw = self._client.issue_add_comment(issue_key, body)
        except Exception as exc:
            raise JiraClientError(f"add_comment({issue_key!r}) failed: {exc}") from exc
        comment = self._parse_comment(raw)
        logger.info("add_comment: added comment %s to %s", comment.id, issue_key)
        return comment

    def update_issue(self, issue_key: str, fields: dict[str, Any]) -> JiraIssue:
        if not issue_key or not fields:
            raise JiraClientError("update_issue requires issue_key and fields")
        try:
            self._client.update_issue_field(issue_key, fields)
        except Exception as exc:
            raise JiraClientError(f"update_issue({issue_key!r}) failed: {exc}") from exc
        logger.info("update_issue %s: updated fields %s", issue_key, list(fields.keys()))
        return self.get_issue(issue_key)

    # ------------------------------------------------------------------ #
    # Parsing helpers
    # ------------------------------------------------------------------ #

    def _parse_project(self, raw: dict[str, Any]) -> JiraProject:
        return JiraProject(
            key=raw.get("key", ""),
            name=raw.get("name", ""),
            project_type=raw.get("projectTypeKey", ""),
            description=raw.get("description", "") or "",
            lead=(raw.get("lead") or {}).get("displayName", ""),
        )

    def _parse_issue(self, raw: dict[str, Any], include_description: bool = False) -> JiraIssue:
        f = raw.get("fields") or {}
        key = raw.get("key", "")
        desc = ""
        if include_description:
            desc_raw = f.get("description")
            if isinstance(desc_raw, str):
                desc = desc_raw
            elif isinstance(desc_raw, dict):
                desc = self._extract_adf_text(desc_raw)
        return JiraIssue(
            key=key,
            summary=f.get("summary", ""),
            status=(f.get("status") or {}).get("name", ""),
            issue_type=(f.get("issuetype") or {}).get("name", ""),
            priority=(f.get("priority") or {}).get("name", ""),
            assignee=(f.get("assignee") or {}).get("displayName", ""),
            reporter=(f.get("reporter") or {}).get("displayName", ""),
            description=desc,
            labels=f.get("labels") or [],
            created=f.get("created", ""),
            updated=f.get("updated", ""),
            url=f"{self._base_url}/browse/{key}" if key else "",
        )

    @staticmethod
    def _parse_comment(raw: dict[str, Any]) -> JiraComment:
        body_raw = raw.get("body", "")
        if isinstance(body_raw, dict):
            body = JiraClient._extract_adf_text(body_raw)
        else:
            body = str(body_raw)
        return JiraComment(
            id=raw.get("id", ""),
            author=(raw.get("author") or {}).get("displayName", ""),
            body=body,
            created=raw.get("created", ""),
            updated=raw.get("updated", ""),
        )

    @staticmethod
    def _extract_adf_text(node: dict[str, Any]) -> str:
        """Extract plain text from an Atlassian Document Format node."""
        if not isinstance(node, dict):
            return str(node)
        if node.get("type") == "text":
            return node.get("text", "")
        parts: list[str] = []
        for child in node.get("content") or []:
            parts.append(JiraClient._extract_adf_text(child))
        return " ".join(p for p in parts if p)
