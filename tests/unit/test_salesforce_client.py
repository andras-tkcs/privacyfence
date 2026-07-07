"""Tests for SalesforceClient's refresh-and-retry session logic and result
normalization. The refresh-on-expired-session behavior (_call/_try_refresh)
is the most bug-prone part of this client -- it's what keeps a long-running
daemon from forcing re-authentication every time a session token expires --
so it gets the deepest coverage here.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from privacyfence.salesforce_client import (
    SalesforceClient,
    SalesforceClientError,
    SalesforceRecord,
    SalesforceReport,
    _is_expired_session_error,
    load_token_file,
)


def make_client(config: dict | None = None, token_file: str | None = None) -> SalesforceClient:
    base_config = {"access_token": "tok", "instance_url": "https://my.salesforce.com"}
    base_config.update(config or {})
    return SalesforceClient(config=base_config, token_file=token_file)


def with_fake_sf(client: SalesforceClient, sf: MagicMock) -> SalesforceClient:
    client._get_sf = lambda: sf
    return client


# ---------------------------------------------------------------------------- #
# load_token_file
# ---------------------------------------------------------------------------- #

class TestLoadTokenFile:
    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(SalesforceClientError, match="No Salesforce token found"):
            load_token_file(str(tmp_path / "nope.json"))

    def test_loads_valid_json(self, tmp_path):
        path = tmp_path / "token.json"
        path.write_text('{"access_token": "t", "instance_url": "https://x.com"}')
        assert load_token_file(str(path)) == {"access_token": "t", "instance_url": "https://x.com"}


# ---------------------------------------------------------------------------- #
# _is_expired_session_error
# ---------------------------------------------------------------------------- #

class TestIsExpiredSessionError:
    def test_invalid_session_id_detected(self):
        assert _is_expired_session_error(Exception("INVALID_SESSION_ID: Session expired or invalid"))

    def test_session_expired_text_detected(self):
        assert _is_expired_session_error(Exception("Session expired"))

    def test_unrelated_error_not_detected(self):
        assert not _is_expired_session_error(Exception("MALFORMED_QUERY"))


# ---------------------------------------------------------------------------- #
# _build_sf: config validation + instance URL normalization
# ---------------------------------------------------------------------------- #

class TestBuildSf:
    def test_missing_access_token_raises_not_authenticated(self):
        client = SalesforceClient(config={"instance_url": "https://x.com"})
        with pytest.raises(SalesforceClientError, match="not authenticated"):
            client._build_sf()

    def test_missing_instance_url_raises_not_authenticated(self):
        client = SalesforceClient(config={"access_token": "t"})
        with pytest.raises(SalesforceClientError, match="not authenticated"):
            client._build_sf()

    def test_instance_url_scheme_stripped_before_passing_to_salesforce(self, monkeypatch):
        captured = {}
        class FakeSalesforce:
            def __init__(self, **kwargs):
                captured.update(kwargs)
        monkeypatch.setattr("simple_salesforce.Salesforce", FakeSalesforce)

        client = make_client({"instance_url": "https://my.salesforce.com/"})
        client._build_sf()

        assert captured == {"instance": "my.salesforce.com", "session_id": "tok"}

    def test_salesforce_constructor_error_becomes_client_error(self, monkeypatch):
        class FakeSalesforce:
            def __init__(self, **kwargs):
                raise RuntimeError("bad session")
        monkeypatch.setattr("simple_salesforce.Salesforce", FakeSalesforce)

        client = make_client()
        with pytest.raises(SalesforceClientError, match="Salesforce authentication failed"):
            client._build_sf()


# ---------------------------------------------------------------------------- #
# _try_refresh
# ---------------------------------------------------------------------------- #

class TestTryRefresh:
    def test_missing_refresh_token_returns_false_without_http_call(self, monkeypatch):
        called = []
        monkeypatch.setattr("requests.post", lambda *a, **kw: called.append(1))
        client = make_client({"consumer_key": "ck", "consumer_secret": "cs"})
        assert client._try_refresh() is False
        assert called == []

    def test_missing_consumer_credentials_returns_false(self):
        client = make_client({"refresh_token": "rt"})
        assert client._try_refresh() is False

    def test_successful_refresh_updates_config_and_clears_cached_sf(self, monkeypatch):
        response = MagicMock()
        response.json.return_value = {"access_token": "new-tok", "instance_url": "https://new.salesforce.com"}
        monkeypatch.setattr("requests.post", lambda *a, **kw: response)

        client = make_client({"refresh_token": "rt", "consumer_key": "ck", "consumer_secret": "cs"})
        client._sf = "stale-sf-object"

        assert client._try_refresh() is True
        assert client._config["access_token"] == "new-tok"
        assert client._config["instance_url"] == "https://new.salesforce.com"
        assert client._sf is None

    def test_successful_refresh_persists_to_token_file_when_given(self, monkeypatch, tmp_path):
        response = MagicMock()
        response.json.return_value = {"access_token": "new-tok", "instance_url": "https://new.salesforce.com"}
        monkeypatch.setattr("requests.post", lambda *a, **kw: response)

        token_file = str(tmp_path / "token.json")
        client = make_client(
            {"refresh_token": "rt", "consumer_key": "ck", "consumer_secret": "cs"}, token_file=token_file,
        )

        client._try_refresh()

        saved = load_token_file(token_file)
        assert saved == {"access_token": "new-tok", "refresh_token": "rt", "instance_url": "https://new.salesforce.com"}

    def test_http_failure_returns_false(self, monkeypatch):
        import requests
        def raise_it(*a, **kw):
            raise requests.RequestException("network error")
        monkeypatch.setattr("requests.post", raise_it)

        client = make_client({"refresh_token": "rt", "consumer_key": "ck", "consumer_secret": "cs"})
        assert client._try_refresh() is False

    def test_login_url_defaults_when_absent(self, monkeypatch):
        captured_urls = []
        response = MagicMock()
        response.json.return_value = {"access_token": "t", "instance_url": "https://x.com"}
        def fake_post(url, **kw):
            captured_urls.append(url)
            return response
        monkeypatch.setattr("requests.post", fake_post)

        client = make_client({"refresh_token": "rt", "consumer_key": "ck", "consumer_secret": "cs"})
        client._try_refresh()

        assert captured_urls[0] == "https://login.salesforce.com/services/oauth2/token"


# ---------------------------------------------------------------------------- #
# _call: the refresh-and-retry wrapper
# ---------------------------------------------------------------------------- #

class TestCall:
    def test_happy_path_returns_fn_result(self):
        client = with_fake_sf(make_client(), MagicMock())
        result = client._call(lambda sf: "ok")
        assert result == "ok"

    def test_salesforce_client_error_propagates_without_retry(self):
        client = with_fake_sf(make_client(), MagicMock())
        def raiser(sf):
            raise SalesforceClientError("already a client error")
        with pytest.raises(SalesforceClientError, match="already a client error"):
            client._call(raiser)

    def test_non_expired_error_wraps_without_attempting_refresh(self, monkeypatch):
        refresh_called = []
        client = with_fake_sf(make_client(), MagicMock())
        monkeypatch.setattr(client, "_try_refresh", lambda: refresh_called.append(1) or True)

        def raiser(sf):
            raise RuntimeError("MALFORMED_QUERY: bad SOQL")

        with pytest.raises(SalesforceClientError, match="MALFORMED_QUERY"):
            client._call(raiser)
        assert refresh_called == []

    def test_expired_session_triggers_refresh_and_retry_succeeds(self, monkeypatch):
        client = with_fake_sf(make_client(), MagicMock())
        monkeypatch.setattr(client, "_try_refresh", lambda: True)

        calls = {"n": 0}
        def fn(sf):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("INVALID_SESSION_ID")
            return "retried-ok"

        result = client._call(fn)
        assert result == "retried-ok"
        assert calls["n"] == 2

    def test_expired_session_but_refresh_fails_reraises_original_wrapped(self, monkeypatch):
        client = with_fake_sf(make_client(), MagicMock())
        monkeypatch.setattr(client, "_try_refresh", lambda: False)

        def raiser(sf):
            raise RuntimeError("INVALID_SESSION_ID")

        with pytest.raises(SalesforceClientError, match="INVALID_SESSION_ID"):
            client._call(raiser)

    def test_expired_session_refresh_succeeds_but_retry_also_fails(self, monkeypatch):
        client = with_fake_sf(make_client(), MagicMock())
        monkeypatch.setattr(client, "_try_refresh", lambda: True)

        def fn(sf):
            raise RuntimeError("INVALID_SESSION_ID")

        with pytest.raises(SalesforceClientError, match="INVALID_SESSION_ID"):
            client._call(fn)


# ---------------------------------------------------------------------------- #
# check_connection / list_reports / get_record / run_report
# ---------------------------------------------------------------------------- #

class TestCheckConnection:
    def test_returns_org_name_from_query(self):
        sf = MagicMock()
        sf.query.return_value = {"records": [{"Name": "Acme Corp"}]}
        client = with_fake_sf(make_client(), sf)
        assert client.check_connection() == "Acme Corp"

    def test_no_records_returns_unknown(self):
        sf = MagicMock()
        sf.query.return_value = {"records": []}
        client = with_fake_sf(make_client(), sf)
        assert client.check_connection() == "unknown"


class TestListReports:
    def test_maps_query_results(self):
        sf = MagicMock()
        sf.query.return_value = {"records": [
            {"Id": "r1", "Name": "Sales Report", "Description": "d", "FolderName": "f", "DeveloperName": "Sales_Report"},
        ]}
        client = with_fake_sf(make_client(), sf)

        reports = client.list_reports()

        assert reports == [SalesforceReport(
            id="r1", name="Sales Report", report_type="Sales_Report", folder_name="f", description="d",
        )]


class TestGetRecord:
    def test_requires_object_type_and_record_id(self):
        client = make_client()
        with pytest.raises(SalesforceClientError, match="requires object_type and record_id"):
            client.get_record("", "id1")
        with pytest.raises(SalesforceClientError, match="requires object_type and record_id"):
            client.get_record("Account", "")

    def test_fetches_record_and_strips_attributes_key(self):
        sf = MagicMock()
        sf.Account.get.return_value = {
            "attributes": {"type": "Account", "url": "/x"}, "Id": "001", "Name": "Acme",
        }
        client = with_fake_sf(make_client(), sf)

        record = client.get_record("Account", "001")

        assert record == SalesforceRecord(object_type="Account", id="001", fields={"Id": "001", "Name": "Acme"})

    def test_unknown_object_type_raises_client_error(self):
        sf = MagicMock(spec=[])  # no attributes at all -> AttributeError on getattr
        client = with_fake_sf(make_client(), sf)
        with pytest.raises(SalesforceClientError, match="Unknown Salesforce object type"):
            client.get_record("NotAThing", "001")


class TestRunReport:
    def test_requires_report_id(self):
        client = make_client()
        with pytest.raises(SalesforceClientError, match="requires a report_id"):
            client.run_report("")

    def test_calls_restful_analytics_endpoint(self):
        sf = MagicMock()
        sf.restful.return_value = {"factMap": {}}
        client = with_fake_sf(make_client(), sf)

        result = client.run_report("report-1")

        assert result == {"factMap": {}}
        sf.restful.assert_called_once_with(
            "analytics/reports/report-1", method="POST", json={"reportMetadata": {}},
        )
