#!/usr/bin/env python3
"""Build a PrivacyFence organization config bundle (org_config.json).

Run this once per organization after registering each cloud app (see the
"For IT admins" section of docs/google-cloud-setup.md, docs/slack-setup.md,
docs/salesforce-setup.md, and docs/atlassian-setup.md). The output file is
what you distribute to your users — they install it via "Install/Update
Organization Config…" on the General page of PrivacyFence Settings (the embedded web page).

Telegram is not part of this bundle: its api_id/api_hash identify the
PrivacyFence app itself (not your organization) and are baked into the
release build — see docs/telegram-setup.md and src/privacyfence/app_credentials.py.

Only pass the flags for services you've set up; a connector is offered to
users only if its section is present in the bundle. Stdlib only — no
PrivacyFence install required to run this -- except --sign-key/
--generate-signing-key (SEC-05 full signing, below), which need the
`cryptography` package (pip install cryptography) specifically, not a
full PrivacyFence install.

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
import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _canonical_payload_bytes(bundle: dict[str, Any]) -> bytes:
    """Mirrors src/privacyfence/org_bundle_signing.py's own
    ``_canonical_payload_bytes`` exactly -- the two MUST stay byte-for-
    byte identical, or a bundle signed here will fail to verify there.
    Not imported from that module because this script is meant to be
    runnable standalone (see module docstring) without a PrivacyFence
    install; only ``cryptography`` needs to be pip-installed separately
    to use --sign-key/--generate-signing-key at all.
    """
    payload = {k: v for k, v in bundle.items() if k != "signature"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _require_cryptography():
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
            PublicFormat,
            load_pem_private_key,
        )
    except ImportError as exc:
        raise SystemExit(
            "--sign-key/--generate-signing-key need the `cryptography` package: "
            "pip install cryptography"
        ) from exc
    return ed25519, Encoding, NoEncryption, PrivateFormat, PublicFormat, load_pem_private_key


def _generate_signing_key(path: str) -> int:
    ed25519, Encoding, NoEncryption, PrivateFormat, PublicFormat, _load = _require_cryptography()
    private_key = ed25519.Ed25519PrivateKey.generate()
    out = Path(path)
    out.write_bytes(private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    try:
        out.chmod(0o600)
    except OSError:  # pragma: no cover - best effort on non-POSIX
        pass
    pub_b64 = base64.b64encode(
        private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode("ascii")
    print(f"Wrote Ed25519 signing private key to {out} -- keep this file secret.")
    print(f"Public key (embedded in every bundle you sign with it): {pub_b64}")
    print(
        "Pass this file to every future build_org_bundle.py run (including --merge) via "
        "--sign-key so all your bundles keep verifying against the same key -- the first signed "
        "bundle each install sees pins that key and rejects anything that doesn't verify against "
        "it afterwards, unsigned bundles included. Losing this key means every install that has "
        "already pinned it can't accept an update until an administrator deletes their pinned "
        "org_config_signing_pubkey.txt by hand."
    )
    return 0


def _sign_bundle(bundle: dict[str, Any], sign_key_path: str) -> dict[str, Any]:
    ed25519, Encoding, _NoEncryption, _PrivateFormat, PublicFormat, load_pem_private_key = _require_cryptography()
    with open(sign_key_path, "rb") as fh:
        private_key = load_pem_private_key(fh.read(), password=None)
    if not isinstance(private_key, ed25519.Ed25519PrivateKey):
        raise SystemExit(
            f"{sign_key_path} is not an Ed25519 private key "
            "(generate one with --generate-signing-key)."
        )
    signed = dict(bundle)
    signed.pop("signature", None)
    signed["signing_public_key"] = base64.b64encode(
        private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode("ascii")
    signature = private_key.sign(_canonical_payload_bytes(signed))
    signed["signature"] = base64.b64encode(signature).decode("ascii")
    return signed


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
            "OAuth client of type 'Desktop app' (local mode's loopback flow) or "
            "'Web application' (org mode's server-redirect flow needs an "
            "explicit, registered HTTPS redirect URI -- see docs/org-mode-"
            "setup-guide.md's §4.2)."
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
             "(OAuth client of type 'Desktop app' for local mode, or 'Web "
             "application' for org mode -- see docs/org-mode-setup-guide.md's "
             "§4.2).",
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

    authz = parser.add_argument_group(
        "App-level authorization policy (SEC-22, docs/security-remediation-plan.md, Phase 3 item 3.7)",
    )
    authz.add_argument(
        "--authz-allowed-domain", action="append", default=[], metavar="DOMAIN", dest="authz_allowed_domains",
        help="Only admit a principal whose (IdP-asserted) email is at this domain -- repeat for "
             "more than one. Layered on top of the IdP's own authentication, not a replacement "
             "for it. Omit to leave this unrestricted (the default).",
    )
    authz.add_argument(
        "--authz-groups-claim", metavar="CLAIM",
        help="ID token claim (e.g. \"groups\") whose value --authz-required-group is checked "
             "against -- required if --authz-required-group is given. Independent of "
             "--idp-admin-group-claim (a different question: who's an admin, not who may sign in "
             "at all) even if your IdP happens to use the same claim name for both.",
    )
    authz.add_argument(
        "--authz-required-group", action="append", default=[], metavar="VALUE", dest="authz_required_groups",
        help="Only admit a principal whose --authz-groups-claim contains one of these -- repeat "
             "for more than one. Requires --authz-groups-claim.",
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

    downloads = parser.add_argument_group(
        "Download delivery (org mode, docs/org-mode-download-delivery-plan.md)",
    )
    downloads.add_argument(
        "--downloads-inline-max-bytes", type=int, metavar="BYTES", default=None,
        help="drive_download_file/gmail_download_attachment/confluence_download_attachment: "
             "files at or under this size are returned directly in the tool result instead of "
             "written to destination_dir (meaningless in local mode). Default: 8000000 (8MB). "
             "0 forces every download through a one-time staged link instead.",
    )
    downloads.add_argument(
        "--downloads-link-ttl-seconds", type=float, metavar="SECONDS", default=None,
        help="How long a staged-download link stays claimable before it expires. Default: 300 "
             "(5 minutes).",
    )
    downloads.add_argument(
        "--downloads-disable-staging", action="store_true",
        help="Refuse (rather than stage to disk, encrypted) a download too large for "
             "--downloads-inline-max-bytes. Off by default -- see the plan doc's \"Org-level "
             "opt-out\" section for when to turn this on.",
    )

    audit_forwarding = parser.add_argument_group(
        "Centralized audit-log forwarding (org mode, SEC-23, "
        "src/privacyfence/audit_forwarding.py)",
    )
    audit_forwarding.add_argument(
        "--audit-forwarding-kind", choices=("syslog", "http"), default=None,
        help='Forward every audit-log entry to a syslog server ("syslog") or post it as JSON to '
             'an HTTPS webhook -- Splunk HEC, Datadog Logs API, an Elastic ingest pipeline, an '
             'OTLP-over-HTTP/JSON log receiver ("http"). Default: "syslog". The local audit log '
             '(with its own append-integrity hash chain) stays the authoritative record either '
             "way -- this is additional visibility, not a replacement.",
    )
    audit_forwarding_toggle = audit_forwarding.add_mutually_exclusive_group()
    audit_forwarding_toggle.add_argument(
        "--enable-audit-forwarding", action="store_true",
        help="Turn on centralized audit-log forwarding. Requires --audit-forwarding-kind and "
             "the matching --audit-forwarding-syslog-host or --audit-forwarding-http-url.",
    )
    audit_forwarding_toggle.add_argument(
        "--disable-audit-forwarding", action="store_true",
        help="Explicitly turn audit-log forwarding back off (useful with --merge).",
    )
    audit_forwarding.add_argument(
        "--audit-forwarding-syslog-host", metavar="HOST", default=None,
        help="syslog server hostname/IP. Required (with --enable-audit-forwarding) when "
             "--audit-forwarding-kind syslog.",
    )
    audit_forwarding.add_argument(
        "--audit-forwarding-syslog-port", type=int, metavar="PORT", default=None,
        help="Default: 6514 (RFC 5425's syslog-tls port -- see audit_forwarding.py's module "
             "docstring for why TLS itself isn't implemented there and this port is still the "
             "sane default).",
    )
    audit_forwarding.add_argument(
        "--audit-forwarding-syslog-protocol", choices=("udp", "tcp"), default=None,
        help="Default: tcp.",
    )
    audit_forwarding.add_argument(
        "--audit-forwarding-http-url", metavar="URL", default=None,
        help="HTTPS endpoint to POST each audit entry to (as one JSON object per request). "
             "Required (with --enable-audit-forwarding) when --audit-forwarding-kind http. Must "
             "start with https:// -- audit entries are sensitive.",
    )
    audit_forwarding.add_argument(
        "--audit-forwarding-http-bearer-token-env", metavar="ENV_VAR", default=None,
        help="Name of an environment variable the daemon process itself reads a bearer token "
             "from at send time -- never stored in this bundle. Set that variable in the "
             "systemd unit/environment on the server, not here.",
    )

    signing = parser.add_argument_group(
        "Bundle signing (SEC-05 full signing, src/privacyfence/org_bundle_signing.py)",
    )
    signing.add_argument(
        "--generate-signing-key", metavar="PATH",
        help="Generate a new Ed25519 signing keypair, write the private key (PEM, PKCS8) to "
             "PATH with 0600 permissions, print its public key, and exit without building a "
             "bundle. Run this once per organization and keep PATH secret -- pass it to "
             "--sign-key on every future run (including --merge) so all your bundles keep "
             "verifying against the same key. Needs the `cryptography` package.",
    )
    signing.add_argument(
        "--sign-key", metavar="PATH",
        help="Sign the bundle with the Ed25519 private key at PATH (from "
             "--generate-signing-key). Required for --mode org -- PrivacyFence refuses to start "
             "in org mode with an unsigned bundle. Optional but recommended otherwise. Needs the "
             "`cryptography` package.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.generate_signing_key:
        return _generate_signing_key(args.generate_signing_key)

    out_path = Path(args.output)
    bundle: dict[str, Any] = {}
    if args.merge and out_path.exists():
        with open(out_path, encoding="utf-8") as fh:
            bundle = json.load(fh)
    # Any existing signature was over the bundle as it stood at that
    # earlier signing -- it's stale the moment anything below changes it,
    # and even when nothing changes, whether to re-affirm it is exactly
    # what --sign-key (re-)signing below decides. Never carry a
    # signature/key forward implicitly.
    bundle.pop("signature", None)
    bundle.pop("signing_public_key", None)

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
        bundle.pop("download_delivery", None)
        bundle.pop("authz", None)
        bundle.pop("audit_forwarding", None)
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

    if (
        args.downloads_inline_max_bytes is not None
        or args.downloads_link_ttl_seconds is not None
        or args.downloads_disable_staging
    ):
        if bundle.get("mode") != "org":
            raise SystemExit(
                "--downloads-* flags require --mode org (or --merge against an existing org-mode bundle)."
            )
        downloads_section: dict[str, Any] = dict(bundle.get("download_delivery") or {})
        if args.downloads_inline_max_bytes is not None:
            downloads_section["inline_max_bytes"] = args.downloads_inline_max_bytes
        if args.downloads_link_ttl_seconds is not None:
            downloads_section["link_ttl_seconds"] = args.downloads_link_ttl_seconds
        if args.downloads_disable_staging:
            downloads_section["allow_disk_staging"] = False
        bundle["download_delivery"] = downloads_section

    if args.authz_allowed_domains or args.authz_groups_claim or args.authz_required_groups:
        if bundle.get("mode") != "org":
            raise SystemExit("--authz-* flags require --mode org (or --merge against an existing org-mode bundle).")
        if args.authz_required_groups and not args.authz_groups_claim:
            raise SystemExit("--authz-required-group requires --authz-groups-claim.")
        authz_section: dict[str, Any] = dict(bundle.get("authz") or {})
        if args.authz_allowed_domains:
            authz_section["allowed_domains"] = args.authz_allowed_domains
        if args.authz_groups_claim:
            authz_section["groups_claim"] = args.authz_groups_claim
        if args.authz_required_groups:
            authz_section["required_groups"] = args.authz_required_groups
        bundle["authz"] = authz_section

    if (
        args.enable_audit_forwarding or args.disable_audit_forwarding
        or args.audit_forwarding_kind or args.audit_forwarding_syslog_host
        or args.audit_forwarding_syslog_port is not None or args.audit_forwarding_syslog_protocol
        or args.audit_forwarding_http_url or args.audit_forwarding_http_bearer_token_env
    ):
        if bundle.get("mode") != "org":
            raise SystemExit(
                "--audit-forwarding-* flags require --mode org (or --merge against an existing "
                "org-mode bundle)."
            )
        forwarding_section: dict[str, Any] = dict(bundle.get("audit_forwarding") or {})
        if args.enable_audit_forwarding:
            forwarding_section["enabled"] = True
        elif args.disable_audit_forwarding:
            forwarding_section["enabled"] = False
        if args.audit_forwarding_kind:
            forwarding_section["kind"] = args.audit_forwarding_kind
        kind = forwarding_section.get("kind", "syslog")
        if kind == "syslog":
            syslog_section: dict[str, Any] = dict(forwarding_section.get("syslog") or {})
            if args.audit_forwarding_syslog_host:
                syslog_section["host"] = args.audit_forwarding_syslog_host
            if args.audit_forwarding_syslog_port is not None:
                syslog_section["port"] = args.audit_forwarding_syslog_port
            if args.audit_forwarding_syslog_protocol:
                syslog_section["protocol"] = args.audit_forwarding_syslog_protocol
            if syslog_section:
                forwarding_section["syslog"] = syslog_section
            if forwarding_section.get("enabled") and not syslog_section.get("host"):
                raise SystemExit(
                    "--enable-audit-forwarding with --audit-forwarding-kind syslog (the default) "
                    "requires --audit-forwarding-syslog-host."
                )
        elif kind == "http":
            http_section: dict[str, Any] = dict(forwarding_section.get("http") or {})
            if args.audit_forwarding_http_url:
                http_section["url"] = args.audit_forwarding_http_url
            if args.audit_forwarding_http_bearer_token_env:
                http_section["bearer_token_env"] = args.audit_forwarding_http_bearer_token_env
            if http_section:
                forwarding_section["http"] = http_section
            if forwarding_section.get("enabled") and not http_section.get("url"):
                raise SystemExit(
                    "--enable-audit-forwarding with --audit-forwarding-kind http requires "
                    "--audit-forwarding-http-url."
                )
        bundle["audit_forwarding"] = forwarding_section

    services = [k for k in ("google", "slack", "salesforce", "atlassian") if k in bundle]
    if not services and "unattended_sessions" not in bundle and "mode" not in bundle:
        raise SystemExit(
            "No service, --mode, or --enable/disable-unattended-sessions flags given — nothing to write."
        )

    if bundle.get("mode") == "org" and not args.sign_key:
        raise SystemExit(
            "This bundle has \"mode\": \"org\" -- PrivacyFence refuses to start in org mode with "
            "an unsigned bundle. Pass --sign-key <path to your Ed25519 private key> (generate one "
            "first with --generate-signing-key if you haven't yet)."
        )
    if args.sign_key:
        bundle = _sign_bundle(bundle, args.sign_key)

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
    if "download_delivery" in bundle:
        summary += f", download_delivery.allow_disk_staging={bundle['download_delivery'].get('allow_disk_staging', True)}"
    if "authz" in bundle:
        n_domains = len(bundle["authz"].get("allowed_domains") or [])
        n_groups = len(bundle["authz"].get("required_groups") or [])
        summary += f", authz.allowed_domains={n_domains}, authz.required_groups={n_groups}"
    if "audit_forwarding" in bundle:
        summary += f", audit_forwarding.enabled={bundle['audit_forwarding'].get('enabled', False)}"
    summary += f", signed={'signature' in bundle}"
    print(f"Wrote {out_path} with: {summary}")
    if bundle.get("mode") == "org":
        print(
            f"Org mode: register {args.server_issuer_url}/oauth/idp/callback and "
            f"{args.server_issuer_url}/oauth/idp/login-callback as redirect URIs for client_id "
            f"{args.idp_client_id!r} with your IdP, if you haven't already."
        )
    if "signature" in bundle:
        print(
            "This bundle is signed. The first install that reads it will trust and pin its "
            "signing key (trust-on-first-use) -- every bundle installed on that machine "
            "afterwards, including any future unsigned one, must verify against that same key."
        )
    print(
        'Distribute this file to your users. They install it via "Install/Update '
        'Organization Config…" on the General page of PrivacyFence Settings.'
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
