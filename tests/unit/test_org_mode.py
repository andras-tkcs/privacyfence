"""Tests for org_mode.py: the mode toggle and §10.2 server config (P7)."""
from __future__ import annotations

import pytest

from privacyfence import org_mode


class TestResolveMode:
    def test_absent_key_defaults_to_local(self):
        assert org_mode.resolve_mode({}) == "local"

    def test_explicit_local(self):
        assert org_mode.resolve_mode({"mode": "local"}) == "local"

    def test_explicit_org(self):
        assert org_mode.resolve_mode({"mode": "org"}) == "org"

    def test_invalid_value_raises(self):
        # SEC-04: ConfigurationError, not a bare ValueError -- so callers
        # (daemon_main.py's main()) can't mistake this for some other
        # ValueError-raising failure further down the same startup path.
        with pytest.raises(org_mode.ConfigurationError):
            org_mode.resolve_mode({"mode": "something-else"})


class TestServerConfigFromOrgConfig:
    def test_requires_issuer_url(self):
        with pytest.raises(org_mode.ConfigurationError):
            org_mode.ServerConfig.from_org_config({"server": {}})

    def test_requires_a_server_section_at_all(self):
        with pytest.raises(org_mode.ConfigurationError):
            org_mode.ServerConfig.from_org_config({})

    def test_builds_config_with_defaults(self):
        config = org_mode.ServerConfig.from_org_config({"server": {"issuer_url": "https://pf.example.com"}})
        assert config.issuer_url == "https://pf.example.com"
        assert config.bind_host == org_mode.DEFAULT_BIND_HOST
        assert config.port == org_mode.DEFAULT_PORT
        assert config.trusted_proxies == ()
        assert config.tls_configured is False

    def test_builds_config_with_every_field_set(self):
        config = org_mode.ServerConfig.from_org_config({
            "server": {
                "bind_host": "0.0.0.0", "port": 443, "issuer_url": "https://pf.example.com",
                "tls": {"cert_file": "/etc/pf/cert.pem", "key_file": "/etc/pf/key.pem"},
                "trusted_proxies": ["10.0.0.5", "10.0.0.6"],
            },
        })
        assert config.bind_host == "0.0.0.0"
        assert config.port == 443
        assert config.cert_file == "/etc/pf/cert.pem"
        assert config.key_file == "/etc/pf/key.pem"
        assert config.trusted_proxies == ("10.0.0.5", "10.0.0.6")
        assert config.tls_configured is True

    def test_tls_configured_is_false_when_only_one_half_is_set(self):
        config = org_mode.ServerConfig.from_org_config({
            "server": {"issuer_url": "https://pf.example.com", "tls": {"cert_file": "/etc/pf/cert.pem"}},
        })
        assert config.tls_configured is False


class TestDefaultsMatchLocalModePosture:
    def test_local_mode_default_bind_host_is_loopback_named(self):
        # D1 (§15/§10.2): "served on localhost, not a bare 127.0.0.1" --
        # this default is what a caller gets if it ever asked ServerConfig
        # for local-mode-shaped values (nothing does today; daemon_main.py
        # keeps its own hardcoded "localhost" for local mode, unchanged --
        # but this default has to keep agreeing with it).
        assert org_mode.ServerConfig().bind_host == "localhost"

    def test_default_has_no_tls(self):
        assert org_mode.ServerConfig().tls_configured is False


class TestStepUpConfigFromOrgConfig:
    """P9, §10.6/§15 D7: WebAuthn step-up is off by default (an existing org
    install with no "step_up" section keeps working exactly as before this
    phase -- see web/routes_org_approvals.py's own decide()'s
    ``if step_up.enabled`` gate)."""

    def test_absent_section_is_disabled_with_defaults(self):
        config = org_mode.StepUpConfig.from_org_config({})
        assert config.enabled is False
        assert config.scope == "writes"
        assert config.rp_id == ""
        assert config.rp_name == org_mode.DEFAULT_RP_NAME

    def test_default_rp_id_falls_back_to_the_caller_supplied_default(self):
        config = org_mode.StepUpConfig.from_org_config({}, default_rp_id="pf.example.com")
        assert config.rp_id == "pf.example.com"

    def test_explicit_rp_id_wins_over_the_default(self):
        config = org_mode.StepUpConfig.from_org_config(
            {"step_up": {"rp_id": "custom.example.com"}}, default_rp_id="pf.example.com",
        )
        assert config.rp_id == "custom.example.com"

    def test_enabled_and_scope_and_rp_name(self):
        config = org_mode.StepUpConfig.from_org_config({
            "step_up": {"enabled": True, "scope": "writes_and_pii_reads", "rp_name": "Acme PrivacyFence"},
        })
        assert config.enabled is True
        assert config.scope == "writes_and_pii_reads"
        assert config.rp_name == "Acme PrivacyFence"

    def test_invalid_scope_raises(self):
        with pytest.raises(org_mode.ConfigurationError):
            org_mode.StepUpConfig.from_org_config({"step_up": {"scope": "everything"}})


