"""Tests for ContactsClient's parsing/normalization logic: person
normalization (_parse_person), the search_contacts API-then-client-filter
fallback, and update_contact's partial-field update building.

Also covers the OAuth2 token lifecycle (authorize_interactive /
_load_credentials / _save_token), mocking at the google-auth library
boundary (Credentials.from_authorized_user_file /
InstalledAppFlow.from_client_config) rather than at _load_credentials
itself -- see test_tasks_client.py's module docstring for why.
"""
from __future__ import annotations

import json
import stat
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from privacyfence.contacts_client import (
    SCOPES,
    Contact,
    ContactEmail,
    ContactPhone,
    ContactsClient,
    ContactsClientError,
    _parse_person,
)
from googleapiclient.errors import HttpError

LIVE_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "live" / "contacts"


def make_client(service: MagicMock) -> ContactsClient:
    client = ContactsClient(client_config={}, token_file="/tmp/unused-token.json")
    client._local.service = service
    return client


def http_error(status: int = 404, body: bytes = b'{"error": "nope"}') -> HttpError:
    class _Resp:
        pass
    resp = _Resp()
    resp.status = status
    resp.reason = "error"
    return HttpError(resp, body)


# ---------------------------------------------------------------------------- #
# authorize_interactive
# ---------------------------------------------------------------------------- #

class TestAuthorizeInteractive:
    def test_missing_client_config_raises(self, tmp_path):
        client = ContactsClient(client_config={}, token_file=str(tmp_path / "token.json"))
        with pytest.raises(ContactsClientError, match="No Google organization config installed"):
            client.authorize_interactive()

    def test_runs_local_server_flow_and_persists_returned_credentials(self, tmp_path, monkeypatch):
        token_file = tmp_path / "nested" / "token.json"
        client = ContactsClient(client_config={"installed": {"client_id": "cid"}}, token_file=str(token_file))

        fake_creds = MagicMock()
        fake_creds.to_json.return_value = '{"token": "abc"}'
        fake_flow = MagicMock()
        fake_flow.run_local_server.return_value = fake_creds
        mock_from_client_config = MagicMock(return_value=fake_flow)
        monkeypatch.setattr(
            "privacyfence.contacts_client.InstalledAppFlow.from_client_config", mock_from_client_config
        )

        client.authorize_interactive()

        mock_from_client_config.assert_called_once_with({"installed": {"client_id": "cid"}}, SCOPES)
        fake_flow.run_local_server.assert_called_once_with(port=0)
        assert token_file.read_text(encoding="utf-8") == '{"token": "abc"}'


# ---------------------------------------------------------------------------- #
# _load_credentials: no-token / valid / expired-refresh-succeeds /
# expired-refresh-fails / expired-unrefreshable. Mocks
# Credentials.from_authorized_user_file (the google-auth library boundary),
# not _load_credentials itself.
# ---------------------------------------------------------------------------- #

class TestLoadCredentials:
    def test_missing_token_file_raises(self, tmp_path):
        client = ContactsClient(client_config={}, token_file=str(tmp_path / "does-not-exist.json"))
        with pytest.raises(ContactsClientError, match="No OAuth token found"):
            client._load_credentials()

    def test_valid_token_is_returned_without_refresh_or_network(self, tmp_path, monkeypatch):
        token_file = tmp_path / "token.json"
        token_file.write_text("{}", encoding="utf-8")
        fake_creds = MagicMock()
        fake_creds.valid = True
        monkeypatch.setattr(
            "privacyfence.contacts_client.Credentials.from_authorized_user_file",
            MagicMock(return_value=fake_creds),
        )
        client = ContactsClient(client_config={}, token_file=str(token_file))

        result = client._load_credentials()

        assert result is fake_creds
        fake_creds.refresh.assert_not_called()

    def test_expired_token_with_refresh_token_is_refreshed_and_saved_back(self, tmp_path, monkeypatch):
        token_file = tmp_path / "token.json"
        token_file.write_text("{}", encoding="utf-8")
        fake_creds = MagicMock()
        fake_creds.valid = False
        fake_creds.expired = True
        fake_creds.refresh_token = "refresh-me"
        fake_creds.to_json.return_value = '{"token": "refreshed"}'
        monkeypatch.setattr(
            "privacyfence.contacts_client.Credentials.from_authorized_user_file",
            MagicMock(return_value=fake_creds),
        )
        client = ContactsClient(client_config={}, token_file=str(token_file))

        result = client._load_credentials()

        assert result is fake_creds
        fake_creds.refresh.assert_called_once()
        assert token_file.read_text(encoding="utf-8") == '{"token": "refreshed"}'

    def test_expired_token_refresh_failure_raises_clear_error(self, tmp_path, monkeypatch):
        token_file = tmp_path / "token.json"
        token_file.write_text("{}", encoding="utf-8")
        fake_creds = MagicMock()
        fake_creds.valid = False
        fake_creds.expired = True
        fake_creds.refresh_token = "refresh-me"
        fake_creds.refresh.side_effect = Exception("token has been revoked")
        monkeypatch.setattr(
            "privacyfence.contacts_client.Credentials.from_authorized_user_file",
            MagicMock(return_value=fake_creds),
        )
        client = ContactsClient(client_config={}, token_file=str(token_file))

        with pytest.raises(ContactsClientError, match="Failed to refresh Contacts OAuth token.*revoked"):
            client._load_credentials()

    def test_expired_token_without_refresh_token_raises_invalid_cached_token(self, tmp_path, monkeypatch):
        token_file = tmp_path / "token.json"
        token_file.write_text("{}", encoding="utf-8")
        fake_creds = MagicMock()
        fake_creds.valid = False
        fake_creds.expired = True
        fake_creds.refresh_token = ""
        monkeypatch.setattr(
            "privacyfence.contacts_client.Credentials.from_authorized_user_file",
            MagicMock(return_value=fake_creds),
        )
        client = ContactsClient(client_config={}, token_file=str(token_file))

        with pytest.raises(ContactsClientError, match="Cached Contacts OAuth token is invalid"):
            client._load_credentials()


