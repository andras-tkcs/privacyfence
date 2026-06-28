"""Google Contacts connector."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..contacts_client import ContactsClient, ContactsClientError
from ..gate import gated_call

logger = logging.getLogger(__name__)


class ContactsConnector(Connector):
    def __init__(self, client: ContactsClient) -> None:
        self._contacts = client
        self.my_email: str = ""

    @property
    def name(self) -> str:
        return "contacts"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="contacts_list",
                description=(
                    "List contacts from the user's Google address book. "
                    "Returns display name, emails, phones, organization, and job title. Auto-approved."
                ),
                params=[ToolParam("max_results", "int", required=False, default=50)],
                read_only=True,
            ),
            ToolSpec(
                name="contacts_search",
                description="Search contacts by name or email address. Auto-approved.",
                params=[
                    ToolParam("query", "str"),
                    ToolParam("max_results", "int", required=False, default=20),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="contacts_get",
                description="Fetch a single contact by resource name (e.g. 'people/c12345'). Auto-approved.",
                params=[ToolParam("resource_name", "str")],
                read_only=True,
            ),
            ToolSpec(
                name="contacts_update",
                description=(
                    "Update a contact's fields. Provide only the fields you want to change. "
                    "Requires user approval. "
                    "emails and phones are JSON strings, e.g. "
                    "'[{\"value\": \"a@b.com\", \"type\": \"work\"}]'."
                ),
                params=[
                    ToolParam("resource_name", "str"),
                    ToolParam("display_name", "str", required=False, default=""),
                    ToolParam("emails", "str", required=False, default="",
                              description="JSON array of {value, type} dicts"),
                    ToolParam("phones", "str", required=False, default="",
                              description="JSON array of {value, type} dicts"),
                    ToolParam("organization", "str", required=False, default=""),
                    ToolParam("job_title", "str", required=False, default=""),
                    ToolParam("notes", "str", required=False, default=""),
                ],
            ),
        ]

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "contacts_list":
            return await self._contacts_list(**args)
        if tool == "contacts_search":
            return await self._contacts_search(**args)
        if tool == "contacts_get":
            return await self._contacts_get(**args)
        if tool == "contacts_update":
            return await self._contacts_update(**args)
        raise ValueError(f"Unknown Contacts tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Auto
    # ------------------------------------------------------------------ #

    async def _contacts_list(self, max_results: int = 50) -> Any:
        t0 = time.time()
        contacts = await self._fetch(self._contacts.list_contacts, max_results)
        result = [c.to_dict() for c in contacts]
        self._auto_audit("contacts_list", "List Contacts",
                         f"List contacts (max {max_results})", f"{len(result)} contact(s)", t0)
        return result

    async def _contacts_search(self, query: str, max_results: int = 20) -> Any:
        t0 = time.time()
        contacts = await self._fetch(self._contacts.search_contacts, query, max_results)
        result = [c.to_dict() for c in contacts]
        self._auto_audit("contacts_search", "Search Contacts",
                         f"Search: {query!r}", f"{len(result)} result(s)", t0)
        return result

    async def _contacts_get(self, resource_name: str) -> Any:
        t0 = time.time()
        contact = await self._fetch(self._contacts.get_contact, resource_name)
        result = contact.to_dict()
        self._auto_audit("contacts_get", "Get Contact",
                         f"Get: {resource_name}", contact.display_name or resource_name, t0)
        return result

    # ------------------------------------------------------------------ #
    # Popup gate (writes)
    # ------------------------------------------------------------------ #

    async def _contacts_update(
        self,
        resource_name: str,
        display_name: str = "",
        emails: str = "",
        phones: str = "",
        organization: str = "",
        job_title: str = "",
        notes: str = "",
    ) -> Any:
        emails_list: list[dict] | None = _parse_json_list(emails)
        phones_list: list[dict] | None = _parse_json_list(phones)

        try:
            current = await self._fetch(self._contacts.get_contact, resource_name)
            contact_name = current.display_name or resource_name
        except Exception:
            contact_name = resource_name

        changes: list[str] = []
        if display_name:
            changes.append(f"Name: {display_name}")
        if emails_list:
            changes.append(f"Emails: {', '.join(e.get('value', '') for e in emails_list)}")
        if phones_list:
            changes.append(f"Phones: {', '.join(p.get('value', '') for p in phones_list)}")
        if organization:
            changes.append(f"Organization: {organization}")
        if job_title:
            changes.append(f"Job title: {job_title}")
        if notes:
            changes.append(f"Notes: {notes[:100]}")
        details = f"Contact: {contact_name}\n\nChanges:\n" + "\n".join(f"  {c}" for c in changes)

        args = {
            "resource_name": resource_name, "display_name": display_name,
            "emails": emails, "phones": phones, "organization": organization,
            "job_title": job_title, "notes": notes,
        }
        await gated_call(
            connector=self.name,
            tool="contacts_update",
            tool_name="Update Contact",
            summary=f"Update contact: {contact_name}",
            sender=contact_name,
            raw_data=args,
            filtered_data=None,
            gate="popup",
            details_text=details,
            my_email=self.my_email,
            args=args,
        )
        updated = await self._fetch(
            self._contacts.update_contact,
            resource_name,
            display_name or None,
            emails_list,
            phones_list,
            organization or None,
            job_title or None,
            notes or None,
        )
        return updated.to_dict()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _fetch(self, func, *args) -> Any:
        try:
            return await asyncio.to_thread(func, *args)
        except ContactsClientError as exc:
            logger.error("Contacts fetch failed: %s", exc)
            raise RuntimeError(str(exc)) from exc

    def _auto_audit(
        self, tool: str, tool_name: str, summary: str, sender: str, created_at: float
    ) -> None:
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id="",
                connector=self.name,
                tool=tool,
                tool_name=tool_name,
                summary=summary,
                sender=sender,
                decision="auto_accepted",
                auto_accept_rule="auto",
                latency_seconds=time.time() - created_at,
            ))
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)


def _parse_json_list(value: str) -> list[dict] | None:
    if not value or not value.strip():
        return None
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else None
    except (json.JSONDecodeError, ValueError):
        return None
