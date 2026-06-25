"""Confluence connector: wraps ConfluenceClient with MCP tool definitions and gating."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..confluence_client import ConfluenceClient, ConfluenceClientError
from ..connector import Connector, ToolParam, ToolSpec
from ..gate import gated_call

logger = logging.getLogger(__name__)


class ConfluenceConnector(Connector):
    def __init__(self, client: ConfluenceClient) -> None:
        self._confluence = client
        self.my_email: str = ""

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
                    ToolParam("space_type", "str", required=False, default="global",
                              description="'global' or 'personal'"),
                ],
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
                ],
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
                ],
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
                ],
            ),
            ToolSpec(
                name="confluence_get_page",
                description=(
                    "Fetch the full content of a Confluence page by page ID. "
                    "Returns the page body as HTML storage format. Requires user approval."
                ),
                params=[
                    ToolParam("page_id", "str"),
                ],
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
                ],
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

    async def _list_spaces(self, max_results: int = 50, space_type: str = "global") -> Any:
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
        data = [asdict(r) for r in results]
        self._auto_audit(
            "confluence_search", "Search Confluence",
            f"Search: {query[:80]}", f"{len(results)} result(s)", t0,
        )
        return data

    async def _cql_search(self, cql: str, max_results: int = 20) -> Any:
        t0 = time.time()
        results = await self._fetch(self._confluence.cql_search, cql, max_results)
        data = [asdict(r) for r in results]
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

    # ------------------------------------------------------------------ #
    # Gated
    # ------------------------------------------------------------------ #

    async def _get_page(self, page_id: str) -> Any:
        page = await self._fetch(self._confluence.get_page, page_id)
        data = asdict(page)
        return await gated_call(
            connector=self.name,
            tool="confluence_get_page",
            tool_name="Read Confluence Page",
            summary=f"Read \"{page.title}\" ({page.space_key})",
            sender=page.author or page_id,
            raw_data=data,
            filtered_data=data,
            my_email=self.my_email,
            args={"page_id": page_id},
        )

    async def _get_page_by_title(self, space_key: str, title: str) -> Any:
        page = await self._fetch(self._confluence.get_page_by_title, space_key, title)
        data = asdict(page)
        return await gated_call(
            connector=self.name,
            tool="confluence_get_page_by_title",
            tool_name="Read Confluence Page",
            summary=f"Read \"{page.title}\" ({page.space_key})",
            sender=page.author or space_key,
            raw_data=data,
            filtered_data=data,
            my_email=self.my_email,
            args={"space_key": space_key, "title": title},
        )

    async def _create_page(
        self,
        space_key: str,
        title: str,
        body: str,
        parent_id: str = "",
    ) -> Any:
        preview = {"space_key": space_key, "title": title, "parent_id": parent_id}
        await gated_call(
            connector=self.name,
            tool="confluence_create_page",
            tool_name="Create Confluence Page",
            summary=f"Create \"{title}\" in {space_key}",
            sender=f"space={space_key}",
            raw_data={**preview, "body": body},
            filtered_data=preview,
            my_email=self.my_email,
            args=preview,
        )
        page = await self._fetch(self._confluence.create_page, space_key, title, body, parent_id)
        return asdict(page)

    async def _update_page(self, page_id: str, title: str, body: str) -> Any:
        preview = {"page_id": page_id, "title": title}
        await gated_call(
            connector=self.name,
            tool="confluence_update_page",
            tool_name="Update Confluence Page",
            summary=f"Update \"{title}\"",
            sender=f"page={page_id}",
            raw_data={**preview, "body": body},
            filtered_data=preview,
            my_email=self.my_email,
            args=preview,
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
                auto_accept_rule="always_allowed",
                latency_seconds=time.time() - created_at,
            ))
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)