# ---------------------------------------------------------------------------- #
# _save_token: file permissions
# ---------------------------------------------------------------------------- #

class TestSaveToken:
    def test_writes_credentials_json_with_owner_only_permissions(self, tmp_path):
        token_file = tmp_path / "nested" / "token.json"
        client = ContactsClient(client_config={}, token_file=str(token_file))
        fake_creds = MagicMock()
        fake_creds.to_json.return_value = '{"token": "abc"}'

        client._save_token(fake_creds)

        assert token_file.read_text(encoding="utf-8") == '{"token": "abc"}'
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600

    def test_chmod_failure_is_non_fatal(self, tmp_path, monkeypatch):
        token_file = tmp_path / "token.json"
        client = ContactsClient(client_config={}, token_file=str(token_file))
        fake_creds = MagicMock()
        fake_creds.to_json.return_value = "{}"
        monkeypatch.setattr("os.chmod", MagicMock(side_effect=OSError("read-only filesystem")))

        client._save_token(fake_creds)  # must not raise

        assert token_file.exists()


# ---------------------------------------------------------------------------- #
# check_connection
# ---------------------------------------------------------------------------- #

class TestCheckConnection:
    def test_returns_confirmation_with_total_people(self):
        service = MagicMock()
        service.people.return_value.connections.return_value.list.return_value.execute.return_value = {
            "totalPeople": 5
        }
        client = make_client(service)
        result = client.check_connection()
        assert "5" in result

    def test_http_error_becomes_contacts_client_error(self):
        service = MagicMock()
        service.people.return_value.connections.return_value.list.return_value.execute.side_effect = http_error(500)
        client = make_client(service)
        with pytest.raises(ContactsClientError, match="Contacts connection check failed"):
            client.check_connection()


# ---------------------------------------------------------------------------- #
# _parse_person
# ---------------------------------------------------------------------------- #

class TestParsePerson:
    def test_full_person_normalized(self):
        person = {
            "resourceName": "people/c1",
            "names": [{"displayName": "Jane Doe", "givenName": "Jane", "familyName": "Doe"}],
            "emailAddresses": [{"value": "jane@x.com", "type": "work"}],
            "phoneNumbers": [{"value": "+1234567890", "type": "mobile"}],
            "organizations": [{"name": "Acme", "title": "Engineer"}],
            "biographies": [{"value": "notes here"}],
            "photos": [{"url": "https://x/photo.jpg"}],
        }
        contact = _parse_person(person)
        assert contact == Contact(
            resource_name="people/c1", display_name="Jane Doe", given_name="Jane", family_name="Doe",
            emails=[ContactEmail(value="jane@x.com", type="work")],
            phones=[ContactPhone(value="+1234567890", type="mobile")],
            organization="Acme", job_title="Engineer", notes="notes here", photo_url="https://x/photo.jpg",
        )

    def test_missing_fields_default_to_empty(self):
        contact = _parse_person({})
        assert contact == Contact(resource_name="", display_name="", given_name="", family_name="")
        assert contact.short_summary() == " ()"

    def test_explicit_null_values_tolerated_not_just_missing_keys(self):
        # The People API sometimes sends explicit `null` instead of omitting
        # a requested-but-empty field.
        person = {
            "names": None, "emailAddresses": None, "phoneNumbers": None,
            "organizations": None, "biographies": None, "photos": None,
        }
        contact = _parse_person(person)
        assert contact.emails == []
        assert contact.phones == []
        assert contact.organization == ""
        assert contact.notes == ""
        assert contact.photo_url == ""

    def test_to_dict_round_trips_all_fields(self):
        contact = Contact(
            resource_name="people/c1", display_name="Jane", given_name="Jane", family_name="",
            emails=[ContactEmail(value="j@x.com", type="work")],
            phones=[ContactPhone(value="123", type="home")],
            organization="Acme", job_title="Eng", notes="n", photo_url="p",
        )
        assert contact.to_dict() == {
            "resource_name": "people/c1", "display_name": "Jane", "given_name": "Jane", "family_name": "",
            "emails": [{"value": "j@x.com", "type": "work"}],
            "phones": [{"value": "123", "type": "home"}],
            "organization": "Acme", "job_title": "Eng", "notes": "n", "photo_url": "p",
            "source": "other", "source_types": [],
        }

    def test_short_summary_uses_at_most_two_emails(self):
        contact = Contact(
            resource_name="", display_name="Jane", given_name="", family_name="",
            emails=[ContactEmail("a@x.com", ""), ContactEmail("b@x.com", ""), ContactEmail("c@x.com", "")],
        )
        assert contact.short_summary() == "Jane (a@x.com, b@x.com)"


