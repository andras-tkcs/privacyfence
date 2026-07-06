"""Unit tests for privacyfence.connectors.salesforce.SalesforceConnector.

Two real bugs found while writing these tests, both fixed in
connectors/salesforce.py:

1. salesforce_get_record read record_dict.get("Name") after
   dataclasses.asdict(SalesforceRecord(...)), but SalesforceRecord nests
   actual Salesforce fields under a "fields" key -- "Name" is never a
   top-level key, so the preview/summary always fell back to showing the
   raw record_id instead of the record's real name.
2. salesforce_run_report read result_dict.get("name")/get("reportName") at
   the top level, but Salesforce's Analytics REST API nests the report's
   name under reportMetadata.name -- same class of bug, same always-None
   result.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.connectors import salesforce as salesforce_module
from privacyfence.connectors.salesforce import SalesforceConnector
from privacyfence.salesforce_client import SalesforceClientError, SalesforceRecord, SalesforceReport


def make_connector(my_email="me@example.com"):
    client = MagicMock()
    connector = SalesforceConnector(client)
    connector.my_email = my_email
    return connector, client


@pytest.fixture
def gated_call_spy(monkeypatch):
    calls = []

    async def fake_gated_call(**kwargs):
        calls.append(kwargs)
        return kwargs["filtered_data"]

    monkeypatch.setattr(salesforce_module, "gated_call", fake_gated_call)
    return calls


class TestDispatch:
    async def test_unknown_tool_raises(self):
        connector, _client = make_connector()
        with pytest.raises(ValueError, match="Unknown Salesforce tool"):
            await connector.call("salesforce_does_not_exist", {})


class TestListReports:
    async def test_auto_accepts_and_serializes(self, tmp_path):
        init_audit_logger(str(tmp_path))
        connector, client = make_connector()
        client.list_reports.return_value = [
            SalesforceReport(id="00O1", name="Pipeline", report_type="Tabular", folder_name="Sales", description=""),
        ]

        result = await connector.call("salesforce_list_reports", {})

        assert result == [{
            "id": "00O1", "name": "Pipeline", "report_type": "Tabular",
            "folder_name": "Sales", "description": "",
        }]
        entries = (tmp_path / f"{current_week()}.jsonl").read_text(encoding="utf-8").splitlines()
        assert '"decision": "auto_accepted"' in entries[0]

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.list_reports.side_effect = SalesforceClientError("session expired")

        with pytest.raises(RuntimeError, match="session expired"):
            await connector.call("salesforce_list_reports", {})


class TestGetRecord:
    async def test_preview_shows_actual_record_name_not_record_id(self, gated_call_spy):
        # Regression test for bug #1: Name lives under record.fields, not at
        # the top level of asdict(record).
        connector, client = make_connector()
        client.get_record.return_value = SalesforceRecord(
            object_type="Account", id="001xx0000012345",
            fields={"Name": "Acme Corp", "Industry": "Technology"},
        )

        await connector.call("salesforce_get_record", {"object_type": "Account", "record_id": "001xx0000012345"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Name"] == "Acme Corp"
        assert kwargs["preview"]["Name"] != "001xx0000012345"
        assert kwargs["summary"] == "Read Account: Acme Corp"

    async def test_falls_back_to_record_id_when_no_name_field(self, gated_call_spy):
        connector, client = make_connector()
        client.get_record.return_value = SalesforceRecord(
            object_type="Task", id="00T1", fields={"Subject": "Follow up"},
        )

        await connector.call("salesforce_get_record", {"object_type": "Task", "record_id": "00T1"})

        assert gated_call_spy[0]["preview"]["Name"] == "00T1"

    async def test_lowercase_name_field_also_recognized(self, gated_call_spy):
        connector, client = make_connector()
        client.get_record.return_value = SalesforceRecord(
            object_type="CustomObject__c", id="a001", fields={"name": "lowercase name"},
        )

        await connector.call("salesforce_get_record", {"object_type": "CustomObject__c", "record_id": "a001"})

        assert gated_call_spy[0]["preview"]["Name"] == "lowercase name"

    async def test_preview_and_gate(self, gated_call_spy):
        connector, client = make_connector()
        client.get_record.return_value = SalesforceRecord(
            object_type="Contact", id="003xx", fields={"Name": "Bob Smith", "Email": "bob@example.com"},
        )

        result = await connector.call("salesforce_get_record", {"object_type": "Contact", "record_id": "003xx"})

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "review"
        assert kwargs["preview"]["Object type"] == "Contact"
        assert kwargs["preview"]["Record ID"] == "003xx"
        assert kwargs["args"] == {"object_type": "Contact", "record_id": "003xx"}
        assert kwargs["raw_data"] == client.get_record.return_value
        assert result == {"object_type": "Contact", "id": "003xx", "fields": {"Name": "Bob Smith", "Email": "bob@example.com"}}

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.get_record.side_effect = SalesforceClientError("insufficient access")

        with pytest.raises(RuntimeError, match="insufficient access"):
            await connector.call("salesforce_get_record", {"object_type": "Account", "record_id": "x"})


class TestRunReport:
    async def test_preview_shows_actual_report_name_from_report_metadata(self, gated_call_spy):
        # Regression test for bug #2: Salesforce's real report-run response
        # nests the name under reportMetadata.name, not top-level "name".
        connector, client = make_connector()
        client.run_report.return_value = {
            "reportMetadata": {"id": "00O1", "name": "Q3 Pipeline Report"},
            "factMap": {},
        }

        await connector.call("salesforce_run_report", {"report_id": "00O1"})

        kwargs = gated_call_spy[0]
        assert kwargs["preview"]["Report"] == "Q3 Pipeline Report"
        assert kwargs["summary"] == "Run report: Q3 Pipeline Report"

    async def test_falls_back_to_top_level_name_if_present(self, gated_call_spy):
        connector, client = make_connector()
        client.run_report.return_value = {"name": "Flat Name Report"}

        await connector.call("salesforce_run_report", {"report_id": "00O2"})

        assert gated_call_spy[0]["preview"]["Report"] == "Flat Name Report"

    async def test_falls_back_to_report_id_when_name_unavailable(self, gated_call_spy):
        connector, client = make_connector()
        client.run_report.return_value = {"factMap": {}}

        await connector.call("salesforce_run_report", {"report_id": "00O3"})

        assert gated_call_spy[0]["preview"]["Report"] == "00O3"

    async def test_preview_and_gate(self, gated_call_spy):
        connector, client = make_connector()
        client.run_report.return_value = {"reportMetadata": {"name": "Report X"}}

        result = await connector.call("salesforce_run_report", {"report_id": "00O1"})

        kwargs = gated_call_spy[0]
        assert kwargs["gate"] == "review"
        assert kwargs["preview"]["Report ID"] == "00O1"
        assert kwargs["args"] == {"report_id": "00O1"}
        assert kwargs["sender"] == "Salesforce"
        assert result == client.run_report.return_value

    async def test_client_error_becomes_runtime_error(self):
        connector, client = make_connector()
        client.run_report.side_effect = SalesforceClientError("report locked")

        with pytest.raises(RuntimeError, match="report locked"):
            await connector.call("salesforce_run_report", {"report_id": "00O1"})
