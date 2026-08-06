"""Telegram client using Telethon (user client, not bot API).

Authentication is session-based. On first run call ``authorize_interactive()``
to perform the phone+code flow.  Subsequent runs load the existing session file
without prompting.

All methods are async — Telethon is natively asyncio-based.  The connector
awaits them directly on the IPC event loop.

``TelegramPrivacyFenceClient`` optionally persists a whole-account chat-name
snapshot to disk (``chat_cache_file``: numeric chat id -> display name, across
users, groups, and channels). When given, ``get_chat_name``/``get_messages``/
``search_messages`` check it first, refreshing it from Telegram (``get_dialogs``)
automatically about once a week, instead of ``search_messages`` in particular
falling back to a bare numeric chat id for every message whose chat hasn't been
seen yet this session (``list_chats`` only ever primes the in-memory cache for
whatever page of dialogs it was last asked for, and that cache doesn't survive
a restart). Defaults to "" (disabled) -- resolution then behaves exactly as it
did before this existed. See ``refresh_chat_directory`` for the explicit,
immediate re-sync the ``telegram_refresh_chat_cache`` bridge tool uses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# How long the on-disk whole-account chat-name snapshot (see
# refresh_chat_directory()) is trusted before get_chat_name()/get_messages()/
# search_messages() transparently re-sync it. A week is plenty for who you
# chat with to stay useful; the telegram_refresh_chat_cache bridge tool
# covers the mid-week exception (a brand-new chat that isn't in last week's
# snapshot yet).
CHAT_DIRECTORY_CACHE_TTL = timedelta(days=7)

# Floor between automatic re-sync attempts once one has failed (e.g. a
# transient network error) -- without this, every cache-miss lookup while the
# directory is stale/unrefreshable would retry the full dialog listing from
# scratch, recreating the very per-lookup problem this cache exists to avoid.
_DIRECTORY_RETRY_COOLDOWN = timedelta(minutes=5)


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


class TelegramPrivacyFenceClient:
    """Thin async wrapper around the Telethon TelegramClient.

    A single instance is kept alive in the connector.  Call
    ``await connect()`` before any other method.  ``check_connection()``
    combines connect + identity check in one step for daemon startup.
    """

    def __init__(
        self, api_id: int, api_hash: str, session_file: str, chat_cache_file: str = ""
    ) -> None:
        self._api_id = api_id
        self._api_hash = api_hash
        self._session_file = session_file
        self._client = None  # telethon.TelegramClient, built lazily
        self._connected = False
        # Small cache so repeated messages from/about the same chat don't
        # trigger a fresh lookup each time within a single fetch. Also
        # doubles as the in-memory home for the whole-account chat directory
        # snapshot below -- a directory hit and a one-off live resolution are
        # indistinguishable to callers, both just end up in this same dict.
        self._chat_name_cache: dict[int, str] = {}

        # Weekly on-disk snapshot (see _ensure_chat_directory/
        # refresh_chat_directory) -- turns the per-uncached-chat get_entity
        # lookup a search/history fetch would otherwise need for anyone/
        # anything not already known as of the last refresh into zero.
        # Empty string (the default) opts this cache out entirely --
        # resolution behaves exactly as before this existed, no lookup at
        # all for a chat missing from list_chats()'s in-memory cache (just
        # the numeric chat id), nothing persisted to disk.
        self._chat_cache_file = chat_cache_file
        self._chat_directory_loaded_from_disk = False
        self._chat_directory_fetched_at: datetime | None = None
        self._chat_directory_last_attempt: datetime | None = None

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
                f"Run 'privacyfence-app --telegram-setup' to authorize interactively. "
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

        if self._chat_cache_file:
            await self._ensure_chat_directory()
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

        if self._chat_cache_file:
            await self._ensure_chat_directory()
        result: list[TelegramMessage] = []
        for msg in messages:
            chat_id = _peer_id(msg.peer_id)
            chat_name = self._chat_name_cache.get(chat_id, str(chat_id))
            result.append(_parse_message(msg, chat_id, chat_name))
        logger.info("search_messages query=%r: returned %d messages", query, len(result))
        return result

    async def get_chat_name(self, chat_id: int) -> str:
        """Best-effort chat display-name lookup (cached, never raises).

        When a ``chat_cache_file`` was given at construction, checks the
        weekly whole-account directory first (see ``_ensure_chat_directory``/
        ``refresh_chat_directory``); otherwise (or on a directory miss)
        reuses ``list_chats()``'s in-memory cache when already populated, or
        resolves the entity directly via Telethon -- same fallback as before
        this cache existed, for anyone missing from the directory (a chat
        started since the last refresh).
        """
        if self._chat_cache_file:
            await self._ensure_chat_directory()
        if chat_id in self._chat_name_cache:
            return self._chat_name_cache[chat_id]
        try:
            from telethon import utils as telethon_utils  # type: ignore[import-untyped]

            await self._ensure_connected()
            entity = await self._client.get_entity(chat_id)  # type: ignore[union-attr]
            name = telethon_utils.get_display_name(entity) or ""
        except Exception as exc:
            logger.debug("Could not resolve chat name for %s: %s", chat_id, exc)
            return ""
        if name:
            self._chat_name_cache[chat_id] = name
        return name

    # ------------------------------------------------------------------ #
    # Directory cache (whole-account chat-name snapshot)
    # ------------------------------------------------------------------ #

    async def ensure_chat_directory_fresh(self) -> None:
        """Eagerly run the same freshness check ``get_chat_name``/
        ``get_messages``/``search_messages`` would otherwise only run lazily
        on first use. Called once right after daemon startup, same as
        Slack's directory-cache counterpart -- see ``_warm_connector_caches``
        in ``daemon_main.py``, which schedules this on the IPC server's own
        event loop rather than awaiting it inline: Telethon's client binds
        to whichever loop first connects it, and that has to be the loop
        every Telegram tool call actually runs on, not a throwaway one.
        Running it in the background (rather than blocking daemon startup on
        it, the way an inline call would) also means a snapshot that's gone
        stale while the app was closed doesn't delay the menu bar icon
        appearing. This does force a Telegram connection at startup, where
        before this existed Telegram connected lazily on first tool call --
        an accepted tradeoff now that the connection happens off the
        critical path. A no-op (no network call at all) when the snapshot is
        already fresh, and best-effort like the lazy path it shares its
        implementation with -- never raises.
        """
        if self._chat_cache_file:
            await self._ensure_chat_directory()

    async def refresh_chat_directory(self) -> int:
        """Force an immediate re-sync of every chat/group/channel dialog via
        ``get_dialogs(limit=None)`` (Telethon paginates internally),
        replacing the current chat-name snapshot and resetting its weekly
        TTL. Raises on failure -- unlike the lazy, best-effort refresh
        ``_ensure_chat_directory`` runs automatically, this is the explicit
        action a caller takes (the ``telegram_refresh_chat_cache`` bridge
        tool) when a chat started mid-week needs to resolve correctly right
        now, rather than waiting for next week's automatic refresh or the
        one-off ``get_entity`` fallback ``get_chat_name`` already does for
        any id missing from the directory. Returns the number of chats
        cached.
        """
        await self._ensure_connected()
        try:
            dialogs = await self._client.get_dialogs(limit=None)  # type: ignore[union-attr]
        except Exception as exc:
            raise TelegramClientError(f"refresh_chat_directory failed: {exc}") from exc
        names: dict[int, str] = {}
        for dialog in dialogs:
            entity_id = getattr(dialog.entity, "id", 0)
            if entity_id:
                names[entity_id] = dialog.name or str(entity_id)
        self._chat_name_cache = names
        self._chat_directory_loaded_from_disk = True
        self._chat_directory_fetched_at = datetime.now(timezone.utc)
        self._save_chat_directory_to_disk()
        logger.info("Telegram chat directory refreshed: %d chat(s) cached", len(names))
        return len(names)

    async def _ensure_chat_directory(self) -> None:
        """Best-effort: loads the on-disk weekly snapshot (once per process)
        and transparently re-syncs it from Telegram if it's missing or older
        than a week. Failures are logged and swallowed, subject to a retry
        cooldown -- a directory miss just means callers fall back to
        whatever they did before this cache existed (list_chats()'s
        in-memory cache, a numeric chat id, or a one-off get_entity call)."""
        if not self._chat_directory_loaded_from_disk:
            self._load_chat_directory_from_disk()
            self._chat_directory_loaded_from_disk = True
        now = datetime.now(timezone.utc)
        if (
            self._chat_directory_fetched_at
            and now - self._chat_directory_fetched_at < CHAT_DIRECTORY_CACHE_TTL
        ):
            return
        if (
            self._chat_directory_last_attempt
            and now - self._chat_directory_last_attempt < _DIRECTORY_RETRY_COOLDOWN
        ):
            return
        self._chat_directory_last_attempt = now
        try:
            await self.refresh_chat_directory()
        except TelegramClientError as exc:
            logger.warning("Could not refresh Telegram chat directory (non-fatal): %s", exc)

    def _load_chat_directory_from_disk(self) -> None:
        if not self._chat_cache_file or not os.path.exists(self._chat_cache_file):
            return
        try:
            with open(self._chat_cache_file, encoding="utf-8") as fh:
                data = json.load(fh)
            fetched_at = datetime.fromisoformat(data.get("fetched_at", ""))
            raw_chats = data.get("chats") or {}
        except Exception as exc:
            logger.warning("Could not load Telegram chat directory cache (non-fatal): %s", exc)
            return
        loaded = 0
        for chat_id_str, name in raw_chats.items():
            try:
                chat_id = int(chat_id_str)
            except (TypeError, ValueError):
                continue
            # setdefault: never clobber a fresher live resolution already
            # cached this session (e.g. via list_chats()) before the disk
            # load ran.
            self._chat_name_cache.setdefault(chat_id, name)
            loaded += 1
        self._chat_directory_fetched_at = fetched_at
        logger.debug("Loaded %d cached Telegram chat name(s) from disk", loaded)

    def _save_chat_directory_to_disk(self) -> None:
        if not self._chat_cache_file or self._chat_directory_fetched_at is None:
            return
        payload = {
            "fetched_at": self._chat_directory_fetched_at.isoformat(),
            "chats": {str(cid): name for cid, name in self._chat_name_cache.items()},
        }
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._chat_cache_file)), exist_ok=True)
            with open(self._chat_cache_file, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.chmod(self._chat_cache_file, 0o600)  # DM chat names are real people's names
        except OSError as exc:
            logger.warning("Could not save Telegram chat directory cache (non-fatal): %s", exc)

    async def send_message(self, chat_id: int, text: str) -> dict:
        """Send a text message to a chat by its numeric id."""
        await self._ensure_connected()
        if not text:
            raise TelegramClientError("send_message requires non-empty text")
        try:
            msg = await self._client.send_message(chat_id, text)  # type: ignore[union-attr]
        except Exception as exc:
            raise TelegramClientError(
                f"send_message(chat_id={chat_id}) failed: {exc}"
            ) from exc
        chat_name = self._chat_name_cache.get(chat_id, str(chat_id))
        logger.info("send_message: chat_id=%d msg_id=%s", chat_id, getattr(msg, "id", "?"))
        return {"chat_id": chat_id, "chat_name": chat_name, "msg_id": getattr(msg, "id", None), "text": text}

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
