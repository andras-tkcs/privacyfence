"""Gmail connector: wraps GmailClient + PrivacyFilter + ReviewQueue."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..connector import Connector, ToolParam, ToolSpec
from ..gmail_client import GmailClient, GmailClientError
from ..privacy_filter import PrivacyFilter
from ..review_queue import ReviewRejected, get_review_queue

logger = logging.getLogger(__name__)

_TOOL_CALL_ERROR = "Tool call failed: {}"
_REJECTED_MSG = "Request denied by user: {}"


class GmailConnector(Connector):
    def __init__(self, client: GmailClient, privacy_filter: PrivacyFilter) -> None:
        self._gmail = client
        self._filter = privacy_filter
        self._queue = get_review_queue()

    @property
    def name(self) -> str:
        return "gmail"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="gmail_list_messages",
                description=(
                    "Search Gmail and return matching message summaries "
                    "(id, thread_id, subject, sender, date). "
                    "Auto-approved — no body content is returned."
                ),
                params=[
                    ToolParam("query", "str"),
                    ToolParam("max_results", "int", required=False, default=10),
                ],
            ),
            ToolSpec(
                name="gmail_list_threads",
                description=(
                    "Search Gmail and return matching thread summaries "
                    "(id, snippet). Auto-approved — no body content is returned."
                ),
                params=[
                    ToolParam("query", "str"),
                    ToolParam("max_results", "int", required=False, default=10),
                ],
            ),
            ToolSpec(
                name="gmail_get_message",
                description=(
                    "Fetch a single Gmail message by id, including body, metadata, "
                    "and attachment list. Requires user approval."
                ),
                params=[ToolParam("message_id", "str")],
            ),
            ToolSpec(
                name="gmail_get_thread",
                description=(
                    "Fetch a full Gmail thread by id, including its messages. "
                    "Requires user approval."
                ),
                params=[ToolParam("thread_id", "str")],
            ),
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "gmail_list_messages":
            return await self._list_messages(**args)
        if tool == "gmail_list_threads":
            return await self._list_threads(**args)
        if tool == "gmail_get_message":
            return await self._get_message(**args)
        if tool == "gmail_get_thread":
            return await self._get_thread(**args)
        raise ValueError(f"Unknown Gmail tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Tool implementations
    # ------------------------------------------------------------------ #

    async def _list_messages(self, query: str, max_results: int = 10) -> Any:
        summaries = await self._fetch(self._gmail.list_messages, query, max_results)
        filtered = [self._filter_summary(s) for s in summaries]
        logger.info("gmail_list_messages auto-approved: query=%r results=%d", query, len(filtered))
        return filtered

    async def _list_threads(self, query: str, max_results: int = 10) -> Any:
        summaries = await self._fetch(self._gmail.list_threads, query, max_results)
        logger.info("gmail_list_threads auto-approved: query=%r results=%d", query, len(summaries))
        return summaries

    async def _get_message(self, message_id: str) -> Any:
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

    async def _get_thread(self, thread_id: str) -> Any:
        thread = await self._fetch(self._gmail.get_thread, thread_id)
        filtered = self._filter.filter_thread(thread)
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
    # Helpers
    # ------------------------------------------------------------------ #

    async def _fetch(self, func, *args) -> Any:
        try:
            return await asyncio.to_thread(func, *args)
        except GmailClientError as exc:
            logger.error("Gmail fetch failed: %s", exc)
            raise RuntimeError(str(exc)) from exc

    async def _review(self, *, tool_name, summary, sender, raw_data, filtered_data, display_hint=None) -> Any:
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
            logger.info("Tool %s rejected: %s", tool_name, exc)
            raise RuntimeError(_REJECTED_MSG.format(exc)) from exc
        logger.info("Tool %s approved", tool_name)
        return result

    def _filter_summary(self, summary: dict[str, str]) -> dict[str, str]:
        policy = self._filter.policy_for("metadata")
        result = dict(summary)
        for field_name in ("subject", "date"):
            if field_name in result:
                result[field_name] = PrivacyFilter._apply_text(result[field_name], policy)  # noqa: SLF001
        if "sender" in result:
            result["sender"] = PrivacyFilter._apply_address(result["sender"], policy)  # noqa: SLF001
        return result
