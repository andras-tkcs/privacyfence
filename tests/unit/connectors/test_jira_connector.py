"""Unit tests for privacyfence.connectors.jira.JiraConnector.

Same approach as the other connector tests: JiraClient is mocked and
gate.gated_call is stubbed to capture what's sent into the gate.
JiraIssue keeps reporter/assignee/description as flat top-level fields
(unlike SalesforceRecord's nested "fields" dict), so asdict(issue)
already matches what auto_accept's _rule_i_am_reporter/_rule_i_am_assignee
expect -- no equivalent bug here.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connectors import jira as jira_module
from privacyfence.connectors.jira import JiraConnector
from privacyfence.jira_client import (
    JiraClient,
    JiraClientError,
    JiraComment,
    JiraIssue,
    JiraProject,
    JiraTransition,
)

from ...helpers import assert_all_tools_leave_an_audit_trail, assert_no_placeholder_fields


def make_connector(my_email="me@example.com"):
    client = MagicMock()
    connector = JiraConnector(client)
    connector.my_email = my_email
    return connector, client


def make_real_client(config: dict | None = None) -> JiraClient:
    """A real JiraClient (real _parse_issue and friends) with only the
    underlying atlassian-python-api object mocked -- same pattern as
    test_jira_client.py's make_client(). Used by TestFieldCompleteness to
    exercise the real raw-response -> dataclass -> popup-preview path end
    to end, instead of a hand-built JiraIssue like every other test here.
    """
    base = {"access_token": "tok", "cloud_id": "cloud-1", "site_url": "https://acme.atlassian.net"}
    base.update(config or {})
    client = JiraClient(config=base)
    client._client = MagicMock()
    return client


def make_issue(**overrides):
    defaults = dict(
        key="ENG-42", summary="Fix login bug", status="In Progress", issue_type="Bug",
        priority="High", assignee="bob@example.com", reporter="alice@example.com",
        description="Users can't log in with SSO.",
    )
    defaults.update(overrides)
    return JiraIssue(**defaults)


@pytest.fixture
def gated_call_spy(monkeypatch):
    calls = []

    async def fake_gated_call(**kwargs):
        calls.append(kwargs)
        return kwargs["filtered_data"]

    monkeypatch.setattr(jira_module, "gated_call", fake_gated_call)
    return calls


class TestDispatch:
    async def test_unknown_tool_raises(self):
        connector, _client = make_connector()
        with pytest.raises(ValueError, match="Unknown Jira tool"):
            await connector.call("jira_does_not_exist", {})


class TestAutoTools:
    async def test_list_projects(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_projects.return_value = [JiraProject(key="ENG", name="Engineering")]

        result = await connector.call("jira_list_projects", {"max_results": 10})

        assert result == [{"key": "ENG", "name": "Engineering", "project_type": "", "description": "", "lead": ""}]
        client.list_projects.assert_called_once_with(10)
        entries = (tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()
        assert '"decision": "auto_accepted"' in entries[0]

    async def test_search_issues(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.search_issues.return_value = [make_issue()]

        result = await connector.call("jira_search_issues", {"jql": "project = ENG", "max_results": 5})

        assert result[0]["key"] == "ENG-42"
        client.search_issues.assert_called_once_with("project = ENG", 5)

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.list_projects.side_effect = JiraClientError("unauthorized")

        with pytest.raises(RuntimeError, match="unauthorized"):
            await connector.call("jira_list_projects", {})

    async def test_get_transitions(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.get_transitions.return_value = [
            JiraTransition(id="11", name="Start Progress", to_status="In Progress"),
        ]

        result = await connector.call("jira_get_transitions", {"issue_key": "ENG-42"})

        assert result == [{"id": "11", "name": "Start Progress", "to_status": "In Progress"}]
        client.get_transitions.assert_called_once_with("ENG-42")
        entries = (tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()
        assert '"decision": "auto_accepted"' in entries[-1]


class TestGetIssue:
    async def test_preview_fields(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.get_issue_comments.return_value = [
            JiraComment(id="c1", author="bob@example.com", body="Looking into it", created="2026-07-01"),
        ]

        await connector.call("jira_get_issue", {"issue_key": "ENG-42"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {
            "Project": "ENG",  # derived from issue_key prefix (JiraIssue has no project_name field)
            "Key": "ENG-42", "Summary": "Fix login bug",
            "Status": "In Progress", "Assignee": "bob@example.com",
        }
        assert kwargs["gate"] == "review"
        assert kwargs["args"] == {"issue_key": "ENG-42"}
        assert kwargs["sender"] == "alice@example.com"  # reporter takes priority

    async def test_summary_truncated_in_preview_but_full_in_details(self, gated_call_spy):
        connector, client = make_connector()
        long_summary = "x" * 100
        client.get_issue.return_value = make_issue(summary=long_summary)
        client.get_issue_comments.return_value = []

        await connector.call("jira_get_issue", {"issue_key": "ENG-42"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Summary"] == "x" * 80 + "…"
        assert long_summary in kwargs["details_text"]

    async def test_unassigned_shows_placeholder(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue(assignee="")
        client.get_issue_comments.return_value = []

        await connector.call("jira_get_issue", {"issue_key": "ENG-42"})

        assert gated_call_spy[0]["preview"]["Assignee"] == "(unassigned)"

    async def test_sender_falls_back_to_assignee_then_issue_key(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue(reporter="", assignee="bob@example.com")
        client.get_issue_comments.return_value = []
        await connector.call("jira_get_issue", {"issue_key": "ENG-42"})
        assert gated_call_spy[0]["sender"] == "bob@example.com"

        client.get_issue.return_value = make_issue(reporter="", assignee="")
        await connector.call("jira_get_issue", {"issue_key": "ENG-99"})
        assert gated_call_spy[1]["sender"] == "ENG-99"

    async def test_result_includes_comments_and_matches_raw_and_filtered(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.get_issue_comments.return_value = [
            JiraComment(id="c1", author="bob@example.com", body="ack", created="2026-07-01"),
        ]

        result = await connector.call("jira_get_issue", {"issue_key": "ENG-42"})

        assert result["comments"] == [{"id": "c1", "author": "bob@example.com", "body": "ack",
                                        "created": "2026-07-01", "updated": ""}]
        assert result["key"] == "ENG-42"
        kwargs = gated_call_spy[0]
        assert kwargs["raw_data"] is kwargs["filtered_data"]

    async def test_pii_scan_text_is_description_and_comments_only(self, gated_call_spy):
        # reporter/assignee/comment author default to email addresses,
        # present on every issue regardless of content -- the PII scan
        # must not see them, only the description and comment bodies.
        connector, client = make_connector()
        client.get_issue.return_value = make_issue(description="nothing sensitive")
        client.get_issue_comments.return_value = [
            JiraComment(id="c1", author="bob@example.com", body="still nothing sensitive", created="2026-07-01"),
        ]

        await connector.call("jira_get_issue", {"issue_key": "ENG-42"})

        kwargs = gated_call_spy[0]
        assert "nothing sensitive" in kwargs["pii_scan_text"]
        assert "still nothing sensitive" in kwargs["pii_scan_text"]
        assert "alice@example.com" in kwargs["details_text"]  # reporter, still shown in the popup
        assert "alice@example.com" not in kwargs["pii_scan_text"]
        assert "bob@example.com" not in kwargs["pii_scan_text"]  # assignee and comment author

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.get_issue.side_effect = JiraClientError("issue not found")

        with pytest.raises(RuntimeError, match="issue not found"):
            await connector.call("jira_get_issue", {"issue_key": "ENG-1"})


class TestCreateIssue:
    async def test_preview_omits_priority_when_absent(self, gated_call_spy):
        connector, client = make_connector()
        client.create_issue.return_value = make_issue(key="ENG-100")

        await connector.call("jira_create_issue", {
            "project_key": "ENG", "summary": "New bug", "issue_type": "Bug",
        })

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {"Project": "ENG", "Type": "Bug", "Summary": "New bug"}
        assert kwargs["gate"] == "popup"
        assert "Priority" not in kwargs["preview"]

    async def test_preview_includes_priority_when_present(self, gated_call_spy):
        connector, client = make_connector()
        client.create_issue.return_value = make_issue(key="ENG-100")

        await connector.call("jira_create_issue", {
            "project_key": "ENG", "summary": "New bug", "priority": "High",
        })

        assert gated_call_spy[0]["preview"]["Priority"] == "High"

    async def test_result_is_serialized_issue(self, gated_call_spy):
        connector, client = make_connector()
        client.create_issue.return_value = make_issue(key="ENG-100")

        result = await connector.call("jira_create_issue", {"project_key": "ENG", "summary": "New bug"})

        assert result["key"] == "ENG-100"
        client.create_issue.assert_called_once_with("ENG", "New bug", "Task", "", "")


class TestAddComment:
    async def test_preview_and_gate(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.add_comment.return_value = JiraComment(id="c2", author="me@example.com", body="ack")

        result = await connector.call("jira_add_comment", {"issue_key": "ENG-42", "body": "On it"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {"Issue": "ENG-42 — Fix login bug"}
        assert kwargs["gate"] == "popup"
        assert kwargs["details_text"] == "On it"
        assert result["id"] == "c2"
        client.add_comment.assert_called_once_with("ENG-42", "On it")


class TestUpdateIssue:
    async def test_requires_at_least_one_field(self):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()

        with pytest.raises(ValueError, match="at least one field"):
            await connector.call("jira_update_issue", {"issue_key": "ENG-42"})

    async def test_preview_shows_summary_diff(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue(summary="Old summary")
        client.update_issue.return_value = make_issue(summary="New summary")

        await connector.call("jira_update_issue", {"issue_key": "ENG-42", "summary": "New summary"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Summary"] == "Old summary → New summary"
        assert kwargs["gate"] == "popup"
        # Regression: details_text used to unconditionally echo the (here,
        # empty) description argument, which gate.py's fallback turns into
        # a raw JSON dump of the update payload instead of a plain-language line.
        assert kwargs["details_text"] == "Summary will be updated; description is unchanged."
        assert "{" not in kwargs["details_text"]

    async def test_description_preview_is_placeholder_not_full_text(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.update_issue.return_value = make_issue()

        await connector.call("jira_update_issue", {"issue_key": "ENG-42", "description": "Confidential new details"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Description"] == "(updated — see below)"
        assert "Confidential new details" not in str(kwargs["preview"])
        assert kwargs["details_text"] == "Confidential new details"

    async def test_priority_sent_as_name_dict(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.update_issue.return_value = make_issue()

        await connector.call("jira_update_issue", {"issue_key": "ENG-42", "priority": "Low"})

        client.update_issue.assert_called_once_with("ENG-42", {"priority": {"name": "Low"}})
        assert gated_call_spy[0]["preview"]["Priority"] == "→ Low"
        assert gated_call_spy[0]["details_text"] == "Priority will be updated; description is unchanged."

    async def test_multi_field_update_lists_all_changed_fields_in_details(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue(summary="Old summary")
        client.update_issue.return_value = make_issue()

        await connector.call(
            "jira_update_issue", {"issue_key": "ENG-42", "summary": "New summary", "priority": "High"},
        )

        assert gated_call_spy[0]["details_text"] == (
            "Summary, Priority will be updated; description is unchanged."
        )

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.get_issue.side_effect = JiraClientError("not found")

        with pytest.raises(RuntimeError, match="not found"):
            await connector.call("jira_update_issue", {"issue_key": "ENG-1", "summary": "x"})

    async def test_custom_fields_resolved_by_name_and_shown_in_preview(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.update_issue.return_value = make_issue()
        client.resolve_custom_field.return_value = ("customfield_10016", 5)

        await connector.call("jira_update_issue", {
            "issue_key": "ENG-42", "custom_fields": '{"Story Points": 5}',
        })

        client.resolve_custom_field.assert_called_once_with("Story Points", 5)
        client.update_issue.assert_called_once_with("ENG-42", {"customfield_10016": 5})
        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Story Points"] == "→ 5"
        assert kwargs["details_text"] == "Story Points will be updated; description is unchanged."

    async def test_custom_fields_combine_with_standard_fields(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue(summary="Old summary")
        client.update_issue.return_value = make_issue()
        client.resolve_custom_field.return_value = ("customfield_10016", 5)

        await connector.call("jira_update_issue", {
            "issue_key": "ENG-42", "summary": "New summary", "custom_fields": '{"Story Points": 5}',
        })

        client.update_issue.assert_called_once_with(
            "ENG-42", {"summary": "New summary", "customfield_10016": 5}
        )
        assert gated_call_spy[0]["details_text"] == (
            "Summary, Story Points will be updated; description is unchanged."
        )

    async def test_invalid_custom_fields_json_raises(self):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()

        with pytest.raises(ValueError, match="custom_fields must be a JSON object"):
            await connector.call("jira_update_issue", {"issue_key": "ENG-42", "custom_fields": "not json"})

    async def test_custom_fields_json_array_raises(self):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()

        with pytest.raises(ValueError, match="custom_fields must be a JSON object"):
            await connector.call("jira_update_issue", {"issue_key": "ENG-42", "custom_fields": "[1, 2]"})

    async def test_unresolvable_custom_field_propagates_as_runtime_error(self):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.resolve_custom_field.side_effect = JiraClientError("no Jira field named 'Nope'")

        with pytest.raises(RuntimeError, match="no Jira field named"):
            await connector.call("jira_update_issue", {
                "issue_key": "ENG-42", "custom_fields": '{"Nope": 1}',
            })


class TestTransitionIssue:
    async def test_preview_and_gate(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue(status="In Progress")
        client.transition_issue.return_value = make_issue(status="Done")

        result = await connector.call(
            "jira_transition_issue", {"issue_key": "ENG-42", "transition_name": "Done"}
        )

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {
            "Issue": "ENG-42 — Fix login bug", "Status": "In Progress → Done",
        }
        assert kwargs["gate"] == "popup"
        assert result["status"] == "Done"
        client.transition_issue.assert_called_once_with("ENG-42", "Done")

    async def test_invalid_transition_propagates_as_runtime_error(self, gated_call_spy):
        connector, client = make_connector()
        client.get_issue.return_value = make_issue()
        client.transition_issue.side_effect = JiraClientError(
            "'Cancelled' is not a valid transition. Available: Done"
        )

        with pytest.raises(RuntimeError, match="not a valid transition"):
            await connector.call(
                "jira_transition_issue", {"issue_key": "ENG-42", "transition_name": "Cancelled"}
            )

    async def test_client_error_on_get_issue_becomes_runtime_error(self):
        connector, client = make_connector()
        client.get_issue.side_effect = JiraClientError("not found")

        with pytest.raises(RuntimeError, match="not found"):
            await connector.call(
                "jira_transition_issue", {"issue_key": "ENG-1", "transition_name": "Done"}
            )


class TestFieldCompleteness:
    """End to end: a fully-populated raw REST response -> the real
    JiraClient._parse_issue -> the real connector's popup preview -- not a
    hand-built JiraIssue, unlike every other test in this file. Mirrors
    test_confluence_connector.py's TestFieldCompleteness, the shape of
    check that would catch a _parse_* field mapping silently degrading to
    a fallback before it ships, not after.
    """

    async def test_get_issue_preview_has_no_placeholder_fields(self, gated_call_spy):
        client = make_real_client()
        # jira_client.py's get_issue and get_issue_comments both call
        # self._client.issue(...) (with different args) -- one raw response
        # with both the full fields and an (empty) comment list satisfies
        # both call sites.
        client._client.issue.return_value = {
            "key": "PFQA-1",
            "fields": {
                "summary": "PrivacyFence QA seed issue [QATEST]",
                "status": {"name": "To Do"},
                "issuetype": {"name": "Task"},
                "reporter": {"displayName": "Real Reporter"},
                "assignee": {"displayName": "Real Assignee"},
                "description": "Synthetic PrivacyFence QA test issue.",
                "created": "2026-01-01T00:00:00Z",
                "updated": "2026-07-01T00:00:00Z",
                "comment": {"comments": []},
            },
        }

        connector = JiraConnector(client)
        connector.my_email = "me@example.com"
        await connector.call("jira_get_issue", {"issue_key": "PFQA-1"})

        assert_no_placeholder_fields(gated_call_spy[0]["preview"])


class TestEveryToolIsAudited:
    async def test_every_declared_tool_leaves_an_audit_trail(self, monkeypatch, tmp_path):
        connector, client = make_connector()
        # get_issue/create_issue/add_comment/update_issue/transition_issue results
        # are asdict()'d unconditionally, so they need real dataclass instances --
        # a bare MagicMock isn't a dataclass. jira_update_issue also validates that
        # at least one field is being changed before reaching the gate, so its
        # stub args need a non-empty field.
        client.get_issue.return_value = make_issue()
        client.create_issue.return_value = make_issue()
        client.add_comment.return_value = JiraComment(id="c1", author="me@example.com", body="ack")
        client.update_issue.return_value = make_issue()
        client.get_transitions.return_value = [
            JiraTransition(id="11", name="Start Progress", to_status="In Progress"),
        ]
        client.transition_issue.return_value = make_issue()

        await assert_all_tools_leave_an_audit_trail(
            connector, jira_module, monkeypatch, tmp_path,
            arg_overrides={"jira_update_issue": {"summary": "Updated summary"}},
        )
