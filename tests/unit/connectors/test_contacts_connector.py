"""Unit tests for privacyfence.connectors.contacts.ContactsConnector.

Same approach as the other Google connector tests: ContactsClient is
mocked, gate.gated_call is stubbed to capture what's sent into the gate.
contacts_update, contacts_create, contacts_add_label, and
contacts_remove_label are all gated (gate="popup"); the read tools
(contacts_list, contacts_search, contacts_get) are unconditionally
auto-approved per README. Contact deletion is not supported by this
connector.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connectors import contacts as contacts_module
from privacyfence.connectors.contacts import ContactsConnector, _parse_json_list
from privacyfence.contacts_client import Contact, ContactEmail, ContactPhone, ContactsClient, ContactsClientError

from ...helpers import assert_all_tools_leave_an_audit_trail, assert_no_placeholder_fields

LIVE_FIXTURES_DIR = Path(__file__).parent.parent.parent / "fixtures" / "live" / "contacts"


def make_connector(my_email="me@example.com"):
    client = MagicMock()
    connector = ContactsConnector(client)
    connector.my_email = my_email
    return connector, client


def make_contact(**overrides):
    defaults = dict(
        resource_name="people/c1", display_name="Bob Smith", given_name="Bob", family_name="Smith",
        emails=[ContactEmail(value="bob@example.com", type="work")],
        phones=[ContactPhone(value="+1 555 0100", type="mobile")],
        organization="Acme", job_title="Engineer", notes="met at conference",
    )
    defaults.update(overrides)
    return Contact(**defaults)


@pytest.fixture
def gated_call_spy(monkeypatch):
    calls = []

    async def fake_gated_call(**kwargs):
        calls.append(kwargs)
        return kwargs["filtered_data"]

    monkeypatch.setattr(contacts_module, "gated_call", fake_gated_call)
    return calls


class TestParseJsonList:
    def test_valid_json_list(self):
        assert _parse_json_list('[{"value": "a@b.com", "type": "work"}]') == [{"value": "a@b.com", "type": "work"}]

    def test_empty_string_returns_none(self):
        assert _parse_json_list("") is None

    def test_whitespace_only_returns_none(self):
        assert _parse_json_list("   ") is None

    def test_invalid_json_returns_none(self):
        assert _parse_json_list("not json") is None

    def test_valid_json_but_not_a_list_returns_none(self):
        assert _parse_json_list('{"value": "a@b.com"}') is None


class TestDispatch:
    async def test_unknown_tool_raises(self):
        connector, _client = make_connector()
        with pytest.raises(ValueError, match="Unknown Contacts tool"):
            await connector.call("contacts_does_not_exist", {})


class TestAutoTools:
    async def test_contacts_list_converts_to_dict(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_contacts.return_value = [make_contact()]

        result = await connector.call("contacts_list", {"max_results": 10})

        assert result == [make_contact().to_dict()]
        client.list_contacts.assert_called_once_with(10, "both")
        entries = (tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()
        assert '"decision": "auto_accepted"' in entries[0]

    async def test_contacts_search(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.search_contacts.return_value = [make_contact()]

        result = await connector.call("contacts_search", {"query": "Bob"})

        assert result == [make_contact().to_dict()]
        client.search_contacts.assert_called_once_with("Bob", 20, "both")

    async def test_contacts_get(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.get_contact.return_value = make_contact()

        result = await connector.call("contacts_get", {"resource_name": "people/c1"})

        assert result == make_contact().to_dict()
        client.get_contact.assert_called_once_with("people/c1", "both")


class TestSourceParam:
    """Verify 'source' defaults to 'both' and is plumbed through to the client."""

    async def test_contacts_list_passes_explicit_source(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_contacts.return_value = [make_contact()]

        await connector.call("contacts_list", {"max_results": 10, "source": "directory"})

        client.list_contacts.assert_called_once_with(10, "directory")

    async def test_contacts_search_passes_explicit_source(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.search_contacts.return_value = [make_contact()]

        await connector.call("contacts_search", {"query": "Bob", "source": "personal"})

        client.search_contacts.assert_called_once_with("Bob", 20, "personal")

    async def test_contacts_get_passes_explicit_source(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.get_contact.return_value = make_contact()

        await connector.call("contacts_get", {"resource_name": "people/c1", "source": "directory"})

        client.get_contact.assert_called_once_with("people/c1", "directory")

    async def test_contacts_get_source_mismatch_becomes_runtime_error(self):
        connector, client = make_connector()
        client.get_contact.side_effect = ContactsClientError(
            "get_contact(people/c1): contact source is 'personal', but source='directory' was requested"
        )

        with pytest.raises(RuntimeError, match="but source='directory' was requested"):
            await connector.call("contacts_get", {"resource_name": "people/c1", "source": "directory"})


class TestContactsUpdate:
    async def test_preview_only_includes_provided_fields(self, gated_call_spy):
        connector, client = make_connector()
        client.get_contact.return_value = make_contact()
        client.update_contact.return_value = make_contact(job_title="Senior Engineer")

        await connector.call("contacts_update", {"resource_name": "people/c1", "job_title": "Senior Engineer"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {"Contact": "Bob Smith", "Job title": "Senior Engineer"}
        assert kwargs["gate"] == "popup"

    async def test_preview_includes_parsed_emails_and_phones(self, gated_call_spy):
        connector, client = make_connector()
        client.get_contact.return_value = make_contact()
        client.update_contact.return_value = make_contact()

        await connector.call("contacts_update", {
            "resource_name": "people/c1",
            "emails": '[{"value": "new@example.com", "type": "home"}]',
            "phones": '[{"value": "+1 555 0199", "type": "mobile"}]',
        })

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Emails"] == "new@example.com"
        assert kwargs["preview"]["Phones"] == "+1 555 0199"
        client.update_contact.assert_called_once_with(
            "people/c1", None,
            [{"value": "new@example.com", "type": "home"}],
            [{"value": "+1 555 0199", "type": "mobile"}],
            None, None, None,
        )

    async def test_invalid_json_emails_are_dropped_not_shown_and_passed_as_none(self, gated_call_spy):
        connector, client = make_connector()
        client.get_contact.return_value = make_contact()
        client.update_contact.return_value = make_contact()

        await connector.call("contacts_update", {"resource_name": "people/c1", "emails": "not valid json"})

        kwargs = gated_call_spy[0]
        assert "Emails" not in kwargs["preview"]
        client.update_contact.assert_called_once_with("people/c1", None, None, None, None, None, None)

    async def test_contact_name_falls_back_to_resource_name_when_lookup_fails(self, gated_call_spy):
        connector, client = make_connector()
        client.get_contact.side_effect = ContactsClientError("not found")
        client.update_contact.return_value = make_contact()

        await connector.call("contacts_update", {"resource_name": "people/c999", "display_name": "New Name"})

        assert gated_call_spy[0]["preview"]["Contact"] == "people/c999"
        assert gated_call_spy[0]["summary"] == "Update contact: people/c999"

    async def test_args_carry_raw_unparsed_json_strings(self, gated_call_spy):
        connector, client = make_connector()
        client.get_contact.return_value = make_contact()
        client.update_contact.return_value = make_contact()
        raw_emails = '[{"value": "x@example.com", "type": "work"}]'

        await connector.call("contacts_update", {"resource_name": "people/c1", "emails": raw_emails})

        assert gated_call_spy[0]["args"]["emails"] == raw_emails
        assert gated_call_spy[0]["raw_data"] == gated_call_spy[0]["args"]

    async def test_result_converted_to_dict(self, gated_call_spy):
        connector, client = make_connector()
        client.get_contact.return_value = make_contact()
        client.update_contact.return_value = make_contact(display_name="Updated Name")

        result = await connector.call("contacts_update", {"resource_name": "people/c1", "display_name": "Updated Name"})

        assert result["display_name"] == "Updated Name"


class TestContactsCreate:
    async def test_preview_only_includes_provided_fields(self, gated_call_spy):
        connector, client = make_connector()
        client.create_contact.return_value = make_contact(display_name="New Person")

        await connector.call("contacts_create", {"display_name": "New Person"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {"Name": "New Person"}
        assert kwargs["gate"] == "popup"
        assert kwargs["summary"] == "Create contact: New Person"

    async def test_preview_includes_parsed_emails_and_phones(self, gated_call_spy):
        connector, client = make_connector()
        client.create_contact.return_value = make_contact()

        await connector.call("contacts_create", {
            "display_name": "New Person",
            "emails": '[{"value": "new@example.com", "type": "home"}]',
            "phones": '[{"value": "+1 555 0199", "type": "mobile"}]',
        })

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Emails"] == "new@example.com"
        assert kwargs["preview"]["Phones"] == "+1 555 0199"
        client.create_contact.assert_called_once_with(
            "New Person",
            [{"value": "new@example.com", "type": "home"}],
            [{"value": "+1 555 0199", "type": "mobile"}],
            None, None, None,
        )

    async def test_invalid_json_emails_are_dropped_not_shown_and_passed_as_none(self, gated_call_spy):
        connector, client = make_connector()
        client.create_contact.return_value = make_contact()

        await connector.call("contacts_create", {"display_name": "New Person", "emails": "not valid json"})

        kwargs = gated_call_spy[0]
        assert "Emails" not in kwargs["preview"]
        client.create_contact.assert_called_once_with("New Person", None, None, None, None, None)

    async def test_result_converted_to_dict(self, gated_call_spy):
        connector, client = make_connector()
        client.create_contact.return_value = make_contact(display_name="New Person")

        result = await connector.call("contacts_create", {"display_name": "New Person"})

        assert result["display_name"] == "New Person"


class TestContactsAddLabel:
    async def test_gates_with_contact_name_and_label_in_preview(self, gated_call_spy):
        connector, client = make_connector()
        client.get_contact.return_value = make_contact()
        client.add_label.return_value = {"resource_name": "people/c1", "label_added": "VIP"}

        result = await connector.call("contacts_add_label", {"resource_name": "people/c1", "label_name": "VIP"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {"Contact": "Bob Smith", "Label": "VIP"}
        assert kwargs["gate"] == "popup"
        assert kwargs["summary"] == "Add label 'VIP' to: Bob Smith"
        assert kwargs["details_text"] == "Label will be added to this contact; no other fields change."
        client.add_label.assert_called_once_with("people/c1", "VIP")
        assert result == {"resource_name": "people/c1", "label_added": "VIP"}

    async def test_contact_name_falls_back_to_resource_name_when_lookup_fails(self, gated_call_spy):
        connector, client = make_connector()
        client.get_contact.side_effect = ContactsClientError("not found")
        client.add_label.return_value = {"resource_name": "people/c999", "label_added": "VIP"}

        await connector.call("contacts_add_label", {"resource_name": "people/c999", "label_name": "VIP"})

        assert gated_call_spy[0]["preview"]["Contact"] == "people/c999"


class TestContactsRemoveLabel:
    async def test_gates_with_contact_name_and_label_in_preview(self, gated_call_spy):
        connector, client = make_connector()
        client.get_contact.return_value = make_contact()
        client.remove_label.return_value = {"resource_name": "people/c1", "label_removed": "VIP"}

        result = await connector.call("contacts_remove_label", {"resource_name": "people/c1", "label_name": "VIP"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"] == {"Contact": "Bob Smith", "Label": "VIP"}
        assert kwargs["gate"] == "popup"
        assert kwargs["summary"] == "Remove label 'VIP' from: Bob Smith"
        assert kwargs["details_text"] == "Label will be removed from this contact; no other fields change."
        client.remove_label.assert_called_once_with("people/c1", "VIP")
        assert result == {"resource_name": "people/c1", "label_removed": "VIP"}


class TestFieldCompleteness:
    """End to end: a fully-populated raw People API person -> the real
    ContactsClient._parse_person -> the real connector's returned data --
    not a hand-built Contact, unlike every other test in this file. Mirrors
    test_confluence_connector.py's TestFieldCompleteness -- the shape of
    check that would catch a _parse_person field mapping silently degrading
    to a fallback before it ships, not after.

    Unlike every other connector's TestFieldCompleteness, this checks
    contacts_get's *returned dict* rather than a gated_call preview: every
    contacts read tool (list/search/get) is unconditionally auto-approved
    (see this module's docstring) -- there's no gated read tool to exercise
    -- so the returned dict is this connector's closest analog to a preview
    for this purpose.
    """

    async def test_contacts_get_result_has_no_placeholder_fields(self):
        path = LIVE_FIXTURES_DIR / "get_contact.json"
        if not path.exists():
            pytest.skip(f"{path} not recorded yet -- run `python3 scripts/qa_fixture_recorder.py --record contacts` locally first")
        raw = json.loads(path.read_text(encoding="utf-8"))
        # The recorded fixture has no organization/job title/notes -- add
        # them so every to_dict() field has something real to carry.
        raw = dict(
            raw,
            organizations=[{"name": "Acme Corp", "title": "QA Engineer"}],
            biographies=[{"value": "Met at the PrivacyFence QA sandbox."}],
        )

        service = MagicMock()
        service.people.return_value.get.return_value.execute.return_value = raw
        client = ContactsClient(client_config={}, token_file="/tmp/unused-token.json")
        # get_contact() runs inside a worker thread (connector._fetch uses
        # asyncio.to_thread), so client._local.service -- thread-local --
        # wouldn't be visible there; overriding _get_service directly is the
        # thread-agnostic equivalent of test_contacts_client.py's make_client().
        client._get_service = lambda: service

        connector = ContactsConnector(client)
        connector.my_email = "me@example.com"
        result = await connector.call("contacts_get", {"resource_name": raw["resourceName"]})

        assert_no_placeholder_fields(result)


class TestFetchErrorMapping:
    async def test_contacts_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.list_contacts.side_effect = ContactsClientError("token expired")

        with pytest.raises(RuntimeError, match="token expired"):
            await connector.call("contacts_list", {})


class TestEveryToolIsAudited:
    async def test_every_declared_tool_leaves_an_audit_trail(self, monkeypatch, tmp_path):
        connector, client = make_connector()
        # contacts_get's audit entry embeds contact.display_name as the
        # "sender" field -- a bare MagicMock there isn't JSON-serializable,
        # which would silently swallow the audit write (caught by the
        # connector's own try/except) rather than reflect a real product bug.
        client.get_contact.return_value = make_contact()

        await assert_all_tools_leave_an_audit_trail(connector, contacts_module, monkeypatch, tmp_path)
