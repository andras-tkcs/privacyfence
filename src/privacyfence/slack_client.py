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
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

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

# How many times a single API call retries a 429 ("ratelimited") response
# before giving up, waiting the Retry-After header tells it to each time
# (slack_sdk's RateLimitErrorRetryHandler, attached below). The directory
# refreshes are the calls most likely to hit this: conversations.list/
# users.list are paginated, so a large workspace makes many calls back to
# back and can trip Slack's rate limit mid-refresh even though each
# individual call is well-formed.
_RATE_LIMIT_MAX_RETRY_COUNT = 3

# How long an *unresolvable* user id is remembered as unresolvable. Without
# this, every id missing from the directory -- a bot, a deactivated account,
# a Slack Connect external user -- costs a fresh, failing users.info call on
# every message it appears in, on every fetch, forever: the exact
# per-message-call problem the positive cache exists to avoid, just on the
# error path where nothing was being written back at all.
_NEGATIVE_LOOKUP_TTL = timedelta(hours=1)

# How long a conversation's member list is reused. conversations.list carries
# no members for channels or group DMs, so the only source is one
# conversations.members call per conversation; a listing that shows members
# for 40 group chats is 40 calls every single time without this. Deliberately
# short -- membership is displayed, never used to decide a participant filter
# (that goes through users.conversations, which is always live).
_MEMBERSHIP_CACHE_TTL = timedelta(minutes=10)

# Upper bound on Slack calls in flight at once across the whole process (see
# _map_concurrent). The synchronous WebClient opens a fresh connection per
# call, so overlapping them is what hides the per-call TLS handshake; kept
# low because Slack's rate limits are per method per workspace, and a wider
# pool just converts round-trip time into 429s.
_FANOUT_CONCURRENCY = 5

# Ceiling on how many conversations _search_by_participant reads history
# from. participant resolution (an id, or an exact/unambiguous directory
# match) already narrows this correctly in the common case; this is the
# backstop for the rarer case where it resolves loosely -- see that
# method's own docstring.
_SEARCH_BY_PARTICIPANT_CONVERSATION_CAP = 10

# Defensive cap on how many pages _conversation_ids_for_user walks. No real
# Slack account is a member of tens of thousands of conversations, so this
# never binds in practice -- it exists so a malformed or misbehaving
# response (an always-truthy cursor) degrades to "stop and use what was
# fetched" instead of looping forever.
_USERS_CONVERSATIONS_PAGE_BUDGET = 100

# Same defensive role as _USERS_CONVERSATIONS_PAGE_BUDGET, for
# _resolve_members' own conversations.members pagination -- 1000 members per
# page, so this covers up to 1,000,000 members before it would ever bind.
_CONVERSATIONS_MEMBERS_PAGE_BUDGET = 1000

# A message permalink's path is /archives/<channel id>/p<17-digit ts>, e.g.
# /archives/C0123ABCD/p1700000000123456 for ts "1700000000.123456".
_PERMALINK_PATH_RE = re.compile(r"^/archives/([A-Z0-9]+)/p(\d{7,})$")

# Slack ids are prefixed by object type. U/W are human users (W in Enterprise
# Grid), B is a bot -- and a B id is never returned by users.list and never
# resolvable via users.info, so it is worth recognizing rather than looking
# up and failing.
_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]+$")
_BOT_ID_RE = re.compile(r"^B[A-Z0-9]+$")

DEFAULT_USER_SCOPES: list[str] = [
    "channels:read", "groups:read", "im:read", "mpim:read",
    "channels:history", "groups:history", "im:history", "mpim:history",
    "users:read", "users:read.email", "search:read", "chat:write",
    "im:write", "channels:write", "groups:write", "mpim:write",
]


class SlackClientError(Exception):
    """Raised for unrecoverable Slack client problems (auth, config, API)."""


class SlackDirectoryUnavailable(SlackClientError):
    """Raised when a participant name can't be resolved because the local user
    directory doesn't know it. Carries an actionable message (refresh the
    cache) rather than falling back to a whole-workspace scan that would cost
    hundreds of API calls and, with no directory to resolve names against,
    still match nothing."""





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


