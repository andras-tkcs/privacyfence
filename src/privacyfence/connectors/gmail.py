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
from ..gmail_client import GmailClient, GmailClientError, resolve_attachment_destination
from ..html_to_text import html_to_text

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
                    "List attachment names, MIME types, and sizes for a Gmail "
                    "message. Auto-approved — metadata only, no attachment "
                    "content is returned. Use gmail_download_attachment to "
                    "fetch the actual file."
                ),
                params=[ToolParam("message_id", "str")],
                read_only=True,
            ),
            ToolSpec(
                name="gmail_download_attachment",
                description=(
                    "Download a Gmail attachment's content to a local directory "
                    "and return the saved file path. Identify the attachment by "
                    "the name returned from gmail_list_message_attachments. "
                    "destination_dir defaults to ~/Downloads. Requires user "
                    "approval."
                ),
                params=[
                    ToolParam("message_id", "str"),
                    ToolParam("attachment_name", "str"),
                    ToolParam("destination_dir", "str", required=False, default=""),
                ],
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
                name="gmail_reply_draft",
                description=(
                    "Create a Gmail draft replying to a single message, staying in the "
                    "same thread (sets threadId plus In-Reply-To/References so it "
                    "actually threads, unlike gmail_create_draft). Addressed only to the "
                    "original sender. Requires user approval."
                ),
                params=[
                    ToolParam("message_id", "str"),
                    ToolParam("body", "str"),
                    ToolParam("cc", "str", required=False, default=""),
                    ToolParam("bcc", "str", required=False, default=""),
                ],
            ),
            ToolSpec(
                name="gmail_reply_all_draft",
                description=(
                    "Create a Gmail draft replying to all participants of a message "
                    "(original sender plus To/Cc recipients, excluding yourself), "
                    "staying in the same thread. Requires user approval."
                ),
                params=[
                    ToolParam("message_id", "str"),
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
            ToolSpec(
                name="gmail_list_filters",
                description=(
                    "List all Gmail filters with their criteria and actions. "
                    "Auto-approved -- filter rules only, no message content is returned."
                ),
                params=[],
                read_only=True,
            ),
            ToolSpec(
                name="gmail_list_labels",
                description=(
                    "List all Gmail labels (system and user-created). Nested labels "
                    "have a '/' in their name (e.g. 'Work/Projects'). "
                    "Auto-approved -- label metadata only."
                ),
                params=[],
                read_only=True,
            ),
            ToolSpec(
                name="gmail_create_filter",
                description=(
                    "Create a Gmail filter. Provide at least one criteria field "
                    "(from_address, to_address, subject, query, has_attachment) and "
                    "at least one action (add_label_names, archive, mark_as_read, "
                    "star, forward_to). Requires user approval."
                ),
                params=[
                    ToolParam("from_address", "str", required=False, default=""),
                    ToolParam("to_address", "str", required=False, default=""),
                    ToolParam("subject", "str", required=False, default=""),
                    ToolParam(
                        "query", "str", required=False, default="",
                        description="Gmail search syntax; matches the filter's 'Has the words' field",
                    ),
                    ToolParam("has_attachment", "bool", required=False, default=False),
                    ToolParam(
                        "add_label_names", "str", required=False, default="",
                        description="Comma-separated label names to apply; created if missing",
                    ),
                    ToolParam("archive", "bool", required=False, default=False, description="Skip the Inbox"),
                    ToolParam("mark_as_read", "bool", required=False, default=False),
                    ToolParam("star", "bool", required=False, default=False),
                    ToolParam("forward_to", "str", required=False, default=""),
                ],
            ),
            ToolSpec(
                name="gmail_update_filter",
                description=(
                    "Replace an existing Gmail filter's criteria and actions, "
                    "identified by filter_id (from gmail_list_filters). Gmail's API "
                    "has no native filter update, so this deletes the filter and "
                    "creates a new one with the given fields, which gets a new id. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("filter_id", "str"),
                    ToolParam("from_address", "str", required=False, default=""),
                    ToolParam("to_address", "str", required=False, default=""),
                    ToolParam("subject", "str", required=False, default=""),
                    ToolParam("query", "str", required=False, default=""),
                    ToolParam("has_attachment", "bool", required=False, default=False),
                    ToolParam("add_label_names", "str", required=False, default=""),
                    ToolParam("archive", "bool", required=False, default=False),
                    ToolParam("mark_as_read", "bool", required=False, default=False),
                    ToolParam("star", "bool", required=False, default=False),
                    ToolParam("forward_to", "str", required=False, default=""),
                ],
            ),
            ToolSpec(
                name="gmail_create_label",
                description=(
                    "Create a Gmail label. Use '/' to create nested labels (e.g. "
                    "'Work/Projects' creates 'Projects' nested under 'Work', "
                    "creating 'Work' first if it doesn't already exist). Fails if "
                    "the exact label name already exists. Requires user approval."
                ),
                params=[ToolParam("label_name", "str")],
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
        if tool == "gmail_download_attachment":
            return await self._download_attachment(**args)
        if tool == "gmail_create_draft":
            return await self._create_draft(**args)
        if tool == "gmail_reply_draft":
            return await self._reply_draft(**args)
        if tool == "gmail_reply_all_draft":
            return await self._reply_all_draft(**args)
        if tool == "gmail_add_label":
            return await self._add_label(**args)
        if tool == "gmail_remove_label":
            return await self._remove_label(**args)
        if tool == "gmail_archive_message":
            return await self._archive_message(**args)
        if tool == "gmail_list_filters":
            return await self._list_filters(**args)
        if tool == "gmail_list_labels":
            return await self._list_labels(**args)
        if tool == "gmail_create_filter":
            return await self._create_filter(**args)
        if tool == "gmail_update_filter":
            return await self._update_filter(**args)
        if tool == "gmail_create_label":
            return await self._create_label(**args)
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

    async def _list_message_attachments(self, message_id: str) -> Any:
        t0 = time.time()
        message = await self._fetch(self._gmail.get_message, message_id)
        attachments = [
            {"name": att.name, "mime_type": att.mime_type, "size": att.size}
            for att in (message.attachments or [])
        ]
        self._auto_audit(
            "gmail_list_message_attachments", "List Gmail Attachments",
            f"List attachments: {message.subject or '(no subject)'}",
            message.sender or "", t0,
        )
        return {"message_id": message_id, "attachments": attachments}

    async def _list_filters(self) -> Any:
        t0 = time.time()
        filters = await self._fetch(self._gmail.list_filters)
        self._auto_audit("gmail_list_filters", "List Gmail Filters",
                         "List filters", f"{len(filters)} result(s)", t0)
        return filters

    async def _list_labels(self) -> Any:
        t0 = time.time()
        labels = await self._fetch(self._gmail.list_labels)
        self._auto_audit("gmail_list_labels", "List Gmail Labels",
                         "List labels", f"{len(labels)} result(s)", t0)
        return labels

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
        body = message.body_text or html_to_text(message.body_html) or "(no body)"
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
            details_text=body,
            pii_scan_text=body,
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
        bodies = []
        for i, m in enumerate(messages, 1):
            lines.append(f"--- Message {i} ---")
            lines.append(f"From: {getattr(m, 'sender', '')}")
            lines.append(f"Date: {getattr(m, 'date', '')}")
            body = getattr(m, "body_text", "") or html_to_text(getattr(m, "body_html", "") or "") or ""
            lines.append(body)
            bodies.append(body)
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
            pii_scan_text="\n".join(bodies),
            my_email=self.my_email,
            args={"thread_id": thread_id},
        )

    async def _download_attachment(
        self, message_id: str, attachment_name: str, destination_dir: str = ""
    ) -> Any:
        message = await self._fetch(self._gmail.get_message, message_id)
        attachment = next(
            (a for a in (message.attachments or []) if a.name == attachment_name), None
        )
        if attachment is None:
            raise RuntimeError(
                f"No attachment named {attachment_name!r} on message {message_id}"
            )
        dest_path = resolve_attachment_destination(attachment.name, destination_dir)
        preview = {
            "From": message.sender or "(unknown)",
            "Subject": message.subject or "(no subject)",
            "Attachment": attachment.name,
            "Type": attachment.mime_type,
            "Size": f"{attachment.size:,} bytes",
            "Will save to": dest_path,
        }
        details = "The attachment above will be downloaded to the destination shown."
        # Gate before touching disk: gated_call raises on denial, and only a
        # decision made here should ever cause the attachment to be written.
        await gated_call(
            connector=self.name,
            tool="gmail_download_attachment",
            tool_name="Download Gmail Attachment",
            summary=f"Download attachment '{attachment.name}' from: {message.subject or '(no subject)'}",
            sender=message.sender or "",
            raw_data=message,
            filtered_data=None,
            gate="review",
            preview=preview,
            details_text=details,
            pii_scan_text="",
            my_email=self.my_email,
            args={"message_id": message_id, "attachment_name": attachment_name},
        )
        return await self._fetch(
            self._gmail.download_attachment,
            message_id, attachment.attachment_id, attachment.name, destination_dir,
        )

    # ------------------------------------------------------------------ #
    # Popup gate (writes)
    # ------------------------------------------------------------------ #

    async def _create_draft(
        self, to: str, subject: str, body: str, cc: str = "", bcc: str = ""
    ) -> Any:
        preview = {"To": to}
        if cc:
            preview["Cc"] = cc
        if bcc:
            preview["Bcc"] = bcc
        preview["Subject"] = subject
        await gated_call(
            connector=self.name,
            tool="gmail_create_draft",
            tool_name="Create Gmail Draft",
            summary=f"Create draft: {subject}",
            sender=to,
            raw_data={"to": to, "subject": subject, "body": body, "cc": cc, "bcc": bcc},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=body,
            my_email=self.my_email,
            args={"to": to, "subject": subject},
        )
        return await self._fetch(self._gmail.create_draft, to, subject, body, cc, bcc)

    async def _reply_draft(
        self, message_id: str, body: str, cc: str = "", bcc: str = ""
    ) -> Any:
        message = await self._fetch(self._gmail.get_message, message_id)
        preview = {
            "In reply to": message.subject or "(no subject)",
            "To": message.sender or "(unknown)",
        }
        if cc:
            preview["Cc"] = cc
        if bcc:
            preview["Bcc"] = bcc
        await gated_call(
            connector=self.name,
            tool="gmail_reply_draft",
            tool_name="Create Gmail Reply Draft",
            summary=f"Reply draft: {message.subject or '(no subject)'}",
            sender=message.sender or "",
            raw_data={"message_id": message_id, "body": body, "cc": cc, "bcc": bcc},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=body,
            my_email=self.my_email,
            args={"message_id": message_id, "to": message.sender or ""},
        )
        return await self._fetch(
            self._gmail.create_reply_draft, message_id, body, False, self.my_email, cc, bcc
        )

    async def _reply_all_draft(
        self, message_id: str, body: str, cc: str = "", bcc: str = ""
    ) -> Any:
        message = await self._fetch(self._gmail.get_message, message_id)
        recipients = (
            message.recipients if isinstance(message.recipients, str) else ", ".join(message.recipients or [])
        )
        preview = {
            "In reply to": message.subject or "(no subject)",
            "To": message.sender or "(unknown)",
            "Also to": recipients or "(none)",
        }
        if cc:
            preview["Cc"] = cc
        if bcc:
            preview["Bcc"] = bcc
        # The full expanded audience this reply-all will actually reach —
        # original sender + original recipients + any extra cc — minus
        # ourselves. Auto-accept rules (to_is_myself, approved_recipient_domain)
        # check ALL of these, not just the primary sender, so a rule scoped to
        # a trusted domain can't be satisfied by the sender alone while an
        # external Cc'd participant slips through.
        original_recipients = message.recipients if isinstance(message.recipients, list) else (
            [r.strip() for r in (message.recipients or "").split(",") if r.strip()]
        )
        extra_cc = [r.strip() for r in cc.split(",") if r.strip()] if cc else []
        all_recipients = [message.sender or ""] + list(original_recipients) + extra_cc
        my_email_lower = self.my_email.lower()
        expanded_to = [
            r for r in all_recipients
            if r and (not self.my_email or my_email_lower not in r.lower())
        ]
        await gated_call(
            connector=self.name,
            tool="gmail_reply_all_draft",
            tool_name="Create Gmail Reply-All Draft",
            summary=f"Reply-all draft: {message.subject or '(no subject)'}",
            sender=message.sender or "",
            raw_data={"message_id": message_id, "body": body, "cc": cc, "bcc": bcc},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=body,
            my_email=self.my_email,
            args={"message_id": message_id, "to": expanded_to or [message.sender or ""]},
        )
        return await self._fetch(
            self._gmail.create_reply_draft, message_id, body, True, self.my_email, cc, bcc
        )

    async def _add_label(self, message_id: str, label_name: str) -> Any:
        message = await self._fetch(self._gmail.get_message, message_id)
        preview = {"From": message.sender or "(unknown)", "Subject": message.subject or "(no subject)", "Label": label_name}
        await gated_call(
            connector=self.name,
            tool="gmail_add_label",
            tool_name="Add Gmail Label",
            summary=f"Add label '{label_name}' to: {message.subject or message_id}",
            sender=message.sender or "",
            raw_data=message,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text="Label will be added; no other content changes.",
            my_email=self.my_email,
            args={"message_id": message_id, "label_name": label_name},
        )
        return await self._fetch(self._gmail.add_label, message_id, label_name)

    async def _remove_label(self, message_id: str, label_name: str) -> Any:
        message = await self._fetch(self._gmail.get_message, message_id)
        preview = {"From": message.sender or "(unknown)", "Subject": message.subject or "(no subject)", "Label": label_name}
        await gated_call(
            connector=self.name,
            tool="gmail_remove_label",
            tool_name="Remove Gmail Label",
            summary=f"Remove label '{label_name}' from: {message.subject or message_id}",
            sender=message.sender or "",
            raw_data=message,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text="Label will be removed; no other content changes.",
            my_email=self.my_email,
            args={"message_id": message_id, "label_name": label_name},
        )
        return await self._fetch(self._gmail.remove_label, message_id, label_name)

    async def _archive_message(self, message_id: str) -> Any:
        message = await self._fetch(self._gmail.get_message, message_id)
        preview = {"From": message.sender or "(unknown)", "Subject": message.subject or "(no subject)"}
        details = (
            "Action: Archive (remove from Inbox)\n"
            "The message will remain in All Mail and is not deleted."
        )
        await gated_call(
            connector=self.name,
            tool="gmail_archive_message",
            tool_name="Archive Email",
            summary=f"Archive: {message.subject or '(no subject)'} from {message.sender or message_id}",
            sender=message.sender or "",
            raw_data=message,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=details,
            my_email=self.my_email,
            args={"message_id": message_id},
        )
        return await self._fetch(self._gmail.archive_message, message_id)

    @staticmethod
    def _filter_preview(
        from_address: str, to_address: str, subject: str, query: str, has_attachment: bool,
        add_label_names: str, archive: bool, mark_as_read: bool, star: bool, forward_to: str,
    ) -> dict[str, str]:
        criteria_parts = []
        if from_address:
            criteria_parts.append(f"from: {from_address}")
        if to_address:
            criteria_parts.append(f"to: {to_address}")
        if subject:
            criteria_parts.append(f"subject: {subject}")
        if query:
            criteria_parts.append(f"has the words: {query}")
        if has_attachment:
            criteria_parts.append("has attachment")
        action_parts = []
        if add_label_names:
            action_parts.append(f"apply label(s): {add_label_names}")
        if star:
            action_parts.append("star it")
        if archive:
            action_parts.append("archive it (skip inbox)")
        if mark_as_read:
            action_parts.append("mark as read")
        if forward_to:
            action_parts.append(f"forward to: {forward_to}")
        return {
            "Criteria": "; ".join(criteria_parts) or "(none)",
            "Actions": "; ".join(action_parts) or "(none)",
        }

    async def _create_filter(
        self,
        from_address: str = "",
        to_address: str = "",
        subject: str = "",
        query: str = "",
        has_attachment: bool = False,
        add_label_names: str = "",
        archive: bool = False,
        mark_as_read: bool = False,
        star: bool = False,
        forward_to: str = "",
    ) -> Any:
        preview = self._filter_preview(
            from_address, to_address, subject, query, has_attachment,
            add_label_names, archive, mark_as_read, star, forward_to,
        )
        await gated_call(
            connector=self.name,
            tool="gmail_create_filter",
            tool_name="Create Gmail Filter",
            summary=f"Create filter — {preview['Criteria']}",
            sender="",
            raw_data=preview,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text="Filter will be created with the criteria and actions above.",
            my_email=self.my_email,
            args={
                "from_address": from_address, "to_address": to_address, "subject": subject,
                "query": query, "has_attachment": has_attachment, "add_label_names": add_label_names,
                "archive": archive, "mark_as_read": mark_as_read, "star": star, "forward_to": forward_to,
            },
        )
        return await self._fetch(
            self._gmail.create_filter, from_address, to_address, subject, query, has_attachment,
            add_label_names, archive, mark_as_read, star, forward_to,
        )

    async def _update_filter(
        self,
        filter_id: str,
        from_address: str = "",
        to_address: str = "",
        subject: str = "",
        query: str = "",
        has_attachment: bool = False,
        add_label_names: str = "",
        archive: bool = False,
        mark_as_read: bool = False,
        star: bool = False,
        forward_to: str = "",
    ) -> Any:
        preview = {
            "Filter ID": filter_id,
            **self._filter_preview(
                from_address, to_address, subject, query, has_attachment,
                add_label_names, archive, mark_as_read, star, forward_to,
            ),
        }
        details = (
            "Gmail has no filter-update API: this deletes the existing filter "
            f"(id: {filter_id}) and creates a new one with the settings above. "
            "The replacement filter will have a different id."
        )
        await gated_call(
            connector=self.name,
            tool="gmail_update_filter",
            tool_name="Update Gmail Filter",
            summary=f"Update filter {filter_id} — {preview['Criteria']}",
            sender="",
            raw_data=preview,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=details,
            my_email=self.my_email,
            args={
                "filter_id": filter_id, "from_address": from_address, "to_address": to_address,
                "subject": subject, "query": query, "has_attachment": has_attachment,
                "add_label_names": add_label_names, "archive": archive, "mark_as_read": mark_as_read,
                "star": star, "forward_to": forward_to,
            },
        )
        return await self._fetch(
            self._gmail.update_filter, filter_id, from_address, to_address, subject, query,
            has_attachment, add_label_names, archive, mark_as_read, star, forward_to,
        )

    async def _create_label(self, label_name: str) -> Any:
        stripped = label_name.strip("/")
        segments = [s.strip() for s in stripped.split("/") if s.strip()]
        normalized_name = "/".join(segments)
        # Check for a duplicate before gating, not after: create_label() only
        # discovers "label already exists" once it's already past the
        # approval popup, so a doomed duplicate call still cost the user an
        # unnecessary approval decision.
        existing = await self._fetch(self._gmail.list_labels)
        existing_names = {lbl.get("name", "").lower() for lbl in existing}
        if normalized_name.lower() in existing_names:
            raise RuntimeError(f"create_label({normalized_name!r}) failed: label already exists")
        preview = {"Label": label_name}
        details = "Label will be created; no other changes."
        if "/" in stripped:
            parent = stripped.rsplit("/", 1)[0]
            details = f"Nested label — parent '{parent}' will be created too if it doesn't already exist."
        await gated_call(
            connector=self.name,
            tool="gmail_create_label",
            tool_name="Create Gmail Label",
            summary=f"Create label: {label_name}",
            sender="",
            raw_data={"label_name": label_name},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=details,
            my_email=self.my_email,
            args={"label_name": label_name},
        )
        return await self._fetch(self._gmail.create_label, label_name)

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
