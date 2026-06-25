"""Contacts connector: wraps ContactsClient with always-allowed reads and gated writes."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..connector import Connector, ToolParam, ToolSpec
from ..contacts_client import Contact, ContactsClient, ContactsClientError
from ..gate import gated_call

logger = logging.getLogger(__name__)


class ContactsConnector(Connector):
    """Connector for Google Contacts (People API).

    Read tools (list, search, get) are always-allowed and never enter the review
    queue.  The write tool (update) is gated and may require user approval.
    """

    def __init__(
        self,
        client: ContactsClient,
        rules_config: dict[str, Any] | None = None,
    ) -> None:
        self._contacts = client
        self._rules_config = rules_config or {}
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
                    "Returns display name, emails, phones, organization, and job title. "
                    "Auto-approved — no sensitive data gate."
                ),
                params=[
                    ToolParam("max_results", "int", required=False, default=50),
                ],
            read_only=True,
            ),
            ToolSpec(
                name="contacts_search",
                description=(
                    "Search contacts by name or email address. "
                    "Auto-approved — no sensitive data gate."
                ),
                params=[
                    ToolParam("query", "str"),
                    ToolParam("max_results", "int", required=False, default=20),
                ],
            read_only=True,
            ),
            ToolSpec(
                name="contacts_get",
                description=(
                    "Fetch a single contact by resource name (e.g. 'people/c12345'). "
                    "Auto-approved."
                ),
                params=[
                    ToolParam("resource_name", "str"),
                ],
            read_only=True,
            ),
            ToolSpec(
                name="contacts_update",
                description=(
                    "Update a contact's fields. Provide only the fields you want to change. "
                    "Requires user approval unless an auto-accept rule matches. "
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
    # Always-allowed read tools
    # ------------------------------------------------------------------ #

    async def _contacts_list(self, max_results: int = 50) -> Any:
        contacts = await self._fetch(self._contacts.list_contacts, max_results)
        result = [c.to_dict() for c in contacts]
        self._log_always_allowed("contacts_list", {"max_results": max_results})
        logger.info("contacts_list auto-accepted: %d contacts", len(result))
        return result

    async def _contacts_search(self, query: str, max_results: int = 20) -> Any:
        contacts = await self._fetch(self._contacts.search_contacts, query, max_results)
        result = [c.to_dict() for c in contacts]
        self._log_always_allowed("contacts_search", {"query": query, "max_results": max_results})
        logger.info("contacts_search query=%r returned %d", query, len(result))
        return result

    async def _contacts_get(self, resource_name: str) -> Any:
        contact = await self._fetch(self._contacts.get_contact, resource_name)
        result = contact.to_dict()
        self._log_always_allowed("contacts_get", {"resource_name": resource_name})
        logger.info("contacts_get %s: %s", resource_name, contact.short_summary())
        return result

    # ------------------------------------------------------------------ #
    # Gated write tool
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

        updated_contact = await self._fetch(
            self._contacts.update_contact,
            resource_name,
            display_name or None,
            emails_list,
            phones_list,
            organization or None,
            job_title or None,
            notes or None,
        )
        filtered_data = updated_contact.to_dict()
        args = {
            "resource_name": resource_name,
            "display_name": display_name,
            "emails": emails,
            "phones": phones,
            "organization": organization,
            "job_title": job_title,
            "notes": notes,
        }
        summary = f"Update contact: {updated_contact.display_name} ({resource_name})"

        return await gated_call(
            connector_name=self.name,
            tool="contacts_update",
            args=args,
            operation_key="contacts.edit",
            summary=summary,
            sender=updated_contact.display_name,
            filtered_data=filtered_data,
            rules_config=self._rules_config,
            context={"my_email": self.my_email},
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _fetch(self, func, *args) -> Any:
        try:
            return await asyncio.to_thread(func, *args)
        except ContactsClientError as exc:
            logger.error("Contacts fetch failed: %s", exc)
            raise RuntimeError(str(exc)) from exc

    def _log_always_allowed(self, tool: str, args: dict[str, Any]) -> None:
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id="",
                connector=self.name,
                tool=tool,
                tool_name=tool,
                summary=str(args),
                sender="",
                decision="auto_accepted",
                auto_accept_rule="always_allowed",
                latency_seconds=0.0,
            ))
        except Exception as exc:
            logger.warning("Audit log write failed: %s", exc)


def _parse_json_list(value: str) -> list[dict] | None:
    """Parse a JSON string into a list of dicts. Returns None if empty or invalid."""
    if not value or not value.strip():
        return None
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return parsed
        logger.warning("contacts: expected JSON array, got %s", type(parsed).__name__)
        return None
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("contacts: failed to parse JSON list %r: %s", value[:80], exc)
        return None
