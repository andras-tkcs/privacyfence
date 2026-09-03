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

import asyncio
import json
import logging
import os
import sys
import threading
import time
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from privacyfence import daemon_main
from privacyfence.connectors.slack import SlackConnector
from privacyfence.connectors.telegram import TelegramConnector
from privacyfence.paths import data_dir


def fake_client_class(*, result=None, connection_error: Exception | None = None,
                       init_error: Exception | None = None, authorize_error: Exception | None = None):
    """A stand-in for a *Client class. Captures the kwargs it was
    constructed with (on the class, since daemon_main always constructs
    exactly one instance per connector) and controls check_connection()."""

    class _FakeClient:
        captured_kwargs: dict | None = None
        instantiated = False
        authorize_called = False
        directories_refreshed = False

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

        def ensure_directories_fresh(self):
            # Only SlackClient has this method for real -- harmless no-op
            # for every other *Client fake built from this same factory.
            type(self).directories_refreshed = True

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


_GOOGLE_CLIENT_ATTRS = [
    "GmailClient", "DriveClient", "CalendarClient", "ContactsClient", "TasksClient",
    "AppsScriptClient",
]


@pytest.fixture(autouse=True)
def _no_ambient_google_clients(monkeypatch):
    """Google-family tests are parametrized to mock only the one *Client class
    under test, leaving the others as the real classes -- previously safe
    because they'd fail closed on a missing token file. A real, valid token
    for any of them in this checkout's credentials/ (e.g. from `--tasks-oauth`
    or the menu bar) would let that one actually construct and succeed,
    silently changing these tests' results. Default all six to fail closed;
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
# contacts, tasks, apps_script) all follow the same "needs installed google
# org config, then check_connection()" shape.
# ---------------------------------------------------------------------------- #

GOOGLE_CONNECTORS = [
    pytest.param("gmail", "GmailClient", "GmailClientError", "GmailConnector", id="gmail"),
    pytest.param("drive", "DriveClient", "DriveClientError", "DriveConnector", id="drive"),
    pytest.param("calendar", "CalendarClient", "CalendarClientError", "CalendarConnector", id="calendar"),
    pytest.param("contacts", "ContactsClient", "ContactsClientError", "ContactsConnector", id="contacts"),
    pytest.param("tasks", "TasksClient", "TasksClientError", "TasksConnector", id="tasks"),
    pytest.param("apps_script", "AppsScriptClient", "AppsScriptClientError", "AppsScriptConnector", id="apps_script"),
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
        monkeypatch.setattr(daemon_main, "AppsScriptClient", fake_client_class(result="user@example.com"))

        connectors = daemon_main.build_connectors({}, GOOGLE_ORG_CONFIG)

        names = {c.name for c in connectors}
        assert names == {"drive", "calendar", "contacts", "tasks", "apps_script"}


class TestBuildConnectorsCalendarFreeBusySetting:
    """settings.yaml's calendar.free_busy_full_event_details is plumbed onto
    the built CalendarConnector -- see calendar.py's _get_free_busy /
    _downgrade_to_busy_only."""

    def test_defaults_to_true_when_unconfigured(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "CalendarClient", fake_client_class(result="user@example.com"))

        connectors = daemon_main.build_connectors({}, GOOGLE_ORG_CONFIG)

        assert connectors[0].free_busy_full_details is True

    def test_reads_explicit_false_from_config(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "CalendarClient", fake_client_class(result="user@example.com"))
        config = {"calendar": {"free_busy_full_event_details": False}}

        connectors = daemon_main.build_connectors(config, GOOGLE_ORG_CONFIG)

        assert connectors[0].free_busy_full_details is False

    def test_reads_explicit_true_from_config(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "CalendarClient", fake_client_class(result="user@example.com"))
        config = {"calendar": {"free_busy_full_event_details": True}}

        connectors = daemon_main.build_connectors(config, GOOGLE_ORG_CONFIG)

        assert connectors[0].free_busy_full_details is True


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
        assert fake.captured_kwargs == {
            "user_token": "xoxp-1",
            "user_cache_file": str(data_dir() / "slack_user_cache.json"),
            "channel_cache_file": str(data_dir() / "slack_channel_cache.json"),
        }
        # Directory-cache warming no longer happens inline in
        # build_connectors() -- it's kicked off separately, in the
        # background, by _warm_connector_caches() (see run_app()), so a
        # large workspace's re-sync can't delay the menu bar icon.
        assert fake.directories_refreshed is False

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
            "chat_cache_file": str(data_dir() / "telegram_chat_cache.json"),
        }
        # Same as Slack (see TestBuildConnectorsSlack): directory-cache
        # warming is no longer inline in build_connectors() for either
        # connector -- it's kicked off separately, in the background, by
        # _warm_connector_caches() (see run_app()).
        assert fake.directories_refreshed is False

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

    def test_all_ten_connectors_built_together(self, monkeypatch, tmp_path):
        for attr in (
            "GmailClient", "DriveClient", "CalendarClient", "ContactsClient", "TasksClient",
            "AppsScriptClient",
        ):
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
            "gmail", "drive", "calendar", "contacts", "tasks", "apps_script",
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
# _maybe_start_web_server -- the P1/P2 rollback levers (see
# docs/https-connector-refactor-plan.md §12): config/settings.yaml's
# web.approval_ui selects native (default, untouched) or web; web.mcp.enabled
# independently turns the /mcp endpoint on. Either one alone starts the one
# embedded server.
# ---------------------------------------------------------------------------- #

class TestMaybeStartWebServer:
    def _no_bind(self, monkeypatch, tmp_path):
        # Never actually binds a real socket -- this suite proves the
        # wiring (which ApprovalUI gets installed, whether a server object
        # comes back, whether /mcp is mounted), not uvicorn's own serve
        # loop. web_token/mcp_token also have to land under an isolated
        # tmp_path, not paths.data_dir()'s real value (the repo root itself
        # in dev mode) -- see web/server.py's load_or_create_token().
        from privacyfence import paths
        from privacyfence.web.server import WebServer
        monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
        started = {}
        monkeypatch.setattr(WebServer, "start", lambda self: started.update(called=True))
        return started

    @staticmethod
    def _ipc_server():
        from privacyfence.ipc_server import IPCServer
        return IPCServer([])

    def test_no_web_section_stays_native_and_starts_nothing(self, monkeypatch, tmp_path):
        from privacyfence.approval_ui import NativeApprovalUI, get_approval_ui
        self._no_bind(monkeypatch, tmp_path)
        result = daemon_main._maybe_start_web_server({}, self._ipc_server(), unattended_sessions_enabled=False)
        assert result is None
        assert isinstance(get_approval_ui(), NativeApprovalUI)

    def test_explicit_native_with_mcp_disabled_starts_nothing(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)
        result = daemon_main._maybe_start_web_server(
            {"web": {"approval_ui": "native"}}, self._ipc_server(), unattended_sessions_enabled=False,
        )
        assert result is None

    def test_web_mode_installs_the_web_approval_ui_and_starts_a_server(self, monkeypatch, tmp_path):
        from privacyfence.web_approval_ui import WebApprovalUI, get_web_approval_ui
        from privacyfence.approval_ui import get_approval_ui
        started = self._no_bind(monkeypatch, tmp_path)

        result = daemon_main._maybe_start_web_server(
            {"web": {"approval_ui": "web", "port": 18765}}, self._ipc_server(),
            unattended_sessions_enabled=False,
        )

        assert result is not None
        assert result.port == 18765
        assert started.get("called") is True
        assert get_approval_ui() is get_web_approval_ui()
        assert isinstance(get_approval_ui(), WebApprovalUI)
        assert result.mcp_url is None  # web.mcp.enabled wasn't set

    def test_web_mode_defaults_to_the_standard_port(self, monkeypatch, tmp_path):
        from privacyfence.web.server import DEFAULT_PORT
        self._no_bind(monkeypatch, tmp_path)
        result = daemon_main._maybe_start_web_server(
            {"web": {"approval_ui": "web"}}, self._ipc_server(), unattended_sessions_enabled=False,
        )
        assert result.port == DEFAULT_PORT

    def test_mcp_enabled_alone_starts_a_server_without_touching_the_approval_ui(self, monkeypatch, tmp_path):
        from privacyfence.approval_ui import NativeApprovalUI, get_approval_ui
        started = self._no_bind(monkeypatch, tmp_path)

        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, self._ipc_server(), unattended_sessions_enabled=False,
        )

        assert result is not None
        assert started.get("called") is True
        assert result.mcp_url == f"{result.base_url}/mcp"
        # web.approval_ui wasn't "web" -- the popup stays native even though
        # the embedded server is now running for /mcp's sake.
        assert isinstance(get_approval_ui(), NativeApprovalUI)

    def test_web_mode_registry_gets_the_real_base_url_once_started(self, monkeypatch, tmp_path):
        # P3: gate.py's pending-result URL (docs/https-connector-refactor-
        # plan.md §5.2 point 4) needs the registry to know the server's real
        # base_url, not just exist -- set once the server actually starts,
        # not at construction time.
        from privacyfence.web_approval_ui import get_web_approval_ui
        self._no_bind(monkeypatch, tmp_path)

        result = daemon_main._maybe_start_web_server(
            {"web": {"approval_ui": "web", "port": 18765}}, self._ipc_server(),
            unattended_sessions_enabled=False,
        )

        registry = get_web_approval_ui().deferred_registry
        assert registry.approval_url("abc") == f"{result.base_url}/approvals/abc"

    def test_mcp_dispatcher_shares_the_same_registry_as_the_approval_ui(self, monkeypatch, tmp_path):
        from privacyfence.web_approval_ui import get_web_approval_ui
        self._no_bind(monkeypatch, tmp_path)

        result = daemon_main._maybe_start_web_server(
            {"web": {"approval_ui": "web", "mcp": {"enabled": True}}}, self._ipc_server(),
            unattended_sessions_enabled=False,
        )

        assert result.mcp_dispatcher._registry is get_web_approval_ui().deferred_registry

    def test_approvals_config_overrides_the_registrys_defaults(self, monkeypatch, tmp_path):
        from privacyfence.web_approval_ui import get_web_approval_ui
        self._no_bind(monkeypatch, tmp_path)

        daemon_main._maybe_start_web_server(
            {
                "web": {
                    "approval_ui": "web",
                    "approvals": {
                        "hold_window_seconds": 5, "pending_ttl_seconds": 60,
                        "ledger_ttl_seconds": 30, "max_pending": 3,
                    },
                },
            },
            self._ipc_server(), unattended_sessions_enabled=False,
        )

        registry = get_web_approval_ui().deferred_registry
        assert registry.hold_window == 5
        assert registry.pending_ttl == 60
        assert registry.ledger_ttl == 30
        assert registry.max_pending == 3

    def test_mcp_dispatcher_sees_the_ipc_servers_live_connector_set(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)
        ipc_server = self._ipc_server()

        result = daemon_main._maybe_start_web_server(
            {"web": {"mcp": {"enabled": True}}}, ipc_server, unattended_sessions_enabled=False,
        )

        assert result.mcp_dispatcher.connectors == {}
        from privacyfence.connectors.gmail import GmailConnector  # any real Connector subclass
        fake_connector = object.__new__(GmailConnector)
        ipc_server.set_connectors([fake_connector])
        # No second push into the dispatcher -- it polls ipc_server.connectors.
        assert list(result.mcp_dispatcher.connectors) == [fake_connector.name]

    # ------------------------------------------------------------------ #
    # web.settings.enabled -- P4's own rollback lever (§16.6), independent
    # of web.approval_ui: a deployment can run the web settings page with
    # the native approval dialog, or the reverse.
    # ------------------------------------------------------------------ #

    def _controller(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        from privacyfence import resource_names, settings_controller as sc, update_checker

        monkeypatch.setattr(resource_names, "_cache_file", lambda: tmp_path / "rn.json")
        monkeypatch.setattr(update_checker, "_cache_file", lambda: tmp_path / "uc.json")
        monkeypatch.setattr(sc, "check_for_update", lambda **kw: None)
        org_dir_path = tmp_path / "org"
        org_dir_path.mkdir()
        monkeypatch.setattr(sc, "org_dir", lambda: org_dir_path)
        settings_dir = tmp_path / "settings_data"
        settings_dir.mkdir()
        monkeypatch.setattr(sc, "data_dir", lambda: settings_dir)
        config_path = tmp_path / "settings.yaml"
        config_path.write_text("auto_accept_rules: {}\nconnectors: {}\n", encoding="utf-8")
        ipc_server = self._ipc_server()
        return sc.SettingsController(str(config_path), connectors=[], ipc_server=ipc_server)

    def test_settings_not_enabled_leaves_the_server_unbuilt_with_a_controller(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)
        controller = self._controller(tmp_path, monkeypatch)

        result = daemon_main._maybe_start_web_server(
            {}, self._ipc_server(), unattended_sessions_enabled=False, controller=controller,
        )

        assert result is None

    def test_settings_enabled_without_a_controller_starts_nothing(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)

        result = daemon_main._maybe_start_web_server(
            {"web": {"settings": {"enabled": True}}}, self._ipc_server(), unattended_sessions_enabled=False,
        )

        assert result is None

    def test_settings_enabled_wires_the_controller_into_the_server(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)
        controller = self._controller(tmp_path, monkeypatch)

        result = daemon_main._maybe_start_web_server(
            {"web": {"settings": {"enabled": True}, "port": 18765}}, self._ipc_server(),
            unattended_sessions_enabled=False, controller=controller,
        )

        assert result is not None
        assert result.controller is controller

    def test_allow_quit_defaults_true_and_is_configurable(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)
        controller = self._controller(tmp_path, monkeypatch)

        result = daemon_main._maybe_start_web_server(
            {"web": {"settings": {"enabled": True, "allow_quit": False}}}, self._ipc_server(),
            unattended_sessions_enabled=False, controller=controller,
        )

        assert result.allow_quit is False

    def test_notifications_enabled_defaults_true_and_is_configurable(self, monkeypatch, tmp_path):
        self._no_bind(monkeypatch, tmp_path)

        default_result = daemon_main._maybe_start_web_server(
            {"web": {"approval_ui": "web"}}, self._ipc_server(), unattended_sessions_enabled=False,
        )
        assert default_result.notifications_enabled is True

        off_result = daemon_main._maybe_start_web_server(
            {"web": {"approval_ui": "web", "notifications": {"enabled": False}}}, self._ipc_server(),
            unattended_sessions_enabled=False,
        )
        assert off_result.notifications_enabled is False

    def test_settings_can_run_with_the_native_approval_ui(self, monkeypatch, tmp_path):
        from privacyfence.approval_ui import NativeApprovalUI, get_approval_ui

        self._no_bind(monkeypatch, tmp_path)
        controller = self._controller(tmp_path, monkeypatch)

        result = daemon_main._maybe_start_web_server(
            {"web": {"settings": {"enabled": True}}}, self._ipc_server(),
            unattended_sessions_enabled=False, controller=controller,
        )

        assert result is not None
        assert isinstance(get_approval_ui(), NativeApprovalUI)


# ---------------------------------------------------------------------------- #
# parse_args
# ---------------------------------------------------------------------------- #

class TestParseArgs:
    def test_defaults_have_no_oauth_flags_set(self):
        args = daemon_main.parse_args([])
        assert not any([
            args.gmail_oauth, args.drive_oauth, args.contacts_oauth, args.calendar_oauth,
            args.tasks_oauth, args.apps_script_oauth, args.slack_oauth, args.salesforce_oauth,
            args.atlassian_oauth, args.telegram_setup,
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
        ("--apps-script-oauth", "apps_script_oauth"),
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
            "apps_script_oauth", "slack_oauth", "salesforce_oauth", "atlassian_oauth", "telegram_setup",
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
    pytest.param("run_apps_script_oauth", "AppsScriptClient", "AppsScriptClientError", id="apps_script"),
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
    """Per-test-unique TOKEN_FILE path -- named short_socket_path for
    history (this used to be a Unix socket path)."""
    directory = f"/tmp/pf-{uuid.uuid4().hex[:8]}"
    os.makedirs(directory, exist_ok=True)
    path = f"{directory}/ipc_token"
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

        monkeypatch.setattr(ipc_server_module, "TOKEN_FILE", short_socket_path)
        monkeypatch.setattr(ipc_server_module, "PORT_FILE", short_socket_path.replace("ipc_token", "ipc_port"))
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
# _warm_connector_caches
# ---------------------------------------------------------------------------- #

class TestWarmConnectorCaches:
    """_warm_connector_caches() is what run_app() calls right after the IPC
    thread is ready -- Slack's client is synchronous, so it gets its own
    background thread; Telegram's is asyncio-native and has to run on the
    IPC server's own loop (see the function's docstring). Both are
    fire-and-forget from the caller's point of view, so these tests poll
    briefly for the background work to land rather than joining a handle
    _warm_connector_caches doesn't expose."""

    def _running_loop(self):
        """A bare event loop on its own thread -- stands in for the IPC
        server's loop without needing a real IPCServer/socket."""
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        return loop, thread

    def _stop(self, loop, thread):
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)

    def _wait_until(self, predicate, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def test_slack_connector_warmed_on_its_own_background_thread(self):
        client = MagicMock()
        connector = SlackConnector(client)
        loop, thread = self._running_loop()
        try:
            daemon_main._warm_connector_caches([connector], loop)
            assert self._wait_until(lambda: client.ensure_directories_fresh.called)
        finally:
            self._stop(loop, thread)

    def test_telegram_connector_warmed_on_the_given_ipc_loop(self):
        calls: list[threading.Thread] = []

        class FakeTelegramClient:
            async def ensure_chat_directory_fresh(self):
                calls.append(threading.current_thread())

        connector = TelegramConnector(FakeTelegramClient())
        loop, thread = self._running_loop()
        try:
            daemon_main._warm_connector_caches([connector], loop)
            assert self._wait_until(lambda: bool(calls))
            assert calls[0] is thread
        finally:
            self._stop(loop, thread)

    def test_telegram_warm_failure_is_logged_not_raised(self, caplog):
        class FailingTelegramClient:
            async def ensure_chat_directory_fresh(self):
                raise RuntimeError("boom")

        connector = TelegramConnector(FailingTelegramClient())
        loop, thread = self._running_loop()
        try:
            with caplog.at_level(logging.WARNING, logger="privacyfence.daemon"):
                daemon_main._warm_connector_caches([connector], loop)
                assert self._wait_until(lambda: "Background Telegram cache warm failed" in caplog.text)
            assert "boom" in caplog.text
        finally:
            self._stop(loop, thread)

    def test_other_connector_types_are_left_untouched(self):
        other = MagicMock()
        loop, thread = self._running_loop()
        try:
            daemon_main._warm_connector_caches([other], loop)
            # Nothing to poll for -- this must be a synchronous no-op for a
            # connector that's neither Slack nor Telegram.
            other.client.ensure_directories_fresh.assert_not_called()
        finally:
            self._stop(loop, thread)

    def test_empty_connector_list_is_a_no_op(self):
        loop, thread = self._running_loop()
        try:
            daemon_main._warm_connector_caches([], loop)  # must not raise
        finally:
            self._stop(loop, thread)


# ---------------------------------------------------------------------------- #
# run_app
# ---------------------------------------------------------------------------- #

class _FakeIPCServerThread:
    instances: list["_FakeIPCServerThread"] = []

    def __init__(self, server):
        self.server = server
        self._ready = threading.Event()
        self._ready.set()
        # A harmless non-None placeholder -- real IPCServerThread sets this
        # to its asyncio event loop, and run_app() only ever passes it
        # through to _warm_connector_caches() (which is itself mocked out
        # in most of these tests). Distinct per instance so a test can
        # assert identity against the specific thread it inspects.
        self._loop = SimpleNamespace()
        self.started = False
        type(self).instances.append(self)

    def start(self):
        self.started = True


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="run_app() reaches menu_bar.run_menu_bar (rumps/AppKit), macOS-only -- "
    "same posture as test_settings_window.py's own skipif",
)
class TestRunApp:
    """Every test here reaches the real (monkeypatched) run_menu_bar, which
    means importing privacyfence.menu_bar -- rumps/AppKit, so this whole
    class is macOS-only, including test_lock_already_held_returns_1_without_
    building_connectors even though that one specific path returns before
    ever reaching menu_bar: keeping the skip at class granularity is what
    lets this file's other, genuinely platform-independent classes
    (TestBuildConnectors*, TestSetupLogging, TestMaybeStartWebApprovalUi,
    ...) run on the web/'s Linux CI leg (docs/testing-policy.md §1) without
    hand-marking each test individually."""

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
            lambda conns, **kw: SimpleNamespace(
                connectors=conns, unattended_sessions_enabled=kw.get("unattended_sessions_enabled"),
                # run_app() now constructs a SettingsController (this
                # phase's own web-settings wiring) and hands it this same
                # fake IPCServer -- SettingsController.__init__ always
                # calls set_unattended_changed_listener on whatever
                # ipc_server it's given, and refresh_connectors() (not
                # reached during startup today, but cheap to support) calls
                # set_connectors. Same no-op shape test_settings_controller.py's
                # own ipc_server fixture already uses.
                set_unattended_changed_listener=lambda callback: None,
                set_connectors=lambda conns: None,
            ),
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

    def test_background_cache_warm_kicked_off_with_connectors_and_ipc_loop(self, monkeypatch):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        connector = SimpleNamespace(name="slack")
        self._patch_common(monkeypatch, connectors=[connector])
        monkeypatch.setattr("privacyfence.menu_bar.run_menu_bar", lambda **kw: None)
        warm_calls = []
        monkeypatch.setattr(daemon_main, "_warm_connector_caches", lambda conns, loop: warm_calls.append((conns, loop)))

        daemon_main.run_app({}, "config.yaml")

        assert len(warm_calls) == 1
        assert warm_calls[0][0] == [connector]
        assert warm_calls[0][1] is _FakeIPCServerThread.instances[0]._loop

    def test_background_cache_warm_skipped_and_logged_if_ipc_loop_never_became_ready(self, monkeypatch, caplog):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch)
        monkeypatch.setattr("privacyfence.menu_bar.run_menu_bar", lambda **kw: None)
        # Simulate the IPC thread's loop never getting assigned in time
        # (see IPCServerThread.run()) -- run_app() must not crash on it.
        original_init = _FakeIPCServerThread.__init__

        def init_with_no_loop(self, server):
            original_init(self, server)
            self._loop = None
        monkeypatch.setattr(_FakeIPCServerThread, "__init__", init_with_no_loop)
        warm_calls = []
        monkeypatch.setattr(daemon_main, "_warm_connector_caches", lambda conns, loop: warm_calls.append((conns, loop)))

        with caplog.at_level(logging.WARNING):
            result = daemon_main.run_app({}, "config.yaml")

        assert result == 0
        assert warm_calls == []
        assert "skipping background cache warm" in caplog.text

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

    def test_inconsistent_drive_privacy_categories_log_a_warning(self, monkeypatch, caplog):
        # check_consistency_warnings() runs right after init_privacy_filter()
        # -- see privacy_filter.py. Advisory only, never changes what
        # actually gets filtered.
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch, connectors=[])
        monkeypatch.setattr("privacyfence.menu_bar.run_menu_bar", lambda **kw: None)
        config = {"drive_privacy": {"categories": {"file_list": "allow", "file_metadata": "block"}}}

        with caplog.at_level(logging.WARNING):
            result = daemon_main.run_app(config, "config.yaml")

        assert result == 0
        assert "file_metadata" in caplog.text
        assert "file_list" in caplog.text

    def test_consistent_drive_privacy_categories_log_no_warning(self, monkeypatch, caplog):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch, connectors=[])
        monkeypatch.setattr("privacyfence.menu_bar.run_menu_bar", lambda **kw: None)
        config = {"drive_privacy": {"categories": {"file_list": "allow", "file_metadata": "allow"}}}

        with caplog.at_level(logging.WARNING):
            daemon_main.run_app(config, "config.yaml")

        assert "file_metadata" not in caplog.text

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

    def test_rule_suggestion_priority_is_ignored_and_logged(self, monkeypatch, tmp_path, caplog):
        # Issue #151: every matching auto-accept rule now gets its own
        # "Always allow" button, so there's nothing left to prioritize or
        # exclude -- a pre-existing rule_suggestion_priority block in a
        # user's settings.yaml must still load without error (ignored,
        # logged), same forward-compatible "unknown key is inert" posture
        # used elsewhere.
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch)
        monkeypatch.setattr("privacyfence.menu_bar.run_menu_bar", lambda **kw: None)

        config = {"rule_suggestion_priority": {"drive_read": ["approved_folder", "i_am_owner"]}}
        with caplog.at_level(logging.INFO):
            result = daemon_main.run_app(config, str(tmp_path / "settings.yaml"))

        assert result == 0
        assert "rule_suggestion_priority is no longer used" in caplog.text

    def test_no_rule_suggestion_priority_logs_nothing_about_it(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setattr(daemon_main, "_acquire_instance_lock", lambda: True)
        monkeypatch.setattr(daemon_main, "_release_instance_lock", lambda: None)
        self._patch_common(monkeypatch)
        monkeypatch.setattr("privacyfence.menu_bar.run_menu_bar", lambda **kw: None)

        with caplog.at_level(logging.INFO):
            result = daemon_main.run_app({}, str(tmp_path / "settings.yaml"))

        assert result == 0
        assert "rule_suggestion_priority" not in caplog.text

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
        ("--apps-script-oauth", "run_apps_script_oauth"),
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
