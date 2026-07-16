"""Tests for ConfluenceClient's parsing logic, refresh-and-retry token
logic, and result normalization. Mirrors test_jira_client.py's approach --
same Atlassian OAuth refresh regression target (commit 862ff43), same
"construct atlassian.Confluence for real, swap in a MagicMock for ._client"
pattern.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from privacyfence import confluence_client as confluence_client_module
from privacyfence.atlassian_oauth import AtlassianOAuthError
from privacyfence.confluence_client import (
    ConfluenceClient,
    ConfluenceClientError,
    ConfluencePage,
    ConfluenceSearchResult,
    ConfluenceSpace,
)

LIVE_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "live" / "confluence"


def make_client(config: dict | None = None, token_file: str | None = None) -> ConfluenceClient:
    base = {"access_token": "tok", "cloud_id": "cloud-1", "site_url": "https://acme.atlassian.net"}
    base.update(config or {})
    client = ConfluenceClient(config=base, token_file=token_file)
    client._client = MagicMock()
    return client


def unauthorized_error() -> Exception:
    return error_with_status(401, "401 Unauthorized")


def error_with_status(status_code: int, message: str) -> Exception:
    exc = Exception(message)
    response = MagicMock()
    response.status_code = status_code
    exc.response = response
    return exc


# ---------------------------------------------------------------------------- #
# Construction
# ---------------------------------------------------------------------------- #

class TestConstruction:
    def test_missing_access_token_raises(self):
        with pytest.raises(ConfluenceClientError, match="not authenticated"):
            ConfluenceClient(config={"cloud_id": "c1"})

    def test_missing_cloud_id_raises(self):
        with pytest.raises(ConfluenceClientError, match="not authenticated"):
            ConfluenceClient(config={"access_token": "t"})

    def test_base_url_prefers_site_url(self):
        client = ConfluenceClient(config={"access_token": "t", "cloud_id": "c1", "site_url": "https://acme.atlassian.net/"})
        assert client._base_url == "https://acme.atlassian.net"

    def test_base_url_falls_back_to_wiki_api_url(self):
        client = ConfluenceClient(config={"access_token": "t", "cloud_id": "c1"})
        assert client._base_url == "https://api.atlassian.com/ex/confluence/c1/wiki"


# ---------------------------------------------------------------------------- #
# _parse_space
# ---------------------------------------------------------------------------- #

class TestParseSpace:
    def test_full_space(self):
        client = make_client()
        raw = {"key": "ENG", "name": "Engineering", "type": "global",
               "description": {"plain": {"value": "desc"}}}
        space = client._parse_space(raw)
        assert space == ConfluenceSpace(
            key="ENG", name="Engineering", space_type="global", description="desc",
            url="https://acme.atlassian.net/wiki/spaces/ENG",
        )

    def test_missing_description_defaults_empty(self):
        client = make_client()
        space = client._parse_space({"key": "ENG", "name": "Engineering"})
        assert space.description == ""

    def test_short_summary(self):
        assert ConfluenceSpace(key="ENG", name="Engineering").short_summary() == "[ENG] Engineering"


# ---------------------------------------------------------------------------- #
# _parse_page_v2
# ---------------------------------------------------------------------------- #

class TestParsePageV2:
    def test_full_page_without_body(self):
        client = make_client()
        raw = {
            "id": "123", "title": "My Page", "spaceId": "999",
            "version": {"number": 3, "createdAt": "updated-date"},
            "authorId": "acc-1", "createdAt": "created-date",
            "_links": {"webui": "/spaces/ENG/pages/123"},
        }
        page = client._parse_page_v2(raw, space_key="ENG")
        assert page.id == "123"
        assert page.title == "My Page"
        assert page.space_key == "ENG"
        assert page.version == 3
        assert page.author == "acc-1"
        assert page.created == "created-date"
        assert page.updated == "updated-date"
        assert page.body == ""
        assert page.url == "https://acme.atlassian.net/wiki/spaces/ENG/pages/123"

    def test_body_included_only_when_requested(self):
        client = make_client()
        raw = {"id": "1", "body": {"storage": {"value": "<p>content</p>"}}}
        without_body = client._parse_page_v2(raw, include_body=False)
        with_body = client._parse_page_v2(raw, include_body=True)
        assert without_body.body == ""
        assert with_body.body == "<p>content</p>"

    def test_missing_optional_fields_default_sensibly(self):
        client = make_client()
        # No spaceId, so _resolve_space_key is skipped rather than firing an
        # HTTP call for an empty id.
        page = client._parse_page_v2({})
        assert page.id == ""
        assert page.version == 0
        assert page.author == ""
        assert page.space_key == ""

    def test_space_key_resolved_from_space_id_when_not_passed_in(self):
        client = make_client()
        client._client.get.return_value = {"key": "ENG"}
        page = client._parse_page_v2({"id": "1", "spaceId": "999"})
        assert page.space_key == "ENG"
        assert client._client.get.call_args.args[0] == "api/v2/spaces/999"

    def test_short_summary_truncates_long_title(self):
        page = ConfluencePage(id="1", title="x" * 100, space_key="ENG")
        assert page.short_summary().startswith("[ENG]")
        assert page.short_summary().endswith("…")


# ---------------------------------------------------------------------------- #
# _parse_search_result: space resolution priority
# ---------------------------------------------------------------------------- #

class TestParseSearchResult:
    def test_space_resolved_from_content_when_present(self):
        client = make_client()
        raw = {
            "title": "Result", "entityType": "page", "excerpt": "...", "url": "/x",
            "content": {"id": "1", "space": {"key": "ENG", "name": "Engineering"}},
            "resultGlobalContainer": {"title": "Fallback Name"},
        }
        result = client._parse_search_result(raw)
        assert result.space_key == "ENG"
        assert result.space_name == "Engineering"

    def test_falls_back_to_global_container_title_when_no_content_space(self):
        client = make_client()
        raw = {"title": "Result", "resultGlobalContainer": {"title": "Container Name"}}
        result = client._parse_search_result(raw)
        assert result.space_key == ""
        assert result.space_name == "Container Name"

    def test_url_prefixed_with_base_url_and_wiki(self):
        client = make_client()
        result = client._parse_search_result({"url": "/spaces/ENG/pages/1"})
        assert result.url == "https://acme.atlassian.net/wiki/spaces/ENG/pages/1"

    def test_short_summary(self):
        result = ConfluenceSearchResult(id="1", title="Found It", content_type="page", space_key="ENG")
        assert result.short_summary() == "[ENG] Found It"


# ---------------------------------------------------------------------------- #
# _try_refresh / _request: same reauth-on-restart regression target as Jira
# ---------------------------------------------------------------------------- #

class TestTryRefresh:
    def test_missing_credentials_returns_false(self):
        client = make_client({"refresh_token": "rt"})
        assert client._try_refresh() is False

    def test_successful_refresh_updates_config_and_session_header(self, monkeypatch):
        monkeypatch.setattr(confluence_client_module, "atlassian_refresh",
                             lambda cid, cs, rt: {"access_token": "new-tok", "refresh_token": "new-rt"})
        client = make_client({"client_id": "ci", "client_secret": "cs", "refresh_token": "rt"})

        assert client._try_refresh() is True
        assert client._config["access_token"] == "new-tok"
        assert client._session.headers["Authorization"] == "Bearer new-tok"

    def test_picks_up_refresh_token_already_rotated_by_jira_client(self, monkeypatch, tmp_path):
        captured = {}
        def fake_refresh(cid, cs, rt):
            captured["used"] = rt
            return {"access_token": "new-tok", "refresh_token": "newer-rt"}
        monkeypatch.setattr(confluence_client_module, "atlassian_refresh", fake_refresh)
        monkeypatch.setattr(confluence_client_module, "load_token_file", lambda path: {"refresh_token": "rotated-by-jira"})
        monkeypatch.setattr(confluence_client_module, "save_token_file", lambda path, record: None)

        client = make_client(
            {"client_id": "ci", "client_secret": "cs", "refresh_token": "stale-rt"},
            token_file=str(tmp_path / "shared_token.json"),
        )
        client._try_refresh()

        assert captured["used"] == "rotated-by-jira"

    def test_refresh_api_failure_returns_false(self, monkeypatch):
        def raiser(cid, cs, rt):
            raise AtlassianOAuthError("refresh failed")
        monkeypatch.setattr(confluence_client_module, "atlassian_refresh", raiser)
        client = make_client({"client_id": "ci", "client_secret": "cs", "refresh_token": "rt"})
        assert client._try_refresh() is False

    def test_persists_to_shared_token_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(confluence_client_module, "atlassian_refresh",
                             lambda cid, cs, rt: {"access_token": "new-tok", "refresh_token": "new-rt"})
        monkeypatch.setattr(
            confluence_client_module, "load_token_file",
            lambda path: (_ for _ in ()).throw(AtlassianOAuthError("no file yet")),
        )
        saved = {}
        monkeypatch.setattr(confluence_client_module, "save_token_file", lambda path, record: saved.update(record))

        client = make_client(
            {"client_id": "ci", "client_secret": "cs", "refresh_token": "rt", "account_email": "me@x.com"},
            token_file=str(tmp_path / "atlassian_token.json"),
        )
        client._try_refresh()

        assert saved == {
            "access_token": "new-tok", "refresh_token": "new-rt",
            "cloud_id": "cloud-1", "site_url": "https://acme.atlassian.net", "account_email": "me@x.com",
        }


class TestRequest:
    def test_happy_path(self):
        client = make_client()
        assert client._request(lambda: "ok") == "ok"

    def test_401_triggers_refresh_and_retry(self, monkeypatch):
        client = make_client()
        monkeypatch.setattr(client, "_try_refresh", lambda: True)
        calls = {"n": 0}
        def fn():
            calls["n"] += 1
            if calls["n"] == 1:
                raise unauthorized_error()
            return "retried-ok"
        assert client._request(fn) == "retried-ok"

    def test_401_refresh_fails_reraises(self, monkeypatch):
        client = make_client()
        monkeypatch.setattr(client, "_try_refresh", lambda: False)
        def fn():
            raise unauthorized_error()
        with pytest.raises(Exception, match="401"):
            client._request(fn)

    @pytest.mark.parametrize("status_code", [403, 404])
    def test_non_401_stale_token_status_triggers_refresh_and_retry(self, monkeypatch, status_code):
        # api.atlassian.com's Confluence proxy doesn't reliably return 401 for
        # an expired/invalid token: v2 REST endpoints 404, and the legacy
        # CQL-backed endpoints 403 ("Current user not permitted to use
        # Confluence"). Both need the same refresh-and-retry as a real 401,
        # or a stale token silently masquerades as "not found"/"not permitted".
        client = make_client()
        monkeypatch.setattr(client, "_try_refresh", lambda: True)
        calls = {"n": 0}
        def fn():
            calls["n"] += 1
            if calls["n"] == 1:
                raise error_with_status(status_code, "boom")
            return "retried-ok"
        assert client._request(fn) == "retried-ok"

    @pytest.mark.parametrize("status_code", [403, 404])
    def test_non_401_stale_token_status_refresh_fails_reraises(self, monkeypatch, status_code):
        client = make_client()
        monkeypatch.setattr(client, "_try_refresh", lambda: False)
        def fn():
            raise error_with_status(status_code, "boom")
        with pytest.raises(Exception, match="boom"):
            client._request(fn)

    def test_unrelated_status_code_does_not_trigger_refresh(self, monkeypatch):
        client = make_client()
        monkeypatch.setattr(client, "_try_refresh", lambda: (_ for _ in ()).throw(AssertionError("should not be called")))
        def fn():
            raise error_with_status(500, "server error")
        with pytest.raises(Exception, match="server error"):
            client._request(fn)


# ---------------------------------------------------------------------------- #
# check_connection / list_spaces / search / cql_search
# ---------------------------------------------------------------------------- #

class TestCheckConnection:
    def test_returns_base_url_on_success(self):
        client = make_client()
        client._client.get.return_value = {"results": []}
        assert client.check_connection() == "https://acme.atlassian.net"

    def test_error_becomes_confluence_client_error(self):
        client = make_client()
        client._client.get.side_effect = RuntimeError("boom")
        with pytest.raises(ConfluenceClientError, match="Confluence connection check failed"):
            client.check_connection()


class TestListSpaces:
    def test_maps_results(self):
        client = make_client()
        client._client.get.return_value = {"results": [{"key": "ENG", "name": "Engineering"}]}
        spaces = client.list_spaces()
        assert spaces[0].key == "ENG"

    def test_uses_v2_spaces_endpoint_with_type_filter(self):
        client = make_client()
        client._client.get.return_value = {"results": []}
        client.list_spaces(space_type="personal")
        args, kwargs = client._client.get.call_args
        assert args[0] == "api/v2/spaces"
        assert kwargs["params"]["type"] == "personal"

    def test_default_omits_type_filter(self):
        client = make_client()
        client._client.get.return_value = {"results": []}
        client.list_spaces()
        _args, kwargs = client._client.get.call_args
        assert "type" not in kwargs["params"]

    def test_none_response_yields_empty_list(self):
        client = make_client()
        client._client.get.return_value = None
        assert client.list_spaces() == []

    def test_http_error_becomes_confluence_client_error(self):
        client = make_client()
        client._client.get.side_effect = RuntimeError("boom")
        with pytest.raises(ConfluenceClientError, match="list_spaces failed"):
            client.list_spaces()


class TestSearch:
    def test_requires_query(self):
        client = make_client()
        with pytest.raises(ConfluenceClientError, match="non-empty query"):
            client.search("")

    def test_wraps_query_in_cql_text_search(self):
        client = make_client()
        client._client.cql.return_value = {"results": []}
        client.search("budget")
        cql = client._client.cql.call_args.args[0]
        assert 'text ~ "budget"' in cql

    def test_maps_results(self):
        client = make_client()
        client._client.cql.return_value = {"results": [{"title": "Doc", "entityType": "page"}]}
        results = client.search("q")
        assert results[0].title == "Doc"

    def test_http_error_becomes_confluence_client_error(self):
        client = make_client()
        client._client.cql.side_effect = RuntimeError("boom")
        with pytest.raises(ConfluenceClientError, match="search\\('q'\\) failed"):
            client.search("q")


class TestCqlSearch:
    def test_requires_cql(self):
        client = make_client()
        with pytest.raises(ConfluenceClientError, match="non-empty CQL"):
            client.cql_search("")

    def test_passes_cql_through_unmodified(self):
        client = make_client()
        client._client.cql.return_value = {"results": []}
        client.cql_search('space = "ENG" and type = "page"')
        assert client._client.cql.call_args.args[0] == 'space = "ENG" and type = "page"'


# ---------------------------------------------------------------------------- #
# list_pages_in_space / get_page / get_page_by_title
# ---------------------------------------------------------------------------- #

class TestListPagesInSpace:
    def test_requires_space_key(self):
        client = make_client()
        with pytest.raises(ConfluenceClientError, match="requires a space_key"):
            client.list_pages_in_space("")

    def test_maps_pages(self):
        client = make_client()
        client._client.get.side_effect = [
            {"results": [{"id": "999", "key": "ENG"}]},  # space id resolution
            {"results": [{"id": "1", "title": "Page"}]},  # pages in space
        ]
        pages = client.list_pages_in_space("ENG")
        assert pages[0].title == "Page"
        assert pages[0].space_key == "ENG"
        assert client._client.get.call_args_list[1].args[0] == "api/v2/spaces/999/pages"

    def test_space_not_found_raises_confluence_client_error(self):
        client = make_client()
        client._client.get.return_value = {"results": []}
        with pytest.raises(ConfluenceClientError, match="Space not found"):
            client.list_pages_in_space("ENG")

    def test_space_not_found_404_raises_confluence_client_error(self):
        # The v2 spaces endpoint 404s (instead of 200 + empty results) when
        # the `keys` filter matches no space.
        client = make_client()
        exc = Exception("404 Client Error: Not Found for url: ...")
        exc.response = MagicMock(status_code=404)
        client._client.get.side_effect = exc
        with pytest.raises(ConfluenceClientError, match="Space not found: 'ENG'"):
            client.list_pages_in_space("ENG")

    def test_http_error_becomes_confluence_client_error(self):
        client = make_client()
        client._client.get.side_effect = [
            {"results": [{"id": "999"}]},
            RuntimeError("boom"),
        ]
        with pytest.raises(ConfluenceClientError, match="list_pages_in_space"):
            client.list_pages_in_space("ENG")


class TestGetPage:
    def test_requires_page_id(self):
        client = make_client()
        with pytest.raises(ConfluenceClientError, match="requires a page_id"):
            client.get_page("")

    def test_fetches_with_body(self):
        client = make_client()
        client._client.get.return_value = {
            "id": "1", "title": "Page", "body": {"storage": {"value": "content"}},
        }
        page = client.get_page("1")
        assert page.body == "content"
        assert client._client.get.call_args.args[0] == "api/v2/pages/1"
        assert client._client.get.call_args.kwargs["params"]["body-format"] == "storage"


class TestGetPageByTitle:
    def test_requires_space_key_and_title(self):
        client = make_client()
        with pytest.raises(ConfluenceClientError, match="requires space_key and title"):
            client.get_page_by_title("", "Title")
        with pytest.raises(ConfluenceClientError, match="requires space_key and title"):
            client.get_page_by_title("ENG", "")

    def test_not_found_raises_confluence_client_error(self):
        client = make_client()
        client._client.get.side_effect = [
            {"results": [{"id": "999"}]},  # space id resolution
            {"results": []},  # no matching page
        ]
        with pytest.raises(ConfluenceClientError, match="Page not found"):
            client.get_page_by_title("ENG", "Nonexistent")

    def test_found_returns_parsed_page(self):
        client = make_client()
        client._client.get.side_effect = [
            {"results": [{"id": "999"}]},
            {"results": [{"id": "1", "title": "Found"}]},
        ]
        page = client.get_page_by_title("ENG", "Found")
        assert page.title == "Found"
        assert page.space_key == "ENG"


# ---------------------------------------------------------------------------- #
# create_page / update_page
# ---------------------------------------------------------------------------- #

class TestCreatePage:
    def test_requires_space_key_and_title(self):
        client = make_client()
        with pytest.raises(ConfluenceClientError, match="requires space_key and title"):
            client.create_page("", "Title", "body")
        with pytest.raises(ConfluenceClientError, match="requires space_key and title"):
            client.create_page("ENG", "", "body")

    def test_creates_and_refetches_full_page(self):
        client = make_client()
        client._client.get.side_effect = [
            {"results": [{"id": "999"}]},  # space id resolution
            {"id": "99", "title": "New Page"},  # refetch after create
        ]
        client._client.post.return_value = {"id": "99"}

        page = client.create_page("ENG", "New Page", "<p>body</p>", parent_id="1")

        create_kwargs = client._client.post.call_args.kwargs
        assert create_kwargs["data"]["spaceId"] == "999"
        assert create_kwargs["data"]["parentId"] == "1"
        assert create_kwargs["data"]["body"] == {"representation": "storage", "value": "<p>body</p>"}
        assert page.title == "New Page"

    def test_no_parent_id_key_omitted(self):
        client = make_client()
        client._client.get.side_effect = [{"results": [{"id": "999"}]}, {"id": "99"}]
        client._client.post.return_value = {"id": "99"}
        client.create_page("ENG", "New Page", "body")
        assert "parentId" not in client._client.post.call_args.kwargs["data"]

    def test_http_error_becomes_confluence_client_error(self):
        client = make_client()
        client._client.get.return_value = {"results": [{"id": "999"}]}
        client._client.post.side_effect = RuntimeError("boom")
        with pytest.raises(ConfluenceClientError, match="create_page failed"):
            client.create_page("ENG", "Title", "body")


class TestUpdatePage:
    def test_requires_page_id_and_title(self):
        client = make_client()
        with pytest.raises(ConfluenceClientError, match="requires page_id and title"):
            client.update_page("", "Title", "body")
        with pytest.raises(ConfluenceClientError, match="requires page_id and title"):
            client.update_page("1", "", "body")

    def test_bumps_version_number_from_current(self):
        client = make_client()
        client._client.get.side_effect = [
            {"version": {"number": 5}},  # fetch current version
            {"id": "1", "title": "Updated", "version": {"number": 6}},  # refetch after update
        ]
        page = client.update_page("1", "Updated", "new body")

        update_kwargs = client._client.put.call_args.kwargs
        assert update_kwargs["data"]["version"]["number"] == 6
        assert page.version == 6

    def test_missing_version_defaults_to_one_then_bumps_to_two(self):
        client = make_client()
        client._client.get.side_effect = [
            {},  # no version field at all
            {"id": "1", "title": "Updated"},
        ]
        client.update_page("1", "Updated", "body")
        assert client._client.put.call_args.kwargs["data"]["version"]["number"] == 2

    def test_http_error_becomes_confluence_client_error(self):
        client = make_client()
        client._client.get.side_effect = RuntimeError("boom")
        with pytest.raises(ConfluenceClientError, match="update_page"):
            client.update_page("1", "Title", "body")


class TestLiveFixtureParsing:
    """Replays fixtures recorded from a real, [QATEST]-tagged seed page by
    scripts/qa_fixture_recorder.py --record confluence -- real API shape,
    not hand-authored, with identity fields already redacted. Skipped
    (not failed) until that fixture exists; see
    tests/fixtures/live/README.md and
    docs/testing-policy.md. Re-record via that
    script if this ever starts failing after a genuine Confluence API
    change.
    """

    def _load(self, name: str) -> dict:
        path = LIVE_FIXTURES_DIR / name
        if not path.exists():
            pytest.skip(
                f"{path} not recorded yet -- run "
                "`python3 scripts/qa_fixture_recorder.py --record confluence` locally first"
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def test_get_page_fixture_still_parses(self):
        client = make_client()
        raw = self._load("get_page.json")
        # get_page.json is the {"results": [...]} envelope from
        # get_page_by_title when the manifest has no seed_page_id yet --
        # unwrap the same way ConfluenceClient.get_page_by_title does.
        page_raw = raw["results"][0] if "results" in raw else raw
        page = client._parse_page_v2(page_raw, include_body=True)
        assert page.title and page.author and page.updated and page.space_key

    def test_list_spaces_fixture_still_parses(self):
        client = make_client()
        raw = self._load("list_spaces.json")
        spaces = [client._parse_space(s) for s in raw.get("results", [])]
        assert spaces, "recorded list_spaces.json has no results"
        assert all(s.key and s.name for s in spaces)
