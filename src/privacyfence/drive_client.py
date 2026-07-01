"""Google Drive API client.

Handles OAuth2 authorization and read-only access to Google Drive. All file and
folder data is normalized into simple dataclasses so the rest of the
application never has to deal with the raw Drive API payload shape.

Per project conventions we always use the documented Google client libraries
(`googleapiclient`, `google.auth`) and authenticate via the standard
google-auth-oauthlib installed-app flow.

The Drive client shares the same OAuth client secret as Gmail but caches its
token separately (``drive_token.json``) so the two services can be authorized
independently.
"""

from __future__ import annotations

import io
import logging
import os
import re as _re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

logger = logging.getLogger(__name__)

# Full Drive scope: read + write + create + move + comment.
SCOPES = ["https://www.googleapis.com/auth/drive"]

# Google Workspace MIME types that must be exported (they cannot be downloaded
# directly). We export everything as plain text for review.
_GOOGLE_DOC_EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}

# Metadata fields requested from the Drive API for a single file.
# driveId is populated for files that live inside a Shared Drive.
_FILE_FIELDS = (
    "id, name, mimeType, size, createdTime, modifiedTime, "
    "owners(emailAddress), shared, webViewLink, parents, driveId"
)


# ------------------------------------------------------------------ #
# Markdown → Google Docs API helpers
# ------------------------------------------------------------------ #

_HEADING_PREFIXES = [
    ("#### ", "HEADING_4"),
    ("### ", "HEADING_3"),
    ("## ", "HEADING_2"),
    ("# ", "HEADING_1"),
]

_INLINE_RE = _re.compile(
    r"\*\*\*(.+?)\*\*\*"         # bold + italic
    r"|\*\*(.+?)\*\*"            # bold
    r"|\*(.+?)\*"                # italic
    r"|`(.+?)`"                  # code (no extra style, just plain text)
    r"|\[([^\]]+)\]\(([^)]+)\)"  # link [text](url)
)


def _parse_inline_runs(
    text: str,
) -> list[tuple[str, bool, bool, str]]:
    """Return a list of (text, bold, italic, url) from an inline Markdown string."""
    runs: list[tuple[str, bool, bool, str]] = []
    last = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > last:
            runs.append((text[last : m.start()], False, False, ""))
        if m.group(1):  # bold+italic
            runs.append((m.group(1), True, True, ""))
        elif m.group(2):  # bold
            runs.append((m.group(2), True, False, ""))
        elif m.group(3):  # italic
            runs.append((m.group(3), False, True, ""))
        elif m.group(4):  # code → plain
            runs.append((m.group(4), False, False, ""))
        elif m.group(5):  # link
            runs.append((m.group(5), False, False, m.group(6)))
        last = m.end()
    if last < len(text):
        runs.append((text[last:], False, False, ""))
    return runs or [("", False, False, "")]


def _markdown_to_docs_requests(markdown: str) -> list[dict]:
    """Convert simple Markdown to a list of Google Docs batchUpdate requests.

    Text is inserted in a single ``insertText`` call at index 1; subsequent
    requests apply paragraph and inline styles by character range.
    """
    lines = markdown.rstrip("\n").split("\n")

    # Parse each line into (inline-runs, paragraph-style, list-bullet-preset)
    parsed: list[tuple[list, str, str]] = []
    for line in lines:
        para_style = "NORMAL_TEXT"
        list_preset = ""
        for prefix, style in _HEADING_PREFIXES:
            if line.startswith(prefix):
                line = line[len(prefix):]
                para_style = style
                break
        else:
            if _re.match(r"^[-*+] ", line):
                line = line[2:]
                list_preset = "BULLET_DISC_CIRCLE_SQUARE"
            elif _re.match(r"^\d+\. ", line):
                line = _re.sub(r"^\d+\. ", "", line)
                list_preset = "NUMBERED_DECIMAL_ALPHA_ROMAN"
        parsed.append((_parse_inline_runs(line), para_style, list_preset))

    # Build full plain text and record per-line doc positions (1-based)
    full_text = ""
    line_spans: list[tuple[int, int, str, list, str]] = []
    for runs, para_style, list_preset in parsed:
        line_start = len(full_text) + 1
        for run_text, _, _, _ in runs:
            full_text += run_text
        full_text += "\n"
        line_end = len(full_text) + 1
        line_spans.append((line_start, line_end, para_style, runs, list_preset))

    if not full_text.strip("\n"):
        return []

    requests: list[dict] = [
        {"insertText": {"location": {"index": 1}, "text": full_text}}
    ]

    for line_start, line_end, para_style, runs, list_preset in line_spans:
        if para_style != "NORMAL_TEXT":
            requests.append(
                {
                    "updateParagraphStyle": {
                        "range": {
                            "startIndex": line_start,
                            "endIndex": line_end,
                        },
                        "paragraphStyle": {"namedStyleType": para_style},
                        "fields": "namedStyleType",
                    }
                }
            )
        if list_preset:
            requests.append(
                {
                    "createParagraphBullets": {
                        "range": {
                            "startIndex": line_start,
                            "endIndex": line_end,
                        },
                        "bulletPreset": list_preset,
                    }
                }
            )
        # Inline styles
        pos = line_start
        for run_text, bold, italic, url in runs:
            if not run_text:
                continue
            run_end = pos + len(run_text)
            text_style: dict = {}
            fields: list[str] = []
            if bold:
                text_style["bold"] = True
                fields.append("bold")
            if italic:
                text_style["italic"] = True
                fields.append("italic")
            if url:
                text_style["link"] = {"url": url}
                fields.append("link")
            if fields:
                requests.append(
                    {
                        "updateTextStyle": {
                            "range": {
                                "startIndex": pos,
                                "endIndex": run_end,
                            },
                            "textStyle": text_style,
                            "fields": ",".join(fields),
                        }
                    }
                )
            pos = run_end

    return requests