# ---------------------------------------------------------------------------- #
# Source classification: Google's People API blends personal ("CONTACT") and
# Workspace directory ("DOMAIN_PROFILE"/"DOMAIN_CONTACT") sources into one
# merged Person; metadata.sources[].type is what lets us split them back apart.
# ---------------------------------------------------------------------------- #

class TestSourceClassification:
    def test_contact_source_classified_personal(self):
        contact = _parse_person({"metadata": {"sources": [{"type": "CONTACT"}]}})
        assert contact.source == "personal"
        assert contact.source_types == ["CONTACT"]

    def test_domain_profile_source_classified_directory(self):
        contact = _parse_person({"metadata": {"sources": [{"type": "DOMAIN_PROFILE"}]}})
        assert contact.source == "directory"

    def test_domain_contact_source_classified_directory(self):
        contact = _parse_person({"metadata": {"sources": [{"type": "DOMAIN_CONTACT"}]}})
        assert contact.source == "directory"

    def test_personal_and_directory_sources_classified_both(self):
        contact = _parse_person({
            "metadata": {"sources": [{"type": "CONTACT"}, {"type": "DOMAIN_PROFILE"}]},
        })
        assert contact.source == "both"
        assert contact.source_types == ["CONTACT", "DOMAIN_PROFILE"]

    def test_no_metadata_classified_other(self):
        contact = _parse_person({})
        assert contact.source == "other"
        assert contact.source_types == []

    def test_explicit_null_metadata_tolerated(self):
        contact = _parse_person({"metadata": None})
        assert contact.source == "other"

    def test_account_only_source_classified_other(self):
        # ACCOUNT-only means "has a Google account" -- not a saved contact or
        # a colleague, so it shouldn't count as personal or directory.
        contact = _parse_person({"metadata": {"sources": [{"type": "ACCOUNT"}]}})
        assert contact.source == "other"


# ---------------------------------------------------------------------------- #
# list_contacts / get_contact
# ---------------------------------------------------------------------------- #

