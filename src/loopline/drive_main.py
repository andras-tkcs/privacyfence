"""Entry point for the Google Drive privacy proxy.

Mirror of ``main.py`` but wires together the Drive client, Drive privacy
filter, Drive MCP server and the shared menu bar.

Threading model (identical to the Gmail proxy):
  - The MCP server runs the stdio transport inside its own asyncio event loop on
    a daemon background thread.
  - The rumps menu bar app runs on the main thread (a hard requirement for any
    AppKit/Cocoa UI on macOS).
  - The ReviewQueue bridges the two via loop.call_soon_threadsafe.

IMPORTANT: when the MCP server uses the stdio transport, stdin/stdout are the
protocol channel and must not be polluted. We therefore send all logs to a file
(and stderr), never to stdout.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from typing import Any

import yaml

from .drive_client import DriveClient, DriveClientError
from .drive_mcp_server import DriveGuardServer
from .floating_window import GuardFloatingWindow
from .privacy_filter import DrivePrivacyFilter

logger = logging.getLogger("loopline")

PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
)


# ---------------------------------------------------------------------------- #
# Configuration & logging
# ---------------------------------------------------------------------------- #
def _resolve_path(path: str) -> str:
    """Resolve a possibly-relative config path against the project root."""
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
    with open(resolved, "r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config, dict):
        raise ValueError(f"Configuration file {resolved} did not parse to a mapping")
    return config


def setup_logging(config: dict[str, Any]) -> None:
    """Configure file + stderr logging. Never logs to stdout (stdio transport)."""
    log_cfg = config.get("logging", {}) or {}
    level_name = str(log_cfg.get("level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    log_file = _resolve_path(log_cfg.get("file", "logs/loopline.log"))

    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)

    # stderr is safe; stdout is reserved for the MCP protocol.
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(stderr_handler)

    logger.info("Logging initialized at level %s -> %s", level_name, log_file)


# ---------------------------------------------------------------------------- #
# Component construction
# ---------------------------------------------------------------------------- #
def build_drive_client(config: dict[str, Any]) -> DriveClient:
    drive_cfg = config.get("drive", {}) or {}
    credentials_file = _resolve_path(
        drive_cfg.get("credentials_file", "credentials/client_secret.json")
    )
    token_file = _resolve_path(
        drive_cfg.get("token_file", "credentials/drive_token.json")
    )
    return DriveClient(credentials_file=credentials_file, token_file=token_file)


def build_drive_privacy_filter(config: dict[str, Any]) -> DrivePrivacyFilter:
    return DrivePrivacyFilter(config.get("drive_privacy", {}) or {})


# ---------------------------------------------------------------------------- #
# MCP server thread
# ---------------------------------------------------------------------------- #
class DriveMCPServerThread(threading.Thread):
    """Runs the FastMCP stdio server inside its own asyncio event loop."""

    def __init__(self, server: DriveGuardServer) -> None:
        super().__init__(name="drive-mcp-server", daemon=True)
        self._server = server

    def run(self) -> None:
        try:
            # FastMCP.run manages its own loop; running it on this thread keeps
            # the event loop off the main (UI) thread.
            self._server.run_stdio()
        except Exception as exc:  # noqa: BLE001
            logger.error("Drive MCP server thread crashed: %s", exc, exc_info=True)


# ---------------------------------------------------------------------------- #
# Commands
# ---------------------------------------------------------------------------- #
def run_oauth_setup(config: dict[str, Any]) -> int:
    """Interactive OAuth authorization. Saves token then exits."""
    client = build_drive_client(config)
    try:
        client.authorize_interactive()
        email = client.check_connection()
    except DriveClientError as exc:
        logger.error("OAuth setup failed: %s", exc)
        print(f"OAuth setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"OAuth setup complete. Authorized as: {email}")
    return 0


def run_app(config: dict[str, Any]) -> int:
    """Start the MCP server thread and the menu bar app (blocks until quit)."""
    drive_client = build_drive_client(config)
    privacy_filter = build_drive_privacy_filter(config)

    # Verify credentials up front so a misconfigured token fails loudly here
    # rather than on the first Claude request.
    try:
        email = drive_client.check_connection()
        logger.info("Drive credentials verified for %s", email)
    except DriveClientError as exc:
        logger.error("Cannot start: %s", exc)
        print(f"Cannot start: {exc}", file=sys.stderr)
        return 1

    mcp_cfg = config.get("drive_mcp", {}) or {}
    server = DriveGuardServer(
        drive_client=drive_client,
        privacy_filter=privacy_filter,
        server_name=mcp_cfg.get("server_name", "drive-guard"),
        server_version=mcp_cfg.get("server_version", "0.1.0"),
    )

    server_thread = DriveMCPServerThread(server)
    server_thread.start()
    logger.info("Drive MCP server thread started")

    def _on_quit() -> None:
        logger.info("Menu bar quit handler invoked; process will exit")

    app = GuardFloatingWindow(privacy_filter=privacy_filter, on_quit=_on_quit, app_name="Drive Guard")
    logger.info("Starting floating window on main thread")
    try:
        app.run()
    except KeyboardInterrupt:
        logger.info("Interrupted; shutting down")
    return 0


# ---------------------------------------------------------------------------- #
# Argument parsing / dispatch
# ---------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="loopline-drive",
        description="macOS menu bar privacy proxy between Claude (MCP) and Google Drive.",
    )
    parser.add_argument(
        "--config",
        default="config/settings.yaml",
        help="Path to the YAML config file (default: config/settings.yaml)",
    )
    parser.add_argument(
        "--oauth-setup",
        action="store_true",
        help="Run the interactive Drive OAuth flow, save the token, then exit.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    setup_logging(config)

    try:
        if args.oauth_setup:
            return run_oauth_setup(config)
        return run_app(config)
    except Exception as exc:  # noqa: BLE001 - top-level safety net
        logger.error("Fatal error: %s", exc, exc_info=True)
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
