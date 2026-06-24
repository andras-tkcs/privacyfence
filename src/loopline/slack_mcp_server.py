"""FastMCP proxy server for Slack.

Exposes a read-only Slack tool surface, but every call follows the same
pipeline as the Gmail proxy:

    fetch from Slack  ->  apply privacy filter  ->  submit to ReviewQueue
                      ->  await user decision    ->  return or raise

If the user rejects (or the filter blocked everything), the tool raises a
ToolError so Claude receives a clean MCP error rather than partial data.

The server runs on the stdio transport (the standard MCP transport used by
desktop clients), so all logging must go to file/stderr - never stdout.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from .privacy_filter import SlackPrivacyFilter
from .review_queue import ReviewRejected, get_review_queue
from .slack_client import SlackClient, SlackClientError

logger = logging.getLogger(__name__)


class SlackGuardServer:
    """Builds and runs the FastMCP Slack proxy server."""

    def __init__(
        self,
        slack_client: SlackClient,
        privacy_filter: SlackPrivacyFilter,
        server_name: str = "slack-guard",
        server_version: str = "0.1.0",
    ) -> None:
        self._slack = slack_client
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
            name="slack_list_channels",
            description=(
                "List Slack channels visible to the bot (id, name, privacy, "
                "topic, purpose, member count). Requires user approval."
            ),
        )
        async def slack_list_channels(
            exclude_archived: bool = True, max_results: int = 100
        ) -> Any:
            channels = await self._fetch(
                self._slack.list_channels, exclude_archived, max_results
            )
            filtered = self._filter.filter_channels(channels)
            return await self._review(
                tool_name="List Slack Channels",
                summary=f"List channels (max {max_results})",
                sender=f"{len(channels)} channel(s)",
                raw_data=channels,
                filtered_data=filtered,
            )

        @mcp.tool(
            name="slack_get_channel_history",
            description=(
                "Fetch recent messages in a Slack channel by channel id. "
                "Requires user approval."
            ),
        )
        async def slack_get_channel_history(
            channel_id: str, limit: int = 50
        ) -> Any:
            messages = await self._fetch(
                self._slack.get_channel_history, channel_id, limit
            )
            filtered = self._filter.filter_messages(messages)
            return await self._review(
                tool_name="Read Slack Channel",
                summary=f"Channel history: {channel_id} (limit {limit})",
                sender=f"{len(messages)} message(s)",
                raw_data=messages,
                filtered_data=filtered,
            )

        @mcp.tool(
            name="slack_get_thread_replies",
            description=(
                "Fetch all replies in a Slack thread by channel id and "
                "thread_ts. Requires user approval."
            ),
        )
        async def slack_get_thread_replies(
            channel_id: str, thread_ts: str
        ) -> Any:
            messages = await self._fetch(
                self._slack.get_thread_replies, channel_id, thread_ts
            )
            filtered = self._filter.filter_thread(messages)
            return await self._review(
                tool_name="Read Slack Thread",
                summary=f"Thread replies: {channel_id}/{thread_ts}",
                sender=f"{len(messages)} message(s)",
                raw_data=messages,
                filtered_data=filtered,
            )

        @mcp.tool(
            name="slack_search_messages",
            description=(
                "Search Slack messages matching a query. Requires user "
                "approval. (Needs the search:read scope.)"
            ),
        )
        async def slack_search_messages(query: str, count: int = 20) -> Any:
            messages = await self._fetch(
                self._slack.search_messages, query, count
            )
            filtered = self._filter.filter_messages(messages)
            return await self._review(
                tool_name="Search Slack Messages",
                summary=f"Search messages: query={query!r} (count {count})",
                sender=f"{len(messages)} match(es)",
                raw_data=messages,
                filtered_data=filtered,
            )

    # ------------------------------------------------------------------ #
    # Pipeline helpers
    # ------------------------------------------------------------------ #
    async def _fetch(self, func, *args) -> Any:
        """Run a blocking Slack client call off the event loop thread."""
        try:
            return await asyncio.to_thread(func, *args)
        except SlackClientError as exc:
            logger.error("Slack fetch failed: %s", exc)
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
        logger.info("Starting Slack MCP server on stdio transport")
        self._mcp.run(transport="stdio")
