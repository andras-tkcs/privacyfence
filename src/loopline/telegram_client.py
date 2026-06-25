"""Telegram client using Telethon (user client, not bot API).

Authentication is session-based. On first run call ``authorize_interactive()``
to perform the phone+code flow.  Subsequent runs load the existing session file
without prompting.

All methods are async — Telethon is natively asyncio-based.  The connector
awaits them directly on the IPC event loop.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class TelegramClientError(Exception):
    """Raised for unrecoverable Telegram client problems."""


@dataclass
class TelegramChat:
    id: int
    name: str           # display name
    username: str       # @handle or ""
    chat_type: str      # "user" | "group" | "channel" | "bot"
    unread_count: int
    is_self: bool       # True for "Saved Messages"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "username": self.username,
            "chat_type": self.chat_type,
            "unread_count": self.unread_count,
            "is_self": self.is_self,
        }


@dataclass
class TelegramMessage:
    id: int
    chat_id: int
    chat_name: str
    sender_id: int
    sender_name: str
    text: str
    date: str           # ISO 8601
    is_outgoing: bool
    media_type: str     # "" | "photo" | "document" | "video" | "audio" | "sticker"
    media_filename: str # for documents

    def short_summary(self) -> str:
        preview = (self.text or f"[{self.media_type}]")[:60]
        return f"{self.sender_name}: {preview}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "chat_id": self.chat_id,
            "chat_name": self.chat_name,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "text": self.text,
            "date": self.date,
            "is_outgoing": self.is_outgoing,
            "media_type": self.media_type,
            "media_filename": self.media_filename,
        }


class TelegramLooplineClient:
    """Thin async wrapper around the Telethon TelegramClient.

    A single instance is kept alive in the connector.  Call
    ``await connect()`` before any other method.  ``check_connection()``
    combines connect + identity check in one step for daemon startup.
    """

    def __init__(self, api_id: int, api_hash: str, session_file: str) -> None:
        self._api_id = api_id
        self._api_hash = api_hash
        self._session_file = session_file
        self._client = None  # telethon.TelegramClient, built lazily
        self._connected = False
        self._chat_name_cache: dict[int, str] = {}

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def _build_client(self):
        """Construct the Telethon TelegramClient (does not connect)."""
        try:
            from telethon import TelegramClient  # type: ignore[import-untyped]
        except ImportError as exc:
            raise TelegramClientError(
                "Telethon is not installed. Run: pip install telethon>=1.36.0"
            ) from exc
        return TelegramClient(self._session_file, self._api_id, self._api_hash)

    async def connect(self) -> None:
        """Connect to Telegram using the cached session file.

        Does NOT start an interactive auth flow.  If the session is not
        authorized (i.e. the session file does not exist or is expired),
        raises TelegramClientError with instructions on how to authorize.
        """
        if self._connected and self._client is not None:
            return

        client = self._build_client()
        try:
            await client.connect()
        except Exception as exc:
            raise TelegramClientError(f"Failed to connect to Telegram: {exc}") from exc

        if not await client.is_user_authorized():
            await client.disconnect()
            raise TelegramClientError(
                "Telegram session is not authorized. "
                f"Run 'loopline-app --telegram-setup' to authorize interactively. "
                f"Session file: {self._session_file}"
            )

        self._client = client
        self._connected = True
        logger.info("Telegram client connected (session: %s)", self._session_file)

    async def _ensure_connected(self) -> None:
        if not self._connected or self._client is None:
            await self.connect()

    # ------------------------------------------------------------------ #
    # Identity
    # ------------------------------------------------------------------ #

    async def check_connection(self) -> str:
        """Connect and return 'Firstname Lastname (@username)'."""
        await self._ensure_connected()
        try:
            me = await self._client.get_me()  # type: ignore[union-attr]
        except Exception as exc:
            raise TelegramClientError(f"get_me() failed: {exc}") from exc
        name = f"{me.first_name or ''} {me.last_name or ''}".strip()
        username = f"@{me.username}" if me.username else ""
        result = f"{name} ({username})" if username else name
        logger.info("Telegram: connected as %s", result)
        return result

    # ------------------------------------------------------------------ #
    # Read operations
    # ------------------------------------------------------------------ #

    async def list_chats(self, limit: int = 50) -> list[TelegramChat]:
        """List the user's dialogs (chats, groups, channels)."""
        await self._ensure_connected()
        limit = max(1, min(int(limit), 200))
        try:
            dialogs = await self._client.get_dialogs(limit=limit)  # type: ignore[union-attr]
        except Exception as exc:
            raise TelegramClientError(f"get_dialogs() failed: {exc}") from exc

        chats: list[TelegramChat] = []
        for dialog in dialogs:
            entity = dialog.entity
            chat = _parse_dialog(dialog, entity)
            self._chat_name_cache[chat.id] = chat.name
            chats.append(chat)

        logger.info("list_chats: returned %d dialogs", len(chats))
        return chats

    async def get_messages(self, chat_id: int, limit: int = 50) -> list[TelegramMessage]:
        """Fetch recent messages from a specific chat."""
        await self._ensure_connected()
        limit = max(1, min(int(limit), 200))
        try:
            messages = await self._client.get_messages(chat_id, limit=limit)  # type: ignore[union-attr]
        except Exception as exc:
            raise TelegramClientError(
                f"get_messages(chat_id={chat_id}) failed: {exc}"
            ) from exc

        chat_name = self._chat_name_cache.get(chat_id, str(chat_id))
        result: list[TelegramMessage] = []
        for msg in messages:
            result.append(_parse_message(msg, chat_id, chat_name))
        logger.info("get_messages chat_id=%d: returned %d messages", chat_id, len(result))
        return result

    async def search_messages(self, query: str, limit: int = 30) -> list[TelegramMessage]:
        """Global full-text search across all chats."""
        await self._ensure_connected()
        limit = max(1, min(int(limit), 100))
        try:
            messages = await self._client.get_messages(  # type: ignore[union-attr]
                None, search=query, limit=limit
            )
        except Exception as exc:
            raise TelegramClientError(f"search_messages({query!r}) failed: {exc}") from exc

        result: list[TelegramMessage] = []
        for msg in messages:
            chat_id = _peer_id(msg.peer_id)
            chat_name = self._chat_name_cache.get(chat_id, str(chat_id))
            result.append(_parse_message(msg, chat_id, chat_name))
        logger.info("search_messages query=%r: returned %d messages", query, len(result))
        return result

    # ------------------------------------------------------------------ #
    # Interactive authorization
    # ------------------------------------------------------------------ #

    async def authorize_interactive(self) -> None:
        """Interactive phone+code authorization flow. Saves the session file."""
        try:
            from telethon import TelegramClient  # type: ignore[import-untyped]
        except ImportError as exc:
            raise TelegramClientError(
                "Telethon is not installed. Run: pip install telethon>=1.36.0"
            ) from exc

        client = TelegramClient(self._session_file, self._api_id, self._api_hash)
        try:
            await client.start(phone=lambda: input("Telegram phone number (with country code): "))
            me = await client.get_me()
            name = f"{me.first_name or ''} {me.last_name or ''}".strip()
            logger.info("Telegram authorized as %s", name)
            print(f"Authorized as: {name}")
        finally:
            await client.disconnect()


