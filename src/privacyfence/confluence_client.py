"""Confluence Cloud API client.

Authenticated via Atlassian OAuth 2.0 (3LO) — see ``atlassian_oauth.py``. The
OAuth app (client id/secret) is organization-level config; the resulting
access token + cloud id are per-user, shared with the Jira client (a single
Atlassian OAuth grant covers both products).

Required config keys:
  access_token – OAuth bearer token from atlassian_oauth.authorize_interactive
  cloud_id     – the Atlassian site's cloud id, used to build the
                 api.atlassian.com/ex/confluence/{cloud_id}/wiki proxy URL
  site_url     – the human-facing site URL (for page links), optional

Optional config keys (needed to refresh an expired access token — see
``_try_refresh`` below):
  client_id / client_secret – the organization's Atlassian OAuth app
  refresh_token             – from the same OAuth grant as access_token
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests
from atlassian import Confluence

from .atlassian_oauth import (
    AtlassianOAuthError,
    is_unauthorized,
    load_token_file,
    refresh as atlassian_refresh,
    save_token_file,
)

logger = logging.getLogger(__name__)

# Confluence Cloud removed the v1 content endpoints (`rest/api/space`,
# `rest/api/content*` — what atlassian-python-api's get_all_spaces(),
# get_all_pages_from_space(), get_page_by_id(), get_page_by_title(),
# create_page(), and update_page() all call) — they now 410 with a
# GoneException. Space and page operations have to go through the v2 API
# instead, via the library's raw get/post/put helpers.
_V2_SPACES_PATH = "api/v2/spaces"
_V2_PAGES_PATH = "api/v2/pages"


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
    """Confluence Cloud client backed by an Atlassian OAuth 2.0 bearer token.

    ``config`` merges the organization's Atlassian OAuth app credentials
    (``client_id``, ``client_secret``) with the per-user token
    (``access_token``, ``refresh_token``, ``cloud_id``, ``site_url``). When
    the access token has expired (Atlassian tokens are short-lived), the
    client refreshes it once and retries automatically; if ``token_file`` is
    given, the refreshed token is persisted back to disk so the next app
    launch doesn't need a fresh sign-in.
    """

    def __init__(self, config: dict[str, Any], token_file: str | None = None) -> None:
        self._config = dict(config)
        self._token_file = token_file
        access_token = self._config.get("access_token", "")
        cloud_id = self._config.get("cloud_id", "")
        site_url = (self._config.get("site_url") or "").rstrip("/")

        if not access_token or not cloud_id:
            raise ConfluenceClientError(
                "Confluence is not authenticated. Use Authenticate… in the PrivacyFence menu bar."
            )

        # Confluence Cloud's REST API lives under /wiki (unlike Jira's), and the
        # atlassian-python-api library only auto-appends it for atlassian.net /
        # jira.com URLs — not for this api.atlassian.com OAuth proxy URL.
        api_url = f"https://api.atlassian.com/ex/confluence/{cloud_id}/wiki"
        self._base_url = site_url or api_url
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {access_token}"
        try:
            self._client = Confluence(url=api_url, session=self._session, cloud=True)
        except Exception as exc:
            raise ConfluenceClientError(f"Failed to initialise Confluence client: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Token refresh
    # ------------------------------------------------------------------ #

    def _try_refresh(self) -> bool:
        """Attempt to refresh the access token in place. Returns True on success."""
        client_id = self._config.get("client_id", "")
        client_secret = self._config.get("client_secret", "")
        refresh_token = self._config.get("refresh_token", "")
        if self._token_file:
            # The token file is shared with JiraClient; if it already
            # refreshed (and Atlassian rotated the refresh token), pick up
            # its latest value instead of retrying a spent one.
            try:
                refresh_token = load_token_file(self._token_file).get("refresh_token") or refresh_token
            except AtlassianOAuthError:
                pass
        if not client_id or not client_secret or not refresh_token:
            return False
        try:
            data = atlassian_refresh(client_id, client_secret, refresh_token)
        except AtlassianOAuthError as exc:
            logger.warning("Confluence token refresh failed: %s", exc)
            return False
        access_token = data.get("access_token", "")
        if not access_token:
            return False
        self._config["access_token"] = access_token
        self._config["refresh_token"] = data.get("refresh_token", refresh_token)
        self._session.headers["Authorization"] = f"Bearer {access_token}"
        if self._token_file:
            save_token_file(self._token_file, {
                "access_token": access_token,
                "refresh_token": self._config["refresh_token"],
                "cloud_id": self._config.get("cloud_id", ""),
                "site_url": self._config.get("site_url", ""),
                "account_email": self._config.get("account_email", ""),
            })
        logger.info("Confluence access token refreshed")
        return True

    def _request(self, fn, *args: Any, **kwargs: Any) -> Any:
        """Call ``fn`` with one automatic refresh-and-retry on an expired token."""
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if is_unauthorized(exc) and self._try_refresh():
                return fn(*args, **kwargs)
            raise

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #

    def check_connection(self) -> str:
        """Verify credentials by listing spaces. Returns the site URL on success."""
        try:
            self._request(self._client.get, _V2_SPACES_PATH, params={"limit": 1})
            logger.info("Connected to Confluence at %s", self._base_url)
            return self._base_url
        except Exception as exc:
            raise ConfluenceClientError(f"Confluence connection check failed: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Spaces
    # ------------------------------------------------------------------ #

    def list_spaces(self, max_results: int = 50, space_type: str = "") -> list[ConfluenceSpace]:
        max_results = max(1, min(max_results, 250))  # v2 API page size cap
        params: dict[str, Any] = {"limit": max_results, "description-format": "plain"}
        if space_type:
            params["type"] = space_type
        try:
            raw = self._request(self._client.get, _V2_SPACES_PATH, params=params)
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
            raw = self._request(
                self._client.cql,
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
            raw = self._request(self._client.cql, cql, limit=max_results)
            results = (raw or {}).get("results") or []
        except Exception as exc:
            raise ConfluenceClientError(f"cql_search failed: {exc}") from exc
        items = [self._parse_search_result(r) for r in results]
        logger.info("cql_search cql=%r returned %d result(s)", cql, len(items))
        return items

    # ------------------------------------------------------------------ #
    # Space id/key resolution (v2 endpoints address spaces by numeric id,
    # not the human-facing key everything else in this module uses)
    # ------------------------------------------------------------------ #

    def _resolve_space_id(self, space_key: str) -> str:
        try:
            raw = self._request(self._client.get, _V2_SPACES_PATH, params={"keys": space_key, "limit": 1})
            results = (raw or {}).get("results") or []
        except Exception as exc:
            # The v2 spaces endpoint 404s (rather than returning 200 with an
            # empty `results` list) when the `keys` filter matches nothing,
            # so an unmatched key surfaces the same way as any other request
            # failure. Recognize it and report the same clean "Space not
            # found" error as the empty-results case below.
            response = getattr(exc, "response", None)
            if getattr(response, "status_code", None) == 404:
                raise ConfluenceClientError(f"Space not found: {space_key!r}") from exc
            raise ConfluenceClientError(f"resolving space id for {space_key!r} failed: {exc}") from exc
        if not results:
            raise ConfluenceClientError(f"Space not found: {space_key!r}")
        return str(results[0].get("id", ""))

    def _resolve_space_key(self, space_id: str) -> str:
        if not space_id:
            return ""
        try:
            raw = self._request(self._client.get, f"{_V2_SPACES_PATH}/{space_id}")
        except Exception as exc:
            logger.warning("resolving space key for id %s failed: %s", space_id, exc)
            return ""
        return (raw or {}).get("key", "")

    # ------------------------------------------------------------------ #
    # Pages
    # ------------------------------------------------------------------ #

    def list_pages_in_space(self, space_key: str, max_results: int = 20) -> list[ConfluencePage]:
        if not space_key:
            raise ConfluenceClientError("list_pages_in_space requires a space_key")
        max_results = max(1, min(max_results, 200))  # v2 API page size cap is 250
        space_id = self._resolve_space_id(space_key)
        try:
            raw = self._request(
                self._client.get,
                f"{_V2_SPACES_PATH}/{space_id}/pages",
                params={"limit": max_results},
            )
            results = (raw or {}).get("results") or []
        except Exception as exc:
            raise ConfluenceClientError(f"list_pages_in_space({space_key!r}) failed: {exc}") from exc
        pages = [self._parse_page_v2(p, space_key=space_key) for p in results]
        logger.info("list_pages_in_space %s returned %d page(s)", space_key, len(pages))
        return pages

    def get_page(self, page_id: str) -> ConfluencePage:
        if not page_id:
            raise ConfluenceClientError("get_page requires a page_id")
        try:
            raw = self._request(
                self._client.get, f"{_V2_PAGES_PATH}/{page_id}", params={"body-format": "storage"},
            )
        except Exception as exc:
            raise ConfluenceClientError(f"get_page({page_id!r}) failed: {exc}") from exc
        page = self._parse_page_v2(raw, include_body=True)
        logger.info("get_page %s: %s", page_id, page.short_summary())
        return page

    def get_page_by_title(self, space_key: str, title: str) -> ConfluencePage:
        if not space_key or not title:
            raise ConfluenceClientError("get_page_by_title requires space_key and title")
        space_id = self._resolve_space_id(space_key)
        try:
            raw = self._request(
                self._client.get,
                _V2_PAGES_PATH,
                params={"space-id": space_id, "title": title, "body-format": "storage"},
            )
            results = (raw or {}).get("results") or []
        except Exception as exc:
            raise ConfluenceClientError(f"get_page_by_title({space_key!r}, {title!r}) failed: {exc}") from exc
        if not results:
            raise ConfluenceClientError(f"Page not found: {title!r} in space {space_key!r}")
        return self._parse_page_v2(results[0], include_body=True, space_key=space_key)

    def create_page(
        self,
        space_key: str,
        title: str,
        body: str,
        parent_id: str = "",
    ) -> ConfluencePage:
        if not space_key or not title:
            raise ConfluenceClientError("create_page requires space_key and title")
        space_id = self._resolve_space_id(space_key)
        payload: dict[str, Any] = {
            "spaceId": space_id,
            "status": "current",
            "title": title,
            "body": {"representation": "storage", "value": body},
        }
        if parent_id:
            payload["parentId"] = parent_id
        try:
            raw = self._request(self._client.post, _V2_PAGES_PATH, data=payload)
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
            current = self._request(self._client.get, f"{_V2_PAGES_PATH}/{page_id}")
            version = int((current.get("version") or {}).get("number", 1))
            payload = {
                "id": page_id,
                "status": "current",
                "title": title,
                "body": {"representation": "storage", "value": body},
                "version": {"number": version + 1},
            }
            self._request(self._client.put, f"{_V2_PAGES_PATH}/{page_id}", data=payload)
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

    def _parse_page_v2(
        self, raw: dict[str, Any], include_body: bool = False, space_key: str = "",
    ) -> ConfluencePage:
        """Parse a Confluence v2 page resource (``api/v2/pages/...`` shape).

        Unlike the old v1 ``history.lastUpdated.by.displayName``, v2 only
        gives back an ``authorId`` (an opaque account id) — resolving it to a
        display name would need a separate Users API call per page, so
        ``author`` is best-effort here rather than a human-readable name.
        """
        version = raw.get("version") or {}
        page_id = raw.get("id", "")
        space_key = space_key or self._resolve_space_key(str(raw.get("spaceId", "")))
        body = ""
        if include_body:
            body_raw = (raw.get("body") or {}).get("storage") or {}
            body = body_raw.get("value", "")
        return ConfluencePage(
            id=page_id,
            title=raw.get("title", ""),
            space_key=space_key,
            space_name="",
            version=int(version.get("number", 0)),
            author=raw.get("authorId", ""),
            created=raw.get("createdAt", ""),
            updated=version.get("createdAt", ""),
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