class DriveClientError(Exception):
    """Raised for unrecoverable Drive client problems (auth, config, API)."""


@dataclass
class DriveFile:
    """A normalized Drive file (metadata only)."""

    id: str
    name: str
    mime_type: str
    size: int  # bytes, 0 if unknown (Google Docs report no size)
    created_time: str = ""
    modified_time: str = ""
    owners: list[str] = field(default_factory=list)  # owner email addresses
    shared: bool = False
    web_view_link: str = ""
    parent_ids: list[str] = field(default_factory=list)
    drive_id: str = ""  # non-empty when the file lives in a Shared Drive

    def short_summary(self) -> str:
        """Human-readable one-liner for the review UI / logs."""
        name = self.name or "(unnamed)"
        return f"{name} ({self.mime_type})"


@dataclass
class DriveFileContent:
    """A Drive file's content after fetching.

    ``content_text`` carries exported text for Google Docs/Sheets/Slides and
    decoded text for text-like binaries. ``content_bytes`` carries raw bytes for
    other binary files. Exactly one of them is normally populated.
    """

    file: DriveFile
    content_text: str = ""
    content_bytes: bytes = b""
    truncated: bool = False


@dataclass
class DriveFolder:
    """A normalized Drive folder."""

    id: str
    name: str
    parents: list[str] = field(default_factory=list)