class TestDownloadDeliveryConfigFromOrgConfig:
    """An existing org install with no "download_delivery" section keeps
    working exactly as before (inline-first, 8MB cap, staging allowed)."""

    def test_absent_section_uses_defaults(self):
        config = org_mode.DownloadDeliveryConfig.from_org_config({})
        assert config.inline_max_bytes == org_mode.DEFAULT_INLINE_MAX_BYTES
        assert config.link_ttl_seconds == org_mode.DEFAULT_LINK_TTL_SECONDS
        assert config.allow_disk_staging is True

    def test_every_field_set(self):
        config = org_mode.DownloadDeliveryConfig.from_org_config({
            "download_delivery": {
                "inline_max_bytes": 1_000_000, "link_ttl_seconds": 60.0, "allow_disk_staging": False,
            },
        })
        assert config.inline_max_bytes == 1_000_000
        assert config.link_ttl_seconds == 60.0
        assert config.allow_disk_staging is False

    def test_zero_inline_max_bytes_is_allowed(self):
        # The knob an org picks for "no file content ever reaches Claude's
        # context, unconditionally" -- forces every download through a
        # staged link.
        config = org_mode.DownloadDeliveryConfig.from_org_config({
            "download_delivery": {"inline_max_bytes": 0},
        })
        assert config.inline_max_bytes == 0

    def test_negative_inline_max_bytes_raises(self):
        with pytest.raises(org_mode.ConfigurationError):
            org_mode.DownloadDeliveryConfig.from_org_config({"download_delivery": {"inline_max_bytes": -1}})

    def test_non_positive_link_ttl_raises(self):
        with pytest.raises(org_mode.ConfigurationError):
            org_mode.DownloadDeliveryConfig.from_org_config({"download_delivery": {"link_ttl_seconds": 0}})


