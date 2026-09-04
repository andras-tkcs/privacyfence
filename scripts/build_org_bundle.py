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

--enable-unattended-sessions turns on privacyfence_begin_unattended_session
for every install of this bundle — a deliberate per-organization choice, see
docs/TECHNICAL_REFERENCE.md's "Scheduled / unattended Cowork tasks" section.

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

    mode = parser.add_argument_group(
        "Deployment mode (P7, docs/https-connector-refactor-plan.md §4/§9.4/§10.2)",
    )
    mode.add_argument(
        "--mode", choices=["local", "org"], default=None,
        help="Absent (the default) leaves \"mode\" out of the bundle entirely, which "
             "PrivacyFence itself treats as \"local\" -- an existing install/bundle needs no "
             "change to keep working exactly as it always has. Pass --mode org together with "
             "the --server-* and --idp-* flags below to turn this into an org-mode bundle.",
    )
    mode.add_argument(
        "--server-issuer-url", metavar="URL",
        help="This daemon's own externally-reachable origin, e.g. https://pf.acme.example.com "
             "-- required with --mode org. Used to build the fixed OAuth/OIDC redirect URIs "
             "you register with your IdP below (<issuer-url>/oauth/idp/callback and "
             "<issuer-url>/oauth/idp/login-callback).",
    )
    mode.add_argument(
        "--server-bind-host", default="0.0.0.0",
        help="Address the embedded server listens on (default: 0.0.0.0 -- every interface; "
             "narrow this if the host has one you'd rather bind specifically).",
    )
    mode.add_argument("--server-port", type=int, default=8765, help="Default: 8765.")
    mode.add_argument(
        "--server-tls-cert", metavar="PATH",
        help="TLS certificate file, terminated directly in the embedded server. Leave both "
             "--server-tls-cert and --server-tls-key unset if a reverse proxy in front of this "
             "daemon terminates TLS instead.",
    )
    mode.add_argument("--server-tls-key", metavar="PATH", help="TLS private key file (paired with --server-tls-cert).")
    mode.add_argument(
        "--server-trusted-proxy", action="append", default=[], metavar="IP", dest="server_trusted_proxies",
        help="An X-Forwarded-For/X-Forwarded-Proto-trusted reverse proxy's own IP address -- "
             "repeat for more than one. §10.2: honored only when at least one is given here, "
             "never by default.",
    )
    mode.add_argument(
        "--idp-issuer", metavar="URL",
        help="Your organization's OIDC identity provider's issuer URL (its "
             "/.well-known/openid-configuration document must be reachable at "
             "<this>/.well-known/openid-configuration) -- required with --mode org.",
    )
    mode.add_argument(
        "--idp-client-id", metavar="ID",
        help="The client_id PrivacyFence is registered under with your IdP -- required with "
             "--mode org. Register it with two redirect URIs: <server-issuer-url>/oauth/idp/"
             "callback and <server-issuer-url>/oauth/idp/login-callback.",
    )
    mode.add_argument("--idp-client-secret", metavar="SECRET", help="Paired with --idp-client-id.")
    mode.add_argument(
        "--idp-admin-group-claim", metavar="CLAIM",
        help="ID token claim (e.g. \"groups\") whose value names the human as an admin when it "
             "contains one of --idp-admin-group-value. Omit to leave nobody an admin via this "
             "mechanism (the fail-closed default).",
    )
    mode.add_argument(
        "--idp-admin-group-value", action="append", default=[], metavar="VALUE", dest="idp_admin_group_values",
        help="A value of --idp-admin-group-claim that marks the human as an admin -- repeat "
             "for more than one (e.g. --idp-admin-group-value privacyfence-admins "
             "--idp-admin-group-value it-admins).",
    )

    step_up = parser.add_argument_group(
        "WebAuthn step-up (P9, docs/https-connector-refactor-plan.md §10.6/§15 D7)",
    )
    step_up_toggle = step_up.add_mutually_exclusive_group()
    step_up_toggle.add_argument(
        "--step-up-enabled", action="store_true",
        help="Require a fresh passkey (or IdP re-authentication) before releasing a write "
             "approval -- off by default. Only meaningful with --mode org (§10.6: local mode's "
             "own trust model is physical possession of the machine, which this doesn't add to).",
    )
    step_up_toggle.add_argument(
        "--step-up-disabled", action="store_true", help="Explicitly turn step-up back off (useful with --merge).",
    )
    step_up.add_argument(
        "--step-up-scope", choices=["writes", "writes_and_pii_reads"], default=None,
        help="Default: writes. \"writes_and_pii_reads\" additionally requires step-up before a "
             "read that detected personal data, not just a write.",
    )
    step_up.add_argument(
        "--step-up-rp-id", metavar="DOMAIN",
        help="WebAuthn Relying Party ID -- must be --server-issuer-url's own registrable domain "
             "(a secure-context requirement, see §10.6). Defaults to that hostname, derived "
             "automatically -- only set this to override it.",
    )
    step_up.add_argument("--step-up-rp-name", metavar="NAME", help='Shown in the OS passkey prompt. Default: "PrivacyFence".')
    step_up.add_argument(
        "--idp-step-up-acr-value", action="append", default=[], metavar="ACR", dest="idp_step_up_acr_values",
        help="An acr_values your IdP accepts to request stronger authentication on step-up's "
             "IdP re-auth path (§10.6's \"IdP acr_values step-up ... where the IdP already does "
             "this well\") -- repeat for more than one. Omit to fall back to plain re-"
             "authentication (prompt=login) with no acr_values hint.",
    )

    unattended = parser.add_argument_group("Unattended / scheduled Cowork tasks")
    unattended_toggle = unattended.add_mutually_exclusive_group()
    unattended_toggle.add_argument(
        "--enable-unattended-sessions", action="store_true",
        help="Let Claude Cowork declare a connection unattended (privacyfence_"
             "begin_unattended_session) for scheduled/triggered runs with no human "
             "present. Off by default -- a deliberate per-organization opt-in, see "
             "docs/TECHNICAL_REFERENCE.md's \"Scheduled / unattended Cowork tasks\" section.",
    )
    unattended_toggle.add_argument(
        "--disable-unattended-sessions", action="store_true",
        help="Explicitly turn unattended sessions back off (useful with --merge).",
    )

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

    if args.enable_unattended_sessions:
        bundle["unattended_sessions"] = {"enabled": True}
    elif args.disable_unattended_sessions:
        bundle["unattended_sessions"] = {"enabled": False}

    if args.mode == "org":
        if not args.server_issuer_url:
            raise SystemExit("--mode org requires --server-issuer-url.")
        if not (args.idp_issuer and args.idp_client_id and args.idp_client_secret):
            raise SystemExit("--mode org requires --idp-issuer, --idp-client-id and --idp-client-secret.")
        bundle["mode"] = "org"
        server: dict[str, Any] = {
            "issuer_url": args.server_issuer_url,
            "bind_host": args.server_bind_host,
            "port": args.server_port,
        }
        if args.server_tls_cert or args.server_tls_key:
            if not (args.server_tls_cert and args.server_tls_key):
                raise SystemExit("--server-tls-cert and --server-tls-key must be given together.")
            server["tls"] = {"cert_file": args.server_tls_cert, "key_file": args.server_tls_key}
        if args.server_trusted_proxies:
            server["trusted_proxies"] = args.server_trusted_proxies
        bundle["server"] = server

        idp: dict[str, Any] = {
            "issuer": args.idp_issuer, "client_id": args.idp_client_id, "client_secret": args.idp_client_secret,
        }
        if args.idp_admin_group_claim:
            idp["admin_group_claim"] = args.idp_admin_group_claim
            idp["admin_group_values"] = args.idp_admin_group_values
        if args.idp_step_up_acr_values:
            idp["step_up_acr_values"] = args.idp_step_up_acr_values
        bundle["idp"] = idp
    elif args.mode == "local":
        bundle["mode"] = "local"
        bundle.pop("server", None)
        bundle.pop("idp", None)
        bundle.pop("step_up", None)
    elif any([
        args.server_issuer_url, args.idp_issuer, args.idp_client_id, args.idp_client_secret,
        args.server_tls_cert, args.server_tls_key, args.server_trusted_proxies, args.idp_step_up_acr_values,
    ]):
        raise SystemExit("--server-*/--idp-*/--idp-step-up-acr-value flags require --mode org.")

    if args.step_up_enabled or args.step_up_disabled or args.step_up_scope or args.step_up_rp_id or args.step_up_rp_name:
        # bundle["mode"] already reflects either this invocation's --mode
        # or (with --merge and no --mode given) whatever mode the existing
        # bundle on disk already had -- either way, "org" is what actually
        # matters here, not args.mode by itself.
        if bundle.get("mode") != "org":
            raise SystemExit("--step-up-* flags require --mode org (or --merge against an existing org-mode bundle).")
        step_up_section: dict[str, Any] = dict(bundle.get("step_up") or {})
        if args.step_up_enabled:
            step_up_section["enabled"] = True
        elif args.step_up_disabled:
            step_up_section["enabled"] = False
        if args.step_up_scope:
            step_up_section["scope"] = args.step_up_scope
        if args.step_up_rp_id:
            step_up_section["rp_id"] = args.step_up_rp_id
        if args.step_up_rp_name:
            step_up_section["rp_name"] = args.step_up_rp_name
        bundle["step_up"] = step_up_section

    services = [k for k in ("google", "slack", "salesforce", "atlassian") if k in bundle]
    if not services and "unattended_sessions" not in bundle and "mode" not in bundle:
        raise SystemExit(
            "No service, --mode, or --enable/disable-unattended-sessions flags given — nothing to write."
        )

    out_path.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    try:
        out_path.chmod(0o600)
    except OSError:  # pragma: no cover - best effort on non-POSIX
        pass
    summary = ", ".join(services) or "none"
    if "unattended_sessions" in bundle:
        summary += f", unattended_sessions.enabled={bundle['unattended_sessions']['enabled']}"
    if "mode" in bundle:
        summary += f", mode={bundle['mode']}"
    if "step_up" in bundle:
        summary += f", step_up.enabled={bundle['step_up'].get('enabled', False)}"
    print(f"Wrote {out_path} with: {summary}")
    if bundle.get("mode") == "org":
        print(
            f"Org mode: register {args.server_issuer_url}/oauth/idp/callback and "
            f"{args.server_issuer_url}/oauth/idp/login-callback as redirect URIs for client_id "
            f"{args.idp_client_id!r} with your IdP, if you haven't already."
        )
    print(
        'Distribute this file to your users. They install it via "Install/Update '
        'Organization Config…" in the PrivacyFence menu bar.'
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
