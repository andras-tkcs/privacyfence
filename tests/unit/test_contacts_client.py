"""Tests for ContactsClient's parsing/normalization logic: person
normalization (_parse_person), the search_contacts API-then-client-filter
fallback, and update_contact's partial-field update building.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from privacyfence.contacts_client import (
    Contact,
    ContactEmail,
    ContactPhone,
    ContactsClient,
    ContactsClientError,
    _parse_person,
)
from googleapiclient.errors import HttpError


def make_client(service: MagicMock) -> ContactsClient:
    client = ContactsClient(client_config={}, token_file="/tmp/unused-token.json")
    client._service = service
    return client


def http_error(status: int = 404, body: bytes = b'{"error": "nope"}') -> HttpError:
    class _Resp:
        pass
    resp = _Resp()
    resp.status = status
    resp.reason = "error"
    return HttpError(resp, body)


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
        }

    def test_short_summary_uses_at_most_two_emails(self):
        contact = Contact(
            resource_name="", display_name="Jane", given_name="", family_name="",
            emails=[ContactEmail("a@x.com", ""), ContactEmail("b@x.com", ""), ContactEmail("c@x.com", "")],
        )
        assert contact.short_summary() == "Jane (a@x.com, b@x.com)"


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
# Thread safety: concurrent calls must not overlap on the shared httplib2
# connection (regression for the "SSL: DECRYPTION_FAILED_OR_BAD_RECORD_MAC"
# crash caused by two threads driving the same connection at once).
# ---------------------------------------------------------------------------- #

class TestConcurrentRequestsAreSerialized:
    def test_list_and_search_calls_never_overlap(self):
        active = 0
        max_active = 0
        state_lock = threading.Lock()

        def fake_execute():
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with state_lock:
                active -= 1
            return {"connections": [], "results": []}

        service = MagicMock()
        service.people.return_value.connections.return_value.list.return_value.execute.side_effect = fake_execute
        service.people.return_value.searchContacts.return_value.execute.side_effect = fake_execute
        client = make_client(service)

        threads = [
            threading.Thread(target=client.list_contacts),
            threading.Thread(target=client.search_contacts, args=("jane",)),
            threading.Thread(target=client.list_contacts),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert max_active == 1