class TestListContacts:
    def test_clamps_max_results(self):
        service = MagicMock()
        service.people.return_value.connections.return_value.list.return_value.execute.return_value = {
            "connections": []
        }
        client = make_client(service)
        client.list_contacts(max_results=5000)
        assert service.people.return_value.connections.return_value.list.call_args.kwargs["pageSize"] == 1000

    def test_maps_connections_to_contacts(self):
        service = MagicMock()
        service.people.return_value.connections.return_value.list.return_value.execute.return_value = {
            "connections": [{"resourceName": "people/c1", "names": [{"displayName": "A"}]}]
        }
        client = make_client(service)
        contacts = client.list_contacts()
        assert contacts[0].display_name == "A"

    def test_http_error_becomes_contacts_client_error(self):
        service = MagicMock()
        service.people.return_value.connections.return_value.list.return_value.execute.side_effect = http_error(500)
        client = make_client(service)
        with pytest.raises(ContactsClientError, match="list_contacts failed"):
            client.list_contacts()

    def test_requests_personal_profile_and_domain_contact_sources(self):
        service = MagicMock()
        service.people.return_value.connections.return_value.list.return_value.execute.return_value = {
            "connections": []
        }
        client = make_client(service)
        client.list_contacts()
        kwargs = service.people.return_value.connections.return_value.list.call_args.kwargs
        assert kwargs["sources"] == [
            "READ_SOURCE_TYPE_CONTACT", "READ_SOURCE_TYPE_PROFILE", "READ_SOURCE_TYPE_DOMAIN_CONTACT",
        ]

    def test_source_personal_filters_out_directory_only_entries(self):
        service = MagicMock()
        service.people.return_value.connections.return_value.list.return_value.execute.return_value = {
            "connections": [
                {"resourceName": "people/c1", "names": [{"displayName": "Directory Only"}],
                 "metadata": {"sources": [{"type": "DOMAIN_PROFILE"}]}},
                {"resourceName": "people/c2", "names": [{"displayName": "Saved Contact"}],
                 "metadata": {"sources": [{"type": "CONTACT"}]}},
            ]
        }
        client = make_client(service)
        contacts = client.list_contacts(source="personal")
        assert [c.display_name for c in contacts] == ["Saved Contact"]

    def test_source_directory_filters_out_personal_only_entries(self):
        service = MagicMock()
        service.people.return_value.connections.return_value.list.return_value.execute.return_value = {
            "connections": [
                {"resourceName": "people/c1", "names": [{"displayName": "Directory Only"}],
                 "metadata": {"sources": [{"type": "DOMAIN_PROFILE"}]}},
                {"resourceName": "people/c2", "names": [{"displayName": "Saved Contact"}],
                 "metadata": {"sources": [{"type": "CONTACT"}]}},
            ]
        }
        client = make_client(service)
        contacts = client.list_contacts(source="directory")
        assert [c.display_name for c in contacts] == ["Directory Only"]

    def test_source_both_returns_everything_unfiltered(self):
        service = MagicMock()
        service.people.return_value.connections.return_value.list.return_value.execute.return_value = {
            "connections": [
                {"resourceName": "people/c1", "names": [{"displayName": "Directory Only"}],
                 "metadata": {"sources": [{"type": "DOMAIN_PROFILE"}]}},
                {"resourceName": "people/c2", "names": [{"displayName": "Unclassified"}]},
            ]
        }
        client = make_client(service)
        contacts = client.list_contacts()
        assert len(contacts) == 2

    def test_invalid_source_raises(self):
        client = make_client(MagicMock())
        with pytest.raises(ContactsClientError, match="Invalid source"):
            client.list_contacts(source="bogus")


class TestGetContact:
    def test_fetches_and_normalizes(self):
        service = MagicMock()
        service.people.return_value.get.return_value.execute.return_value = {
            "resourceName": "people/c1", "names": [{"displayName": "A"}],
        }
        client = make_client(service)
        contact = client.get_contact("people/c1")
        assert contact.display_name == "A"

    def test_http_error_becomes_contacts_client_error(self):
        service = MagicMock()
        service.people.return_value.get.return_value.execute.side_effect = http_error(404)
        client = make_client(service)
        with pytest.raises(ContactsClientError, match="get_contact"):
            client.get_contact("people/c1")

    def test_source_both_default_skips_matching(self):
        service = MagicMock()
        service.people.return_value.get.return_value.execute.return_value = {
            "resourceName": "people/c1", "names": [{"displayName": "A"}],
        }
        client = make_client(service)
        contact = client.get_contact("people/c1")
        assert contact.display_name == "A"

    def test_source_match_returns_contact(self):
        service = MagicMock()
        service.people.return_value.get.return_value.execute.return_value = {
            "resourceName": "people/c1", "names": [{"displayName": "A"}],
            "metadata": {"sources": [{"type": "DOMAIN_PROFILE"}]},
        }
        client = make_client(service)
        contact = client.get_contact("people/c1", source="directory")
        assert contact.display_name == "A"

    def test_source_mismatch_raises(self):
        service = MagicMock()
        service.people.return_value.get.return_value.execute.return_value = {
            "resourceName": "people/c1", "names": [{"displayName": "A"}],
            "metadata": {"sources": [{"type": "CONTACT"}]},
        }
        client = make_client(service)
        with pytest.raises(ContactsClientError, match="source is 'personal'"):
            client.get_contact("people/c1", source="directory")


# ---------------------------------------------------------------------------- #
# search_contacts: API search, then client-side fallback
# ---------------------------------------------------------------------------- #

