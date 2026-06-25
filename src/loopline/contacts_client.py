"""Google People API client.

Handles OAuth2 authorization and read/write access to Google Contacts (People
API).  All data is normalized into simple dataclasses so the connector never
has to deal with the raw People API payload.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/contacts"]


class ContactsClientError(Exception):
    """Raised for unrecoverable Contacts client problems (auth, config, API)."""


@dataclass
class ContactEmail:
    value: str
    type: str  # "work" | "home" | ""


@dataclass
class ContactPhone:
    value: str
    type: str


@dataclass
class Contact:
    resource_name: str  # "people/c12345"
    display_name: str
    given_name: str
    family_name: str
    emails: list[ContactEmail] = field(default_factory=list)
    phones: list[ContactPhone] = field(default_factory=list)
    organization: str = ""  # company name
    job_title: str = ""
    notes: str = ""
    photo_url: str = ""

    def short_summary(self) -> str:
        emails = ", ".join(e.value for e in self.emails[:2])
        return f"{self.display_name} ({emails})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_name": self.resource_name,
            "display_name": self.display_name,
            "given_name": self.given_name,
            "family_name": self.family_name,
            "emails": [{"value": e.value, "type": e.type} for e in self.emails],
            "phones": [{"value": p.value, "type": p.type} for p in self.phones],
            "organization": self.organization,
            "job_title": self.job_title,
            "notes": self.notes,
            "photo_url": self.photo_url,
        }


_PERSON_FIELDS = "names,emailAddresses,phoneNumbers,organizations,biographies,photos"


class ContactsClient:
    """Google Contacts (People API) client with OAuth2 token caching."""

    def __init__(self, credentials_file: str, token_file: str) -> None:
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._service = None  # lazily built

    # ------------------------------------------------------------------ #
    # Authentication
    # ------------------------------------------------------------------ #

    def authorize_interactive(self) -> None:
        """Run the interactive OAuth flow and persist the token."""
        if not os.path.exists(self._credentials_file):
            raise ContactsClientError(
                f"OAuth client secret not found at '{self._credentials_file}'. "
                "Download it from the Google Cloud Console (OAuth client of type "
                "'Desktop app') and place it there."
            )
        logger.info("Starting interactive OAuth flow for Contacts")
        flow = InstalledAppFlow.from_client_secrets_file(
            self._credentials_file, SCOPES
        )
        creds = flow.run_local_server(port=0)
        self._save_token(creds)
        logger.info("Contacts OAuth token saved to '%s'", self._token_file)

    def _load_credentials(self) -> Credentials:
        if not os.path.exists(self._token_file):
            raise ContactsClientError(
                f"No OAuth token found at '{self._token_file}'. "
                "Run the application with '--contacts-oauth' to authorize."
            )
        creds = Credentials.from_authorized_user_file(self._token_file, SCOPES)
        if creds.valid:
            return creds
        if creds.expired and creds.refresh_token:
            logger.info("Refreshing expired Contacts OAuth token")
            try:
                creds.refresh(Request())
            except Exception as exc:
                raise ContactsClientError(
                    f"Failed to refresh Contacts OAuth token: {exc}. "
                    "Re-run with '--contacts-oauth' to re-authorize."
                ) from exc
            self._save_token(creds)
            return creds
        raise ContactsClientError(
            "Cached Contacts OAuth token is invalid and cannot be refreshed. "
            "Re-run with '--contacts-oauth' to re-authorize."
        )

    def _save_token(self, creds: Credentials) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self._token_file)), exist_ok=True)
        with open(self._token_file, "w", encoding="utf-8") as fh:
            fh.write(creds.to_json())
        try:
            os.chmod(self._token_file, 0o600)
        except OSError:
            logger.debug("Could not chmod Contacts token file (non-fatal)")

    def _get_service(self):
        if self._service is None:
            creds = self._load_credentials()
            self._service = build(
                "people", "v1", credentials=creds, cache_discovery=False
            )
            logger.debug("People API service initialized")
        return self._service

    # ------------------------------------------------------------------ #
    # Connection check
    # ------------------------------------------------------------------ #

    def check_connection(self) -> str:
        """Verify credentials work. Returns a confirmation string."""
        try:
            result = (
                self._get_service()
                .people()
                .connections()
                .list(
                    resourceName="people/me",
                    pageSize=1,
                    personFields="names",
                )
                .execute()
            )
        except HttpError as exc:
            raise ContactsClientError(f"Contacts connection check failed: {exc}") from exc
        total = result.get("totalPeople", result.get("totalItems", "?"))
        logger.info("Connected to Contacts (total contacts: %s)", total)
        return f"contacts-api (found {total} contact(s))"

    # ------------------------------------------------------------------ #
    # Read operations
    # ------------------------------------------------------------------ #

    def list_contacts(self, max_results: int = 50) -> list[Contact]:
        """List contacts from the authenticated user's address book."""
        max_results = max(1, min(int(max_results), 1000))
        try:
            result = (
                self._get_service()
                .people()
                .connections()
                .list(
                    resourceName="people/me",
                    pageSize=max_results,
                    personFields=_PERSON_FIELDS,
                )
                .execute()
            )
        except HttpError as exc:
            raise ContactsClientError(f"list_contacts failed: {exc}") from exc
        contacts = [
            _parse_person(p) for p in result.get("connections", [])
        ]
        logger.info("list_contacts returned %d contacts", len(contacts))
        return contacts

    def search_contacts(self, query: str, max_results: int = 20) -> list[Contact]:
        """Search contacts by name/email using the People API searchContacts endpoint."""
        max_results = max(1, min(int(max_results), 1000))
        service = self._get_service()
        try:
            result = (
                service.people()
                .searchContacts(
                    query=query,
                    readMask=_PERSON_FIELDS,
                    pageSize=max_results,
                )
                .execute()
            )
            contacts = [
                _parse_person(r.get("person", r))
                for r in result.get("results", [])
            ]
        except HttpError as exc:
            logger.warning("searchContacts failed (%s); falling back to connections.list", exc)
            contacts = []

        if not contacts:
            # Fallback: list all and filter client-side
            all_contacts = self.list_contacts(max_results=1000)
            q = query.lower()
            contacts = [
                c for c in all_contacts
                if q in c.display_name.lower()
                or any(q in e.value.lower() for e in c.emails)
            ][:max_results]

        logger.info("search_contacts query=%r returned %d", query, len(contacts))
        return contacts

    def get_contact(self, resource_name: str) -> Contact:
        """Fetch a single contact by resource name."""
        try:
            person = (
                self._get_service()
                .people()
                .get(resourceName=resource_name, personFields=_PERSON_FIELDS)
                .execute()
            )
        except HttpError as exc:
            raise ContactsClientError(f"get_contact({resource_name}) failed: {exc}") from exc
        contact = _parse_person(person)
        logger.info("get_contact %s: %s", resource_name, contact.short_summary())
        return contact

    # ------------------------------------------------------------------ #
    # Write operations
    # ------------------------------------------------------------------ #

    def update_contact(
        self,
        resource_name: str,
        display_name: str | None = None,
        emails: list[dict] | None = None,
        phones: list[dict] | None = None,
        organization: str | None = None,
        job_title: str | None = None,
        notes: str | None = None,
    ) -> Contact:
        """Update a contact. Only provided fields are changed.

        Fetches the current person first to obtain the etag and preserve
        un-touched fields.
        """
        service = self._get_service()
        # Fetch current data + etag.
        try:
            person = (
                service.people()
                .get(resourceName=resource_name, personFields=_PERSON_FIELDS)
                .execute()
            )
        except HttpError as exc:
            raise ContactsClientError(
                f"update_contact: fetch failed for {resource_name}: {exc}"
            ) from exc

        etag = person.get("etag", "")
        update_fields: list[str] = []

        if display_name is not None:
            names = person.get("names", [{}])
            if names:
                names[0]["displayName"] = display_name
                names[0]["givenName"] = display_name.split()[0] if display_name else ""
                names[0]["familyName"] = " ".join(display_name.split()[1:]) if display_name else ""
            else:
                names = [{"displayName": display_name}]
            person["names"] = names
            update_fields.append("names")

        if emails is not None:
            person["emailAddresses"] = [
                {"value": e.get("value", ""), "type": e.get("type", "")}
                for e in emails
            ]
            update_fields.append("emailAddresses")

        if phones is not None:
            person["phoneNumbers"] = [
                {"value": p.get("value", ""), "type": p.get("type", "")}
                for p in phones
            ]
            update_fields.append("phoneNumbers")

        if organization is not None or job_title is not None:
            orgs = person.get("organizations", [{}])
            if not orgs:
                orgs = [{}]
            if organization is not None:
                orgs[0]["name"] = organization
            if job_title is not None:
                orgs[0]["title"] = job_title
            person["organizations"] = orgs
            update_fields.append("organizations")

        if notes is not None:
            person["biographies"] = [{"value": notes, "contentType": "TEXT_PLAIN"}]
            update_fields.append("biographies")

        if not update_fields:
            logger.info("update_contact: no fields to update for %s", resource_name)
            return _parse_person(person)

        person["etag"] = etag
        try:
            updated = (
                service.people()
                .updateContact(
                    resourceName=resource_name,
                    updatePersonFields=",".join(update_fields),
                    body=person,
                )
                .execute()
            )
        except HttpError as exc:
            raise ContactsClientError(f"update_contact failed: {exc}") from exc

        contact = _parse_person(updated)
        logger.info("update_contact %s: %s", resource_name, contact.short_summary())
        return contact


