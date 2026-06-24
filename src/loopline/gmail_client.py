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
from dataclasses import dataclass, field
from typing import Any, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# Read-only scope. We never request write/send scopes - this tool only reads.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailClientError(Exception):
    """Raised for unrecoverable Gmail client problems (auth, config, API)."""


@dataclass
class Attachment:
    """Attachment metadata. Content is intentionally never carried here."""

    name: str
    mime_type: str
    size: int  # bytes, as reported by Gmail (0 if unknown)


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

    def __init__(self, credentials_file: str, token_file: str) -> None:
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._service = None  # lazily built googleapiclient resource

    # ------------------------------------------------------------------ #
    # Authentication
    # ------------------------------------------------------------------ #
    def authorize_interactive(self) -> None:
        """Run the interactive OAuth flow and persist the token.

        Intended to be called from the `--oauth-setup` command. Opens a local
        browser window, lets the user grant access, then writes the token to
        ``token_file``.
        """
        if not os.path.exists(self._credentials_file):
            raise GmailClientError(
                f"OAuth client secret not found at '{self._credentials_file}'. "
                "Download it from the Google Cloud Console (OAuth client of type "
                "'Desktop app') and place it there."
            )

        logger.info("Starting interactive OAuth flow")
        flow = InstalledAppFlow.from_client_secrets_file(
            self._credentials_file, SCOPES
        )
        creds = flow.run_local_server(port=0)
        self._save_token(creds)
        logger.info("OAuth token saved to '%s'", self._token_file)

    def _load_credentials(self) -> Credentials:
        """Load cached credentials, refreshing them if expired.

        Raises if no usable token exists - the user must run `--oauth-setup`.
        """
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
        """Build (or reuse) the Gmail API service resource."""
        if self._service is None:
            creds = self._load_credentials()
            # cache_discovery=False avoids noisy warnings without a file cache.
            self._service = build(
                "gmail", "v1", credentials=creds, cache_discovery=False
            )
            logger.debug("Gmail API service initialized")
        return self._service

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
                )
            )

        data = part_body.get("data")
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


@dataclass
class _Body:
    """Internal accumulator used while walking MIME parts."""

    text: str = ""
    html: str = ""
