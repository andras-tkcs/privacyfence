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

import base64
import io
import logging
import mimetypes
import os
import re as _re
import threading
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


# ------------------------------------------------------------------ #
# Sheets API helpers
# ------------------------------------------------------------------ #

def _col_letters_to_index(letters: str) -> int:
    """Convert an A1 column reference ('A', 'Z', 'AA', ...) to a 0-based index."""
    idx = 0
    for ch in letters.upper():
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def _parse_a1_range(range_a1: str) -> dict:
    """Parse a fully-bounded A1 range ('A1:C10') into a 0-indexed GridRange dict
    (without sheetId, which the caller merges in).

    Only the ``<col><row>:<col><row>`` form is supported - no whole-row,
    whole-column, or sheet-name-prefixed references. The caller specifies the
    sheet separately via sheet_id, so no sheet-name prefix is expected here.
    """
    m = _re.match(r"^([A-Za-z]+)(\d+):([A-Za-z]+)(\d+)$", range_a1.strip())
    if not m:
        raise DriveClientError(
            f"Unsupported range syntax {range_a1!r}; use a fully-bounded "
            "range like 'A1:C10' (no sheet-name prefix, no open-ended rows/columns)."
        )
    c1, r1, c2, r2 = m.groups()
    col1, col2 = _col_letters_to_index(c1), _col_letters_to_index(c2)
    row1, row2 = int(r1) - 1, int(r2) - 1
    return {
        "startRowIndex": min(row1, row2),
        "endRowIndex": max(row1, row2) + 1,
        "startColumnIndex": min(col1, col2),
        "endColumnIndex": max(col1, col2) + 1,
    }