# ------------------------------------------------------------------ #
# Parsing helper
# ------------------------------------------------------------------ #

def _parse_person(person: dict[str, Any]) -> Contact:
    """Normalize a raw People API person resource into a Contact."""
    resource_name = person.get("resourceName", "")

    # Names
    names = person.get("names", [])
    primary_name = names[0] if names else {}
    display_name = primary_name.get("displayName", "")
    given_name = primary_name.get("givenName", "")
    family_name = primary_name.get("familyName", "")

    # Emails
    emails = [
        ContactEmail(
            value=e.get("value", ""),
            type=e.get("type", ""),
        )
        for e in person.get("emailAddresses", [])
    ]

    # Phones
    phones = [
        ContactPhone(
            value=p.get("value", ""),
            type=p.get("type", ""),
        )
        for p in person.get("phoneNumbers", [])
    ]

    # Organization
    orgs = person.get("organizations", [])
    primary_org = orgs[0] if orgs else {}
    organization = primary_org.get("name", "")
    job_title = primary_org.get("title", "")

    # Notes / biographies
    bios = person.get("biographies", [])
    notes = bios[0].get("value", "") if bios else ""

    # Photo
    photos = person.get("photos", [])
    photo_url = photos[0].get("url", "") if photos else ""

    return Contact(
        resource_name=resource_name,
        display_name=display_name,
        given_name=given_name,
        family_name=family_name,
        emails=emails,
        phones=phones,
        organization=organization,
        job_title=job_title,
        notes=notes,
        photo_url=photo_url,
    )
