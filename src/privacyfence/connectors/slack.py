"""Slack connector."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..gate import current_reason, gated_call
from ..privacy_filter import apply_list, apply_text, category_policy
from ..slack_client import SlackClient, SlackClientError

logger = logging.getLogger(__name__)


def _message_to_dict(m: Any) -> dict[str, Any]:
    return {
        "ts": m.id,
        "channel_id": m.channel_id,
        "channel_name": m.channel_name,
        "user_id": m.user_id,
        "user_name": m.user_name,
        "text": m.text,
        "thread_ts": m.thread_ts,
        "reply_count": m.reply_count,
    }


def _apply_message_privacy(dicts: list[dict[str, Any]], content_category: str) -> list[dict[str, Any]]:
    """Apply slack_privacy's content_category (message_content or
    thread_content) and user_identity policy to already-dict-shaped
    messages, once, in place -- lines/details/pii_scan_text/filtered_data
    below are all derived from this one filtered representation so a
    block/redact decision can't accidentally apply to some but not others.
    See privacy_filter.py's module docstring for allow/redact/block semantics.
    """
    for d in dicts:
        d["text"] = apply_text("slack_privacy", content_category, d["text"] or "")
        d["user_name"] = apply_text("slack_privacy", "user_identity", d["user_name"] or "")
        d["user_id"] = apply_text("slack_privacy", "user_identity", d["user_id"] or "")
    return dicts


class SlackConnector(Connector):
    def __init__(self, client: SlackClient) -> None:
        self._slack = client
        self.my_email: str = ""

    @property
    def client(self) -> SlackClient:
        return self._slack

    @property
    def name(self) -> str:
        return "slack"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="slack_list_channels",
                description=(
                    "List Slack channels visible to the user "
                    "(id, name, privacy, topic, purpose, member count). Optionally "
                    "filter to channels a specific participant belongs to. "
                    "Auto-approved."
                ),
                params=[
                    ToolParam("exclude_archived", "bool", required=False, default=True),
                    ToolParam("max_results", "int", required=False, default=100),
                    ToolParam(
                        "participant", "str", required=False, default="",
                        description=(
                            "Filter to channels this participant (user id, handle, or name) "
                            "belongs to; comma-separated to require all of them as members of "
                            "the same channel; empty returns all"
                        ),
                    ),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="slack_list_dms",
                description=(
                    "List 1:1 direct-message conversations visible to the user "
                    "(id, other participant). Optionally filter to the DM with a "
                    "specific participant (user id, handle, or display name). "
                    "Auto-approved."
                ),
                params=[
                    ToolParam("max_results", "int", required=False, default=100),
                    ToolParam(
                        "participant", "str", required=False, default="",
                        description="Filter to the DM with this participant (user id, handle, or name); empty returns all",
                    ),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="slack_list_group_chats",
                description=(
                    "List group-DM conversations visible to the user "
                    "(id, name, participants). Optionally filter to group chats "
                    "containing a specific participant (user id, handle, or "
                    "display name). Auto-approved."
                ),
                params=[
                    ToolParam("max_results", "int", required=False, default=100),
                    ToolParam(
                        "participant", "str", required=False, default="",
                        description=(
                            "Filter to group chats containing this participant (user id, "
                            "handle, or name); comma-separated to require all of them as "
                            "members of the same group chat; empty returns all"
                        ),
                    ),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="slack_resolve_permalink",
                description=(
                    "Parse a Slack message permalink (from a message's \"Copy link\") "
                    "into the channel id, timestamp, and (if the link points at a "
                    "threaded reply) thread root timestamp needed by "
                    "slack_get_channel_history/slack_get_thread_replies. Reads no "
                    "message content -- just decodes the link. Auto-approved."
                ),
                params=[
                    ToolParam(
                        "url", "str",
                        description=(
                            "A Slack message permalink, e.g. "
                            "https://workspace.slack.com/archives/C0123/p1700000000123456"
                        ),
                    ),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="slack_get_channel_history",
                description="Fetch recent messages in a Slack channel. Requires user approval.",
                params=[
                    ToolParam("channel_id", "str"),
                    ToolParam("limit", "int", required=False, default=50),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="slack_get_thread_replies",
                description="Fetch all replies in a Slack thread. Requires user approval.",
                params=[
                    ToolParam("channel_id", "str"),
                    ToolParam("thread_ts", "str"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="slack_search_messages",
                description=(
                    "Search Slack messages matching a query, a participant, or both. "
                    "Prefer participant (a user id, handle, or display name) over a "
                    "text-only query when looking for messages from or with someone "
                    "-- e.g. 'Bob wrote me' is participant='Bob'; 'Bob in a chat with "
                    "Jane' is participant='Bob,Jane' -- it reads the matching DM/group-"
                    "chat conversation(s) directly instead of relying on Slack's search "
                    "index, which is more reliable for participant-based lookups. "
                    "Combine with query to also filter those conversations' text. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("query", "str", required=False, default=""),
                    ToolParam("count", "int", required=False, default=20),
                    ToolParam(
                        "participant", "str", required=False, default="",
                        description=(
                            "Filter to the DM/group-chat conversation(s) with this "
                            "participant (user id, handle, or name); comma-separated "
                            "to match a group chat containing all of them"
                        ),
                    ),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="slack_create_group_chat",
                description=(
                    "Create (or reopen the existing) group-DM conversation with the given "
                    "participants and return its channel id, ready for slack_send_message. "
                    "Participants must already have a Slack user id (from slack_list_dms, "
                    "slack_list_group_chats, or a message's user_id) -- this does not resolve "
                    "email addresses or handles. Requires user approval."
                ),
                params=[
                    ToolParam(
                        "participants", "str",
                        description="Comma-separated Slack user IDs to include (at least 2, e.g. 'U123,U456')",
                    ),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="slack_send_message",
                description=(
                    "Send a message to a Slack channel or DM. Requires user approval. "
                    "Set mark_unread=true to leave the message unread after sending "
                    "(useful when sending a DM to yourself as a note; requires the "
                    "im:write scope on the user token for DMs)."
                ),
                params=[
                    ToolParam("channel_id", "str"),
                    ToolParam("text", "str"),
                    ToolParam("thread_ts", "str", required=False, default=""),
                    ToolParam("mark_unread", "bool", required=False, default=False),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "slack_list_channels":
            return await self._list_channels(**args)
        if tool == "slack_list_dms":
            return await self._list_dms(**args)
        if tool == "slack_list_group_chats":
            return await self._list_group_chats(**args)
        if tool == "slack_resolve_permalink":
            return await self._resolve_permalink(**args)
        if tool == "slack_get_channel_history":
            return await self._get_channel_history(**args)
        if tool == "slack_get_thread_replies":
            return await self._get_thread_replies(**args)
        if tool == "slack_search_messages":
            return await self._search_messages(**args)
        if tool == "slack_create_group_chat":
            return await self._create_group_chat(**args)
        if tool == "slack_send_message":
            return await self._send_message(**args)
        raise ValueError(f"Unknown Slack tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Auto
    # ------------------------------------------------------------------ #

    async def _list_channels(
        self, exclude_archived: bool = True, max_results: int = 100, participant: str = ""
    ) -> Any:
        t0 = time.time()
        channels = await self._fetch(
            self._slack.list_channels, exclude_archived, max_results, participant
        )
        label = f"List channels (max {max_results})"
        if participant:
            label += f" with {participant}"
        self._auto_audit("slack_list_channels", "List Slack Channels",
                         label, f"{len(channels)} channel(s)", t0)
        result = [
            {
                "id": c.id,
                "name": c.name,
                "is_private": c.is_private,
                "topic": c.topic,
                "purpose": c.purpose,
                "member_count": c.member_count,
            }
            for c in channels
        ]
        return apply_list("slack_privacy", "channel_list", result)

    async def _list_dms(self, max_results: int = 100, participant: str = "") -> Any:
        t0 = time.time()
        dms = await self._fetch(self._slack.list_dms, max_results, participant)
        label = f"List DMs (max {max_results})"
        if participant:
            label += f" with {participant}"
        self._auto_audit("slack_list_dms", "List Slack DMs", label, f"{len(dms)} DM(s)", t0)
        result = [
            {
                "id": d.id,
                "user_id": apply_text("slack_privacy", "user_identity", d.user_id or ""),
                "user_name": apply_text("slack_privacy", "user_identity", d.user_name or ""),
            }
            for d in dms
        ]
        return apply_list("slack_privacy", "dm_list", result)

    async def _list_group_chats(self, max_results: int = 100, participant: str = "") -> Any:
        t0 = time.time()
        chats = await self._fetch(self._slack.list_group_chats, max_results, participant)
        label = f"List group chats (max {max_results})"
        if participant:
            label += f" with {participant}"
        self._auto_audit("slack_list_group_chats", "List Slack Group Chats", label, f"{len(chats)} group chat(s)", t0)
        result = [
            {
                "id": c.id,
                "name": c.name,
                "member_ids": [apply_text("slack_privacy", "user_identity", i or "") for i in c.member_ids],
                "member_names": [apply_text("slack_privacy", "user_identity", n or "") for n in c.member_names],
            }
            for c in chats
        ]
        return apply_list("slack_privacy", "group_chat_list", result)

    async def _resolve_permalink(self, url: str) -> Any:
        t0 = time.time()
        result = await self._fetch(self._slack.resolve_permalink, url)
        self._auto_audit(
            "slack_resolve_permalink", "Resolve Slack Permalink",
            f"Resolve permalink -> {result['channel_name'] or result['channel_id']}",
            result["channel_id"], t0,
        )
        return result

    # ------------------------------------------------------------------ #
    # Review gate (reads)
    # ------------------------------------------------------------------ #

    async def _get_channel_history(self, channel_id: str, limit: int = 50) -> Any:
        messages = await self._fetch(self._slack.get_channel_history, channel_id, limit)
        n = len(messages)
        channel_display = await self._channel_display(channel_id, messages)
        is_group_dm = await self._fetch(self._slack.resolve_is_group_dm, channel_id)
        filtered = _apply_message_privacy([_message_to_dict(m) for m in messages], "message_content")
        # Channel is known via slack_list_channels; Messages (count) is
        # only learned once approved. No literal excerpt here (no "First
        # message") -- the visibility row below already discloses "Message
        # text: Full message text" (or the redact/block equivalent), and
        # the actual text is in the right-pane table, so a separate literal
        # excerpt would just duplicate one of those two.
        preview = {"Channel": channel_display}
        new_info = {"Messages": str(n)}
        lines = [
            f"[{d['ts']}] {d['user_name'] or d['user_id'] or 'unknown'}: {d['text']}"
            for d in filtered
        ]
        details = "\n".join(lines)
        table = {
            "headers": ["Sender", "Date", "Message"],
            "rows": [[d["user_name"] or d["user_id"] or "unknown", d["ts"], d["text"]] for d in filtered],
        }
        return await gated_call(
            connector=self.name,
            tool="slack_get_channel_history",
            tool_name="Read Slack Channel",
            summary=f"{n} message{'s' if n != 1 else ''} from {channel_display}",
            sender=channel_id,
            raw_data=messages,
            filtered_data=filtered,
            gate="review",
            preview=preview,
            new_info=new_info,
            details_text=details,
            pii_scan_text="\n".join(d["text"] or "" for d in filtered),
            visibility={
                "Message text": category_policy("slack_privacy", "message_content"),
                "Usernames": category_policy("slack_privacy", "user_identity"),
            },
            preview_tables=[table] if filtered else [],
            table_only=True,
            my_email=self.my_email,
            args={"channel_id": channel_id, "is_group_dm": is_group_dm},
        )

    async def _get_thread_replies(self, channel_id: str, thread_ts: str) -> Any:
        messages = await self._fetch(self._slack.get_thread_replies, channel_id, thread_ts)
        n = len(messages)
        channel_display = await self._channel_display(channel_id, messages)
        is_group_dm = await self._fetch(self._slack.resolve_is_group_dm, channel_id)
        filtered = _apply_message_privacy([_message_to_dict(m) for m in messages], "thread_content")
        # Channel is known via slack_list_channels; Replies (count) is only
        # learned once approved. No literal excerpt here (no "Thread
        # starter") -- same reasoning as slack_get_channel_history: the
        # visibility row already discloses "Reply text: Full reply text"
        # (or the redact/block equivalent), and the actual text is in the
        # right-pane table.
        preview = {"Channel": channel_display}
        new_info = {"Replies": str(max(0, n - 1))}
        lines = [
            f"[{d['ts']}] {d['user_name'] or d['user_id'] or 'unknown'}: {d['text']}"
            for d in filtered
        ]
        details = f"Thread: {thread_ts}\n\n" + "\n".join(lines)
        table = {
            "headers": ["Sender", "Date", "Message"],
            "rows": [[d["user_name"] or d["user_id"] or "unknown", d["ts"], d["text"]] for d in filtered],
        }
        return await gated_call(
            connector=self.name,
            tool="slack_get_thread_replies",
            tool_name="Read Slack Thread",
            summary=f"{n} repl{'ies' if n != 1 else 'y'} in {channel_display}",
            sender=channel_id,
            raw_data=messages,
            filtered_data=filtered,
            gate="review",
            preview=preview,
            new_info=new_info,
            details_text=details,
            pii_scan_text="\n".join(d["text"] or "" for d in filtered),
            visibility={
                "Reply text": category_policy("slack_privacy", "thread_content"),
                "Usernames": category_policy("slack_privacy", "user_identity"),
            },
            preview_tables=[table] if filtered else [],
            table_only=True,
            my_email=self.my_email,
            args={"channel_id": channel_id, "thread_ts": thread_ts, "is_group_dm": is_group_dm},
        )

    async def _search_messages(self, query: str = "", count: int = 20, participant: str = "") -> Any:
        # Validate before gating, not after -- same reasoning as
        # calendar_set_event_visibility's/slack_create_group_chat's own
        # early checks: a doomed call shouldn't cost the user an
        # unnecessary approval decision.
        if not query and not participant:
            raise ValueError("slack_search_messages requires a query, a participant, or both")
        messages = await self._fetch(self._slack.search_messages, query, count, participant)
        n = len(messages)
        filtered = _apply_message_privacy([_message_to_dict(m) for m in messages], "message_content")
        # Query/participant are Claude's own input (kept in §1 as
        # identifying context, same reasoning as drive_sheets_get_values's
        # Range); Results (count) is only learned once this call is approved.
        preview: dict[str, str] = {}
        if query:
            preview["Query"] = query
        if participant:
            preview["Participant"] = participant
        new_info = {"Results": str(n)}
        lines = [
            f"[{d['channel_name']}] {d['user_name'] or d['user_id'] or 'unknown'}: {d['text']}"
            for d in filtered
        ]
        details = "\n".join(lines)
        # Search results span channels, unlike channel_history/thread_replies
        # (already fixed to one channel in §1), so the table needs an
        # explicit Channel column too.
        table = {
            "headers": ["Channel", "Sender", "Date", "Message"],
            "rows": [
                [d["channel_name"], d["user_name"] or d["user_id"] or "unknown", d["ts"], d["text"]]
                for d in filtered
            ],
        }
        if participant and query:
            target_desc = f"\"{query}\" with {participant}"
        elif participant:
            target_desc = f"messages with {participant}"
        else:
            target_desc = f"\"{query}\""
        return await gated_call(
            connector=self.name,
            tool="slack_search_messages",
            tool_name="Search Slack",
            summary=f"{n} result{'s' if n != 1 else ''} for {target_desc}",
            sender=query or participant,
            raw_data=messages,
            filtered_data=filtered,
            gate="review",
            preview=preview,
            new_info=new_info,
            details_text=details,
            pii_scan_text="\n".join(d["text"] or "" for d in filtered),
            visibility={
                "Message text": category_policy("slack_privacy", "message_content"),
                "Usernames": category_policy("slack_privacy", "user_identity"),
            },
            preview_tables=[table] if filtered else [],
            table_only=True,
            my_email=self.my_email,
            args={"query": query, "participant": participant},
        )

    # ------------------------------------------------------------------ #
    # Popup gate (writes)
    # ------------------------------------------------------------------ #

    async def _create_group_chat(self, participants: str) -> Any:
        # Validate before gating, not after -- same reasoning as
        # calendar_set_event_visibility's early visibility check: a doomed
        # call shouldn't cost the user an unnecessary approval decision.
        user_ids = [p.strip() for p in participants.split(",") if p.strip()]
        if len(set(user_ids)) < 2:
            raise ValueError(
                "slack_create_group_chat requires at least 2 distinct participant user IDs, "
                f"got {participants!r}"
            )
        # Unfiltered here, unlike the member_ids/member_names on the return
        # value below -- this is the popup shown to the human approving
        # their own action, not data flowing to Claude, so it always shows
        # who's really being added (same as _send_message's _channel_display).
        display_names = []
        for uid in user_ids:
            name = await self._fetch(self._slack.resolve_user_name, uid)
            display_names.append(name or uid)
        participants_text = ", ".join(display_names)
        preview = {"Participants": participants_text}
        await gated_call(
            connector=self.name,
            tool="slack_create_group_chat",
            tool_name="Create Slack Group Chat",
            summary=f"New group chat with {participants_text}",
            sender=",".join(user_ids),
            raw_data={"participants": user_ids},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=f"Participants: {participants_text}",
            my_email=self.my_email,
            args={"participants": participants},
        )
        chat = await self._fetch(self._slack.open_conversation, user_ids)
        return {
            "id": chat.id,
            "name": chat.name,
            "member_ids": [apply_text("slack_privacy", "user_identity", i or "") for i in chat.member_ids],
            "member_names": [apply_text("slack_privacy", "user_identity", n or "") for n in chat.member_names],
        }

    async def _send_message(
        self,
        channel_id: str,
        text: str,
        thread_ts: str = "",
        mark_unread: bool = False,
    ) -> Any:
        channel_display = await self._channel_display(channel_id)
        preview = {"Channel": channel_display}
        if thread_ts:
            # The thread's first (root) message, not the raw timestamp id --
            # conversations.replies (the same fetch _get_thread_replies uses
            # for its own root message) returns it as the first entry.
            # Best-effort: falls back to the raw thread_ts if the fetch
            # fails, same as other lookups in this file.
            try:
                thread_messages = await self._fetch(self._slack.get_thread_replies, channel_id, thread_ts)
                first_message_text = thread_messages[0].text if thread_messages else ""
            except RuntimeError:
                first_message_text = ""
            preview["In thread"] = first_message_text or thread_ts
        if mark_unread:
            preview["Mark unread"] = "after sending"
        await gated_call(
            connector=self.name,
            tool="slack_send_message",
            tool_name="Send Slack Message",
            summary=f"To {channel_display}: {text[:80]}{'…' if len(text) > 80 else ''}",
            sender=channel_id,
            raw_data={"channel_id": channel_id, "text": text, "thread_ts": thread_ts},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=text,
            my_email=self.my_email,
            args={"channel_id": channel_id, "thread_ts": thread_ts},
        )
        result = await self._fetch(self._slack.send_message, channel_id, text, thread_ts)
        if mark_unread and isinstance(result, dict):
            sent_ts = result.get("ts", "")
            # Use the channel ID from the response: chat.postMessage accepts user IDs
            # but conversations.mark requires the resolved DM channel ID (D...).
            resolved_channel = result.get("channel_id", channel_id)
            if sent_ts and resolved_channel:
                await self._fetch(
                    self._slack.mark_channel_unread_before, resolved_channel, sent_ts
                )
        return result

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _fetch(self, func, *args) -> Any:
        try:
            return await asyncio.to_thread(func, *args)
        except SlackClientError as exc:
            logger.error("Slack fetch failed: %s", exc)
            raise RuntimeError(str(exc)) from exc

    async def _channel_display(self, channel_id: str, messages: list | None = None) -> str:
        """'#channel-name' when resolvable, else the raw channel id. Prefers
        the channel_name already resolved onto a fetched message (avoids a
        redundant lookup) before falling back to a direct client call."""
        name = messages[0].channel_name if messages else ""
        if not name:
            name = await self._fetch(self._slack.resolve_channel_name, channel_id)
        return f"#{name}" if name else channel_id

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
                claude_reason=current_reason(),
            ))
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)
