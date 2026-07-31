"""Confluence connector."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..confluence_client import ConfluenceClient, ConfluenceClientError, resolve_attachment_destination
from ..connector import Connector, ToolParam, ToolSpec
from ..gate import current_reason, gated_call
from ..html_to_text import html_to_text
from ..privacy_filter import apply_list, apply_text
from ..quicklook_preview import generate_thumbnail, is_quicklook_enabled
from ..text_extraction import extract_text, is_prefetch_worthy

logger = logging.getLogger(__name__)

# Cap on how big an attachment we'll fetch pre-approval -- for a preview
# (images) or a PII scan (text/PDF/DOCX/PPTX). Same reasoning and value as
# gmail.py's _ATTACHMENT_PREFETCH_MAX_BYTES: confluence_download_attachment's
# gate has to fully fetch the attachment to do either at all (no
# partial-fetch API), so this bounds how much we'll pull down before the
# human has decided anything.
_ATTACHMENT_PREFETCH_MAX_BYTES = 5_000_000


class ConfluenceConnector(Connector):
    def __init__(self, client: ConfluenceClient) -> None:
        self._confluence = client
        self.my_email: str = ""

    @property
    def client(self) -> ConfluenceClient:
        return self._confluence

    @property
    def name(self) -> str:
        return "confluence"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="confluence_list_spaces",
                description=(
                    "List Confluence spaces the user has access to "
                    "(key, name, type, description). Auto-approved."
                ),
                params=[
                    ToolParam("max_results", "int", required=False, default=50),
                    ToolParam("space_type", "str", required=False, default="",
                              description="Filter to 'global' or 'personal'; "
                                           "omit/empty to return all types"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            read_only=True,
            ),
            ToolSpec(
                name="confluence_search",
                description=(
                    "Full-text search across Confluence content. "
                    "Returns matching pages/blog posts with excerpts. Auto-approved."
                ),
                params=[
                    ToolParam("query", "str", description="Plain-text search terms"),
                    ToolParam("max_results", "int", required=False, default=20),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            read_only=True,
            ),
            ToolSpec(
                name="confluence_cql_search",
                description=(
                    "Search Confluence using CQL (Confluence Query Language). "
                    "Auto-approved."
                ),
                params=[
                    ToolParam("cql", "str", description="e.g. 'space = MYSPACE AND type = page'"),
                    ToolParam("max_results", "int", required=False, default=20),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            read_only=True,
            ),
            ToolSpec(
                name="confluence_list_pages",
                description=(
                    "List pages in a Confluence space (title, id, version). "
                    "Auto-approved."
                ),
                params=[
                    ToolParam("space_key", "str"),
                    ToolParam("max_results", "int", required=False, default=20),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            read_only=True,
            ),
            ToolSpec(
                name="confluence_list_attachments",
                description=(
                    "List attachment names, media types, and sizes for a "
                    "Confluence page. Auto-approved -- metadata only, no "
                    "attachment content is returned. Use "
                    "confluence_download_attachment to fetch the actual file."
                ),
                params=[
                    ToolParam("page_id", "str"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            read_only=True,
            ),
            ToolSpec(
                name="confluence_download_attachment",
                description=(
                    "Download a Confluence page attachment's content to a "
                    "local directory and return the saved file path. Identify "
                    "the attachment by the name returned from "
                    "confluence_list_attachments. destination_dir is required "
                    "-- there is no default, so choose deliberately: pass "
                    "~/Downloads (or another path the user asked for) when "
                    "this attachment is a deliverable the user should find "
                    "afterward, or your own working/scratch directory when "
                    "you're only downloading it to read or process it "
                    "yourself. Requires user approval."
                ),
                params=[
                    ToolParam("page_id", "str"),
                    ToolParam("attachment_name", "str"),
                    ToolParam(
                        "destination_dir",
                        "str",
                        required=True,
                        description=(
                            "Where to save the attachment -- required, no default. "
                            "Use ~/Downloads (or a path the user specified) if the "
                            "user should find this file afterward; use your own "
                            "working/scratch directory if it's only for you to read "
                            "or process."
                        ),
                    ),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            read_only=True,
            ),
            ToolSpec(
                name="confluence_get_page",
                description=(
                    "Fetch the full content of a Confluence page by page ID. "
                    "Returns the page body as HTML storage format. Requires user approval."
                ),
                params=[
                    ToolParam("page_id", "str"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            read_only=True,
            ),
            ToolSpec(
                name="confluence_get_page_by_title",
                description=(
                    "Fetch a Confluence page by space key and exact title. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("space_key", "str"),
                    ToolParam("title", "str"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            read_only=True,
            ),
            ToolSpec(
                name="confluence_create_page",
                description=(
                    "Create a new Confluence page in the given space. "
                    "Body is HTML storage format. Requires user approval."
                ),
                params=[
                    ToolParam("space_key", "str"),
                    ToolParam("title", "str"),
                    ToolParam("body", "str", description="HTML storage format body"),
                    ToolParam("parent_id", "str", required=False, default="",
                              description="Optional parent page ID"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="confluence_update_page",
                description=(
                    "Update the title and/or body of an existing Confluence page. "
                    "Body is HTML storage format. Requires user approval."
                ),
                params=[
                    ToolParam("page_id", "str"),
                    ToolParam("title", "str"),
                    ToolParam("body", "str", description="New HTML storage format body"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "confluence_list_spaces":
            return await self._list_spaces(**args)
        if tool == "confluence_search":
            return await self._search(**args)
        if tool == "confluence_cql_search":
            return await self._cql_search(**args)
        if tool == "confluence_list_pages":
            return await self._list_pages(**args)
        if tool == "confluence_list_attachments":
            return await self._list_attachments(**args)
        if tool == "confluence_download_attachment":
            return await self._download_attachment(**args)
        if tool == "confluence_get_page":
            return await self._get_page(**args)
        if tool == "confluence_get_page_by_title":
            return await self._get_page_by_title(**args)
        if tool == "confluence_create_page":
            return await self._create_page(**args)
        if tool == "confluence_update_page":
            return await self._update_page(**args)
        raise ValueError(f"Unknown Confluence tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Always-allowed
    # ------------------------------------------------------------------ #

    async def _list_spaces(self, max_results: int = 50, space_type: str = "") -> Any:
        t0 = time.time()
        spaces = await self._fetch(self._confluence.list_spaces, max_results, space_type)
        data = [asdict(s) for s in spaces]
        self._auto_audit(
            "confluence_list_spaces", "List Confluence Spaces",
            f"List spaces (max {max_results}, type={space_type})",
            f"{len(spaces)} space(s)", t0,
        )
        return data

    async def _search(self, query: str, max_results: int = 20) -> Any:
        t0 = time.time()
        results = await self._fetch(self._confluence.search, query, max_results)
        data = [_redact_excerpt(asdict(r)) for r in results]
        self._auto_audit(
            "confluence_search", "Search Confluence",
            f"Search: {query[:80]}", f"{len(results)} result(s)", t0,
        )
        return data

    async def _cql_search(self, cql: str, max_results: int = 20) -> Any:
        t0 = time.time()
        results = await self._fetch(self._confluence.cql_search, cql, max_results)
        data = [_redact_excerpt(asdict(r)) for r in results]
        self._auto_audit(
            "confluence_cql_search", "CQL Search Confluence",
            f"CQL: {cql[:80]}", f"{len(results)} result(s)", t0,
        )
        return data

    async def _list_pages(self, space_key: str, max_results: int = 20) -> Any:
        t0 = time.time()
        pages = await self._fetch(self._confluence.list_pages_in_space, space_key, max_results)
        data = [asdict(p) for p in pages]
        self._auto_audit(
            "confluence_list_pages", "List Confluence Pages",
            f"List pages in {space_key} (max {max_results})",
            f"{len(pages)} page(s)", t0,
        )
        return data

    async def _list_attachments(self, page_id: str) -> Any:
        t0 = time.time()
        attachments = await self._fetch(self._confluence.list_attachments, page_id)
        data = apply_list(
            "confluence_privacy", "attachments",
            [{"name": a.name, "media_type": a.media_type, "size": a.size} for a in attachments],
        )
        self._auto_audit(
            "confluence_list_attachments", "List Confluence Attachments",
            f"List attachments: page {page_id}", page_id, t0,
        )
        return {"page_id": page_id, "attachments": data}

    # ------------------------------------------------------------------ #
    # Gated
    # ------------------------------------------------------------------ #

    async def _get_page(self, page_id: str) -> Any:
        page = await self._fetch(self._confluence.get_page, page_id)
        data = asdict(page)
        # Title/Space are known for free via either confluence_list_pages OR
        # confluence_search/confluence_cql_search. Author/Last modified are
        # only ever known via confluence_list_pages -- confluence_search's
        # own ConfluenceSearchResult has no author/updated field at all (see
        # claude-knowledge-boundary.md) -- so, unlike Drive's "known if that
        # other call happened first" caveat, there's a real, likely path
        # (search) that never surfaces them, and they're treated as new
        # rather than assumed known. Page body has no fixed size, so it gets
        # one fixed summary row rather than a literal (possibly huge) value
        # -- the real content lives in the right-pane preview/details_text.
        preview_fields = {
            "Title": page.title or page_id,
            "Space": page.space_key or "(unknown)",
        }
        new_info = {
            "Author": page.author or "(unknown)",
            "Last modified": page.updated or "(unknown)",
            "Page body": "Full page content",
        }
        body_raw = getattr(page, "body", "") or getattr(page, "body_text", "") or ""
        body_text = html_to_text(body_raw)
        return await gated_call(
            connector=self.name,
            tool="confluence_get_page",
            tool_name="Read Confluence Page",
            summary=f"Read \"{page.title}\" ({page.space_key})",
            sender=page.author or page_id,
            raw_data=data,
            filtered_data=data,
            gate="review",
            preview=preview_fields,
            new_info=new_info,
            details_text=body_text,
            pii_scan_text=body_text,
            my_email=self.my_email,
            args={"page_id": page_id},
        )

    async def _get_page_by_title(self, space_key: str, title: str) -> Any:
        page = await self._fetch(self._confluence.get_page_by_title, space_key, title)
        data = asdict(page)
        # Same knowledge boundary as _get_page above.
        preview_fields = {
            "Title": page.title or title,
            "Space": page.space_key or space_key,
        }
        new_info = {
            "Author": page.author or "(unknown)",
            "Last modified": page.updated or "(unknown)",
            "Page body": "Full page content",
        }
        body_raw = getattr(page, "body", "") or getattr(page, "body_text", "") or ""
        body_text = html_to_text(body_raw)
        return await gated_call(
            connector=self.name,
            tool="confluence_get_page_by_title",
            tool_name="Read Confluence Page",
            summary=f"Read \"{page.title}\" ({page.space_key})",
            sender=page.author or space_key,
            raw_data=data,
            filtered_data=data,
            gate="review",
            preview=preview_fields,
            new_info=new_info,
            details_text=body_text,
            pii_scan_text=body_text,
            my_email=self.my_email,
            args={"space_key": space_key, "title": title},
        )

    async def _download_attachment(
        self, page_id: str, attachment_name: str, destination_dir: str = ""
    ) -> Any:
        page = await self._fetch(self._confluence.get_page, page_id)
        attachments = await self._fetch(self._confluence.list_attachments, page_id)
        attachment = next((a for a in attachments if a.name == attachment_name), None)
        if attachment is None:
            raise RuntimeError(f"No attachment named {attachment_name!r} on page {page_id}")
        dest_path = resolve_attachment_destination(attachment.name, destination_dir)
        # Title/Space are known for free via confluence_list_pages/
        # confluence_search; Attachment/Type/Size are known for free via
        # confluence_list_attachments -- same knowledge-boundary reasoning
        # as gmail_download_attachment (see claude-knowledge-boundary.md's
        # Gmail worked example). The only genuinely new facts from approving
        # this call are that no file content reaches Claude, and where it'll
        # be saved.
        preview = {
            "Title": page.title or page_id,
            "Space": page.space_key or "(unknown)",
            "Attachment": attachment.name,
            "Type": attachment.media_type,
            "Size": f"{attachment.size:,} bytes",
        }
        new_info = {
            "Content returned to Claude": "None — file bytes are never sent",
            "Will save to": dest_path,
        }
        details = "The attachment above will be downloaded to the destination shown."

        # Confluence's attachment download link has no partial/range fetch --
        # previewing or PII-scanning means fully fetching the attachment
        # before the human has decided anything, same tradeoff
        # gmail_download_attachment makes. Only worth it for types
        # is_prefetch_worthy() recognizes, under a sane size cap; anything
        # else keeps a metadata-only preview and unscanned content.
        preview_bytes = b""
        preview_mime_type = ""
        pii_scan_text = ""
        fetched_bytes: bytes | None = None
        if (
            is_prefetch_worthy(attachment.media_type)
            and 0 < attachment.size <= _ATTACHMENT_PREFETCH_MAX_BYTES
        ):
            try:
                fetched_bytes = await self._fetch(
                    self._confluence.fetch_attachment_bytes, attachment.download_url,
                )
            except RuntimeError:
                # _fetch() already turned the underlying ConfluenceClientError
                # into a RuntimeError and logged it -- this is a best-effort
                # preview/scan, not the actual download, so fall back to
                # today's metadata-only preview instead of failing the call.
                pass
            else:
                if attachment.media_type.startswith("image/"):
                    preview_bytes = fetched_bytes
                    preview_mime_type = attachment.media_type
                elif is_quicklook_enabled():
                    # Not an image -- QuickLook (off by default, menu-bar
                    # toggle) is the fallback preview source for anything its
                    # own renderer recognizes (PDFs, Office docs, and more).
                    # asyncio.to_thread, not a direct call: generate_thumbnail
                    # can block its calling thread for the full timeout, and
                    # this is an async def -- calling it directly would stall
                    # the whole daemon's event loop, not just this request.
                    thumbnail = await asyncio.to_thread(generate_thumbnail, fetched_bytes, attachment.name)
                    if thumbnail is not None:
                        preview_bytes = thumbnail
                        preview_mime_type = "image/png"
                pii_scan_text = extract_text(fetched_bytes, attachment.media_type)

        # Gate before touching disk: gated_call raises on denial, and only a
        # decision made here should ever cause the attachment to be written.
        await gated_call(
            connector=self.name,
            tool="confluence_download_attachment",
            tool_name="Download Confluence Attachment",
            summary=f"Download attachment '{attachment.name}' from: {page.title or page_id}",
            sender=page.author or page_id,
            raw_data=asdict(page),
            filtered_data=None,
            gate="review",
            preview=preview,
            new_info=new_info,
            details_text=details,
            pii_scan_text=pii_scan_text,
            preview_bytes=preview_bytes,
            preview_mime_type=preview_mime_type,
            my_email=self.my_email,
            args={"page_id": page_id, "attachment_name": attachment_name},
        )
        if fetched_bytes is not None:
            # Already fetched above for the preview/scan -- reuse it instead
            # of fetching the same attachment from Confluence a second time.
            return await self._fetch(
                self._confluence.save_attachment_bytes, fetched_bytes, attachment.name, destination_dir,
            )
        return await self._fetch(
            self._confluence.download_attachment, attachment.download_url, attachment.name, destination_dir,
        )

    async def _create_page(
        self,
        space_key: str,
        title: str,
        body: str,
        parent_id: str = "",
    ) -> Any:
        preview = {"Space": space_key, "Title": title}
        if parent_id:
            preview["Parent page ID"] = parent_id
        raw = {"space_key": space_key, "title": title, "parent_id": parent_id, "body": body}
        await gated_call(
            connector=self.name,
            tool="confluence_create_page",
            tool_name="Create Confluence Page",
            summary=f"Create \"{title}\" in {space_key}",
            sender=f"space={space_key}",
            raw_data=raw,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=body,
            my_email=self.my_email,
            args={"space_key": space_key, "title": title, "parent_id": parent_id},
        )
        page = await self._fetch(self._confluence.create_page, space_key, title, body, parent_id)
        return asdict(page)

    async def _update_page(self, page_id: str, title: str, body: str) -> Any:
        current = await self._fetch(self._confluence.get_page, page_id)
        preview = {
            "Page ID": page_id,
            "Space": current.space_key or "(unknown)",
            "Title": f"{current.title} → {title}" if title != current.title else title,
        }
        await gated_call(
            connector=self.name,
            tool="confluence_update_page",
            tool_name="Update Confluence Page",
            summary=f"Update \"{title}\"",
            sender=f"page={page_id}",
            raw_data={"page_id": page_id, "space_key": current.space_key, "title": title, "body": body},
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=body,
            my_email=self.my_email,
            args={"page_id": page_id, "space_key": current.space_key, "title": title},
        )
        page = await self._fetch(self._confluence.update_page, page_id, title, body)
        return asdict(page)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _fetch(self, func, *args) -> Any:
        try:
            return await asyncio.to_thread(func, *args)
        except ConfluenceClientError as exc:
            logger.error("Confluence fetch failed: %s", exc)
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


def _redact_excerpt(result_dict: dict[str, Any]) -> dict[str, Any]:
    """Apply confluence_privacy's "search_excerpt" category to one
    confluence_search/confluence_cql_search result -- excerpt is a genuine
    content excerpt built to show *why* a page matched (straight from
    Confluence's search API), not structural metadata like title/space/id,
    which have no category of their own."""
    result_dict["excerpt"] = apply_text(
        "confluence_privacy", "search_excerpt", result_dict.get("excerpt", "") or ""
    )
    return result_dict
