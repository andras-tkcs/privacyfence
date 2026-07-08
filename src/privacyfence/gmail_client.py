"""Gmail API client.

Handles OAuth2 authorization and read-only access to Gmail. All message and
thread data is normalized into simple dataclasses so the rest of the
application never has to deal with the raw Gmail API payload shape.

Per project conventions we always use the documented Google client libraries
(`googleapiclient`, `google.auth`) and authenticate via the standard
google-auth-oauthlib installed-app flow.
"""

from __future__ import annotations

import base64
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# Modify scope: allows reading and modifying messages/labels, creating drafts.
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


class GmailClientError(Exception):
    """Raised for unrecoverable Gmail client problems (auth, config, API)."""


def resolve_attachment_destination(filename: str, destination_dir: str = "") -> str:
    """Compute where an attachment will be saved, without touching disk.

    ``filename`` comes from the sender's MIME headers and is untrusted, so
    only its basename is kept - this is what stops a crafted name like
    "../../.ssh/authorized_keys" from writing outside ``destination_dir``.
    Used both to preview the save path before download approval and by
    ``download_attachment`` to actually write the file, so the two never
    disagree.
    """
    dest_dir = os.path.expanduser(destination_dir.strip() or "~/Downloads")
    safe_name = os.path.basename(filename) or "attachment"
    return os.path.join(dest_dir, safe_name)


@dataclass
class Attachment:
    """Attachment metadata. Content is intentionally never carried here."""

    name: str
    mime_type: str
    size: int  # bytes, as reported by Gmail (0 if unknown)
    attachment_id: str = ""  # Gmail API id, used to fetch content on demand


@dataclass
class GmailMessage:
    """A normalized Gmail message."""

    id: str
    thread_id: str
    subject: str
    sender: str
    recipients: list[str] = field(default_factory=list)
    date: str = ""
    body_text: str = ""
    body_html: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)

    def short_summary(self) -> str:
        """Human-readable one-liner for the review UI / logs."""
        subject = self.subject or "(no subject)"
        sender = self.sender or "(unknown sender)"
        return f"{subject} - from {sender}"


@dataclass
class GmailThread:
    """A normalized Gmail thread with its messages."""

    id: str
    subject: str
    messages: list[GmailMessage] = field(default_factory=list)

    def short_summary(self) -> str:
        subject = self.subject or "(no subject)"
        return f"{subject} ({len(self.messages)} messages)"