class TestAuditForwardingConfigFromOrgConfig:
    """Org mode's centralized audit-log forwarding destination. An existing org
    install with no "audit_forwarding" section keeps working exactly as
    before this phase (forwarding off)."""

    def test_absent_section_uses_defaults(self):
        config = org_mode.AuditForwardingConfig.from_org_config({})
        assert config.enabled is False
        assert config.kind == org_mode.DEFAULT_AUDIT_FORWARDING_KIND
        assert config.syslog_host == ""
        assert config.syslog_port == org_mode.DEFAULT_SYSLOG_PORT
        assert config.syslog_protocol == org_mode.DEFAULT_SYSLOG_PROTOCOL
        assert config.http_url == ""
        assert config.http_bearer_token_env == ""

    def test_every_syslog_field_set(self):
        config = org_mode.AuditForwardingConfig.from_org_config({
            "audit_forwarding": {
                "enabled": True, "kind": "syslog",
                "syslog": {"host": "siem.example.com", "port": 601, "protocol": "udp"},
            },
        })
        assert config.enabled is True
        assert config.kind == "syslog"
        assert config.syslog_host == "siem.example.com"
        assert config.syslog_port == 601
        assert config.syslog_protocol == "udp"

    def test_every_http_field_set(self):
        config = org_mode.AuditForwardingConfig.from_org_config({
            "audit_forwarding": {
                "enabled": True, "kind": "http",
                "http": {"url": "https://siem.example.com/ingest", "bearer_token_env": "SIEM_TOKEN"},
            },
        })
        assert config.kind == "http"
        assert config.http_url == "https://siem.example.com/ingest"
        assert config.http_bearer_token_env == "SIEM_TOKEN"

    def test_non_dict_section_is_ignored(self):
        config = org_mode.AuditForwardingConfig.from_org_config({"audit_forwarding": "not a dict"})
        assert config.enabled is False

    def test_non_dict_syslog_and_http_subsections_are_ignored(self):
        config = org_mode.AuditForwardingConfig.from_org_config({
            "audit_forwarding": {"syslog": "nope", "http": ["nope"]},
        })
        assert config.syslog_host == ""
        assert config.http_url == ""

    def test_invalid_kind_raises(self):
        with pytest.raises(org_mode.ConfigurationError):
            org_mode.AuditForwardingConfig.from_org_config({"audit_forwarding": {"kind": "carrier_pigeon"}})

    def test_invalid_syslog_protocol_raises(self):
        with pytest.raises(org_mode.ConfigurationError):
            org_mode.AuditForwardingConfig.from_org_config({
                "audit_forwarding": {"syslog": {"protocol": "quic"}},
            })

    def test_enabled_http_kind_with_non_https_url_raises(self):
        with pytest.raises(org_mode.ConfigurationError):
            org_mode.AuditForwardingConfig.from_org_config({
                "audit_forwarding": {"enabled": True, "kind": "http", "http": {"url": "http://siem.example.com"}},
            })

    def test_disabled_http_kind_with_non_https_url_does_not_raise(self):
        # A mid-rollout config -- enabled=False, section not fully filled
        # in yet -- must not be rejected just because the URL isn't
        # https:// yet; the check only applies once it's actually live.
        config = org_mode.AuditForwardingConfig.from_org_config({
            "audit_forwarding": {"enabled": False, "kind": "http", "http": {"url": "http://siem.example.com"}},
        })
        assert config.http_url == "http://siem.example.com"

    def test_syslog_kind_with_non_https_http_url_present_does_not_raise(self):
        # The https:// check only applies when kind is actually "http" --
        # an org that's set up both sections while trying out syslog first
        # shouldn't be blocked by an unrelated http.url value.
        config = org_mode.AuditForwardingConfig.from_org_config({
            "audit_forwarding": {"enabled": True, "kind": "syslog", "http": {"url": "http://siem.example.com"}},
        })
        assert config.kind == "syslog"

    def test_enabled_http_kind_with_https_url_does_not_raise(self):
        config = org_mode.AuditForwardingConfig.from_org_config({
            "audit_forwarding": {"enabled": True, "kind": "http", "http": {"url": "https://siem.example.com"}},
        })
        assert config.http_url == "https://siem.example.com"

    def test_enabled_http_kind_with_empty_url_does_not_raise(self):
        # An incomplete config (enabled flipped on before the URL is filled
        # in) -- audit_forwarding.build_sender() is where "http_url is
        # required" is actually enforced, not here.
        config = org_mode.AuditForwardingConfig.from_org_config({
            "audit_forwarding": {"enabled": True, "kind": "http"},
        })
        assert config.http_url == ""


class TestAuthzPolicyConfigFromOrgConfig:
    """An existing org install with no "authz" section keeps admitting every
    IdP-authenticated principal exactly as before this landed."""

    def test_absent_section_is_disabled_with_defaults(self):
        config = org_mode.AuthzPolicyConfig.from_org_config({})
        assert config.enabled is False
        assert config.allowed_domains == ()
        assert config.groups_claim == ""
        assert config.required_groups == ()

    def test_allowed_domains_are_lowercased_and_at_stripped(self):
        config = org_mode.AuthzPolicyConfig.from_org_config({
            "authz": {"allowed_domains": ["Acme.com", "@sub.Acme.com"]},
        })
        assert config.allowed_domains == ("acme.com", "sub.acme.com")
        assert config.enabled is True

    def test_required_groups_with_groups_claim(self):
        config = org_mode.AuthzPolicyConfig.from_org_config({
            "authz": {"groups_claim": "groups", "required_groups": ["privacyfence-users"]},
        })
        assert config.groups_claim == "groups"
        assert config.required_groups == ("privacyfence-users",)
        assert config.enabled is True

    def test_required_groups_without_groups_claim_raises(self):
        with pytest.raises(org_mode.ConfigurationError, match="groups_claim"):
            org_mode.AuthzPolicyConfig.from_org_config({"authz": {"required_groups": ["admins"]}})

    def test_blank_domain_entries_are_dropped(self):
        config = org_mode.AuthzPolicyConfig.from_org_config({"authz": {"allowed_domains": ["", "  ", "acme.com"]}})
        assert config.allowed_domains == ("acme.com",)

    def test_non_dict_authz_section_is_treated_as_absent(self):
        config = org_mode.AuthzPolicyConfig.from_org_config({"authz": "not-a-dict"})
        assert config.enabled is False


class TestConfigurationError:
    def test_is_a_value_error_subclass(self):
        # So every existing `except ValueError`/`pytest.raises(ValueError)`
        # around org-config parsing keeps working unchanged -- this is a
        # narrowing of the exception type raised, not a new one call sites
        # must learn to catch.
        assert issubclass(org_mode.ConfigurationError, ValueError)
