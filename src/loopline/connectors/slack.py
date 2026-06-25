"""Slack connector: wraps SlackClient + SlackPrivacyFilter + gated_call."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..gate import gated_call
from ..privacy_filter import SlackPrivacyFilter
from ..slack_client import SlackClient, SlackClientError

logger = logging.getLogger(__name__)


class SlackConnector(Connector):
    def __init__(self, client: SlackClient, privacy_filter: SlackPrivacyFilter) -> None:
        self._slack = client
        self._filter = privacy_filter
        self.my_email: str = ""

    @property
    def name(self) -> str:
        return "slack"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="slack_list_channels",
                description=(
                    "List Slack channels visible to the bot "
                    "(id, name, privacy, topic, purpose, member count). Auto-approved."
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
            ToolSpec(
                name="slack_send_message",
                description=(
                    "Send a message to a Slack channel or DM. Requires user approval."
                ),
                params=[
                    ToolParam("channel_id", "str"),
                    ToolParam("text", "str"),
                    ToolParam("thread_ts", "str", required=False, default=""),
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
        if tool == "slack_send_message":
            return await self._send_message(**args)
        raise ValueError(f"Unknown Slack tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Always-allowed
    # ------------------------------------------------------------------ #

    async def _list_channels(self, exclude_archived: bool = True, max_results: int = 100) -> Any:
        t0 = time.time()
        channels = await self._fetch(self._slack.list_channels, exclude_archived, max_results)
        filtered = self._filter.filter_channels(channels)
        self._auto_audit(
            "slack_list_channels", "List Slack Channels",
            f"List channels (max {max_results})", f"{len(channels)} channel(s)", t0,
        )
        return filtered

    # ------------------------------------------------------------------ #
    # Gated
    # ------------------------------------------------------------------ #

    async def _get_channel_history(self, channel_id: str, limit: int = 50) -> Any:
        messages = await self._fetch(self._slack.get_channel_history, channel_id, limit)
        filtered = self._filter.filter_messages(messages)
        n = len(messages)
        preview = ""
        if messages:
            first = messages[0]
            preview = (first.get("text") or "")[:160]
        return await gated_call(
            connector=self.name,
            tool="slack_get_channel_history",
            tool_name="Read Slack Channel",
            summary=f"{n} message{'s' if n != 1 else ''} from {channel_id}",
            sender=channel_id,
            raw_data=messages,
            filtered_data=filtered,
            display_hint={
                "type": "slack_read",
                "channel_id": channel_id,
                "message_count": n,
                "preview": preview,
            },
            my_email=self.my_email,
            args={"channel_id": channel_id},
        )

    async def _get_thread_replies(self, channel_id: str, thread_ts: str) -> Any:
        messages = await self._fetch(self._slack.get_thread_replies, channel_id, thread_ts)
        filtered = self._filter.filter_thread(messages)
        n = len(messages)
        preview = ""
        if messages:
            first = messages[0]
            preview = (first.get("text") or "")[:160]
        return await gated_call(
            connector=self.name,
            tool="slack_get_thread_replies",
            tool_name="Read Slack Thread",
            summary=f"{n} repl{'ies' if n != 1 else 'y'} in {channel_id}",
            sender=channel_id,
            raw_data=messages,
            filtered_data=filtered,
            display_hint={
                "type": "slack_read",
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "message_count": n,
                "preview": preview,
            },
            my_email=self.my_email,
            args={"channel_id": channel_id, "thread_ts": thread_ts},
        )

    async def _search_messages(self, query: str, count: int = 20) -> Any:
        messages = await self._fetch(self._slack.search_messages, query, count)
        filtered = self._filter.filter_messages(messages)
        n = len(messages)
        return await gated_call(
            connector=self.name,
            tool="slack_search_messages",
            tool_name="Search Slack",
            summary=f"{n} result{'s' if n != 1 else ''} for \"{query}\"",
            sender=query,
            raw_data=messages,
            filtered_data=filtered,
            display_hint={
                "type": "slack_search",
                "query": query,
                "result_count": n,
            },
            my_email=self.my_email,
            args={"query": query},
        )

    async def _send_message(self, channel_id: str, text: str, thread_ts: str = "") -> Any:
        in_thread = bool(thread_ts)
        return await gated_call(
            connector=self.name,
            tool="slack_send_message",
            tool_name="Send Slack Message",
            summary=f"To {channel_id}: {text[:80]}{'…' if len(text) > 80 else ''}",
            sender=channel_id,
            raw_data={"channel_id": channel_id, "text": text, "thread_ts": thread_ts},
            filtered_data={"channel_id": channel_id, "text": text, "thread_ts": thread_ts},
            display_hint={
                "type": "slack_send",
                "channel_id": channel_id,
                "text": text,
                "in_thread": in_thread,
            },
            my_email=self.my_email,
            args={"channel_id": channel_id, "thread_ts": thread_ts},
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _fetch(self, func, *args) -> Any:
        try:
            return await asyncio.to_thread(func, *args)
        except SlackClientError as exc:
            logger.error("Slack fetch failed: %s", exc)
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
