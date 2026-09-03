"""PrivacyFence daemon: persistent macOS app that owns the UI, credentials, and connectors.

Started at login via LaunchAgent (com.privacyfence.app.plist), or automatically
by Claude Desktop's ``.mcpb`` shim on first use. Only one instance is allowed
(enforced via a lock file). Claude reaches this process over the embedded
``/mcp`` Streamable HTTP endpoint (see web/mcp_dispatch.py's module
docstring) -- the original bridge/IPC-socket transport was retired at P5
(docs/https-connector-refactor-plan.md §12); ``connector_host.py``'s
``ConnectorHost`` is what's left of ``ipc_server.py``'s own role once the
socket and its dispatch logic are gone.

Threading model:
  - Main thread:   rumps menu bar app (macOS requirement for AppKit).
  - Web thread:    uvicorn serving the embedded HTTP server (web/server.py)
    that hosts ``/mcp``, and (opt-in) the web approval/settings surfaces --
    this is also the event loop every connector call now actually runs on.
  - Cache warm:    short-lived background thread(s) started right after the
    web server's event loop is known, refreshing Slack/Telegram directory
    caches if they've gone stale -- see _warm_connector_caches(). Kept off
    the main thread so a large workspace/account doesn't delay the menu bar
    icon appearing.
  - Popups:        approval_popup.py shows native AppKit/WKWebView windows (any thread).

Configuration is split into two files (see paths.py):
  - ``org/org_config.json``    — organization-level app registrations (Google
    OAuth client, Slack app, Salesforce Connected App, Atlassian OAuth app),
    installed via "Install/Update Organization Config…" in the menu bar.
    Optional per service; a connector is offered only if its section is
    present. Telegram's api_id/api_hash are the one exception: they identify
    the PrivacyFence app itself (not an organization) and are baked into the
    release build — see app_credentials.py. Also carries
    ``unattended_sessions.enabled`` — a deliberate per-organization opt-in,
    not a per-user setting, so it lives here rather than settings.yaml.
    ``rooms`` (optional) is a static room/resource directory snapshot IT
    refreshes with ``scripts/sync_room_directory.py``, using a separate,
    admin-scoped Google Cloud project — see room_directory_client.py and
    docs/google-cloud-setup.md. It's plain data, not a credential, and is
    handed straight to CalendarConnector; the Calendar OAuth client itself
    never carries Workspace-admin directory scope.
  - ``config/settings.yaml``   — per-user settings: privacy policy,
    connectors{enabled}, auto_accept_rules,
    pii_detection{enabled, detect_ip_addresses, detect_financial_figures,
    audit_match_details}. No secrets live here.
Per-user credentials (OAuth tokens, Telegram session) live under
``credentials/``, one file per connector.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

import yaml

from .paths import data_dir, org_dir, user_dir
from .principal import LOCAL_PRINCIPAL_ID, current_principal
from .app_credentials import telegram_app_credentials
from .approval_ui import init_approval_ui
from .audit_log import init_audit_logger
from .auto_accept import (
    init_config_path,
    migrate_telegram_search_operation_key,
    reload_rules,
)
from .pii_detector import init_pii_detection
from .privacy_filter import check_consistency_warnings, init_privacy_filter
from .resource_grants import build_effective_rules, migrate_rules_to_grants
from .connectors.apps_script import AppsScriptConnector
from .connectors.calendar import CalendarConnector
from .connectors.confluence import ConfluenceConnector
from .connectors.contacts import ContactsConnector
from .connectors.drive import DriveConnector
from .connectors.gmail import GmailConnector
from .connectors.jira import JiraConnector
from .connectors.salesforce import SalesforceConnector
from .connectors.slack import SlackConnector
from .connectors.tasks import TasksConnector
from .connectors.telegram import TelegramConnector
from .apps_script_client import AppsScriptClient, AppsScriptClientError
from .atlassian_oauth import AtlassianOAuthError
from .atlassian_oauth import authorize_interactive as atlassian_authorize_interactive
from .atlassian_oauth import load_token_file as load_atlassian_token
from .calendar_client import CalendarClient, CalendarClientError
from .confluence_client import ConfluenceClient, ConfluenceClientError
from .connector_host import ConnectorHost
from .contacts_client import ContactsClient, ContactsClientError
from .drive_client import DriveClient, DriveClientError
from .gmail_client import GmailClient, GmailClientError
from .jira_client import JiraClient, JiraClientError
from .salesforce_client import SalesforceClient, SalesforceClientError
from .salesforce_client import authorize_interactive as salesforce_authorize_interactive
from .salesforce_client import load_token_file as load_salesforce_token
from .slack_client import SlackClient, SlackClientError
from .slack_client import authorize_interactive as slack_authorize_interactive
from .slack_client import load_token_file as load_slack_token
from .tasks_client import TasksClient, TasksClientError
from .telegram_client import TelegramClientError, TelegramPrivacyFenceClient

logger = logging.getLogger("privacyfence.daemon")

PROJECT_ROOT = str(data_dir())
LOCK_FILE = os.path.join(PROJECT_ROOT, "privacyfence.lock")

# Where each connector's per-user credential is cached. Purely internal — no
# longer user-configurable, since org app registration and per-user auth are
# now handled separately (see module docstring).
TOKEN_FILES: dict[str, str] = {
    "gmail": "credentials/token.json",
    "drive": "credentials/drive_token.json",
    "calendar": "credentials/calendar_token.json",
    "contacts": "credentials/contacts_token.json",
    "tasks": "credentials/tasks_token.json",
    "apps_script": "credentials/apps_script_token.json",
    "slack": "credentials/slack_token.json",
    "salesforce": "credentials/salesforce_token.json",
    "atlassian": "credentials/atlassian_token.json",
    "telegram": "credentials/telegram.session",
}

_lock_fd: int | None = None


# ---------------------------------------------------------------------------- #
# Instance lock
# ---------------------------------------------------------------------------- #

def _acquire_instance_lock() -> bool:
    global _lock_fd
    fd = os.open(LOCK_FILE, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    _lock_fd = fd
    return True


def _release_instance_lock() -> None:
    global _lock_fd
    if _lock_fd is not None:
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            os.close(_lock_fd)
        except OSError:
            pass
        _lock_fd = None


# ---------------------------------------------------------------------------- #
# Configuration & logging
# ---------------------------------------------------------------------------- #

def _resolve_path(path: str) -> str:
    """Relative to ``PROJECT_ROOT`` for the local principal -- exactly as
    before this phase, including for the tests that monkeypatch
    ``PROJECT_ROOT`` directly to sandbox where a test run reads/writes --
    or to that *other* principal's own storage root (P6, docs/
    https-connector-refactor-plan.md §9.2) when this runs inside a
    ``principal_scope()`` block for someone else (only
    connector_registry.py's ``ConnectorRegistry.get()`` does that today).
    """
    if os.path.isabs(path):
        return path
    principal = current_principal()
    if principal.id == LOCAL_PRINCIPAL_ID:
        return os.path.join(PROJECT_ROOT, path)
    return str(user_dir(principal) / path)


def _bootstrap_config(resolved: str) -> None:
    """Seed a default settings.yaml from the packaged example on first run.

    The example carries no secrets (org credentials and per-user auth are
    handled separately via the menu bar), so it's safe to install
    automatically now that there's no setup wizard to do it.
    """
    example = Path(__file__).parent / "resources" / "settings.yaml.example"
    os.makedirs(os.path.dirname(resolved), exist_ok=True)
    shutil.copyfile(example, resolved)


def load_config(config_path: str) -> dict[str, Any]:
    resolved = _resolve_path(config_path)
    if not os.path.exists(resolved):
        _bootstrap_config(resolved)
    with open(resolved, encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}
    if not isinstance(config, dict):
        raise ValueError(f"Config file {resolved} did not parse to a mapping")
    return config


def load_org_config() -> dict[str, Any]:
    """Load the installed organization config bundle, or {} if none is installed.

    Never fatal — same "missing config → connector skipped" philosophy used
    for every connector below. Installed via "Install/Update Organization
    Config…" in the menu bar (see menu_bar.py).
    """
    path = org_dir() / "org_config.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read organization config at %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("Organization config at %s is not a JSON object; ignoring", path)
        return {}
    return data


def setup_logging(config: dict[str, Any]) -> None:
    log_cfg = config.get("logging", {}) or {}
    level_name = str(log_cfg.get("level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    log_file = _resolve_path(log_cfg.get("file", "logs/privacyfence.log"))
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    fmt = logging.Formatter("%(asctime)s %(levelname)-8s [%(name)s] %(message)s")
    handlers: list[logging.Handler] = [
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ]
    for h in handlers:
        h.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    for h in handlers:
        root.addHandler(h)

    logger.info("Logging initialized → %s", log_file)


# ---------------------------------------------------------------------------- #
# Web approval UI + MCP-over-HTTP (see docs/https-connector-refactor-plan.md's
# P1/P2) -- opt-in, selected by config/settings.yaml's web.approval_ui and
# web.mcp.enabled respectively. web.approval_ui's "web" is the seam
# approval_ui.init_approval_ui() switches; native (NativeApprovalUI, AppKit)
# stays the default so nothing changes for an install that doesn't set this.
# web.mcp.enabled is independent of it (§8 of that document is a transport
# change, orthogonal to which ApprovalUI shows the resulting popup) -- either
# setting alone is enough to start the embedded HTTP server; both share the
# one server/one port, per §3's target architecture.
# ---------------------------------------------------------------------------- #

def _maybe_start_web_server(
    config: dict[str, Any],
    connector_host: ConnectorHost,
    *,
    unattended_sessions_enabled: bool,
    controller: Any = None,
) -> Any:
    """Returns the started WebServer, or None when neither web.approval_ui
    nor web.mcp.enabled nor web.settings.enabled opts in -- the rollback
    lever for each surface from docs/https-connector-refactor-plan.md §12
    ("P1: init_approval_ui() -- the seam itself. A config key selects
    native or web." / "P2: the HTTP listener is off unless configured" /
    §16.6's ``web.settings.enabled``). Since P5 retired the bridge, turning
    ``mcp.enabled`` off leaves this install with no way for Claude to reach
    it at all -- ``web.mcp.enabled: true`` (settings.yaml.example's default
    since D11/P4b) is no longer "additive alongside the bridge", it is the
    only transport there is; the key survives as a deliberate full-stop
    kill switch, not as a rollback to some other still-working path.
    Imports the web/starlette/uvicorn/mcp stack lazily so a daemon that
    never opts into any of the three doesn't pay for it at startup, the
    same "menu_bar imported inside run_app(), not at module scope" posture
    this module already takes for its own AppKit-only pieces.

    ``connector_host`` is already built and holds the real connector set by
    the time this is called (see run_app's ordering) -- the MCP dispatcher
    polls ``connector_host.connectors`` live rather than taking its own
    snapshot, so a connector rebuild pushed by SettingsController.
    refresh_connectors (-> ConnectorHost.set_connectors) reaches the
    ``/mcp`` endpoint too, with nothing here needing a second push.

    ``controller``, when given, is the *same* SettingsController instance
    run_app() also hands to the native menu bar/settings window -- one
    controller, two ``on_change`` consumers (§16.8's risk #2), so a rule
    changed from one surface reaches the other with no separate plumbing.
    Independent of ``use_web_approval_ui``/``mcp_enabled``: a deployment can
    run the web settings page with the *native* approval dialog, or the
    reverse (§16.6).
    """
    web_config = config.get("web", {}) or {}
    mcp_config = web_config.get("mcp", {}) or {}
    settings_config = web_config.get("settings", {}) or {}
    notifications_config = web_config.get("notifications", {}) or {}
    use_web_approval_ui = web_config.get("approval_ui", "native") == "web"
    mcp_enabled = bool(mcp_config.get("enabled", False))
    use_web_settings = bool(settings_config.get("enabled", False)) and controller is not None
    if not use_web_approval_ui and not mcp_enabled and not use_web_settings:
        return None

    from .approvals import PendingApprovalRegistry
    from .web.mcp_dispatch import McpDispatcher
    from .web.server import DEFAULT_PORT, WebServer
    from .web_approval_ui import init_web_approval_ui

    # web.approvals.* overrides D3's defaults (docs/https-connector-refactor-
    # plan.md §15: "hold 30s, pending TTL 15 min, ledger TTL 5 min" --
    # "these defaults are what P3's beta measures against"). One registry
    # backs both the web approval surface and privacyfence_await_approval
    # (below), whichever of use_web_approval_ui/mcp_enabled is actually on --
    # constructing it unconditionally here costs nothing (it's just an empty
    # dict-backed object until something registers into it) and means
    # turning mcp.enabled on later, without restarting, would find it ready.
    approvals_config = web_config.get("approvals", {}) or {}
    registry = PendingApprovalRegistry(
        hold_window=float(approvals_config.get("hold_window_seconds", 30.0)),
        pending_ttl=float(approvals_config.get("pending_ttl_seconds", 15 * 60.0)),
        ledger_ttl=float(approvals_config.get("ledger_ttl_seconds", 5 * 60.0)),
        max_pending=int(approvals_config.get("max_pending", 50)),
    )
    web_ui = init_web_approval_ui(registry=registry)
    if use_web_approval_ui:
        init_approval_ui(web_ui)

    mcp_dispatcher = None
    if mcp_enabled:
        mcp_dispatcher = McpDispatcher(
            lambda: connector_host.connectors, unattended_sessions_enabled=unattended_sessions_enabled,
            registry=registry,
        )
        if controller is not None:
            # The direct successor of ipc_server.py's own constructor-time
            # ``ipc_server.set_unattended_changed_listener(self._on_
            # unattended_changed)`` wiring -- moved out here because, unlike
            # the old always-on IPCServer, whether a dispatcher exists at
            # all now depends on mcp_enabled, which SettingsController's own
            # constructor has no visibility into.
            controller.wire_unattended_listener(mcp_dispatcher)

    server = WebServer(
        web_ui,
        port=int(web_config.get("port", DEFAULT_PORT)),
        mcp_dispatcher=mcp_dispatcher,
        controller=controller if use_web_settings else None,
        allow_quit=bool(settings_config.get("allow_quit", True)),
        notifications_enabled=bool(notifications_config.get("enabled", True)),
        notifications_detail=str(notifications_config.get("detail", "minimal")),
    )
    server.start()
    # The pending-result URL gate.py hands back to Claude (§5.2 point 4) is
    # only meaningful once the server is actually listening -- set here,
    # not at registry construction, and left unset (None) if this daemon
    # never starts the web server at all, in which case gate.py's own
    # _pending_result() just omits it.
    registry.set_base_url(server.base_url)
    if use_web_approval_ui:
        logger.info(
            "Web approval UI active -- approvals open at %s/approvals?token=%s",
            server.base_url, server.token,
        )
    if use_web_settings:
        logger.info(
            "Web settings active -- open at %s/settings?token=%s",
            server.base_url, server.token,
        )
    if server.mcp_url:
        from .web.mcp_auth import MCP_TOKEN_FILE_NAME

        logger.info(
            "MCP-over-HTTP active -- %s (Authorization: Bearer <token in %s>)",
            server.mcp_url, data_dir() / MCP_TOKEN_FILE_NAME,
        )
    return server


def _google_client_config(org_config: dict[str, Any]) -> dict[str, Any]:
    """Wrap the bundle's flat Google app fields back into the "installed" shape
    that ``InstalledAppFlow.from_client_config`` expects."""
    google = org_config.get("google") or {}
    if not google.get("client_id") or not google.get("client_secret"):
        return {}
    return {"installed": google}


# ---------------------------------------------------------------------------- #
# Connector construction (graceful: missing org config or auth → connector skipped)
# ---------------------------------------------------------------------------- #

def build_connectors(config: dict[str, Any], org_config: dict[str, Any]) -> list:
    """Builds every enabled, currently-authenticated connector for the
    *current principal* (P6, docs/https-connector-refactor-plan.md §9.2):
    every credential/cache path below resolves through ``_resolve_path()``/
    ``user_dir()``, which is the local principal's own storage root (i.e.
    unchanged from before this phase) unless this is called from inside a
    ``principal_scope()`` block for someone else -- see
    connector_registry.py's ``ConnectorRegistry``, which is what actually
    does that once a second principal's connectors are buildable at all
    (P8). ``run_app()`` below still calls this directly, once, for the local
    principal only -- that's what keeps local mode byte-identical."""
    connectors: list[Any] = []
    connectors_cfg: dict[str, dict] = config.get("connectors", {}) or {}

    def enabled(name: str) -> bool:
        return (connectors_cfg.get(name) or {}).get("enabled", True)

    google_client_config = _google_client_config(org_config)

    # Gmail
    if enabled("gmail"):
        try:
            if not google_client_config:
                raise GmailClientError("Google organization config not installed")
            client = GmailClient(
                client_config=google_client_config,
                token_file=_resolve_path(TOKEN_FILES["gmail"]),
            )
            email = client.check_connection()
            logger.info("Gmail connector ready for %s", email)
            connector = GmailConnector(client)
            connector.my_email = email
            connectors.append(connector)
        except (GmailClientError, FileNotFoundError) as exc:
            logger.warning("Gmail connector disabled: %s", exc)

    # Drive
    if enabled("drive"):
        try:
            if not google_client_config:
                raise DriveClientError("Google organization config not installed")
            client = DriveClient(
                client_config=google_client_config,
                token_file=_resolve_path(TOKEN_FILES["drive"]),
            )
            email = client.check_connection()
            logger.info("Drive connector ready for %s", email)
            connector = DriveConnector(client)
            connector.my_email = email
            connectors.append(connector)
        except (DriveClientError, FileNotFoundError) as exc:
            logger.warning("Drive connector disabled: %s", exc)

    # Calendar
    if enabled("calendar"):
        try:
            if not google_client_config:
                raise CalendarClientError("Google organization config not installed")
            client = CalendarClient(
                client_config=google_client_config,
                token_file=_resolve_path(TOKEN_FILES["calendar"]),
            )
            email = client.check_connection()
            logger.info("Calendar connector ready for %s", email)
            connector = CalendarConnector(client, rooms=org_config.get("rooms", []))
            connector.my_email = email
            connector.free_busy_full_details = bool(
                (config.get("calendar", {}) or {}).get("free_busy_full_event_details", True)
            )
            connectors.append(connector)
        except (CalendarClientError, FileNotFoundError) as exc:
            logger.warning("Calendar connector disabled: %s", exc)

    # Contacts
    if enabled("contacts"):
        try:
            if not google_client_config:
                raise ContactsClientError("Google organization config not installed")
            client = ContactsClient(
                client_config=google_client_config,
                token_file=_resolve_path(TOKEN_FILES["contacts"]),
            )
            email = client.check_connection()
            logger.info("Contacts connector ready for %s", email)
            connector = ContactsConnector(client)
            connector.my_email = email
            connectors.append(connector)
        except (ContactsClientError, FileNotFoundError) as exc:
            logger.warning("Contacts connector disabled: %s", exc)

    # Tasks
    if enabled("tasks"):
        try:
            if not google_client_config:
                raise TasksClientError("Google organization config not installed")
            client = TasksClient(
                client_config=google_client_config,
                token_file=_resolve_path(TOKEN_FILES["tasks"]),
            )
            email = client.check_connection()
            logger.info("Tasks connector ready for %s", email)
            connectors.append(TasksConnector(client))
        except (TasksClientError, FileNotFoundError) as exc:
            logger.warning("Tasks connector disabled: %s", exc)

    # Apps Script
    if enabled("apps_script"):
        try:
            if not google_client_config:
                raise AppsScriptClientError("Google organization config not installed")
            client = AppsScriptClient(
                client_config=google_client_config,
                token_file=_resolve_path(TOKEN_FILES["apps_script"]),
            )
            email = client.check_connection()
            logger.info("Apps Script connector ready for %s", email)
            connectors.append(AppsScriptConnector(client))
        except (AppsScriptClientError, FileNotFoundError) as exc:
            logger.warning("Apps Script connector disabled: %s", exc)

    # Slack
    if enabled("slack"):
        try:
            slack_org = org_config.get("slack") or {}
            if not slack_org.get("client_id"):
                raise SlackClientError("Slack organization config not installed")
            token = load_slack_token(_resolve_path(TOKEN_FILES["slack"]))
            client = SlackClient(
                user_token=token.get("access_token", ""),
                user_cache_file=str(user_dir() / "slack_user_cache.json"),
                channel_cache_file=str(user_dir() / "slack_channel_cache.json"),
            )
            workspace = client.check_connection()
            # Directory-cache warming (if stale) happens after the whole
            # connector list is built, in the background -- see
            # _warm_connector_caches() in run_app(). Doing it here,
            # synchronously, used to delay the menu bar icon appearing
            # until a full users.list/conversations.list re-sync finished,
            # which read as "the app isn't running yet."
            logger.info("Slack connector ready for workspace %r", workspace)
            connector = SlackConnector(client)
            connector.my_email = token.get("email", "")
            connectors.append(connector)
        except (SlackClientError, FileNotFoundError) as exc:
            logger.warning("Slack connector disabled: %s", exc)

    # Salesforce
    if enabled("salesforce"):
        try:
            sf_org = org_config.get("salesforce") or {}
            if not sf_org.get("consumer_key"):
                raise SalesforceClientError("Salesforce organization config not installed")
            token = load_salesforce_token(_resolve_path(TOKEN_FILES["salesforce"]))
            merged = {**sf_org, **token}
            client = SalesforceClient(config=merged, token_file=_resolve_path(TOKEN_FILES["salesforce"]))
            client.check_connection()
            logger.info("Salesforce connector ready for %s", merged.get("instance_url"))
            connectors.append(SalesforceConnector(client))
        except (SalesforceClientError, FileNotFoundError) as exc:
            logger.warning("Salesforce connector disabled: %s", exc)

    # Jira / Confluence — share one Atlassian OAuth grant.
    atlassian_org = org_config.get("atlassian") or {}
    atlassian_token: dict[str, Any] | None = None
    if atlassian_org.get("client_id"):
        try:
            atlassian_token = load_atlassian_token(_resolve_path(TOKEN_FILES["atlassian"]))
        except AtlassianOAuthError:
            atlassian_token = None
    # Merge in client_id/client_secret so JiraClient/ConfluenceClient can
    # refresh an expired access token instead of forcing re-authentication
    # on every restart (the token file only ever holds the per-user fields).
    atlassian_config = {**atlassian_org, **(atlassian_token or {})}

    if enabled("jira"):
        try:
            if not atlassian_org.get("client_id"):
                raise JiraClientError("Atlassian organization config not installed")
            if not atlassian_token:
                raise JiraClientError("Jira is not authenticated. Use Authenticate… in the menu bar.")
            client = JiraClient(config=atlassian_config, token_file=_resolve_path(TOKEN_FILES["atlassian"]))
            info = client.check_connection()
            logger.info("Jira connector ready: %s", info)
            connector = JiraConnector(client)
            connector.my_email = atlassian_token.get("account_email", "")
            connectors.append(connector)
        except (JiraClientError, FileNotFoundError) as exc:
            logger.warning("Jira connector disabled: %s", exc)

    if enabled("confluence"):
        try:
            if not atlassian_org.get("client_id"):
                raise ConfluenceClientError("Atlassian organization config not installed")
            if not atlassian_token:
                raise ConfluenceClientError("Confluence is not authenticated. Use Authenticate… in the menu bar.")
            client = ConfluenceClient(config=atlassian_config, token_file=_resolve_path(TOKEN_FILES["atlassian"]))
            url = client.check_connection()
            logger.info("Confluence connector ready: %s", url)
            connector = ConfluenceConnector(client)
            connector.my_email = atlassian_token.get("account_email", "")
            connectors.append(connector)
        except (ConfluenceClientError, FileNotFoundError) as exc:
            logger.warning("Confluence connector disabled: %s", exc)

    # Telegram — the sole exception to browser OAuth (MTProto has no
    # equivalent for full user-session access). api_id/api_hash identify the
    # PrivacyFence app itself and are baked into the build (app_credentials.py),
    # not part of the organization config bundle; phone+code(+2FA) auth is
    # still per-user.
    if enabled("telegram"):
        try:
            creds = telegram_app_credentials()
            if not creds:
                raise TelegramClientError("Telegram app credentials not available in this build")
            api_id, api_hash = creds
            session_file = _resolve_path(TOKEN_FILES["telegram"])
            if not os.path.exists(session_file) and not os.path.exists(session_file + ".session"):
                raise TelegramClientError(
                    "Telegram is not authenticated. Use Authenticate… in the PrivacyFence menu bar."
                )
            tg_client = TelegramPrivacyFenceClient(
                api_id=api_id,
                api_hash=api_hash,
                session_file=session_file,
                chat_cache_file=str(user_dir() / "telegram_chat_cache.json"),
            )
            # Directory-cache warming happens the same way as Slack's now --
            # see _warm_connector_caches() in run_app() -- just scheduled on
            # the web server's own event loop rather than awaited here,
            # since that's the loop every Telegram tool call now actually
            # runs on (McpDispatcher.call(), on the ASGI app's own loop) and
            # therefore the loop Telethon's client ends up bound to on its
            # first connection (see telegram_client.py).
            logger.info("Telegram connector registered (chat cache will warm in the background)")
            connectors.append(TelegramConnector(tg_client))
        except (TelegramClientError, FileNotFoundError, Exception) as exc:
            logger.warning("Telegram connector disabled: %s", exc)

    return connectors


def _warm_connector_caches(connectors: list, web_loop: asyncio.AbstractEventLoop) -> None:
    """Kick off each connector's directory-cache freshness check (Slack's
    user/channel snapshots, Telegram's chat snapshot) in the background,
    right after the web server's event loop is known (see run_app()). Both
    ensure_directories_fresh() and ensure_chat_directory_fresh() are
    best-effort and never raise -- a failure here just means the cache
    stays stale until the next lazy lookup or the explicit
    slack_refresh_*/telegram_refresh_chat_cache tool, same as if this
    warming never ran.

    Deliberately not run inline in build_connectors(), and not awaited
    here either: a full weekly re-sync (users.list/conversations.list/
    get_dialogs) can take a while on a large workspace/account, and running
    it synchronously on the main thread used to delay the menu bar icon
    appearing until it finished -- which read as "the app isn't running
    yet."

    Slack's client is synchronous (blocking HTTP via slack_sdk), so it gets
    its own plain background thread. Telegram's is asyncio-native
    (Telethon) and its client binds to whichever event loop first connects
    it -- that has to be ``web_loop`` (the ASGI app's own loop, captured by
    web/server.py's WebServer -- see run_app()'s wait_until_ready() call),
    the same loop every Telegram tool call now actually runs on
    (McpDispatcher.call()), not a throwaway loop of some other thread -- so
    it's scheduled there via run_coroutine_threadsafe instead.
    """
    for connector in connectors:
        if isinstance(connector, SlackConnector):
            threading.Thread(
                target=connector.client.ensure_directories_fresh,
                name="slack-cache-warm",
                daemon=True,
            ).start()
        elif isinstance(connector, TelegramConnector):
            future = asyncio.run_coroutine_threadsafe(
                connector.client.ensure_chat_directory_fresh(), web_loop
            )
            future.add_done_callback(_log_cache_warm_failure)


def _log_cache_warm_failure(future: "asyncio.Future[None]") -> None:
    # Defensive only -- ensure_chat_directory_fresh() is documented never to
    # raise, same as ensure_directories_fresh(). If it somehow does, this is
    # a background warm with nothing waiting on its result, so log instead
    # of letting the exception vanish into the event loop's default handler.
    exc = future.exception()
    if exc is not None:
        logger.warning("Background Telegram cache warm failed: %s", exc)


# ---------------------------------------------------------------------------- #
# OAuth / interactive-auth setup commands (headless/dev use — the primary UX
# path is now "Authenticate…" in the menu bar, see menu_bar.py)
# ---------------------------------------------------------------------------- #

def run_gmail_oauth(org_config: dict[str, Any]) -> int:
    client_config = _google_client_config(org_config)
    client = GmailClient(client_config=client_config, token_file=_resolve_path(TOKEN_FILES["gmail"]))
    try:
        client.authorize_interactive()
        email = client.check_connection()
    except GmailClientError as exc:
        print(f"Gmail OAuth setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Gmail OAuth complete. Authorized as: {email}")
    return 0


def run_drive_oauth(org_config: dict[str, Any]) -> int:
    client_config = _google_client_config(org_config)
    client = DriveClient(client_config=client_config, token_file=_resolve_path(TOKEN_FILES["drive"]))
    try:
        client.authorize_interactive()
        email = client.check_connection()
    except DriveClientError as exc:
        print(f"Drive OAuth setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Drive OAuth complete. Authorized as: {email}")
    return 0


def run_contacts_oauth(org_config: dict[str, Any]) -> int:
    client_config = _google_client_config(org_config)
    client = ContactsClient(client_config=client_config, token_file=_resolve_path(TOKEN_FILES["contacts"]))
    try:
        client.authorize_interactive()
        result = client.check_connection()
    except ContactsClientError as exc:
        print(f"Contacts OAuth setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Contacts OAuth complete. Authorized as: {result}")
    return 0


def run_calendar_oauth(org_config: dict[str, Any]) -> int:
    client_config = _google_client_config(org_config)
    client = CalendarClient(client_config=client_config, token_file=_resolve_path(TOKEN_FILES["calendar"]))
    try:
        client.authorize_interactive()
        email = client.check_connection()
    except CalendarClientError as exc:
        print(f"Calendar OAuth setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Calendar OAuth complete. Authorized as: {email}")
    return 0


def run_tasks_oauth(org_config: dict[str, Any]) -> int:
    client_config = _google_client_config(org_config)
    client = TasksClient(client_config=client_config, token_file=_resolve_path(TOKEN_FILES["tasks"]))
    try:
        client.authorize_interactive()
        email = client.check_connection()
    except TasksClientError as exc:
        print(f"Tasks OAuth setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Tasks OAuth complete. Authorized as: {email}")
    return 0


def run_apps_script_oauth(org_config: dict[str, Any]) -> int:
    client_config = _google_client_config(org_config)
    client = AppsScriptClient(client_config=client_config, token_file=_resolve_path(TOKEN_FILES["apps_script"]))
    try:
        client.authorize_interactive()
        email = client.check_connection()
    except AppsScriptClientError as exc:
        print(f"Apps Script OAuth setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Apps Script OAuth complete. Authorized as: {email}")
    return 0


def run_slack_oauth(org_config: dict[str, Any]) -> int:
    slack_org = org_config.get("slack") or {}
    if not slack_org.get("client_id") or not slack_org.get("client_secret"):
        print("No Slack organization config installed.", file=sys.stderr)
        return 1
    try:
        token = slack_authorize_interactive(
            client_id=slack_org["client_id"],
            client_secret=slack_org["client_secret"],
            token_file=_resolve_path(TOKEN_FILES["slack"]),
            user_scopes=slack_org.get("user_scopes"),
        )
    except SlackClientError as exc:
        print(f"Slack OAuth setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Slack OAuth complete. Authorized for workspace: {token.get('team_name')}")
    return 0


def run_salesforce_oauth(org_config: dict[str, Any]) -> int:
    sf_org = org_config.get("salesforce") or {}
    if not sf_org.get("consumer_key") or not sf_org.get("consumer_secret"):
        print("No Salesforce organization config installed.", file=sys.stderr)
        return 1
    try:
        token = salesforce_authorize_interactive(
            consumer_key=sf_org["consumer_key"],
            consumer_secret=sf_org["consumer_secret"],
            token_file=_resolve_path(TOKEN_FILES["salesforce"]),
            login_url=sf_org.get("login_url", "https://login.salesforce.com"),
        )
    except SalesforceClientError as exc:
        print(f"Salesforce OAuth setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Salesforce OAuth complete. Authorized for instance: {token.get('instance_url')}")
    return 0


def run_atlassian_oauth(org_config: dict[str, Any]) -> int:
    atlassian_org = org_config.get("atlassian") or {}
    if not atlassian_org.get("client_id") or not atlassian_org.get("client_secret"):
        print("No Atlassian organization config installed.", file=sys.stderr)
        return 1
    try:
        token = atlassian_authorize_interactive(
            client_id=atlassian_org["client_id"],
            client_secret=atlassian_org["client_secret"],
            token_file=_resolve_path(TOKEN_FILES["atlassian"]),
        )
    except AtlassianOAuthError as exc:
        print(f"Atlassian OAuth setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Atlassian OAuth complete. Authorized for site: {token.get('site_url')}")
    return 0


def run_telegram_setup() -> int:
    creds = telegram_app_credentials()
    if not creds:
        print(
            "No Telegram app credentials in this build. For local dev, set "
            "PRIVACYFENCE_TELEGRAM_API_ID and PRIVACYFENCE_TELEGRAM_API_HASH.",
            file=sys.stderr,
        )
        return 1
    api_id, api_hash = creds
    session_file = _resolve_path(TOKEN_FILES["telegram"])
    client = TelegramPrivacyFenceClient(api_id=api_id, api_hash=api_hash, session_file=session_file)
    asyncio.run(client.authorize_interactive())
    print(f"Telegram session saved to {session_file}")
    return 0


# ---------------------------------------------------------------------------- #
# Main app
# ---------------------------------------------------------------------------- #

def run_app(config: dict[str, Any], config_path: str) -> int:
    if not _acquire_instance_lock():
        logger.error("Another instance is already running; exiting.")
        print("PrivacyFence daemon is already running.", file=sys.stderr)
        return 1

    init_config_path(_resolve_path(config_path))

    config, migration_summary = migrate_rules_to_grants(config)
    config, telegram_search_migrated = migrate_telegram_search_operation_key(config)
    if migration_summary or telegram_search_migrated:
        try:
            with open(_resolve_path(config_path), "w", encoding="utf-8") as fh:
                yaml.safe_dump(config, fh, default_flow_style=False, allow_unicode=True)
            if migration_summary:
                logger.info(
                    "Auto-accept config migrated to connector-scoped grants:\n  %s",
                    "\n  ".join(migration_summary),
                )
            if telegram_search_migrated:
                logger.info(
                    "Auto-accept config migrated: telegram.search_messages rules "
                    "moved onto telegram.read_chat_messages"
                )
        except OSError as exc:
            logger.warning("Could not persist auto-accept config migration: %s", exc)

    reload_rules(build_effective_rules(config))
    if "rule_suggestion_priority" in config:
        # Issue #151: every matching auto-accept rule now gets its own
        # "Always allow" button, so there's nothing left to prioritize or
        # exclude -- same forward-compatible "unknown key is inert" posture
        # used elsewhere, not a dedicated migration (nothing to migrate
        # *to*). A pre-existing settings.yaml with this key still loads
        # without error; it's just never consulted again.
        logger.info(
            "rule_suggestion_priority is no longer used -- every matching rule now gets its own "
            "\"Always allow\" button. Ignoring this settings.yaml key."
        )
    pii_config = config.get("pii_detection", {}) or {}
    init_pii_detection(
        pii_config.get("enabled", True),
        detect_ip_addresses=pii_config.get("detect_ip_addresses", True),
        detect_financial_figures=pii_config.get("detect_financial_figures", True),
        audit_match_details=pii_config.get("audit_match_details", False),
    )
    init_privacy_filter(config)
    for warning in check_consistency_warnings():
        logger.warning(warning)

    audit_logger = init_audit_logger(str(Path(data_dir()) / "logs" / "audit"))
    audit_logger.export_all_pending()

    org_config = load_org_config()
    connectors = build_connectors(config, org_config)
    if not connectors:
        logger.warning("No connectors could be initialized; daemon still starting.")

    unattended_enabled = bool((org_config.get("unattended_sessions", {}) or {}).get("enabled", False))
    connector_host = ConnectorHost(connectors)

    # Built once, here, and handed to *both* the web settings surface
    # (_maybe_start_web_server, below) and the native menu bar/settings
    # window (run_menu_bar, further down) -- one SettingsController
    # instance either way (§16.8's risk #2: "two on_change consumers"),
    # rather than each surface building its own and drifting out of sync
    # with the other's in-memory state (_busy_connectors, _telegram_auth,
    # the update-check cache). PrivacyFenceMenuBar.__init__ builds its own
    # only when none is passed in (native-only installs, unchanged).
    from .settings_controller import SettingsController

    connector_names = [c.name for c in connectors]
    settings_controller = SettingsController(
        config_path=config_path, connectors=connector_names, connector_host=connector_host,
        connector_objs=connectors,
    )

    # Built after connector_host so the MCP dispatcher (if web.mcp.enabled)
    # can poll connector_host.connectors for the live connector set -- see
    # _maybe_start_web_server's own docstring.
    server = _maybe_start_web_server(
        config, connector_host, unattended_sessions_enabled=unattended_enabled, controller=settings_controller,
    )

    # Every connector call now runs on the embedded web server's own ASGI
    # event loop (McpDispatcher.call(), via /mcp) -- there is no separate
    # IPC loop to wait on any more, so Telegram's cache warm (the one piece
    # that needs a live loop, not just a thread -- see
    # _warm_connector_caches' own docstring) waits for that loop to be
    # captured instead. None when this daemon never starts the web server
    # at all (every web.* surface opted out) -- in that configuration
    # nothing can reach a connector regardless, so there is nothing to warm
    # a cache for.
    web_loop = server.wait_until_ready(timeout=5) if server is not None else None
    logger.info("Startup complete, starting menu bar")

    if web_loop is not None:
        _warm_connector_caches(connectors, web_loop)
    elif server is not None:
        logger.warning("Web server event loop not ready in time; skipping background cache warm")

    from .menu_bar import run_menu_bar
    try:
        run_menu_bar(
            config_path=config_path,
            connectors=connector_names,
            connector_host=connector_host,
            connector_objs=connectors,
            controller=settings_controller,
        )
    except KeyboardInterrupt:
        logger.info("Interrupted; shutting down")
    finally:
        _release_instance_lock()
    return 0


# ---------------------------------------------------------------------------- #
# Argument parsing
# ---------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="privacyfence-app",
        description="PrivacyFence daemon — governance UI and connector host.",
    )
    default_config = os.path.join(PROJECT_ROOT, "config", "settings.yaml")
    parser.add_argument("--config", default=default_config)
    parser.add_argument("--gmail-oauth", action="store_true")
    parser.add_argument("--drive-oauth", action="store_true")
    parser.add_argument("--contacts-oauth", action="store_true")
    parser.add_argument("--calendar-oauth", action="store_true")
    parser.add_argument("--tasks-oauth", action="store_true")
    parser.add_argument("--apps-script-oauth", action="store_true")
    parser.add_argument("--slack-oauth", action="store_true")
    parser.add_argument("--salesforce-oauth", action="store_true")
    parser.add_argument("--atlassian-oauth", action="store_true")
    parser.add_argument("--telegram-setup", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    oauth_flag = (
        args.gmail_oauth or args.drive_oauth or args.contacts_oauth
        or args.calendar_oauth or args.tasks_oauth or args.apps_script_oauth
        or args.slack_oauth
        or args.salesforce_oauth or args.atlassian_oauth or args.telegram_setup
    )

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    setup_logging(config)

    try:
        if oauth_flag:
            org_config = load_org_config()
            if args.gmail_oauth:
                return run_gmail_oauth(org_config)
            if args.drive_oauth:
                return run_drive_oauth(org_config)
            if args.contacts_oauth:
                return run_contacts_oauth(org_config)
            if args.calendar_oauth:
                return run_calendar_oauth(org_config)
            if args.tasks_oauth:
                return run_tasks_oauth(org_config)
            if args.apps_script_oauth:
                return run_apps_script_oauth(org_config)
            if args.slack_oauth:
                return run_slack_oauth(org_config)
            if args.salesforce_oauth:
                return run_salesforce_oauth(org_config)
            if args.atlassian_oauth:
                return run_atlassian_oauth(org_config)
            if args.telegram_setup:
                return run_telegram_setup()
        return run_app(config, args.config)
    except Exception as exc:
        logger.error("Fatal error: %s", exc, exc_info=True)
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
