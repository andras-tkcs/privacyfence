"""Generate WireGuard-compatible X25519 keypairs without the `wg` CLI.

WireGuard keys are just raw 32-byte X25519 keys, base64-encoded -- there is nothing
WireGuard-specific about the math. `wg genkey`/`wg pubkey` aren't guaranteed to be
installed everywhere a keypair needs to be minted (e.g. scripted provisioning of the
throwaway relay box before WireGuard itself is installed), so this generates the same
keys `wg` would, using the `cryptography` package PrivacyFence already depends on.

Output is deliberately shaped like `wg genkey`/`wg pubkey`'s own output (bare
base64, one key per file) so it drops straight into `wg-quick` configs or the
`.conf.template` files in this directory.
"""

from __future__ import annotations

import argparse
import base64
import pathlib

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519


def generate_keypair() -> tuple[str, str]:
    """Return (private_key_b64, public_key_b64) for a fresh WireGuard peer."""
    private_key = x25519.X25519PrivateKey.generate()
    public_key = private_key.public_key()

    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return (
        base64.b64encode(private_bytes).decode("ascii"),
        base64.b64encode(public_bytes).decode("ascii"),
    )


def write_keypair(out_dir: pathlib.Path, name: str) -> tuple[str, str]:
    """Write `<name>.privatekey`/`<name>.publickey` into out_dir, mode 0600 for the private key."""
    private_b64, public_b64 = generate_keypair()
    private_path = out_dir / f"{name}.privatekey"
    public_path = out_dir / f"{name}.publickey"

    private_path.write_text(private_b64 + "\n")
    private_path.chmod(0o600)
    public_path.write_text(public_b64 + "\n")

    return private_b64, public_b64


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "names",
        nargs="+",
        help="One or more peer names to generate keys for, e.g. `relay mac-daemon phone-1`.",
    )
    parser.add_argument(
        "--out",
        type=pathlib.Path,
        default=pathlib.Path("keys"),
        help="Directory to write <name>.privatekey/<name>.publickey into (default: ./keys).",
    )
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    for name in args.names:
        private_b64, public_b64 = write_keypair(args.out, name)
        print(f"{name}: public key = {public_b64}")
    print(
        f"\nPrivate keys written to {args.out}/ with mode 0600 -- keep them off shared "
        "storage and out of git. Only the .publickey files need to be shared between peers."
    )


if __name__ == "__main__":
    main()
