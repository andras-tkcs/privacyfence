"""Google Drive connector."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..drive_client import DriveClient, DriveClientError
from ..gate import gated_call

logger = logging.getLogger(__name__)


class DriveConnector(Connector):
    def __init__(self, client: DriveClient) -> None:
        self._drive = client
        self.my_email: str = ""
        self.session_created_ids: set[str] = set()

    @property
    def name(self) -> str:
        return "drive"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="drive_list_files",
                description=(
                    "Search Google Drive and return matching file metadata "
                    "(id, name, mime_type, owners, sharing status). Auto-approved."
                ),
                params=[
                    ToolParam("query", "str"),
                    ToolParam("max_results", "int", required=False, default=20),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="drive_get_file_metadata",
                description=(
                    "Fetch metadata for a single Drive file by id "
                    "(name, owners, times, sharing status). Auto-approved."
                ),
                params=[ToolParam("file_id", "str")],
                read_only=True,
            ),
            ToolSpec(
                name="drive_list_folder",
                description="List the direct children of a Drive folder by id. Auto-approved.",
                params=[
                    ToolParam("folder_id", "str"),
                    ToolParam("max_results", "int", required=False, default=50),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="drive_create_blank_file",
                description="Create a new blank Drive file. Auto-approved.",
                params=[
                    ToolParam("name", "str"),
                    ToolParam("mime_type", "str"),
                    ToolParam("parent_folder_id", "str", required=False, default=""),
                ],
            ),
            ToolSpec(
                name="drive_get_file_content",
                description=(
                    "Fetch the content of a Drive file by id. Requires user approval."
                ),
                params=[ToolParam("file_id", "str")],
                read_only=True,
            ),
            ToolSpec(
                name="drive_write_file_content",
                description="Write content to an existing Drive file. Requires user approval.",
                params=[
                    ToolParam("file_id", "str"),
                    ToolParam("content", "str"),
                ],
            ),
            ToolSpec(
                name="drive_move_file",
                description="Move a Drive file to a different folder. Requires user approval.",
                params=[
                    ToolParam("file_id", "str"),
                    ToolParam("destination_folder_id", "str"),
                ],
            ),
            ToolSpec(
                name="drive_add_comment",
                description="Add a comment to a Drive file. Requires user approval.",
                params=[
                    ToolParam("file_id", "str"),
                    ToolParam("comment", "str"),
                ],
            ),
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "drive_list_files":
            return await self._list_files(**args)
        if tool == "drive_get_file_metadata":
            return await self._get_file_metadata(**args)
        if tool == "drive_get_file_content":
            return await self._get_file_content(**args)
        if tool == "drive_list_folder":
            return await self._list_folder(**args)
        if tool == "drive_create_blank_file":
            return await self._create_blank_file(**args)
        if tool == "drive_write_file_content":
            return await self._write_file_content(**args)
        if tool == "drive_move_file":
            return await self._move_file(**args)
        if tool == "drive_add_comment":
            return await self._add_comment(**args)
        raise ValueError(f"Unknown Drive tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Auto
    # ------------------------------------------------------------------ #

    async def _list_files(self, query: str, max_results: int = 20) -> Any:
        t0 = time.time()
        files = await self._fetch(self._drive.list_files, query, max_results)
        self._auto_audit("drive_list_files", "Search Drive Files",
                         f"List files: query={query!r}", f"{len(files)} result(s)", t0)
        return files if isinstance(files, list) else (files.to_dict() if hasattr(files, "to_dict") else files)

    async def _get_file_metadata(self, file_id: str) -> Any:
        t0 = time.time()
        drive_file = await self._fetch(self._drive.get_file_metadata, file_id)
        self._auto_audit("drive_get_file_metadata", "Get Drive File Info",
                         f"Get metadata: {getattr(drive_file, 'name', file_id)}",
                         ", ".join(getattr(drive_file, "owners", [])) or "(unknown)", t0)
        return drive_file.to_dict() if hasattr(drive_file, "to_dict") else drive_file

    async def _list_folder(self, folder_id: str, max_results: int = 50) -> Any:
        t0 = time.time()
        files = await self._fetch(self._drive.list_folder, folder_id, max_results)
        self._auto_audit("drive_list_folder", "List Drive Folder",
                         f"List folder: {folder_id}", f"{len(files)} item(s)", t0)
        return files

    async def _create_blank_file(
        self, name: str, mime_type: str, parent_folder_id: str = ""
    ) -> Any:
        t0 = time.time()
        result = await self._fetch(self._drive.create_blank_file, name, mime_type, parent_folder_id)
        file_id = result.get("id", "") if isinstance(result, dict) else getattr(result, "id", "")
        if file_id:
            self.session_created_ids.add(file_id)
        self._auto_audit("drive_create_blank_file", "Create Drive File",
                         f"Create: {name} ({mime_type})", f"id={file_id}", t0)
        return result

    # ------------------------------------------------------------------ #
    # Review gate (reads)
    # ------------------------------------------------------------------ #

    async def _get_file_content(self, file_id: str) -> Any:
        content = await self._fetch(self._drive.get_file_content, file_id)
        drive_file = getattr(content, "file", None)
        name = getattr(drive_file, "name", None) or file_id
        owners = getattr(drive_file, "owners", [])
        size = getattr(drive_file, "size", "")
        modified = getattr(drive_file, "modified_time", "")
        text = getattr(content, "text", None) or str(content)
        preview = {
            "File": name,
            "Owner": ", ".join(owners) if owners else "(unknown)",
            "Size": str(size) if size else "(unknown)",
            "Modified": str(modified) if modified else "(unknown)",
        }
        details = f"File: {name}\nOwner: {', '.join(owners)}\nModified: {modified}\n\n{text[:2000]}"
        filtered = content.to_dict() if hasattr(content, "to_dict") else {"file_id": file_id, "content": text}
        return await gated_call(
            connector=self.name,
            tool="drive_get_file_content",
            tool_name="Read Drive File",
            summary=f"Read \"{name}\"",
            sender=", ".join(owners) or "(unknown)",
            raw_data=content,
            filtered_data=filtered,
            gate="review",
            preview=preview,
            details_text=details,
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"file_id": file_id},
        )

    # ------------------------------------------------------------------ #
    # Popup gate (writes)
    # ------------------------------------------------------------------ #

    async def _write_file_content(self, file_id: str, content: str) -> Any:
        drive_file = await self._fetch(self._drive.get_file_metadata, file_id)
        name = getattr(drive_file, "name", file_id)
        owners = getattr(drive_file, "owners", [])
        preview_text = content[:500] + ("…" if len(content) > 500 else "")
        details = f"File: {name}\nOwner: {', '.join(owners)}\n\nNew content:\n{preview_text}"
        await gated_call(
            connector=self.name,
            tool="drive_write_file_content",
            tool_name="Write Drive File",
            summary=f"Write to \"{name}\"",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "content_preview": content[:200]},
            filtered_data=None,
            gate="popup",
            details_text=details,
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"file_id": file_id},
        )
        return await self._fetch(self._drive.write_file_content, file_id, content)

    async def _move_file(self, file_id: str, destination_folder_id: str) -> Any:
        drive_file = await self._fetch(self._drive.get_file_metadata, file_id)
        name = getattr(drive_file, "name", file_id)
        owners = getattr(drive_file, "owners", [])
        details = f"File: {name}\nOwner: {', '.join(owners)}\nMove to folder: {destination_folder_id}"
        await gated_call(
            connector=self.name,
            tool="drive_move_file",
            tool_name="Move Drive File",
            summary=f"Move \"{name}\" to new folder",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "destination_folder_id": destination_folder_id},
            filtered_data=None,
            gate="popup",
            details_text=details,
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"file_id": file_id, "destination_folder_id": destination_folder_id},
        )
        return await self._fetch(self._drive.move_file, file_id, destination_folder_id)

    async def _add_comment(self, file_id: str, comment: str) -> Any:
        drive_file = await self._fetch(self._drive.get_file_metadata, file_id)
        name = getattr(drive_file, "name", file_id)
        owners = getattr(drive_file, "owners", [])
        details = f"File: {name}\nOwner: {', '.join(owners)}\n\nComment:\n{comment}"
        await gated_call(
            connector=self.name,
            tool="drive_add_comment",
            tool_name="Add Drive Comment",
            summary=f"Comment on \"{name}\"",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "comment": comment},
            filtered_data=None,
            gate="popup",
            details_text=details,
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"file_id": file_id},
        )
        return await self._fetch(self._drive.add_comment, file_id, comment)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _fetch(self, func, *args) -> Any:
        try:
            return await asyncio.to_thread(func, *args)
        except DriveClientError as exc:
            logger.error("Drive fetch failed: %s", exc)
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
