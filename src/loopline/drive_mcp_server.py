"""FastMCP proxy server for Google Drive.

Exposes a read-only Drive tool surface, but every call follows the same
pipeline as the Gmail proxy:

    fetch from Drive  ->  apply privacy filter  ->  submit to ReviewQueue
                      ->  await user decision    ->  return or raise

If the user rejects (or the filter blocked everything), the tool raises a
ToolError so Claude receives a clean MCP error rather than partial data.

The server runs on the stdio transport, which is the standard MCP transport
used by desktop clients.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from .drive_client import DriveClient, DriveClientError
from .privacy_filter import DrivePrivacyFilter
from .review_queue import ReviewRejected, get_review_queue

logger = logging.getLogger(__name__)


class DriveGuardServer:
    """Builds and runs the FastMCP proxy server for Google Drive."""

    def __init__(
        self,
        drive_client: DriveClient,
        privacy_filter: DrivePrivacyFilter,
        server_name: str = "drive-guard",
        server_version: str = "0.1.0",
    ) -> None:
        self._drive = drive_client
        self._filter = privacy_filter
        self._queue = get_review_queue()
        self._mcp = FastMCP(name=server_name, version=server_version)
        self._register_tools()

    # ------------------------------------------------------------------ #
    # Tool registration
    # ------------------------------------------------------------------ #
    def _register_tools(self) -> None:
        mcp = self._mcp

        @mcp.tool(
            name="drive_list_files",
            description=(
                "Search Google Drive and return matching file metadata (id, "
                "name, mime_type, owners, sharing status). Requires user "
                "approval."
            ),
        )
        async def drive_list_files(query: str, max_results: int = 20) -> Any:
            files = await self._fetch(self._drive.list_files, query, max_results)
            filtered = self._filter.filter_file_list(files)
            return await self._review(
                tool_name="Search Drive Files",
                summary=f"List files: query={query!r} (max {max_results})",
                sender=f"{len(files)} result(s)",
                raw_data=files,
                filtered_data=filtered,
            )

        @mcp.tool(
            name="drive_get_file_metadata",
            description=(
                "Fetch metadata for a single Drive file by id (name, owners, "
                "times, sharing status). Requires user approval."
            ),
        )
        async def drive_get_file_metadata(file_id: str) -> Any:
            drive_file = await self._fetch(
                self._drive.get_file_metadata, file_id
            )
            filtered = self._filter.filter_file_metadata(drive_file)
            return await self._review(
                tool_name="Get Drive File Info",
                summary=f"Get metadata: {drive_file.short_summary()}",
                sender=", ".join(drive_file.owners) or "(unknown owner)",
                raw_data=drive_file,
                filtered_data=filtered.to_dict(),
            )

        @mcp.tool(
            name="drive_get_file_content",
            description=(
                "Fetch the content of a single Drive file by id. Google Docs/"
                "Sheets/Slides are exported as text. Highest privacy risk - "
                "requires user approval."
            ),
        )
        async def drive_get_file_content(file_id: str) -> Any:
            content = await self._fetch(
                self._drive.get_file_content, file_id
            )
            filtered = self._filter.filter_file_content(content)
            return await self._review(
                tool_name="Read Drive File",
                summary=f"Get content: {content.file.short_summary()}",
                sender=", ".join(content.file.owners) or "(unknown owner)",
                raw_data=content,
                filtered_data=filtered.to_dict(),
            )

        @mcp.tool(
            name="drive_list_folder",
            description=(
                "List the direct children of a Drive folder by id. Requires "
                "user approval."
            ),
        )
        async def drive_list_folder(folder_id: str, max_results: int = 50) -> Any:
            files = await self._fetch(
                self._drive.list_folder, folder_id, max_results
            )
            filtered = self._filter.filter_folder_listing(files)
            return await self._review(
                tool_name="List Drive Folder",
                summary=f"List folder: {folder_id} (max {max_results})",
                sender=f"{len(files)} child item(s)",
                raw_data=files,
                filtered_data=filtered,
            )

    # ------------------------------------------------------------------ #
    # Pipeline helpers
    # ------------------------------------------------------------------ #
    async def _fetch(self, func, *args) -> Any:
        """Run a blocking Drive client call off the event loop thread."""
        try:
            return await asyncio.to_thread(func, *args)
        except DriveClientError as exc:
            logger.error("Drive fetch failed: %s", exc)
            raise ToolError(str(exc)) from exc

    async def _review(
        self,
        tool_name: str,
        summary: str,
        sender: str,
        raw_data: Any,
        filtered_data: Any,
    ) -> Any:
        """Submit to the review queue and await the user's decision."""
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
            logger.info("Tool %s rejected by user: %s", tool_name, exc)
            raise ToolError(f"Request denied by user: {exc}") from exc
        logger.info("Tool %s approved; returning data to client", tool_name)
        return result

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def run_stdio(self) -> None:
        """Run the server on the stdio transport (blocking).

        Intended to be called from a dedicated background thread that owns its
        own asyncio event loop.
        """
        logger.info("Starting Drive MCP server on stdio transport")
        self._mcp.run(transport="stdio")
