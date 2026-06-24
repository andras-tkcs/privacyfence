"""Slack API client.

Read-only access to a Slack workspace via a Bot User OAuth Token
(``xoxb-...``). Unlike the Gmail client there is no interactive OAuth flow: the
token is provisioned once in the Slack app settings and stored in config.

All Slack API payloads are normalized into simple dataclasses so the rest of
the application never has to deal with the raw API response shape.

We use the documented ``slack_sdk`` library (``pip install slack-sdk``).

Required bot token scopes (configured in the Slack app):
  - ``channels:read`` / ``groups:read`` : list channels
  - ``channels:history`` / ``groups:history`` : read channel history & replies
  - ``users:read`` / ``users:read.email`` : resolve user identity
  - ``search:read`` : search messages (note: search.messages requires a
    user token in many workspaces; see ``search_messages`` for details)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)


class SlackClientError(Exception):
    """Raised for unrecoverable Slack client problems (auth, config, API)."""


@dataclass
class SlackFile:
    """File metadata attached to a Slack message. Content is never carried."""

    id: str
    name: str
    title: str
    mimetype: str
    size: int  # bytes, as reported by Slack (0 if unknown)
    url_private: str = ""


@dataclass
class SlackMessage:
    """A normalized Slack message."""

    id: str  # the message ts
    channel_id: str
    channel_name: str
    user_id: str
    user_name: str
    text: str
    thread_ts: str = ""
    reply_count: int = 0
    attachments: list[dict[str, Any]] = field(default_factory=list)
    files: list[SlackFile] = field(default_factory=list)
    timestamp: Optional[datetime] = None

    def short_summary(self) -> str:
        """Human-readable one-liner for the review UI / logs."""
        who = self.user_name or self.user_id or "(unknown user)"
        snippet = (self.text or "").replace("\n", " ").strip()
        if len(snippet) > 60:
            snippet = snippet[:59] + "…"
        if not snippet:
            snippet = "(no text)"
        return f"{who}: {snippet}"


@dataclass
class SlackChannel:
    """A normalized Slack channel."""

    id: str
    name: str
    is_private: bool = False
    topic: str = ""
    purpose: str = ""
    member_count: int = 0

    def short_summary(self) -> str:
        kind = "private" if self.is_private else "public"
        return f"#{self.name} ({kind}, {self.member_count} members)"


@dataclass
class SlackUser:
    """A normalized Slack user."""

    id: str
    name: str
    real_name: str = ""
    email: str = ""
    is_bot: bool = False

    def short_summary(self) -> str:
        return self.real_name or self.name or self.id


class SlackClient:
    """Read-only Slack client backed by a static bot token."""

    def __init__(self, bot_token: str) -> None:
        if not bot_token:
            raise SlackClientError(
                "No Slack bot token configured. Set 'slack.bot_token' "
                "(a 'xoxb-...' Bot User OAuth Token) in the config."
            )
        self._token = bot_token
        self._client = WebClient(token=bot_token)
        # Small cache so repeated messages from the same author don't trigger a
        # users.info call each time within a single fetch.
        self._user_cache: dict[str, SlackUser] = {}
        self._channel_name_cache: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #
    def check_connection(self) -> str:
        """Verify the token works. Returns the workspace (team) name."""
        try:
            response = self._client.auth_test()
        except SlackApiError as exc:
            raise SlackClientError(
                f"Slack connection check failed: {self._describe_error(exc)}"
            ) from exc
        team = response.get("team", "unknown workspace")
        user = response.get("user", "unknown bot")
        logger.info("Connected to Slack workspace %r as %r", team, user)
        return team

    # ------------------------------------------------------------------ #
    # Read operations
    # ------------------------------------------------------------------ #
    def list_channels(
        self, exclude_archived: bool = True, max_results: int = 100
    ) -> list[SlackChannel]:
        """List channels visible to the bot.

        Uses ``conversations.list`` with public and private channel types.
        """
        max_results = self._clamp(max_results, default=100, hi=1000)
        channels: list[SlackChannel] = []
        cursor: Optional[str] = None
        try:
            while len(channels) < max_results:
                page_size = min(200, max_results - len(channels))
                response = self._client.conversations_list(
                    exclude_archived=exclude_archived,
                    types="public_channel,private_channel",
                    limit=page_size,
                    cursor=cursor,
                )
                for raw in response.get("channels", []):
                    channels.append(self._parse_channel(raw))
                    self._channel_name_cache[raw.get("id", "")] = raw.get("name", "")
                cursor = (response.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
        except SlackApiError as exc:
            raise SlackClientError(
                f"list_channels failed: {self._describe_error(exc)}"
            ) from exc

        channels = channels[:max_results]
        logger.info("list_channels returned %d channel(s)", len(channels))
        return channels

    def get_channel_history(
        self,
        channel_id: str,
        limit: int = 50,
        oldest: str = None,
        latest: str = None,
    ) -> list[SlackMessage]:
        """Fetch recent messages in a channel via ``conversations.history``."""
        if not channel_id:
            raise SlackClientError("get_channel_history requires a channel_id")
        limit = self._clamp(limit, default=50, hi=1000)
        channel_name = self._resolve_channel_name(channel_id)

        kwargs: dict[str, Any] = {"channel": channel_id, "limit": limit}
        if oldest:
            kwargs["oldest"] = oldest
        if latest:
            kwargs["latest"] = latest

        try:
            response = self._client.conversations_history(**kwargs)
        except SlackApiError as exc:
            raise SlackClientError(
                f"get_channel_history({channel_id}) failed: "
                f"{self._describe_error(exc)}"
            ) from exc

        messages = [
            self._parse_message(raw, channel_id, channel_name)
            for raw in response.get("messages", [])
        ]
        logger.info(
            "get_channel_history %s returned %d message(s)", channel_id, len(messages)
        )
        return messages

    def get_thread_replies(
        self, channel_id: str, thread_ts: str
    ) -> list[SlackMessage]:
        """Fetch all replies in a thread via ``conversations.replies``."""
        if not channel_id or not thread_ts:
            raise SlackClientError(
                "get_thread_replies requires a channel_id and thread_ts"
            )
        channel_name = self._resolve_channel_name(channel_id)
        try:
            response = self._client.conversations_replies(
                channel=channel_id, ts=thread_ts
            )
        except SlackApiError as exc:
            raise SlackClientError(
                f"get_thread_replies({channel_id}, {thread_ts}) failed: "
                f"{self._describe_error(exc)}"
            ) from exc

        messages = [
            self._parse_message(raw, channel_id, channel_name)
            for raw in response.get("messages", [])
        ]
        logger.info(
            "get_thread_replies %s/%s returned %d message(s)",
            channel_id,
            thread_ts,
            len(messages),
        )
        return messages

    def search_messages(self, query: str, count: int = 20) -> list[SlackMessage]:
        """Search messages via ``search.messages`` (requires ``search:read``).

        Note: ``search.messages`` historically requires a user token; on
        workspaces where the bot token is not permitted Slack returns
        ``not_allowed_token_type`` which we surface as a clear error.
        """
        if not query:
            raise SlackClientError("search_messages requires a non-empty query")
        count = self._clamp(count, default=20, hi=100)
        try:
            response = self._client.search_messages(query=query, count=count)
        except SlackApiError as exc:
            raise SlackClientError(
                f"search_messages failed: {self._describe_error(exc)}"
            ) from exc

        matches = (response.get("messages") or {}).get("matches", [])
        messages: list[SlackMessage] = []
        for raw in matches:
            channel = raw.get("channel") or {}
            channel_id = channel.get("id", "")
            channel_name = channel.get("name", "") or self._resolve_channel_name(
                channel_id
            )
            messages.append(self._parse_message(raw, channel_id, channel_name))
        logger.info("search_messages query=%r returned %d match(es)", query, len(messages))
        return messages

    def send_message(self, channel_id: str, text: str, thread_ts: str = "") -> dict:
        """Send a message to a channel or DM via ``chat.postMessage``.

        Requires the ``chat:write`` scope on the bot token.
        """
        if not channel_id:
            raise SlackClientError("send_message requires a channel_id")
        if not text:
            raise SlackClientError("send_message requires non-empty text")
        kwargs: dict[str, Any] = {"channel": channel_id, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        try:
            response = self._client.chat_postMessage(**kwargs)
        except SlackApiError as exc:
            raise SlackClientError(
                f"send_message({channel_id}) failed: {self._describe_error(exc)}"
            ) from exc
        ts = response.get("ts", "")
        logger.info("send_message: channel=%s ts=%s", channel_id, ts)
        return {"channel_id": channel_id, "ts": ts, "text": text}

    def get_user_info(self, user_id: str) -> SlackUser:
        """Resolve a single user's identity via ``users.info`` (cached)."""
        if not user_id:
            raise SlackClientError("get_user_info requires a user_id")
        if user_id in self._user_cache:
            return self._user_cache[user_id]
        try:
            response = self._client.users_info(user=user_id)
        except SlackApiError as exc:
            raise SlackClientError(
                f"get_user_info({user_id}) failed: {self._describe_error(exc)}"
            ) from exc
        user = self._parse_user(response.get("user", {}))
        self._user_cache[user_id] = user
        return user

    # ------------------------------------------------------------------ #
    # Parsing helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _clamp(value: Any, default: int, hi: int, lo: int = 1) -> int:
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = default
        return max(lo, min(value, hi))

    def _resolve_channel_name(self, channel_id: str) -> str:
        """Best-effort channel name lookup (cached, never raises)."""
        if not channel_id:
            return ""
        if channel_id in self._channel_name_cache:
            return self._channel_name_cache[channel_id]
        try:
            response = self._client.conversations_info(channel=channel_id)
            name = (response.get("channel") or {}).get("name", "")
        except SlackApiError as exc:
            logger.debug("Could not resolve channel name for %s: %s", channel_id, exc)
            name = ""
        self._channel_name_cache[channel_id] = name
        return name

    def _resolve_user_name(self, user_id: str) -> str:
        """Best-effort user display-name lookup (cached, never raises)."""
        if not user_id:
            return ""
        try:
            return self.get_user_info(user_id).short_summary()
        except SlackClientError as exc:
            logger.debug("Could not resolve user name for %s: %s", user_id, exc)
            return ""

    def _parse_channel(self, raw: dict[str, Any]) -> SlackChannel:
        return SlackChannel(
            id=raw.get("id", ""),
            name=raw.get("name", ""),
            is_private=bool(raw.get("is_private", False)),
            topic=(raw.get("topic") or {}).get("value", ""),
            purpose=(raw.get("purpose") or {}).get("value", ""),
            member_count=int(raw.get("num_members", 0) or 0),
        )

    def _parse_user(self, raw: dict[str, Any]) -> SlackUser:
        profile = raw.get("profile") or {}
        return SlackUser(
            id=raw.get("id", ""),
            name=raw.get("name", ""),
            real_name=raw.get("real_name", "") or profile.get("real_name", ""),
            email=profile.get("email", ""),
            is_bot=bool(raw.get("is_bot", False)),
        )

    def _parse_message(
        self, raw: dict[str, Any], channel_id: str, channel_name: str
    ) -> SlackMessage:
        user_id = raw.get("user", "") or raw.get("bot_id", "")
        files = [self._parse_file(f) for f in raw.get("files", []) or []]
        ts = raw.get("ts", "")
        return SlackMessage(
            id=ts,
            channel_id=channel_id,
            channel_name=channel_name,
            user_id=user_id,
            user_name=self._resolve_user_name(user_id) if user_id else "",
            text=raw.get("text", ""),
            thread_ts=raw.get("thread_ts", ""),
            reply_count=int(raw.get("reply_count", 0) or 0),
            attachments=raw.get("attachments", []) or [],
            files=files,
            timestamp=self._parse_ts(ts),
        )

    @staticmethod
    def _parse_file(raw: dict[str, Any]) -> SlackFile:
        return SlackFile(
            id=raw.get("id", ""),
            name=raw.get("name", ""),
            title=raw.get("title", ""),
            mimetype=raw.get("mimetype", ""),
            size=int(raw.get("size", 0) or 0),
            url_private=raw.get("url_private", ""),
        )

    @staticmethod
    def _parse_ts(ts: str) -> Optional[datetime]:
        """Slack ts is a unix epoch string like '1697030400.001500'."""
        if not ts:
            return None
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _describe_error(exc: SlackApiError) -> str:
        """Pull the Slack 'error' code out of the response for clearer logs."""
        try:
            error = exc.response.get("error")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - defensive
            error = None
        return f"{error or exc}"
