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


def resolve_mode(org_config: dict[str, Any]) -> Mode:
    mode = org_config.get("mode", DEFAULT_MODE)
    if mode not in ("local", "org"):
        raise ValueError(f"org_config.json's \"mode\" must be \"local\" or \"org\", got {mode!r}")
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
            raise ValueError("org mode requires org_config.json's \"server\".\"issuer_url\"")
        return ServerConfig(
            bind_host=raw.get("bind_host", DEFAULT_BIND_HOST),
            port=int(raw.get("port", DEFAULT_PORT)),
            issuer_url=issuer_url,
            cert_file=tls.get("cert_file", ""),
            key_file=tls.get("key_file", ""),
            trusted_proxies=tuple(raw.get("trusted_proxies") or ()),
        )


__all__ = ["DEFAULT_MODE", "Mode", "ServerConfig", "resolve_mode"]
