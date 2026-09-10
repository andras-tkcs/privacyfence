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

Like build_org_bundle.py, this script deliberately doesn't import the
`privacyfence` package, so it's runnable standalone (e.g. copied out of the
repo to wherever IT actually runs it) without a full PrivacyFence install.
It carries its own copy of the pieces it needs from
src/privacyfence/room_directory_client.py, src/privacyfence/calendar_client.py
(just the CalendarRoom field shape) and src/privacyfence/secure_files.py
(just the atomic-write helper) -- room_directory_client.py itself was
retired once this became the module's only caller. Unlike
build_org_bundle.py this DOES perform a live OAuth handshake + Admin SDK
call, so it isn't stdlib-only: it needs the same three Google client
libraries PrivacyFence itself depends on (see pyproject.toml's
[project.dependencies]):

    pip install google-auth google-auth-oauthlib google-api-python-client

SEC-05 full signing (org_bundle_signing.py): merging "rooms"/"rooms_synced_at"
into the bundle changes what a previous --sign-key signature covers, so any
"signature"/"signing_public_key" already on the file is stale the moment
this script writes to it -- see org_bundle_signing.sign_bundle's own
docstring ("Any change to the bundle after this invalidates the signature,
by design"). Leaving that stale signature in place would be worse than
dropping it: every install that already pinned your org's signing key (or
mode: org, which requires one) would then refuse to start on the very next
load, with no obvious cause. So: if the bundle you're merging into already
carries a signature, --sign-key <path to the SAME Ed25519 private key
build_org_bundle.py signed it with> is required, or this script refuses to
write anything at all. Needs the `cryptography` package (pip install
cryptography) specifically, same as build_org_bundle.py's --sign-key.

Example:

    python3 scripts/sync_room_directory.py \\
        --admin-client-secret ~/Downloads/room_sync_client_secret.json \\
        --org-config org_config.json
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import secrets
import sys
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/admin.directory.resource.calendar.readonly"]


def _canonical_payload_bytes(bundle: dict[str, Any]) -> bytes:
    """Byte-for-byte copy of build_org_bundle.py's own
    ``_canonical_payload_bytes`` (which itself mirrors src/privacyfence/
    org_bundle_signing.py's -- see that module's docstring on why all three
    MUST stay identical: a bundle re-signed here must still verify against
    the daemon's own org_bundle_signing.verify_and_maybe_pin()). Not
    imported from either for the same standalone-script reason as
    everything else in this file.
    """
    payload = {k: v for k, v in bundle.items() if k != "signature"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _require_cryptography():
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat,
            load_pem_private_key,
        )
    except ImportError as exc:
        raise SystemExit(
            "--sign-key needs the `cryptography` package: pip install cryptography"
        ) from exc
    return ed25519, Encoding, PublicFormat, load_pem_private_key


