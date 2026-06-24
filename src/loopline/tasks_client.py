"""Google Tasks API client.

Handles OAuth2 authorization and full read/write access to Google Tasks.
All task data is normalized into simple dataclasses.

Per project conventions we always use the documented Google client libraries
(`googleapiclient`, `google.auth`) and authenticate via the standard
google-auth-oauthlib installed-app flow.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/tasks"]


class TasksClientError(Exception):
    """Raised for unrecoverable Tasks client problems (auth, config, API)."""


@dataclass
class TaskList:
    id: str
    title: str
    updated: str


@dataclass
class Task:
    id: str
    task_list_id: str
    title: str
    notes: str
    due: str
    status: str   # "needsAction" | "completed"
    completed: str
    updated: str
    position: str
    parent: str   # parent task id or ""

    def short_summary(self) -> str:
        return f"{self.title} ({'done' if self.status == 'completed' else 'todo'})"


class TasksClient:
    """Google Tasks client with OAuth2 token caching."""

    def __init__(self, credentials_file: str, token_file: str) -> None:
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._service = None  # lazily built

    # ------------------------------------------------------------------ #
    # Authentication
    # ------------------------------------------------------------------ #

    def authorize_interactive(self) -> None:
        """Run the interactive OAuth flow and persist the token."""
        if not os.path.exists(self._credentials_file):
            raise TasksClientError(
                f"OAuth client secret not found at '{self._credentials_file}'. "
                "Download it from the Google Cloud Console (OAuth client of type "
                "'Desktop app') and place it there."
            )
        logger.info("Starting Tasks interactive OAuth flow")
        flow = InstalledAppFlow.from_client_secrets_file(self._credentials_file, SCOPES)
        creds = flow.run_local_server(port=0)
        self._save_token(creds)
        logger.info("Tasks OAuth token saved to '%s'", self._token_file)

    def _load_credentials(self) -> Credentials:
        if not os.path.exists(self._token_file):
            raise TasksClientError(
                f"No OAuth token found at '{self._token_file}'. "
                "Run with '--tasks-oauth' to authorize."
            )
        creds = Credentials.from_authorized_user_file(self._token_file, SCOPES)
        if creds.valid:
            return creds
        if creds.expired and creds.refresh_token:
            logger.info("Refreshing expired Tasks OAuth token")
            try:
                creds.refresh(Request())
            except Exception as exc:
                raise TasksClientError(
                    f"Failed to refresh Tasks OAuth token: {exc}. "
                    "Re-run with '--tasks-oauth' to re-authorize."
                ) from exc
            self._save_token(creds)
            return creds
        raise TasksClientError(
            "Cached Tasks OAuth token is invalid. Re-run with '--tasks-oauth'."
        )

    def _save_token(self, creds: Credentials) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self._token_file)), exist_ok=True)
        with open(self._token_file, "w", encoding="utf-8") as fh:
            fh.write(creds.to_json())
        try:
            os.chmod(self._token_file, 0o600)
        except OSError:
            logger.debug("Could not chmod tasks token file (non-fatal)")

    def _get_service(self):
        if self._service is None:
            creds = self._load_credentials()
            self._service = build("tasks", "v1", credentials=creds, cache_discovery=False)
            logger.debug("Tasks API service initialized")
        return self._service

    # ------------------------------------------------------------------ #
    # Connection check
    # ------------------------------------------------------------------ #

    def check_connection(self) -> str:
        """Verify credentials. Returns a summary string."""
        try:
            result = self._get_service().tasklists().list(maxResults=1).execute()
        except HttpError as exc:
            raise TasksClientError(f"Tasks connection check failed: {exc}") from exc
        count = len(result.get("items", []))
        logger.info("Connected to Google Tasks (%d task list(s) visible)", count)
        return f"tasks-api (found {count} task list(s))"

    # ------------------------------------------------------------------ #
    # Read operations
    # ------------------------------------------------------------------ #

    def list_task_lists(self) -> list[TaskList]:
        """List all task lists for the authenticated user."""
        try:
            result = self._get_service().tasklists().list().execute()
        except HttpError as exc:
            raise TasksClientError(f"list_task_lists failed: {exc}") from exc
        items = [
            TaskList(
                id=raw.get("id", ""),
                title=raw.get("title", ""),
                updated=raw.get("updated", ""),
            )
            for raw in result.get("items", [])
        ]
        logger.info("list_task_lists returned %d list(s)", len(items))
        return items

    def list_tasks(self, task_list_id: str, show_completed: bool = False) -> list[Task]:
        """List tasks in a task list."""
        if not task_list_id:
            raise TasksClientError("list_tasks requires a task_list_id")
        kwargs: dict[str, Any] = {"tasklist": task_list_id, "showCompleted": show_completed}
        try:
            result = self._get_service().tasks().list(**kwargs).execute()
        except HttpError as exc:
            raise TasksClientError(f"list_tasks({task_list_id}) failed: {exc}") from exc
        tasks = [self._parse_task(raw, task_list_id) for raw in result.get("items", [])]
        logger.info("list_tasks %s returned %d task(s)", task_list_id, len(tasks))
        return tasks

    def get_task(self, task_list_id: str, task_id: str) -> Task:
        """Fetch a single task by id."""
        if not task_list_id or not task_id:
            raise TasksClientError("get_task requires task_list_id and task_id")
        try:
            raw = self._get_service().tasks().get(tasklist=task_list_id, task=task_id).execute()
        except HttpError as exc:
            raise TasksClientError(f"get_task({task_list_id}, {task_id}) failed: {exc}") from exc
        return self._parse_task(raw, task_list_id)

    # ------------------------------------------------------------------ #
    # Write operations
    # ------------------------------------------------------------------ #

    def create_task(
        self, task_list_id: str, title: str, notes: str = "", due: str = ""
    ) -> Task:
        """Create a new task."""
        if not task_list_id or not title:
            raise TasksClientError("create_task requires task_list_id and title")
        body: dict[str, Any] = {"title": title}
        if notes:
            body["notes"] = notes
        if due:
            body["due"] = due
        try:
            raw = self._get_service().tasks().insert(tasklist=task_list_id, body=body).execute()
        except HttpError as exc:
            raise TasksClientError(f"create_task({task_list_id}) failed: {exc}") from exc
        task = self._parse_task(raw, task_list_id)
        logger.info("create_task: %s", task.short_summary())
        return task

    def update_task(
        self,
        task_list_id: str,
        task_id: str,
        title: str | None = None,
        notes: str | None = None,
        due: str | None = None,
    ) -> Task:
        """Update fields on an existing task."""
        existing = self.get_task(task_list_id, task_id)
        raw = {
            "id": task_id,
            "title": title if title is not None else existing.title,
            "notes": notes if notes is not None else existing.notes,
        }
        if due is not None:
            raw["due"] = due
        elif existing.due:
            raw["due"] = existing.due
        try:
            result = (
                self._get_service()
                .tasks()
                .update(tasklist=task_list_id, task=task_id, body=raw)
                .execute()
            )
        except HttpError as exc:
            raise TasksClientError(f"update_task({task_id}) failed: {exc}") from exc
        updated = self._parse_task(result, task_list_id)
        logger.info("update_task: %s", updated.short_summary())
        return updated

    def complete_task(self, task_list_id: str, task_id: str) -> Task:
        """Mark a task as completed."""
        try:
            raw = (
                self._get_service()
                .tasks()
                .patch(tasklist=task_list_id, task=task_id, body={"status": "completed"})
                .execute()
            )
        except HttpError as exc:
            raise TasksClientError(f"complete_task({task_id}) failed: {exc}") from exc
        return self._parse_task(raw, task_list_id)

    def uncomplete_task(self, task_list_id: str, task_id: str) -> Task:
        """Mark a task as not completed."""
        try:
            raw = (
                self._get_service()
                .tasks()
                .patch(
                    tasklist=task_list_id,
                    task=task_id,
                    body={"status": "needsAction", "completed": None},
                )
                .execute()
            )
        except HttpError as exc:
            raise TasksClientError(f"uncomplete_task({task_id}) failed: {exc}") from exc
        return self._parse_task(raw, task_list_id)

    def move_task(self, source_list_id: str, task_id: str, destination_list_id: str) -> Task:
        """Move a task from one list to another."""
        if not source_list_id or not task_id or not destination_list_id:
            raise TasksClientError("move_task requires source_list_id, task_id, destination_list_id")
        # Get existing task data
        existing = self.get_task(source_list_id, task_id)
        body: dict[str, Any] = {"title": existing.title}
        if existing.notes:
            body["notes"] = existing.notes
        if existing.due:
            body["due"] = existing.due
        # Create in destination
        try:
            new_raw = (
                self._get_service()
                .tasks()
                .insert(tasklist=destination_list_id, body=body)
                .execute()
            )
        except HttpError as exc:
            raise TasksClientError(f"move_task insert({destination_list_id}) failed: {exc}") from exc
        # Delete from source
        try:
            self._get_service().tasks().delete(tasklist=source_list_id, task=task_id).execute()
        except HttpError as exc:
            raise TasksClientError(f"move_task delete({source_list_id}, {task_id}) failed: {exc}") from exc
        return self._parse_task(new_raw, destination_list_id)

    # ------------------------------------------------------------------ #
    # Parsing helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_task(raw: dict[str, Any], task_list_id: str) -> Task:
        return Task(
            id=raw.get("id", ""),
            task_list_id=task_list_id,
            title=raw.get("title", ""),
            notes=raw.get("notes", ""),
            due=raw.get("due", ""),
            status=raw.get("status", "needsAction"),
            completed=raw.get("completed", ""),
            updated=raw.get("updated", ""),
            position=raw.get("position", ""),
            parent=raw.get("parent", ""),
        )
