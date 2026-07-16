"""Tests for JiraClient's ADF parsing, refresh-and-retry token logic, and
result normalization.

The refresh-on-401 behavior (_request/_try_refresh) is a direct regression
target: commit 862ff43 fixed Jira/Confluence forcing re-authentication on
every restart because neither client refreshed its (short-lived) Atlassian
access token. These tests exercise _try_refresh and _request against a real
JiraClient instance (constructing atlassian.Jira for real does no network
I/O) with jira_client_module.atlassian_refresh/load_token_file/
save_token_file monkeypatched at their call sites.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from privacyfence import jira_client as jira_client_module
from privacyfence.atlassian_oauth import AtlassianOAuthError
from privacyfence.jira_client import (
    JiraClient,
    JiraClientError,
    JiraComment,
    JiraIssue,
    JiraProject,
    JiraTransition,
    _text_to_adf,
)

LIVE_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "live" / "jira"


def make_client(config: dict | None = None, token_file: str | None = None) -> JiraClient:
    base = {"access_token": "tok", "cloud_id": "cloud-1", "site_url": "https://acme.atlassian.net"}
    base.update(config or {})
    client = JiraClient(config=base, token_file=token_file)
    client._client = MagicMock()
    return client


def unauthorized_error() -> Exception:
    exc = Exception("401 Unauthorized")
    response = MagicMock()
    response.status_code = 401
    exc.response = response
    return exc


# ---------------------------------------------------------------------------- #
# Construction
# ---------------------------------------------------------------------------- #

class TestConstruction:
    def test_missing_access_token_raises(self):
        with pytest.raises(JiraClientError, match="not authenticated"):
            JiraClient(config={"cloud_id": "c1"})

    def test_missing_cloud_id_raises(self):
        with pytest.raises(JiraClientError, match="not authenticated"):
            JiraClient(config={"access_token": "t"})

    def test_base_url_prefers_site_url_over_api_url(self):
        client = JiraClient(config={"access_token": "t", "cloud_id": "c1", "site_url": "https://acme.atlassian.net/"})
        assert client._base_url == "https://acme.atlassian.net"

    def test_base_url_falls_back_to_api_url_without_site_url(self):
        client = JiraClient(config={"access_token": "t", "cloud_id": "c1"})
        assert client._base_url == "https://api.atlassian.com/ex/jira/c1"


# ---------------------------------------------------------------------------- #
# _text_to_adf
# ---------------------------------------------------------------------------- #

class TestTextToAdf:
    def test_wraps_plain_text_in_single_paragraph(self):
        assert _text_to_adf("hello") == {
            "type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}],
        }


# ---------------------------------------------------------------------------- #
# _extract_adf_text: recursive ADF walking
# ---------------------------------------------------------------------------- #

class TestExtractAdfText:
    def test_single_text_node(self):
        assert JiraClient._extract_adf_text({"type": "text", "text": "hi"}) == "hi"

    def test_paragraph_with_multiple_text_children_joined_with_space(self):
        node = {"type": "paragraph", "content": [
            {"type": "text", "text": "hello"}, {"type": "text", "text": "world"},
        ]}
        assert JiraClient._extract_adf_text(node) == "hello world"

    def test_nested_document_structure(self):
        node = {
            "type": "doc", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "line one"}]},
                {"type": "paragraph", "content": [{"type": "text", "text": "line two"}]},
            ],
        }
        assert JiraClient._extract_adf_text(node) == "line one line two"

    def test_empty_content_yields_empty_string(self):
        assert JiraClient._extract_adf_text({"type": "doc", "content": []}) == ""

    def test_non_dict_input_stringified(self):
        assert JiraClient._extract_adf_text("already a string") == "already a string"


# ---------------------------------------------------------------------------- #
# _parse_project / _parse_issue / _parse_comment
# ---------------------------------------------------------------------------- #

class TestParseProject:
    def test_full_project(self):
        client = make_client()
        raw = {"key": "ENG", "name": "Engineering", "projectTypeKey": "software",
               "description": "desc", "lead": {"displayName": "Jane"}}
        assert client._parse_project(raw) == JiraProject(
            key="ENG", name="Engineering", project_type="software", description="desc", lead="Jane",
        )

    def test_short_summary(self):
        assert JiraProject(key="ENG", name="Engineering").short_summary() == "[ENG] Engineering"


class TestParseIssue:
    def test_basic_fields_without_description(self):
        client = make_client()
        raw = {"key": "ENG-1", "fields": {
            "summary": "Fix bug", "status": {"name": "In Progress"}, "issuetype": {"name": "Bug"},
            "priority": {"name": "High"}, "assignee": {"displayName": "Jane"},
            "reporter": {"displayName": "Bob"}, "labels": ["urgent"],
            "created": "c", "updated": "u",
        }}
        issue = client._parse_issue(raw)
        assert issue.key == "ENG-1"
        assert issue.description == ""
        assert issue.url == "https://acme.atlassian.net/browse/ENG-1"
        assert issue.labels == ["urgent"]

    def test_description_extracted_from_adf_when_requested(self):
        client = make_client()
        raw = {"key": "ENG-1", "fields": {
            "description": {"type": "doc", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "the bug details"}]},
            ]},
        }}
        issue = client._parse_issue(raw, include_description=True)
        assert issue.description == "the bug details"

    def test_plain_string_description_used_as_is(self):
        client = make_client()
        raw = {"key": "ENG-1", "fields": {"description": "plain text description"}}
        issue = client._parse_issue(raw, include_description=True)
        assert issue.description == "plain text description"

    def test_description_omitted_unless_requested(self):
        client = make_client()
        raw = {"key": "ENG-1", "fields": {"description": "should not appear"}}
        issue = client._parse_issue(raw, include_description=False)
        assert issue.description == ""

    def test_missing_key_yields_empty_url(self):
        client = make_client()
        issue = client._parse_issue({"fields": {}})
        assert issue.url == ""

    def test_short_summary_truncates_long_summary(self):
        long_summary = "x" * 100
        issue = JiraIssue(key="ENG-1", summary=long_summary, status="Open", issue_type="Bug")
        assert issue.short_summary().endswith("…")
        assert issue.short_summary().startswith("ENG-1 (Open):")


class TestParseComment:
    def test_adf_body_extracted(self):
        raw = {
            "id": "c1", "author": {"displayName": "Jane"},
            "body": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "nice work"}]}]},
            "created": "c", "updated": "u",
        }
        comment = JiraClient._parse_comment(raw)
        assert comment == JiraComment(id="c1", author="Jane", body="nice work", created="c", updated="u")

    def test_plain_string_body_used_as_is(self):
        comment = JiraClient._parse_comment({"id": "c1", "body": "plain comment"})
        assert comment.body == "plain comment"


# ---------------------------------------------------------------------------- #
# _try_refresh: the reauth-on-restart regression target
# ---------------------------------------------------------------------------- #

class TestTryRefresh:
    def test_missing_client_credentials_returns_false(self):
        client = make_client({"refresh_token": "rt"})
        assert client._try_refresh() is False

    def test_missing_refresh_token_returns_false(self):
        client = make_client({"client_id": "ci", "client_secret": "cs"})
        assert client._try_refresh() is False

    def test_successful_refresh_updates_config_and_session_header(self, monkeypatch):
        monkeypatch.setattr(jira_client_module, "atlassian_refresh",
                             lambda cid, cs, rt: {"access_token": "new-tok", "refresh_token": "new-rt"})
        client = make_client({"client_id": "ci", "client_secret": "cs", "refresh_token": "rt"})

        assert client._try_refresh() is True
        assert client._config["access_token"] == "new-tok"
        assert client._config["refresh_token"] == "new-rt"
        assert client._session.headers["Authorization"] == "Bearer new-tok"

    def test_successful_refresh_persists_to_shared_token_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(jira_client_module, "atlassian_refresh",
                             lambda cid, cs, rt: {"access_token": "new-tok", "refresh_token": "new-rt"})
        token_file = str(tmp_path / "atlassian_token.json")
        client = make_client(
            {"client_id": "ci", "client_secret": "cs", "refresh_token": "rt", "account_email": "me@x.com"},
            token_file=token_file,
        )
        monkeypatch.setattr(
            jira_client_module, "load_token_file",
            lambda path: (_ for _ in ()).throw(AtlassianOAuthError("no file yet")),
        )
        saved = {}
        monkeypatch.setattr(jira_client_module, "save_token_file", lambda path, record: saved.update(record))

        client._try_refresh()

        assert saved == {
            "access_token": "new-tok", "refresh_token": "new-rt",
            "cloud_id": "cloud-1", "site_url": "https://acme.atlassian.net", "account_email": "me@x.com",
        }

    def test_picks_up_refresh_token_already_rotated_by_confluence_client(self, monkeypatch, tmp_path):
        # Jira/Confluence share one token file; if Confluence refreshed first
        # (rotating the refresh token), Jira must use the *new* one rather
        # than retry the now-spent token from its own config.
        captured_refresh_token = {}
        def fake_refresh(cid, cs, rt):
            captured_refresh_token["used"] = rt
            return {"access_token": "new-tok", "refresh_token": "newer-rt"}
        monkeypatch.setattr(jira_client_module, "atlassian_refresh", fake_refresh)
        monkeypatch.setattr(jira_client_module, "load_token_file", lambda path: {"refresh_token": "rotated-by-confluence"})
        monkeypatch.setattr(jira_client_module, "save_token_file", lambda path, record: None)

        client = make_client(
            {"client_id": "ci", "client_secret": "cs", "refresh_token": "stale-rt"},
            token_file=str(tmp_path / "shared_token.json"),
        )

        client._try_refresh()

        assert captured_refresh_token["used"] == "rotated-by-confluence"

    def test_refresh_api_failure_returns_false(self, monkeypatch):
        def raiser(cid, cs, rt):
            raise AtlassianOAuthError("refresh failed")
        monkeypatch.setattr(jira_client_module, "atlassian_refresh", raiser)
        client = make_client({"client_id": "ci", "client_secret": "cs", "refresh_token": "rt"})
        assert client._try_refresh() is False

    def test_refresh_response_without_access_token_returns_false(self, monkeypatch):
        monkeypatch.setattr(jira_client_module, "atlassian_refresh", lambda cid, cs, rt: {})
        client = make_client({"client_id": "ci", "client_secret": "cs", "refresh_token": "rt"})
        assert client._try_refresh() is False


# ---------------------------------------------------------------------------- #
# _request: refresh-and-retry wrapper
# ---------------------------------------------------------------------------- #

class TestRequest:
    def test_happy_path_returns_fn_result(self):
        client = make_client()
        assert client._request(lambda: "ok") == "ok"

    def test_401_triggers_refresh_and_retry_succeeds(self, monkeypatch):
        client = make_client({"client_id": "ci", "client_secret": "cs", "refresh_token": "rt"})
        monkeypatch.setattr(client, "_try_refresh", lambda: True)

        calls = {"n": 0}
        def fn():
            calls["n"] += 1
            if calls["n"] == 1:
                raise unauthorized_error()
            return "retried-ok"

        assert client._request(fn) == "retried-ok"
        assert calls["n"] == 2

    def test_401_but_refresh_fails_reraises_original(self, monkeypatch):
        client = make_client()
        monkeypatch.setattr(client, "_try_refresh", lambda: False)

        def fn():
            raise unauthorized_error()

        with pytest.raises(Exception, match="401"):
            client._request(fn)

    def test_non_401_error_reraises_without_attempting_refresh(self, monkeypatch):
        client = make_client()
        refresh_called = []
        monkeypatch.setattr(client, "_try_refresh", lambda: refresh_called.append(1) or True)

        def fn():
            raise ValueError("some other error")

        with pytest.raises(ValueError):
            client._request(fn)
        assert refresh_called == []


# ---------------------------------------------------------------------------- #
# check_connection / list_projects / search_issues / get_issue /
# get_issue_comments / create_issue / add_comment / update_issue
# ---------------------------------------------------------------------------- #

class TestCheckConnection:
    def test_returns_display_name_and_base_url(self):
        client = make_client()
        client._client.myself.return_value = {"displayName": "Jane Doe"}
        assert client.check_connection() == "Jane Doe @ https://acme.atlassian.net"

    def test_error_becomes_jira_client_error(self):
        client = make_client()
        client._client.myself.side_effect = RuntimeError("boom")
        with pytest.raises(JiraClientError, match="Jira connection check failed"):
            client.check_connection()


class TestListProjects:
    def test_clamps_max_results_after_fetch(self):
        client = make_client()
        client._client.projects.return_value = [{"key": f"P{i}", "name": f"Proj {i}"} for i in range(10)]
        projects = client.list_projects(max_results=3)
        assert len(projects) == 3

    def test_http_error_becomes_jira_client_error(self):
        client = make_client()
        client._client.projects.side_effect = RuntimeError("boom")
        with pytest.raises(JiraClientError, match="list_projects failed"):
            client.list_projects()


class TestSearchIssues:
    def test_requires_jql(self):
        client = make_client()
        with pytest.raises(JiraClientError, match="non-empty JQL"):
            client.search_issues("")

    def test_maps_issues(self):
        client = make_client()
        client._client.jql.return_value = {"issues": [{"key": "ENG-1", "fields": {"summary": "x"}}]}
        issues = client.search_issues("project = ENG")
        assert issues[0].key == "ENG-1"

    def test_http_error_becomes_jira_client_error(self):
        client = make_client()
        client._client.jql.side_effect = RuntimeError("boom")
        with pytest.raises(JiraClientError, match="search_issues failed"):
            client.search_issues("project = ENG")


class TestGetIssue:
    def test_requires_issue_key(self):
        client = make_client()
        with pytest.raises(JiraClientError, match="requires an issue key"):
            client.get_issue("")

    def test_fetches_with_description(self):
        client = make_client()
        client._client.issue.return_value = {"key": "ENG-1", "fields": {"summary": "x", "description": "d"}}
        issue = client.get_issue("ENG-1")
        assert issue.description == "d"


class TestGetIssueComments:
    def test_requires_issue_key(self):
        client = make_client()
        with pytest.raises(JiraClientError, match="requires an issue key"):
            client.get_issue_comments("")

    def test_maps_comments(self):
        client = make_client()
        client._client.issue.return_value = {
            "fields": {"comment": {"comments": [{"id": "c1", "body": "hi", "author": {"displayName": "Jane"}}]}}
        }
        comments = client.get_issue_comments("ENG-1")
        assert comments[0].body == "hi"


class TestCreateIssue:
    def test_requires_project_key_and_summary(self):
        client = make_client()
        with pytest.raises(JiraClientError, match="requires project_key and summary"):
            client.create_issue("", "summary")
        with pytest.raises(JiraClientError, match="requires project_key and summary"):
            client.create_issue("ENG", "")

    def test_builds_fields_and_refetches_created_issue(self):
        client = make_client()
        client._client.create_issue.return_value = {"key": "ENG-99"}
        client._client.issue.return_value = {"key": "ENG-99", "fields": {"summary": "New issue"}}

        issue = client.create_issue(
            "ENG", "New issue", issue_type="Story", description="desc", priority="High",
            assignee_account_id="acc1", labels=["urgent"],
        )

        fields = client._client.create_issue.call_args.kwargs["fields"]
        assert fields["project"] == {"key": "ENG"}
        assert fields["issuetype"] == {"name": "Story"}
        assert fields["priority"] == {"name": "High"}
        assert fields["assignee"] == {"accountId": "acc1"}
        assert fields["labels"] == ["urgent"]
        assert fields["description"] == _text_to_adf("desc")
        assert issue.key == "ENG-99"

    def test_optional_fields_omitted_when_not_given(self):
        client = make_client()
        client._client.create_issue.return_value = {"key": "ENG-99"}
        client._client.issue.return_value = {"key": "ENG-99", "fields": {}}

        client.create_issue("ENG", "New issue")

        fields = client._client.create_issue.call_args.kwargs["fields"]
        assert "description" not in fields
        assert "priority" not in fields
        assert "assignee" not in fields
        assert "labels" not in fields

    def test_http_error_becomes_jira_client_error(self):
        client = make_client()
        client._client.create_issue.side_effect = RuntimeError("boom")
        with pytest.raises(JiraClientError, match="create_issue failed"):
            client.create_issue("ENG", "New issue")


class TestAddComment:
    def test_requires_issue_key_and_body(self):
        client = make_client()
        with pytest.raises(JiraClientError, match="requires issue_key and body"):
            client.add_comment("", "body")
        with pytest.raises(JiraClientError, match="requires issue_key and body"):
            client.add_comment("ENG-1", "")

    def test_sends_adf_body(self):
        client = make_client()
        client._client.issue_add_comment.return_value = {"id": "c1", "body": "hi"}
        client.add_comment("ENG-1", "hi")
        args = client._client.issue_add_comment.call_args.args
        assert args == ("ENG-1", _text_to_adf("hi"))


class TestUpdateIssue:
    def test_requires_issue_key_and_fields(self):
        client = make_client()
        with pytest.raises(JiraClientError, match="requires issue_key and fields"):
            client.update_issue("", {"summary": "x"})
        with pytest.raises(JiraClientError, match="requires issue_key and fields"):
            client.update_issue("ENG-1", {})

    def test_updates_then_refetches(self):
        client = make_client()
        client._client.issue.return_value = {"key": "ENG-1", "fields": {"summary": "Updated"}}
        issue = client.update_issue("ENG-1", {"summary": "Updated"})
        client._client.update_issue_field.assert_called_once_with("ENG-1", {"summary": "Updated"})
        assert issue.summary == "Updated"

    def test_http_error_becomes_jira_client_error(self):
        client = make_client()
        client._client.update_issue_field.side_effect = RuntimeError("boom")
        with pytest.raises(JiraClientError, match="update_issue"):
            client.update_issue("ENG-1", {"summary": "x"})


# ---------------------------------------------------------------------------- #
# resolve_custom_field / _get_field_descriptor: display-name -> id + value shaping
# ---------------------------------------------------------------------------- #

class TestResolveCustomField:
    def test_resolves_id_and_passes_plain_value_through_for_non_option_schema(self):
        client = make_client()
        client._client.get_all_fields.return_value = [
            {"id": "customfield_10016", "name": "Story Points", "schema": {"type": "number"}},
        ]
        field_id, value = client.resolve_custom_field("Story Points", 5)
        assert field_id == "customfield_10016"
        assert value == 5

    def test_option_schema_wraps_value_in_value_dict(self):
        client = make_client()
        client._client.get_all_fields.return_value = [
            {"id": "customfield_10020", "name": "Release Track", "schema": {"type": "option"}},
        ]
        field_id, value = client.resolve_custom_field("Release Track", "Beta")
        assert field_id == "customfield_10020"
        assert value == {"value": "Beta"}

    def test_array_of_option_schema_wraps_each_value(self):
        client = make_client()
        client._client.get_all_fields.return_value = [
            {"id": "customfield_10030", "name": "Components", "schema": {"type": "array", "items": "option"}},
        ]
        field_id, value = client.resolve_custom_field("Components", ["Backend", "Frontend"])
        assert field_id == "customfield_10030"
        assert value == [{"value": "Backend"}, {"value": "Frontend"}]

    def test_array_of_option_schema_wraps_single_scalar_into_a_list(self):
        client = make_client()
        client._client.get_all_fields.return_value = [
            {"id": "customfield_10030", "name": "Components", "schema": {"type": "array", "items": "option"}},
        ]
        _field_id, value = client.resolve_custom_field("Components", "Backend")
        assert value == [{"value": "Backend"}]

    def test_generic_array_schema_wraps_scalar_but_not_list(self):
        client = make_client()
        client._client.get_all_fields.return_value = [
            {"id": "customfield_10040", "name": "Tags", "schema": {"type": "array", "items": "string"}},
        ]
        _field_id, scalar_value = client.resolve_custom_field("Tags", "one")
        assert scalar_value == ["one"]
        _field_id, list_value = client.resolve_custom_field("Tags", ["one", "two"])
        assert list_value == ["one", "two"]

    def test_lookup_is_case_insensitive(self):
        client = make_client()
        client._client.get_all_fields.return_value = [
            {"id": "customfield_10016", "name": "Story Points", "schema": {"type": "number"}},
        ]
        field_id, _value = client.resolve_custom_field("story points", 3)
        assert field_id == "customfield_10016"

    def test_field_list_fetched_only_once_across_calls(self):
        client = make_client()
        client._client.get_all_fields.return_value = [
            {"id": "customfield_10016", "name": "Story Points", "schema": {"type": "number"}},
        ]
        client.resolve_custom_field("Story Points", 1)
        client.resolve_custom_field("Story Points", 2)
        client._client.get_all_fields.assert_called_once()

    def test_unknown_field_name_raises(self):
        client = make_client()
        client._client.get_all_fields.return_value = [
            {"id": "customfield_10016", "name": "Story Points", "schema": {"type": "number"}},
        ]
        with pytest.raises(JiraClientError, match="no Jira field named"):
            client.resolve_custom_field("Not A Real Field", 1)

    def test_ambiguous_field_name_raises(self):
        client = make_client()
        client._client.get_all_fields.return_value = [
            {"id": "customfield_10016", "name": "Team", "schema": {"type": "string"}},
            {"id": "customfield_10099", "name": "Team", "schema": {"type": "string"}},
        ]
        with pytest.raises(JiraClientError, match="multiple Jira fields are named"):
            client.resolve_custom_field("Team", "Platform")

    def test_field_list_fetch_error_becomes_jira_client_error(self):
        client = make_client()
        client._client.get_all_fields.side_effect = RuntimeError("boom")
        with pytest.raises(JiraClientError, match="failed to list Jira fields"):
            client.resolve_custom_field("Story Points", 1)


# ---------------------------------------------------------------------------- #
# get_transitions / transition_issue
# ---------------------------------------------------------------------------- #

class TestGetTransitions:
    def test_requires_issue_key(self):
        client = make_client()
        with pytest.raises(JiraClientError, match="requires an issue key"):
            client.get_transitions("")

    def test_maps_transitions(self):
        client = make_client()
        client._client.get_issue_transitions.return_value = [
            {"id": 11, "name": "Start Progress", "to": "In Progress"},
            {"id": 21, "name": "Done", "to": "Done"},
        ]
        transitions = client.get_transitions("ENG-1")
        assert transitions == [
            JiraTransition(id="11", name="Start Progress", to_status="In Progress"),
            JiraTransition(id="21", name="Done", to_status="Done"),
        ]

    def test_http_error_becomes_jira_client_error(self):
        client = make_client()
        client._client.get_issue_transitions.side_effect = RuntimeError("boom")
        with pytest.raises(JiraClientError, match="get_transitions\\(.*\\) failed"):
            client.get_transitions("ENG-1")


class TestTransitionIssue:
    def test_requires_issue_key_and_transition_name(self):
        client = make_client()
        with pytest.raises(JiraClientError, match="requires issue_key and transition_name"):
            client.transition_issue("", "Done")
        with pytest.raises(JiraClientError, match="requires issue_key and transition_name"):
            client.transition_issue("ENG-1", "")

    def test_transitions_by_id_and_refetches(self):
        client = make_client()
        client._client.get_issue_transitions.return_value = [
            {"id": 21, "name": "Done", "to": "Done"},
        ]
        client._client.issue.return_value = {"key": "ENG-1", "fields": {"summary": "x", "status": {"name": "Done"}}}

        issue = client.transition_issue("ENG-1", "Done")

        client._client.set_issue_status_by_transition_id.assert_called_once_with("ENG-1", "21")
        assert issue.status == "Done"

    def test_transition_name_lookup_is_case_insensitive(self):
        client = make_client()
        client._client.get_issue_transitions.return_value = [
            {"id": 21, "name": "Done", "to": "Done"},
        ]
        client._client.issue.return_value = {"key": "ENG-1", "fields": {}}

        client.transition_issue("ENG-1", "done")

        client._client.set_issue_status_by_transition_id.assert_called_once_with("ENG-1", "21")

    def test_invalid_transition_name_raises_with_available_list(self):
        client = make_client()
        client._client.get_issue_transitions.return_value = [
            {"id": 11, "name": "Start Progress", "to": "In Progress"},
            {"id": 21, "name": "Done", "to": "Done"},
        ]
        with pytest.raises(JiraClientError, match="Available: Start Progress, Done"):
            client.transition_issue("ENG-1", "Cancelled")

    def test_transition_api_error_becomes_jira_client_error(self):
        client = make_client()
        client._client.get_issue_transitions.return_value = [{"id": 21, "name": "Done", "to": "Done"}]
        client._client.set_issue_status_by_transition_id.side_effect = RuntimeError("boom")
        with pytest.raises(JiraClientError, match="transition_issue\\(.*\\) failed"):
            client.transition_issue("ENG-1", "Done")


class TestLiveFixtureParsing:
    """Replays fixtures recorded from a real, [QATEST]-tagged seed issue by
    scripts/qa_fixture_recorder.py --record jira -- real API shape, not
    hand-authored, with identity fields already redacted. Skipped (not
    failed) until that fixture exists; see tests/fixtures/live/README.md and
    docs/testing-policy.md. Re-record via that
    script if this ever starts failing after a genuine Jira API change.
    """

    def _load(self, name: str) -> dict:
        path = LIVE_FIXTURES_DIR / name
        if not path.exists():
            pytest.skip(
                f"{path} not recorded yet -- run "
                "`python3 scripts/qa_fixture_recorder.py --record jira` locally first"
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def test_get_issue_fixture_still_parses(self):
        client = make_client()
        raw = self._load("get_issue.json")
        issue = client._parse_issue(raw, include_description=True)
        assert issue.key and issue.summary and issue.status

    def test_list_projects_fixture_still_parses(self):
        client = make_client()
        raw = self._load("list_projects.json")
        projects = [client._parse_project(p) for p in raw]
        assert projects, "recorded list_projects.json has no results"
        assert all(p.key and p.name for p in projects)