def _hex_to_rgb_dict(hex_color: str) -> dict:
    """Convert '#rrggbb' (or 'rrggbb') to a Sheets API Color dict (0..1 floats)."""
    value = hex_color.strip().lstrip("#")
    if len(value) != 6:
        raise DriveClientError(f"Invalid hex color {hex_color!r}; expected '#rrggbb'")
    try:
        r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError as exc:
        raise DriveClientError(f"Invalid hex color {hex_color!r}: {exc}") from exc
    return {"red": r / 255, "green": g / 255, "blue": b / 255}


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
            raise DriveClientError(
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
        """Build (or reuse) the Drive API service resource for this thread."""
        service = getattr(self._local, "service", None)
        if service is None:
            creds = self._load_credentials()
            # cache_discovery=False avoids noisy warnings without a file cache.
            service = build(
                "drive", "v3", credentials=creds, cache_discovery=False
            )
            self._local.service = service
            logger.debug("Drive API service initialized for thread %s", threading.current_thread().name)
        return service

    def get_credentials(self) -> Credentials:
        """Expose the cached OAuth credentials for sibling API clients (Sheets)
        that reuse the Drive OAuth grant instead of requesting their own scope."""
        return self._load_credentials()

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

    def upload_file(
        self,
        local_path: str = "",
        name: str = "",
        parent_folder_id: str = "",
        content_base64: str = "",
    ) -> dict:
        """Upload a file as a new Drive file, either from disk or inline bytes.

        Exactly one of ``local_path`` or ``content_base64`` must be given.
        Both paths use a resumable Google API media upload instead of the
        ``write_file_content`` path, which always encodes as UTF-8 text and
        uploads with a hardcoded ``text/plain`` media type — arbitrary binary
        files (PDFs, images, …) can't round-trip through that.

        ``local_path`` reads straight from disk via ``MediaFileUpload`` and is
        preferred when the file already lives on the same machine as
        PrivacyFence. ``content_base64`` lets a caller that only has the bytes
        in hand (no shared filesystem) hand them over directly — PrivacyFence
        decodes the base64 itself via ``MediaIoBaseUpload``.
        """
        from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload

        if bool(local_path.strip()) == bool(content_base64.strip()):
            raise DriveClientError(
                "upload_file: provide exactly one of local_path or content_base64"
            )

        if local_path.strip():
            path = os.path.expanduser(local_path.strip())
            if not os.path.isfile(path):
                raise DriveClientError(f"upload_file: no such file: {local_path!r}")
            resolved_name = name.strip() or os.path.basename(path)
            mime_type = mimetypes.guess_type(resolved_name)[0] or "application/octet-stream"
            media = MediaFileUpload(path, mimetype=mime_type, resumable=True)
            size_bytes = os.path.getsize(path)
        else:
            resolved_name = name.strip()
            if not resolved_name:
                raise DriveClientError("upload_file: name is required with content_base64")
            try:
                data = base64.b64decode(content_base64, validate=True)
            except (base64.binascii.Error, ValueError) as exc:
                raise DriveClientError(f"upload_file: invalid content_base64: {exc}") from exc
            mime_type = mimetypes.guess_type(resolved_name)[0] or "application/octet-stream"
            media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=True)
            size_bytes = len(data)

        body: dict = {"name": resolved_name}
        if parent_folder_id:
            body["parents"] = [parent_folder_id]

        service = self._get_service()
        try:
            result = (
                service.files()
                .create(body=body, media_body=media, fields=_FILE_FIELDS, supportsAllDrives=True)
                .execute()
            )
        except HttpError as exc:
            raise DriveClientError(f"upload_file({resolved_name}) failed: {exc}") from exc

        parsed = self._parse_file(result)
        logger.info("upload_file: id=%s name=%s mime=%s", parsed.id, parsed.name, mime_type)
        return {
            "id": parsed.id,
            "name": parsed.name,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
        }

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
    # Sheets operations
    #
    # These reuse the Drive OAuth grant (the Sheets API v4 accepts the
    # ``drive`` scope) the same way write_doc_rich_content() above reuses it
    # for the Docs API - no separate consent screen or token file.
    # ------------------------------------------------------------------ #
    def _get_sheets_service(self):
        service = getattr(self._local, "sheets_service", None)
        if service is None:
            creds = self._load_credentials()
            service = build(
                "sheets", "v4", credentials=creds, cache_discovery=False
            )
            self._local.sheets_service = service
            logger.debug("Sheets API service initialized for thread %s", threading.current_thread().name)
        return service

    def create_spreadsheet(
        self, name: str, sheet_titles: list[str] | None = None, parent_folder_id: str = ""
    ) -> dict:
        """Create a new spreadsheet, optionally with named tabs (defaults to one
        tab named 'Sheet1' if ``sheet_titles`` is empty). Returns id/name/web link.

        The Sheets API always creates in "My Drive" root; if a parent folder is
        given we move the resulting file there via the Drive API afterward.
        """
        if not name.strip():
            raise DriveClientError("create_spreadsheet requires a non-empty name")
        body: dict = {"properties": {"title": name}}
        if sheet_titles:
            body["sheets"] = [{"properties": {"title": t}} for t in sheet_titles]
        service = self._get_sheets_service()
        try:
            result = service.spreadsheets().create(
                body=body, fields="spreadsheetId,properties.title,spreadsheetUrl"
            ).execute()
        except HttpError as exc:
            raise DriveClientError(f"create_spreadsheet({name}) failed: {exc}") from exc

        spreadsheet_id = result.get("spreadsheetId", "")
        if parent_folder_id:
            self.move_file(spreadsheet_id, parent_folder_id)
        logger.info("create_spreadsheet: id=%s name=%s", spreadsheet_id, name)
        return {
            "id": spreadsheet_id,
            "name": result.get("properties", {}).get("title", name),
            "web_view_link": result.get("spreadsheetUrl", ""),
        }

    def list_sheets(self, spreadsheet_id: str) -> list[dict]:
        """List the tabs (sheets) within a spreadsheet."""
        if not spreadsheet_id:
            raise DriveClientError("list_sheets requires a non-empty spreadsheet_id")
        service = self._get_sheets_service()
        try:
            result = service.spreadsheets().get(
                spreadsheetId=spreadsheet_id, fields="sheets.properties"
            ).execute()
        except HttpError as exc:
            raise DriveClientError(f"list_sheets({spreadsheet_id}) failed: {exc}") from exc
        sheets = []
        for s in result.get("sheets", []):
            props = s.get("properties", {})
            grid = props.get("gridProperties", {})
            sheets.append({
                "sheet_id": props.get("sheetId"),
                "title": props.get("title", ""),
                "index": props.get("index"),
                "row_count": grid.get("rowCount"),
                "column_count": grid.get("columnCount"),
                "hidden": bool(props.get("hidden", False)),
            })
        logger.info("list_sheets %s returned %d tab(s)", spreadsheet_id, len(sheets))
        return sheets

    def get_sheet_values(self, spreadsheet_id: str, range_a1: str) -> list[list]:
        """Read a range of cell values (A1 notation, e.g. 'Sheet1!A1:C10')."""
        if not spreadsheet_id or not range_a1:
            raise DriveClientError("get_sheet_values requires spreadsheet_id and range")
        service = self._get_sheets_service()
        try:
            result = service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id, range=range_a1
            ).execute()
        except HttpError as exc:
            raise DriveClientError(
                f"get_sheet_values({spreadsheet_id}, {range_a1}) failed: {exc}"
            ) from exc
        values = result.get("values", [])
        logger.info("get_sheet_values %s %s: %d row(s)", spreadsheet_id, range_a1, len(values))
        return values

    def write_sheet_values(
        self, spreadsheet_id: str, range_a1: str, values: list[list], value_input_option: str = "USER_ENTERED"
    ) -> dict:
        """Write a 2D array of values into a range (A1 notation, e.g.
        'Sheet1!A1:C10'). With the default ``USER_ENTERED`` option, cell strings
        starting with '=' are evaluated as formulas, exactly as if typed into
        the Sheets UI - there is no separate "set formula" operation.
        """
        if not spreadsheet_id or not range_a1:
            raise DriveClientError("write_sheet_values requires spreadsheet_id and range")
        service = self._get_sheets_service()
        try:
            result = service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=range_a1,
                valueInputOption=value_input_option,
                body={"values": values},
            ).execute()
        except HttpError as exc:
            raise DriveClientError(
                f"write_sheet_values({spreadsheet_id}, {range_a1}) failed: {exc}"
            ) from exc
        logger.info(
            "write_sheet_values %s %s: updated %s cell(s)",
            spreadsheet_id, range_a1, result.get("updatedCells", 0),
        )
        return {
            "spreadsheet_id": spreadsheet_id,
            "updated_range": result.get("updatedRange", range_a1),
            "updated_cells": result.get("updatedCells", 0),
        }

    def add_sheet(self, spreadsheet_id: str, title: str, rows: int = 1000, cols: int = 26) -> dict:
        """Add a new tab to an existing spreadsheet."""
        if not spreadsheet_id or not title.strip():
            raise DriveClientError("add_sheet requires spreadsheet_id and a non-empty title")
        service = self._get_sheets_service()
        request = {
            "addSheet": {
                "properties": {
                    "title": title,
                    "gridProperties": {"rowCount": max(1, rows), "columnCount": max(1, cols)},
                }
            }
        }
        try:
            result = service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={"requests": [request]}
            ).execute()
        except HttpError as exc:
            raise DriveClientError(f"add_sheet({spreadsheet_id}, {title}) failed: {exc}") from exc
        props = result["replies"][0]["addSheet"]["properties"]
        logger.info("add_sheet: spreadsheet=%s sheet_id=%s title=%s", spreadsheet_id, props.get("sheetId"), title)
        return {"sheet_id": props.get("sheetId"), "title": props.get("title", title), "index": props.get("index")}

    def rename_sheet(self, spreadsheet_id: str, sheet_id: int, new_title: str) -> dict:
        """Rename an existing tab. Also the sanctioned way to mark a tab for
        deletion (rename it, e.g. to 'TO BE DELETED - <original title>') since
        this client intentionally has no delete-sheet operation."""
        if not spreadsheet_id or not new_title.strip():
            raise DriveClientError("rename_sheet requires spreadsheet_id and a non-empty new_title")
        service = self._get_sheets_service()
        request = {
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "title": new_title},
                "fields": "title",
            }
        }
        try:
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={"requests": [request]}
            ).execute()
        except HttpError as exc:
            raise DriveClientError(
                f"rename_sheet({spreadsheet_id}, {sheet_id}) failed: {exc}"
            ) from exc
        logger.info("rename_sheet: spreadsheet=%s sheet_id=%s new_title=%s", spreadsheet_id, sheet_id, new_title)
        return {"spreadsheet_id": spreadsheet_id, "sheet_id": sheet_id, "title": new_title}

    def format_sheet_range(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        range_a1: str,
        bold: str = "",
        italic: str = "",
        background_color: str = "",
        text_color: str = "",
        number_format: str = "",
        horizontal_alignment: str = "",
        freeze_rows: int = -1,
        freeze_cols: int = -1,
        column_width: int = -1,
        merge_type: str = "KEEP",
    ) -> dict:
        """Apply formatting to a range. Every parameter is opt-in: its "unset"
        value (empty string / -1 / 'KEEP') means "leave that aspect unchanged" -
        a format call only ever touches the aspects it's explicitly given, so
        e.g. changing a background color never silently clears bold text or
        un-merges cells set by an earlier call.

        ``range_a1`` is plain A1 notation scoped to ``sheet_id`` (e.g. 'A1:C10',
        no sheet-name prefix - only fully-bounded ranges are supported).
        ``merge_type`` is one of KEEP / NONE (unmerge) / MERGE_ALL /
        MERGE_COLUMNS / MERGE_ROWS.
        """
        if not spreadsheet_id:
            raise DriveClientError("format_sheet_range requires a non-empty spreadsheet_id")
        grid_range = {"sheetId": sheet_id, **_parse_a1_range(range_a1)}

        requests: list[dict] = []

        cell_format: dict = {}
        fields: list[str] = []
        text_style: dict = {}
        text_fields: list[str] = []
        if bold:
            text_style["bold"] = bold.strip().lower() == "true"
            text_fields.append("bold")
        if italic:
            text_style["italic"] = italic.strip().lower() == "true"
            text_fields.append("italic")
        if text_color:
            text_style["foregroundColor"] = _hex_to_rgb_dict(text_color)
            text_fields.append("foregroundColor")
        if text_fields:
            cell_format["textFormat"] = text_style
            fields.append("userEnteredFormat.textFormat(" + ",".join(text_fields) + ")")
        if background_color:
            cell_format["backgroundColor"] = _hex_to_rgb_dict(background_color)
            fields.append("userEnteredFormat.backgroundColor")
        if number_format:
            cell_format["numberFormat"] = {"type": "NUMBER", "pattern": number_format}
            fields.append("userEnteredFormat.numberFormat")
        if horizontal_alignment:
            cell_format["horizontalAlignment"] = horizontal_alignment.upper()
            fields.append("userEnteredFormat.horizontalAlignment")
        if fields:
            requests.append({
                "repeatCell": {
                    "range": grid_range,
                    "cell": {"userEnteredFormat": cell_format},
                    "fields": ",".join(fields),
                }
            })

        if column_width >= 0:
            requests.append({
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": grid_range["startColumnIndex"],
                        "endIndex": grid_range["endColumnIndex"],
                    },
                    "properties": {"pixelSize": column_width},
                    "fields": "pixelSize",
                }
            })

        if freeze_rows >= 0 or freeze_cols >= 0:
            grid_properties: dict = {}
            sheet_fields: list[str] = []
            if freeze_rows >= 0:
                grid_properties["frozenRowCount"] = freeze_rows
                sheet_fields.append("gridProperties.frozenRowCount")
            if freeze_cols >= 0:
                grid_properties["frozenColumnCount"] = freeze_cols
                sheet_fields.append("gridProperties.frozenColumnCount")
            requests.append({
                "updateSheetProperties": {
                    "properties": {"sheetId": sheet_id, "gridProperties": grid_properties},
                    "fields": ",".join(sheet_fields),
                }
            })

        merge_type = merge_type.upper()
        if merge_type == "NONE":
            requests.append({"unmergeCells": {"range": grid_range}})
        elif merge_type in ("MERGE_ALL", "MERGE_COLUMNS", "MERGE_ROWS"):
            requests.append({"mergeCells": {"range": grid_range, "mergeType": merge_type}})
        elif merge_type != "KEEP":
            raise DriveClientError(
                f"format_sheet_range: invalid merge_type {merge_type!r}; "
                "use KEEP, NONE, MERGE_ALL, MERGE_COLUMNS, or MERGE_ROWS"
            )

        if not requests:
            return {"spreadsheet_id": spreadsheet_id, "sheet_id": sheet_id, "requests_applied": 0}

        service = self._get_sheets_service()
        try:
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={"requests": requests}
            ).execute()
        except HttpError as exc:
            raise DriveClientError(
                f"format_sheet_range({spreadsheet_id}, {range_a1}) failed: {exc}"
            ) from exc
        logger.info(
            "format_sheet_range: spreadsheet=%s sheet_id=%s range=%s requests=%d",
            spreadsheet_id, sheet_id, range_a1, len(requests),
        )
        return {"spreadsheet_id": spreadsheet_id, "sheet_id": sheet_id, "requests_applied": len(requests)}

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
