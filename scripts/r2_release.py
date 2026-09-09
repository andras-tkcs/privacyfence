#!/usr/bin/env python3
"""Publish release artifacts to the Cloudflare R2 release archive.

Replaces (and was rehearsed by) the one-off ".github/workflows/r2-smoke-test.yml" workflow --
that workflow proved the GitHub Actions -> R2 credentials/endpoint plumbing works and has been
deleted now that this script exists.

Every tagged release (stable and pre-release alike) uploads its artifacts here, laid out as:

    releases/
      stable/<version>/...
      beta/<version>/...
      alpha/<version>/...
      rc/<version>/...

"<version>" is __version__ as setuptools_scm resolves it from the git tag (see this repo's
CLAUDE.md "Releasing" section), e.g. "4.1.0" or "4.2.0b1" -- never the "v"-prefixed tag name
itself. The channel directory is derived from that version's PEP 440 pre-release suffix (a/b/rc,
same short spellings src/privacyfence/update_checker.py's _STAGE_RANK already uses to rank
update-check results); a version with no suffix is "stable".

This bucket is where alpha/beta/rc artifacts live *instead of* going anywhere public: unlike
stable, which also reaches PyPI and a public GitHub Release, pre-release tags stop at R2 --
see .github/workflows/publish-pypi.yml and build.yml for exactly which channels reach which public
index. The bucket itself is left at Cloudflare R2's default (private, no public bucket policy or
custom domain) -- distributing an alpha/beta download link to testers is a separate, not-yet-
decided step, deliberately not automated by this script.

Stdlib + boto3 only (`pip install boto3`) -- no PrivacyFence install required, matching the other
release-time scripts in this directory (scripts/build_org_bundle.py, scripts/sync_room_directory.py).
This script deliberately doesn't import the ``privacyfence`` package for the same reason: it
carries its own copy of the version-parsing regex, mirroring update_checker.py's rather than
importing it, so a CI job can run it (e.g. the SBOM job in build.yml) without first installing the
full package.

Requires these environment variables (matching r2-smoke-test.yml's, before its deletion):
    CF_R2_ACCESS_KEY_ID, CF_R2_SECRET_ACCESS_KEY  -- R2 API token
    CF_R2_ENDPOINT                                -- R2 S3-compatible endpoint URL
    R2_BUCKET                                     -- defaults to "privacyfence-releases"

Examples:
    python3 scripts/r2_release.py channel 4.2.0b1
        -> prints "beta"

    python3 scripts/r2_release.py upload --version 4.1.0 \\
        dist/PrivacyFence-4.1.0.dmg scripts/build_org_bundle.py scripts/sync_room_directory.py
        -> uploads each to releases/stable/4.1.0/<basename> in R2
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Mirrors src/privacyfence/update_checker.py's _VERSION_RE / _STAGE_RANK -- see this module's
# docstring for why this is a deliberate copy rather than an import. Unlike that module (which
# also needs to parse a leading "v" and a trailing ".dev<n>+local" dev-build segment, for comparing
# against a running dev build's own __version__), this script only ever sees a real resolved
# version string, so the "v" and ".dev" pieces are still matched here -- just to reject them below
# with a clear error, not to fall back to anything.
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?(?:\.dev(\d+))?(?:\+.*)?$")
_STAGE_TO_CHANNEL = {"a": "alpha", "b": "beta", "rc": "rc"}

DEFAULT_BUCKET = "privacyfence-releases"


def channel_for_version(version: str) -> str:
    """Returns "stable", "beta", "alpha", or "rc" for a resolved version string like "4.1.0" or
    "4.2.0b1". Raises ValueError for anything that isn't a real tagged release -- most importantly
    a between-tags dev build (e.g. "4.2.1.dev3+gabc1234"), which was never `git tag`d and has
    nothing to publish."""
    match = _VERSION_RE.match(version.strip())
    if not match:
        raise ValueError(f"{version!r} doesn't look like a release version (major.minor.patch[a|b|rc<n>])")
    if match.group(6) is not None:
        raise ValueError(
            f"{version!r} is a between-tags dev build, not a tagged release -- nothing to publish "
            "(run this against a commit that's actually tagged)"
        )
    stage = match.group(4)
    return _STAGE_TO_CHANNEL[stage] if stage else "stable"


def _r2_client():
    import boto3

    missing = [
        name
        for name in ("CF_R2_ACCESS_KEY_ID", "CF_R2_SECRET_ACCESS_KEY", "CF_R2_ENDPOINT")
        if not os.environ.get(name)
    ]
    if missing:
        raise SystemExit(f"Missing required environment variable(s): {', '.join(missing)}")

    return boto3.client(
        "s3",
        endpoint_url=os.environ["CF_R2_ENDPOINT"],
        aws_access_key_id=os.environ["CF_R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["CF_R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def upload(version: str, files: list[str], *, bucket: str | None = None) -> None:
    channel = channel_for_version(version)
    bucket = bucket or os.environ.get("R2_BUCKET", DEFAULT_BUCKET)
    s3 = _r2_client()

    for file_arg in files:
        path = Path(file_arg)
        if not path.is_file():
            raise SystemExit(f"Not a file: {path}")

        key = f"releases/{channel}/{version}/{path.name}"
        s3.upload_file(str(path), bucket, key)
        result = s3.head_object(Bucket=bucket, Key=key)
        print(f"uploaded s3://{bucket}/{key} ({result['ContentLength']} bytes)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    channel_parser = subparsers.add_parser("channel", help="Print the release channel for a version string")
    channel_parser.add_argument("version")

    upload_parser = subparsers.add_parser("upload", help="Upload files to the R2 release archive")
    upload_parser.add_argument("--version", required=True, help="Resolved release version, e.g. 4.2.0b1")
    upload_parser.add_argument("--bucket", default=None, help=f"R2 bucket (default: {DEFAULT_BUCKET})")
    upload_parser.add_argument("files", nargs="+", help="Local files to upload")

    args = parser.parse_args(argv)

    if args.command == "channel":
        try:
            print(channel_for_version(args.version))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.command == "upload":
        try:
            upload(args.version, args.files, bucket=args.bucket)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    return 1  # pragma: no cover -- unreachable, argparse enforces `required=True` above


if __name__ == "__main__":
    sys.exit(main())
