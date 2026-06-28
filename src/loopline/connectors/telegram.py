"""Telegram connector."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..gate import gated_call
from ..telegram_client import TelegramClientError, TelegramLooplineClient

logger = logging.getLogger(__name__)


class TelegramConnector(Connector):
    def __init__(self, client: TelegramLooplineClient) -> None:
        self._telegram = client

    @property
    def name(self) -> str:
        return "telegram"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="telegram_list_chats",
                description="List Telegram chats (id, name, type, unread count). Auto-approved.",
                params=[
                    ToolParam("limit", "int", required=False, default=50),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="telegram_get_messages",
                description=(
                    "Fetch recent messages from a Telegram chat by chat id. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("chat_id", "int"),
                    ToolParam("limit", "int", required=False, default=50),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="telegram_search_messages",
                description=(
                    "Search messages across Telegram chats by keyword. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("query", "str"),
                    ToolParam("limit", "int", required=False, default=30),
                ],
                read_only=True,
            ),
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "telegram_list_chats":
            return await self._list_chats(**args)
        if tool == "telegram_get_messages":
            return await self._get_messages(**args)
        if tool == "telegram_search_messages":
            return await self._search_messages(**args)
        raise ValueError(f"Unknown Telegram tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Auto
    # ------------------------------------------------------------------ #

    async def _list_chats(self, limit: int = 50) -> Any:
        t0 = time.time()
        try:
            chats = await self._telegram.list_chats(limit)
        except TelegramClientError as exc:
            raise RuntimeError(str(exc)) from exc
        result = [
            {"id": c.id, "name": c.name, "type": c.type, "unread_count": c.unread_count}
            for c in chats
        ]
        self._auto_audit("telegram_list_chats", "List Telegram Chats",
                         f"List chats (max {limit})", f"{len(result)} chat(s)", t0)
        return result

    # ------------------------------------------------------------------ #
    # Review gate (reads)
    # ------------------------------------------------------------------ #

    async def _get_messages(self, chat_id: int, limit: int = 50) -> Any:
        try:
            messages = await self._telegram.get_messages(chat_id, limit)
        except TelegramClientError as exc:
            raise RuntimeError(str(exc)) from exc
        chat_name = str(chat_id)
        if messages and hasattr(messages[0], "chat_name"):
            chat_name = messages[0].chat_name or chat_name
        n = len(messages)
        preview = {"Chat": chat_name, "Messages": str(n)}
        lines = [
            f"[{getattr(m, 'date', '')}] {getattr(m, 'sender_name', 'unknown')}: {getattr(m, 'text', '')}"
            for m in messages
        ]
        result = [
            {
                "id": getattr(m, "id", ""),
                "sender_name": getattr(m, "sender_name", ""),
                "text": getattr(m, "text", ""),
                "date": str(getattr(m, "date", "")),
            }
            for m in messages
        ]
        return await gated_call(
            connector=self.name,
            tool="telegram_get_messages",
            tool_name="Read Telegram Chat",
            summary=f"{n} message{'s' if n != 1 else ''} from {chat_name}",
            sender=chat_name,
            raw_data=messages,
            filtered_data=result,
            gate="review",
            preview=preview,
            details_text=f"Chat: {chat_name}\n\n" + "\n".join(lines),
            args={"chat_id": chat_id},
        )

    async def _search_messages(self, query: str, limit: int = 30) -> Any:
        try:
            messages = await self._telegram.search_messages(query, limit)
        except TelegramClientError as exc:
            raise RuntimeError(str(exc)) from exc
        n = len(messages)
        preview = {"Query": query, "Results": str(n)}
        lines = [
            f"[{getattr(m, 'chat_name', '')}] {getattr(m, 'sender_name', 'unknown')}: {getattr(m, 'text', '')}"
            for m in messages
        ]
        result = [
            {
                "id": getattr(m, "id", ""),
                "chat_name": getattr(m, "chat_name", ""),
                "sender_name": getattr(m, "sender_name", ""),
                "text": getattr(m, "text", ""),
                "date": str(getattr(m, "date", "")),
            }
            for m in messages
        ]
        return await gated_call(
            connector=self.name,
            tool="telegram_search_messages",
            tool_name="Search Telegram",
            summary=f"{n} result{'s' if n != 1 else ''} for \"{query}\"",
            sender=query,
            raw_data=messages,
            filtered_data=result,
            gate="review",
            preview=preview,
            details_text=f"Search: {query}\n\n" + "\n".join(lines),
            args={"query": query},
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

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
