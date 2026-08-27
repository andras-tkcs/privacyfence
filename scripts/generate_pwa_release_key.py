#!/usr/bin/env python3
"""Generate the ECDSA P-256 keypair used to sign mobile-approval-pwa/ releases
(issue #55, Phase 3's "signed/pinned bundle-release mechanism").

Run this ONCE per organization, offline, on a machine you trust -- this is a
real code-signing key. The private key it writes must NEVER be committed to
this repository or distributed alongside the PWA bundle it signs: anyone who
holds it can sign a malicious bundle that every paired phone will accept as
genuine. Keep it in a password manager, an HSM, or an offline machine --
whatever your organization already uses for release-signing keys.

The public key is the opposite: it's meant to be distributed. It's pinned
into a phone's trust store at pairing time (see mobile_relay_pairing.py's
PairingSession.pwa_release_public_key and org_config.json's
"pwa_release_public_key_base64") and re-verified on every bundle update --
see mobile-approval-pwa/js/release_verify.js's own docstring for exactly
what that buys.

P-256 ECDSA, not the X25519/Ed25519 the rest of issue #55's crypto uses:
this key is verified in a browser Service Worker via `crypto.subtle`, which
has reliable native ECDSA-P256 support in every target browser (including
Safari/WebKit) -- unlike X25519, which needed a hand-rolled implementation
(see mobile-approval-pwa/js/x25519.js's own docstring). There's no
requirement that this key use the same curve as the daemon-phone pairing
keys; it's an unrelated signing operation with its own keypair.

Usage:
    python3 scripts/generate_pwa_release_key.py -o release_key.json
    # -> distribute release_key.json's "public_key_base64" via
    #    scripts/build_org_bundle.py --pwa-release-public-key
    # -> keep the file itself (which also holds the private key) offline
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)


def generate() -> dict[str, str]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_bytes = private_key.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption())
    public_bytes = private_key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return {
        "private_key_der_base64": base64.b64encode(private_bytes).decode("ascii"),
        "public_key_base64": base64.b64encode(public_bytes).decode("ascii"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--output", default="pwa_release_key.json", help="Output path.")
    args = parser.parse_args(argv)

    out_path = Path(args.output)
    if out_path.exists():
        print(f"{out_path} already exists -- refusing to overwrite a release key.", file=sys.stderr)
        return 1

    keys = generate()
    out_path.write_text(json.dumps(keys, indent=2) + "\n", encoding="utf-8")
    try:
        out_path.chmod(0o600)
    except OSError:  # pragma: no cover - best effort on non-POSIX
        pass

    print(f"Wrote {out_path} (mode 0600).")
    print(f"Public key (safe to distribute): {keys['public_key_base64']}")
    print(
        "Keep this file itself -- including the private key -- offline and out of version control. "
        "Pass its public_key_base64 to scripts/build_org_bundle.py --pwa-release-public-key, and this "
        "file's path to scripts/sign_pwa_bundle.py --release-key each time you sign a new PWA release."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
