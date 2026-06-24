"""FastMCP proxy server.

Approval policy
---------------
  auto-allow  : gmail_list_messages, gmail_list_threads
                (search / metadata only — no message bodies)
  approval     : gmail_get_message, gmail_get_thread
                (body content shown as HTML preview in the review window)
  future-gate  : attachment content download (tool not yet implemented)

If the user rejects a request the tool raises ToolError so Claude receives a
clean MCP error rather than partial data.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from .gmail_client import GmailClient, GmailClientError
from .privacy_filter import PrivacyFilter
from .review_queue import ReviewRejected, get_review_queue

logger = logging.getLogger(__name__)


class GmailGuardServer:
    """Builds and runs the FastMCP proxy server."""

    def __init__(
        self,
        gmail_client: GmailClient,
        privacy_filter: PrivacyFilter,
        server_name: str = "loopline-gmail",
        server_version: str = "0.1.0",
    ) -> None:
        self._gmail = gmail_client
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
            name="gmail_list_messages",
            description=(
                "Search Gmail and return matching message summaries "
                "(id, thread_id, subject, sender, date). "
                "Auto-approved — no body content is returned."
            ),
        )
        async def gmail_list_messages(query: str, max_results: int = 10) -> Any:
            summaries = await self._fetch(self._gmail.list_messages, query, max_results)
            filtered = [self._filter_summary(s) for s in summaries]
            logger.info(
                "gmail_list_messages auto-approved: query=%r results=%d",
                query,
                len(filtered),
            )
            return filtered

        @mcp.tool(
            name="gmail_list_threads",
            description=(
                "Search Gmail and return matching thread summaries "
                "(id, snippet). "
                "Auto-approved — no body content is returned."
            ),
        )
        async def gmail_list_threads(query: str, max_results: int = 10) -> Any:
            summaries = await self._fetch(self._gmail.list_threads, query, max_results)
            logger.info(
                "gmail_list_threads auto-approved: query=%r results=%d",
                query,
                len(summaries),
            )
            return summaries

        @mcp.tool(
            name="gmail_get_message",
            description=(
                "Fetch a single Gmail message by id, including body, metadata, "
                "and attachment list. Requires user approval — the HTML body is "
                "shown in the review window before any data is returned."
            ),
        )
        async def gmail_get_message(message_id: str) -> Any:
            message = await self._fetch(self._gmail.get_message, message_id)
            filtered = self._filter.filter_message(message)
            display_hint = {
                "type": "email",
                "sender": filtered.sender,
                "recipients": filtered.recipients,
                "subject": filtered.subject,
                "date": filtered.date,
                "html_body": message.body_html or message.body_text,
                "attachment_count": len(message.attachments),
            }
            return await self._review(
                tool_name="Read Email",
                summary=f"Read email: {message.short_summary()}",
                sender=message.sender,
                raw_data=message,
                filtered_data=filtered.to_dict(),
                display_hint=display_hint,
            )

        @mcp.tool(
            name="gmail_get_thread",
            description=(
                "Fetch a full Gmail thread by id, including its messages. "
                "Requires user approval — the HTML body of each message is "
                "shown in the review window before any data is returned."
            ),
        )
        async def gmail_get_thread(thread_id: str) -> Any:
            thread = await self._fetch(self._gmail.get_thread, thread_id)
            filtered = self._filter.filter_thread(thread)
            # Build per-message previews for the display hint.
            message_previews = [
                {
                    "sender": m.sender,
                    "recipients": m.recipients,
                    "subject": m.subject,
                    "date": m.date,
                    "html_body": raw.body_html or raw.body_text,
                    "attachment_count": len(raw.attachments),
                }
                for m, raw in zip(filtered.messages, thread.messages)
            ]
            display_hint = {
                "type": "thread",
                "subject": filtered.subject,
                "message_count": len(thread.messages),
                "messages": message_previews,
            }
            return await self._review(
                tool_name="Read Email Thread",
                summary=f"Read thread: {thread.short_summary()}",
                sender=thread.subject,
                raw_data=thread,
                filtered_data=filtered.to_dict(),
                display_hint=display_hint,
            )

    # ------------------------------------------------------------------ #
    # Pipeline helpers
    # ------------------------------------------------------------------ #
    async def _fetch(self, func, *args) -> Any:
        """Run a blocking Gmail client call off the event loop thread."""
        try:
            return await asyncio.to_thread(func, *args)
        except GmailClientError as exc:
            logger.error("Gmail fetch failed: %s", exc)
            raise ToolError(str(exc)) from exc

    async def _review(
        self,
        tool_name: str,
        summary: str,
        sender: str,
        raw_data: Any,
        filtered_data: Any,
        display_hint: dict | None = None,
    ) -> Any:
        """Submit to the review queue and await the user's decision."""
        future = self._queue.submit(
            tool_name=tool_name,
            summary=summary,
            sender=sender,
            raw_data=raw_data,
            filtered_data=filtered_data,
            display_hint=display_hint,
        )
        try:
            result = await future
        except ReviewRejected as exc:
            logger.info("Tool %s rejected by user: %s", tool_name, exc)
            raise ToolError(f"Request denied by user: {exc}") from exc
        logger.info("Tool %s approved; returning data to client", tool_name)
        return result

    def _filter_summary(self, summary: dict[str, str]) -> dict[str, str]:
        """Apply the metadata policy to a lightweight list_messages summary."""
        policy = self._filter.policy_for("metadata")
        result = dict(summary)
        for field_name in ("subject", "date"):
            if field_name in result:
                result[field_name] = PrivacyFilter._apply_text(  # noqa: SLF001
                    result[field_name], policy
                )
        if "sender" in result:
            result["sender"] = PrivacyFilter._apply_address(  # noqa: SLF001
                result["sender"], policy
            )
        return result

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def run_stdio(self) -> None:
        """Run the server on the stdio transport (blocking).

        Intended to be called from a dedicated background thread that owns its
        own asyncio event loop.
        """
        logger.info("Starting MCP server on stdio transport")
        self._mcp.run(transport="stdio")
