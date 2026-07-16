"""Unit tests for privacyfence.connectors.confluence.ConfluenceConnector.

Same approach as the other connector tests: ConfluenceClient is mocked
and gate.gated_call is stubbed to capture what's sent into the gate.

One real bug found and fixed while writing these: confluence_get_page and
confluence_get_page_by_title built the "Last modified" preview field from
getattr(page, "last_modified", ""), but ConfluencePage has no
last_modified attribute -- only `updated` -- so that field was always
blank, even though README documents "last modified" as part of the
Cowork preview.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.confluence_client import (
    ConfluenceClient,
    ConfluenceClientError,
    ConfluencePage,
    ConfluenceSearchResult,
    ConfluenceSpace,
)
from privacyfence.connectors import confluence as confluence_module
from privacyfence.connectors.confluence import ConfluenceConnector

from ...helpers import assert_all_tools_leave_an_audit_trail, assert_no_placeholder_fields


def make_connector(my_email="me@example.com"):
    client = MagicMock()
    connector = ConfluenceConnector(client)
    connector.my_email = my_email
    return connector, client


def make_real_client(config: dict | None = None) -> ConfluenceClient:
    """A real ConfluenceClient (real _parse_page_v2 and friends) with only
    the underlying atlassian-python-api object mocked -- same pattern as
    test_confluence_client.py's make_client(). Used by TestFieldCompleteness
    to exercise the real raw-response -> dataclass -> popup-preview path end
    to end, instead of stubbing ConfluencePage directly like every other
    test in this file does.
    """
    base = {"access_token": "tok", "cloud_id": "cloud-1", "site_url": "https://acme.atlassian.net"}
    base.update(config or {})
    client = ConfluenceClient(config=base)
    client._client = MagicMock()
    return client


def make_page(**overrides):
    defaults = dict(
        id="p1", title="Runbook", space_key="ENG", space_name="Engineering",
        version=3, author="alice@example.com", created="2026-01-01T00:00:00Z",
        updated="2026-07-01T00:00:00Z", body="<p>Confidential steps here</p>",
    )
    defaults.update(overrides)
    return ConfluencePage(**defaults)


@pytest.fixture
def gated_call_spy(monkeypatch):
    calls = []

    async def fake_gated_call(**kwargs):
        calls.append(kwargs)
        return kwargs["filtered_data"]

    monkeypatch.setattr(confluence_module, "gated_call", fake_gated_call)
    return calls


class TestDispatch:
    async def test_unknown_tool_raises(self):
        connector, _client = make_connector()
        with pytest.raises(ValueError, match="Unknown Confluence tool"):
            await connector.call("confluence_does_not_exist", {})


class TestAutoTools:
    async def test_list_spaces(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_spaces.return_value = [ConfluenceSpace(key="ENG", name="Engineering")]

        result = await connector.call("confluence_list_spaces", {"max_results": 10, "space_type": "global"})

        assert result[0]["key"] == "ENG"
        client.list_spaces.assert_called_once_with(10, "global")
        entries = (tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()
        assert '"decision": "auto_accepted"' in entries[0]

    async def test_list_spaces_default_omits_type_filter(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_spaces.return_value = []

        await connector.call("confluence_list_spaces", {})

        client.list_spaces.assert_called_once_with(50, "")

    async def test_search(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.search.return_value = [
            ConfluenceSearchResult(id="s1", title="Runbook", content_type="page", space_key="ENG"),
        ]

        result = await connector.call("confluence_search", {"query": "runbook"})

        assert result[0]["title"] == "Runbook"
        client.search.assert_called_once_with("runbook", 20)

    async def test_cql_search(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.cql_search.return_value = []

        result = await connector.call("confluence_cql_search", {"cql": "space = ENG"})

        assert result == []
        client.cql_search.assert_called_once_with("space = ENG", 20)

    async def test_list_pages(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_pages_in_space.return_value = [make_page()]

        result = await connector.call("confluence_list_pages", {"space_key": "ENG"})

        assert result[0]["id"] == "p1"
        client.list_pages_in_space.assert_called_once_with("ENG", 20)

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.list_spaces.side_effect = ConfluenceClientError("no access")

        with pytest.raises(RuntimeError, match="no access"):
            await connector.call("confluence_list_spaces", {})


class TestGetPage:
    async def test_preview_shows_real_last_modified_not_blank(self, gated_call_spy):
        # Regression test for the last_modified bug: preview must reflect
        # page.updated, not silently be blank.
        connector, client = make_connector()
        client.get_page.return_value = make_page(updated="2026-07-01T00:00:00Z")

        await connector.call("confluence_get_page", {"page_id": "p1"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Last modified"] == "2026-07-01T00:00:00Z"

    async def test_last_modified_placeholder_when_missing(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page(updated="")

        await connector.call("confluence_get_page", {"page_id": "p1"})

        assert gated_call_spy[0]["preview"]["Last modified"] == "(unknown)"

    async def test_preview_excludes_body_details_include_it(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page(body="<p>Confidential steps here</p>")

        await connector.call("confluence_get_page", {"page_id": "p1"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {
            "Title": "Runbook", "Space": "ENG", "Author": "alice@example.com",
            "Last modified": "2026-07-01T00:00:00Z",
        }
        assert "Confidential steps here" not in str(kwargs["preview"])
        assert "Confidential steps here" in kwargs["details_text"]
        assert kwargs["gate"] == "review"
        assert kwargs["args"] == {"page_id": "p1"}
        assert kwargs["raw_data"] is kwargs["filtered_data"]

    async def test_pii_scan_text_is_body_only_not_author(self, gated_call_spy):
        # author defaults to an email address, present on every page
        # regardless of content -- the PII scan must not see it.
        connector, client = make_connector()
        client.get_page.return_value = make_page(body="nothing sensitive here")

        await connector.call("confluence_get_page", {"page_id": "p1"})

        kwargs = gated_call_spy[0]
        assert kwargs["pii_scan_text"] == "nothing sensitive here"
        assert kwargs["preview"]["Author"] == "alice@example.com"  # still shown in the popup
        assert "alice@example.com" not in kwargs["pii_scan_text"]

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.get_page.side_effect = ConfluenceClientError("page deleted")

        with pytest.raises(RuntimeError, match="page deleted"):
            await connector.call("confluence_get_page", {"page_id": "p1"})


class TestGetPageByTitle:
    async def test_preview_and_gate(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page_by_title.return_value = make_page()

        await connector.call("confluence_get_page_by_title", {"space_key": "ENG", "title": "Runbook"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Last modified"] == "2026-07-01T00:00:00Z"
        assert kwargs["args"] == {"space_key": "ENG", "title": "Runbook"}
        client.get_page_by_title.assert_called_once_with("ENG", "Runbook")

    async def test_sender_falls_back_to_space_key_when_author_unknown(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page_by_title.return_value = make_page(author="")

        await connector.call("confluence_get_page_by_title", {"space_key": "ENG", "title": "Runbook"})

        assert gated_call_spy[0]["sender"] == "ENG"


class TestCreatePage:
    async def test_preview_omits_parent_id_when_absent(self, gated_call_spy):
        connector, client = make_connector()
        client.create_page.return_value = make_page(id="new1")

        await connector.call("confluence_create_page", {
            "space_key": "ENG", "title": "New Page", "body": "<p>content</p>",
        })

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {"Space": "ENG", "Title": "New Page"}
        assert kwargs["gate"] == "popup"
        assert kwargs["details_text"] == "<p>content</p>"

    async def test_preview_includes_parent_id_when_present(self, gated_call_spy):
        connector, client = make_connector()
        client.create_page.return_value = make_page(id="new1")

        await connector.call("confluence_create_page", {
            "space_key": "ENG", "title": "New Page", "body": "<p>x</p>", "parent_id": "p0",
        })

        assert gated_call_spy[0]["preview"]["Parent page ID"] == "p0"
        assert gated_call_spy[0]["args"] == {"space_key": "ENG", "title": "New Page", "parent_id": "p0"}

    async def test_result_is_serialized_page(self, gated_call_spy):
        connector, client = make_connector()
        client.create_page.return_value = make_page(id="new1", title="New Page")

        result = await connector.call("confluence_create_page", {
            "space_key": "ENG", "title": "New Page", "body": "<p>x</p>",
        })

        assert result["id"] == "new1"
        client.create_page.assert_called_once_with("ENG", "New Page", "<p>x</p>", "")


class TestUpdatePage:
    async def test_preview_shows_title_diff_when_changed(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page(title="Old Title", space_key="ENG")
        client.update_page.return_value = make_page(title="New Title")

        await connector.call("confluence_update_page", {"page_id": "p1", "title": "New Title", "body": "<p>x</p>"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Title"] == "Old Title → New Title"
        assert kwargs["gate"] == "popup"

    async def test_preview_shows_plain_title_when_unchanged(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page(title="Same Title")
        client.update_page.return_value = make_page(title="Same Title")

        await connector.call("confluence_update_page", {"page_id": "p1", "title": "Same Title", "body": "<p>x</p>"})

        assert gated_call_spy[0]["preview"]["Title"] == "Same Title"

    async def test_result_is_serialized_page(self, gated_call_spy):
        connector, client = make_connector()
        client.get_page.return_value = make_page()
        client.update_page.return_value = make_page(title="Updated")

        result = await connector.call("confluence_update_page", {"page_id": "p1", "title": "Updated", "body": "<p>x</p>"})

        assert result["title"] == "Updated"
        client.update_page.assert_called_once_with("p1", "Updated", "<p>x</p>")

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.get_page.side_effect = ConfluenceClientError("locked")

        with pytest.raises(RuntimeError, match="locked"):
            await connector.call("confluence_update_page", {"page_id": "p1", "title": "x", "body": "y"})


class TestFieldCompleteness:
    """End to end: a fully-populated raw v2 API response -> the real
    ConfluenceClient._parse_page_v2 -> the real connector's popup preview
    -- not a hand-built ConfluencePage, unlike every other test in this
    file. This is the shape of check that would have caught the
    last_modified bug (see module docstring) without already knowing it
    existed: assert_no_placeholder_fields fails loudly the moment any
    preview field silently degrades to a fallback.
    """

    async def test_get_page_preview_has_no_placeholder_fields(self, gated_call_spy):
        client = make_real_client()
        # First _client.get() call is get_page's own fetch; the second is
        # _resolve_space_key's follow-up lookup for the human-readable key
        # (v2 pages only carry a numeric spaceId).
        client._client.get.side_effect = [
            {
                "id": "123", "title": "My Page", "spaceId": "999",
                "version": {"number": 3, "createdAt": "2026-07-01T00:00:00Z"},
                "authorId": "acc-1", "createdAt": "2026-01-01T00:00:00Z",
                "_links": {"webui": "/spaces/ENG/pages/123"},
                "body": {"storage": {"value": "<p>content</p>"}},
            },
            {"key": "ENG"},
        ]

        connector = ConfluenceConnector(client)
        connector.my_email = "me@example.com"
        await connector.call("confluence_get_page", {"page_id": "123"})

        assert_no_placeholder_fields(gated_call_spy[0]["preview"])


class TestEveryToolIsAudited:
    async def test_every_declared_tool_leaves_an_audit_trail(self, monkeypatch, tmp_path):
        connector, client = make_connector()
        # get_page/get_page_by_title/create_page/update_page results are
        # asdict()'d unconditionally -- need real ConfluencePage instances,
        # not a bare MagicMock.
        client.get_page.return_value = make_page()
        client.get_page_by_title.return_value = make_page()
        client.create_page.return_value = make_page()
        client.update_page.return_value = make_page()

        await assert_all_tools_leave_an_audit_trail(connector, confluence_module, monkeypatch, tmp_path)