# ------------------------------------------------------------------ #
# Parsing helpers
# ------------------------------------------------------------------ #

def _parse_dialog(dialog: Any, entity: Any) -> TelegramChat:
    """Normalize a Telethon Dialog into a TelegramChat."""
    try:
        from telethon.tl.types import (  # type: ignore[import-untyped]
            Channel,
            Chat,
            User,
        )
    except ImportError:
        Channel = Chat = User = object  # type: ignore[assignment,misc]

    entity_id = getattr(entity, "id", 0)
    name = dialog.name or str(entity_id)
    username = getattr(entity, "username", None) or ""
    unread = getattr(dialog, "unread_count", 0)
    is_self = getattr(entity, "is_self", False)

    if isinstance(entity, User):
        if getattr(entity, "bot", False):
            chat_type = "bot"
        elif is_self:
            chat_type = "user"
        else:
            chat_type = "user"
    elif isinstance(entity, Chat):
        chat_type = "group"
    elif isinstance(entity, Channel):
        if getattr(entity, "megagroup", False):
            chat_type = "group"
        else:
            chat_type = "channel"
    else:
        chat_type = "user"

    return TelegramChat(
        id=entity_id,
        name=name,
        username=username,
        chat_type=chat_type,
        unread_count=unread,
        is_self=is_self,
    )


def _parse_message(msg: Any, chat_id: int, chat_name: str) -> TelegramMessage:
    """Normalize a Telethon Message into a TelegramMessage."""
    sender_id = 0
    sender_name = ""
    if msg.sender:
        sender_id = getattr(msg.sender, "id", 0)
        sender_name = (
            getattr(msg.sender, "username", "")
            or (
                f"{getattr(msg.sender, 'first_name', '') or ''} "
                f"{getattr(msg.sender, 'last_name', '') or ''}"
            ).strip()
            or str(sender_id)
        )

    date_str = ""
    if msg.date:
        dt = msg.date
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        date_str = dt.isoformat()

    media_type, media_filename = _classify_media(msg)
    text = msg.text or msg.message or ""

    return TelegramMessage(
        id=msg.id,
        chat_id=chat_id,
        chat_name=chat_name,
        sender_id=sender_id,
        sender_name=sender_name,
        text=text,
        date=date_str,
        is_outgoing=bool(getattr(msg, "out", False)),
        media_type=media_type,
        media_filename=media_filename,
    )


def _classify_media(msg: Any) -> tuple[str, str]:
    """Return (media_type, media_filename) for a message."""
    media = getattr(msg, "media", None)
    if media is None:
        return "", ""

    class_name = type(media).__name__.lower()

    if "photo" in class_name:
        return "photo", ""
    if "document" in class_name:
        doc = getattr(media, "document", None) or getattr(media, "document", None)
        filename = ""
        if doc:
            for attr in getattr(doc, "attributes", []):
                fn = getattr(attr, "file_name", None)
                if fn:
                    filename = fn
                    break
            mime = getattr(doc, "mime_type", "")
            if "video" in mime:
                return "video", filename
            if "audio" in mime or "voice" in mime:
                return "audio", filename
        return "document", filename
    if "geo" in class_name or "venue" in class_name:
        return "location", ""
    if "poll" in class_name:
        return "poll", ""
    if "contact" in class_name:
        return "contact", ""
    if "sticker" in class_name:
        return "sticker", ""

    # Check via attribute presence for stickers
    doc = getattr(media, "document", None)
    if doc:
        for attr in getattr(doc, "attributes", []):
            if "sticker" in type(attr).__name__.lower():
                return "sticker", ""

    return "media", ""


def _peer_id(peer: Any) -> int:
    """Extract the integer ID from a Telethon Peer object."""
    if peer is None:
        return 0
    return (
        getattr(peer, "user_id", None)
        or getattr(peer, "chat_id", None)
        or getattr(peer, "channel_id", None)
        or 0
    )