class GmailClient:
    """Read-only Gmail client with OAuth2 token caching."""

    def __init__(self, client_config: dict, token_file: str) -> None:
        self._client_config = client_config
        self._token_file = token_file
        # googleapiclient service objects (and the httplib2 transport they
        # wrap) are not thread-safe. Requests are dispatched to a thread per
        # call (see connectors/*.py._fetch), so a single shared service can
        # have two threads read/write the same socket concurrently,
        # corrupting the connection (observed as SSL: WRONG_VERSION_NUMBER
        # on a later, unrelated request reusing the same connection). Keep
        # one service per thread instead of one shared instance.
        self._local = threading.local()
        self._creds_lock = threading.Lock()
        self._current_message_id: str = ""

    # ------------------------------------------------------------------ #
    # Authentication
    # ------------------------------------------------------------------ #
    def authorize_interactive(self) -> None:
        """Run the interactive OAuth flow and persist the token.

        Opens a local browser window, lets the user grant access, then writes
        the token to ``token_file``. ``client_config`` comes from the
        organization config bundle (installed via the menu bar), not a file
        on disk.
        """
        if not self._client_config:
            raise GmailClientError(
                "No Google organization config installed. Install/Update "
                "Organization Config from the PrivacyFence menu bar first."
            )

        logger.info("Starting interactive OAuth flow")
        flow = InstalledAppFlow.from_client_config(self._client_config, SCOPES)
        creds = flow.run_local_server(port=0)
        self._save_token(creds)
        logger.info("OAuth token saved to '%s'", self._token_file)

    def _load_credentials(self) -> Credentials:
        """Load cached credentials, refreshing them if expired.

        Raises if no usable token exists - the user must run `--oauth-setup`.
        """
        # Guards concurrent refresh/save of the shared token file when
        # multiple threads hit an expired token at the same time.
        with self._creds_lock:
            if not os.path.exists(self._token_file):
                raise GmailClientError(
                    f"No OAuth token found at '{self._token_file}'. "
                    "Run the application once with '--oauth-setup' to authorize."
                )

            creds = Credentials.from_authorized_user_file(self._token_file, SCOPES)

            if creds.valid:
                return creds

            if creds.expired and creds.refresh_token:
                logger.info("Refreshing expired OAuth token")
                try:
                    creds.refresh(Request())
                except Exception as exc:  # noqa: BLE001 - surface a clear message
                    raise GmailClientError(
                        f"Failed to refresh OAuth token: {exc}. "
                        "Re-run with '--oauth-setup' to re-authorize."
                    ) from exc
                self._save_token(creds)
                return creds

            raise GmailClientError(
                "Cached OAuth token is invalid and cannot be refreshed. "
                "Re-run with '--oauth-setup' to re-authorize."
            )

    def _save_token(self, creds: Credentials) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self._token_file)), exist_ok=True)
        with open(self._token_file, "w", encoding="utf-8") as handle:
            handle.write(creds.to_json())
        # Tighten permissions - this file is a bearer credential.
        try:
            os.chmod(self._token_file, 0o600)
        except OSError:  # pragma: no cover - best effort on non-POSIX
            logger.debug("Could not chmod token file (non-fatal)")

    def _get_service(self):
        """Build (or reuse) the Gmail API service resource for this thread."""
        service = getattr(self._local, "service", None)
        if service is None:
            creds = self._load_credentials()
            # cache_discovery=False avoids noisy warnings without a file cache.
            service = build(
                "gmail", "v1", credentials=creds, cache_discovery=False
            )
            self._local.service = service
            logger.debug("Gmail API service initialized for thread %s", threading.current_thread().name)
        return service

    def check_connection(self) -> str:
        """Verify the credentials work. Returns the authorized email address."""
        try:
            profile = (
                self._get_service().users().getProfile(userId="me").execute()
            )
        except HttpError as exc:
            raise GmailClientError(f"Gmail connection check failed: {exc}") from exc
        email = profile.get("emailAddress", "unknown")
        logger.info("Connected to Gmail as %s", email)
        return email

    # ------------------------------------------------------------------ #
    # Read operations
    # ------------------------------------------------------------------ #
    def list_messages(self, query: str, max_results: int = 10) -> list[dict[str, str]]:
        """List message summaries matching a Gmail search query.

        Returns a list of dicts with ``id``, ``thread_id``, ``subject``,
        ``sender`` and ``date`` for lightweight display. We fetch metadata only
        (not full bodies) to keep this call cheap.
        """
        max_results = self._clamp_max_results(max_results)
        service = self._get_service()
        try:
            response = (
                service.users()
                .messages()
                .list(userId="me", q=query or "", maxResults=max_results)
                .execute()
            )
        except HttpError as exc:
            raise GmailClientError(f"list_messages failed: {exc}") from exc

        summaries: list[dict[str, str]] = []
        for stub in response.get("messages", []):
            try:
                meta = (
                    service.users()
                    .messages()
                    .get(
                        userId="me",
                        id=stub["id"],
                        format="metadata",
                        metadataHeaders=["Subject", "From", "Date"],
                    )
                    .execute()
                )
            except HttpError as exc:
                logger.warning("Skipping message %s: %s", stub.get("id"), exc)
                continue
            headers = self._headers_to_dict(meta)
            summaries.append(
                {
                    "id": meta.get("id", ""),
                    "thread_id": meta.get("threadId", ""),
                    "subject": headers.get("subject", ""),
                    "sender": headers.get("from", ""),
                    "date": headers.get("date", ""),
                }
            )
        logger.info(
            "list_messages query=%r returned %d summaries", query, len(summaries)
        )
        return summaries

    def get_message(self, message_id: str) -> GmailMessage:
        """Fetch a single full message and normalize it."""
        if not message_id:
            raise GmailClientError("get_message requires a non-empty message_id")
        service = self._get_service()
        try:
            raw = (
                service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
        except HttpError as exc:
            raise GmailClientError(
                f"get_message({message_id}) failed: {exc}"
            ) from exc
        message = self._parse_message(raw)
        logger.info("get_message %s: %s", message_id, message.short_summary())
        return message

    def list_threads(self, query: str, max_results: int = 10) -> list[dict[str, str]]:
        """List thread summaries matching a Gmail search query."""
        max_results = self._clamp_max_results(max_results)
        service = self._get_service()
        try:
            response = (
                service.users()
                .threads()
                .list(userId="me", q=query or "", maxResults=max_results)
                .execute()
            )
        except HttpError as exc:
            raise GmailClientError(f"list_threads failed: {exc}") from exc

        summaries: list[dict[str, str]] = []
        for stub in response.get("threads", []):
            summaries.append(
                {
                    "id": stub.get("id", ""),
                    "snippet": stub.get("snippet", ""),
                }
            )
        logger.info(
            "list_threads query=%r returned %d summaries", query, len(summaries)
        )
        return summaries

    def download_attachment(
        self, message_id: str, attachment_id: str, filename: str, destination_dir: str = ""
    ) -> dict:
        """Fetch an attachment's bytes and save it to a local directory.

        If ``destination_dir`` is empty, defaults to ``~/Downloads``. Returns a
        dict with ``path``, ``name``, and ``size_bytes``.
        """
        if not message_id or not attachment_id:
            raise GmailClientError(
                "download_attachment requires a non-empty message_id and attachment_id"
            )
        service = self._get_service()
        try:
            raw = (
                service.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=message_id, id=attachment_id)
                .execute()
            )
        except HttpError as exc:
            raise GmailClientError(
                f"download_attachment({message_id}, {attachment_id}) failed: {exc}"
            ) from exc

        data = base64.urlsafe_b64decode(raw.get("data", "").encode("utf-8"))
        dest_path = resolve_attachment_destination(filename or attachment_id, destination_dir)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as fh:
            fh.write(data)

        name = os.path.basename(dest_path)
        logger.info(
            "download_attachment: message_id=%s name=%s size=%d",
            message_id, name, len(data),
        )
        return {"path": dest_path, "name": name, "size_bytes": len(data)}

    # ------------------------------------------------------------------ #
    # Write operations
    # ------------------------------------------------------------------ #
    def create_draft(
        self, to: str, subject: str, body: str, cc: str = "", bcc: str = ""
    ) -> dict:
        """Create a Gmail draft and return its id."""
        import email.mime.text
        import email.mime.multipart
        import base64

        msg = email.mime.text.MIMEText(body)
        msg["to"] = self._encode_addresses(to)
        msg["subject"] = subject
        if cc:
            msg["cc"] = self._encode_addresses(cc)
        if bcc:
            msg["bcc"] = self._encode_addresses(bcc)

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        service = self._get_service()
        try:
            draft = (
                service.users()
                .drafts()
                .create(userId="me", body={"message": {"raw": raw}})
                .execute()
            )
        except HttpError as exc:
            raise GmailClientError(f"create_draft failed: {exc}") from exc
        draft_id = draft.get("id", "")
        logger.info("create_draft: draft_id=%s to=%s", draft_id, to)
        return {"draft_id": draft_id, "to": to, "subject": subject}

    def create_reply_draft(
        self,
        message_id: str,
        body: str,
        reply_all: bool = False,
        my_email: str = "",
        cc: str = "",
        bcc: str = "",
    ) -> dict:
        """Create a draft that replies to an existing message in-thread.

        Gmail only nests a draft under the original thread in the UI if,
        beyond setting ``threadId``, the RFC 2822 ``In-Reply-To``/``References``
        headers chain back to the original message's ``Message-ID``. We fetch
        those headers (plus From/To/Cc/Subject) with a cheap metadata-only
        call rather than reusing the normalized GmailMessage, which doesn't
        carry them.
        """
        import email.mime.text
        import email.utils

        if not message_id:
            raise GmailClientError("create_reply_draft requires a non-empty message_id")

        headers = self._get_reply_headers(message_id)
        thread_id = headers.get("thread_id", "")
        original_message_id = headers.get("message-id", "")
        original_subject = headers.get("subject", "")
        original_from = headers.get("from", "")
        original_references = headers.get("references", "")

        subject = (
            original_subject
            if original_subject.lower().startswith("re:")
            else f"Re: {original_subject}"
        )

        my_addr = email.utils.parseaddr(my_email)[1].lower()
        to_addr = original_from

        cc_candidates: list[str] = []
        if reply_all:
            cc_candidates.extend(self._split_addresses(headers.get("to", "")))
            cc_candidates.extend(self._split_addresses(headers.get("cc", "")))
        if cc:
            cc_candidates.extend(self._split_addresses(cc))

        exclude = {my_addr, email.utils.parseaddr(to_addr)[1].lower()}
        seen: set[str] = set()
        final_cc: list[str] = []
        for addr in cc_candidates:
            key = email.utils.parseaddr(addr)[1].lower()
            if not key or key in exclude or key in seen:
                continue
            seen.add(key)
            final_cc.append(addr)

        msg = email.mime.text.MIMEText(body)
        msg["to"] = self._encode_address(to_addr)
        msg["subject"] = subject
        if final_cc:
            msg["cc"] = ", ".join(self._encode_address(addr) for addr in final_cc)
        if bcc:
            msg["bcc"] = self._encode_addresses(bcc)
        if original_message_id:
            msg["In-Reply-To"] = original_message_id
            msg["References"] = (
                f"{original_references} {original_message_id}".strip()
                if original_references
                else original_message_id
            )

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        message_body: dict[str, Any] = {"raw": raw}
        if thread_id:
            message_body["threadId"] = thread_id

        service = self._get_service()
        try:
            draft = (
                service.users()
                .drafts()
                .create(userId="me", body={"message": message_body})
                .execute()
            )
        except HttpError as exc:
            raise GmailClientError(f"create_reply_draft failed: {exc}") from exc
        draft_id = draft.get("id", "")
        logger.info(
            "create_reply_draft: draft_id=%s thread_id=%s to=%s reply_all=%s",
            draft_id, thread_id, to_addr, reply_all,
        )
        return {
            "draft_id": draft_id,
            "thread_id": thread_id,
            "to": to_addr,
            "cc": ", ".join(final_cc),
            "subject": subject,
        }

    def _get_reply_headers(self, message_id: str) -> dict[str, str]:
        """Fetch just the headers needed to build a correctly threaded reply."""
        service = self._get_service()
        try:
            raw = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["Subject", "From", "To", "Cc", "Message-ID", "References"],
                )
                .execute()
            )
        except HttpError as exc:
            raise GmailClientError(f"get_message({message_id}) failed: {exc}") from exc
        headers = self._headers_to_dict(raw)
        headers["thread_id"] = raw.get("threadId", "")
        return headers

    def add_label(self, message_id: str, label_name: str) -> dict:
        """Add a label to a message. Creates the label if it does not exist."""
        service = self._get_service()
        label_id = self._get_or_create_label(label_name)
        try:
            service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"addLabelIds": [label_id]},
            ).execute()
        except HttpError as exc:
            raise GmailClientError(
                f"add_label({message_id}, {label_name!r}) failed: {exc}"
            ) from exc
        logger.info("add_label: message_id=%s label=%s", message_id, label_name)
        return {"message_id": message_id, "label_added": label_name}

    def archive_message(self, message_id: str) -> dict:
        """Archive a message by removing the INBOX system label."""
        service = self._get_service()
        try:
            service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"removeLabelIds": ["INBOX"]},
            ).execute()
        except HttpError as exc:
            raise GmailClientError(
                f"archive_message({message_id}) failed: {exc}"
            ) from exc
        logger.info("archive_message: message_id=%s", message_id)
        return {"message_id": message_id, "archived": True}

    def remove_label(self, message_id: str, label_name: str) -> dict:
        """Remove a label from a message."""
        service = self._get_service()
        label_id = self._get_label_id(label_name)
        if not label_id:
            return {"message_id": message_id, "label_removed": label_name, "note": "label not found"}
        try:
            service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"removeLabelIds": [label_id]},
            ).execute()
        except HttpError as exc:
            raise GmailClientError(
                f"remove_label({message_id}, {label_name!r}) failed: {exc}"
            ) from exc
        logger.info("remove_label: message_id=%s label=%s", message_id, label_name)
        return {"message_id": message_id, "label_removed": label_name}

    def _get_or_create_label(self, label_name: str) -> str:
        """Return an existing label id, or create the label and return its new id."""
        existing = self._get_label_id(label_name)
        if existing:
            return existing
        service = self._get_service()
        try:
            result = service.users().labels().create(
                userId="me", body={"name": label_name}
            ).execute()
        except HttpError as exc:
            raise GmailClientError(f"create_label({label_name!r}) failed: {exc}") from exc
        return result.get("id", "")

    def _get_label_id(self, label_name: str) -> str:
        """Return the id for a label name, or '' if not found."""
        service = self._get_service()
        try:
            response = service.users().labels().list(userId="me").execute()
        except HttpError as exc:
            raise GmailClientError(f"labels.list failed: {exc}") from exc
        for label in response.get("labels", []):
            if label.get("name", "").lower() == label_name.lower():
                return label.get("id", "")
        return ""

    def get_thread(self, thread_id: str) -> GmailThread:
        """Fetch a full thread and normalize each message."""
        if not thread_id:
            raise GmailClientError("get_thread requires a non-empty thread_id")
        service = self._get_service()
        try:
            raw = (
                service.users()
                .threads()
                .get(userId="me", id=thread_id, format="full")
                .execute()
            )
        except HttpError as exc:
            raise GmailClientError(f"get_thread({thread_id}) failed: {exc}") from exc

        messages = [self._parse_message(m) for m in raw.get("messages", [])]
        subject = messages[0].subject if messages else ""
        thread = GmailThread(id=raw.get("id", thread_id), subject=subject, messages=messages)
        logger.info("get_thread %s: %s", thread_id, thread.short_summary())
        return thread

    # ------------------------------------------------------------------ #
    # Parsing helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _clamp_max_results(max_results: int) -> int:
        """Defensive bounds on caller-supplied result counts."""
        try:
            value = int(max_results)
        except (TypeError, ValueError):
            value = 10
        return max(1, min(value, 100))

    @staticmethod
    def _headers_to_dict(message: dict[str, Any]) -> dict[str, str]:
        headers = message.get("payload", {}).get("headers", [])
        return {h.get("name", "").lower(): h.get("value", "") for h in headers}

    def _parse_message(self, raw: dict[str, Any]) -> GmailMessage:
        headers = self._headers_to_dict(raw)
        recipients = self._split_addresses(headers.get("to", ""))
        attachments: list[Attachment] = []
        body = _Body()
        self._current_message_id = raw.get("id", "")
        self._walk_parts(raw.get("payload", {}), body, attachments)

        return GmailMessage(
            id=raw.get("id", ""),
            thread_id=raw.get("threadId", ""),
            subject=headers.get("subject", ""),
            sender=headers.get("from", ""),
            recipients=recipients,
            date=headers.get("date", ""),
            body_text=body.text,
            body_html=body.html,
            attachments=attachments,
            labels=raw.get("labelIds", []),
        )

    def _walk_parts(
        self, part: dict[str, Any], body: "_Body", attachments: list[Attachment]
    ) -> None:
        """Recursively walk a MIME part tree collecting bodies and attachments."""
        mime_type = part.get("mimeType", "")
        filename = part.get("filename", "")
        part_body = part.get("body", {})

        if filename:
            attachments.append(
                Attachment(
                    name=filename,
                    mime_type=mime_type,
                    size=int(part_body.get("size", 0) or 0),
                    attachment_id=part_body.get("attachmentId", "") or "",
                )
            )

        data = part_body.get("data")
        attachment_id = part_body.get("attachmentId")
        if not data and attachment_id and not filename:
            # Large body parts are stored as attachments; fetch inline.
            try:
                att = (
                    self._get_service()
                    .users()
                    .messages()
                    .attachments()
                    .get(userId="me", messageId=self._current_message_id, id=attachment_id)
                    .execute()
                )
                data = att.get("data")
            except Exception:  # noqa: BLE001
                pass
        if data and not filename:
            decoded = self._decode_body(data)
            if mime_type == "text/plain":
                body.text += decoded
            elif mime_type == "text/html":
                body.html += decoded

        for sub_part in part.get("parts", []) or []:
            self._walk_parts(sub_part, body, attachments)

    @staticmethod
    def _decode_body(data: str) -> str:
        try:
            return base64.urlsafe_b64decode(data.encode("utf-8")).decode(
                "utf-8", errors="replace"
            )
        except (ValueError, TypeError) as exc:
            logger.warning("Failed to decode message body part: %s", exc)
            return ""

    @staticmethod
    def _split_addresses(value: str) -> list[str]:
        if not value:
            return []
        return [addr.strip() for addr in value.split(",") if addr.strip()]

    @staticmethod
    def _encode_address(addr: str) -> str:
        """Re-encode a "Display Name <addr@x.com>" string for a safe RFC 2822 header.

        Assigning a raw ``"Kázmér Kovács <kazmer@example.com>"`` string straight
        to a ``Message`` header encodes the *entire* value (name, brackets, and
        address) as one opaque RFC 2047 encoded-word once it contains non-ASCII
        text. Gmail's own header parser then rejects it with "Invalid To
        header", since an encoded-word isn't a valid substitute for a whole
        addr-spec. Round-tripping through parseaddr/formataddr instead encodes
        only the display name, leaving the address itself as plain ASCII
        inside ``<...>``.
        """
        import email.utils

        if not addr:
            return addr
        name, email_addr = email.utils.parseaddr(addr)
        if not email_addr:
            return addr
        return email.utils.formataddr((name, email_addr))

    @classmethod
    def _encode_addresses(cls, value: str) -> str:
        return ", ".join(cls._encode_address(a) for a in cls._split_addresses(value))


@dataclass
class _Body:
    """Internal accumulator used while walking MIME parts."""

    text: str = ""
    html: str = ""
