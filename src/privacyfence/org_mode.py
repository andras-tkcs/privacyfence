"""``mode: local`` vs. ``mode: org`` (P7, docs/https-connector-refactor-
plan.md §4's operating-modes table) -- the one setting the rest of the
table's rows follow from. Lives in ``org_config.json`` (§4: "org_config.json
| as today | as today, plus server/TLS/IdP config"), not settings.yaml: it's
an install-wide decision, not a per-user preference, and org_config.json is
already the file daemon_main.py reads before it knows anything about a
principal at all.

Absent entirely, ``mode`` resolves to ``"local"`` -- an existing install's
org_config.json (today only ever carrying Google/Slack/Salesforce/
Atlassian app registrations, per daemon_main.py's own module docstring)
keeps meaning exactly what it already means, with no migration.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Mode = Literal["local", "org"]

DEFAULT_MODE: Mode = "local"
DEFAULT_BIND_HOST = "localhost"
DEFAULT_PORT = 8765


class ConfigurationError(ValueError):
    """Raised for organization configuration that is present but broken --
    unreadable, malformed JSON, a non-object top level, an invalid
    ``mode``, or (in org mode) missing/incomplete required sections --
    rather than genuinely absent (SEC-04). daemon_main.py's ``main()``
    never catches this specifically: it's a ``ValueError`` subclass, so it
    falls into the same "print and refuse to start" path every other
    startup configuration error already takes, deliberately -- there is no
    silent fallback to local mode for a *broken* config, only for a
    missing one (see load_org_config's own docstring for why that
    distinction matters)."""


def resolve_mode(org_config: dict[str, Any]) -> Mode:
    mode = org_config.get("mode", DEFAULT_MODE)
    if mode not in ("local", "org"):
        raise ConfigurationError(f"org_config.json's \"mode\" must be \"local\" or \"org\", got {mode!r}")
    return mode


@dataclass(frozen=True)
class ServerConfig:
    """§10.2's transport decision, made concrete per install. Local mode's
    defaults here (``bind_host="localhost"``, no TLS, no trusted proxies)
    are exactly D1's decision -- loopback plain HTTP -- so an install that
    never sets ``mode: org`` never has a reason to look at this class at
    all; ``daemon_main.py`` only calls ``from_org_config`` when
    ``resolve_mode`` says org.
    """

    bind_host: str = DEFAULT_BIND_HOST
    port: int = DEFAULT_PORT
    # The externally-reachable origin used to build OAuth/OIDC redirect
    # URIs (web/oauth_provider.py's IdP callback, web/routes_org_
    # identity.py's login callback, and the AS's own issuer_url) -- may
    # differ from bind_host:port when this daemon sits behind a reverse
    # proxy or load balancer.
    issuer_url: str = ""
    cert_file: str = ""
    key_file: str = ""
    # §10.2: "X-Forwarded-For / X-Forwarded-Proto are honored only when an
    # explicit trusted_proxies list is configured, never by default."
    trusted_proxies: tuple[str, ...] = ()

    @property
    def tls_configured(self) -> bool:
        return bool(self.cert_file and self.key_file)

    @staticmethod
    def from_org_config(org_config: dict[str, Any]) -> "ServerConfig":
        raw = org_config.get("server")
        raw = raw if isinstance(raw, dict) else {}
        tls = raw.get("tls")
        tls = tls if isinstance(tls, dict) else {}
        issuer_url = raw.get("issuer_url", "")
        if not issuer_url:
            raise ConfigurationError("org mode requires org_config.json's \"server\".\"issuer_url\"")
        return ServerConfig(
            bind_host=raw.get("bind_host", DEFAULT_BIND_HOST),
            port=int(raw.get("port", DEFAULT_PORT)),
            issuer_url=issuer_url,
            cert_file=tls.get("cert_file", ""),
            key_file=tls.get("key_file", ""),
            trusted_proxies=tuple(raw.get("trusted_proxies") or ()),
        )


StepUpScope = Literal["writes", "writes_and_pii_reads"]

DEFAULT_STEP_UP_SCOPE: StepUpScope = "writes"
DEFAULT_RP_NAME = "PrivacyFence"


@dataclass(frozen=True)
class StepUpConfig:
    """§10.6/§15 D7's step-up decision, made concrete per install: "Yes in
    org mode, scoped and configurable -- via a WebAuthn platform
    authenticator ... with IdP acr_values step-up as the org-mode
    alternative ... and OIDC re-auth as the fallback." Lives in
    ``org_config.json``'s ``step_up`` section, org-mode-only for the same
    reason ``ServerConfig``/``IdpConfig`` are: local mode's own trust model
    (physical possession of the machine) is D7's explicit "not this mode"
    case, not merely an unconfigured default here.

    ``enabled=False`` (the default -- absent ``step_up`` section, or an
    existing org install that predates P9) is a real off switch, not just
    "no credentials enrolled yet": web/routes_org_approvals.py's decide
    endpoint skips the whole step-up check when this is False, so turning
    P9 on is an explicit opt-in per deployment, exactly like every other
    org-mode surface this codebase has shipped so far.
    """

    enabled: bool = False
    # "writes" (gate_kind == "popup") is D7's baseline scope -- "scope it to
    # writes, or to writes plus PII-flagged reads ... make the scope
    # configurable" (§10.6). "writes_and_pii_reads" additionally covers a
    # read whose PendingApproval.pii_detected is True, the same signal
    # gate.py's own PII "are you sure?" confirmation already gates on.
    scope: StepUpScope = DEFAULT_STEP_UP_SCOPE
    # WebAuthn's Relying Party ID -- must be this server's own registrable
    # domain (§10.6: "WebAuthn needs a secure context and a registrable-
    # domain RP ID"). Defaults to ServerConfig.issuer_url's own hostname
    # (from_org_config, below) rather than requiring a redundant setting.
    rp_id: str = ""
    rp_name: str = DEFAULT_RP_NAME

    @staticmethod
    def from_org_config(org_config: dict[str, Any], *, default_rp_id: str = "") -> "StepUpConfig":
        raw = org_config.get("step_up")
        raw = raw if isinstance(raw, dict) else {}
        scope = raw.get("scope", DEFAULT_STEP_UP_SCOPE)
        if scope not in ("writes", "writes_and_pii_reads"):
            raise ConfigurationError(
                f"org_config.json's \"step_up\".\"scope\" must be \"writes\" or "
                f"\"writes_and_pii_reads\", got {scope!r}"
            )
        return StepUpConfig(
            enabled=bool(raw.get("enabled", False)),
            scope=scope,
            rp_id=raw.get("rp_id", "") or default_rp_id,
            rp_name=raw.get("rp_name", DEFAULT_RP_NAME) or DEFAULT_RP_NAME,
        )


# Deliberately *larger* than connectors/drive.py's/connectors/gmail.py's/
# connectors/confluence.py's own pre-approval prefetch caps (5MB),
# reflecting that in org mode, inline delivery is the primary transport
# for drive_download_file/gmail_download_attachment/confluence_download_
# attachment, not a small-file convenience. The real ceiling here is
# practical MCP Streamable HTTP response size and base64's ~33%
# inflation, not a privacy argument for staying small.
DEFAULT_INLINE_MAX_BYTES = 8_000_000

# 5 minutes -- see download_staging.DEFAULT_TTL_SECONDS's own docstring for
# why this is short: staging means an encrypted-but-real copy of the file
# sits on the server's disk for this long.
DEFAULT_LINK_TTL_SECONDS = 300.0


@dataclass(frozen=True)
class DownloadDeliveryConfig:
    """org mode's own answer to the local-disk-write bug docs/org-mode-
    download-delivery-plan.md exists to fix: how ``drive_download_file``/
    ``gmail_download_attachment``/``confluence_download_attachment``
    deliver file bytes to a principal who has no shell on the daemon's own
    machine. Lives in ``org_config.json``'s ``download_delivery`` section,
    org-mode-only for the same reason ``ServerConfig``/``StepUpConfig``
    are -- local mode keeps writing straight to ``destination_dir``,
    unchanged, and never looks at this class at all.
    """

    inline_max_bytes: int = DEFAULT_INLINE_MAX_BYTES
    link_ttl_seconds: float = DEFAULT_LINK_TTL_SECONDS
    # The org-level opt-out (see the plan's "Org-level opt-out" section):
    # when False, a file too large for inline delivery is refused outright
    # rather than ever being written -- encrypted or not -- to this
    # server's disk. Default True: encryption-at-rest (download_staging.py)
    # is the primary mitigation, and staging still happens for oversized
    # files by default.
    allow_disk_staging: bool = True

    def fits_inline(self, size_bytes: int) -> bool:
        """Whether a file this size should be delivered inline (base64, in
        the tool result) rather than staged behind a one-time link.
        ``inline_max_bytes == 0`` (the "force every download through a
        staged link, unconditionally" knob) always returns False here --
        even for an empty (0-byte) file -- rather than the arithmetically
        tempting but wrong ``0 <= 0``."""
        return self.inline_max_bytes > 0 and size_bytes <= self.inline_max_bytes

    @staticmethod
    def from_org_config(org_config: dict[str, Any]) -> "DownloadDeliveryConfig":
        raw = org_config.get("download_delivery")
        raw = raw if isinstance(raw, dict) else {}
        inline_max_bytes = int(raw.get("inline_max_bytes", DEFAULT_INLINE_MAX_BYTES))
        if inline_max_bytes < 0:
            raise ConfigurationError(
                "org_config.json's \"download_delivery\".\"inline_max_bytes\" must be >= 0 "
                f"(0 forces every download through a staged link), got {inline_max_bytes}"
            )
        link_ttl_seconds = float(raw.get("link_ttl_seconds", DEFAULT_LINK_TTL_SECONDS))
        if link_ttl_seconds <= 0:
            raise ConfigurationError(
                "org_config.json's \"download_delivery\".\"link_ttl_seconds\" must be > 0, "
                f"got {link_ttl_seconds}"
            )
        return DownloadDeliveryConfig(
            inline_max_bytes=inline_max_bytes,
            link_ttl_seconds=link_ttl_seconds,
            allow_disk_staging=bool(raw.get("allow_disk_staging", True)),
        )


AuditForwardingKind = Literal["syslog", "http"]
DEFAULT_AUDIT_FORWARDING_KIND: AuditForwardingKind = "syslog"

SyslogProtocol = Literal["udp", "tcp"]
DEFAULT_SYSLOG_PROTOCOL: SyslogProtocol = "tcp"
# IANA's syslog-tls port (RFC 5425), not 514 (RFC 5424's plaintext-only
# default) -- see audit_forwarding.py's own module docstring for why this
# module doesn't speak TLS itself and expects this port to be fronted by a
# TLS-terminating relay when that matters.
DEFAULT_SYSLOG_PORT = 6514


@dataclass(frozen=True)
class AuditForwardingConfig:
    """SEC-23: org
    mode's centralized audit-log forwarding destination. Lives in
    ``org_config.json``'s ``audit_forwarding`` section, org-mode-only like
    ``ServerConfig``/``StepUpConfig``/``DownloadDeliveryConfig`` above --
    local mode never looks at this at all (there is no "centralize" to
    speak of for a single employee's own machine); see daemon_main.py's
    ``run_app`` for the ``resolve_mode(org_config) == "org"`` gate.

    Forwarding is *additional* visibility, never a replacement for the
    local audit log ``audit_log.py``'s ``AuditLogger`` always writes --
    that JSONL file (with its own append-integrity hash chain, SEC-23's
    other half, always on regardless of this config) stays the
    authoritative record even when forwarding is enabled and even when a
    specific entry fails to forward. See ``audit_forwarding.py`` for what
    each ``kind`` actually sends on the wire.
    """

    enabled: bool = False
    kind: AuditForwardingKind = DEFAULT_AUDIT_FORWARDING_KIND
    syslog_host: str = ""
    syslog_port: int = DEFAULT_SYSLOG_PORT
    syslog_protocol: SyslogProtocol = DEFAULT_SYSLOG_PROTOCOL
    http_url: str = ""
    # Name of an environment variable *this daemon's own process*  reads a
    # bearer token from at forward-send time -- never itself stored in
    # org_config.json, so a leaked or mis-shared bundle doesn't also leak
    # the SIEM credential (the same reasoning behind every OAuth client
    # secret in this file being a real secret, just via env var since
    # there's no build-time secrets store equivalent for an IT-run
    # server's own SIEM API key).
    http_bearer_token_env: str = ""

    @staticmethod
    def from_org_config(org_config: dict[str, Any]) -> "AuditForwardingConfig":
        raw = org_config.get("audit_forwarding")
        raw = raw if isinstance(raw, dict) else {}
        enabled = bool(raw.get("enabled", False))
        kind = raw.get("kind", DEFAULT_AUDIT_FORWARDING_KIND)
        if kind not in ("syslog", "http"):
            raise ConfigurationError(
                f"org_config.json's \"audit_forwarding\".\"kind\" must be \"syslog\" or \"http\", "
                f"got {kind!r}"
            )
        syslog_raw = raw.get("syslog")
        syslog_raw = syslog_raw if isinstance(syslog_raw, dict) else {}
        http_raw = raw.get("http")
        http_raw = http_raw if isinstance(http_raw, dict) else {}

        syslog_protocol = syslog_raw.get("protocol", DEFAULT_SYSLOG_PROTOCOL)
        if syslog_protocol not in ("udp", "tcp"):
            raise ConfigurationError(
                f"org_config.json's \"audit_forwarding\".\"syslog\".\"protocol\" must be \"udp\" "
                f"or \"tcp\", got {syslog_protocol!r}"
            )
        http_url = str(http_raw.get("url", "") or "")
        if enabled and kind == "http" and http_url and not http_url.startswith("https://"):
            raise ConfigurationError(
                "org_config.json's \"audit_forwarding\".\"http\".\"url\" must use https:// -- "
                "audit entries are sensitive, and this endpoint is otherwise reached over "
                "plaintext HTTP."
            )
        return AuditForwardingConfig(
            enabled=enabled,
            kind=kind,
            syslog_host=str(syslog_raw.get("host", "") or ""),
            syslog_port=int(syslog_raw.get("port", DEFAULT_SYSLOG_PORT)),
            syslog_protocol=syslog_protocol,
            http_url=http_url,
            http_bearer_token_env=str(http_raw.get("bearer_token_env", "") or ""),
        )


@dataclass(frozen=True)
class AuthzPolicyConfig:
    """SEC-22: an
    optional PrivacyFence-level allowlist layered *on top of* the IdP's own
    authentication, not a replacement for it -- the IdP has already decided
    who this human is by the time anything here runs (org_identity.py's
    ``check_authz_policy`` is only ever called after ``principal_from_
    claims`` has a real ``Principal`` in hand); this decides whether
    PrivacyFence itself is willing to admit them.

    Exists because docs/org-mode-setup-guide.md §4.1 flags this as a real
    gap: for a plain (non-Workspace) Google IdP, the OAuth consent screen's
    own test-user list or verification status is the *only* access control
    most org-mode deployments have -- an IdP-side setting this repo can't
    see or audit, let alone enforce consistently across a different IdP.

    Lives in ``org_config.json``'s ``authz`` section, org-mode-only like
    every other org_mode.py config class. Absent entirely (or an ``authz``
    section with neither list set) means ``enabled`` is ``False`` -- "no
    additional restriction, every IdP-authenticated principal is admitted"
    -- so an existing org-mode install with no ``authz`` section keeps
    working exactly as before this landed, the same additive/opt-in
    posture every other org-mode config in this module already has.
    """

    # Case-folded, leading-"@"-stripped at parse time (see from_org_config)
    # so "acme.com", "Acme.com" and "@acme.com" in org_config.json all mean
    # the same thing -- matched against the domain half of the principal's
    # own (IdP-asserted) email.
    allowed_domains: tuple[str, ...] = ()
    # ID token claim (e.g. "groups") that carries group membership -- kept
    # separate from IdpConfig.admin_group_claim (a different question:
    # "is this human an admin", not "may this human sign in at all") so an
    # org can gate sign-in on group membership without also having to
    # configure -- or share values with -- the admin mapping.
    groups_claim: str = ""
    required_groups: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return bool(self.allowed_domains) or bool(self.required_groups)

    @staticmethod
    def from_org_config(org_config: dict[str, Any]) -> "AuthzPolicyConfig":
        raw = org_config.get("authz")
        raw = raw if isinstance(raw, dict) else {}
        raw_domains = (str(d).strip().lower().lstrip("@") for d in (raw.get("allowed_domains") or ()))
        allowed_domains = tuple(domain for domain in raw_domains if domain)
        required_groups = tuple(str(g) for g in (raw.get("required_groups") or ()) if str(g))
        groups_claim = raw.get("groups_claim", "") or ""
        if required_groups and not groups_claim:
            raise ConfigurationError(
                "org_config.json's \"authz\".\"required_groups\" is set but \"groups_claim\" is "
                "empty -- PrivacyFence has no ID token claim to read group membership from"
            )
        return AuthzPolicyConfig(
            allowed_domains=allowed_domains, groups_claim=groups_claim, required_groups=required_groups,
        )


__all__ = [
    "AuditForwardingConfig",
    "AuditForwardingKind",
    "AuthzPolicyConfig",
    "ConfigurationError",
    "DEFAULT_AUDIT_FORWARDING_KIND",
    "DEFAULT_INLINE_MAX_BYTES",
    "DEFAULT_LINK_TTL_SECONDS",
    "DEFAULT_MODE",
    "DEFAULT_RP_NAME",
    "DEFAULT_STEP_UP_SCOPE",
    "DEFAULT_SYSLOG_PORT",
    "DEFAULT_SYSLOG_PROTOCOL",
    "DownloadDeliveryConfig",
    "Mode",
    "ServerConfig",
    "StepUpConfig",
    "StepUpScope",
    "SyslogProtocol",
    "resolve_mode",
]
