"""Loopline daemon: persistent macOS app that owns the UI, credentials, and connectors.

Started at login via LaunchAgent (com.loopline.app.plist), or automatically
by the bridge on first use. Only one instance is allowed (enforced via a lock
file). The bridge connects to this process over a Unix socket.

Threading model:
  - Main thread:   tkinter floating window (hard macOS requirement).
  - IPC thread:    asyncio event loop serving the bridge socket connection.
  - ReviewQueue:   bridges the two via loop.call_soon_threadsafe.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import logging
import os
import sys
import threading
from typing import Any

import yaml

from .paths import data_dir, is_bundled
from .connectors.calendar import CalendarConnector
from .connectors.contacts import ContactsConnector
from .connectors.drive import DriveConnector
from .connectors.gmail import GmailConnector
from .connectors.slack import SlackConnector
from .connectors.tasks import TasksConnector
from .connectors.telegram import TelegramConnector
from .calendar_client import CalendarClient, CalendarClientError
from .contacts_client import ContactsClient, ContactsClientError
from .drive_client import DriveClient, DriveClientError
from .floating_window import GuardFloatingWindow
from .gmail_client import GmailClient, GmailClientError
from .ipc_server import IPCServer
from .privacy_filter import DrivePrivacyFilter, PrivacyFilter, SlackPrivacyFilter
from .slack_client import SlackClient, SlackClientError
from .tasks_client import TasksClient, TasksClientError
from .telegram_client import TelegramClientError, TelegramLooplineClient

logger = logging.getLogger("loopline.daemon")

PROJECT_ROOT = str(data_dir())
LOCK_FILE = os.path.join(PROJECT_ROOT, "loopline.lock")
# Written by the setup wizard on first-run completion.
SETUP_SENTINEL = os.path.join(PROJECT_ROOT, "setup_complete")

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


def load_config(config_path: str) -> dict[str, Any]:
    resolved = _resolve_path(config_path)
    if not os.path.exists(resolved):
        raise FileNotFoundError(
            f"Configuration file not found: {resolved}. "
            "Copy config/settings.yaml.example to config/settings.yaml."
        )
    with open(resolved, encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}
    if not isinstance(config, dict):
        raise ValueError(f"Config file {resolved} did not parse to a mapping")
    return config


def setup_logging(config: dict[str, Any]) -> None:
    log_cfg = config.get("logging", {}) or {}
    level_name = str(log_cfg.get("level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    log_file = _resolve_path(log_cfg.get("file", "logs/loopline.log"))
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    fmt = logging.Formatter("%(asctime)s %(levelname)-8s [%(name)s] %(message)s")
    handlers = [
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
# Connector construction (graceful: missing config → connector skipped)
# ---------------------------------------------------------------------------- #

def _build_connectors(config: dict[str, Any]) -> list:
    connectors = []

    # Gmail
    try:
        gmail_cfg = config.get("gmail", {}) or {}
        client = GmailClient(
            credentials_file=_resolve_path(gmail_cfg.get("credentials_file", "credentials/client_secret.json")),
            token_file=_resolve_path(gmail_cfg.get("token_file", "credentials/token.json")),
        )
        email = client.check_connection()
        logger.info("Gmail connector ready for %s", email)
        connectors.append(GmailConnector(client, PrivacyFilter(config.get("privacy", {}) or {})))
    except (GmailClientError, FileNotFoundError) as exc:
        logger.warning("Gmail connector disabled: %s", exc)

    # Drive
    try:
        drive_cfg = config.get("drive", {}) or {}
        client = DriveClient(
            credentials_file=_resolve_path(drive_cfg.get("credentials_file", "credentials/client_secret.json")),
            token_file=_resolve_path(drive_cfg.get("token_file", "credentials/drive_token.json")),
        )
        email = client.check_connection()
        logger.info("Drive connector ready for %s", email)
        connectors.append(DriveConnector(client, DrivePrivacyFilter(config.get("drive_privacy", {}) or {})))
    except (DriveClientError, FileNotFoundError) as exc:
        logger.warning("Drive connector disabled: %s", exc)

    # Slack
    try:
        slack_cfg = config.get("slack", {}) or {}
        bot_token = slack_cfg.get("bot_token", "")
        if not bot_token or bot_token.startswith("xoxb-your-"):
            raise SlackClientError("bot_token not configured")
        user_token = slack_cfg.get("user_token", "")
        client = SlackClient(bot_token=bot_token, user_token=user_token)
        workspace = client.check_connection()
        logger.info("Slack connector ready for workspace %r", workspace)
        connectors.append(SlackConnector(client, SlackPrivacyFilter(config.get("slack_privacy", {}) or {})))
    except (SlackClientError, FileNotFoundError) as exc:
        logger.warning("Slack connector disabled: %s", exc)

    # Contacts
    try:
        contacts_cfg = config.get("contacts", {}) or {}
        client = ContactsClient(
            credentials_file=_resolve_path(contacts_cfg.get("credentials_file", "credentials/client_secret.json")),
            token_file=_resolve_path(contacts_cfg.get("token_file", "credentials/contacts_token.json")),
        )
        email = client.check_connection()
        logger.info("Contacts connector ready for %s", email)
        connector = ContactsConnector(
            client,
            rules_config=(config.get("auto_accept_rules", {}) or {}),
        )
        connector.my_email = email
        connectors.append(connector)
    except (ContactsClientError, FileNotFoundError) as exc:
        logger.warning("Contacts connector disabled: %s", exc)

    # Calendar
    try:
        calendar_cfg = config.get("calendar", {}) or {}
        client = CalendarClient(
            credentials_file=_resolve_path(calendar_cfg.get("credentials_file", "credentials/client_secret.json")),
            token_file=_resolve_path(calendar_cfg.get("token_file", "credentials/calendar_token.json")),
        )
        email = client.check_connection()
        logger.info("Calendar connector ready for %s", email)
        connector = CalendarConnector(client)
        connector.my_email = email
        connectors.append(connector)
    except (CalendarClientError, FileNotFoundError) as exc:
        logger.warning("Calendar connector disabled: %s", exc)

    # Tasks
    try:
        tasks_cfg = config.get("tasks", {}) or {}
        client = TasksClient(
            credentials_file=_resolve_path(tasks_cfg.get("credentials_file", "credentials/client_secret.json")),
            token_file=_resolve_path(tasks_cfg.get("token_file", "credentials/tasks_token.json")),
        )
        email = client.check_connection()
        logger.info("Tasks connector ready for %s", email)
        connectors.append(TasksConnector(client))
    except (TasksClientError, FileNotFoundError) as exc:
        logger.warning("Tasks connector disabled: %s", exc)

    # Telegram
    try:
        tg_cfg = config.get("telegram", {}) or {}
        api_id = tg_cfg.get("api_id")
        api_hash = tg_cfg.get("api_hash", "")
        if not api_id or not api_hash:
            raise TelegramClientError("telegram.api_id and api_hash not configured")
        session_file = _resolve_path(tg_cfg.get("session_file", "credentials/telegram.session"))
        tg_client = TelegramLooplineClient(
            api_id=int(api_id),
            api_hash=api_hash,
            session_file=session_file,
        )
        import asyncio as _asyncio
        tg_name = _asyncio.run(tg_client.check_connection())
        logger.info("Telegram connector ready: %s", tg_name)
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
        except Exception as exc:  # noqa: BLE001
            logger.error("IPC server thread crashed: %s", exc, exc_info=True)

    async def _main(self) -> None:
        await self._server.start()
        self._ready.set()
        # Run forever until the loop is stopped.
        await asyncio.get_running_loop().create_future()


# ---------------------------------------------------------------------------- #
# OAuth setup commands
# ---------------------------------------------------------------------------- #

def run_gmail_oauth(config: dict[str, Any]) -> int:
    gmail_cfg = config.get("gmail", {}) or {}
    client = GmailClient(
        credentials_file=_resolve_path(gmail_cfg.get("credentials_file", "credentials/client_secret.json")),
        token_file=_resolve_path(gmail_cfg.get("token_file", "credentials/token.json")),
    )
    try:
        client.authorize_interactive()
        email = client.check_connection()
    except GmailClientError as exc:
        print(f"Gmail OAuth setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Gmail OAuth complete. Authorized as: {email}")
    return 0


def run_drive_oauth(config: dict[str, Any]) -> int:
    drive_cfg = config.get("drive", {}) or {}
    client = DriveClient(
        credentials_file=_resolve_path(drive_cfg.get("credentials_file", "credentials/client_secret.json")),
        token_file=_resolve_path(drive_cfg.get("token_file", "credentials/drive_token.json")),
    )
    try:
        client.authorize_interactive()
        email = client.check_connection()
    except DriveClientError as exc:
        print(f"Drive OAuth setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Drive OAuth complete. Authorized as: {email}")
    return 0


def run_contacts_oauth(config: dict[str, Any]) -> int:
    contacts_cfg = config.get("contacts", {}) or {}
    client = ContactsClient(
        credentials_file=_resolve_path(contacts_cfg.get("credentials_file", "credentials/client_secret.json")),
        token_file=_resolve_path(contacts_cfg.get("token_file", "credentials/contacts_token.json")),
    )
    try:
        client.authorize_interactive()
        email = client.check_connection()
    except ContactsClientError as exc:
        print(f"Contacts OAuth setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Contacts OAuth complete. Authorized as: {email}")
    return 0


def run_calendar_oauth(config: dict[str, Any]) -> int:
    calendar_cfg = config.get("calendar", {}) or {}
    client = CalendarClient(
        credentials_file=_resolve_path(calendar_cfg.get("credentials_file", "credentials/client_secret.json")),
        token_file=_resolve_path(calendar_cfg.get("token_file", "credentials/calendar_token.json")),
    )
    try:
        client.authorize_interactive()
        email = client.check_connection()
    except CalendarClientError as exc:
        print(f"Calendar OAuth setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Calendar OAuth complete. Authorized as: {email}")
    return 0


def run_tasks_oauth(config: dict[str, Any]) -> int:
    tasks_cfg = config.get("tasks", {}) or {}
    client = TasksClient(
        credentials_file=_resolve_path(tasks_cfg.get("credentials_file", "credentials/client_secret.json")),
        token_file=_resolve_path(tasks_cfg.get("token_file", "credentials/tasks_token.json")),
    )
    try:
        client.authorize_interactive()
        email = client.check_connection()
    except TasksClientError as exc:
        print(f"Tasks OAuth setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Tasks OAuth complete. Authorized as: {email}")
    return 0


def run_telegram_setup(config: dict[str, Any]) -> int:
    """Interactive Telegram phone+code authorization."""
    tg_cfg = config.get("telegram", {}) or {}
    api_id = tg_cfg.get("api_id")
    api_hash = tg_cfg.get("api_hash", "")
    if not api_id or not api_hash:
        print("Set telegram.api_id and telegram.api_hash in config/settings.yaml first.", file=sys.stderr)
        return 1
    session_file = _resolve_path(tg_cfg.get("session_file", "credentials/telegram.session"))
    client = TelegramLooplineClient(api_id=int(api_id), api_hash=api_hash, session_file=session_file)
    import asyncio
    asyncio.run(client.authorize_interactive())
    print(f"Telegram session saved to {session_file}")
    return 0


# ---------------------------------------------------------------------------- #
# Main app
# ---------------------------------------------------------------------------- #

def run_app(config: dict[str, Any]) -> int:
    if not _acquire_instance_lock():
        logger.error("Another instance is already running; exiting.")
        print("Loopline daemon is already running.", file=sys.stderr)
        return 1

    connectors = _build_connectors(config)
    if not connectors:
        logger.warning("No connectors could be initialized; daemon still starting for IPC.")

    ipc_server = IPCServer(connectors)
    ipc_thread = IPCServerThread(ipc_server)
    ipc_thread.start()
    # Wait for socket to be ready before the UI starts so the bridge can connect
    # the moment the daemon process is visible.
    ipc_thread._ready.wait(timeout=5)
    logger.info("IPC server ready, starting UI")

    # Pick any available filter for the UI (first connector's filter, or a blank one).
    if connectors:
        ui_filter = connectors[0]._filter  # type: ignore[attr-defined]
    else:
        ui_filter = PrivacyFilter({})

    def _on_quit() -> None:
        logger.info("Quit requested")

    app = GuardFloatingWindow(privacy_filter=ui_filter, on_quit=_on_quit)
    try:
        app.run()
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
        prog="loopline-app",
        description="Loopline daemon — privacy proxy UI and connector host.",
    )
    default_config = os.path.join(PROJECT_ROOT, "config", "settings.yaml")
    parser.add_argument("--config", default=default_config)
    parser.add_argument("--gmail-oauth", action="store_true", help="Run Gmail OAuth setup and exit.")
    parser.add_argument("--drive-oauth", action="store_true", help="Run Drive OAuth setup and exit.")
    parser.add_argument("--contacts-oauth", action="store_true", help="Run Contacts OAuth setup and exit.")
    parser.add_argument("--calendar-oauth", action="store_true", help="Run Calendar OAuth setup and exit.")
    parser.add_argument("--tasks-oauth", action="store_true", help="Run Tasks OAuth setup and exit.")
    parser.add_argument("--telegram-setup", action="store_true", help="Run Telegram interactive auth and exit.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # First-run: launch setup wizard instead of the normal app.
    oauth_flag = args.gmail_oauth or args.drive_oauth or args.contacts_oauth or args.calendar_oauth or args.tasks_oauth or args.telegram_setup
    if is_bundled() and not oauth_flag and not os.path.exists(SETUP_SENTINEL):
        from .setup_wizard import run_setup_wizard
        run_setup_wizard()
        return 0

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    setup_logging(config)

    try:
        if args.gmail_oauth:
            return run_gmail_oauth(config)
        if args.drive_oauth:
            return run_drive_oauth(config)
        if args.contacts_oauth:
            return run_contacts_oauth(config)
        if args.calendar_oauth:
            return run_calendar_oauth(config)
        if args.tasks_oauth:
            return run_tasks_oauth(config)
        if args.telegram_setup:
            return run_telegram_setup(config)
        return run_app(config)
    except Exception as exc:  # noqa: BLE001
        logger.error("Fatal error: %s", exc, exc_info=True)
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
