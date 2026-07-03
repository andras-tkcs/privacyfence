#!/usr/bin/env python3
"""Build a PrivacyFence organization config bundle (org_config.json).

Run this once per organization after registering each cloud app (see the
"For IT admins" section of docs/google-cloud-setup.md, docs/slack-setup.md,
docs/salesforce-setup.md, and docs/atlassian-setup.md). The output file is
what you distribute to your users — they install it via "Install/Update
Organization Config…" in the PrivacyFence menu bar.

Telegram is not part of this bundle: its api_id/api_hash identify the
PrivacyFence app itself (not your organization) and are baked into the
release build — see docs/telegram-setup.md and src/privacyfence/app_credentials.py.

Only pass the flags for services you've set up; a connector is offered to
users only if its section is present in the bundle. Stdlib only — no
PrivacyFence install required to run this.

Example:
    python3 scripts/build_org_bundle.py \\
        --org-name "Acme Corp" \\
        --google-client-secret ~/Downloads/client_secret_....json \\
        --slack-client-id 1234.5678 --slack-client-secret abcdef \\
        --salesforce-consumer-key 3MVG9... --salesforce-consumer-secret abc \\
        --atlassian-client-id abc123 --atlassian-client-secret def456 \\
        -o org_config.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _load_google_client_secret(path: str) -> dict[str, Any]:
    """Extract the inner "installed"/"web" block from Google's client_secret.json.

    PrivacyFence stores it flat (no wrapper) in the bundle and re-wraps it
    when handing it to google-auth-oauthlib at authorize time.
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    inner = data.get("installed") or data.get("web")
    if not inner:
        raise SystemExit(
            f"{path} doesn't look like a Google OAuth client_secret.json "
            '(expected a top-level "installed" or "web" key). Download it from '
            "Google Cloud Console -> APIs & Services -> Credentials, for an "
            "OAuth client of type 'Desktop app'."
        )
    return inner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a PrivacyFence organization config bundle (org_config.json).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--org-name", default="", help="Shown to users after they install the bundle.")
    parser.add_argument("-o", "--output", default="org_config.json", help="Output path (default: org_config.json).")
    parser.add_argument(
        "--merge", action="store_true",
        help="Merge into an existing bundle at the output path instead of overwriting it "
             "(useful for adding one more service to an already-distributed bundle).",
    )

    google = parser.add_argument_group("Google (Gmail, Drive, Calendar, Contacts, Tasks)")
    google.add_argument(
        "--google-client-secret", metavar="PATH",
        help="Path to the client_secret.json downloaded from Google Cloud Console "
             "(OAuth client of type 'Desktop app').",
    )

    slack = parser.add_argument_group("Slack")
    slack.add_argument("--slack-client-id")
    slack.add_argument("--slack-client-secret")
    slack.add_argument(
        "--slack-scopes", nargs="+", metavar="SCOPE",
        help="Override the default Slack user-token scopes (advanced; usually leave unset).",
    )

    salesforce = parser.add_argument_group("Salesforce")
    salesforce.add_argument("--salesforce-consumer-key")
    salesforce.add_argument("--salesforce-consumer-secret")
    salesforce.add_argument(
        "--salesforce-login-url", default="https://login.salesforce.com",
        help="Default: https://login.salesforce.com (use https://test.salesforce.com for sandboxes).",
    )

    atlassian = parser.add_argument_group("Atlassian (Jira + Confluence)")
    atlassian.add_argument("--atlassian-client-id")
    atlassian.add_argument("--atlassian-client-secret")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    out_path = Path(args.output)
    bundle: dict[str, Any] = {}
    if args.merge and out_path.exists():
        with open(out_path, encoding="utf-8") as fh:
            bundle = json.load(fh)

    bundle["version"] = 1
    if args.org_name:
        bundle["org_name"] = args.org_name
    bundle["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if args.google_client_secret:
        bundle["google"] = _load_google_client_secret(args.google_client_secret)

    if args.slack_client_id or args.slack_client_secret:
        if not (args.slack_client_id and args.slack_client_secret):
            raise SystemExit("--slack-client-id and --slack-client-secret must be given together.")
        slack: dict[str, Any] = {"client_id": args.slack_client_id, "client_secret": args.slack_client_secret}
        if args.slack_scopes:
            slack["user_scopes"] = args.slack_scopes
        bundle["slack"] = slack

    if args.salesforce_consumer_key or args.salesforce_consumer_secret:
        if not (args.salesforce_consumer_key and args.salesforce_consumer_secret):
            raise SystemExit("--salesforce-consumer-key and --salesforce-consumer-secret must be given together.")
        bundle["salesforce"] = {
            "consumer_key": args.salesforce_consumer_key,
            "consumer_secret": args.salesforce_consumer_secret,
            "login_url": args.salesforce_login_url,
        }

    if args.atlassian_client_id or args.atlassian_client_secret:
        if not (args.atlassian_client_id and args.atlassian_client_secret):
            raise SystemExit("--atlassian-client-id and --atlassian-client-secret must be given together.")
        bundle["atlassian"] = {"client_id": args.atlassian_client_id, "client_secret": args.atlassian_client_secret}

    services = [k for k in ("google", "slack", "salesforce", "atlassian") if k in bundle]
    if not services:
        raise SystemExit("No service flags given — nothing to write. Pass at least one service's credentials.")

    out_path.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out_path} with: {', '.join(services)}")
    print(
        'Distribute this file to your users. They install it via "Install/Update '
        'Organization Config…" in the PrivacyFence menu bar.'
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