def _sign_bundle(bundle: dict[str, Any], sign_key_path: str) -> dict[str, Any]:
    ed25519, Encoding, PublicFormat, load_pem_private_key = _require_cryptography()
    with open(sign_key_path, "rb") as fh:
        private_key = load_pem_private_key(fh.read(), password=None)
    if not isinstance(private_key, ed25519.Ed25519PrivateKey):
        raise SystemExit(f"{sign_key_path} is not an Ed25519 private key.")
    signed = dict(bundle)
    signed.pop("signature", None)
    signed["signing_public_key"] = base64.b64encode(
        private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode("ascii")
    signature = private_key.sign(_canonical_payload_bytes(signed))
    signed["signature"] = base64.b64encode(signature).decode("ascii")
    return signed


def _atomic_write_text(path: str, text: str, *, mode: int = 0o600) -> None:
    """Write ``text`` to ``path`` atomically and with owner-only permissions
    from the moment the file exists.

    A minimal standalone copy of src/privacyfence/secure_files.py's
    ``atomic_write_text`` -- just the write-then-rename + chmod core, without
    that module's directory-permission-tightening (secure_mkdir); this
    script only ever writes into a directory the caller already controls
    (--token-file's parent), so that extra hardening isn't needed here to
    keep the OAuth token (the only sensitive file this script writes) off
    disk unprotected even momentarily.
    """
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.parent / f".{dest.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, dest)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best effort cleanup
            pass
        raise


@dataclass
class _Room:
    """Mirrors src/privacyfence/calendar_client.py's ``CalendarRoom`` field-
    for-field -- that's the shape CalendarConnector expects out of
    org_config.json's "rooms" list (see daemon_main.py). Not imported from
    there for the same standalone-script reason as everything else in this
    file; the two MUST stay in sync.
    """

    resource_id: str
    resource_name: str
    resource_email: str
    building_id: str
    floor_name: str
    capacity: int
    description: str


class RoomDirectoryClientError(Exception):
    """Raised for unrecoverable room-directory sync problems (auth, config, API)."""


class RoomDirectoryClient:
    """Admin SDK Directory client, read-only, rooms/resources only.

    A standalone copy of the retired src/privacyfence/room_directory_client.py
    -- see this script's module docstring for why it lives here now instead.
    """

    def __init__(self, client_config: dict, token_file: str) -> None:
        self._client_config = client_config
        self._token_file = token_file
        self._local = threading.local()
        self._creds_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Authentication
    # ------------------------------------------------------------------ #

    def authorize_interactive(self) -> None:
        """Run the interactive OAuth flow and persist the token.

        The signed-in Google account must hold Workspace admin / Directory
        Reader privilege — this is enforced by Google, not by PrivacyFence
        (see list_rooms()'s 403 handling below).
        """
        if not self._client_config:
            raise RoomDirectoryClientError(
                "No admin client config given. Pass --admin-client-secret to "
                "scripts/sync_room_directory.py."
            )
        logger.info("Starting Room Directory interactive OAuth flow")
        flow = InstalledAppFlow.from_client_config(self._client_config, SCOPES)
        creds = flow.run_local_server(port=0)
        self._save_token(creds)
        logger.info("Room Directory OAuth token saved to '%s'", self._token_file)

    def _load_credentials(self) -> Credentials:
        with self._creds_lock:
            if not os.path.exists(self._token_file):
                raise RoomDirectoryClientError(
                    f"No OAuth token found at '{self._token_file}'. Run "
                    "scripts/sync_room_directory.py to authorize."
                )
            creds = Credentials.from_authorized_user_file(self._token_file, SCOPES)
            if creds.valid:
                return creds
            if creds.expired and creds.refresh_token:
                logger.info("Refreshing expired Room Directory OAuth token")
                try:
                    creds.refresh(Request())
                except Exception as exc:
                    raise RoomDirectoryClientError(
                        f"Failed to refresh Room Directory OAuth token: {exc}. "
                        "Re-run scripts/sync_room_directory.py to re-authorize."
                    ) from exc
                self._save_token(creds)
                return creds
            raise RoomDirectoryClientError(
                "Cached Room Directory OAuth token is invalid. Re-run "
                "scripts/sync_room_directory.py."
            )

    def _save_token(self, creds: Credentials) -> None:
        _atomic_write_text(self._token_file, creds.to_json())

    def _get_service(self):
        service = getattr(self._local, "service", None)
        if service is None:
            creds = self._load_credentials()
            service = build("admin", "directory_v1", credentials=creds, cache_discovery=False)
            self._local.service = service
            logger.debug(
                "Room Directory API service initialized for thread %s",
                threading.current_thread().name,
            )
        return service

    # ------------------------------------------------------------------ #
    # Read operations
    # ------------------------------------------------------------------ #

    def list_rooms(self, query: str = "") -> list[_Room]:
        """List meeting rooms/resources from the Google Workspace directory."""
        try:
            kwargs: dict[str, Any] = {"customer": "my_customer", "maxResults": 500}
            if query:
                kwargs["query"] = query
            result = self._get_service().resources().calendars().list(**kwargs).execute()
        except HttpError as exc:
            if exc.resp.status == 403:
                raise RoomDirectoryClientError(
                    "Room directory listing requires Google Workspace admin access. "
                    "Sign in with an account that has the 'Directory Reader' role, "
                    "or ask your Workspace admin to grant it."
                ) from exc
            raise RoomDirectoryClientError(f"list_rooms failed: {exc}") from exc
        rooms = []
        for raw in result.get("items", []):
            rooms.append(_Room(
                resource_id=raw.get("resourceId", ""),
                resource_name=raw.get("resourceName", ""),
                resource_email=raw.get("resourceEmail", ""),
                building_id=raw.get("buildingId", ""),
                floor_name=raw.get("floorName", ""),
                capacity=int(raw.get("capacity", 0)),
                description=raw.get("generatedResourceName", raw.get("resourceDescription", "")),
            ))
        logger.info("list_rooms returned %d room(s)", len(rooms))
        return rooms


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
    parser.add_argument(
        "--sign-key", metavar="PATH",
        help="Re-sign the merged bundle with the Ed25519 private key at PATH -- the SAME key "
             "build_org_bundle.py --sign-key originally signed --org-config with (from "
             "build_org_bundle.py --generate-signing-key). Required if --org-config is already "
             "signed (this script's own merge invalidates the existing signature regardless -- "
             "see module docstring); optional otherwise. Needs the `cryptography` package.",
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

    was_signed = bool(bundle.get("signature")) or bool(bundle.get("signing_public_key"))
    if was_signed and not args.sign_key:
        print(
            f"{out_path} is already signed, but merging the room directory into it invalidates "
            "that signature -- every install that has pinned your org's signing key (and any "
            "mode: org install, which requires one) would refuse to start on the resulting file. "
            "Re-run with --sign-key <path to the same Ed25519 private key you originally signed "
            "it with> to re-sign after merging.",
            file=sys.stderr,
        )
        return 1
    # Any existing signature was over the bundle as it stood at its last
    # signing -- stale the moment "rooms"/"rooms_synced_at" below change
    # it, whether or not --sign-key re-signs it in this same run. Never
    # carry a signature/key forward implicitly (mirrors build_org_bundle.
    # py --merge's identical never-implicitly-reaffirm stance).
    bundle.pop("signature", None)
    bundle.pop("signing_public_key", None)

    bundle["rooms"] = [
        {k: v for k, v in asdict(room).items() if k != "resource_id"}
        for room in rooms
    ]
    bundle["rooms_synced_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if args.sign_key:
        bundle = _sign_bundle(bundle, args.sign_key)

    out_path.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out_path} with {len(rooms)} room(s), signed={'signature' in bundle}.")
    print(
        'Distribute the updated org_config.json to your users as usual (via "Install/Update '
        'Organization Config…" on the General page of PrivacyFence Settings). Do NOT distribute the '
        "--admin-client-secret file or --token-file you passed in — keep those private."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