def _map_concurrent(items: list[Any], fn, *, max_workers: int = _FANOUT_CONCURRENCY) -> list[Any]:
    """``[fn(i) for i in items]``, but run up to ``max_workers`` at a time on a
    throwaway pool. Exists because ``SlackClient`` is synchronous (blocking
    HTTP via slack_sdk) yet several call sites need one Slack API call per
    item -- ``conversations.members`` per group chat, ``conversations.history``
    per matched conversation. The connector already runs the whole client
    call on its own thread (``SlackConnector._fetch``'s
    ``asyncio.to_thread``), so nesting a small pool in here overlaps the
    otherwise-serial round trips (and the TLS handshake each one pays --
    slack_sdk's sync ``WebClient`` opens a fresh connection per call, no
    pooling) without touching the event loop. Kept low and created per call,
    not shared module-wide: Slack's rate limits are per method per
    workspace, so a wider or longer-lived pool just converts round-trip time
    into 429s. Preserves ``items``' order; a single item skips the pool
    entirely.
    """
    if len(items) <= 1:
        return [fn(i) for i in items]
    with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as pool:
        return list(pool.map(fn, items))


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
        # WebClient's own default retry handlers cover connection errors only
        # -- a 429 would otherwise surface immediately as a SlackClientError
        # (see refresh_channel_directory/refresh_user_directory's paginated
        # conversations.list/users.list calls) instead of waiting out the
        # Retry-After window Slack asks for.
        self._client.retry_handlers.append(
            RateLimitErrorRetryHandler(max_retry_count=_RATE_LIMIT_MAX_RETRY_COUNT)
        )
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
        # Slack-side pagination cursor for an in-progress bounded refresh --
        # see refresh_channel_directory()'s max_pages parameter. None means
        # "nothing in progress" (either never started, or the last walk
        # finished cleanly).
        self._channel_refresh_cursor: str | None = None

        # Ids resolved to "no such user" (a bot, a deactivated account, a
        # Slack Connect external user, ...) -- see get_user_info(). Without
        # this, an id absent from the directory costs a fresh, failing
        # users.info call every time it's looked up, forever.
        self._user_negative_cache: dict[str, datetime] = {}

        # channel_id -> (member ids, fetched_at). conversations.list carries
        # no membership for channels or group DMs, so this is the only cache
        # for what _resolve_members()/conversations.members otherwise fetches
        # fresh on every single call -- see _MEMBERSHIP_CACHE_TTL's comment
        # for why this is short-lived rather than folded into the weekly
        # directory snapshots.
        self._member_cache: dict[str, tuple[list[str], datetime]] = {}

        # Guards against starting a second concurrent background directory
        # refresh (see _ensure_user_directory/_ensure_channel_directory) --
        # a lock per directory, not one shared lock, so a user-directory
        # refresh never blocks on a channel-directory refresh or vice versa.
        self._user_directory_refresh_lock = threading.Lock()
        self._channel_directory_refresh_lock = threading.Lock()

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
        separated to require all of them), the fast path resolves it to user
        id(s) against the cached user directory and asks ``users.conversations``
        directly which channels each shares with the caller -- one paginated
        call per needle, replacing what used to be one ``conversations.members``
        call *per channel returned*. A participant string the directory can't
        resolve unambiguously (see ``_resolve_participant_user_ids``) falls back
        to the old per-channel ``conversations.members`` walk, run concurrently
        across channels rather than one at a time.

        The filter -- either path -- is applied to each page of
        ``conversations.list`` as it arrives, and pagination continues until
        ``max_results`` *matching* channels have been collected or every page
        has been walked. Filtering only after truncating to the first
        ``max_results`` raw channels (as this used to) can silently miss a
        participant who is only a member of a channel that happens to sort
        past that cutoff -- ``max_results`` bounds how many matches come
        back, not which channels are eligible to match.
        """
        max_results = self._clamp(max_results, default=100, hi=1000)
        allowed_ids = self._participant_conversation_ids(
            participant, types="public_channel,private_channel"
        ) if participant else None
        allowed = set(allowed_ids) if allowed_ids is not None else None

        channels: list[SlackChannel] = []
        cursor: str | None = None
        try:
            while len(channels) < max_results:
                # Full pages regardless of how many matches are still
                # needed when filtering -- unlike the unfiltered case, a
                # page of raw channels doesn't map 1:1 to matches, so
                # shrinking the request wouldn't reduce API calls, only
                # the odds of finding enough matches per call.
                page_size = 200 if participant else min(200, max_results - len(channels))
                response = self._client.conversations_list(
                    exclude_archived=exclude_archived,
                    types="public_channel,private_channel",
                    limit=page_size,
                    cursor=cursor,
                )
                page_raw = response.get("channels", [])
                for raw in page_raw:
                    self._channel_name_cache[raw.get("id", "")] = raw.get("name", "")

                if not participant:
                    channels.extend(self._parse_channel(raw) for raw in page_raw)
                elif allowed is not None:
                    channels.extend(
                        self._parse_channel(raw) for raw in page_raw if raw.get("id", "") in allowed
                    )
                else:
                    page_channels = [self._parse_channel(raw) for raw in page_raw]
                    matches = _map_concurrent(
                        [c.id for c in page_channels],
                        lambda cid: self._channel_matches_participant(cid, participant),
                    )
                    channels.extend(c for c, ok in zip(page_channels, matches) if ok)

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
        group DMs, so knowing who's in each one costs one
        ``conversations.members`` call per chat regardless of ``participant``
        -- run concurrently (``_map_concurrent``) rather than one at a time.
        When ``participant`` is given (comma-separated to require all of them
        as members of the same group chat -- the same multi-name matching
        ``search_messages`` uses to resolve e.g. "Bob and Jane's group chat"
        to one conversation) and resolves against the cached user directory,
        that membership call only runs for the chats ``users.conversations``
        already confirmed match, typically a handful rather than every chat
        returned. A participant string the directory can't resolve
        unambiguously (see ``_resolve_participant_user_ids``) falls back to
        resolving every chat's membership first and matching by name, same
        as before this existed.
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

        allowed_ids = self._participant_conversation_ids(participant, types="mpim") if participant else None
        if allowed_ids is not None:
            allowed = set(allowed_ids)
            raw_chats = [raw for raw in raw_chats if raw.get("id", "") in allowed]

        chats = _map_concurrent(raw_chats, self._parse_group_chat)

        if participant and allowed_ids is None:
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
    ) -> tuple[list[SlackMessage], bool]:
        """Fetch recent messages in a channel via ``conversations.history``.

        Returns ``(messages, has_more)`` -- ``has_more`` is Slack's own
        pagination signal, not a comparison of ``len(messages)`` against
        ``limit``: a 3-message channel legitimately returns 3 messages with
        ``has_more=False``, while a workspace where the Slack app is
        distributed outside the Marketplace (see slack-setup.md) gets
        ``has_more=True`` at exactly 15 messages regardless of what
        ``limit`` asked for. Either way, the caller -- not this method --
        decides whether/how to disclose that to Claude (see
        connectors/slack.py's ``_get_channel_history``).
        """
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
        has_more = bool(response.get("has_more", False))
        logger.info(
            "get_channel_history %s returned %d message(s), has_more=%s",
            channel_id, len(messages), has_more,
        )
        return messages, has_more

    def get_thread_replies(
        self, channel_id: str, thread_ts: str
    ) -> tuple[list[SlackMessage], bool]:
        """Fetch all replies in a thread via ``conversations.replies``.

        Returns ``(messages, has_more)`` -- see ``get_channel_history``'s
        docstring for what ``has_more`` means and why it isn't derived from
        a message count.
        """
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
        has_more = bool(response.get("has_more", False))
        logger.info(
            "get_thread_replies %s/%s returned %d message(s), has_more=%s",
            channel_id, thread_ts, len(messages), has_more,
        )
        return messages, has_more

    def get_message(self, channel_id: str, ts: str) -> SlackMessage | None:
        """Fetch a single message by timestamp via one ``conversations.history``
        call (``latest``/``oldest`` both set to ``ts``, ``inclusive=True``,
        ``limit=1``) -- for a caller that only needs to display one known
        message, not fetch a whole channel or thread. ``send_message``'s own
        "In thread" preview line is the motivating case: reading it used to
        mean pulling the entire thread via ``get_thread_replies``
        (``conversations.replies``, the single most rate-limited method
        under the 2025 non-Marketplace rules -- see slack-setup.md) just to
        look at its first entry.

        Best-effort, like ``resolve_channel_name`` -- returns None rather
        than raising on any failure (API error, or the message no longer
        existing at that timestamp), since a preview line degrading to the
        raw id/ts is an acceptable fallback here, not a caller error.
        """
        if not channel_id or not ts:
            return None
        channel_name = self.resolve_channel_name(channel_id)
        try:
            response = self._client.conversations_history(
                channel=channel_id, latest=ts, oldest=ts, inclusive=True, limit=1,
            )
        except SlackApiError as exc:
            logger.debug("Could not fetch message %s/%s (non-fatal): %s", channel_id, ts, exc)
            return None
        raw_messages = response.get("messages", [])
        if not raw_messages:
            return None
        return self._parse_message(raw_messages[0], channel_id, channel_name)

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

        ``list_dms``/``list_group_chats`` are asked for up to 1000 matching
        conversations each -- generous on purpose, since ``participant``'s
        own resolution (id, or an exact/unambiguous directory match) already
        narrows this correctly in the common case; ``_SEARCH_BY_PARTICIPANT_
        CONVERSATION_CAP`` below is the real bound, protecting the fan-out
        below it against the rarer case where ``participant`` resolves
        loosely (a substring matching several people) and would otherwise
        turn one search into a history fetch per accidental match. The
        history fetches themselves run concurrently (``_map_concurrent``).
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

        capped = len(channel_ids) > _SEARCH_BY_PARTICIPANT_CONVERSATION_CAP
        channel_ids = channel_ids[:_SEARCH_BY_PARTICIPANT_CONVERSATION_CAP]
        if capped:
            logger.warning(
                "search_messages participant=%r matched more than %d conversations; "
                "only reading the first %d",
                participant, _SEARCH_BY_PARTICIPANT_CONVERSATION_CAP,
                _SEARCH_BY_PARTICIPANT_CONVERSATION_CAP,
            )

        oldest = None
        if days > 0:
            oldest = str((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())

        per_channel = _map_concurrent(
            channel_ids,
            lambda channel_id: self.get_channel_history(channel_id, limit=count, oldest=oldest),
        )
        # has_more is discarded here -- a participant search already reports
        # its own aggregate "matched N conversations" / count cap, so a
        # per-conversation has_more wouldn't have anywhere sensible to
        # surface at this level.
        messages: list[SlackMessage] = [m for msgs, _has_more in per_channel for m in msgs]

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

        Two id shapes never reach that live fallback at all: a ``B...`` bot
        id (``users.info`` can never resolve one -- it isn't a user) fails
        immediately with no API call, and an id that failed once recently is
        remembered in ``_user_negative_cache`` for ``_NEGATIVE_LOOKUP_TTL``
        and fails immediately too. Without this, an id absent from the
        directory -- a bot, a deactivated account, a Slack Connect external
        user -- costs a fresh, failing ``users.info`` call every single time
        it's looked up: on a channel history read, that's once per message
        it authored, every fetch, forever.
        """
        if not user_id:
            raise SlackClientError("get_user_info requires a user_id")
        if self._user_cache_file:
            self._ensure_user_directory()
        if user_id in self._user_cache:
            return self._user_cache[user_id]
        if _BOT_ID_RE.match(user_id):
            raise SlackClientError(f"get_user_info({user_id}) failed: not a user id (bot)")
        negative_at = self._user_negative_cache.get(user_id)
        if negative_at is not None and datetime.now(timezone.utc) - negative_at < _NEGATIVE_LOOKUP_TTL:
            raise SlackClientError(f"get_user_info({user_id}) failed: previously unresolvable")
        try:
            response = self._client.users_info(user=user_id)
        except SlackApiError as exc:
            self._user_negative_cache[user_id] = datetime.now(timezone.utc)
            raise SlackClientError(
                f"get_user_info({user_id}) failed: {self._describe_error(exc)}"
            ) from exc
        user = self._parse_user(response.get("user", {}))
        self._user_cache[user_id] = user
        self._user_negative_cache.pop(user_id, None)
        return user

    # ------------------------------------------------------------------ #
    # Directory caches (whole-workspace user/channel snapshots)
    # ------------------------------------------------------------------ #

    def ensure_directories_fresh(self) -> None:
        """Eagerly run the same freshness check ``get_user_info``/
        ``resolve_channel_name``/``resolve_is_group_dm`` would otherwise only
        run lazily on first use. Meant to be called once right after
        connecting, on a background thread (see ``_warm_connector_caches``
        in ``daemon_main.py``) so a snapshot that's gone stale while the app
        was closed (more than a week between restarts) gets refreshed
        without blocking daemon startup -- this can be a genuinely slow,
        multi-page ``conversations.list``/``users.list`` walk on a large
        workspace, and running it synchronously on the main thread would
        delay the menu bar icon appearing. A no-op (no network call at all)
        when both snapshots are already fresh. Never raises -- same
        best-effort semantics as the lazy path it shares its implementation
        with.

        Passes ``block=True`` -- unlike every lazy caller, this is already
        running on its own background thread by construction, so there is
        no request to avoid blocking and no reason to hand a stale snapshot
        back before the refresh it kicks off has actually finished.
        """
        if self._user_cache_file:
            self._ensure_user_directory(block=True)
        if self._channel_cache_file:
            self._ensure_channel_directory(block=True)

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

    def refresh_channel_directory(self, *, max_pages: int | None = None) -> tuple[int, bool]:
        """Force a re-sync of every conversation the token can see
        (public/private channels, DMs, group DMs) via ``conversations.list``
        (paginated, across all four types), replacing the current name/
        group-DM-flag snapshot. Raises on failure, same reasoning as
        ``refresh_user_directory``. DM ("im") entries contribute no name
        (that conversation type doesn't have one -- ``resolve_channel_name``
        already returns "" for them) but are fetched anyway so
        ``resolve_is_group_dm`` is covered for every conversation type, not
        just channels.

        ``max_pages=None`` (the default) walks every page in one call, same
        as before this parameter existed -- what the eager background warm
        at daemon startup uses (``ensure_directories_fresh()``), since
        that's on its own background thread and isn't racing a caller's
        timeout. ``max_pages`` set to a positive int instead does at most
        that many ``conversations.list`` calls (200 conversations each)
        before returning, merges whatever it fetched into the existing
        in-memory snapshot, and remembers its Slack-side pagination cursor
        on the client so the *next* call resumes from there instead of
        starting over -- what the ``slack_refresh_channel_cache`` bridge
        tool uses, so a workspace with enough channels to run one unbounded
        refresh past the calling MCP client's own tool-call timeout instead
        completes over a few bounded, resumable calls.

        The in-memory snapshot this merges into is loaded from disk first if
        it hasn't been already (same as the lazy ``_ensure_channel_directory``
        path), so a bounded call on a fresh process resumes on top of
        whatever was already cached, rather than starting from an empty map
        and briefly losing what's already known. Both the merged-so-far
        snapshot and the resume cursor are persisted to disk after *every*
        call, complete or not -- the weekly TTL (``_channel_directory_fetched_at``)
        only advances once a walk actually finishes (cursor exhausted), so a
        partial refresh is never mistaken for a fresh one, but the walk
        itself survives a daemon restart mid-way through instead of
        starting over from page one.

        Returns ``(total conversations cached so far, has_more)``.
        """
        if not self._channel_directory_loaded_from_disk:
            self._load_channel_directory_from_disk()
            self._channel_directory_loaded_from_disk = True
        names = dict(self._channel_name_cache)
        is_mpim = dict(self._channel_is_mpim_cache)
        cursor = self._channel_refresh_cursor
        pages_fetched = 0
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
                cursor = (response.get("response_metadata") or {}).get("next_cursor") or None
                pages_fetched += 1
                if not cursor or (max_pages is not None and pages_fetched >= max_pages):
                    break
        except SlackApiError as exc:
            raise SlackClientError(
                f"refresh_channel_directory failed: {self._describe_error(exc)}"
            ) from exc

        self._channel_name_cache = names
        self._channel_is_mpim_cache = is_mpim
        self._channel_directory_loaded_from_disk = True
        self._channel_refresh_cursor = cursor

        if cursor:
            self._save_channel_directory_to_disk()
            logger.info(
                "Slack channel directory refresh in progress: %d conversation(s) cached so far, more remain",
                len(names),
            )
            return len(names), True

        self._channel_directory_fetched_at = datetime.now(timezone.utc)
        self._save_channel_directory_to_disk()
        logger.info("Slack channel directory refreshed: %d conversation(s) cached", len(names))
        return len(names), False

    def _ensure_user_directory(self, *, block: bool = False) -> None:
        """Best-effort: loads the on-disk weekly snapshot (once per process)
        and re-syncs it from Slack if it's missing or older than a week.
        Failures are logged and swallowed, subject to a retry cooldown -- a
        directory miss just means ``get_user_info`` falls back to its
        existing per-id ``users.info`` call, same as if this cache didn't
        exist.

        ``block=False`` (the default -- every lazy, per-tool-call caller)
        serves the existing snapshot immediately, stale or not, and kicks
        the actual re-sync onto a background thread instead of running it
        inline: a gated tool call must never sit behind a multi-page
        ``users.list`` walk just because the weekly TTL happened to expire
        mid-session (see docs/slack-performance-review.md's R5). The one
        exception is when no snapshot has ever loaded at all (a fresh
        install, or on-disk load never having succeeded) -- there's nothing
        to serve while a background refresh runs, so that case still blocks,
        same as before background refresh existed. ``block=True`` (used only
        by ``ensure_directories_fresh``, itself already running on its own
        background thread at daemon startup) always waits for the refresh
        to finish before returning.

        The background refresh is single-flight: a lazy call that finds one
        already in progress (``_user_directory_refresh_lock`` held) returns
        immediately without starting a second one.
        """
        if not self._user_directory_loaded_from_disk:
            self._load_user_directory_from_disk()
            self._user_directory_loaded_from_disk = True
        now = datetime.now(timezone.utc)
        if self._user_directory_fetched_at and now - self._user_directory_fetched_at < USER_DIRECTORY_CACHE_TTL:
            return
        if self._user_directory_last_attempt and now - self._user_directory_last_attempt < _DIRECTORY_RETRY_COOLDOWN:
            return
        have_snapshot = self._user_directory_fetched_at is not None
        if block or not have_snapshot:
            self._user_directory_last_attempt = now
            try:
                self.refresh_user_directory()
            except SlackClientError as exc:
                logger.warning("Could not refresh Slack user directory (non-fatal): %s", exc)
            return
        if not self._user_directory_refresh_lock.acquire(blocking=False):
            return
        self._user_directory_last_attempt = now

        def _run() -> None:
            try:
                self.refresh_user_directory()
            except SlackClientError as exc:
                logger.warning("Could not refresh Slack user directory (non-fatal): %s", exc)
            finally:
                self._user_directory_refresh_lock.release()

        self._spawn_background("slack-user-dir-refresh", _run)

    def _ensure_channel_directory(self, *, block: bool = False) -> None:
        """Channel-directory counterpart to ``_ensure_user_directory`` -- see
        that method's docstring for the block/background-refresh reasoning,
        identical here."""
        if not self._channel_directory_loaded_from_disk:
            self._load_channel_directory_from_disk()
            self._channel_directory_loaded_from_disk = True
        now = datetime.now(timezone.utc)
        if self._channel_directory_fetched_at and now - self._channel_directory_fetched_at < CHANNEL_DIRECTORY_CACHE_TTL:
            return
        if self._channel_directory_last_attempt and now - self._channel_directory_last_attempt < _DIRECTORY_RETRY_COOLDOWN:
            return
        have_snapshot = self._channel_directory_fetched_at is not None
        if block or not have_snapshot:
            self._channel_directory_last_attempt = now
            try:
                self.refresh_channel_directory()
            except SlackClientError as exc:
                logger.warning("Could not refresh Slack channel directory (non-fatal): %s", exc)
            return
        if not self._channel_directory_refresh_lock.acquire(blocking=False):
            return
        self._channel_directory_last_attempt = now

        def _run() -> None:
            try:
                self.refresh_channel_directory()
            except SlackClientError as exc:
                logger.warning("Could not refresh Slack channel directory (non-fatal): %s", exc)
            finally:
                self._channel_directory_refresh_lock.release()

        self._spawn_background("slack-channel-dir-refresh", _run)

    @staticmethod
    def _spawn_background(name: str, target: Any) -> None:
        """Seam around starting a background thread -- a real thread for
        every production caller; tests substitute a synchronous stand-in
        (see TestGetUserInfoWithDirectoryCache) so a refresh's effect on the
        cache can be asserted deterministically without sleeping or joining
        a real thread."""
        threading.Thread(target=target, name=name, daemon=True).start()

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
            fetched_at_raw = data.get("fetched_at") or ""
            # "" (rather than a real timestamp) means the file only ever
            # holds a partial refresh's progress -- see
            # refresh_channel_directory's own docstring. Not yet fresh, but
            # still worth loading: the channels below, and the resume
            # cursor, both carry real progress a full re-walk would repeat.
            fetched_at = datetime.fromisoformat(fetched_at_raw) if fetched_at_raw else None
            raw_channels = data.get("channels") or {}
            partial_cursor = data.get("partial_cursor") or None
        except Exception as exc:
            logger.warning("Could not load Slack channel directory cache (non-fatal): %s", exc)
            return
        for channel_id, raw in raw_channels.items():
            if not isinstance(raw, dict):
                continue
            self._channel_name_cache.setdefault(channel_id, raw.get("name", ""))
            self._channel_is_mpim_cache.setdefault(channel_id, bool(raw.get("is_mpim", False)))
        self._channel_directory_fetched_at = fetched_at
        if partial_cursor and self._channel_refresh_cursor is None:
            self._channel_refresh_cursor = partial_cursor
        logger.debug("Loaded %d cached Slack channel(s) from disk", len(raw_channels))

    def _save_channel_directory_to_disk(self) -> None:
        """Persists the current in-memory channel/group-DM-flag snapshot,
        called both when a walk completes and, via
        ``refresh_channel_directory``'s bounded ``max_pages`` path, mid-walk
        -- so a daemon restart before a bounded refresh finishes resumes
        from ``_channel_refresh_cursor`` (see that method's docstring)
        instead of re-walking every conversation from page one. ``fetched_at``
        is omitted (not written as an empty/garbage value) when no walk has
        ever completed -- ``_load_channel_directory_from_disk`` treats its
        absence as "not fresh yet," not as a parse error.
        """
        if not self._channel_cache_file:
            return
        channel_ids = set(self._channel_name_cache) | set(self._channel_is_mpim_cache)
        payload: dict[str, Any] = {
            "channels": {
                cid: {
                    "name": self._channel_name_cache.get(cid, ""),
                    "is_mpim": self._channel_is_mpim_cache.get(cid, False),
                }
                for cid in channel_ids
            },
        }
        if self._channel_directory_fetched_at is not None:
            payload["fetched_at"] = self._channel_directory_fetched_at.isoformat()
        if self._channel_refresh_cursor:
            payload["partial_cursor"] = self._channel_refresh_cursor
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
        """Best-effort user display-name lookup (cached, never raises). Falls
        back to a live ``users.info`` call on a cache miss -- reserved for
        callers where that's bounded and worth it: the public
        ``resolve_user_name`` wrapper (a handful of ids named in a write's
        approval popup) and the per-item fallback matching
        ``_channel_matches_participant`` already only reaches when a
        participant string can't be resolved via the directory at all. Bulk
        parsing (a name per message/DM/member returned by a read) uses
        ``_resolve_user_name_cached`` instead -- see that method."""
        if not user_id:
            return ""
        try:
            return self.get_user_info(user_id).short_summary()
        except SlackClientError as exc:
            logger.debug("Could not resolve user name for %s: %s", user_id, exc)
            return ""

    def _resolve_user_name_cached(self, user_id: str) -> str:
        """Cache-only counterpart to ``_resolve_user_name`` -- never makes a
        live ``users.info`` call for a miss, degrading to "" (the caller
        already falls back to the raw id) instead. Used by the bulk parsing
        paths (``_parse_message``, ``_parse_dm``, ``_parse_group_chat``)
        that resolve one name per message/DM/member a read returns: a live
        fallback there is exactly the per-item fan-out this module's
        directory caches exist to remove, for a name that's cosmetic --
        never load-bearing for a gated call's approval decision, which
        already shows the raw id when a name isn't available.

        When no ``user_cache_file`` was configured at all, there is no
        directory to be cache-only *about* -- falls back to
        ``_resolve_user_name``'s live-per-id lookup instead, same as every
        other method in this class when the cache is opted out of.
        """
        if not user_id:
            return ""
        if not self._user_cache_file:
            return self._resolve_user_name(user_id)
        self._ensure_user_directory()
        user = self._user_cache.get(user_id)
        return user.short_summary() if user is not None else ""

    def _resolve_members(self, channel_id: str) -> list[str]:
        """Fetch a conversation's member user ids via ``conversations.members``,
        paginated to completion (best-effort, never raises -- an
        unresolvable channel reads as having no members rather than
        blocking the whole listing; a channel with more than 1000 members
        used to silently lose everyone past the first page). Cached for
        ``_MEMBERSHIP_CACHE_TTL`` -- conversations.list carries no membership
        for channels or group DMs, so without this every listing call (and
        every participant filter that falls back to per-item matching) pays
        this again from scratch, even for a conversation just asked about.
        """
        cached = self._member_cache.get(channel_id)
        if cached is not None:
            members, fetched_at = cached
            if datetime.now(timezone.utc) - fetched_at < _MEMBERSHIP_CACHE_TTL:
                return members
        members: list[str] = []
        cursor: str | None = None
        try:
            for _ in range(_CONVERSATIONS_MEMBERS_PAGE_BUDGET):
                response = self._client.conversations_members(
                    channel=channel_id, limit=1000, cursor=cursor
                )
                members.extend(response.get("members", []) or [])
                cursor = (response.get("response_metadata") or {}).get("next_cursor") or None
                if not cursor:
                    break
        except SlackApiError as exc:
            logger.debug("Could not resolve members for %s: %s", channel_id, exc)
            return []
        self._member_cache[channel_id] = (members, datetime.now(timezone.utc))
        return members

    def _build_user_name_index(self) -> dict[str, str]:
        """Lowercased handle / real name / email -> user_id, built fresh from
        the in-memory user cache each time ``_resolve_participant_user_ids``
        needs it. A dict comprehension over a few thousand cached users is
        single-digit milliseconds -- far cheaper than the API calls it
        replaces -- so this rebuilds on demand rather than maintaining a
        second cache that could go stale relative to ``_user_cache``.
        ``setdefault`` keeps the first entry seen for a given key, so a name
        collision resolves deterministically rather than to whichever user
        happened to be inserted last.
        """
        index: dict[str, str] = {}
        for user_id, user in self._user_cache.items():
            for key in (user.name, user.real_name, user.email):
                if key:
                    index.setdefault(key.lower(), user_id)
        return index

    def _resolve_participant_user_ids(self, participant: str) -> list[str] | None:
        """Resolve every comma-separated needle in ``participant`` to exactly
        one Slack user id, or None if any needle can't be resolved
        unambiguously -- the signal callers use to fall back to the old
        per-item enumeration and matching.

        A needle that already looks like a user id (``_USER_ID_RE``) passes
        through unchanged -- it may not be in the directory yet (a brand new
        hire), but the per-item matching this replaces never validated raw
        ids against the directory either, so this preserves that. A needle
        that isn't an id is resolved against the cached user directory: an
        exact (case-insensitive) match on handle, real name, or email wins
        outright; failing that, a substring match on those same fields is
        accepted only when it's unique. An ambiguous substring resolves the
        whole call to None rather than picking one arbitrarily or matching
        all of them -- letting a needle like "user 7" match every "User 7x"
        would turn one intended conversation into a fan-out of extra API
        calls, the opposite of what this method exists to prevent (see
        docs/slack-performance-review.md's bug #6). The old, unmodified
        per-item path still applies that same loose substring matching when
        this returns None, so nothing here narrows what a caller can find --
        only how cheaply the unambiguous, common case is found.
        """
        needles = [p.strip() for p in participant.split(",") if p.strip()]
        if not needles:
            return None
        if self._user_cache_file:
            self._ensure_user_directory()
        index = self._build_user_name_index()
        resolved: list[str] = []
        for needle in needles:
            if _USER_ID_RE.match(needle):
                resolved.append(needle)
                continue
            key = needle.lower()
            exact = index.get(key)
            if exact is not None:
                resolved.append(exact)
                continue
            candidates = {user_id for name, user_id in index.items() if key in name}
            if len(candidates) == 1:
                resolved.append(next(iter(candidates)))
                continue
            return None
        return resolved

    def _conversation_ids_for_user(self, user_id: str, *, types: str) -> list[str] | None:
        """Every conversation of ``types`` (a Slack conversation-type string,
        e.g. ``"public_channel,private_channel"`` or ``"mpim"``) that
        ``user_id`` shares with the token holder, via one paginated
        ``users.conversations`` call -- the question ``list_channels``/
        ``list_group_chats``'s old participant filter answered by asking
        every conversation who its members were instead. Returns None (not
        an empty list -- that's a real "no matches") on any failure,
        including an unexpected response shape, so a resolvable participant
        whose ``users.conversations`` call can't complete falls back to the
        old per-item enumeration rather than reporting zero matches.
        """
        ids: list[str] = []
        cursor: str | None = None
        try:
            for _ in range(_USERS_CONVERSATIONS_PAGE_BUDGET):
                response = self._client.users_conversations(
                    user=user_id, types=types, limit=200, cursor=cursor,
                )
                ids.extend(response.get("channels", []) or [])
                cursor = (response.get("response_metadata") or {}).get("next_cursor") or None
                if not cursor:
                    break
        except Exception as exc:  # noqa: BLE001 -- best-effort fallback signal,
            # not a user-facing error: any failure here (a SlackApiError, or
            # an unexpected response shape from a test double or a future
            # API change) must degrade to "the caller falls back to its own
            # per-item walk," never propagate and abort a call this helper
            # only exists to make cheaper.
            logger.debug("users.conversations(%s) failed, falling back: %s", user_id, exc)
            return None
        return ids

    def _participant_conversation_ids(self, participant: str, *, types: str) -> list[str] | None:
        """The ids of conversations of ``types`` that satisfy every
        comma-separated needle in ``participant`` (AND) -- resolved via
        ``users.conversations`` instead of enumerating every conversation's
        membership. None means "couldn't resolve this cheaply," the signal
        callers use to fall back to their own per-item membership walk;
        never an empty list unless resolution genuinely found zero matching
        conversations.
        """
        if not participant:
            return None
        user_ids = self._resolve_participant_user_ids(participant)
        if user_ids is None:
            return None
        id_sets: list[set[str]] = []
        for user_id in user_ids:
            ids = self._conversation_ids_for_user(user_id, types=types)
            if ids is None:
                return None
            id_sets.append(set(ids))
        if not id_sets:
            return None
        result = id_sets[0]
        for s in id_sets[1:]:
            result &= s
        return list(result)

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
            user_name=self._resolve_user_name_cached(user_id) if user_id else "",
        )

    def _parse_group_chat(self, raw: dict[str, Any]) -> SlackGroupChat:
        channel_id = raw.get("id", "")
        member_ids = self._resolve_members(channel_id)
        return SlackGroupChat(
            id=channel_id,
            name=raw.get("name", ""),
            member_ids=member_ids,
            member_names=[self._resolve_user_name_cached(uid) for uid in member_ids],
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
            user_name=self._resolve_user_name_cached(user_id) if user_id else "",
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