class TestSearchContacts:
    def test_uses_search_contacts_endpoint_when_it_returns_results(self):
        service = MagicMock()
        service.people.return_value.searchContacts.return_value.execute.return_value = {
            "results": [{"person": {"resourceName": "people/c1", "names": [{"displayName": "Jane"}]}}]
        }
        client = make_client(service)

        contacts = client.search_contacts("jane")

        assert len(contacts) == 1
        assert contacts[0].display_name == "Jane"
        service.people.return_value.connections.return_value.list.assert_not_called()

    def test_falls_back_to_client_side_filter_when_search_returns_empty(self):
        service = MagicMock()
        service.people.return_value.searchContacts.return_value.execute.return_value = {"results": []}
        service.people.return_value.connections.return_value.list.return_value.execute.return_value = {
            "connections": [
                {"resourceName": "people/c1", "names": [{"displayName": "Jane Doe"}], "emailAddresses": []},
                {"resourceName": "people/c2", "names": [{"displayName": "Bob"}], "emailAddresses": [{"value": "bob@x.com"}]},
            ]
        }
        client = make_client(service)

        contacts = client.search_contacts("jane")

        assert len(contacts) == 1
        assert contacts[0].display_name == "Jane Doe"

    def test_falls_back_when_search_contacts_endpoint_raises(self):
        service = MagicMock()
        service.people.return_value.searchContacts.return_value.execute.side_effect = http_error(400)
        service.people.return_value.connections.return_value.list.return_value.execute.return_value = {
            "connections": [{"resourceName": "people/c1", "names": [{"displayName": "Jane"}], "emailAddresses": []}]
        }
        client = make_client(service)

        contacts = client.search_contacts("jane")

        assert len(contacts) == 1

    def test_fallback_filter_matches_on_email_too(self):
        service = MagicMock()
        service.people.return_value.searchContacts.return_value.execute.return_value = {"results": []}
        service.people.return_value.connections.return_value.list.return_value.execute.return_value = {
            "connections": [
                {"resourceName": "people/c1", "names": [{"displayName": "No Match"}],
                 "emailAddresses": [{"value": "findme@x.com"}]},
            ]
        }
        client = make_client(service)

        contacts = client.search_contacts("findme")

        assert len(contacts) == 1

    def test_fallback_respects_max_results_cap(self):
        service = MagicMock()
        service.people.return_value.searchContacts.return_value.execute.return_value = {"results": []}
        service.people.return_value.connections.return_value.list.return_value.execute.return_value = {
            "connections": [
                {"resourceName": f"people/c{i}", "names": [{"displayName": "Match"}], "emailAddresses": []}
                for i in range(5)
            ]
        }
        client = make_client(service)

        contacts = client.search_contacts("match", max_results=2)

        assert len(contacts) == 2

    def test_source_directory_uses_list_contacts_not_search_endpoint(self):
        service = MagicMock()
        service.people.return_value.connections.return_value.list.return_value.execute.return_value = {
            "connections": [
                {"resourceName": "people/c1", "names": [{"displayName": "Jane Directory"}],
                 "emailAddresses": [], "metadata": {"sources": [{"type": "DOMAIN_PROFILE"}]}},
                {"resourceName": "people/c2", "names": [{"displayName": "Bob Personal"}],
                 "emailAddresses": [], "metadata": {"sources": [{"type": "CONTACT"}]}},
            ]
        }
        client = make_client(service)

        contacts = client.search_contacts("jane", source="directory")

        assert [c.display_name for c in contacts] == ["Jane Directory"]
        service.people.return_value.searchContacts.assert_not_called()

    def test_source_directory_matches_on_email_too(self):
        service = MagicMock()
        service.people.return_value.connections.return_value.list.return_value.execute.return_value = {
            "connections": [
                {"resourceName": "people/c1", "names": [{"displayName": "No Match"}],
                 "emailAddresses": [{"value": "findme@x.com"}],
                 "metadata": {"sources": [{"type": "DOMAIN_PROFILE"}]}},
            ]
        }
        client = make_client(service)

        contacts = client.search_contacts("findme", source="directory")

        assert len(contacts) == 1

    def test_source_personal_does_not_call_directory_scan(self):
        service = MagicMock()
        service.people.return_value.searchContacts.return_value.execute.return_value = {
            "results": [{"person": {"resourceName": "people/c1", "names": [{"displayName": "Jane"}]}}]
        }
        client = make_client(service)

        contacts = client.search_contacts("jane", source="personal")

        assert len(contacts) == 1
        service.people.return_value.connections.return_value.list.assert_not_called()

    def test_invalid_source_raises(self):
        client = make_client(MagicMock())
        with pytest.raises(ContactsClientError, match="Invalid source"):
            client.search_contacts("jane", source="bogus")


# ---------------------------------------------------------------------------- #
# update_contact: partial-field update building
# ---------------------------------------------------------------------------- #

