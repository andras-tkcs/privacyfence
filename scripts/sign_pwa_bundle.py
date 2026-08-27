#!/usr/bin/env python3
"""Sign a mobile-approval-pwa/ release (issue #55, Phase 3).

Computes a SHA-256 hash of every static file in the bundle, builds a
manifest, and signs it with the organization's PWA release private key
(see scripts/generate_pwa_release_key.py). Run this every time you publish
a new build of mobile-approval-pwa/ to wherever your organization hosts it.

Output (written into the bundle directory, served alongside everything
else): bundle_manifest.json (the file-hash manifest plus version/signed_at
metadata) and bundle_manifest.sig (the raw ECDSA-P256-SHA256 signature over
the manifest's exact JSON bytes, base64). mobile-approval-pwa/js/
release_verify.js is what a paired phone uses to check both before trusting
an update -- see that file's own docstring for the full trust model this
plugs into, including why "signed" alone isn't enough without the pinning
step happening at pairing time.

Usage:
    python3 scripts/sign_pwa_bundle.py --release-key pwa_release_key.json \
        --bundle-dir mobile-approval-pwa --bundle-version 2026.08.27
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, load_der_private_key

# Files that are themselves part of the trust mechanism, or aren't meant to
# be integrity-checked content (this script's own output, anything under
# tests/, and index.html -- the entry point a browser navigates to directly
# can't be SRI/manifest-verified before it's already loaded and running, so
# it's out of scope for this manifest; see release_verify.js's docstring for
# why that's an accepted, documented limitation rather than a gap).
EXCLUDED_NAMES = {"bundle_manifest.json", "bundle_manifest.sig", "index.html"}
EXCLUDED_DIRS = {"tests", ".git"}


def _iter_bundle_files(bundle_dir: Path):
    for path in sorted(bundle_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.name in EXCLUDED_NAMES:
            continue
        if any(part in EXCLUDED_DIRS for part in path.relative_to(bundle_dir).parts):
            continue
        yield path


def build_manifest(bundle_dir: Path, version: str) -> dict:
    files = {}
    for path in _iter_bundle_files(bundle_dir):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files[str(path.relative_to(bundle_dir))] = digest
    return {
        "version": version,
        "signed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": files,
    }


def sign_manifest(manifest_bytes: bytes, private_key_der: bytes) -> bytes:
    """Returns a raw (r||s, 64-byte) ECDSA-P256-SHA256 signature -- WebCrypto's
    `crypto.subtle.verify` expects raw, not the DER encoding `cryptography`
    produces by default (see release_verify.js)."""
    private_key = load_der_private_key(private_key_der, password=None)
    der_signature = private_key.sign(manifest_bytes, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_signature)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release-key", required=True, help="Path to the JSON file generate_pwa_release_key.py wrote.")
    parser.add_argument("--bundle-dir", default="mobile-approval-pwa", help="Directory to sign (default: mobile-approval-pwa).")
    parser.add_argument("--bundle-version", required=True, help="A version string for this release, e.g. 2026.08.27 or a git SHA.")
    args = parser.parse_args(argv)

    release_key_path = Path(args.release_key)
    bundle_dir = Path(args.bundle_dir)
    if not bundle_dir.is_dir():
        print(f"{bundle_dir} is not a directory.", file=sys.stderr)
        return 1

    try:
        release_key = json.loads(release_key_path.read_text(encoding="utf-8"))
        private_key_der = base64.b64decode(release_key["private_key_der_base64"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        print(f"Could not read a release key from {release_key_path}: {exc}", file=sys.stderr)
        return 1

    manifest = build_manifest(bundle_dir, args.bundle_version)
    # Canonical, stable serialization -- the exact bytes signed must be the
    # exact bytes verified; sort_keys keeps this reproducible across
    # Python's own dict-ordering quirks, and this is also, byte for byte,
    # what's written to bundle_manifest.json below and what release_verify.js
    # must re-serialize identically before checking the signature.
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = sign_manifest(manifest_bytes, private_key_der)

    (bundle_dir / "bundle_manifest.json").write_bytes(manifest_bytes)
    (bundle_dir / "bundle_manifest.sig").write_text(base64.b64encode(signature).decode("ascii"), encoding="utf-8")

    print(f"Signed {len(manifest['files'])} file(s) in {bundle_dir} as version {args.bundle_version}.")
    for name in manifest["files"]:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
