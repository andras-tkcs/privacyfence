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
from ..gate import current_reason, gated_call
from ..privacy_filter import apply_list, apply_text, category_policy

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

    @property
    def client(self) -> DriveClient:
        return self._drive

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
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="drive_get_file_metadata",
                description=(
                    "Fetch metadata for a single Drive file by id "
                    "(name, owners, times, sharing status). Auto-approved."
                ),
                params=[ToolParam("file_id", "str"), ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?")],
                read_only=True,
            ),
            ToolSpec(
                name="drive_list_folder",
                description="List the direct children of a Drive folder by id. Auto-approved.",
                params=[
                    ToolParam("folder_id", "str"),
                    ToolParam("max_results", "int", required=False, default=50),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
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
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="drive_get_file_content",
                description=(
                    "Fetch the content of a Drive file by id. Requires user approval."
                ),
                params=[ToolParam("file_id", "str"), ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?")],
                read_only=True,
            ),
            ToolSpec(
                name="drive_write_file_content",
                description="Write content to an existing Drive file. Requires user approval.",
                params=[
                    ToolParam("file_id", "str"),
                    ToolParam("content", "str"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
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
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="drive_move_file",
                description="Move a Drive file to a different folder. Requires user approval.",
                params=[
                    ToolParam("file_id", "str"),
                    ToolParam("destination_folder_id", "str"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="drive_add_comment",
                description="Add a comment to a Drive file. Requires user approval.",
                params=[
                    ToolParam("file_id", "str"),
                    ToolParam("comment", "str"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
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
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="drive_write_doc_content",
                description=(
                    "Write Markdown content to a Google Doc with rich formatting "
                    "(headings, bold, italic, ==highlight==, links, bullet and "
                    "numbered lists). Clears the existing document content "
                    "before writing — use drive_docs_edit_content or "
                    "drive_docs_format_content instead for a change that "
                    "shouldn't touch the rest of the document. "
                    "Use this instead of drive_write_file_content when the target "
                    "is a Google Doc and you want formatted output. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("file_id", "str"),
                    ToolParam("markdown", "str"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="drive_docs_edit_content",
                description=(
                    "Replace one occurrence of existing text in a Google Doc "
                    "with new Markdown, without touching the rest of the "
                    "document. find_text must match exactly one location in "
                    "the document's plain text (as read by "
                    "drive_get_file_content) — include enough surrounding "
                    "context to make it unique, the same way a unique-match "
                    "text editor requires; set replace_all=true to replace "
                    "every occurrence instead. replace_markdown supports the "
                    "same inline syntax as drive_write_doc_content, including "
                    "==highlight==. Requires user approval."
                ),
                params=[
                    ToolParam("file_id", "str"),
                    ToolParam("find_text", "str", description="Exact plain-text substring to locate"),
                    ToolParam(
                        "replace_markdown", "str",
                        description="Markdown to insert in its place",
                    ),
                    ToolParam("replace_all", "bool", required=False, default=False),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="drive_docs_format_content",
                description=(
                    "Apply formatting (bold, italic, highlight, text color) to "
                    "existing text in a Google Doc, located the same way as "
                    "drive_docs_edit_content, without changing the text itself. "
                    "Every formatting parameter is opt-in — its default means "
                    "'leave that aspect unchanged', so a call that only sets "
                    "highlight_color never touches bold/italic already on the "
                    "matched text. Requires user approval."
                ),
                params=[
                    ToolParam("file_id", "str"),
                    ToolParam("find_text", "str", description="Exact plain-text substring to locate"),
                    ToolParam("bold", "str", required=False, default="",
                              description="'true' or 'false'; omit to leave unchanged"),
                    ToolParam("italic", "str", required=False, default="",
                              description="'true' or 'false'; omit to leave unchanged"),
                    ToolParam("highlight_color", "str", required=False, default="",
                              description="hex color e.g. '#fff59d'; omit to leave unchanged"),
                    ToolParam("text_color", "str", required=False, default="",
                              description="hex color e.g. '#000000'; omit to leave unchanged"),
                    ToolParam("replace_all", "bool", required=False, default=False),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
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
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
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
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="drive_sheets_get_metadata",
                description=(
                    "List the tabs in a spreadsheet (id, title, index, row/column "
                    "count). Auto-approved."
                ),
                params=[ToolParam("spreadsheet_id", "str"), ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?")],
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
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
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
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
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
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
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
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
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
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="drive_sheets_insert_dimensions",
                description=(
                    "Insert blank rows or columns into a sheet tab, shifting "
                    "existing content after the insertion point. Values/"
                    "formulas are untouched, only their position shifts; "
                    "formulas referencing shifted cells are adjusted "
                    "automatically. Requires user approval."
                ),
                params=[
                    ToolParam("spreadsheet_id", "str"),
                    ToolParam("sheet_id", "int", description="Numeric tab id, from drive_sheets_get_metadata"),
                    ToolParam("dimension", "str", description="'ROWS' or 'COLUMNS'"),
                    ToolParam("start_index", "int", description="0-based index to insert before"),
                    ToolParam("count", "int", required=False, default=1),
                    ToolParam(
                        "inherit_from_before", "bool", required=False, default=True,
                        description="Copy formatting from the row/column before the insertion point (Sheets UI default)",
                    ),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="drive_sheets_delete_dimensions",
                description=(
                    "Delete rows or columns from a sheet tab, including any "
                    "values, formulas, and formatting they contain. This is "
                    "destructive — deleted cell content is not recoverable "
                    "through PrivacyFence. Remaining rows/columns shift to "
                    "close the gap. Requires user approval."
                ),
                params=[
                    ToolParam("spreadsheet_id", "str"),
                    ToolParam("sheet_id", "int", description="Numeric tab id, from drive_sheets_get_metadata"),
                    ToolParam("dimension", "str", description="'ROWS' or 'COLUMNS'"),
                    ToolParam("start_index", "int", description="0-based, inclusive of the first row/column removed"),
                    ToolParam("count", "int", required=False, default=1),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
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
        if tool == "drive_sheets_insert_dimensions":
            return await self._sheets_insert_dimensions(**args)
        if tool == "drive_sheets_delete_dimensions":
            return await self._sheets_delete_dimensions(**args)
        if tool == "drive_docs_edit_content":
            return await self._docs_edit_content(**args)
        if tool == "drive_docs_format_content":
            return await self._docs_format_content(**args)
        raise ValueError(f"Unknown Drive tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Auto
    # ------------------------------------------------------------------ #

    async def _list_files(self, query: str, max_results: int = 20) -> Any:
        t0 = time.time()
        files = await self._fetch(self._drive.list_files, query, max_results)
        self._auto_audit("drive_list_files", "Search Drive Files",
                         f"List files: query={query!r}", f"{len(files)} result(s)", t0)
        result = files if isinstance(files, list) else (files.to_dict() if hasattr(files, "to_dict") else files)
        return apply_list("drive_privacy", "file_list", result) if isinstance(result, list) else result

    async def _get_file_metadata(self, file_id: str) -> Any:
        t0 = time.time()
        drive_file = await self._fetch(self._drive.get_file_metadata, file_id)
        self._auto_audit("drive_get_file_metadata", "Get Drive File Info",
                         f"Get metadata: {getattr(drive_file, 'name', file_id)}",
                         ", ".join(getattr(drive_file, "owners", [])) or "(unknown)", t0)
        # file_metadata has no single "the whole record" shape to redact --
        # unlike apply_list's collection-of-records case, this is one
        # record, so block collapses it to just the id (still needed to
        # correlate the call), not an empty value.
        if category_policy("drive_privacy", "file_metadata") == "allow":
            return drive_file.to_dict() if hasattr(drive_file, "to_dict") else drive_file
        return {"id": getattr(drive_file, "id", file_id)}

    async def _list_folder(self, folder_id: str, max_results: int = 50) -> Any:
        t0 = time.time()
        files = await self._fetch(self._drive.list_folder, folder_id, max_results)
        self._auto_audit("drive_list_folder", "List Drive Folder",
                         f"List folder: {folder_id}", f"{len(files)} item(s)", t0)
        return apply_list("drive_privacy", "folder_structure", files)

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
        raw_text = getattr(content, "content_text", "") or (
            f"[binary content — {len(content_bytes)} bytes; use drive_download_file to save it]"
            if content_bytes else "(no content)"
        )
        text = apply_text("drive_privacy", "file_content", raw_text)
        # Native PDFView embed instead of the placeholder text above.
        # Gated on category_policy == "allow", the same condition
        # raw_text/text already require to
        # flow through unredacted -- a reviewer must never see a rendered
        # PDF that's richer than what the "AI will receive" checklist
        # already discloses Claude gets for this same call (see gate.py's
        # gated_call docstring). Also requires the fetch to be untruncated:
        # a partial PDF stream (get_file_content's max_bytes cap) almost
        # always fails to parse as a valid document anyway, and a page
        # silently missing its back half is worse than the plain-text
        # fallback, not better.
        pdf_bytes = b""
        if (
            content_bytes
            and not getattr(content, "truncated", False)
            and getattr(drive_file, "mime_type", "") == "application/pdf"
            and category_policy("drive_privacy", "file_content") == "allow"
        ):
            pdf_bytes = content_bytes
        file_display = apply_text("drive_privacy", "file_metadata", name)
        owner_display = apply_text("drive_privacy", "file_metadata", ", ".join(owners) if owners else "")
        size_display = apply_text("drive_privacy", "file_metadata", str(size) if size else "")
        modified_display = apply_text("drive_privacy", "file_metadata", str(modified) if modified else "")
        preview = {
            "File": file_display or "(unknown)",
            "Owner": owner_display or "(unknown)",
            "Size": size_display or "(unknown)",
            "Modified": modified_display or "(unknown)",
        }
        filtered = {"file_id": file_id, "content": text}
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
            details_text=text[:2000],
            pii_scan_text=text[:2000],
            visibility={
                "File metadata": category_policy("drive_privacy", "file_metadata"),
                "Document content": category_policy("drive_privacy", "file_content"),
            },
            pdf_bytes=pdf_bytes,
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"file_id": file_id},
        )

    async def _sheets_get_values(self, spreadsheet_id: str, range_a1: str) -> Any:
        drive_file = await self._fetch(self._drive.get_file_metadata, spreadsheet_id)
        name = getattr(drive_file, "name", spreadsheet_id)
        owners = getattr(drive_file, "owners", [])
        raw_values = await self._fetch(self._drive.get_sheet_values, spreadsheet_id, range_a1)
        values = apply_list("drive_privacy", "file_content", raw_values)
        preview = {"Spreadsheet": name, "Owner": ", ".join(owners) or "(unknown)", "Range": range_a1}
        rows_preview = _format_sheet_rows(values)
        return await gated_call(
            connector=self.name,
            tool="drive_sheets_get_values",
            tool_name="Read Sheet Values",
            summary=f"Read {range_a1} from \"{name}\"",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "values": raw_values},
            filtered_data=values,
            gate="review",
            preview=preview,
            details_text=rows_preview,
            pii_scan_text=rows_preview,
            visibility={"Cell values": category_policy("drive_privacy", "file_content")},
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
            "Modified": str(modified) if modified else "(unknown)",
            "Saved to": path,
        }
        details = "The file above has been downloaded to the destination shown."
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

    async def _docs_edit_content(
        self, file_id: str, find_text: str, replace_markdown: str, replace_all: bool = False
    ) -> Any:
        drive_file = await self._fetch(self._drive.get_file_metadata, file_id)
        name = getattr(drive_file, "name", file_id)
        owners = getattr(drive_file, "owners", [])
        preview = {
            "File": name, "Owner": ", ".join(owners) or "(unknown)",
            "Match": "every occurrence" if replace_all else "the one matching occurrence",
        }
        details = f"Find:\n{find_text}\n\nReplace with:\n{replace_markdown}"
        await gated_call(
            connector=self.name,
            tool="drive_docs_edit_content",
            tool_name="Edit Google Doc",
            summary=f"Replace text in \"{name}\"",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "find_text_preview": find_text[:200]},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=details,
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"file_id": file_id},
        )
        return await self._fetch(
            self._drive.edit_doc_content, file_id, find_text, replace_markdown, replace_all
        )

    async def _docs_format_content(
        self,
        file_id: str,
        find_text: str,
        bold: str = "",
        italic: str = "",
        highlight_color: str = "",
        text_color: str = "",
        replace_all: bool = False,
    ) -> Any:
        drive_file = await self._fetch(self._drive.get_file_metadata, file_id)
        name = getattr(drive_file, "name", file_id)
        owners = getattr(drive_file, "owners", [])

        applied = []
        if bold:
            applied.append(f"bold={bold}")
        if italic:
            applied.append(f"italic={italic}")
        if highlight_color:
            applied.append(f"highlight={highlight_color}")
        if text_color:
            applied.append(f"text_color={text_color}")
        summary_detail = ", ".join(applied) or "(no changes)"

        preview = {
            "File": name, "Owner": ", ".join(owners) or "(unknown)",
            "Format": summary_detail,
        }
        details = f"Applying the formatting above to:\n{find_text}"
        await gated_call(
            connector=self.name,
            tool="drive_docs_format_content",
            tool_name="Format Google Doc Text",
            summary=f"Format text in \"{name}\": {summary_detail}",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "find_text_preview": find_text[:200], "format": summary_detail},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=details,
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"file_id": file_id},
        )
        return await self._fetch(
            self._drive.format_doc_content, file_id, find_text, bold, italic, highlight_color, text_color, replace_all
        )

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
            source = local_path
            sender = "(local file)"
        else:
            display_name = name.strip() or "(unnamed file)"
            try:
                size_bytes = len(base64.b64decode(content_base64, validate=True))
            except (base64.binascii.Error, ValueError):
                size_bytes = 0
            source = "inline content"
            sender = "(inline content)"

        preview = {
            "File": display_name,
            "Source": source,
            "Size": f"{size_bytes:,} bytes",
            "Destination": parent_folder_id or "My Drive (root)",
        }
        details = "The file above will be uploaded to the destination shown."
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
            details_text=_format_sheet_rows(parsed_values),
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
        preview = {
            "Spreadsheet": name, "Owner": ", ".join(owners) or "(unknown)",
            "New tab": title, "Size": f"{rows} rows x {cols} cols",
        }
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
            details_text="A new tab will be added with the settings shown above.",
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
            details_text="The tab above will be renamed; its contents are unchanged.",
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
            details_text="The formatting above will be applied to the range; other formatting is unchanged.",
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={"spreadsheet_id": spreadsheet_id, "sheet_id": sheet_id, "range_a1": range_a1},
        )
        return await self._fetch(
            self._drive.format_sheet_range, spreadsheet_id, sheet_id, range_a1,
            bold, italic, background_color, text_color, number_format,
            horizontal_alignment, freeze_rows, freeze_cols, column_width, merge_type,
        )

    async def _sheets_insert_dimensions(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        dimension: str,
        start_index: int,
        count: int = 1,
        inherit_from_before: bool = True,
    ) -> Any:
        # Validate before gating, not after -- same reasoning as
        # _sheets_format_range's early range_a1 check: a doomed call
        # shouldn't cost the user an unnecessary approval decision.
        dimension = dimension.strip().upper()
        if dimension not in ("ROWS", "COLUMNS"):
            raise ValueError(
                f"drive_sheets_insert_dimensions: dimension must be 'ROWS' or 'COLUMNS', got {dimension!r}"
            )
        drive_file = await self._fetch(self._drive.get_file_metadata, spreadsheet_id)
        name = getattr(drive_file, "name", spreadsheet_id)
        owners = getattr(drive_file, "owners", [])
        preview = {
            "Spreadsheet": name, "Owner": ", ".join(owners) or "(unknown)",
            "Tab id": sheet_id, "Action": f"Insert {count} {dimension} before index {start_index}",
        }
        await gated_call(
            connector=self.name,
            tool="drive_sheets_insert_dimensions",
            tool_name="Insert Sheet Rows/Columns",
            summary=f"Insert {count} {dimension} in \"{name}\"",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "dimension": dimension, "start_index": start_index, "count": count},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=(
                "Existing rows/columns from the insertion point shift accordingly; "
                "other cells are unchanged."
            ),
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={
                "spreadsheet_id": spreadsheet_id, "sheet_id": sheet_id,
                "dimension": dimension, "start_index": start_index, "count": count,
            },
        )
        return await self._fetch(
            self._drive.insert_dimensions, spreadsheet_id, sheet_id, dimension,
            start_index, count, inherit_from_before,
        )

    async def _sheets_delete_dimensions(
        self, spreadsheet_id: str, sheet_id: int, dimension: str, start_index: int, count: int = 1
    ) -> Any:
        dimension = dimension.strip().upper()
        if dimension not in ("ROWS", "COLUMNS"):
            raise ValueError(
                f"drive_sheets_delete_dimensions: dimension must be 'ROWS' or 'COLUMNS', got {dimension!r}"
            )
        drive_file = await self._fetch(self._drive.get_file_metadata, spreadsheet_id)
        name = getattr(drive_file, "name", spreadsheet_id)
        owners = getattr(drive_file, "owners", [])
        preview = {
            "Spreadsheet": name, "Owner": ", ".join(owners) or "(unknown)",
            "Tab id": sheet_id, "Action": f"Delete {count} {dimension} starting at index {start_index}",
        }
        await gated_call(
            connector=self.name,
            tool="drive_sheets_delete_dimensions",
            tool_name="Delete Sheet Rows/Columns",
            summary=f"Delete {count} {dimension} in \"{name}\"",
            sender=", ".join(owners) or "(unknown)",
            raw_data={"file": drive_file, "dimension": dimension, "start_index": start_index, "count": count},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=(
                "Rows/columns and any values, formulas, or formatting they contain will be "
                "removed — not recoverable through PrivacyFence. Remaining rows/columns shift "
                "to close the gap."
            ),
            my_email=self.my_email,
            session_created_ids=self.session_created_ids,
            args={
                "spreadsheet_id": spreadsheet_id, "sheet_id": sheet_id,
                "dimension": dimension, "start_index": start_index, "count": count,
            },
        )
        return await self._fetch(
            self._drive.delete_dimensions, spreadsheet_id, sheet_id, dimension, start_index, count,
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
                claude_reason=current_reason(),
            ))
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)