class TestUpdateContact:
    def test_fetch_http_error_becomes_contacts_client_error(self):
        service = MagicMock()
        service.people.return_value.get.return_value.execute.side_effect = http_error(404)
        client = make_client(service)
        with pytest.raises(ContactsClientError, match="update_contact: fetch failed"):
            client.update_contact("people/c1", display_name="New")

    def test_no_fields_provided_skips_update_call_returns_current(self):
        service = MagicMock()
        service.people.return_value.get.return_value.execute.return_value = {
            "resourceName": "people/c1", "names": [{"displayName": "Current"}],
        }
        client = make_client(service)

        contact = client.update_contact("people/c1")

        assert contact.display_name == "Current"
        service.people.return_value.updateContact.assert_not_called()

    def test_display_name_update_splits_given_and_family_name(self):
        service = MagicMock()
        service.people.return_value.get.return_value.execute.return_value = {
            "resourceName": "people/c1", "names": [{"displayName": "Old Name"}], "etag": "e1",
        }
        service.people.return_value.updateContact.return_value.execute.return_value = {
            "resourceName": "people/c1", "names": [{"displayName": "Jane Doe Smith"}],
        }
        client = make_client(service)

        client.update_contact("people/c1", display_name="Jane Doe Smith")

        body = service.people.return_value.updateContact.call_args.kwargs["body"]
        assert body["names"][0]["displayName"] == "Jane Doe Smith"
        assert body["names"][0]["givenName"] == "Jane"
        assert body["names"][0]["familyName"] == "Doe Smith"
        assert service.people.return_value.updateContact.call_args.kwargs["updatePersonFields"] == "names"

    def test_display_name_update_when_no_existing_name(self):
        # person.get("names") or [{}] is always truthy, so this still goes
        # through the split-name path (giving displayName/givenName/
        # familyName) rather than a bare {"displayName": ...} entry.
        service = MagicMock()
        service.people.return_value.get.return_value.execute.return_value = {"resourceName": "people/c1"}
        service.people.return_value.updateContact.return_value.execute.return_value = {"resourceName": "people/c1"}
        client = make_client(service)

        client.update_contact("people/c1", display_name="New Person")

        body = service.people.return_value.updateContact.call_args.kwargs["body"]
        assert body["names"] == [{"displayName": "New Person", "givenName": "New", "familyName": "Person"}]

    def test_emails_and_phones_replace_existing_lists(self):
        service = MagicMock()
        service.people.return_value.get.return_value.execute.return_value = {"resourceName": "people/c1"}
        service.people.return_value.updateContact.return_value.execute.return_value = {"resourceName": "people/c1"}
        client = make_client(service)

        client.update_contact(
            "people/c1",
            emails=[{"value": "new@x.com", "type": "work"}],
            phones=[{"value": "555", "type": "mobile"}],
        )

        body = service.people.return_value.updateContact.call_args.kwargs["body"]
        assert body["emailAddresses"] == [{"value": "new@x.com", "type": "work"}]
        assert body["phoneNumbers"] == [{"value": "555", "type": "mobile"}]
        fields = service.people.return_value.updateContact.call_args.kwargs["updatePersonFields"]
        assert set(fields.split(",")) == {"emailAddresses", "phoneNumbers"}

    def test_organization_and_job_title_merge_into_first_org_entry(self):
        service = MagicMock()
        service.people.return_value.get.return_value.execute.return_value = {
            "resourceName": "people/c1", "organizations": [{"name": "Old Co"}],
        }
        service.people.return_value.updateContact.return_value.execute.return_value = {"resourceName": "people/c1"}
        client = make_client(service)

        client.update_contact("people/c1", job_title="New Title")

        body = service.people.return_value.updateContact.call_args.kwargs["body"]
        assert body["organizations"][0] == {"name": "Old Co", "title": "New Title"}

    def test_notes_update_sets_biography(self):
        service = MagicMock()
        service.people.return_value.get.return_value.execute.return_value = {"resourceName": "people/c1"}
        service.people.return_value.updateContact.return_value.execute.return_value = {"resourceName": "people/c1"}
        client = make_client(service)

        client.update_contact("people/c1", notes="new note")

        body = service.people.return_value.updateContact.call_args.kwargs["body"]
        assert body["biographies"] == [{"value": "new note", "contentType": "TEXT_PLAIN"}]

    def test_etag_preserved_from_fetched_person(self):
        service = MagicMock()
        service.people.return_value.get.return_value.execute.return_value = {
            "resourceName": "people/c1", "etag": "abc123",
        }
        service.people.return_value.updateContact.return_value.execute.return_value = {"resourceName": "people/c1"}
        client = make_client(service)

        client.update_contact("people/c1", notes="x")

        body = service.people.return_value.updateContact.call_args.kwargs["body"]
        assert body["etag"] == "abc123"

    def test_update_http_error_becomes_contacts_client_error(self):
        service = MagicMock()
        service.people.return_value.get.return_value.execute.return_value = {"resourceName": "people/c1"}
        service.people.return_value.updateContact.return_value.execute.side_effect = http_error(400)
        client = make_client(service)
        with pytest.raises(ContactsClientError, match="update_contact failed"):
            client.update_contact("people/c1", notes="x")

    def test_unexpected_non_http_error_becomes_contacts_client_error(self):
        # Regression: a bare Python exception raised while mutating the
        # fetched person (e.g. a surprising People API payload shape) used to
        # propagate straight past this function as a raw, unhelpful error
        # ("'NoneType' object is not iterable") instead of a clean
        # ContactsClientError.
        service = MagicMock()
        service.people.return_value.get.return_value.execute.return_value = {"resourceName": "people/c1"}
        service.people.return_value.updateContact.return_value.execute.side_effect = TypeError(
            "'NoneType' object is not iterable"
        )
        client = make_client(service)
        with pytest.raises(ContactsClientError, match="update_contact failed unexpectedly"):
            client.update_contact("people/c1", notes="x")


