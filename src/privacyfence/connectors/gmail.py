"""Gmail connector."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..gate import gated_call
from ..gmail_client import GmailClient, GmailClientError

logger = logging.getLogger(__name__)


class GmailConnector(Connector):
    def __init__(self, client: GmailClient) -> None:
        self._gmail = client
        self.my_email: str = ""

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
                read_only=True,
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
                read_only=True,
            ),
            ToolSpec(
                name="gmail_get_message",
                description=(
                    "Fetch a single Gmail message by id, including body, metadata, "
                    "and attachment list. Requires user approval."
                ),
                params=[ToolParam("message_id", "str")],
                read_only=True,
            ),
            ToolSpec(
                name="gmail_get_thread",
                description=(
                    "Fetch a full Gmail thread by id, including all messages. "
                    "Requires user approval."
                ),
                params=[ToolParam("thread_id", "str")],
                read_only=True,
            ),
            ToolSpec(
                name="gmail_list_message_attachments",
                description=(
                    "List attachment names and sizes for a Gmail message. "
                    "Requires user approval."
                ),
                params=[ToolParam("message_id", "str")],
                read_only=True,
            ),
            ToolSpec(
                name="gmail_create_draft",
                description="Create a Gmail draft. Requires user approval.",
                params=[
                    ToolParam("to", "str"),
                    ToolParam("subject", "str"),
                    ToolParam("body", "str"),
                    ToolParam("cc", "str", required=False, default=""),
                    ToolParam("bcc", "str", required=False, default=""),
                ],
            ),
            ToolSpec(
                name="gmail_add_label",
                description="Add a label to a Gmail message. Requires user approval.",
                params=[
                    ToolParam("message_id", "str"),
                    ToolParam("label_name", "str"),
                ],
            ),
            ToolSpec(
                name="gmail_remove_label",
                description="Remove a label from a Gmail message. Requires user approval.",
                params=[
                    ToolParam("message_id", "str"),
                    ToolParam("label_name", "str"),
                ],
            ),
            ToolSpec(
                name="gmail_archive_message",
                description=(
                    "Archive a Gmail message by removing it from the Inbox. "
                    "The message is not deleted and remains searchable. Requires user approval."
                ),
                params=[ToolParam("message_id", "str")],
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
        if tool == "gmail_list_message_attachments":
            return await self._list_message_attachments(**args)
        if tool == "gmail_create_draft":
            return await self._create_draft(**args)
        if tool == "gmail_add_label":
            return await self._add_label(**args)
        if tool == "gmail_remove_label":
            return await self._remove_label(**args)
        if tool == "gmail_archive_message":
            return await self._archive_message(**args)
        raise ValueError(f"Unknown Gmail tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Auto (no gate)
    # ------------------------------------------------------------------ #

    async def _list_messages(self, query: str, max_results: int = 10) -> Any:
        t0 = time.time()
        summaries = await self._fetch(self._gmail.list_messages, query, max_results)
        self._auto_audit("gmail_list_messages", "List Gmail Messages",
                         f"List messages: {query!r}", f"{len(summaries)} result(s)", t0)
        return summaries

    async def _list_threads(self, query: str, max_results: int = 10) -> Any:
        t0 = time.time()
        summaries = await self._fetch(self._gmail.list_threads, query, max_results)
        self._auto_audit("gmail_list_threads", "List Gmail Threads",
                         f"List threads: {query!r}", f"{len(summaries)} result(s)", t0)
        return summaries

    # ------------------------------------------------------------------ #
    # Review gate (reads)
    # ------------------------------------------------------------------ #

    async def _get_message(self, message_id: str) -> Any:
        message = await self._fetch(self._gmail.get_message, message_id)
        recipients = message.recipients if isinstance(message.recipients, str) else ", ".join(message.recipients or [])
        preview = {
            "From": message.sender or "(unknown)",
            "To": recipients or "(unknown)",
            "Date": message.date or "(unknown)",
            "Subject": message.subject or "(no subject)",
        }
        body = message.body_text or message.body_html or "(no body)"
        details = f"From: {message.sender}\nTo: {recipients}\nDate: {message.date}\nSubject: {message.subject}\n\n{body}"
        return await gated_call(
            connector=self.name,
            tool="gmail_get_message",
            tool_name="Read Email",
            summary=f"Read email: {message.subject or '(no subject)'}",
            sender=message.sender or "",
            raw_data=message,
            filtered_data=message.to_dict() if hasattr(message, "to_dict") else vars(message),
            gate="review",
            preview=preview,
            details_text=details,
            my_email=self.my_email,
            args={"message_id": message_id},
        )

    async def _get_thread(self, thread_id: str) -> Any:
        thread = await self._fetch(self._gmail.get_thread, thread_id)
        messages = thread.messages if hasattr(thread, "messages") else []
        all_participants: set[str] = set()
        for m in messages:
            if hasattr(m, "sender") and m.sender:
                all_participants.add(m.sender)
            if hasattr(m, "recipients"):
                recips = m.recipients if isinstance(m.recipients, list) else [m.recipients]
                all_participants.update(r for r in recips if r)
        subject = (messages[0].subject if messages and hasattr(messages[0], "subject") else "") or thread_id
        dates = [m.date for m in messages if hasattr(m, "date") and m.date]
        date_range = f"{dates[0]} – {dates[-1]}" if len(dates) > 1 else (dates[0] if dates else "")
        preview = {
            "Subject": subject,
            "Participants": ", ".join(sorted(all_participants)) or "(unknown)",
            "Messages": str(len(messages)),
            "Dates": date_range,
        }
        lines = []
        for i, m in enumerate(messages, 1):
            lines.append(f"--- Message {i} ---")
            lines.append(f"From: {getattr(m, 'sender', '')}")
            lines.append(f"Date: {getattr(m, 'date', '')}")
            body = getattr(m, "body_text", "") or getattr(m, "body_html", "") or ""
            lines.append(body)
        details = "\n".join(lines)
        filtered = thread.to_dict() if hasattr(thread, "to_dict") else vars(thread)
        return await gated_call(
            connector=self.name,
            tool="gmail_get_thread",
            tool_name="Read Email Thread",
            summary=f"Read thread: {subject}",
            sender=subject,
            raw_data=thread,
            filtered_data=filtered,
            gate="review",
            preview=preview,
            details_text=details,
            my_email=self.my_email,
            args={"thread_id": thread_id},
        )

    async def _list_message_attachments(self, message_id: str) -> Any:
        message = await self._fetch(self._gmail.get_message, message_id)
        recipients = message.recipients if isinstance(message.recipients, str) else ", ".join(message.recipients or [])
        preview = {
            "From": message.sender or "(unknown)",
            "To": recipients or "(unknown)",
            "Date": message.date or "(unknown)",
            "Subject": message.subject or "(no subject)",
        }
        attachments = [
            {"name": att.name, "mime_type": att.mime_type, "size": att.size}
            for att in (message.attachments or [])
        ]
        lines = [f"{a['name']}  ({a['mime_type']}, {a['size']} bytes)" for a in attachments] or ["(no attachments)"]
        details = f"From: {message.sender}\nSubject: {message.subject}\n\nAttachments:\n" + "\n".join(lines)
        return await gated_call(
            connector=self.name,
            tool="gmail_list_message_attachments",
            tool_name="List Email Attachments",
            summary=f"List attachments: {message.subject or '(no subject)'}",
            sender=message.sender or "",
            raw_data=message,
            filtered_data={"message_id": message_id, "attachments": attachments},
            gate="review",
            preview=preview,
            details_text=details,
            my_email=self.my_email,
            args={"message_id": message_id},
        )

    # ------------------------------------------------------------------ #
    # Popup gate (writes)
    # ------------------------------------------------------------------ #

    async def _create_draft(
        self, to: str, subject: str, body: str, cc: str = "", bcc: str = ""
    ) -> Any:
        details_lines = [f"To: {to}"]
        if cc:
            details_lines.append(f"Cc: {cc}")
        if bcc:
            details_lines.append(f"Bcc: {bcc}")
        details_lines += [f"Subject: {subject}", "", body]
        await gated_call(
            connector=self.name,
            tool="gmail_create_draft",
            tool_name="Create Gmail Draft",
            summary=f"Create draft: {subject}",
            sender=to,
            raw_data={"to": to, "subject": subject, "body": body, "cc": cc, "bcc": bcc},
            filtered_data=None,
            gate="popup",
            details_text="\n".join(details_lines),
            my_email=self.my_email,
            args={"to": to, "subject": subject},
        )
        return await self._fetch(self._gmail.create_draft, to, subject, body, cc, bcc)

    async def _add_label(self, message_id: str, label_name: str) -> Any:
        message = await self._fetch(self._gmail.get_message, message_id)
        details = f"From: {message.sender}\nSubject: {message.subject}\n\nAdd label: {label_name}"
        await gated_call(
            connector=self.name,
            tool="gmail_add_label",
            tool_name="Add Gmail Label",
            summary=f"Add label '{label_name}' to: {message.subject or message_id}",
            sender=message.sender or "",
            raw_data={"message_id": message_id, "label_name": label_name},
            filtered_data=None,
            gate="popup",
            details_text=details,
            my_email=self.my_email,
            args={"message_id": message_id, "label_name": label_name},
        )
        return await self._fetch(self._gmail.add_label, message_id, label_name)

    async def _remove_label(self, message_id: str, label_name: str) -> Any:
        message = await self._fetch(self._gmail.get_message, message_id)
        details = f"From: {message.sender}\nSubject: {message.subject}\n\nRemove label: {label_name}"
        await gated_call(
            connector=self.name,
            tool="gmail_remove_label",
            tool_name="Remove Gmail Label",
            summary=f"Remove label '{label_name}' from: {message.subject or message_id}",
            sender=message.sender or "",
            raw_data={"message_id": message_id, "label_name": label_name},
            filtered_data=None,
            gate="popup",
            details_text=details,
            my_email=self.my_email,
            args={"message_id": message_id, "label_name": label_name},
        )
        return await self._fetch(self._gmail.remove_label, message_id, label_name)

    async def _archive_message(self, message_id: str) -> Any:
        message = await self._fetch(self._gmail.get_message, message_id)
        details = (
            f"From: {message.sender}\n"
            f"Subject: {message.subject}\n\n"
            "Action: Archive (remove from Inbox)\n"
            "The message will remain in All Mail and is not deleted."
        )
        await gated_call(
            connector=self.name,
            tool="gmail_archive_message",
            tool_name="Archive Email",
            summary=f"Archive: {message.subject or '(no subject)'} from {message.sender or message_id}",
            sender=message.sender or "",
            raw_data={"message_id": message_id},
            filtered_data=None,
            gate="popup",
            details_text=details,
            my_email=self.my_email,
            args={"message_id": message_id},
        )
        return await self._fetch(self._gmail.archive_message, message_id)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _fetch(self, func, *args) -> Any:
        try:
            return await asyncio.to_thread(func, *args)
        except GmailClientError as exc:
            logger.error("Gmail fetch failed: %s", exc)
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
                auto_accept_rule="auto",
                latency_seconds=time.time() - created_at,
            ))
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)
