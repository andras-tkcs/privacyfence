"""Tests for daemon_main's connector-wiring and config-loading logic.

build_connectors() is the function that turns (settings.yaml, org_config.json,
per-user token files) into the live connector list the IPC server exposes to
the bridge. Its contract, stated in the module docstring, is "graceful:
missing org config or auth -> connector skipped" -- a bug here means a
connector silently vanishes (or, worse, gets wired up without the gating it's
supposed to have). Every *Client class it touches is faked out at the
daemon_main import site so these tests exercise only the wiring, not the
real OAuth/HTTP clients (those are covered separately per-client).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from privacyfence import daemon_main


def fake_client_class(*, result=None, connection_error: Exception | None = None,
                       init_error: Exception | None = None, authorize_error: Exception | None = None):
    """A stand-in for a *Client class. Captures the kwargs it was
    constructed with (on the class, since daemon_main always constructs
    exactly one instance per connector) and controls check_connection()."""

    class _FakeClient:
        captured_kwargs: dict | None = None
        instantiated = False
        authorize_called = False

        def __init__(self, **kwargs):
            type(self).instantiated = True
            type(self).captured_kwargs = kwargs
            if init_error is not None:
                raise init_error

        def authorize_interactive(self):
            type(self).authorize_called = True
            if authorize_error is not None:
                raise authorize_error

        def check_connection(self):
            if connection_error is not None:
                raise connection_error
            return result

    return _FakeClient


@pytest.fixture(autouse=True)
def _no_ambient_telegram(monkeypatch):
    """build_connectors() wires up Telegram independently of any org config --
    only telegram_app_credentials() (baked into the local checkout, or set via
    PRIVACYFENCE_TELEGRAM_API_ID/HASH) and a real credentials/telegram.session
    under PROJECT_ROOT gate it. Without this, tests that don't care about
    Telegram would silently pick up whatever real session a developer has
    authenticated from source with -- default it off here; the Telegram-
    specific tests below override this themselves via their own
    monkeypatch.setattr calls."""
    monkeypatch.setattr(daemon_main, "telegram_app_credentials", lambda: None)


_GOOGLE_CLIENT_ATTRS = ["GmailClient", "DriveClient", "CalendarClient", "ContactsClient", "TasksClient"]


@pytest.fixture(autouse=True)
def _no_ambient_google_clients(monkeypatch):
    """Google-family tests are parametrized to mock only the one *Client class
    under test, leaving the other four as the real classes -- previously safe
    because they'd fail closed on a missing token file. A real, valid token
    for any of them in this checkout's credentials/ (e.g. from `--tasks-oauth`
    or the menu bar) would let that one actually construct and succeed,
    silently changing these tests' results. Default all five to fail closed;
    a test overrides one via its own monkeypatch.setattr, same as above."""
    for attr in _GOOGLE_CLIENT_ATTRS:
        monkeypatch.setattr(daemon_main, attr, fake_client_class(init_error=FileNotFoundError("no token file")))


# ---------------------------------------------------------------------------- #
# _resolve_path / _google_client_config
# ---------------------------------------------------------------------------- #

class TestResolvePath:
    def test_absolute_path_is_returned_unchanged(self):
        assert daemon_main._resolve_path("/etc/hosts") == "/etc/hosts"

    def test_relative_path_is_joined_with_project_root(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "PROJECT_ROOT", "/tmp/pf-root")
        assert daemon_main._resolve_path("credentials/x.json") == "/tmp/pf-root/credentials/x.json"


class TestGoogleClientConfig:
    def test_empty_when_no_google_section(self):
        assert daemon_main._google_client_config({}) == {}

    def test_empty_when_client_id_missing(self):
        org_config = {"google": {"client_secret": "s"}}
        assert daemon_main._google_client_config(org_config) == {}

    def test_empty_when_client_secret_missing(self):
        org_config = {"google": {"client_id": "i"}}
        assert daemon_main._google_client_config(org_config) == {}

    def test_wraps_into_installed_shape_when_both_present(self):
        org_config = {"google": {"client_id": "i", "client_secret": "s", "extra": "x"}}
        assert daemon_main._google_client_config(org_config) == {
            "installed": {"client_id": "i", "client_secret": "s", "extra": "x"}
        }


# ---------------------------------------------------------------------------- #
# load_config / load_org_config
# ---------------------------------------------------------------------------- #

class TestLoadConfig:
    def test_bootstraps_default_when_missing(self, tmp_path):
        config_path = str(tmp_path / "settings.yaml")
        config = daemon_main.load_config(config_path)
        assert os.path.exists(config_path)
        assert isinstance(config, dict)

    def test_loads_existing_file_without_overwriting(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(yaml.dump({"connectors": {"gmail": {"enabled": False}}}))
        config = daemon_main.load_config(str(config_path))
        assert config == {"connectors": {"gmail": {"enabled": False}}}

    def test_raises_value_error_when_not_a_mapping(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text(yaml.dump(["not", "a", "mapping"]))
        with pytest.raises(ValueError, match="did not parse to a mapping"):
            daemon_main.load_config(str(config_path))

    def test_empty_file_yields_empty_dict(self, tmp_path):
        config_path = tmp_path / "settings.yaml"
        config_path.write_text("")
        assert daemon_main.load_config(str(config_path)) == {}


class TestLoadOrgConfig:
    def test_returns_empty_dict_when_no_file_installed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        assert daemon_main.load_org_config() == {}

    def test_returns_parsed_dict_when_valid(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        (tmp_path / "org_config.json").write_text(json.dumps({"slack": {"client_id": "abc"}}))
        assert daemon_main.load_org_config() == {"slack": {"client_id": "abc"}}

    def test_returns_empty_dict_on_malformed_json(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        (tmp_path / "org_config.json").write_text("{not valid json")
        assert daemon_main.load_org_config() == {}

    def test_returns_empty_dict_when_top_level_not_an_object(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_main, "org_dir", lambda: tmp_path)
        (tmp_path / "org_config.json").write_text(json.dumps(["not", "an", "object"]))
        assert daemon_main.load_org_config() == {}


# ---------------------------------------------------------------------------- #
# build_connectors: the Google-backed connectors (gmail, drive, calendar,
# contacts, tasks) all follow the same "needs installed google org config,
# then check_connection()" shape.
# ---------------------------------------------------------------------------- #

GOOGLE_CONNECTORS = [
    pytest.param("gmail", "GmailClient", "GmailClientError", "GmailConnector", id="gmail"),
    pytest.param("drive", "DriveClient", "DriveClientError", "DriveConnector", id="drive"),
    pytest.param("calendar", "CalendarClient", "CalendarClientError", "CalendarConnector", id="calendar"),
    pytest.param("contacts", "ContactsClient", "ContactsClientError", "ContactsConnector", id="contacts"),
    pytest.param("tasks", "TasksClient", "TasksClientError", "TasksConnector", id="tasks"),
]

GOOGLE_ORG_CONFIG = {"google": {"client_id": "id", "client_secret": "secret"}}


class TestBuildConnectorsGoogleFamily:
    @pytest.mark.parametrize("name,client_attr,error_attr,connector_attr", GOOGLE_CONNECTORS)
    def test_built_when_configured_and_reachable(self, monkeypatch, name, client_attr, error_attr, connector_attr):
        fake = fake_client_class(result="user@example.com")
        monkeypatch.setattr(daemon_main, client_attr, fake)

        connectors = daemon_main.build_connectors({}, GOOGLE_ORG_CONFIG)

        assert len(connectors) == 1
        assert connectors[0].name == name
        assert fake.captured_kwargs["client_config"] == {"installed": GOOGLE_ORG_CONFIG["google"]}

    @pytest.mark.parametrize("name,client_attr,error_attr,connector_attr", GOOGLE_CONNECTORS)
    def test_skipped_when_google_org_config_absent(self, monkeypatch, name, client_attr, error_attr, connector_attr):
        fake = fake_client_class(result="user@example.com")
        monkeypatch.setattr(daemon_main, client_attr, fake)

        connectors = daemon_main.build_connectors({}, {})

        assert connectors == []
        assert fake.instantiated is False

    @pytest.mark.parametrize("name,client_attr,error_attr,connector_attr", GOOGLE_CONNECTORS)
    def test_skipped_when_disabled_via_config(self, monkeypatch, name, client_attr, error_attr, connector_attr):
        fake = fake_client_class(result="user@example.com")
        monkeypatch.setattr(daemon_main, client_attr, fake)
        config = {"connectors": {name: {"enabled": False}}}

        connectors = daemon_main.build_connectors(config, GOOGLE_ORG_CONFIG)

        assert connectors == []
        assert fake.instantiated is False

    @pytest.mark.parametrize("name,client_attr,error_attr,connector_attr", GOOGLE_CONNECTORS)
    def test_skipped_when_check_connection_raises(self, monkeypatch, name, client_attr, error_attr, connector_attr):
        error_cls = getattr(daemon_main, error_attr)
        fake = fake_client_class(connection_error=error_cls("token expired"))
        monkeypatch.setattr(daemon_main, client_attr, fake)

        connectors = daemon_main.build_connectors({}, GOOGLE_ORG_CONFIG)

        assert connectors == []

    @pytest.mark.parametrize("name,client_attr,error_attr,connector_attr", GOOGLE_CONNECTORS)
    def test_skipped_when_construction_raises_file_not_found(
        self, monkeypatch, name, client_attr, error_attr, connector_attr
    ):
        fake = fake_client_class(init_error=FileNotFoundError("no token file"))
        monkeypatch.setattr(daemon_main, client_attr, fake)

        connectors = daemon_main.build_connectors({}, GOOGLE_ORG_CONFIG)

        assert connectors == []

    def test_only_this_connector_is_skipped_when_others_succeed(self, monkeypatch):
        # Gmail fails, Drive (also Google-backed) still succeeds independently.
        monkeypatch.setattr(daemon_main, "GmailClient", fake_client_class(
            connection_error=daemon_main.GmailClientError("boom")
        ))
        monkeypatch.setattr(daemon_main, "DriveClient", fake_client_class(result="user@example.com"))
        monkeypatch.setattr(daemon_main, "CalendarClient", fake_client_class(result="user@example.com"))
        monkeypatch.setattr(daemon_main, "ContactsClient", fake_client_class(result="user@example.com"))
        monkeypatch.setattr(daemon_main, "TasksClient", fake_client_class(result="user@example.com"))

        connectors = daemon_main.build_connectors({}, GOOGLE_ORG_CONFIG)

        names = {c.name for c in connectors}
        assert names == {"drive", "calendar", "contacts", "tasks"}


# ---------------------------------------------------------------------------- #
# build_connectors: Slack
# ---------------------------------------------------------------------------- #

class TestBuildConnectorsSlack:
    def _org_config(self):
        return {"slack": {"client_id": "abc"}}

    def test_built_when_configured_and_reachable(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "load_slack_token", lambda path: {"access_token": "xoxp-1", "email": "me@x.com"})
        fake = fake_client_class(result="my-workspace")
        monkeypatch.setattr(daemon_main, "SlackClient", fake)

        connectors = daemon_main.build_connectors({}, self._org_config())

        assert len(connectors) == 1
        assert connectors[0].name == "slack"
        assert connectors[0].my_email == "me@x.com"
        assert fake.captured_kwargs == {"user_token": "xoxp-1"}

    def test_skipped_when_org_config_absent(self, monkeypatch):
        fake = fake_client_class(result="my-workspace")
        monkeypatch.setattr(daemon_main, "SlackClient", fake)

        connectors = daemon_main.build_connectors({}, {})

        assert connectors == []
        assert fake.instantiated is False

    def test_skipped_when_token_missing(self, monkeypatch):
        def raise_missing(path):
            raise daemon_main.SlackClientError("no token")
        monkeypatch.setattr(daemon_main, "load_slack_token", raise_missing)
        fake = fake_client_class(result="my-workspace")
        monkeypatch.setattr(daemon_main, "SlackClient", fake)

        connectors = daemon_main.build_connectors({}, self._org_config())

        assert connectors == []
        assert fake.instantiated is False

    def test_skipped_when_check_connection_raises(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "load_slack_token", lambda path: {"access_token": "xoxp-1"})
        fake = fake_client_class(connection_error=daemon_main.SlackClientError("revoked"))
        monkeypatch.setattr(daemon_main, "SlackClient", fake)

        connectors = daemon_main.build_connectors({}, self._org_config())

        assert connectors == []

    def test_skipped_when_disabled_via_config(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "load_slack_token", lambda path: {"access_token": "xoxp-1"})
        fake = fake_client_class(result="my-workspace")
        monkeypatch.setattr(daemon_main, "SlackClient", fake)

        connectors = daemon_main.build_connectors({"connectors": {"slack": {"enabled": False}}}, self._org_config())

        assert connectors == []
        assert fake.instantiated is False


# ---------------------------------------------------------------------------- #
# build_connectors: Salesforce
# ---------------------------------------------------------------------------- #

class TestBuildConnectorsSalesforce:
    def _org_config(self):
        return {"salesforce": {"consumer_key": "ck", "login_url": "https://login.salesforce.com"}}

    def test_built_when_configured_and_reachable_merges_org_and_token(self, monkeypatch):
        monkeypatch.setattr(
            daemon_main, "load_salesforce_token",
            lambda path: {"access_token": "sf-tok", "instance_url": "https://my.salesforce.com"},
        )
        fake = fake_client_class(result="https://my.salesforce.com")
        monkeypatch.setattr(daemon_main, "SalesforceClient", fake)

        connectors = daemon_main.build_connectors({}, self._org_config())

        assert len(connectors) == 1
        assert connectors[0].name == "salesforce"
        # config= must carry both the org registration and the per-user token.
        assert fake.captured_kwargs["config"] == {
            "consumer_key": "ck",
            "login_url": "https://login.salesforce.com",
            "access_token": "sf-tok",
            "instance_url": "https://my.salesforce.com",
        }

    def test_skipped_when_org_config_absent(self, monkeypatch):
        fake = fake_client_class(result="ok")
        monkeypatch.setattr(daemon_main, "SalesforceClient", fake)

        connectors = daemon_main.build_connectors({}, {})

        assert connectors == []
        assert fake.instantiated is False

    def test_skipped_when_token_missing(self, monkeypatch):
        def raise_missing(path):
            raise daemon_main.SalesforceClientError("no token")
        monkeypatch.setattr(daemon_main, "load_salesforce_token", raise_missing)
        fake = fake_client_class(result="ok")
        monkeypatch.setattr(daemon_main, "SalesforceClient", fake)

        connectors = daemon_main.build_connectors({}, self._org_config())

        assert connectors == []

    def test_skipped_when_check_connection_raises(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "load_salesforce_token", lambda path: {"access_token": "t"})
        fake = fake_client_class(connection_error=daemon_main.SalesforceClientError("expired"))
        monkeypatch.setattr(daemon_main, "SalesforceClient", fake)

        connectors = daemon_main.build_connectors({}, self._org_config())

        assert connectors == []


# ---------------------------------------------------------------------------- #
# build_connectors: Jira / Confluence share one Atlassian OAuth grant
# ---------------------------------------------------------------------------- #

class TestBuildConnectorsAtlassian:
    def _org_config(self):
        return {"atlassian": {"client_id": "ac", "client_secret": "as"}}

    def _patch_token(self, monkeypatch, token=None, error=None):
        def loader(path):
            if error is not None:
                raise error
            return token
        monkeypatch.setattr(daemon_main, "load_atlassian_token", loader)

    def test_both_built_when_configured_and_authenticated(self, monkeypatch):
        self._patch_token(monkeypatch, token={"access_token": "at", "account_email": "me@x.com"})
        jira_fake = fake_client_class(result="jira info")
        confluence_fake = fake_client_class(result="https://x.atlassian.net/wiki")
        monkeypatch.setattr(daemon_main, "JiraClient", jira_fake)
        monkeypatch.setattr(daemon_main, "ConfluenceClient", confluence_fake)

        connectors = daemon_main.build_connectors({}, self._org_config())

        names = {c.name for c in connectors}
        assert names == {"jira", "confluence"}
        for c in connectors:
            assert c.my_email == "me@x.com"

    def test_config_passed_to_clients_merges_org_registration_and_token(self, monkeypatch):
        # Regression coverage for the reauth-on-restart fix: JiraClient/
        # ConfluenceClient need client_id/client_secret (from org config) *and*
        # the per-user access/refresh token merged into one dict so they can
        # refresh an expired token instead of forcing re-authentication.
        self._patch_token(monkeypatch, token={"access_token": "at", "refresh_token": "rt", "account_email": "me@x.com"})
        jira_fake = fake_client_class(result="jira info")
        monkeypatch.setattr(daemon_main, "JiraClient", jira_fake)
        monkeypatch.setattr(daemon_main, "ConfluenceClient", fake_client_class(result="url"))

        daemon_main.build_connectors({}, self._org_config())

        assert jira_fake.captured_kwargs["config"] == {
            "client_id": "ac", "client_secret": "as",
            "access_token": "at", "refresh_token": "rt", "account_email": "me@x.com",
        }

    def test_both_skipped_when_atlassian_org_config_absent(self, monkeypatch):
        jira_fake = fake_client_class(result="ok")
        confluence_fake = fake_client_class(result="ok")
        monkeypatch.setattr(daemon_main, "JiraClient", jira_fake)
        monkeypatch.setattr(daemon_main, "ConfluenceClient", confluence_fake)

        connectors = daemon_main.build_connectors({}, {})

        assert connectors == []
        assert jira_fake.instantiated is False
        assert confluence_fake.instantiated is False

    def test_both_skipped_when_not_authenticated(self, monkeypatch):
        self._patch_token(monkeypatch, error=daemon_main.AtlassianOAuthError("no token file"))
        jira_fake = fake_client_class(result="ok")
        confluence_fake = fake_client_class(result="ok")
        monkeypatch.setattr(daemon_main, "JiraClient", jira_fake)
        monkeypatch.setattr(daemon_main, "ConfluenceClient", confluence_fake)

        connectors = daemon_main.build_connectors({}, self._org_config())

        assert connectors == []
        assert jira_fake.instantiated is False
        assert confluence_fake.instantiated is False

    def test_jira_disabled_does_not_affect_confluence(self, monkeypatch):
        self._patch_token(monkeypatch, token={"access_token": "at", "account_email": "me@x.com"})
        jira_fake = fake_client_class(result="ok")
        confluence_fake = fake_client_class(result="ok")
        monkeypatch.setattr(daemon_main, "JiraClient", jira_fake)
        monkeypatch.setattr(daemon_main, "ConfluenceClient", confluence_fake)
        config = {"connectors": {"jira": {"enabled": False}}}

        connectors = daemon_main.build_connectors(config, self._org_config())

        assert [c.name for c in connectors] == ["confluence"]
        assert jira_fake.instantiated is False

    def test_jira_skipped_when_check_connection_raises_confluence_unaffected(self, monkeypatch):
        self._patch_token(monkeypatch, token={"access_token": "at", "account_email": "me@x.com"})
        jira_fake = fake_client_class(connection_error=daemon_main.JiraClientError("401"))
        confluence_fake = fake_client_class(result="ok")
        monkeypatch.setattr(daemon_main, "JiraClient", jira_fake)
        monkeypatch.setattr(daemon_main, "ConfluenceClient", confluence_fake)

        connectors = daemon_main.build_connectors({}, self._org_config())

        assert [c.name for c in connectors] == ["confluence"]


# ---------------------------------------------------------------------------- #
# build_connectors: Telegram
# ---------------------------------------------------------------------------- #

class TestBuildConnectorsTelegram:
    def _make_session(self, tmp_path, monkeypatch, exists=True):
        monkeypatch.setattr(daemon_main, "PROJECT_ROOT", str(tmp_path))
        os.makedirs(tmp_path / "credentials", exist_ok=True)
        if exists:
            (tmp_path / "credentials" / "telegram.session").write_bytes(b"")

    def test_built_when_creds_and_session_present(self, monkeypatch, tmp_path):
        self._make_session(tmp_path, monkeypatch, exists=True)
        monkeypatch.setattr(daemon_main, "telegram_app_credentials", lambda: (123, "hash"))
        fake = fake_client_class()
        monkeypatch.setattr(daemon_main, "TelegramPrivacyFenceClient", fake)

        connectors = daemon_main.build_connectors({}, {})

        assert len(connectors) == 1
        assert connectors[0].name == "telegram"
        assert fake.captured_kwargs == {
            "api_id": 123, "api_hash": "hash",
            "session_file": str(tmp_path / "credentials" / "telegram.session"),
        }

    def test_skipped_when_no_app_credentials(self, monkeypatch, tmp_path):
        self._make_session(tmp_path, monkeypatch, exists=True)
        monkeypatch.setattr(daemon_main, "telegram_app_credentials", lambda: None)
        fake = fake_client_class()
        monkeypatch.setattr(daemon_main, "TelegramPrivacyFenceClient", fake)

        connectors = daemon_main.build_connectors({}, {})

        assert connectors == []
        assert fake.instantiated is False

    def test_skipped_when_session_file_absent(self, monkeypatch, tmp_path):
        self._make_session(tmp_path, monkeypatch, exists=False)
        monkeypatch.setattr(daemon_main, "telegram_app_credentials", lambda: (123, "hash"))
        fake = fake_client_class()
        monkeypatch.setattr(daemon_main, "TelegramPrivacyFenceClient", fake)

        connectors = daemon_main.build_connectors({}, {})

        assert connectors == []
        assert fake.instantiated is False

    def test_skipped_when_disabled_via_config(self, monkeypatch, tmp_path):
        self._make_session(tmp_path, monkeypatch, exists=True)
        monkeypatch.setattr(daemon_main, "telegram_app_credentials", lambda: (123, "hash"))
        fake = fake_client_class()
        monkeypatch.setattr(daemon_main, "TelegramPrivacyFenceClient", fake)

        connectors = daemon_main.build_connectors({"connectors": {"telegram": {"enabled": False}}}, {})

        assert connectors == []
        assert fake.instantiated is False

    def test_unexpected_construction_error_is_caught_not_fatal(self, monkeypatch, tmp_path):
        # build_connectors deliberately catches bare Exception for Telegram
        # (MTProto client construction can fail in more ways than a typed
        # error) -- a bug here would crash daemon startup entirely.
        self._make_session(tmp_path, monkeypatch, exists=True)
        monkeypatch.setattr(daemon_main, "telegram_app_credentials", lambda: (123, "hash"))
        monkeypatch.setattr(
            daemon_main, "TelegramPrivacyFenceClient",
            fake_client_class(init_error=RuntimeError("unexpected MTProto failure")),
        )

        connectors = daemon_main.build_connectors({}, {})

        assert connectors == []


# ---------------------------------------------------------------------------- #
# build_connectors: cross-cutting
# ---------------------------------------------------------------------------- #

class TestBuildConnectorsCrossCutting:
    def test_no_connectors_configured_returns_empty_list_not_fatal(self):
        assert daemon_main.build_connectors({}, {}) == []

    def test_all_nine_connectors_built_together(self, monkeypatch, tmp_path):
        for attr in ("GmailClient", "DriveClient", "CalendarClient", "ContactsClient", "TasksClient"):
            monkeypatch.setattr(daemon_main, attr, fake_client_class(result="user@example.com"))
        monkeypatch.setattr(daemon_main, "load_slack_token", lambda path: {"access_token": "t"})
        monkeypatch.setattr(daemon_main, "SlackClient", fake_client_class(result="ws"))
        monkeypatch.setattr(daemon_main, "load_salesforce_token", lambda path: {"access_token": "t"})
        monkeypatch.setattr(daemon_main, "SalesforceClient", fake_client_class(result="ok"))
        monkeypatch.setattr(daemon_main, "load_atlassian_token", lambda path: {"access_token": "t"})
        monkeypatch.setattr(daemon_main, "JiraClient", fake_client_class(result="ok"))
        monkeypatch.setattr(daemon_main, "ConfluenceClient", fake_client_class(result="ok"))
        monkeypatch.setattr(daemon_main, "PROJECT_ROOT", str(tmp_path))
        os.makedirs(tmp_path / "credentials", exist_ok=True)
        (tmp_path / "credentials" / "telegram.session").write_bytes(b"")
        monkeypatch.setattr(daemon_main, "telegram_app_credentials", lambda: (1, "h"))
        monkeypatch.setattr(daemon_main, "TelegramPrivacyFenceClient", fake_client_class())

        org_config = {
            **GOOGLE_ORG_CONFIG,
            "slack": {"client_id": "x"},
            "salesforce": {"consumer_key": "x"},
            "atlassian": {"client_id": "x"},
        }
        connectors = daemon_main.build_connectors({}, org_config)

        assert {c.name for c in connectors} == {
            "gmail", "drive", "calendar", "contacts", "tasks",
            "slack", "salesforce", "jira", "confluence", "telegram",
        }


# ---------------------------------------------------------------------------- #
# setup_logging
# ---------------------------------------------------------------------------- #

class TestSetupLogging:
    @pytest.fixture(autouse=True)
    def _restore_root_logger(self):
        # setup_logging() clears and replaces the *real* root logger's
        # handlers/level as a side effect -- restore it so this doesn't leak
        # into other tests' log capture or leave a FileHandler pointing at a
        # deleted tmp_path.
        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level
        yield
        for h in root.handlers:
            h.close()
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)

    def test_creates_log_file_at_configured_path(self, tmp_path):
        log_file = tmp_path / "sub" / "privacyfence.log"
        daemon_main.setup_logging({"logging": {"file": str(log_file)}})
        assert log_file.exists()

    def test_defaults_to_info_level(self, tmp_path):
        log_file = tmp_path / "privacyfence.log"
        daemon_main.setup_logging({"logging": {"file": str(log_file)}})
        assert logging.getLogger().level == logging.INFO

    def test_honors_configured_level(self, tmp_path):
        log_file = tmp_path / "privacyfence.log"
        daemon_main.setup_logging({"logging": {"level": "DEBUG", "file": str(log_file)}})
        assert logging.getLogger().level == logging.DEBUG

    def test_invalid_level_name_falls_back_to_info(self, tmp_path):
        log_file = tmp_path / "privacyfence.log"
        daemon_main.setup_logging({"logging": {"level": "NOT_A_REAL_LEVEL", "file": str(log_file)}})
        assert logging.getLogger().level == logging.INFO

    def test_missing_logging_section_uses_defaults(self, monkeypatch, tmp_path):
        monkeypatch.setattr(daemon_main, "PROJECT_ROOT", str(tmp_path))
        daemon_main.setup_logging({})
        assert (tmp_path / "logs" / "privacyfence.log").exists()


# ---------------------------------------------------------------------------- #
# parse_args
# ---------------------------------------------------------------------------- #

class TestParseArgs:
    def test_defaults_have_no_oauth_flags_set(self):
        args = daemon_main.parse_args([])
        assert not any([
            args.gmail_oauth, args.drive_oauth, args.contacts_oauth, args.calendar_oauth,
            args.tasks_oauth, args.slack_oauth, args.salesforce_oauth, args.atlassian_oauth,
            args.telegram_setup,
        ])

    def test_config_flag_overrides_default(self):
        args = daemon_main.parse_args(["--config", "/tmp/custom.yaml"])
        assert args.config == "/tmp/custom.yaml"

    @pytest.mark.parametrize("flag,attr", [
        ("--gmail-oauth", "gmail_oauth"),
        ("--drive-oauth", "drive_oauth"),
        ("--contacts-oauth", "contacts_oauth"),
        ("--calendar-oauth", "calendar_oauth"),
        ("--tasks-oauth", "tasks_oauth"),
        ("--slack-oauth", "slack_oauth"),
        ("--salesforce-oauth", "salesforce_oauth"),
        ("--atlassian-oauth", "atlassian_oauth"),
        ("--telegram-setup", "telegram_setup"),
    ])
    def test_each_oauth_flag_sets_only_its_own_attribute(self, flag, attr):
        args = daemon_main.parse_args([flag])
        assert getattr(args, attr) is True
        other_attrs = {
            "gmail_oauth", "drive_oauth", "contacts_oauth", "calendar_oauth", "tasks_oauth",
            "slack_oauth", "salesforce_oauth", "atlassian_oauth", "telegram_setup",
        } - {attr}
        assert not any(getattr(args, other) for other in other_attrs)


# ---------------------------------------------------------------------------- #
# Instance lock
# ---------------------------------------------------------------------------- #

class TestInstanceLock:
    @pytest.fixture(autouse=True)
    def _reset_lock_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(daemon_main, "LOCK_FILE", str(tmp_path / "privacyfence.lock"))
        daemon_main._lock_fd = None
        yield
        daemon_main._release_instance_lock()

    def test_first_acquire_succeeds(self):
        assert daemon_main._acquire_instance_lock() is True

    def test_second_acquire_fails_while_first_is_held(self):
        assert daemon_main._acquire_instance_lock() is True
        assert daemon_main._acquire_instance_lock() is False

    def test_acquire_succeeds_again_after_release(self):
        assert daemon_main._acquire_instance_lock() is True
        daemon_main._release_instance_lock()
        assert daemon_main._acquire_instance_lock() is True

    def test_release_without_acquire_is_a_no_op(self):
        daemon_main._release_instance_lock()  # must not raise


# ---------------------------------------------------------------------------- #
# run_*_oauth: headless/dev CLI setup commands
# ---------------------------------------------------------------------------- #

GOOGLE_OAUTH_RUNNERS = [
    pytest.param("run_gmail_oauth", "GmailClient", "GmailClientError", id="gmail"),
    pytest.param("run_drive_oauth", "DriveClient", "DriveClientError", id="drive"),
    pytest.param("run_contacts_oauth", "ContactsClient", "ContactsClientError", id="contacts"),
    pytest.param("run_calendar_oauth", "CalendarClient", "CalendarClientError", id="calendar"),
    pytest.param("run_tasks_oauth", "TasksClient", "TasksClientError", id="tasks"),
]


class TestGoogleOauthRunners:
    @pytest.mark.parametrize("runner_name,client_attr,error_attr", GOOGLE_OAUTH_RUNNERS)
    def test_success_authorizes_and_prints_email(self, monkeypatch, capsys, runner_name, client_attr, error_attr):
        fake = fake_client_class(result="me@example.com")
        monkeypatch.setattr(daemon_main, client_attr, fake)
        runner = getattr(daemon_main, runner_name)

        code = runner({"google": {"client_id": "id", "client_secret": "secret"}})

        assert code == 0
        assert fake.authorize_called is True
        assert "me@example.com" in capsys.readouterr().out

    @pytest.mark.parametrize("runner_name,client_attr,error_attr", GOOGLE_OAUTH_RUNNERS)
    def test_client_error_prints_to_stderr_and_returns_1(self, monkeypatch, capsys, runner_name, client_attr, error_attr):
        error_cls = getattr(daemon_main, error_attr)
        fake = fake_client_class(authorize_error=error_cls("no browser available"))
        monkeypatch.setattr(daemon_main, client_attr, fake)
        runner = getattr(daemon_main, runner_name)

        code = runner({})

        assert code == 1
        assert "no browser available" in capsys.readouterr().err


class TestSlackOauthRunner:
    def test_missing_org_config_prints_error_and_returns_1(self, capsys):
        assert daemon_main.run_slack_oauth({}) == 1
        assert "No Slack organization config" in capsys.readouterr().err

    def test_success_prints_team_name_and_returns_0(self, monkeypatch, capsys):
        monkeypatch.setattr(
            daemon_main, "slack_authorize_interactive",
            lambda **kw: {"team_name": "Acme"},
        )
        code = daemon_main.run_slack_oauth({"slack": {"client_id": "id", "client_secret": "s"}})
        assert code == 0
        assert "Acme" in capsys.readouterr().out

    def test_client_error_prints_to_stderr_and_returns_1(self, monkeypatch, capsys):
        def raiser(**kw):
            raise daemon_main.SlackClientError("invalid redirect")
        monkeypatch.setattr(daemon_main, "slack_authorize_interactive", raiser)
        code = daemon_main.run_slack_oauth({"slack": {"client_id": "id", "client_secret": "s"}})
        assert code == 1
        assert "invalid redirect" in capsys.readouterr().err


class TestSalesforceOauthRunner:
    def test_missing_org_config_prints_error_and_returns_1(self, capsys):
        assert daemon_main.run_salesforce_oauth({}) == 1
        assert "No Salesforce organization config" in capsys.readouterr().err

    def test_success_prints_instance_url_and_returns_0(self, monkeypatch, capsys):
        monkeypatch.setattr(
            daemon_main, "salesforce_authorize_interactive",
            lambda **kw: {"instance_url": "https://x.salesforce.com"},
        )
        code = daemon_main.run_salesforce_oauth({"salesforce": {"consumer_key": "ck", "consumer_secret": "cs"}})
        assert code == 0
        assert "x.salesforce.com" in capsys.readouterr().out

    def test_client_error_prints_to_stderr_and_returns_1(self, monkeypatch, capsys):
        def raiser(**kw):
            raise daemon_main.SalesforceClientError("bad login url")
        monkeypatch.setattr(daemon_main, "salesforce_authorize_interactive", raiser)
        code = daemon_main.run_salesforce_oauth({"salesforce": {"consumer_key": "ck", "consumer_secret": "cs"}})
        assert code == 1
        assert "bad login url" in capsys.readouterr().err


class TestAtlassianOauthRunner:
    def test_missing_org_config_prints_error_and_returns_1(self, capsys):
        assert daemon_main.run_atlassian_oauth({}) == 1
        assert "No Atlassian organization config" in capsys.readouterr().err

    def test_success_prints_site_url_and_returns_0(self, monkeypatch, capsys):
        monkeypatch.setattr(
            daemon_main, "atlassian_authorize_interactive",
            lambda **kw: {"site_url": "https://acme.atlassian.net"},
        )
        code = daemon_main.run_atlassian_oauth({"atlassian": {"client_id": "ci", "client_secret": "cs"}})
        assert code == 0
        assert "acme.atlassian.net" in capsys.readouterr().out

    def test_client_error_prints_to_stderr_and_returns_1(self, monkeypatch, capsys):
        def raiser(**kw):
            raise daemon_main.AtlassianOAuthError("consent denied")
        monkeypatch.setattr(daemon_main, "atlassian_authorize_interactive", raiser)
        code = daemon_main.run_atlassian_oauth({"atlassian": {"client_id": "ci", "client_secret": "cs"}})
        assert code == 1
        assert "consent denied" in capsys.readouterr().err


class TestTelegramSetupRunner:
    def test_missing_app_credentials_prints_error_and_returns_1(self, monkeypatch, capsys):
        monkeypatch.setattr(daemon_main, "telegram_app_credentials", lambda: None)
        code = daemon_main.run_telegram_setup()
        assert code == 1
        assert "No Telegram app credentials" in capsys.readouterr().err

    def test_success_authorizes_and_prints_session_path(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setattr(daemon_main, "telegram_app_credentials", lambda: (123, "hash"))
        monkeypatch.setattr(daemon_main, "PROJECT_ROOT", str(tmp_path))

        captured = {}
        class FakeTelegramClient:
            def __init__(self, api_id, api_hash, session_file):
                captured["api_id"] = api_id
                captured["session_file"] = session_file
            async def authorize_interactive(self):
                captured["authorized"] = True
        monkeypatch.setattr(daemon_main, "TelegramPrivacyFenceClient", FakeTelegramClient)

        code = daemon_main.run_telegram_setup()

        assert code == 0
        assert captured["authorized"] is True
        assert captured["session_file"] in capsys.readouterr().out


# ---------------------------------------------------------------------------- #
# IPCServerThread
# ---------------------------------------------------------------------------- #

@pytest.fixture
def short_socket_path():
    """AF_UNIX's sun_path is too short (~104 bytes) for pytest's nested
    tmp_path dirs -- use /tmp directly with a short unique name."""
    directory = f"/tmp/pf-{uuid.uuid4().hex[:8]}"
    os.makedirs(directory, exist_ok=True)
    path = f"{directory}/s.sock"
    yield path
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    try:
        os.rmdir(directory)
    except OSError:
        pass


class TestIPCServerThread:
    def test_starts_a_fresh_event_loop_and_becomes_ready(self, monkeypatch, short_socket_path):
        from privacyfence import ipc_server as ipc_server_module
        from privacyfence.ipc_server import IPCServer

        monkeypatch.setattr(ipc_server_module, "SOCKET_PATH", short_socket_path)
        server = IPCServer([])
        thread = daemon_main.IPCServerThread(server)

        thread.start()
        try:
            assert thread._ready.wait(timeout=5)
            assert thread._loop is not None
            assert thread.is_alive()
        finally:
            thread._loop.call_soon_threadsafe(thread._loop.stop)
            thread.join(timeout=5)

    def test_crash_during_startup_is_logged_not_raised(self, caplog):
        class FailingServer:
            async def start(self):
                raise RuntimeError("bind failed")

        thread = daemon_main.IPCServerThread(FailingServer())
        with caplog.at_level(logging.ERROR):
            thread.start()
            thread.join(timeout=5)

        assert not thread.is_alive()
        assert "IPC server thread crashed" in caplog.text


# ---------------------------------------------------------------------------- #
# run_app
# ---------------------------------------------------------------------------- #

class _FakeIPCServerThread:
    instances: list["_FakeIPCServerThread"] = []

    def __init__(self, server):
        self.server = server
        self._ready = threading.Event()
        self._ready.set()
        self.started = False
        type(self).instances.append(self)

    def start(self):
        self.started = True


class TestRunApp:
    def _patch_common(self, monkeypatch, connectors=None):
        connectors = [] if connectors is None else connectors
        monkeypatch.setattr(daemon_main, "init_config_path", lambda path: None)
        monkeypatch.setattr(daemon_main, "reload_rules", lambda rules: None)
        fake_audit_logger = MagicMock()
        monkeypatch.setattr(daemon_main, "init_audit_logger", lambda path: fake_audit_logger)
        monkeypatch.setattr(daemon_main, "load_org_config", lambda: {})
        monkeypatch.setattr(daemon_main, "build_connectors", lambda cfg, org: connectors)
        monkeypatch.setattr(
            daemon_main, "IPCServer",
            lambda conns, **kw: SimpleNamespace(connectors=conns, unattended_sessions_enabled=kw.get("unattended_sessions_enabled")),
        )
        _FakeIPCServerThread.instances = []
        monkeypatch.setattr(daemon_main, "IPCServerThread", _FakeIPCServerThread)
        return fake_audit_logger

    def test_lock_already_held_returns_1_without_building_connectors(self, monkeypatch, capsys):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: False)
        build_calls = []
        monkeypatch.setattr(daemon_main, "build_connectors", lambda cfg, org: build_calls.append(1))

        result = daemon_main.run_app({}, "config.yaml")

        assert result == 1
        assert build_calls == []
        assert "already running" in capsys.readouterr().err

    def test_successful_startup_runs_menu_bar_and_releases_lock(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        release_calls = []
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: release_calls.append(1))
        connector = SimpleNamespace(name="gmail")
        self._patch_common(monkeypatch, connectors=[connector])

        menu_bar_calls = []
        monkeypatch.setattr("privacyfence.menu_bar.run_menu_bar", lambda **kw: menu_bar_calls.append(kw))

        result = daemon_main.run_app({}, "config.yaml")

        assert result == 0
        assert len(menu_bar_calls) == 1
        assert menu_bar_calls[0]["config_path"] == "config.yaml"
        assert menu_bar_calls[0]["connectors"] == ["gmail"]
        assert menu_bar_calls[0]["ipc_server"] is _FakeIPCServerThread.instances[0].server
        assert _FakeIPCServerThread.instances[0].started is True
        assert release_calls == [1]

    def test_no_connectors_built_still_starts_ipc_and_menu_bar(self, monkeypatch, caplog):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch, connectors=[])
        menu_bar_calls = []
        monkeypatch.setattr("privacyfence.menu_bar.run_menu_bar", lambda **kw: menu_bar_calls.append(kw))

        with caplog.at_level(logging.WARNING):
            result = daemon_main.run_app({}, "config.yaml")

        assert result == 0
        assert menu_bar_calls[0]["connectors"] == []
        assert "No connectors could be initialized" in caplog.text

    def test_keyboard_interrupt_is_caught_lock_released_returns_0(self, monkeypatch, caplog):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        release_calls = []
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: release_calls.append(1))
        self._patch_common(monkeypatch)

        def raise_interrupt(**kw):
            raise KeyboardInterrupt()
        monkeypatch.setattr("privacyfence.menu_bar.run_menu_bar", raise_interrupt)

        with caplog.at_level(logging.INFO):
            result = daemon_main.run_app({}, "config.yaml")

        assert result == 0
        assert release_calls == [1]
        assert "Interrupted; shutting down" in caplog.text

    def test_unexpected_exception_still_releases_lock_then_propagates(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        release_calls = []
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: release_calls.append(1))
        self._patch_common(monkeypatch)

        def raise_other(**kw):
            raise RuntimeError("menu bar crashed")
        monkeypatch.setattr("privacyfence.menu_bar.run_menu_bar", raise_other)

        with pytest.raises(RuntimeError, match="menu bar crashed"):
            daemon_main.run_app({}, "config.yaml")

        assert release_calls == [1]

    def test_migrations_run_persist_and_log_then_reload_sees_new_keys(self, monkeypatch, tmp_path, caplog):
        # Real migrate_rules_to_grants/migrate_telegram_search_operation_key
        # (not mocked, unlike _patch_common's other collaborators) so this
        # covers the actual persist-to-disk branch: a grant-eligible
        # auto_accept_rules block (full match across drive.folders' one
        # target) plus a legacy telegram.search_messages entry, both of
        # which should be migrated and written back to config_path.
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch)
        monkeypatch.setattr("privacyfence.menu_bar.run_menu_bar", lambda **kw: None)
        reloaded = []
        monkeypatch.setattr(daemon_main, "reload_rules", lambda rules: reloaded.append(rules))

        config_path = str(tmp_path / "settings.yaml")
        config = {
            "auto_accept_rules": {
                "drive.read_file_contents": [{"rule": "approved_folder", "value": ["F1"]}],
                "drive.download_file": [{"rule": "approved_folder", "value": ["F1"]}],
                "sheets.read_values": [{"rule": "approved_folder", "value": ["F1"]}],
                "telegram.search_messages": [{"rule": "no_media_attachments"}],
            }
        }

        with caplog.at_level(logging.INFO):
            result = daemon_main.run_app(config, config_path)

        assert result == 0
        on_disk = yaml.safe_load(open(config_path, encoding="utf-8"))
        assert on_disk["auto_accept_grants"]["drive"]["folders"] == [{"id": "F1", "read": True}]
        assert "telegram.search_messages" not in on_disk.get("auto_accept_rules", {})
        assert on_disk["auto_accept_rules"]["telegram.read_chat_messages"] == [
            {"rule": "no_media_attachments"}
        ]
        assert "migrated to connector-scoped grants" in caplog.text
        assert "telegram.search_messages rules" in caplog.text
        # reload_rules() ran against the post-migration config, not the
        # pre-migration one Claude/the caller originally passed in.
        assert len(reloaded) == 1

    def test_unattended_sessions_disabled_by_default(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch)
        monkeypatch.setattr("privacyfence.menu_bar.run_menu_bar", lambda **kw: None)

        daemon_main.run_app({}, "config.yaml")

        assert _FakeIPCServerThread.instances[0].server.unattended_sessions_enabled is False

    def test_unattended_sessions_enabled_flag_passed_through_from_org_config(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch)
        monkeypatch.setattr(daemon_main, "load_org_config", lambda: {"unattended_sessions": {"enabled": True}})
        monkeypatch.setattr("privacyfence.menu_bar.run_menu_bar", lambda **kw: None)

        daemon_main.run_app({}, "config.yaml")

        assert _FakeIPCServerThread.instances[0].server.unattended_sessions_enabled is True

    def test_unattended_sessions_enabled_in_settings_yaml_is_ignored(self, monkeypatch):
        """unattended_sessions.enabled lives in org_config.json, not settings.yaml -- a
        stray copy in settings.yaml (e.g. left over pre-migration) must not enable it."""
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch)
        monkeypatch.setattr("privacyfence.menu_bar.run_menu_bar", lambda **kw: None)

        daemon_main.run_app({"unattended_sessions": {"enabled": True}}, "config.yaml")

        assert _FakeIPCServerThread.instances[0].server.unattended_sessions_enabled is False

    def test_exports_pending_audit_entries_on_startup(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        fake_audit_logger = self._patch_common(monkeypatch)
        monkeypatch.setattr("privacyfence.menu_bar.run_menu_bar", lambda **kw: None)

        daemon_main.run_app({}, "config.yaml")

        fake_audit_logger.export_all_pending.assert_called_once()


# ---------------------------------------------------------------------------- #
# main(): CLI dispatch
# ---------------------------------------------------------------------------- #

class TestMain:
    def _patch_config(self, monkeypatch, config=None):
        monkeypatch.setattr(daemon_main, "load_config", lambda path: config or {})
        monkeypatch.setattr(daemon_main, "setup_logging", lambda cfg: None)
        monkeypatch.setattr(daemon_main, "load_org_config", lambda: {})

    def test_config_load_failure_prints_error_and_returns_1(self, monkeypatch, capsys):
        def raiser(path):
            raise ValueError("bad yaml")
        monkeypatch.setattr(daemon_main, "load_config", raiser)

        result = daemon_main.main([])

        assert result == 1
        assert "Configuration error" in capsys.readouterr().err

    @pytest.mark.parametrize("flag,runner_name", [
        ("--gmail-oauth", "run_gmail_oauth"),
        ("--drive-oauth", "run_drive_oauth"),
        ("--contacts-oauth", "run_contacts_oauth"),
        ("--calendar-oauth", "run_calendar_oauth"),
        ("--tasks-oauth", "run_tasks_oauth"),
        ("--slack-oauth", "run_slack_oauth"),
        ("--salesforce-oauth", "run_salesforce_oauth"),
        ("--atlassian-oauth", "run_atlassian_oauth"),
    ])
    def test_oauth_flag_dispatches_to_the_right_runner(self, monkeypatch, flag, runner_name):
        self._patch_config(monkeypatch)
        calls = []
        monkeypatch.setattr(daemon_main, runner_name, lambda org_config: calls.append(1) or 0)

        result = daemon_main.main([flag])

        assert result == 0
        assert calls == [1]

    def test_telegram_setup_flag_dispatches_with_no_org_config_arg(self, monkeypatch):
        self._patch_config(monkeypatch)
        calls = []
        monkeypatch.setattr(daemon_main, "run_telegram_setup", lambda: calls.append(1) or 0)

        result = daemon_main.main(["--telegram-setup"])

        assert result == 0
        assert calls == [1]

    def test_no_oauth_flag_calls_run_app(self, monkeypatch):
        self._patch_config(monkeypatch)
        calls = []
        monkeypatch.setattr(daemon_main, "run_app", lambda config, path: calls.append((config, path)) or 0)

        result = daemon_main.main([])

        assert result == 0
        assert len(calls) == 1

    def test_fatal_exception_is_caught_prints_error_and_returns_1(self, monkeypatch, capsys):
        self._patch_config(monkeypatch)
        def raiser(config, path):
            raise RuntimeError("unexpected crash")
        monkeypatch.setattr(daemon_main, "run_app", raiser)

        result = daemon_main.main([])

        assert result == 1
        assert "Fatal error" in capsys.readouterr().err