# ---------------------------------------------------------------------------- #
# create_contact
# ---------------------------------------------------------------------------- #

class TestCreateContact:
    def test_builds_person_body_from_provided_fields(self):
        service = MagicMock()
        service.people.return_value.createContact.return_value.execute.return_value = {
            "resourceName": "people/c9", "names": [{"displayName": "Jane Doe"}],
        }
        client = make_client(service)

        contact = client.create_contact(
            display_name="Jane Doe",
            emails=[{"value": "jane@x.com", "type": "work"}],
            phones=[{"value": "555", "type": "mobile"}],
            organization="Acme",
            job_title="Engineer",
            notes="met at conference",
        )

        body = service.people.return_value.createContact.call_args.kwargs["body"]
        assert body["names"] == [{"displayName": "Jane Doe", "givenName": "Jane", "familyName": "Doe"}]
        assert body["emailAddresses"] == [{"value": "jane@x.com", "type": "work"}]
        assert body["phoneNumbers"] == [{"value": "555", "type": "mobile"}]
        assert body["organizations"] == [{"name": "Acme", "title": "Engineer"}]
        assert body["biographies"] == [{"value": "met at conference", "contentType": "TEXT_PLAIN"}]
        assert contact.display_name == "Jane Doe"

    def test_omitted_fields_are_not_included_in_body(self):
        service = MagicMock()
        service.people.return_value.createContact.return_value.execute.return_value = {
            "resourceName": "people/c9", "names": [{"displayName": "Jane"}],
        }
        client = make_client(service)

        client.create_contact(display_name="Jane")

        body = service.people.return_value.createContact.call_args.kwargs["body"]
        assert "emailAddresses" not in body
        assert "phoneNumbers" not in body
        assert "organizations" not in body
        assert "biographies" not in body

    def test_no_display_name_produces_no_names_field(self):
        service = MagicMock()
        service.people.return_value.createContact.return_value.execute.return_value = {"resourceName": "people/c9"}
        client = make_client(service)

        client.create_contact(emails=[{"value": "jane@x.com", "type": "work"}])

        body = service.people.return_value.createContact.call_args.kwargs["body"]
        assert "names" not in body

    def test_http_error_becomes_contacts_client_error(self):
        service = MagicMock()
        service.people.return_value.createContact.return_value.execute.side_effect = http_error(400)
        client = make_client(service)
        with pytest.raises(ContactsClientError, match="create_contact failed"):
            client.create_contact(display_name="Jane")


# ---------------------------------------------------------------------------- #
# Labels (contact groups): add_label / remove_label
# ---------------------------------------------------------------------------- #

