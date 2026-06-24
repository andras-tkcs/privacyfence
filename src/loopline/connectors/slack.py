"""Slack connector: wraps SlackClient + SlackPrivacyFilter + ReviewQueue."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..connector import Connector, ToolParam, ToolSpec
from ..privacy_filter import SlackPrivacyFilter
from ..review_queue import ReviewRejected, get_review_queue
from ..slack_client import SlackClient, SlackClientError

logger = logging.getLogger(__name__)


class SlackConnector(Connector):
    def __init__(self, client: SlackClient, privacy_filter: SlackPrivacyFilter) -> None:
        self._slack = client
        self._filter = privacy_filter
        self._queue = get_review_queue()

    @property
    def name(self) -> str:
        return "slack"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="slack_list_channels",
                description=(
                    "List Slack channels visible to the bot "
                    "(id, name, privacy, topic, purpose, member count). Requires user approval."
                ),
                params=[
                    ToolParam("exclude_archived", "bool", required=False, default=True),
                    ToolParam("max_results", "int", required=False, default=100),
                ],
            ),
            ToolSpec(
                name="slack_get_channel_history",
                description=(
                    "Fetch recent messages in a Slack channel by channel id. Requires user approval."
                ),
                params=[
                    ToolParam("channel_id", "str"),
                    ToolParam("limit", "int", required=False, default=50),
                ],
            ),
            ToolSpec(
                name="slack_get_thread_replies",
                description=(
                    "Fetch all replies in a Slack thread by channel id and thread_ts. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("channel_id", "str"),
                    ToolParam("thread_ts", "str"),
                ],
            ),
            ToolSpec(
                name="slack_search_messages",
                description=(
                    "Search Slack messages matching a query. "
                    "Requires user approval. (Needs the search:read scope.)"
                ),
                params=[
                    ToolParam("query", "str"),
                    ToolParam("count", "int", required=False, default=20),
                ],
            ),
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "slack_list_channels":
            return await self._list_channels(**args)
        if tool == "slack_get_channel_history":
            return await self._get_channel_history(**args)
        if tool == "slack_get_thread_replies":
            return await self._get_thread_replies(**args)
        if tool == "slack_search_messages":
            return await self._search_messages(**args)
        raise ValueError(f"Unknown Slack tool: {tool!r}")

    # ------------------------------------------------------------------ #

    async def _list_channels(self, exclude_archived: bool = True, max_results: int = 100) -> Any:
        channels = await self._fetch(self._slack.list_channels, exclude_archived, max_results)
        filtered = self._filter.filter_channels(channels)
        return await self._review(
            tool_name="List Slack Channels",
            summary=f"List channels (max {max_results})",
            sender=f"{len(channels)} channel(s)",
            raw_data=channels,
            filtered_data=filtered,
        )

    async def _get_channel_history(self, channel_id: str, limit: int = 50) -> Any:
        messages = await self._fetch(self._slack.get_channel_history, channel_id, limit)
        filtered = self._filter.filter_messages(messages)
        return await self._review(
            tool_name="Read Slack Channel",
            summary=f"Channel history: {channel_id} (limit {limit})",
            sender=f"{len(messages)} message(s)",
            raw_data=messages,
            filtered_data=filtered,
        )

    async def _get_thread_replies(self, channel_id: str, thread_ts: str) -> Any:
        messages = await self._fetch(self._slack.get_thread_replies, channel_id, thread_ts)
        filtered = self._filter.filter_thread(messages)
        return await self._review(
            tool_name="Read Slack Thread",
            summary=f"Thread replies: {channel_id}/{thread_ts}",
            sender=f"{len(messages)} message(s)",
            raw_data=messages,
            filtered_data=filtered,
        )

    async def _search_messages(self, query: str, count: int = 20) -> Any:
        messages = await self._fetch(self._slack.search_messages, query, count)
        filtered = self._filter.filter_messages(messages)
        return await self._review(
            tool_name="Search Slack Messages",
            summary=f"Search messages: query={query!r} (count {count})",
            sender=f"{len(messages)} match(es)",
            raw_data=messages,
            filtered_data=filtered,
        )

    # ------------------------------------------------------------------ #

    async def _fetch(self, func, *args) -> Any:
        try:
            return await asyncio.to_thread(func, *args)
        except SlackClientError as exc:
            logger.error("Slack fetch failed: %s", exc)
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
