"""Telegram connector: read-only access to Telegram chats and messages.

All tools are always-allowed per the privacy matrix (no review gate).
Calls are logged to the audit log with decision="auto_accepted".

Telethon is async-native; the connector's async call() awaits Telethon
methods directly on the IPC event loop (no asyncio.to_thread needed).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..telegram_client import TelegramClientError, TelegramLooplineClient

logger = logging.getLogger(__name__)


class TelegramConnector(Connector):
    """Read-only connector for Telegram.

    All three tools (list_chats, get_messages, search_messages) are
    always-allowed and bypass the review queue.
    """

    def __init__(self, client: TelegramLooplineClient) -> None:
        self._tg = client

    @property
    def name(self) -> str:
        return "telegram"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="telegram_list_chats",
                description=(
                    "List the user's Telegram dialogs (chats, groups, channels). "
                    "Returns id, name, username, type, and unread count. "
                    "Auto-approved — read-only."
                ),
                params=[
                    ToolParam("limit", "int", required=False, default=50),
                ],
            ),
            ToolSpec(
                name="telegram_get_messages",
                description=(
                    "Fetch recent messages from a specific Telegram chat by its numeric id. "
                    "Auto-approved — read-only."
                ),
                params=[
                    ToolParam("chat_id", "int"),
                    ToolParam("limit", "int", required=False, default=50),
                ],
            ),
            ToolSpec(
                name="telegram_search_messages",
                description=(
                    "Global full-text search for messages across all Telegram chats. "
                    "Auto-approved — read-only."
                ),
                params=[
                    ToolParam("query", "str"),
                    ToolParam("limit", "int", required=False, default=30),
                ],
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
    # Tool implementations
    # ------------------------------------------------------------------ #

    async def _list_chats(self, limit: int = 50) -> Any:
        try:
            chats = await self._tg.list_chats(limit=limit)
        except TelegramClientError as exc:
            logger.error("telegram_list_chats failed: %s", exc)
            raise RuntimeError(str(exc)) from exc

        result = [c.to_dict() for c in chats]
        self._log_always_allowed("telegram_list_chats", {"limit": limit})
        logger.info("telegram_list_chats auto-accepted: %d chats", len(result))
        return result

    async def _get_messages(self, chat_id: int, limit: int = 50) -> Any:
        try:
            messages = await self._tg.get_messages(chat_id=int(chat_id), limit=limit)
        except TelegramClientError as exc:
            logger.error("telegram_get_messages failed: %s", exc)
            raise RuntimeError(str(exc)) from exc

        result = [m.to_dict() for m in messages]
        self._log_always_allowed("telegram_get_messages", {"chat_id": chat_id, "limit": limit})
        logger.info("telegram_get_messages chat_id=%s auto-accepted: %d messages", chat_id, len(result))
        return result

    async def _search_messages(self, query: str, limit: int = 30) -> Any:
        try:
            messages = await self._tg.search_messages(query=query, limit=limit)
        except TelegramClientError as exc:
            logger.error("telegram_search_messages failed: %s", exc)
            raise RuntimeError(str(exc)) from exc

        result = [m.to_dict() for m in messages]
        self._log_always_allowed("telegram_search_messages", {"query": query, "limit": limit})
        logger.info("telegram_search_messages query=%r auto-accepted: %d messages", query, len(result))
        return result

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _log_always_allowed(self, tool: str, args: dict[str, Any]) -> None:
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id="",
                connector=self.name,
                tool=tool,
                tool_name=tool,
                summary=str(args),
                sender="",
                decision="auto_accepted",
                auto_accept_rule="always_allowed",
                latency_seconds=0.0,
            ))
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)
