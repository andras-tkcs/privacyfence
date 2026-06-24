"""Google Drive connector: wraps DriveClient + DrivePrivacyFilter + ReviewQueue."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..connector import Connector, ToolParam, ToolSpec
from ..drive_client import DriveClient, DriveClientError
from ..privacy_filter import DrivePrivacyFilter
from ..review_queue import ReviewRejected, get_review_queue

logger = logging.getLogger(__name__)


class DriveConnector(Connector):
    def __init__(self, client: DriveClient, privacy_filter: DrivePrivacyFilter) -> None:
        self._drive = client
        self._filter = privacy_filter
        self._queue = get_review_queue()

    @property
    def name(self) -> str:
        return "drive"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="drive_list_files",
                description=(
                    "Search Google Drive and return matching file metadata "
                    "(id, name, mime_type, owners, sharing status). Requires user approval."
                ),
                params=[
                    ToolParam("query", "str"),
                    ToolParam("max_results", "int", required=False, default=20),
                ],
            ),
            ToolSpec(
                name="drive_get_file_metadata",
                description=(
                    "Fetch metadata for a single Drive file by id "
                    "(name, owners, times, sharing status). Requires user approval."
                ),
                params=[ToolParam("file_id", "str")],
            ),
            ToolSpec(
                name="drive_get_file_content",
                description=(
                    "Fetch the content of a single Drive file by id. "
                    "Google Docs/Sheets/Slides are exported as text. Requires user approval."
                ),
                params=[ToolParam("file_id", "str")],
            ),
            ToolSpec(
                name="drive_list_folder",
                description=(
                    "List the direct children of a Drive folder by id. Requires user approval."
                ),
                params=[
                    ToolParam("folder_id", "str"),
                    ToolParam("max_results", "int", required=False, default=50),
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
        raise ValueError(f"Unknown Drive tool: {tool!r}")

    # ------------------------------------------------------------------ #

    async def _list_files(self, query: str, max_results: int = 20) -> Any:
        files = await self._fetch(self._drive.list_files, query, max_results)
        filtered = self._filter.filter_file_list(files)
        return await self._review(
            tool_name="Search Drive Files",
            summary=f"List files: query={query!r} (max {max_results})",
            sender=f"{len(files)} result(s)",
            raw_data=files,
            filtered_data=filtered,
        )

    async def _get_file_metadata(self, file_id: str) -> Any:
        drive_file = await self._fetch(self._drive.get_file_metadata, file_id)
        filtered = self._filter.filter_file_metadata(drive_file)
        return await self._review(
            tool_name="Get Drive File Info",
            summary=f"Get metadata: {drive_file.short_summary()}",
            sender=", ".join(drive_file.owners) or "(unknown owner)",
            raw_data=drive_file,
            filtered_data=filtered.to_dict(),
        )

    async def _get_file_content(self, file_id: str) -> Any:
        content = await self._fetch(self._drive.get_file_content, file_id)
        filtered = self._filter.filter_file_content(content)
        return await self._review(
            tool_name="Read Drive File",
            summary=f"Get content: {content.file.short_summary()}",
            sender=", ".join(content.file.owners) or "(unknown owner)",
            raw_data=content,
            filtered_data=filtered.to_dict(),
        )

    async def _list_folder(self, folder_id: str, max_results: int = 50) -> Any:
        files = await self._fetch(self._drive.list_folder, folder_id, max_results)
        filtered = self._filter.filter_folder_listing(files)
        return await self._review(
            tool_name="List Drive Folder",
            summary=f"List folder: {folder_id} (max {max_results})",
            sender=f"{len(files)} child item(s)",
            raw_data=files,
            filtered_data=filtered,
        )

    # ------------------------------------------------------------------ #

    async def _fetch(self, func, *args) -> Any:
        try:
            return await asyncio.to_thread(func, *args)
        except DriveClientError as exc:
            logger.error("Drive fetch failed: %s", exc)
            raise RuntimeError(str(exc)) from exc

    async def _review(self, *, tool_name, summary, sender, raw_data, filtered_data) -> Any:
        future = self._queue.submit(
            tool_name=tool_name,
            summary=summary,
            sender=sender,
            raw_data=raw_data,
            filtered_data=filtered_data,
        )
        try:
            result = await future
        except ReviewRejected as exc:
            logger.info("Tool %s rejected: %s", tool_name, exc)
            raise RuntimeError(f"Request denied by user: {exc}") from exc
        logger.info("Tool %s approved", tool_name)
        return result
