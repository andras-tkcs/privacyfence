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
from ..gate import current_reason, gated_call
from ..privacy_filter import apply_text

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
                    "List contacts from the user's Google address book. Google blends "
                    "personally-saved contacts together with Workspace directory profiles "
                    "(colleagues) by default; use 'source' to split them apart. "
                    "Returns display name, emails, phones, organization, job title, and a "
                    "'source' field ('personal', 'directory', or 'both' if the same person "
                    "is both a saved contact and a colleague). Auto-approved."
                ),
                params=[
                    ToolParam("max_results", "int", required=False, default=50),
                    ToolParam("source", "str", required=False, default="both",
                              description="'personal' (saved contacts only), 'directory' "
                                           "(Workspace directory only), or 'both' (default)."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="contacts_search",
                description=(
                    "Search contacts by name or email address. Use 'source' to search only "
                    "personally-saved contacts, only Workspace directory contacts, or both "
                    "(default). Note: 'directory' search only finds directory profiles you "
                    "already have some contact history with; there is no full company-directory "
                    "search under this app's permissions. Auto-approved."
                ),
                params=[
                    ToolParam("query", "str"),
                    ToolParam("max_results", "int", required=False, default=20),
                    ToolParam("source", "str", required=False, default="both",
                              description="'personal', 'directory', or 'both' (default)."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
                read_only=True,
            ),
            ToolSpec(
                name="contacts_get",
                description=(
                    "Fetch a single contact by resource name (e.g. 'people/c12345'). "
                    "'source' asserts the expected kind of contact ('personal', 'directory', "
                    "or 'both'/default); the call fails if the resource doesn't match. "
                    "Auto-approved."
                ),
                params=[
                    ToolParam("resource_name", "str"),
                    ToolParam("source", "str", required=False, default="both",
                              description="'personal', 'directory', or 'both' (default)."),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
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
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="contacts_create",
                description=(
                    "Create a new contact in the user's Google address book. "
                    "Requires user approval. "
                    "emails and phones are JSON strings, e.g. "
                    "'[{\"value\": \"a@b.com\", \"type\": \"work\"}]'. "
                    "Contact deletion is not supported."
                ),
                params=[
                    ToolParam("display_name", "str"),
                    ToolParam("emails", "str", required=False, default="",
                              description="JSON array of {value, type} dicts"),
                    ToolParam("phones", "str", required=False, default="",
                              description="JSON array of {value, type} dicts"),
                    ToolParam("organization", "str", required=False, default=""),
                    ToolParam("job_title", "str", required=False, default=""),
                    ToolParam("notes", "str", required=False, default=""),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="contacts_add_label",
                description=(
                    "Add a label to a contact, creating the label if it doesn't already exist. "
                    "Requires user approval."
                ),
                params=[
                    ToolParam("resource_name", "str"),
                    ToolParam("label_name", "str"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
                ],
            ),
            ToolSpec(
                name="contacts_remove_label",
                description="Remove a label from a contact. Requires user approval.",
                params=[
                    ToolParam("resource_name", "str"),
                    ToolParam("label_name", "str"),
                    ToolParam("reason", "str", required=True, description="One sentence: why are you calling this tool right now?"),
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
        if tool == "contacts_create":
            return await self._contacts_create(**args)
        if tool == "contacts_add_label":
            return await self._contacts_add_label(**args)
        if tool == "contacts_remove_label":
            return await self._contacts_remove_label(**args)
        raise ValueError(f"Unknown Contacts tool: {tool!r}")

    # ------------------------------------------------------------------ #
    # Auto
    # ------------------------------------------------------------------ #

    async def _contacts_list(self, max_results: int = 50, source: str = "both") -> Any:
        t0 = time.time()
        contacts = await self._fetch(self._contacts.list_contacts, max_results, source)
        result = [_redact_notes(c.to_dict()) for c in contacts]
        self._auto_audit("contacts_list", "List Contacts",
                         f"List contacts (max {max_results}, source={source})", f"{len(result)} contact(s)", t0)
        return result

    async def _contacts_search(self, query: str, max_results: int = 20, source: str = "both") -> Any:
        t0 = time.time()
        contacts = await self._fetch(self._contacts.search_contacts, query, max_results, source)
        result = [_redact_notes(c.to_dict()) for c in contacts]
        self._auto_audit("contacts_search", "Search Contacts",
                         f"Search: {query!r} (source={source})", f"{len(result)} result(s)", t0)
        return result

    async def _contacts_get(self, resource_name: str, source: str = "both") -> Any:
        t0 = time.time()
        contact = await self._fetch(self._contacts.get_contact, resource_name, source)
        result = _redact_notes(contact.to_dict())
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

        contact_name = await self._contact_name_for(resource_name)

        preview = {"Contact": contact_name}
        if display_name:
            preview["Name"] = display_name
        if emails_list:
            preview["Emails"] = ", ".join(e.get("value", "") for e in emails_list)
        if phones_list:
            preview["Phones"] = ", ".join(p.get("value", "") for p in phones_list)
        if organization:
            preview["Organization"] = organization
        if job_title:
            preview["Job title"] = job_title

        args = {
            "resource_name": resource_name, "display_name": display_name,
            "emails": emails, "phones": phones, "organization": organization,
            "job_title": job_title, "notes": notes,
        }
        if notes:
            details_text = notes
        else:
            changed_fields = ", ".join(k for k in preview if k != "Contact") or "no fields"
            details_text = f"{changed_fields} will be updated; notes unchanged."
        await gated_call(
            connector=self.name,
            tool="contacts_update",
            tool_name="Update Contact",
            summary=f"Update contact: {contact_name}",
            sender=contact_name,
            raw_data=args,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=details_text,
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

    async def _contacts_create(
        self,
        display_name: str,
        emails: str = "",
        phones: str = "",
        organization: str = "",
        job_title: str = "",
        notes: str = "",
    ) -> Any:
        emails_list = _parse_json_list(emails)
        phones_list = _parse_json_list(phones)

        preview = {"Name": display_name}
        if emails_list:
            preview["Emails"] = ", ".join(e.get("value", "") for e in emails_list)
        if phones_list:
            preview["Phones"] = ", ".join(p.get("value", "") for p in phones_list)
        if organization:
            preview["Organization"] = organization
        if job_title:
            preview["Job title"] = job_title

        args = {
            "display_name": display_name, "emails": emails, "phones": phones,
            "organization": organization, "job_title": job_title, "notes": notes,
        }
        await gated_call(
            connector=self.name,
            tool="contacts_create",
            tool_name="Create Contact",
            summary=f"Create contact: {display_name}",
            sender=display_name,
            raw_data=args,
            filtered_data=None,
            gate="popup",
            preview=preview,
            details_text=notes or "No notes provided; see preview for contact details.",
            my_email=self.my_email,
            args=args,
        )
        created = await self._fetch(
            self._contacts.create_contact,
            display_name, emails_list, phones_list,
            organization or None, job_title or None, notes or None,
        )
        return created.to_dict()

    async def _contacts_add_label(self, resource_name: str, label_name: str) -> Any:
        contact_name = await self._contact_name_for(resource_name)
        args = {"resource_name": resource_name, "label_name": label_name}
        await gated_call(
            connector=self.name,
            tool="contacts_add_label",
            tool_name="Add Contact Label",
            summary=f"Add label '{label_name}' to: {contact_name}",
            sender=contact_name,
            raw_data=args,
            filtered_data=None,
            gate="popup",
            preview={"Contact": contact_name, "Label": label_name},
            details_text="Label will be added to this contact; no other fields change.",
            my_email=self.my_email,
            args=args,
        )
        return await self._fetch(self._contacts.add_label, resource_name, label_name)

    async def _contacts_remove_label(self, resource_name: str, label_name: str) -> Any:
        contact_name = await self._contact_name_for(resource_name)
        args = {"resource_name": resource_name, "label_name": label_name}
        await gated_call(
            connector=self.name,
            tool="contacts_remove_label",
            tool_name="Remove Contact Label",
            summary=f"Remove label '{label_name}' from: {contact_name}",
            sender=contact_name,
            raw_data=args,
            filtered_data=None,
            gate="popup",
            preview={"Contact": contact_name, "Label": label_name},
            details_text="Label will be removed from this contact; no other fields change.",
            my_email=self.my_email,
            args=args,
        )
        return await self._fetch(self._contacts.remove_label, resource_name, label_name)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _contact_name_for(self, resource_name: str) -> str:
        try:
            current = await self._fetch(self._contacts.get_contact, resource_name)
            return current.display_name or resource_name
        except Exception:
            return resource_name

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
                claude_reason=current_reason(),
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


def _redact_notes(contact_dict: dict[str, Any]) -> dict[str, Any]:
    """Apply contacts_privacy's "notes" category to a contact's free-text
    biography field -- the one field on a contact that can carry arbitrary
    personal content, unlike the structured name/email/phone/org fields
    around it, none of which have a category of their own (see
    privacy_filter.py's module docstring for scope)."""
    contact_dict["notes"] = apply_text("contacts_privacy", "notes", contact_dict.get("notes", "") or "")
    return contact_dict
