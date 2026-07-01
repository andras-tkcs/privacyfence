"""Confluence Cloud API client.

Uses Atlassian's API token authentication (email + token).
Create a token at https://id.atlassian.com/manage/api-tokens.

Required config keys:
  cloud_url   – e.g. https://yourcompany.atlassian.net
  email       – your Atlassian account email
  api_token   – personal API token

Shared with Jira: a single Atlassian account covers both products.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from atlassian import Confluence

logger = logging.getLogger(__name__)


class ConfluenceClientError(Exception):
    """Raised for unrecoverable Confluence client problems (auth, config, API)."""


@dataclass
class ConfluenceSpace:
    key: str
    name: str
    space_type: str = ""
    description: str = ""
    url: str = ""

    def short_summary(self) -> str:
        return f"[{self.key}] {self.name}"


@dataclass
class ConfluencePage:
    id: str
    title: str
    space_key: str
    space_name: str = ""
    version: int = 0
    author: str = ""
    created: str = ""
    updated: str = ""
    body: str = ""
    url: str = ""

    def short_summary(self) -> str:
        title = self.title[:60] + "…" if len(self.title) > 60 else self.title
        return f"[{self.space_key}] {title}"


@dataclass
class ConfluenceSearchResult:
    id: str
    title: str
    content_type: str
    space_key: str
    space_name: str = ""
    excerpt: str = ""
    url: str = ""

    def short_summary(self) -> str:
        title = self.title[:60] + "…" if len(self.title) > 60 else self.title
        return f"[{self.space_key}] {title}"


class ConfluenceClient:
    """Confluence Cloud client backed by email + API token (Basic auth)."""

    def __init__(self, config: dict[str, Any]) -> None:
        cloud_url = config.get("cloud_url", "").rstrip("/")
        email = config.get("email", "")
        api_token = config.get("api_token", "")

        if not cloud_url:
            raise ConfluenceClientError("confluence.cloud_url not configured")
        if not email:
            raise ConfluenceClientError("confluence.email not configured")
        if not api_token or api_token.startswith("your-"):
            raise ConfluenceClientError("confluence.api_token not configured")

        self._base_url = cloud_url
        try:
            self._client = Confluence(
                url=cloud_url,
                username=email,
                password=api_token,
                cloud=True,
            )
        except Exception as exc:
            raise ConfluenceClientError(f"Failed to initialise Confluence client: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #

    def check_connection(self) -> str:
        """Verify credentials by listing spaces. Returns the site URL on success."""
        try:
            result = self._client.get_all_spaces(start=0, limit=1)
            _ = result  # just checking it doesn't raise
            logger.info("Connected to Confluence at %s", self._base_url)
            return self._base_url
        except Exception as exc:
            raise ConfluenceClientError(f"Confluence connection check failed: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Spaces
    # ------------------------------------------------------------------ #

    def list_spaces(self, max_results: int = 50, space_type: str = "global") -> list[ConfluenceSpace]:
        max_results = max(1, min(max_results, 500))
        try:
            raw = self._client.get_all_spaces(
                start=0, limit=max_results, space_type=space_type
            )
            results = (raw or {}).get("results") or []
        except Exception as exc:
            raise ConfluenceClientError(f"list_spaces failed: {exc}") from exc
        spaces = [self._parse_space(s) for s in results]
        logger.info("list_spaces returned %d space(s)", len(spaces))
        return spaces

    # ------------------------------------------------------------------ #
    # Search
    # ------------------------------------------------------------------ #

    def search(self, query: str, max_results: int = 20) -> list[ConfluenceSearchResult]:
        if not query:
            raise ConfluenceClientError("search requires a non-empty query")
        max_results = max(1, min(max_results, 100))
        try:
            raw = self._client.cql(
                f'text ~ "{query}" order by lastmodified desc',
                limit=max_results,
            )
            results = (raw or {}).get("results") or []
        except Exception as exc:
            raise ConfluenceClientError(f"search({query!r}) failed: {exc}") from exc
        items = [self._parse_search_result(r) for r in results]
        logger.info("search query=%r returned %d result(s)", query, len(items))
        return items

    def cql_search(self, cql: str, max_results: int = 20) -> list[ConfluenceSearchResult]:
        if not cql:
            raise ConfluenceClientError("cql_search requires a non-empty CQL query")
        max_results = max(1, min(max_results, 100))
        try:
            raw = self._client.cql(cql, limit=max_results)
            results = (raw or {}).get("results") or []
        except Exception as exc:
            raise ConfluenceClientError(f"cql_search failed: {exc}") from exc
        items = [self._parse_search_result(r) for r in results]
        logger.info("cql_search cql=%r returned %d result(s)", cql, len(items))
        return items

    # ------------------------------------------------------------------ #
    # Pages
    # ------------------------------------------------------------------ #

    def list_pages_in_space(self, space_key: str, max_results: int = 20) -> list[ConfluencePage]:
        if not space_key:
            raise ConfluenceClientError("list_pages_in_space requires a space_key")
        max_results = max(1, min(max_results, 200))
        try:
            raw = self._client.get_all_pages_from_space(
                space_key, start=0, limit=max_results, expand="version,space"
            )
        except Exception as exc:
            raise ConfluenceClientError(f"list_pages_in_space({space_key!r}) failed: {exc}") from exc
        pages = [self._parse_page(p) for p in (raw or [])]
        logger.info("list_pages_in_space %s returned %d page(s)", space_key, len(pages))
        return pages

    def get_page(self, page_id: str) -> ConfluencePage:
        if not page_id:
            raise ConfluenceClientError("get_page requires a page_id")
        try:
            raw = self._client.get_page_by_id(
                page_id, expand="body.storage,version,space,history.lastUpdated"
            )
        except Exception as exc:
            raise ConfluenceClientError(f"get_page({page_id!r}) failed: {exc}") from exc
        page = self._parse_page(raw, include_body=True)
        logger.info("get_page %s: %s", page_id, page.short_summary())
        return page

    def get_page_by_title(self, space_key: str, title: str) -> ConfluencePage:
        if not space_key or not title:
            raise ConfluenceClientError("get_page_by_title requires space_key and title")
        try:
            raw = self._client.get_page_by_title(
                space_key, title, expand="body.storage,version,space,history.lastUpdated"
            )
        except Exception as exc:
            raise ConfluenceClientError(f"get_page_by_title({space_key!r}, {title!r}) failed: {exc}") from exc
        if not raw:
            raise ConfluenceClientError(f"Page not found: {title!r} in space {space_key!r}")
        return self._parse_page(raw, include_body=True)

    def create_page(
        self,
        space_key: str,
        title: str,
        body: str,
        parent_id: str = "",
    ) -> ConfluencePage:
        if not space_key or not title:
            raise ConfluenceClientError("create_page requires space_key and title")
        try:
            raw = self._client.create_page(
                space=space_key,
                title=title,
                body=body,
                parent_id=parent_id or None,
                representation="storage",
            )
        except Exception as exc:
            raise ConfluenceClientError(f"create_page failed: {exc}") from exc
        page_id = raw.get("id", "")
        logger.info("create_page created %s in %s", page_id, space_key)
        return self.get_page(page_id)

    def update_page(
        self,
        page_id: str,
        title: str,
        body: str,
    ) -> ConfluencePage:
        if not page_id or not title:
            raise ConfluenceClientError("update_page requires page_id and title")
        try:
            # Get current version to bump it
            current = self._client.get_page_by_id(page_id, expand="version")
            version = int((current.get("version") or {}).get("number", 1))
            self._client.update_page(
                page_id=page_id,
                title=title,
                body=body,
                version_number=version + 1,
                representation="storage",
            )
        except Exception as exc:
            raise ConfluenceClientError(f"update_page({page_id!r}) failed: {exc}") from exc
        logger.info("update_page %s updated to version %d", page_id, version + 1)
        return self.get_page(page_id)

    # ------------------------------------------------------------------ #
    # Parsing helpers
    # ------------------------------------------------------------------ #

    def _parse_space(self, raw: dict[str, Any]) -> ConfluenceSpace:
        desc_raw = (raw.get("description") or {}).get("plain", {})
        desc = desc_raw.get("value", "") if isinstance(desc_raw, dict) else ""
        return ConfluenceSpace(
            key=raw.get("key", ""),
            name=raw.get("name", ""),
            space_type=raw.get("type", ""),
            description=desc,
            url=f"{self._base_url}/wiki/spaces/{raw.get('key', '')}",
        )

    def _parse_page(self, raw: dict[str, Any], include_body: bool = False) -> ConfluencePage:
        space = raw.get("space") or {}
        version = raw.get("version") or {}
        history = (raw.get("history") or {}).get("lastUpdated") or {}
        page_id = raw.get("id", "")
        body = ""
        if include_body:
            body_raw = (raw.get("body") or {}).get("storage") or {}
            body = body_raw.get("value", "")
        return ConfluencePage(
            id=page_id,
            title=raw.get("title", ""),
            space_key=space.get("key", ""),
            space_name=space.get("name", ""),
            version=int(version.get("number", 0)),
            author=(history.get("by") or {}).get("displayName", ""),
            created=raw.get("history", {}).get("createdDate", "") if isinstance(raw.get("history"), dict) else "",
            updated=history.get("when", ""),
            body=body,
            url=f"{self._base_url}/wiki{raw.get('_links', {}).get('webui', '')}",
        )

    def _parse_search_result(self, raw: dict[str, Any]) -> ConfluenceSearchResult:
        result_space = (raw.get("resultGlobalContainer") or {})
        space_key = ""
        space_name = result_space.get("title", "")
        # Try to get space from the content itself
        content = raw.get("content") or {}
        content_space = content.get("space") or {}
        if content_space:
            space_key = content_space.get("key", "")
            space_name = content_space.get("name", space_name)

        content_id = content.get("id", "")
        url = f"{self._base_url}/wiki{raw.get('url', '')}"
        return ConfluenceSearchResult(
            id=content_id,
            title=raw.get("title", ""),
            content_type=raw.get("entityType", ""),
            space_key=space_key,
            space_name=space_name,
            excerpt=raw.get("excerpt", ""),
            url=url,
        )
