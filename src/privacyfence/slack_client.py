"""Slack API client.

Uses a single Slack user token (``xoxp-...``) so the connector sees exactly
what the authenticated user sees — all channels, DMs, and private groups they
are a member of — without requiring a bot to be invited anywhere.

The token is obtained via Slack's OAuth v2 browser flow (see
``authorize_interactive`` below), driven from the PrivacyFence menu bar. The
Slack app itself (client id/secret) is organization-level config installed via
the "Install/Update Organization Config…" menu bar action — a user only ever
sees a browser consent screen, never a token to copy/paste.

Required user token scopes (see ``DEFAULT_USER_SCOPES``):
  - ``channels:read`` / ``groups:read`` / ``im:read`` / ``mpim:read``
  - ``channels:history`` / ``groups:history`` / ``im:history`` / ``mpim:history``
  - ``users:read`` / ``users:read.email``
  - ``search:read``
  - ``chat:write``
  - ``im:write`` / ``channels:write`` / ``groups:write`` / ``mpim:write`` (mark_unread /
    conversations.mark, and opening new conversations via ``open_conversation`` -- the needed
    scope depends on channel type: ``im:write`` for a 1:1 DM, ``mpim:write`` for a group DM)

``SlackClient`` optionally persists two whole-workspace directory snapshots to disk --
``user_cache_file`` (id -> name/email/is_bot) and ``channel_cache_file`` (id -> name/is_mpim, across
public/private channels, DMs, and group DMs). When given, ``get_user_info``/``resolve_channel_name``/
``resolve_is_group_dm`` check these first, refreshing them from Slack (``users.list``/
``conversations.list``) automatically about once a week, instead of falling back to a live per-id
call for every unique message author/channel a search or history fetch turns up. Both default to ""
(disabled) -- resolution then behaves exactly as it did before this existed. See
``refresh_user_directory``/``refresh_channel_directory`` for the explicit, immediate re-sync the
``slack_refresh_user_cache``/``slack_refresh_channel_cache`` bridge tools use.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .oauth_loopback import OAuthLoopbackError, run_browser_oauth

logger = logging.getLogger(__name__)

SLACK_OAUTH_PORT = 53682
SLACK_REDIRECT_PATH = "/callback"

# How long the on-disk whole-workspace directory snapshots (see
# refresh_user_directory()/refresh_channel_directory()) are trusted before
# get_user_info()/resolve_channel_name()/resolve_is_group_dm() transparently
# re-sync them. A week is plenty for an org's roster/channel list to stay
# useful; the slack_refresh_user_cache/slack_refresh_channel_cache bridge
# tools cover the mid-week exception (a new hire or new channel that isn't in
# last week's snapshot yet). Two separate constants (currently the same
# value) since a channel list plausibly churns faster than headcount and may
# warrant its own tuning later.
USER_DIRECTORY_CACHE_TTL = timedelta(days=7)
CHANNEL_DIRECTORY_CACHE_TTL = timedelta(days=7)

# Floor between automatic re-sync attempts once one has failed (e.g. a
# transient network error) -- without this, every cache-miss lookup while a
# directory is stale/unrefreshable would retry the full listing call from
# scratch, recreating the very per-message-call problem these caches exist
# to avoid.
_DIRECTORY_RETRY_COOLDOWN = timedelta(minutes=5)

# A message permalink's path is /archives/<channel id>/p<17-digit ts>, e.g.
# /archives/C0123ABCD/p1700000000123456 for ts "1700000000.123456".
_PERMALINK_PATH_RE = re.compile(r"^/archives/([A-Z0-9]+)/p(\d{7,})$")

DEFAULT_USER_SCOPES: list[str] = [
    "channels:read", "groups:read", "im:read", "mpim:read",
    "channels:history", "groups:history", "im:history", "mpim:history",
    "users:read", "users:read.email", "search:read", "chat:write",
    "im:write", "channels:write", "groups:write", "mpim:write",
]


class SlackClientError(Exception):
    """Raised for unrecoverable Slack client problems (auth, config, API)."""


def authorize_interactive(
    client_id: str,
    client_secret: str,
    token_file: str,
    user_scopes: list[str] | None = None,
    port: int = SLACK_OAUTH_PORT,
) -> dict[str, Any]:
    """Run Slack's OAuth v2 browser flow and persist the resulting user token.

    ``client_id``/``client_secret`` come from the organization config bundle
    (the Slack app IT registered). Returns the saved token record; raises
    ``SlackClientError`` on failure.
    """
    scopes = ",".join(user_scopes or DEFAULT_USER_SCOPES)

    def build_authorize_url(redirect_uri: str, state: str, code_challenge: str) -> str:
        params = {
            "client_id": client_id,
            "user_scope": scopes,
            "redirect_uri": redirect_uri,
            "state": state,
        }
        return "https://slack.com/oauth/v2/authorize?" + urlencode(params)

    def exchange(code: str, redirect_uri: str, code_verifier: str) -> dict[str, Any]:
        client = WebClient()
        try:
            response = client.oauth_v2_access(
                client_id=client_id,
                client_secret=client_secret,
                code=code,
                redirect_uri=redirect_uri,
            )
        except SlackApiError as exc:
            raise SlackClientError(
                f"Slack OAuth exchange failed: {SlackClient._describe_error(exc)}"
            ) from exc
        if not response.get("ok", False):
            raise SlackClientError(f"Slack OAuth exchange failed: {response.get('error')}")
        return response.data

    try:
        response = run_browser_oauth(
            build_authorize_url, exchange, port=port, path=SLACK_REDIRECT_PATH
        )
    except OAuthLoopbackError as exc:
        raise SlackClientError(f"Slack sign-in failed: {exc}") from exc

    authed_user = response.get("authed_user") or {}
    access_token = authed_user.get("access_token", "")
    if not access_token:
        raise SlackClientError(f"Slack OAuth did not return a user access token: {response}")

    token_record = {
        "access_token": access_token,
        "user_id": authed_user.get("id", ""),
        "team_id": (response.get("team") or {}).get("id", ""),
        "team_name": (response.get("team") or {}).get("name", ""),
        "email": _fetch_account_email(access_token, authed_user.get("id", "")),
    }
    os.makedirs(os.path.dirname(os.path.abspath(token_file)), exist_ok=True)
    with open(token_file, "w", encoding="utf-8") as fh:
        json.dump(token_record, fh)
    try:
        os.chmod(token_file, 0o600)
    except OSError:  # pragma: no cover - best effort on non-POSIX
        logger.debug("Could not chmod Slack token file (non-fatal)")
    logger.info("Slack OAuth complete for team %r", token_record["team_name"])
    return token_record


def _fetch_account_email(access_token: str, user_id: str) -> str:
    """Best-effort lookup of the signed-in user's email (for auto-accept rules)."""
    if not user_id:
        return ""
    try:
        response = WebClient(token=access_token).users_info(user=user_id)
        return (response.get("user") or {}).get("profile", {}).get("email", "")
    except SlackApiError as exc:
        logger.debug("Could not resolve Slack account email (non-fatal): %s", SlackClient._describe_error(exc))
        return ""