class TestLabels:
    def test_add_label_uses_existing_group(self):
        service = MagicMock()
        service.contactGroups.return_value.list.return_value.execute.return_value = {
            "contactGroups": [{"resourceName": "contactGroups/g1", "formattedName": "VIP"}]
        }
        client = make_client(service)

        result = client.add_label("people/c1", "VIP")

        service.contactGroups.return_value.create.assert_not_called()
        service.contactGroups.return_value.members.return_value.modify.assert_called_once_with(
            resourceName="contactGroups/g1",
            body={"resourceNamesToAdd": ["people/c1"]},
        )
        assert result == {"resource_name": "people/c1", "label_added": "VIP"}

    def test_add_label_is_case_insensitive_match(self):
        service = MagicMock()
        service.contactGroups.return_value.list.return_value.execute.return_value = {
            "contactGroups": [{"resourceName": "contactGroups/g1", "formattedName": "vip"}]
        }
        client = make_client(service)

        client.add_label("people/c1", "VIP")

        service.contactGroups.return_value.create.assert_not_called()

    def test_add_label_creates_group_when_missing(self):
        service = MagicMock()
        service.contactGroups.return_value.list.return_value.execute.return_value = {"contactGroups": []}
        service.contactGroups.return_value.create.return_value.execute.return_value = {
            "resourceName": "contactGroups/new"
        }
        client = make_client(service)

        client.add_label("people/c1", "Brand New Label")

        service.contactGroups.return_value.create.assert_called_once_with(
            body={"contactGroup": {"name": "Brand New Label"}}
        )
        service.contactGroups.return_value.members.return_value.modify.assert_called_once_with(
            resourceName="contactGroups/new",
            body={"resourceNamesToAdd": ["people/c1"]},
        )

    def test_add_label_modify_http_error_becomes_contacts_client_error(self):
        service = MagicMock()
        service.contactGroups.return_value.list.return_value.execute.return_value = {
            "contactGroups": [{"resourceName": "contactGroups/g1", "formattedName": "VIP"}]
        }
        service.contactGroups.return_value.members.return_value.modify.return_value.execute.side_effect = http_error(400)
        client = make_client(service)
        with pytest.raises(ContactsClientError, match="add_label"):
            client.add_label("people/c1", "VIP")

    def test_remove_label_uses_existing_group(self):
        service = MagicMock()
        service.contactGroups.return_value.list.return_value.execute.return_value = {
            "contactGroups": [{"resourceName": "contactGroups/g1", "formattedName": "VIP"}]
        }
        client = make_client(service)

        result = client.remove_label("people/c1", "VIP")

        service.contactGroups.return_value.members.return_value.modify.assert_called_once_with(
            resourceName="contactGroups/g1",
            body={"resourceNamesToRemove": ["people/c1"]},
        )
        assert result == {"resource_name": "people/c1", "label_removed": "VIP"}

    def test_remove_label_missing_group_is_a_no_op_not_an_error(self):
        service = MagicMock()
        service.contactGroups.return_value.list.return_value.execute.return_value = {"contactGroups": []}
        client = make_client(service)

        result = client.remove_label("people/c1", "Nonexistent")

        service.contactGroups.return_value.members.return_value.modify.assert_not_called()
        assert result == {
            "resource_name": "people/c1", "label_removed": "Nonexistent", "note": "label not found",
        }

    def test_remove_label_modify_http_error_becomes_contacts_client_error(self):
        service = MagicMock()
        service.contactGroups.return_value.list.return_value.execute.return_value = {
            "contactGroups": [{"resourceName": "contactGroups/g1", "formattedName": "VIP"}]
        }
        service.contactGroups.return_value.members.return_value.modify.return_value.execute.side_effect = http_error(400)
        client = make_client(service)
        with pytest.raises(ContactsClientError, match="remove_label"):
            client.remove_label("people/c1", "VIP")

    def test_contact_groups_list_http_error_becomes_contacts_client_error(self):
        service = MagicMock()
        service.contactGroups.return_value.list.return_value.execute.side_effect = http_error(500)
        client = make_client(service)
        with pytest.raises(ContactsClientError, match="contactGroups.list failed"):
            client.add_label("people/c1", "VIP")


# ---------------------------------------------------------------------------- #
# _get_service: must not share one service (and its underlying httplib2
# transport) across threads, since concurrent requests dispatched via
# asyncio.to_thread corrupt a shared connection (regression for the "SSL:
# DECRYPTION_FAILED_OR_BAD_RECORD_MAC" crash caused by two threads driving
# the same connection at once).
# ---------------------------------------------------------------------------- #

class TestServiceIsThreadLocal:
    def test_each_thread_gets_its_own_service_instance(self):
        client = ContactsClient(client_config={}, token_file="/tmp/unused-token.json")
        with patch("privacyfence.contacts_client.build") as mock_build, \
             patch.object(client, "_load_credentials", return_value=MagicMock()):
            mock_build.side_effect = lambda *a, **k: MagicMock()

            services: dict[int, object] = {}

            def worker(idx: int) -> None:
                services[idx] = client._get_service()

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert len({id(s) for s in services.values()}) == 5

    def test_same_thread_reuses_cached_service(self):
        client = ContactsClient(client_config={}, token_file="/tmp/unused-token.json")
        with patch("privacyfence.contacts_client.build") as mock_build, \
             patch.object(client, "_load_credentials", return_value=MagicMock()):
            mock_build.side_effect = lambda *a, **k: MagicMock()
            assert client._get_service() is client._get_service()
            assert mock_build.call_count == 1


class TestLiveFixtureParsing:
    """Replays a fixture recorded from a real, [QATEST]-tagged seed contact
    by scripts/qa_fixture_recorder.py --record contacts -- real API shape,
    not hand-authored. Unlike every other connector's fixture, this one is
    deliberately *not* redacted (the contact's own fields are the content
    under test, not someone else's identity -- see check_contacts() in
    scripts/qa_fixture_recorder.py). Skipped (not failed) until that
    fixture exists; see tests/fixtures/live/README.md and
    docs/testing-policy.md. Re-record via that
    script if this ever starts failing after a genuine People API change.
    """

    def test_get_contact_fixture_still_parses(self):
        path = LIVE_FIXTURES_DIR / "get_contact.json"
        if not path.exists():
            pytest.skip(
                f"{path} not recorded yet -- run "
                "`python3 scripts/qa_fixture_recorder.py --record contacts` locally first"
            )
        raw = json.loads(path.read_text(encoding="utf-8"))

        contact = _parse_person(raw)

        assert contact.resource_name and contact.display_name
