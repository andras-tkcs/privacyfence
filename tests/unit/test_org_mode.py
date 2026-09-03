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
        with pytest.raises(ValueError):
            org_mode.resolve_mode({"mode": "something-else"})


class TestServerConfigFromOrgConfig:
    def test_requires_issuer_url(self):
        with pytest.raises(ValueError):
            org_mode.ServerConfig.from_org_config({"server": {}})

    def test_requires_a_server_section_at_all(self):
        with pytest.raises(ValueError):
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
