"""PrivacyFence daemon: persistent macOS app that owns the UI, credentials, and connectors.

Started at login via LaunchAgent (com.privacyfence.app.plist), or automatically
by the bridge on first use. Only one instance is allowed (enforced via a lock
file). The bridge connects to this process over a Unix socket.

Threading model:
  - Main thread:   rumps menu bar app (macOS requirement for AppKit).
  - IPC thread:    asyncio event loop serving the bridge socket connection.
  - Popups:        approval_popup.py uses osascript subprocesses (any thread).

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
  - ``config/settings.yaml``   — per-user settings: privacy policy,
    connectors{enabled}, auto_accept_rules,
    pii_detection{enabled, detect_ip_addresses, detect_financial_figures}. No
    secrets live here.
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

from .paths import data_dir, org_dir
from .app_credentials import telegram_app_credentials
from .audit_log import init_audit_logger
from .auto_accept import init_config_path, reload_rules
from .pii_detector import init_pii_detection
from .privacy_filter import init_privacy_filter
from .resource_grants import build_effective_rules, migrate_rules_to_grants
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
from .atlassian_oauth import AtlassianOAuthError
from .atlassian_oauth import authorize_interactive as atlassian_authorize_interactive
from .atlassian_oauth import load_token_file as load_atlassian_token
from .calendar_client import CalendarClient, CalendarClientError
from .confluence_client import ConfluenceClient, ConfluenceClientError
from .contacts_client import ContactsClient, ContactsClientError
from .drive_client import DriveClient, DriveClientError
from .gmail_client import GmailClient, GmailClientError
from .ipc_server import IPCServer
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
    if os.path.isabs(path):
        return path
    return os.path.join(PROJECT_ROOT, path)


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
            connector = CalendarConnector(client)
            connector.my_email = email
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

    # Slack
    if enabled("slack"):
        try:
            slack_org = org_config.get("slack") or {}
            if not slack_org.get("client_id"):
                raise SlackClientError("Slack organization config not installed")
            token = load_slack_token(_resolve_path(TOKEN_FILES["slack"]))
            client = SlackClient(user_token=token.get("access_token", ""))
            workspace = client.check_connection()
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
            )
            logger.info("Telegram connector registered (will connect on first use)")
            connectors.append(TelegramConnector(tg_client))
        except (TelegramClientError, FileNotFoundError, Exception) as exc:
            logger.warning("Telegram connector disabled: %s", exc)

    return connectors


# ---------------------------------------------------------------------------- #
# IPC server thread
# ---------------------------------------------------------------------------- #

class IPCServerThread(threading.Thread):
    """Runs the asyncio IPC server on a dedicated daemon thread."""

    def __init__(self, server: IPCServer) -> None:
        super().__init__(name="ipc-server", daemon=True)
        self._server = server
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()

    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception as exc:
            logger.error("IPC server thread crashed: %s", exc, exc_info=True)

    async def _main(self) -> None:
        await self._server.start()
        self._ready.set()
        await asyncio.get_running_loop().create_future()


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
    if migration_summary:
        try:
            with open(_resolve_path(config_path), "w", encoding="utf-8") as fh:
                yaml.dump(config, fh, default_flow_style=False, allow_unicode=True)
            logger.info(
                "Auto-accept config migrated to connector-scoped grants:\n  %s",
                "\n  ".join(migration_summary),
            )
        except OSError as exc:
            logger.warning("Could not persist auto-accept grants migration: %s", exc)

    reload_rules(build_effective_rules(config))
    pii_config = config.get("pii_detection", {}) or {}
    init_pii_detection(
        pii_config.get("enabled", True),
        detect_ip_addresses=pii_config.get("detect_ip_addresses", True),
        detect_financial_figures=pii_config.get("detect_financial_figures", True),
    )
    init_privacy_filter(config)

    audit_logger = init_audit_logger(str(Path(data_dir()) / "logs" / "audit"))
    audit_logger.export_all_pending()

    org_config = load_org_config()
    connectors = build_connectors(config, org_config)
    if not connectors:
        logger.warning("No connectors could be initialized; daemon still starting for IPC.")

    unattended_enabled = bool((org_config.get("unattended_sessions", {}) or {}).get("enabled", False))
    ipc_server = IPCServer(connectors, unattended_sessions_enabled=unattended_enabled)
    ipc_thread = IPCServerThread(ipc_server)
    ipc_thread.start()
    ipc_thread._ready.wait(timeout=5)
    logger.info("IPC server ready, starting menu bar")

    from .menu_bar import run_menu_bar
    connector_names = [c.name for c in connectors]
    try:
        run_menu_bar(
            config_path=config_path,
            connectors=connector_names,
            ipc_server=ipc_server,
            connector_objs=connectors,
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
    parser.add_argument("--slack-oauth", action="store_true")
    parser.add_argument("--salesforce-oauth", action="store_true")
    parser.add_argument("--atlassian-oauth", action="store_true")
    parser.add_argument("--telegram-setup", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    oauth_flag = (
        args.gmail_oauth or args.drive_oauth or args.contacts_oauth
        or args.calendar_oauth or args.tasks_oauth or args.slack_oauth
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
