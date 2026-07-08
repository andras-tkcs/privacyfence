#!/usr/bin/env python3
"""Headless lookup of QA fixture IDs — Google Tasks list IDs and Telegram
chat IDs — for docs/qa-environment-setup.md, without needing a live Claude
session just to call telegram_list_chats / tasks_list_task_lists yourself.

Reuses the same connector construction and stored credentials the daemon
uses (privacyfence.daemon_main.build_connectors), so connectors must already
be authenticated via the menu bar (or `privacyfence-app --tasks-oauth` /
`--telegram-setup`) before this will return anything for them.

Caution: Telethon's session file allows only one active connection at a
time. Don't run this while a dev daemon (scripts/dev_start.sh /
privacyfence-app) already has Telegram connected — stop it first if you hit
a "database is locked" error.

Usage:
    .venv/bin/python scripts/qa_list_ids.py tasks
    .venv/bin/python scripts/qa_list_ids.py telegram
    .venv/bin/python scripts/qa_list_ids.py           # both
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from privacyfence.daemon_main import (  # noqa: E402
    PROJECT_ROOT,
    build_connectors,
    load_config,
    load_org_config,
)


async def _list(connectors: list, connector_name: str, tool: str, args: dict) -> list[dict]:
    connector = next((c for c in connectors if c.name == connector_name), None)
    if connector is None:
        print(
            f"  [{connector_name}] connector not available — check it's enabled, "
            "org config installed, and authenticated (menu bar → Connectors).",
            file=sys.stderr,
        )
        return []
    return await connector.call(tool, args)


def _print_rows(rows: list[dict], columns: list[tuple[str, str]]) -> None:
    if not rows:
        print("  (none found)")
        return
    for row in rows:
        print("  " + "  ".join(f"{label}={row.get(key)!r}" for key, label in columns))


async def main_async(which: str) -> int:
    config = load_config(os.path.join(PROJECT_ROOT, "config", "settings.yaml"))
    org_config = load_org_config()
    connectors = build_connectors(config, org_config)

    if which in ("tasks", "all"):
        print("Google Tasks — task lists (use the id for approved_task_list):")
        rows = await _list(connectors, "tasks", "tasks_list_task_lists", {})
        _print_rows(rows, [("id", "id"), ("title", "title")])
        print()

    if which in ("telegram", "all"):
        print("Telegram — chats (use the id for approved_chats):")
        rows = await _list(connectors, "telegram", "telegram_list_chats", {"limit": 100})
        _print_rows(rows, [("id", "chat_id"), ("name", "name"), ("type", "type"), ("is_self", "is_self")])
        print()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("which", choices=["tasks", "telegram", "all"], nargs="?", default="all")
    args = parser.parse_args()
    return asyncio.run(main_async(args.which))


if __name__ == "__main__":
    raise SystemExit(main())