def load_token_file(token_file: str) -> dict[str, Any]:
    """Load a previously saved Slack token record, or raise SlackClientError."""
    if not os.path.exists(token_file):
        raise SlackClientError(
            f"No Slack token found at '{token_file}'. Use Authenticate… in the "
            "PrivacyFence menu bar to sign in."
        )
    with open(token_file, encoding="utf-8") as fh:
        return json.load(fh)


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
    timestamp: datetime | None = None

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
class SlackDirectMessage:
    """A normalized 1:1 Slack DM (Slack's "im" conversation type)."""

    id: str
    user_id: str
    user_name: str = ""

    def short_summary(self) -> str:
        return f"DM with {self.user_name or self.user_id}"


@dataclass
class SlackGroupChat:
    """A normalized Slack group DM (Slack's "mpim" conversation type -- a
    private multi-person conversation, distinct from a 1:1 DM and from a
    private channel). ``conversations.list`` doesn't return members for
    this type, so ``member_ids``/``member_names`` come from a separate
    ``conversations.members`` call per chat (see ``list_group_chats``)."""

    id: str
    name: str
    member_ids: list[str] = field(default_factory=list)
    member_names: list[str] = field(default_factory=list)

    def short_summary(self) -> str:
        return f"Group DM with {', '.join(self.member_names or self.member_ids)}"


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
    """Slack client backed by a single user token (xoxp-).

    Using a user token means the connector sees exactly what the authenticated
    user sees, with no bot to invite and no visibility beyond their own access.
    """

    def __init__(
        self, user_token: str, user_cache_file: str = "", channel_cache_file: str = ""
    ) -> None:
        if not user_token:
            raise SlackClientError(
                "No Slack user token available. Use Authenticate… in the "
                "PrivacyFence menu bar to sign in."
            )
        self._client = WebClient(token=user_token)
        # Small cache so repeated messages from the same author/channel don't
        # trigger a fresh API call each time within a single fetch. Also
        # doubles as the in-memory home for the whole-workspace directory
        # snapshots below -- a directory hit and a one-off live lookup are
        # indistinguishable to callers, both just end up in these same dicts.
        self._user_cache: dict[str, SlackUser] = {}
        self._channel_name_cache: dict[str, str] = {}
        self._channel_is_mpim_cache: dict[str, bool] = {}

        # Weekly on-disk snapshots (see _ensure_user_directory/
        # _ensure_channel_directory and refresh_user_directory/
        # refresh_channel_directory) -- turn the per-message users.info/
        # conversations.info calls a search/history/thread fetch would
        # otherwise make into zero, for anyone/anything already known as of
        # the last refresh. Empty string (the default for either) opts that
        # cache out entirely -- resolution behaves exactly as before this
        # existed, one live call per uncached id, nothing persisted to disk.
        self._user_cache_file = user_cache_file
        self._user_directory_loaded_from_disk = False
        self._user_directory_fetched_at: datetime | None = None
        self._user_directory_last_attempt: datetime | None = None

        self._channel_cache_file = channel_cache_file
        self._channel_directory_loaded_from_disk = False
        self._channel_directory_fetched_at: datetime | None = None
        self._channel_directory_last_attempt: datetime | None = None

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
        self, exclude_archived: bool = True, max_results: int = 100, participant: str = ""
    ) -> list[SlackChannel]:
        """List channels visible to the bot.

        Uses ``conversations.list`` with public and private channel types. When
        ``participant`` is given (a user id, handle, or display name, comma-
        separated to require all of them), each returned channel's membership is
        checked via ``conversations.members`` -- one extra Slack API call per
        channel, same per-item cost ``list_group_chats`` accepts for its own
        participant filter (see that method's docstring), except a channel's
        member list is typically far larger, so ``_channel_matches_participant``
        checks ids first (cheap) and only resolves display names -- one call per
        unresolved member -- when an id-only match doesn't already decide it.
        """
        max_results = self._clamp(max_results, default=100, hi=1000)
        channels: list[SlackChannel] = []
        cursor: str | None = None
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

        if participant:
            channels = [
                c for c in channels if self._channel_matches_participant(c.id, participant)
            ]

        logger.info("list_channels returned %d channel(s)", len(channels))
        return channels

    def list_dms(
        self, max_results: int = 100, participant: str = ""
    ) -> list[SlackDirectMessage]:
        """List 1:1 direct messages visible to the user via
        ``conversations.list(types="im")``.

        Each ``im`` conversation exposes a single ``user`` field (the other
        party), so unlike group chats no extra per-conversation call is
        needed to know who's on the other end -- filtering by ``participant``
        (a user id, handle, or display name, case-insensitive) is a plain
        client-side match against that one field.
        """
        max_results = self._clamp(max_results, default=100, hi=1000)
        dms: list[SlackDirectMessage] = []
        cursor: str | None = None
        try:
            while len(dms) < max_results:
                page_size = min(200, max_results - len(dms))
                response = self._client.conversations_list(
                    types="im",
                    limit=page_size,
                    cursor=cursor,
                )
                for raw in response.get("channels", []):
                    dms.append(self._parse_dm(raw))
                cursor = (response.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
        except SlackApiError as exc:
            raise SlackClientError(
                f"list_dms failed: {self._describe_error(exc)}"
            ) from exc

        if participant:
            dms = [
                d for d in dms
                if self._matches_participant(participant, [d.user_id], [d.user_name])
            ]

        dms = dms[:max_results]
        logger.info("list_dms returned %d DM(s)", len(dms))
        return dms

    def list_group_chats(
        self, max_results: int = 100, participant: str = ""
    ) -> list[SlackGroupChat]:
        """List group DMs ("mpim") visible to the user via
        ``conversations.list(types="mpim")``.

        Unlike channels/DMs, the list response carries no member info for
        group DMs, so resolving (and hence filtering by) ``participant``
        costs one extra ``conversations.members`` call per group chat --
        O(n) Slack API calls where n is the number of group chats returned,
        not O(1) like ``list_channels``/``list_dms``. ``participant`` may be
        comma-separated to require all of them as members of the same group
        chat (AND) -- the same multi-name matching ``search_messages`` uses
        to resolve e.g. "Bob and Jane's group chat" to one conversation.
        """
        max_results = self._clamp(max_results, default=100, hi=1000)
        raw_chats: list[dict[str, Any]] = []
        cursor: str | None = None
        try:
            while len(raw_chats) < max_results:
                page_size = min(200, max_results - len(raw_chats))
                response = self._client.conversations_list(
                    types="mpim",
                    limit=page_size,
                    cursor=cursor,
                )
                raw_chats.extend(response.get("channels", []))
                cursor = (response.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
        except SlackApiError as exc:
            raise SlackClientError(
                f"list_group_chats failed: {self._describe_error(exc)}"
            ) from exc

        raw_chats = raw_chats[:max_results]
        chats = [self._parse_group_chat(raw) for raw in raw_chats]

        if participant:
            needles = [p.strip() for p in participant.split(",") if p.strip()]
            chats = [
                c for c in chats
                if all(self._matches_participant(n, c.member_ids, c.member_names) for n in needles)
            ]

        logger.info("list_group_chats returned %d group chat(s)", len(chats))
        return chats

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
        channel_name = self.resolve_channel_name(channel_id)

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
        channel_name = self.resolve_channel_name(channel_id)
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

    def search_messages(
        self, query: str = "", count: int = 20, participant: str = "", days: int = 90
    ) -> list[SlackMessage]:
        """Search messages via ``search.messages`` (requires ``search:read``), or,
        when ``participant`` is given, read the matching DM/group-DM
        conversation(s) directly instead -- Slack's own `from:`/`in:` search
        modifiers need the right handle syntax to work at all and its search
        index doesn't reliably surface every message, whereas participant
        resolution here reuses the same list_dms/list_group_chats matching
        (id, handle, or display name) already exposed on those tools, so
        "messages from Bob" or "messages with Bob and Jane" resolves
        deterministically to the right conversation(s). ``query``, if also
        given alongside ``participant``, is applied as a client-side
        case-insensitive substring filter over those conversations' text
        instead of Slack's full-text index.

        ``days`` bounds both paths to the last N days (default 90, ~3
        months) -- on a workspace with years of history, neither Slack's
        search index nor a channel's most-recent-``count`` messages are
        otherwise biased toward *relevant* results, just whatever last
        matched, however old. Pass ``days=0`` for no cutoff.
        """
        if not query and not participant:
            raise SlackClientError("search_messages requires a query, a participant, or both")
        count = self._clamp(count, default=20, hi=100)
        days = self._clamp(days, default=90, hi=3650, lo=0)
        if participant:
            return self._search_by_participant(participant, query, count, days)

        search_query = query
        if days > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
            search_query = f"{query} after:{cutoff}".strip()

        try:
            response = self._client.search_messages(query=search_query, count=count)
        except SlackApiError as exc:
            raise SlackClientError(
                f"search_messages failed: {self._describe_error(exc)}"
            ) from exc

        matches = (response.get("messages") or {}).get("matches", [])
        messages: list[SlackMessage] = []
        for raw in matches:
            channel = raw.get("channel") or {}
            channel_id = channel.get("id", "")
            channel_name = channel.get("name", "") or self.resolve_channel_name(
                channel_id
            )
            messages.append(self._parse_message(raw, channel_id, channel_name))
        logger.info("search_messages query=%r returned %d match(es)", query, len(messages))
        return messages

    def _search_by_participant(
        self, participant: str, query: str, count: int, days: int = 90
    ) -> list[SlackMessage]:
        """Most-recent-first messages from the DM/group-DM conversation(s)
        matching ``participant``, optionally narrowed to those whose text
        contains ``query`` (case-insensitive substring). ``participant`` may
        be comma-separated to require all of them as members of the same
        group chat -- a 1:1 DM only ever matches a single, unsegmented
        participant since it has exactly one other party. ``days`` (0 = no
        cutoff) bounds each conversation's history fetch via ``oldest``, same
        reasoning as ``search_messages``'s own ``days`` param.
        """
        needles = [p.strip() for p in participant.split(",") if p.strip()]
        if not needles:
            return []

        channel_ids: list[str] = []
        if len(needles) == 1:
            channel_ids += [d.id for d in self.list_dms(max_results=1000, participant=needles[0])]
        channel_ids += [
            c.id for c in self.list_group_chats(max_results=1000, participant=participant)
        ]

        oldest = None
        if days > 0:
            oldest = str((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())

        messages: list[SlackMessage] = []
        for channel_id in channel_ids:
            messages.extend(self.get_channel_history(channel_id, limit=count, oldest=oldest))

        if query:
            needle = query.lower()
            messages = [m for m in messages if needle in (m.text or "").lower()]

        messages.sort(
            key=lambda m: m.timestamp or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        logger.info(
            "search_messages participant=%r query=%r matched %d conversation(s), %d message(s)",
            participant, query, len(channel_ids), len(messages),
        )
        return messages[:count]

    def send_message(self, channel_id: str, text: str, thread_ts: str = "") -> dict:
        """Send a message to a channel or DM via ``chat.postMessage``.

        Requires the ``chat:write`` scope on the user token.
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
        # response["channel"] is the resolved channel ID (D... for DMs even when a
        # user ID was passed as channel_id — needed for conversations.mark).
        resolved_channel = response.get("channel", channel_id)
        logger.info("send_message: channel=%s ts=%s", resolved_channel, ts)
        return {"channel_id": resolved_channel, "ts": ts, "text": text}

    def open_conversation(self, user_ids: list[str]) -> SlackGroupChat:
        """Create (or reopen the existing) conversation with `user_ids` via
        ``conversations.open``. A single user id opens a 1:1 DM; two or more
        open a group DM ("mpim") -- Slack itself decides which based on how
        many ids are given, same as ``conversations.open``'s own behavior.

        Unlike ``list_group_chats``, this is the only way to reach a group DM
        that doesn't already exist yet (there's nothing to list until it's
        been opened at least once). Requires the ``im:write`` (1:1) or
        ``mpim:write`` (group) scope on the user token.
        """
        if not user_ids:
            raise SlackClientError("open_conversation requires at least one user_id")
        try:
            response = self._client.conversations_open(users=",".join(user_ids))
        except SlackApiError as exc:
            raise SlackClientError(
                f"open_conversation({user_ids}) failed: {self._describe_error(exc)}"
            ) from exc
        channel = response.get("channel") or {}
        channel_id = channel.get("id", "")
        name = channel.get("name", "")
        if name:
            self._channel_name_cache[channel_id] = name
        logger.info("open_conversation users=%s -> channel=%s", user_ids, channel_id)
        return SlackGroupChat(
            id=channel_id,
            name=name or self.resolve_channel_name(channel_id),
            member_ids=list(user_ids),
            member_names=[self._resolve_user_name(uid) for uid in user_ids],
        )

    def resolve_permalink(self, url: str) -> dict[str, str]:
        """Parse a Slack message permalink (from a message's "Copy link") into
        the channel id and timestamp ``get_channel_history``/``get_thread_replies``
        need. A permalink already encodes both in its path
        (``/archives/<channel id>/p<ts digits>``), so this needs no Slack API
        call for them -- only the returned ``channel_name`` is looked up
        (best-effort, cached, same as ``resolve_channel_name``). A permalink to
        a threaded reply also carries the thread root's timestamp as a
        ``thread_ts`` query parameter; a permalink to a top-level message (or
        a thread's own root) has none, so ``thread_ts`` comes back empty.
        """
        if not url:
            raise SlackClientError("resolve_permalink requires a url")
        parsed = urlparse(url.strip())
        match = _PERMALINK_PATH_RE.match(parsed.path)
        if not match:
            raise SlackClientError(f"Not a recognizable Slack message permalink: {url!r}")
        channel_id, ts_digits = match.groups()
        ts = f"{ts_digits[:-6]}.{ts_digits[-6:]}"
        thread_ts = (parse_qs(parsed.query).get("thread_ts") or [""])[0]
        return {
            "channel_id": channel_id,
            "channel_name": self.resolve_channel_name(channel_id),
            "ts": ts,
            "thread_ts": thread_ts,
        }

    def resolve_user_name(self, user_id: str) -> str:
        """Best-effort display-name lookup for `user_id` (cached, never
        raises) -- public wrapper around `_resolve_user_name` for callers
        outside this module, same as `resolve_channel_name`'s own role for
        channel names."""
        return self._resolve_user_name(user_id)

    def mark_channel_unread_before(self, channel_id: str, ts: str) -> None:
        """Set the channel's read cursor to just before ``ts``.

        Any message with a timestamp >= ``ts`` will appear as unread.
        Uses conversations.mark. Required scope depends on channel type:
        ``im:write`` for DMs, ``channels:write`` for public channels,
        ``groups:write`` for private channels, ``mpim:write`` for group DMs.
        """
        if not channel_id or not ts:
            raise SlackClientError(
                "mark_channel_unread_before requires channel_id and ts"
            )
        try:
            mark_ts = f"{float(ts) - 0.000001:.6f}"
            self._client.conversations_mark(channel=channel_id, ts=mark_ts)
        except SlackApiError as exc:
            raise SlackClientError(
                f"mark_channel_unread_before({channel_id}) failed: "
                f"{self._describe_error(exc)}"
            ) from exc
        logger.info("mark_channel_unread_before: channel=%s before=%s", channel_id, ts)

    def get_user_info(self, user_id: str) -> SlackUser:
        """Resolve a single user's identity (cached).

        When a ``user_cache_file`` was given at construction, checks the
        weekly whole-workspace directory first (see
        ``_ensure_user_directory``/``refresh_user_directory``) before
        falling back to a live ``users.info`` call -- the fallback still
        runs, and its result is cached in memory, for anyone missing from
        the directory (a bot, or someone who joined since the last refresh),
        same as before this cache existed.
        """
        if not user_id:
            raise SlackClientError("get_user_info requires a user_id")
        if self._user_cache_file:
            self._ensure_user_directory()
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
    # Directory caches (whole-workspace user/channel snapshots)
    # ------------------------------------------------------------------ #

    def ensure_directories_fresh(self) -> None:
        """Eagerly run the same freshness check ``get_user_info``/
        ``resolve_channel_name``/``resolve_is_group_dm`` would otherwise only
        run lazily on first use. Meant to be called once right after
        connecting (see ``daemon_main.py``) so a snapshot that's gone stale
        while the app was closed (more than a week between restarts) gets
        refreshed during startup instead of blocking whatever Slack tool
        call happens to run first. A no-op (no network call at all) when
        both snapshots are already fresh. Never raises -- same best-effort
        semantics as the lazy path it shares its implementation with.
        """
        if self._user_cache_file:
            self._ensure_user_directory()
        if self._channel_cache_file:
            self._ensure_channel_directory()

    def refresh_user_directory(self) -> int:
        """Force an immediate re-sync of the whole Slack user directory via
        ``users.list`` (paginated), replacing the current snapshot and
        resetting its weekly TTL. Raises on failure -- unlike the lazy,
        best-effort refresh ``_ensure_user_directory`` runs automatically,
        this is the explicit action a caller takes (the
        ``slack_refresh_user_cache`` bridge tool) when a teammate who joined
        mid-week needs to resolve correctly right now, rather than waiting
        for next week's automatic refresh or the one-off ``users.info``
        fallback ``get_user_info`` already does for any id missing from the
        directory. Returns the number of users cached.
        """
        users: dict[str, SlackUser] = {}
        cursor: str | None = None
        try:
            while True:
                response = self._client.users_list(cursor=cursor, limit=200)
                for raw in response.get("members", []):
                    user = self._parse_user(raw)
                    if user.id:
                        users[user.id] = user
                cursor = (response.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
        except SlackApiError as exc:
            raise SlackClientError(
                f"refresh_user_directory failed: {self._describe_error(exc)}"
            ) from exc
        self._user_cache = users
        self._user_directory_loaded_from_disk = True
        self._user_directory_fetched_at = datetime.now(timezone.utc)
        self._save_user_directory_to_disk()
        logger.info("Slack user directory refreshed: %d user(s) cached", len(users))
        return len(users)

    def refresh_channel_directory(self) -> int:
        """Force an immediate re-sync of every conversation the token can
        see (public/private channels, DMs, group DMs) via
        ``conversations.list`` (paginated, across all four types),
        replacing the current name/group-DM-flag snapshot and resetting its
        weekly TTL. Raises on failure, same reasoning as
        ``refresh_user_directory``. DM ("im") entries contribute no name
        (that conversation type doesn't have one -- ``resolve_channel_name``
        already returns "" for them) but are fetched anyway so
        ``resolve_is_group_dm`` is covered for every conversation type, not
        just channels. Returns the number of conversations cached.
        """
        names: dict[str, str] = {}
        is_mpim: dict[str, bool] = {}
        cursor: str | None = None
        try:
            while True:
                response = self._client.conversations_list(
                    types="public_channel,private_channel,mpim,im",
                    exclude_archived=False,
                    limit=200,
                    cursor=cursor,
                )
                for raw in response.get("channels", []):
                    channel_id = raw.get("id", "")
                    if not channel_id:
                        continue
                    names[channel_id] = raw.get("name", "")
                    is_mpim[channel_id] = bool(raw.get("is_mpim", False))
                cursor = (response.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
        except SlackApiError as exc:
            raise SlackClientError(
                f"refresh_channel_directory failed: {self._describe_error(exc)}"
            ) from exc
        self._channel_name_cache = names
        self._channel_is_mpim_cache = is_mpim
        self._channel_directory_loaded_from_disk = True
        self._channel_directory_fetched_at = datetime.now(timezone.utc)
        self._save_channel_directory_to_disk()
        logger.info("Slack channel directory refreshed: %d conversation(s) cached", len(names))
        return len(names)

    def _ensure_user_directory(self) -> None:
        """Best-effort: loads the on-disk weekly snapshot (once per process)
        and transparently re-syncs it from Slack if it's missing or older
        than a week. Failures are logged and swallowed, subject to a retry
        cooldown -- a directory miss just means ``get_user_info`` falls back
        to its existing per-id ``users.info`` call, same as if this cache
        didn't exist."""
        if not self._user_directory_loaded_from_disk:
            self._load_user_directory_from_disk()
            self._user_directory_loaded_from_disk = True
        now = datetime.now(timezone.utc)
        if self._user_directory_fetched_at and now - self._user_directory_fetched_at < USER_DIRECTORY_CACHE_TTL:
            return
        if self._user_directory_last_attempt and now - self._user_directory_last_attempt < _DIRECTORY_RETRY_COOLDOWN:
            return
        self._user_directory_last_attempt = now
        try:
            self.refresh_user_directory()
        except SlackClientError as exc:
            logger.warning("Could not refresh Slack user directory (non-fatal): %s", exc)

    def _ensure_channel_directory(self) -> None:
        """Channel-directory counterpart to ``_ensure_user_directory`` -- see
        that method's docstring for the load-once/refresh-if-stale/cooldown
        reasoning, identical here."""
        if not self._channel_directory_loaded_from_disk:
            self._load_channel_directory_from_disk()
            self._channel_directory_loaded_from_disk = True
        now = datetime.now(timezone.utc)
        if self._channel_directory_fetched_at and now - self._channel_directory_fetched_at < CHANNEL_DIRECTORY_CACHE_TTL:
            return
        if self._channel_directory_last_attempt and now - self._channel_directory_last_attempt < _DIRECTORY_RETRY_COOLDOWN:
            return
        self._channel_directory_last_attempt = now
        try:
            self.refresh_channel_directory()
        except SlackClientError as exc:
            logger.warning("Could not refresh Slack channel directory (non-fatal): %s", exc)

    def _load_user_directory_from_disk(self) -> None:
        if not self._user_cache_file or not os.path.exists(self._user_cache_file):
            return
        try:
            with open(self._user_cache_file, encoding="utf-8") as fh:
                data = json.load(fh)
            fetched_at = datetime.fromisoformat(data.get("fetched_at", ""))
            raw_users = data.get("users") or {}
        except Exception as exc:
            logger.warning("Could not load Slack user directory cache (non-fatal): %s", exc)
            return
        loaded = 0
        for user_id, raw in raw_users.items():
            try:
                # setdefault: never clobber a fresher live users.info result
                # already resolved this session before the disk load ran.
                self._user_cache.setdefault(user_id, SlackUser(**raw))
                loaded += 1
            except TypeError:
                continue
        self._user_directory_fetched_at = fetched_at
        logger.debug("Loaded %d cached Slack user(s) from disk", loaded)

    def _save_user_directory_to_disk(self) -> None:
        if not self._user_cache_file or self._user_directory_fetched_at is None:
            return
        payload = {
            "fetched_at": self._user_directory_fetched_at.isoformat(),
            "users": {
                uid: {
                    "id": u.id, "name": u.name, "real_name": u.real_name,
                    "email": u.email, "is_bot": u.is_bot,
                }
                for uid, u in self._user_cache.items()
            },
        }
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._user_cache_file)), exist_ok=True)
            with open(self._user_cache_file, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.chmod(self._user_cache_file, 0o600)  # holds every workspace member's email
        except OSError as exc:
            logger.warning("Could not save Slack user directory cache (non-fatal): %s", exc)

    def _load_channel_directory_from_disk(self) -> None:
        if not self._channel_cache_file or not os.path.exists(self._channel_cache_file):
            return
        try:
            with open(self._channel_cache_file, encoding="utf-8") as fh:
                data = json.load(fh)
            fetched_at = datetime.fromisoformat(data.get("fetched_at", ""))
            raw_channels = data.get("channels") or {}
        except Exception as exc:
            logger.warning("Could not load Slack channel directory cache (non-fatal): %s", exc)
            return
        for channel_id, raw in raw_channels.items():
            if not isinstance(raw, dict):
                continue
            self._channel_name_cache.setdefault(channel_id, raw.get("name", ""))
            self._channel_is_mpim_cache.setdefault(channel_id, bool(raw.get("is_mpim", False)))
        self._channel_directory_fetched_at = fetched_at
        logger.debug("Loaded %d cached Slack channel(s) from disk", len(raw_channels))

    def _save_channel_directory_to_disk(self) -> None:
        if not self._channel_cache_file or self._channel_directory_fetched_at is None:
            return
        channel_ids = set(self._channel_name_cache) | set(self._channel_is_mpim_cache)
        payload = {
            "fetched_at": self._channel_directory_fetched_at.isoformat(),
            "channels": {
                cid: {
                    "name": self._channel_name_cache.get(cid, ""),
                    "is_mpim": self._channel_is_mpim_cache.get(cid, False),
                }
                for cid in channel_ids
            },
        }
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._channel_cache_file)), exist_ok=True)
            with open(self._channel_cache_file, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
        except OSError as exc:
            logger.warning("Could not save Slack channel directory cache (non-fatal): %s", exc)

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

    def resolve_channel_name(self, channel_id: str) -> str:
        """Best-effort channel name lookup (cached, never raises)."""
        if not channel_id:
            return ""
        if self._channel_cache_file:
            self._ensure_channel_directory()
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

    def resolve_is_group_dm(self, channel_id: str) -> bool:
        """Whether `channel_id` is a group DM (Slack's "mpim" conversation
        type -- a private multi-person conversation, distinct from a 1:1 DM
        (`im`) and from a private *channel*, both of which can also surface
        as `G`-prefixed IDs, so the id alone doesn't tell them apart).
        Best-effort (cached, never raises) -- an unresolvable channel reads
        as not-a-group-DM rather than blocking the caller on a lookup that
        can't succeed.
        """
        if not channel_id:
            return False
        if self._channel_cache_file:
            self._ensure_channel_directory()
        if channel_id in self._channel_is_mpim_cache:
            return self._channel_is_mpim_cache[channel_id]
        try:
            response = self._client.conversations_info(channel=channel_id)
            is_mpim = bool((response.get("channel") or {}).get("is_mpim", False))
        except SlackApiError as exc:
            logger.debug("Could not resolve channel type for %s: %s", channel_id, exc)
            is_mpim = False
        self._channel_is_mpim_cache[channel_id] = is_mpim
        return is_mpim

    def _resolve_user_name(self, user_id: str) -> str:
        """Best-effort user display-name lookup (cached, never raises)."""
        if not user_id:
            return ""
        try:
            return self.get_user_info(user_id).short_summary()
        except SlackClientError as exc:
            logger.debug("Could not resolve user name for %s: %s", user_id, exc)
            return ""

    def _resolve_members(self, channel_id: str) -> list[str]:
        """Fetch a conversation's member user ids via ``conversations.members``
        (best-effort, never raises -- an unresolvable channel reads as having
        no members rather than blocking the whole listing)."""
        try:
            response = self._client.conversations_members(channel=channel_id, limit=1000)
            return list(response.get("members", []) or [])
        except SlackApiError as exc:
            logger.debug("Could not resolve members for %s: %s", channel_id, exc)
            return []

    @staticmethod
    def _matches_participant(participant: str, ids: list[str], names: list[str]) -> bool:
        """Case-insensitive match of ``participant`` against a conversation's
        member ids (exact) and resolved display names (substring) -- lets
        callers filter by Slack user id, handle, or real name without
        needing the exact id."""
        needle = participant.strip().lower()
        if not needle:
            return True
        if any(needle == i.lower() for i in ids if i):
            return True
        return any(needle in n.lower() for n in names if n)

    def _channel_matches_participant(self, channel_id: str, participant: str) -> bool:
        """Whether ``channel_id``'s membership satisfies every comma-separated
        name in ``participant`` (AND), same semantics as ``list_group_chats``.
        Member ids are resolved and checked first; display names -- one
        ``users.info`` call per unresolved member -- are only resolved if an
        id-only match doesn't already decide every needle, since a channel's
        membership can be far larger than a group chat's."""
        needles = [p.strip() for p in participant.split(",") if p.strip()]
        if not needles:
            return True
        member_ids = self._resolve_members(channel_id)
        if all(self._matches_participant(n, member_ids, []) for n in needles):
            return True
        member_names = [self._resolve_user_name(uid) for uid in member_ids]
        return all(self._matches_participant(n, member_ids, member_names) for n in needles)

    def _parse_dm(self, raw: dict[str, Any]) -> SlackDirectMessage:
        user_id = raw.get("user", "")
        return SlackDirectMessage(
            id=raw.get("id", ""),
            user_id=user_id,
            user_name=self._resolve_user_name(user_id) if user_id else "",
        )

    def _parse_group_chat(self, raw: dict[str, Any]) -> SlackGroupChat:
        channel_id = raw.get("id", "")
        member_ids = self._resolve_members(channel_id)
        return SlackGroupChat(
            id=channel_id,
            name=raw.get("name", ""),
            member_ids=member_ids,
            member_names=[self._resolve_user_name(uid) for uid in member_ids],
        )

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
    def _parse_ts(ts: str) -> datetime | None:
        """Slack ts is a unix epoch string like '1697030400.001500'."""
        if not ts:
            return None
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _describe_error(exc: SlackApiError) -> str:
        """Pull the Slack 'error' code and needed scope out of the response."""
        try:
            resp = exc.response  # type: ignore[union-attr]
            error = resp.get("error")
            needed = resp.get("needed")
        except Exception:  # noqa: BLE001 - defensive
            error = needed = None
        if error and needed:
            return f"{error} (needed scope: {needed})"
        return f"{error or exc}"
