"""Google Drive connector."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..drive_client import DriveClient, DriveClientError, _parse_a1_range
from ..gate import gated_call

logger = logging.getLogger(__name__)


def _parse_json_str_list(value: str) -> list[str] | None:
    """Parse a JSON array-of-strings tool argument, or None if empty/invalid."""
    if not value or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) and all(isinstance(v, str) for v in parsed) else None


def _parse_json_2d_list(value: str) -> list[list] | None:
    """Parse a JSON array-of-arrays tool argument, or None if invalid."""
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) else None


def _format_sheet_rows(rows: list[list], limit: int = 50) -> str:
    """Render 2D sheet cell data as comma-joined lines, capped at `limit` rows."""
    shown = "\n".join(", ".join(str(cell) for cell in row) for row in rows[:limit])
    if len(rows) > limit:
        shown += f"\n… and {len(rows) - limit} more row(s)"
    return shown


class DriveConnector(Connector):
    def __init__(self, client: DriveClient) -> None:
        self._drive = client
        self.my_email: str = ""
        self.session_created_ids: set[str] = set()

    @property
    def name(self) -> str:
        return "drive"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="drive_list_files",
                description=(
                    "Search Google Drive and return matching file metadata "
                    "(id, name, mime_type, owners, sharing status). Auto-approved."
                ),
                params=[
                    ToolParam("query", "str"),
                    ToolParam("max_results", "int", required=False, default=20),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="drive_get_file_metadata",
                description=(
                    "Fetch metadata for a single Drive file by id "
                    "(name, owners, times, sharing status). Auto-approved."
                ),
                params=[ToolParam("file_id", "str")],
                read_only=True,
            ),
            ToolSpec(
                name="drive_list_folder",
                description="List the direct children of a Drive folder by id. Auto-approved.",
                params=[
                    ToolParam("folder_id", "str"),
                    ToolParam("max_results", "int", required=False, default=50),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="drive_create_blank_file",
                description="Create a new blank Drive file. Auto-approved.",
                params=[
                    ToolParam("name", "str"),
                    ToolParam("mime_type", "str"),
                    ToolParam("parent_folder_id", "str", required=False, default=""),
                ],
            ),
            ToolSpec(
                name="drive_get_file_content",
                description=(
                    "Fetch the content of a Drive file by id. Requires user approval."
                ),
                params=[ToolParam("file_id", "str")],
                read_only=True,
            ),
            ToolSpec(
                name="drive_write_file_content",
                description="Write content to an existing Drive file. Requires user approval.",
                params=[
                    ToolParam("file_id", "str"),
                    ToolParam("content", "str"),
                ],
            ),
            ToolSpec(
                name="drive_upload_file",
                description=(
                    "Upload any file (e.g. a PDF or image) to Drive as a new file — "
                    "use this instead of drive_write_file_content for any binary "
                    "file, since that tool only writes UTF-8 text. Provide exactly "
                    "one of local_path (read directly from disk by path — prefer "
                    "this when the file is on the same machine as PrivacyFence) or "
                    "content_base64 (base64-encoded file bytes, decoded by "
                    "PrivacyFence itself — use this when you only have the file's "
                    "bytes and not a local path; 'name' is then required). "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("local_path", "str", required=False, default=""),
                    ToolParam("content_base64", "str", required=False, default=""),
                    ToolParam("name", "str", required=False, default=""),
                    ToolParam("parent_folder_id", "str", required=False, default=""),
                ],
            ),
            ToolSpec(
                name="drive_move_file",
                description="Move a Drive file to a different folder. Requires user approval.",
                params=[
                    ToolParam("file_id", "str"),
                    ToolParam("destination_folder_id", "str"),
                ],
            ),
            ToolSpec(
                name="drive_add_comment",
                description="Add a comment to a Drive file. Requires user approval.",
                params=[
                    ToolParam("file_id", "str"),
                    ToolParam("comment", "str"),
                ],
            ),
            ToolSpec(
                name="drive_list_shared_drives",
                description=(
                    "List all Google Workspace Shared Drives the user can access "
                    "(returns id and name for each). Auto-approved."
                ),
                params=[
                    ToolParam("max_results", "int", required=False, default=50),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="drive_write_doc_content",
                description=(
                    "Write Markdown content to a Google Doc with rich formatting "
                    "(headings, bold, italic, links, bullet and numbered lists). "
                    "Clears the existing document content before writing. "
                    "Use this instead of drive_write_file_content when the target "
                    "is a Google Doc and you want formatted output. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("file_id", "str"),
                    ToolParam("markdown", "str"),
                ],
            ),
            ToolSpec(
                name="drive_download_file",
                description=(
                    "Download a Drive file to a local directory and return the saved "
                    "file path. Use this for large files (e.g. >100 KB) that cannot "
                    "be returned inline. Google Workspace documents are exported as "
                    "text/CSV. destination_dir defaults to ~/Downloads. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("file_id", "str"),
                    ToolParam(
                        "destination_dir",
                        "str",
                        required=False,
                        default="",
                    ),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="drive_sheets_create",
                description=(
                    "Create a new Google Sheets spreadsheet, optionally with "
                    "named tabs. Auto-approved."
                ),
                params=[
                    ToolParam("name", "str"),
                    ToolParam(
                        "sheet_titles", "str", required=False, default="",
                        description=(
                            'JSON array of tab names, e.g. ["Q1","Q2"]. '
                            "Defaults to a single 'Sheet1' tab if omitted."
                        ),
                    ),
                    ToolParam("parent_folder_id", "str", required=False, default=""),
                ],
            ),
            ToolSpec(
                name="drive_sheets_get_metadata",
                description=(
                    "List the tabs in a spreadsheet (id, title, index, row/column "
                    "count). Auto-approved."
                ),
                params=[ToolParam("spreadsheet_id", "str")],
                read_only=True,
            ),
            ToolSpec(
                name="drive_sheets_get_values",
                description="Read a range of cell values from a spreadsheet. Requires user approval.",
                params=[
                    ToolParam("spreadsheet_id", "str"),
                    ToolParam(
                        "range_a1", "str",
                        description="A1 notation range, e.g. 'Sheet1!A1:C10'",
                    ),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="drive_sheets_write_range",
                description=(
                    "Write values and/or formulas into a range of an existing "
                    "spreadsheet. A cell string starting with '=' is evaluated as "
                    "a formula, exactly as if typed into the Sheets UI — there is "
                    "no separate tool for formulas. Writing an empty row/column "
                    "clears those cells. Requires user approval."
                ),
                params=[
                    ToolParam("spreadsheet_id", "str"),
                    ToolParam(
                        "range_a1", "str",
                        description="A1 notation range, e.g. 'Sheet1!A1:C10'",
                    ),
                    ToolParam(
                        "values", "str",
                        description=(
                            'JSON 2D array of rows, e.g. [["Name","Total"],'
                            '["Alice","=B2*2"]]'
                        ),
                    ),
                ],
            ),
            ToolSpec(
                name="drive_sheets_add_sheet",
                description="Add a new tab to an existing spreadsheet. Requires user approval.",
                params=[
                    ToolParam("spreadsheet_id", "str"),
                    ToolParam("title", "str"),
                    ToolParam("rows", "int", required=False, default=1000),
                    ToolParam("cols", "int", required=False, default=26),
                ],
            ),
            ToolSpec(
                name="drive_sheets_rename_sheet",
                description=(
                    "Rename an existing tab in a spreadsheet. There is no delete-sheet "
                    "tool — to mark a tab for removal, rename it (e.g. to "
                    "'TO BE DELETED - <original title>') and the user can delete it "
                    "by hand in the Sheets UI. Requires user approval."
                ),
                params=[
                    ToolParam("spreadsheet_id", "str"),
                    ToolParam("sheet_id", "int", description="Numeric tab id, from drive_sheets_get_metadata"),
                    ToolParam("new_title", "str"),
                ],
            ),
            ToolSpec(
                name="drive_sheets_format_range",
                description=(
                    "Apply formatting to a range in a spreadsheet: bold/italic, "
                    "colors, number format, alignment, column width, frozen rows/"
                    "columns, and merged cells. Every parameter is opt-in — its "
                    "default means 'leave that aspect unchanged', so a call that "
                    "only sets a background color never touches unrelated "
                    "formatting already on the range. Requires user approval."
                ),
                params=[
                    ToolParam("spreadsheet_id", "str"),
                    ToolParam("sheet_id", "int", description="Numeric tab id, from drive_sheets_get_metadata"),
                    ToolParam(
                        "range_a1", "str",
                        description=(
                            "Plain A1 range scoped to sheet_id, e.g. 'A1:C10' "
                            "(no sheet-name prefix, must be fully bounded)"
                        ),
                    ),
                    ToolParam("bold", "str", required=False, default="",
                              description="'true' or 'false'; omit to leave unchanged"),
                    ToolParam("italic", "str", required=False, default="",
                              description="'true' or 'false'; omit to leave unchanged"),
                    ToolParam("background_color", "str", required=False, default="",
                              description="hex color e.g. '#ffcc00'; omit to leave unchanged"),
                    ToolParam("text_color", "str", required=False, default="",
                              description="hex color e.g. '#ffffff'; omit to leave unchanged"),
                    ToolParam("number_format", "str", required=False, default="",
                              description="Sheets number-format pattern, e.g. '0.00%', '$#,##0.00', 'yyyy-mm-dd'; omit to leave unchanged"),
                    ToolParam("horizontal_alignment", "str", required=False, default="",
                              description="'LEFT' / 'CENTER' / 'RIGHT'; omit to leave unchanged"),
                    ToolParam("freeze_rows", "int", required=False, default=-1,
                              description="Number of rows to freeze at the top (0 unfreezes); omit (-1) to leave unchanged"),
                    ToolParam("freeze_cols", "int", required=False, default=-1,
                              description="Number of columns to freeze at the left (0 unfreezes); omit (-1) to leave unchanged"),
                    ToolParam("column_width", "int", required=False, default=-1,
                              description="Pixel width for the range's columns; omit (-1) to leave unchanged"),
                    ToolParam("merge_type", "str", required=False, default="KEEP",
                              description="KEEP (default) / NONE (unmerge) / MERGE_ALL / MERGE_COLUMNS / MERGE_ROWS"),
                ],
            ),
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "drive_list_files":
            return await self._list_files(**args)
        if tool == "drive_get_file_metadata":
            return await self._get_file_metadata(**args)
        if tool == "drive_get_file_content":
            return await self._get_file_content(**args)
        if tool == "drive_list_folder":
            return await self._list_folder(**args)
        if tool == "drive_create_blank_file":
            return await self._create_blank_file(**args)
        if tool == "drive_write_file_content":
            return await self._write_file_content(**args)
        if tool == "drive_upload_file":
            return await self._upload_file(**args)
        if tool == "drive_write_doc_content":
            return await self._write_doc_content(**args)
        if tool == "drive_move_file":
            return await self._move_file(**args)
        if tool == "drive_add_comment":
            return await self._add_comment(**args)
        if tool == "drive_list_shared_drives":
            return await self._list_shared_drives(**args)
        if tool == "drive_download_file":
            return await self._download_file(**args)
        if tool == "drive_sheets_create":
            return await self._sheets_create(**args)
        if tool == "drive_sheets_get_metadata":
            return await self._sheets_get_metadata(**args)
        if tool == "drive_sheets_get_values":
            return await self._sheets_get_values(**args)
        if tool == "drive_sheets_write_range":
            return await self._sheets_write_range(**args)
        if tool == "drive_sheets_add_sheet":
            return await self._sheets_add_sheet(**args)
        if tool == "drive_sheets_rename_sheet":
            return await self._sheets_rename_sheet(**args)
        if tool == "drive_sheets_format_range":
            return await self._sheets_format_range(**args)
        raise ValueError(f"Unknown Drive tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Auto
    # ------------------------------------------------------------------ #

    async def _list_files(self, query: str, max_results: int = 20) -> Any:
        t0 = time.time()
        files = await self._fetch(self._drive.list_files, query, max_results)
        self._auto_audit("drive_list_files", "Search Drive Files",
                         f"List files: query={query!r}", f"{len(files)} result(s)", t0)
        return files if isinstance(files, list) else (files.to_dict() if hasattr(files, "to_dict") else files)

    async def _get_file_metadata(self, file_id: str) -> Any:
        t0 = time.time()
        drive_file = await self._fetch(self._drive.get_file_metadata, file_id)
        self._auto_audit("drive_get_file_metadata", "Get Drive File Info",
                         f"Get metadata: {getattr(drive_file, 'name', file_id)}",
                         ", ".join(getattr(drive_file, "owners", [])) or "(unknown)", t0)
        return drive_file.to_dict() if hasattr(drive_file, "to_dict") else drive_file

    async def _list_folder(self, folder_id: str, max_results: int = 50) -> Any:
        t0 = time.time()
        files = await self._fetch(self._drive.list_folder, folder_id, max_results)
        self._auto_audit("drive_list_folder", "List Drive Folder",
                         f"List folder: {folder_id}", f"{len(files)} item(s)", t0)
        return files

    async def _list_shared_drives(self, max_results: int = 50) -> Any:
        t0 = time.time()
        drives = await self._fetch(self._drive.list_shared_drives, max_results)
        self._auto_audit("drive_list_shared_drives", "List Shared Drives",
                         "List Shared Drives", f"{len(drives)} drive(s)", t0)
        return drives

    async def _create_blank_file(
        self, name: str, mime_type: str, parent_folder_id: str = ""
    ) -> Any:
        t0 = time.time()
        result = await self._fetch(self._drive.create_blank_file, name, mime_type, parent_folder_id)
        file_id = result.get("id", "") if isinstance(result, dict) else getattr(result, "id", "")
        if file_id:
            self.session_created_ids.add(file_id)
        self._auto_audit("drive_create_blank_file", "Create Drive File",
                         f"Create: {name} ({mime_type})", f"id={file_id}", t0)
        return result

    async def _sheets_create(
        self, name: str, sheet_titles: str = "", parent_folder_id: str = ""
    ) -> Any:
        t0 = time.time()
        titles = _parse_json_str_list(sheet_titles)
        result = await self._fetch(self._drive.create_spreadsheet, name, titles, parent_folder_id)
        spreadsheet_id = result.get("id", "")
        if spreadsheet_id:
            self.session_created_ids.add(spreadsheet_id)
        self._auto_audit("drive_sheets_create", "Create Spreadsheet",
                         f"Create spreadsheet: {name}", f"id={spreadsheet_id}", t0)
        return result

    async def _sheets_get_metadata(self, spreadsheet_id: str) -> Any:
        t0 = time.time()
        sheets = await self._fetch(self._drive.list_sheets, spreadsheet_id)
        self._auto_audit("drive_sheets_get_metadata", "Get Spreadsheet Metadata",
                         f"List tabs: {spreadsheet_id}", f"{len(sheets)} tab(s)", t0)
        return sheets

    # ------------------------------------------------------------------ #
    # Review gate (reads)
    # ------------------------------------------------------------------ #

    async def _get_file_content(self, file_id: str) -> Any:
        content = await self._fetch(self._drive.get_file_content, file_id)
        drive_file = getattr(content, "file", None)
        name = getattr(drive_file, "name", None) or file_id
        owners = getattr(drive_file, "owners", [])
        size = getattr(drive_file, "size", "")
        modified = getattr(drive_file, "modified_time", "")
        content_bytes = getattr(content, "content_bytes", b"") or b""
        text = getattr(content, "content_text", "") or (
            f"[binary content — {len(content_bytes)} bytes; use drive_download_file to save it]"
            if content_bytes else "(no content)"
        )
        preview = {
            "File": name,
            "Owner": ", ".join(owners) if owners else "(unknown)",
            "Size": str(size) if size else "(unknown)",
            "Modified": str(modified) if modified else "(unknown)",
        }
        details = f"File: {name}\nOwner: {', '.join(owners)}\nModified: {modified}\n\n{text[:2000]}"
        filtered = content.to_dict() if hasattr(content, "to_dict") else {"file_id": file_id, "content": text}
        return await gated_call(
            connector=self.name,
            tool="drive_get_file_content",
            tool_name="Read Drive File",
            summary=f"Read \"{name}\"",
            sender=", ".join(owners) or "(unknown)",
            raw_data=content,
            filtered_data=filtered,
            gate="review",
            preview=preview,
            details_text=details,
            pii_scan_text=text[:2000],
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"file_id": file_id},
        )

    async def _sheets_get_values(self, spreadsheet_id: str, range_a1: str) -> Any:
        drive_file = await self._fetch(self._drive.get_file_metadata, spreadsheet_id)
        name = getattr(drive_file, "name", spreadsheet_id)
        owners = getattr(drive_file, "owners", [])
        values = await self._fetch(self._drive.get_sheet_values, spreadsheet_id, range_a1)
        preview = {"Spreadsheet": name, "Owner": ", ".join(owners) or "(unknown)", "Range": range_a1}
        rows_preview = _format_sheet_rows(values)
        details = f"Spreadsheet: {name}\nOwner: {', '.join(owners)}\nRange: {range_a1}\n\n{rows_preview}"
        return await gated_call(
            connector=self.name,
            tool="drive_sheets_get_values",
            tool_name="Read Sheet Values",
            summary=f"Read {range_a1} from \"{name}\"",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "values": values},
            filtered_data=values,
            gate="review",
            preview=preview,
            details_text=details,
            pii_scan_text=rows_preview,
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"spreadsheet_id": spreadsheet_id, "range_a1": range_a1},
        )

    async def _download_file(
        self, file_id: str, destination_dir: str = ""
    ) -> Any:
        import os
        result = await self._fetch(self._drive.download_file, file_id, destination_dir)
        name = result.get("name", file_id)
        path = result.get("path", "")
        size_bytes = result.get("size_bytes", 0)

        drive_file = await self._fetch(self._drive.get_file_metadata, file_id)
        owners = getattr(drive_file, "owners", [])
        modified = getattr(drive_file, "modified_time", "")

        preview = {
            "File": name,
            "Owner": ", ".join(owners) if owners else "(unknown)",
            "Size": f"{size_bytes:,} bytes",
            "Saved to": path,
        }
        details = (
            f"File: {name}\nOwner: {', '.join(owners)}\n"
            f"Size: {size_bytes:,} bytes\nModified: {modified}\n"
            f"Saved to: {path}"
        )
        return await gated_call(
            connector=self.name,
            tool="drive_download_file",
            tool_name="Download Drive File",
            summary=f"Download \"{name}\" to {os.path.dirname(path)}",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "path": path, "name": name, "size_bytes": size_bytes},
            filtered_data=result,
            gate="review",
            preview=preview,
            details_text=details,
            pii_scan_text="",
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"file_id": file_id, "destination_dir": destination_dir},
        )

    # ------------------------------------------------------------------ #
    # Popup gate (writes)
    # ------------------------------------------------------------------ #

    async def _write_doc_content(self, file_id: str, markdown: str) -> Any:
        drive_file = await self._fetch(self._drive.get_file_metadata, file_id)
        name = getattr(drive_file, "name", file_id)
        owners = getattr(drive_file, "owners", [])
        preview = {"File": name, "Owner": ", ".join(owners) or "(unknown)"}
        await gated_call(
            connector=self.name,
            tool="drive_write_doc_content",
            tool_name="Write Google Doc",
            summary=f"Write rich content to \"{name}\"",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "markdown_preview": markdown[:200]},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=markdown,
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"file_id": file_id},
        )
        return await self._fetch(self._drive.write_doc_rich_content, file_id, markdown)

    async def _upload_file(
        self,
        local_path: str = "",
        name: str = "",
        parent_folder_id: str = "",
        content_base64: str = "",
    ) -> Any:
        import base64
        import os

        if bool(local_path.strip()) == bool(content_base64.strip()):
            raise ValueError("drive_upload_file: provide exactly one of local_path or content_base64")

        if local_path.strip():
            display_name = name.strip() or os.path.basename(local_path)
            expanded = os.path.expanduser(local_path)
            size_bytes = os.path.getsize(expanded) if os.path.isfile(expanded) else 0
            source = f"From: {local_path}"
            sender = "(local file)"
        else:
            display_name = name.strip() or "(unnamed file)"
            try:
                size_bytes = len(base64.b64decode(content_base64, validate=True))
            except (base64.binascii.Error, ValueError):
                size_bytes = 0
            source = "From: inline content"
            sender = "(inline content)"

        preview = {
            "File": display_name,
            "Size": f"{size_bytes:,} bytes",
            "Destination": parent_folder_id or "My Drive (root)",
        }
        details = (
            f"Upload \"{display_name}\" ({size_bytes:,} bytes)\n"
            f"{source}\n"
            f"To: {parent_folder_id or 'My Drive (root)'}"
        )
        await gated_call(
            connector=self.name,
            tool="drive_upload_file",
            tool_name="Upload File to Drive",
            summary=f"Upload \"{display_name}\" to Drive",
            sender=sender,
            raw_data={"local_path": local_path, "name": display_name, "size_bytes": size_bytes},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=details,
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={
                "local_path": local_path,
                "name": name,
                "parent_folder_id": parent_folder_id,
                "content_base64": content_base64,
            },
        )
        result = await self._fetch(
            self._drive.upload_file, local_path, name, parent_folder_id, content_base64
        )
        file_id = result.get("id", "")
        if file_id:
            self.session_created_ids.add(file_id)
        return result

    async def _write_file_content(self, file_id: str, content: str) -> Any:
        drive_file = await self._fetch(self._drive.get_file_metadata, file_id)
        name = getattr(drive_file, "name", file_id)
        owners = getattr(drive_file, "owners", [])
        preview = {"File": name, "Owner": ", ".join(owners) or "(unknown)"}
        await gated_call(
            connector=self.name,
            tool="drive_write_file_content",
            tool_name="Write Drive File",
            summary=f"Write to \"{name}\"",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "content_preview": content[:200]},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=content,
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"file_id": file_id},
        )
        return await self._fetch(self._drive.write_file_content, file_id, content)

    async def _move_file(self, file_id: str, destination_folder_id: str) -> Any:
        drive_file = await self._fetch(self._drive.get_file_metadata, file_id)
        name = getattr(drive_file, "name", file_id)
        owners = getattr(drive_file, "owners", [])
        try:
            destination_folder = await self._fetch(self._drive.get_file_metadata, destination_folder_id)
            destination_name = getattr(destination_folder, "name", "") or destination_folder_id
        except RuntimeError:
            # Best-effort: some destinations (e.g. a Shared Drive root) aren't
            # fetchable as a regular file. Fall back to the raw id rather than
            # blocking the popup on a name lookup that can't succeed.
            destination_name = destination_folder_id
        preview = {"File": name, "Owner": ", ".join(owners) or "(unknown)", "Move to folder": destination_name}
        await gated_call(
            connector=self.name,
            tool="drive_move_file",
            tool_name="Move Drive File",
            summary=f"Move \"{name}\" to new folder",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "destination_folder_id": destination_folder_id},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text="File will be moved to the new folder; its content is unchanged.",
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"file_id": file_id, "destination_folder_id": destination_folder_id},
        )
        return await self._fetch(self._drive.move_file, file_id, destination_folder_id)

    async def _add_comment(self, file_id: str, comment: str) -> Any:
        drive_file = await self._fetch(self._drive.get_file_metadata, file_id)
        name = getattr(drive_file, "name", file_id)
        owners = getattr(drive_file, "owners", [])
        preview = {"File": name, "Owner": ", ".join(owners) or "(unknown)"}
        await gated_call(
            connector=self.name,
            tool="drive_add_comment",
            tool_name="Add Drive Comment",
            summary=f"Comment on \"{name}\"",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "comment": comment},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=comment,
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"file_id": file_id},
        )
        return await self._fetch(self._drive.add_comment, file_id, comment)

    async def _sheets_write_range(self, spreadsheet_id: str, range_a1: str, values: str) -> Any:
        drive_file = await self._fetch(self._drive.get_file_metadata, spreadsheet_id)
        name = getattr(drive_file, "name", spreadsheet_id)
        owners = getattr(drive_file, "owners", [])
        parsed_values = _parse_json_2d_list(values)
        if parsed_values is None:
            raise ValueError(
                "drive_sheets_write_range: 'values' must be a JSON 2D array, e.g. [[\"a\",\"b\"]]"
            )
        preview = {"Spreadsheet": name, "Owner": ", ".join(owners) or "(unknown)", "Range": range_a1}
        await gated_call(
            connector=self.name,
            tool="drive_sheets_write_range",
            tool_name="Write Sheet Range",
            summary=f"Write {range_a1} in \"{name}\"",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "values_preview": values[:200]},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=f"Spreadsheet: {name}\nRange: {range_a1}\n\n{_format_sheet_rows(parsed_values)}",
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"spreadsheet_id": spreadsheet_id, "range_a1": range_a1},
        )
        return await self._fetch(self._drive.write_sheet_values, spreadsheet_id, range_a1, parsed_values)

    async def _sheets_add_sheet(
        self, spreadsheet_id: str, title: str, rows: int = 1000, cols: int = 26
    ) -> Any:
        drive_file = await self._fetch(self._drive.get_file_metadata, spreadsheet_id)
        name = getattr(drive_file, "name", spreadsheet_id)
        owners = getattr(drive_file, "owners", [])
        preview = {"Spreadsheet": name, "Owner": ", ".join(owners) or "(unknown)", "New tab": title}
        await gated_call(
            connector=self.name,
            tool="drive_sheets_add_sheet",
            tool_name="Add Sheet Tab",
            summary=f"Add tab \"{title}\" to \"{name}\"",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "title": title},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=f"Add tab \"{title}\" ({rows} rows x {cols} cols) to \"{name}\"",
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"spreadsheet_id": spreadsheet_id, "title": title, "rows": rows, "cols": cols},
        )
        return await self._fetch(self._drive.add_sheet, spreadsheet_id, title, rows, cols)

    async def _sheets_rename_sheet(self, spreadsheet_id: str, sheet_id: int, new_title: str) -> Any:
        drive_file = await self._fetch(self._drive.get_file_metadata, spreadsheet_id)
        name = getattr(drive_file, "name", spreadsheet_id)
        owners = getattr(drive_file, "owners", [])
        preview = {
            "Spreadsheet": name, "Owner": ", ".join(owners) or "(unknown)",
            "Tab id": sheet_id, "New title": new_title,
        }
        await gated_call(
            connector=self.name,
            tool="drive_sheets_rename_sheet",
            tool_name="Rename Sheet Tab",
            summary=f"Rename tab {sheet_id} in \"{name}\" to \"{new_title}\"",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "sheet_id": sheet_id, "new_title": new_title},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=f"Rename tab {sheet_id} to \"{new_title}\"",
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"spreadsheet_id": spreadsheet_id, "sheet_id": sheet_id, "new_title": new_title},
        )
        return await self._fetch(self._drive.rename_sheet, spreadsheet_id, sheet_id, new_title)

    async def _sheets_format_range(
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
    ) -> Any:
        # Validate the range syntax before gating, not after: format_sheet_range()
        # only discovers bad A1 syntax once it's already past the approval
        # popup, so a doomed call still cost the user an unnecessary approval
        # decision. Same parser format_sheet_range() itself uses, just run early.
        try:
            _parse_a1_range(range_a1)
        except DriveClientError as exc:
            raise RuntimeError(str(exc)) from exc
        drive_file = await self._fetch(self._drive.get_file_metadata, spreadsheet_id)
        name = getattr(drive_file, "name", spreadsheet_id)
        owners = getattr(drive_file, "owners", [])

        applied = []
        if bold:
            applied.append(f"bold={bold}")
        if italic:
            applied.append(f"italic={italic}")
        if background_color:
            applied.append(f"background={background_color}")
        if text_color:
            applied.append(f"text_color={text_color}")
        if number_format:
            applied.append(f"number_format={number_format}")
        if horizontal_alignment:
            applied.append(f"align={horizontal_alignment}")
        if freeze_rows >= 0:
            applied.append(f"freeze_rows={freeze_rows}")
        if freeze_cols >= 0:
            applied.append(f"freeze_cols={freeze_cols}")
        if column_width >= 0:
            applied.append(f"column_width={column_width}px")
        if merge_type != "KEEP":
            applied.append(f"merge={merge_type}")
        summary_detail = ", ".join(applied) or "(no changes)"

        preview = {
            "Spreadsheet": name, "Owner": ", ".join(owners) or "(unknown)",
            "Range": range_a1, "Format": summary_detail,
        }
        await gated_call(
            connector=self.name,
            tool="drive_sheets_format_range",
            tool_name="Format Sheet Range",
            summary=f"Format {range_a1} in \"{name}\": {summary_detail}",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "range_a1": range_a1, "format": summary_detail},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=f"Range: {range_a1}\nFormat: {summary_detail}",
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"spreadsheet_id": spreadsheet_id, "sheet_id": sheet_id, "range_a1": range_a1},
        )
        return await self._fetch(
            self._drive.format_sheet_range, spreadsheet_id, sheet_id, range_a1,
            bold, italic, background_color, text_color, number_format,
            horizontal_alignment, freeze_rows, freeze_cols, column_width, merge_type,
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _fetch(self, func, *args) -> Any:
        try:
            return await asyncio.to_thread(func, *args)
        except DriveClientError as exc:
            logger.error("Drive fetch failed: %s", exc)
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
