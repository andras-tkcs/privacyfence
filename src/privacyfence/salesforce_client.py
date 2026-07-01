"""Salesforce REST API client.

Uses the `simple-salesforce` library if available; otherwise raises a clear
error. Config required: instance_url + (username/password/security_token) OR
access_token.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


class SalesforceClientError(Exception):
    """Raised for unrecoverable Salesforce client problems (config, API)."""


@dataclass
class SalesforceReport:
    id: str
    name: str
    report_type: str
    folder_name: str
    description: str


@dataclass
class SalesforceRecord:
    object_type: str
    id: str
    fields: dict


class SalesforceClient:
    """Salesforce client backed by simple-salesforce."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._sf = None  # lazily initialized

    def _get_sf(self):
        if self._sf is not None:
            return self._sf
        try:
            from simple_salesforce import Salesforce
        except ImportError as exc:
            raise SalesforceClientError(
                "The 'simple-salesforce' package is not installed. "
                "Run: pip install simple-salesforce"
            ) from exc

        instance_url = self._config.get("instance_url", "")
        access_token = self._config.get("access_token", "")
        username = self._config.get("username", "")
        password = self._config.get("password", "")
        security_token = self._config.get("security_token", "")

        if not instance_url:
            raise SalesforceClientError(
                "Salesforce 'instance_url' is not configured in settings.yaml."
            )

        try:
            if access_token:
                # Strip protocol from instance_url for simple-salesforce
                instance = instance_url.replace("https://", "").rstrip("/")
                self._sf = Salesforce(instance=instance, session_id=access_token)
            elif username and password:
                self._sf = Salesforce(
                    username=username,
                    password=password,
                    security_token=security_token,
                    instance_url=instance_url,
                )
            else:
                raise SalesforceClientError(
                    "Salesforce config requires either 'access_token' or "
                    "'username' + 'password' + 'security_token' in settings.yaml."
                )
        except Exception as exc:
            if isinstance(exc, SalesforceClientError):
                raise
            raise SalesforceClientError(f"Salesforce authentication failed: {exc}") from exc

        return self._sf

    def check_connection(self) -> str:
        """Verify credentials. Returns the org name."""
        sf = self._get_sf()
        try:
            result = sf.query("SELECT Id, Name FROM Organization LIMIT 1")
            records = result.get("records", [])
            org_name = records[0].get("Name", "unknown") if records else "unknown"
        except Exception as exc:
            raise SalesforceClientError(f"Salesforce connection check failed: {exc}") from exc
        logger.info("Connected to Salesforce org: %s", org_name)
        return org_name

    def list_reports(self) -> list[SalesforceReport]:
        """List reports accessible to the authenticated user."""
        sf = self._get_sf()
        try:
            result = sf.query(
                "SELECT Id, Name, Description, FolderName, DeveloperName "
                "FROM Report ORDER BY Name LIMIT 200"
            )
        except Exception as exc:
            raise SalesforceClientError(f"list_reports failed: {exc}") from exc
        reports = []
        for raw in result.get("records", []):
            reports.append(SalesforceReport(
                id=raw.get("Id", ""),
                name=raw.get("Name", ""),
                report_type=raw.get("DeveloperName", ""),
                folder_name=raw.get("FolderName", ""),
                description=raw.get("Description", ""),
            ))
        logger.info("list_reports returned %d report(s)", len(reports))
        return reports

    def get_record(self, object_type: str, record_id: str) -> SalesforceRecord:
        """Fetch a single record by object type and id."""
        if not object_type or not record_id:
            raise SalesforceClientError("get_record requires object_type and record_id")
        sf = self._get_sf()
        try:
            obj = getattr(sf, object_type)
            raw = obj.get(record_id)
        except AttributeError as exc:
            raise SalesforceClientError(f"Unknown Salesforce object type: {object_type!r}") from exc
        except Exception as exc:
            raise SalesforceClientError(
                f"get_record({object_type}, {record_id}) failed: {exc}"
            ) from exc
        # Remove Salesforce internal keys
        fields = {k: v for k, v in raw.items() if not k.startswith("attributes")}
        return SalesforceRecord(object_type=object_type, id=record_id, fields=fields)

    def run_report(self, report_id: str) -> dict:
        """Run a Salesforce report and return its result as a dict."""
        if not report_id:
            raise SalesforceClientError("run_report requires a report_id")
        sf = self._get_sf()
        try:
            result = sf.restful(
                f"analytics/reports/{report_id}",
                method="POST",
                json={"reportMetadata": {}},
            )
        except Exception as exc:
            raise SalesforceClientError(f"run_report({report_id}) failed: {exc}") from exc
        logger.info("run_report %s completed", report_id)
        return result
