#!/usr/bin/env python3
"""Sync the Google Workspace room/resource directory into org_config.json.

Unlike scripts/build_org_bundle.py, this is deliberately *not* the same
Google Cloud project as the one your organization's everyday Gmail/Drive/
Calendar/Contacts/Tasks OAuth client uses. It's driven by a second project
whose OAuth client requests only the Admin SDK's
`admin.directory.resource.calendar.readonly` scope — a Workspace-admin-level
scope that has no business being on the OAuth token every employee carries
day to day just so `calendar_list_rooms` can work for the minority who book
rooms. See "Room directory sync" in docs/google-cloud-setup.md for how to set
that second project up.

Run this once to seed the room directory, and again whenever your
organization's rooms change. It merges into an existing org_config.json (its
"google"/"slack"/"salesforce"/"atlassian" sections are left untouched) and
only touches the "rooms" and "rooms_synced_at" keys. Distribute the
resulting org_config.json exactly as you already do today — the room data in
it is plain metadata (name, email, building, floor, capacity), not a
credential.

The admin client_secret.json you pass in, and the --token-file this script
caches its own OAuth token in, are NOT part of the bundle and must never be
distributed alongside it — keep them private to whoever runs this script.

Needs PrivacyFence's dependencies installed (unlike build_org_bundle.py, this
performs a live OAuth handshake + API call, so it can't be stdlib-only):

    .venv/bin/python scripts/sync_room_directory.py \\
        --admin-client-secret ~/Downloads/room_sync_client_secret.json \\
        --org-config org_config.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from privacyfence.room_directory_client import (  # noqa: E402
    RoomDirectoryClient,
    RoomDirectoryClientError,
)


def _load_admin_client_secret(path: str) -> dict[str, Any]:
    """Extract the inner "installed"/"web" block from Google's client_secret.json.

    Same shape PrivacyFence stores flat for the main "google" org bundle
    section (see build_org_bundle.py::_load_google_client_secret) — this is
    a separate Google Cloud project's Desktop app client, so it gets its own
    copy of this helper rather than importing across scripts.
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    inner = data.get("installed") or data.get("web")
    if not inner:
        raise SystemExit(
            f"{path} doesn't look like a Google OAuth client_secret.json "
            '(expected a top-level "installed" or "web" key). Download it from '
            "the *room sync* Google Cloud project's Credentials page, for an "
            "OAuth client of type 'Desktop app'."
        )
    return inner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync the Google Workspace room directory into org_config.json.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--admin-client-secret", required=True, metavar="PATH",
        help="Path to the client_secret.json from the separate, admin-scoped Google Cloud "
             "project (OAuth client of type 'Desktop app').",
    )
    parser.add_argument(
        "--org-config", default="org_config.json", metavar="PATH",
        help="Bundle to merge the room directory into (default: org_config.json in the "
             "current directory). Created fresh if it doesn't exist yet.",
    )
    parser.add_argument(
        "--token-file", default=".room_sync_token.json", metavar="PATH",
        help="Where this script caches its own OAuth token between runs (default: "
             ".room_sync_token.json). Keep this private -- never distribute it, and never "
             "add it to the org config bundle.",
    )
    parser.add_argument(
        "--query", default="", metavar="STR",
        help="Optional Directory API query to filter which rooms are fetched "
             "(same syntax Google's Admin SDK accepts for resources().calendars().list).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    client_config = {"installed": _load_admin_client_secret(args.admin_client_secret)}
    client = RoomDirectoryClient(client_config=client_config, token_file=args.token_file)

    try:
        if not os.path.exists(args.token_file):
            client.authorize_interactive()
        rooms = client.list_rooms(query=args.query)
    except RoomDirectoryClientError as exc:
        print(f"Room directory sync failed: {exc}", file=sys.stderr)
        return 1

    out_path = Path(args.org_config)
    bundle: dict[str, Any] = {}
    if out_path.exists():
        with open(out_path, encoding="utf-8") as fh:
            bundle = json.load(fh)
    bundle.setdefault("version", 1)

    bundle["rooms"] = [
        {k: v for k, v in asdict(room).items() if k != "resource_id"}
        for room in rooms
    ]
    bundle["rooms_synced_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    out_path.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out_path} with {len(rooms)} room(s).")
    print(
        'Distribute the updated org_config.json to your users as usual (via "Install/Update '
        'Organization Config…" in the PrivacyFence menu bar). Do NOT distribute '
        f"{args.admin_client_secret} or {args.token_file} — those are yours to keep private."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