class DriveClient:
    """Read-only Google Drive client with OAuth2 token caching."""

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
            raise DriveClientError(
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
            raise DriveClientError(
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
                raise DriveClientError(
                    f"Failed to refresh OAuth token: {exc}. "
                    "Re-run with '--oauth-setup' to re-authorize."
                ) from exc
            self._save_token(creds)
            return creds

        raise DriveClientError(
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
        """Build (or reuse) the Drive API service resource."""
        if self._service is None:
            creds = self._load_credentials()
            # cache_discovery=False avoids noisy warnings without a file cache.
            self._service = build(
                "drive", "v3", credentials=creds, cache_discovery=False
            )
            logger.debug("Drive API service initialized")
        return self._service

    def check_connection(self) -> str:
        """Verify the credentials work. Returns the authorized email address."""
        try:
            about = self._get_service().about().get(fields="user").execute()
        except HttpError as exc:
            raise DriveClientError(f"Drive connection check failed: {exc}") from exc
        email = about.get("user", {}).get("emailAddress", "unknown")
        logger.info("Connected to Drive as %s", email)
        return email

    # ------------------------------------------------------------------ #
    # Read operations
    # ------------------------------------------------------------------ #
    def list_files(self, query: str, max_results: int = 20) -> list[DriveFile]:
        """List files matching a Drive search query (the ``q`` parameter).

        See https://developers.google.com/drive/api/guides/search-files for the
        query syntax. Returns normalized ``DriveFile`` metadata.
        """
        max_results = self._clamp_max_results(max_results)
        service = self._get_service()
        try:
            response = (
                service.files()
                .list(
                    q=query or None,
                    pageSize=max_results,
                    fields=f"files({_FILE_FIELDS})",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
        except HttpError as exc:
            raise DriveClientError(f"list_files failed: {exc}") from exc

        files = [self._parse_file(f) for f in response.get("files", [])]
        logger.info("list_files query=%r returned %d files", query, len(files))
        return files

    def get_file_metadata(self, file_id: str) -> DriveFile:
        """Fetch metadata for a single file."""
        if not file_id:
            raise DriveClientError("get_file_metadata requires a non-empty file_id")
        service = self._get_service()
        try:
            raw = (
                service.files()
                .get(fileId=file_id, fields=_FILE_FIELDS, supportsAllDrives=True)
                .execute()
            )
        except HttpError as exc:
            raise DriveClientError(
                f"get_file_metadata({file_id}) failed: {exc}"
            ) from exc
        drive_file = self._parse_file(raw)
        logger.info("get_file_metadata %s: %s", file_id, drive_file.short_summary())
        return drive_file

    def get_file_content(
        self, file_id: str, max_bytes: int = 102400
    ) -> DriveFileContent:
        """Fetch a file's content, capped at ``max_bytes``.

        Google Workspace documents are exported as text (Docs/Slides as
        text/plain, Sheets as CSV). Other files are downloaded as raw bytes. If
        the content exceeds ``max_bytes`` it is truncated and ``truncated`` is
        set to True.
        """
        if not file_id:
            raise DriveClientError("get_file_content requires a non-empty file_id")
        if max_bytes <= 0:
            max_bytes = 102400

        metadata = self.get_file_metadata(file_id)
        service = self._get_service()

        export_mime = _GOOGLE_DOC_EXPORTS.get(metadata.mime_type)
        try:
            if export_mime is not None:
                request = service.files().export_media(
                    fileId=file_id, mimeType=export_mime
                )
            else:
                request = service.files().get_media(
                    fileId=file_id, supportsAllDrives=True
                )
            data = self._download(request, max_bytes)
        except HttpError as exc:
            raise DriveClientError(
                f"get_file_content({file_id}) failed: {exc}"
            ) from exc

        truncated = len(data) > max_bytes
        if truncated:
            data = data[:max_bytes]

        # Workspace exports are always text; for downloads, only treat clearly
        # text-like MIME types as text, otherwise keep raw bytes.
        is_text = export_mime is not None or metadata.mime_type.startswith("text/")
        content = DriveFileContent(file=metadata, truncated=truncated)
        if is_text:
            content.content_text = data.decode("utf-8", errors="replace")
        else:
            content.content_bytes = data

        logger.info(
            "get_file_content %s: %d bytes (truncated=%s, text=%s)",
            file_id,
            len(data),
            truncated,
            is_text,
        )
        return content

    def download_file(
        self, file_id: str, destination_dir: str = ""
    ) -> dict[str, Any]:
        """Download a file to a local directory and return the saved path.

        If ``destination_dir`` is empty, defaults to ``~/Downloads``.
        Google Workspace documents are exported as text (Docs/Slides → .txt,
        Sheets → .csv). Binary files are saved with their original extension.
        Returns a dict with ``path``, ``name``, ``size_bytes``, and
        ``truncated`` (always False for full downloads).
        """
        if not file_id:
            raise DriveClientError("download_file requires a non-empty file_id")

        dest = os.path.expanduser(destination_dir.strip() or "~/Downloads")
        os.makedirs(dest, exist_ok=True)

        metadata = self.get_file_metadata(file_id)
        export_mime = _GOOGLE_DOC_EXPORTS.get(metadata.mime_type)

        # Choose filename and extension
        name = metadata.name or file_id
        if export_mime == "text/plain" and not name.endswith(".txt"):
            name = name + ".txt"
        elif export_mime == "text/csv" and not name.endswith(".csv"):
            name = name + ".csv"

        dest_path = os.path.join(dest, name)

        try:
            creds = self._load_credentials()
            session = AuthorizedSession(creds)
            if export_mime is not None:
                url = (
                    "https://www.googleapis.com/drive/v3/files/"
                    f"{file_id}/export?mimeType={urllib.parse.quote(export_mime)}"
                )
            else:
                url = (
                    f"https://www.googleapis.com/drive/v3/files/{file_id}"
                    "?alt=media&supportsAllDrives=true"
                )
            with session.get(url, stream=True) as resp:
                resp.raise_for_status()
                with open(dest_path, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=8 * 1024 * 1024):
                        if chunk:
                            fh.write(chunk)
        except Exception as exc:
            raise DriveClientError(
                f"download_file({file_id}) failed: {exc}"
            ) from exc

        size = os.path.getsize(dest_path)
        logger.info("download_file %s → %s (%d bytes)", file_id, dest_path, size)
        return {
            "path": dest_path,
            "name": name,
            "size_bytes": size,
            "truncated": False,
        }

    def list_folder(self, folder_id: str, max_results: int = 50) -> list[DriveFile]:
        """List the direct children of a folder."""
        if not folder_id:
            raise DriveClientError("list_folder requires a non-empty folder_id")
        max_results = self._clamp_max_results(max_results)
        query = f"'{folder_id}' in parents and trashed = false"
        service = self._get_service()
        try:
            response = (
                service.files()
                .list(
                    q=query,
                    pageSize=max_results,
                    fields=f"files({_FILE_FIELDS})",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
        except HttpError as exc:
            raise DriveClientError(f"list_folder({folder_id}) failed: {exc}") from exc

        files = [self._parse_file(f) for f in response.get("files", [])]
        logger.info("list_folder %s returned %d children", folder_id, len(files))
        return files

    # ------------------------------------------------------------------ #
    # Write operations
    # ------------------------------------------------------------------ #
    def create_blank_file(
        self, name: str, mime_type: str, parent_folder_id: str = ""
    ) -> dict:
        """Create a new blank file and return its metadata dict."""
        body: dict = {"name": name, "mimeType": mime_type}
        if parent_folder_id:
            body["parents"] = [parent_folder_id]
        service = self._get_service()
        try:
            result = (
                service.files()
                .create(body=body, fields=_FILE_FIELDS, supportsAllDrives=True)
                .execute()
            )
        except HttpError as exc:
            raise DriveClientError(f"create_blank_file failed: {exc}") from exc
        logger.info("create_blank_file: id=%s name=%s", result.get("id"), name)
        return {"id": result.get("id", ""), "name": name, "mime_type": mime_type}

    def write_file_content(self, file_id: str, content: str) -> dict:
        """Write (overwrite) the content of a file."""
        import io
        from googleapiclient.http import MediaIoBaseUpload

        if not file_id:
            raise DriveClientError("write_file_content requires a non-empty file_id")
        service = self._get_service()
        media = MediaIoBaseUpload(
            io.BytesIO(content.encode("utf-8")), mimetype="text/plain"
        )
        try:
            result = (
                service.files()
                .update(
                    fileId=file_id,
                    media_body=media,
                    fields="id,name,modifiedTime",
                    supportsAllDrives=True,
                )
                .execute()
            )
        except HttpError as exc:
            raise DriveClientError(f"write_file_content({file_id}) failed: {exc}") from exc
        logger.info("write_file_content: file_id=%s", file_id)
        return {"file_id": result.get("id", file_id), "modified_time": result.get("modifiedTime", "")}

    def write_doc_rich_content(self, file_id: str, markdown: str) -> dict:
        """Write Markdown to a Google Doc with rich formatting via the Docs API.

        Supports: headings (# / ## / ### / ####), **bold**, *italic*,
        ***bold-italic***, [link](url), unordered lists (- item),
        numbered lists (1. item), and plain paragraphs.
        Clears existing document content before writing.
        Requires the ``drive`` or ``documents`` OAuth scope (already granted).
        """
        if not file_id:
            raise DriveClientError(
                "write_doc_rich_content requires a non-empty file_id"
            )
        creds = self._load_credentials()
        docs_service = build(
            "docs", "v1", credentials=creds, cache_discovery=False
        )
        try:
            doc = docs_service.documents().get(documentId=file_id).execute()
        except HttpError as exc:
            raise DriveClientError(
                f"write_doc_rich_content get({file_id}) failed: {exc}"
            ) from exc

        # Find end index so we can delete existing content
        end_index = 1
        for element in doc.get("body", {}).get("content", []):
            if "endIndex" in element:
                end_index = element["endIndex"]

        requests: list[dict] = []
        if end_index > 2:
            requests.append(
                {
                    "deleteContentRange": {
                        "range": {"startIndex": 1, "endIndex": end_index - 1}
                    }
                }
            )
        requests.extend(_markdown_to_docs_requests(markdown))

        if requests:
            try:
                docs_service.documents().batchUpdate(
                    documentId=file_id, body={"requests": requests}
                ).execute()
            except HttpError as exc:
                raise DriveClientError(
                    f"write_doc_rich_content batchUpdate({file_id}) failed: {exc}"
                ) from exc

        logger.info("write_doc_rich_content: file_id=%s", file_id)
        return {"file_id": file_id}

    def move_file(self, file_id: str, destination_folder_id: str) -> dict:
        """Move a file to a different folder."""
        if not file_id or not destination_folder_id:
            raise DriveClientError("move_file requires file_id and destination_folder_id")
        service = self._get_service()
        # Get current parents
        try:
            file_meta = service.files().get(
                fileId=file_id, fields="parents", supportsAllDrives=True
            ).execute()
        except HttpError as exc:
            raise DriveClientError(f"move_file get_parents({file_id}) failed: {exc}") from exc
        current_parents = ",".join(file_meta.get("parents", []))
        try:
            result = (
                service.files()
                .update(
                    fileId=file_id,
                    addParents=destination_folder_id,
                    removeParents=current_parents,
                    fields="id,parents",
                    supportsAllDrives=True,
                )
                .execute()
            )
        except HttpError as exc:
            raise DriveClientError(f"move_file({file_id}) failed: {exc}") from exc
        logger.info("move_file: file_id=%s dest=%s", file_id, destination_folder_id)
        return {"file_id": file_id, "new_parent": destination_folder_id}

    def add_comment(self, file_id: str, comment: str) -> dict:
        """Add a comment to a file."""
        if not file_id:
            raise DriveClientError("add_comment requires a non-empty file_id")
        service = self._get_service()
        try:
            result = (
                service.comments()
                .create(fileId=file_id, body={"content": comment}, fields="id,content")
                .execute()
            )
        except HttpError as exc:
            raise DriveClientError(f"add_comment({file_id}) failed: {exc}") from exc
        logger.info("add_comment: file_id=%s comment_id=%s", file_id, result.get("id"))
        return {"file_id": file_id, "comment_id": result.get("id", ""), "content": comment}

    def list_shared_drives(self, max_results: int = 50) -> list[dict]:
        """Return a list of Shared Drives the authorized user can access."""
        max_results = self._clamp_max_results(max_results)
        service = self._get_service()
        try:
            response = (
                service.drives()
                .list(pageSize=max_results, fields="drives(id,name,kind)")
                .execute()
            )
        except HttpError as exc:
            raise DriveClientError(f"list_shared_drives failed: {exc}") from exc
        drives = response.get("drives", [])
        logger.info("list_shared_drives returned %d drives", len(drives))
        return [{"id": d.get("id", ""), "name": d.get("name", "")} for d in drives]

    # ------------------------------------------------------------------ #
    # Parsing helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _clamp_max_results(max_results: int) -> int:
        """Defensive bounds on caller-supplied result counts."""
        try:
            value = int(max_results)
        except (TypeError, ValueError):
            value = 20
        return max(1, min(value, 1000))

    @staticmethod
    def _download(request, max_bytes: int) -> bytes:
        """Stream a media request, stopping once we have more than max_bytes.

        We read one extra byte's worth of chunks beyond the cap so the caller
        can reliably detect truncation.
        """
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request, chunksize=max(8192, min(max_bytes, 1048576)))
        done = False
        while not done:
            _status, done = downloader.next_chunk()
            if buffer.tell() > max_bytes:
                break
        return buffer.getvalue()

    @staticmethod
    def _parse_file(raw: dict[str, Any]) -> DriveFile:
        owners = [
            o.get("emailAddress", "")
            for o in raw.get("owners", []) or []
            if o.get("emailAddress")
        ]
        try:
            size = int(raw.get("size", 0) or 0)
        except (TypeError, ValueError):
            size = 0
        return DriveFile(
            id=raw.get("id", ""),
            name=raw.get("name", ""),
            mime_type=raw.get("mimeType", ""),
            size=size,
            created_time=raw.get("createdTime", ""),
            modified_time=raw.get("modifiedTime", ""),
            owners=owners,
            shared=bool(raw.get("shared", False)),
            web_view_link=raw.get("webViewLink", ""),
            parent_ids=list(raw.get("parents", []) or []),
            drive_id=raw.get("driveId", ""),
        )
