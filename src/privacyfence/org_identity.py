"""Org identity: OIDC against the organization's IdP (P7, decision B).

PrivacyFence never asks a human for a password of its own. Every org-mode
sign-in -- whether it's a browser visiting ``/login`` (web/routes_org_
identity.py) or Claude's own OAuth 2.1 dance completing through the org
authorization server (web/oauth_provider.py's ``OrgOAuthProvider``) -- goes
through the exact same four functions below: build an authorization URL,
exchange the code the IdP redirects back with, verify the ID token it
returns, and turn its claims into a ``Principal``. That's deliberate: §9.4's
"the browser session and the MCP token are then provably the same
identity" argument for decision B only holds if both paths resolve identity
through literally the same code, not two implementations that happen to
agree today.

This module talks to the IdP with the same posture every other connector's
OAuth flow in this codebase already has (``oauth_loopback.py``,
``atlassian_oauth.py``, ...): synchronous ``requests`` calls, a handful of
them, never on a hot path. Callers on the ASGI event loop (web/oauth_
provider.py, web/routes_org_identity.py) run them via ``asyncio.to_thread``,
the same pattern gate.py already uses for every blocking connector call
(see that module's own comments on why).
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import jwt
import requests
from jwt import PyJWKClient

from .org_mode import AuthzPolicyConfig, ConfigurationError
from .paths import safe_principal_id
from .principal import Principal

logger = logging.getLogger(__name__)

DISCOVERY_PATH = "/.well-known/openid-configuration"
DEFAULT_SCOPE = "openid email profile"
# A handful of interactive HTTP calls during a sign-in, never a hot path --
# generous but bounded, so a slow/unreachable IdP fails the sign-in instead
# of hanging the request indefinitely.
_HTTP_TIMEOUT_SECONDS = 10
ID_TOKEN_ALGORITHMS = ["RS256", "ES256"]

# SEC-11: the escape hatch for local development against an IdP that only
# speaks plain HTTP (a devcontainer Keycloak, a loopback OIDC test server,
# ...). Never set this in a real deployment -- everything it bypasses
# (discover_idp's own issuer fetch, and every endpoint the discovery
# document names) is exactly the set of URLs org mode's trust model
# depends on being unspoofable in transit.
_DEV_ALLOW_INSECURE_IDP_ENV = "PRIVACYFENCE_DEV_ALLOW_INSECURE_IDP"
_REQUIRED_DISCOVERY_FIELDS = ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri")


def _dev_allows_insecure_idp() -> bool:
    return os.environ.get(_DEV_ALLOW_INSECURE_IDP_ENV, "") not in ("", "0", "false", "False")


def _require_https(url: str, *, what: str) -> None:
    if urlsplit(url).scheme == "https" or _dev_allows_insecure_idp():
        return
    raise ConfigurationError(
        f"{what} {url!r} is not HTTPS -- org mode requires every IdP endpoint to be HTTPS "
        f"(set {_DEV_ALLOW_INSECURE_IDP_ENV}=1 to bypass for local development against a "
        "plain-HTTP test IdP; never set this for a real deployment)"
    )


@dataclass(frozen=True)
class IdpConfig:
    """Everything needed to run the OIDC authorization-code dance against
    one org IdP. Built once at daemon startup (``from_org_config``, via
    live discovery) and passed around explicitly -- not a singleton, since
    nothing about it needs to vary per principal or per request."""

    issuer: str
    client_id: str
    client_secret: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    # §9.4: "Group/claim mapping decides who is an admin ... versus a plain
    # user." Empty admin_group_claim means "nobody is admin via this
    # mechanism" -- not "everybody is", the fail-closed direction.
    admin_group_claim: str = ""
    admin_group_values: tuple[str, ...] = ()
    # P9, §10.6/§15 D7: "IdP acr_values step-up ... where the IdP already
    # does this well." Empty means the IdP has no configured step-up ACR to
    # ask for -- web/routes_org_approvals.py's IdP step-up flow still works
    # (it always sends prompt=login/max_age=0, OIDC re-auth alone is D7's
    # documented fallback for a user with no passkey enrolled), it just
    # never adds an acr_values hint the IdP might not support.
    step_up_acr_values: tuple[str, ...] = ()

    @staticmethod
    def from_org_config(org_config: dict[str, Any]) -> "IdpConfig | None":
        """``None`` when org_config carries no (or an incomplete) ``idp``
        section -- the caller (daemon_main.py) treats that as "org mode
        configured without an IdP", which is a startup error for org mode,
        not silently falling back to local mode (mode is its own explicit
        key -- see org_mode.py).

        SEC-11: this is the only place ``discover_idp``'s result feeds an
        ``IdpConfig`` that every subsequent OIDC call trusts, so
        ``discover_idp`` itself is where the discovery document gets
        validated (shape, issuer cross-check, HTTPS-only endpoints) --
        raises ``org_mode.ConfigurationError`` rather than returning
        ``None`` for those, since unlike a genuinely absent ``idp`` section
        this is a *broken* config, the same distinction SEC-04 draws for
        ``org_config.json`` itself."""
        idp = org_config.get("idp")
        if not isinstance(idp, dict):
            return None
        issuer = idp.get("issuer")
        client_id = idp.get("client_id")
        if not issuer or not client_id:
            return None
        discovered = discover_idp(issuer)
        return IdpConfig(
            issuer=issuer,
            client_id=client_id,
            client_secret=idp.get("client_secret", ""),
            authorization_endpoint=discovered["authorization_endpoint"],
            token_endpoint=discovered["token_endpoint"],
            jwks_uri=discovered["jwks_uri"],
            admin_group_claim=idp.get("admin_group_claim", "") or "",
            admin_group_values=tuple(idp.get("admin_group_values") or ()),
            step_up_acr_values=tuple(idp.get("step_up_acr_values") or ()),
        )


def discover_idp(issuer: str) -> dict[str, Any]:
    """OIDC Discovery (an extension of RFC 8414): fetch
    ``{issuer}/.well-known/openid-configuration`` and return it as a dict.
    Every IdP this is aimed at (Okta, Entra ID, Google, Auth0, Keycloak, ...)
    supports this -- no manual-endpoint-override config exists, on purpose,
    so there's exactly one way this ever goes wrong, not two to keep in
    sync when the IdP rotates an endpoint URL.

    SEC-11: ``issuer`` itself must be HTTPS (an org-mode IdP config that
    somehow ends up plain-HTTP means everything downstream -- the
    discovery fetch below, the authorization redirect, the token exchange,
    JWKS fetch -- is interceptable/spoofable on the network path), and the
    fetched document is validated (required fields present and
    HTTPS-scheme, and its own ``issuer`` matching the one just requested)
    before this returns -- see ``_validate_discovery_metadata``'s
    docstring for why that cross-check specifically matters. Both checks
    raise ``org_mode.ConfigurationError``, same as every other "broken
    org-mode config" case (SEC-04)."""
    _require_https(issuer, what="idp.issuer")
    url = issuer.rstrip("/") + DISCOVERY_PATH
    resp = requests.get(url, timeout=_HTTP_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return _validate_discovery_metadata(resp.json(), expected_issuer=issuer)


def _validate_discovery_metadata(metadata: Any, *, expected_issuer: str) -> dict[str, Any]:
    """SEC-11: validate the discovery document's shape and provenance
    before any of it is trusted to build an ``IdpConfig`` -- a document
    that merely happens to be well-formed JSON is not the same as one
    that's actually authoritative for this IdP.

    The ``issuer`` cross-check (RFC 8414 §3.3: "the value of the 'issuer'
    member MUST be identical to the ... URL used to retrieve the
    configuration information") is what catches a discovery document
    served from the wrong place -- a DNS hijack, a misconfigured reverse
    proxy sitting in front of the real IdP, a copy-pasted issuer that
    doesn't actually match what's hosted there -- rather than one that's
    merely syntactically fine.
    """
    if not isinstance(metadata, dict):
        raise ConfigurationError("OIDC discovery document is not a JSON object")
    for field in _REQUIRED_DISCOVERY_FIELDS:
        value = metadata.get(field)
        if not isinstance(value, str) or not value:
            raise ConfigurationError(f"OIDC discovery document is missing a valid {field!r}")
    discovered_issuer = metadata["issuer"]
    if discovered_issuer.rstrip("/") != expected_issuer.rstrip("/"):
        raise ConfigurationError(
            f"OIDC discovery document's issuer {discovered_issuer!r} does not match the "
            f"configured issuer {expected_issuer!r} -- refusing to trust it"
        )
    for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        _require_https(metadata[field], what=f"discovery document's {field!r}")
    return metadata


def generate_pkce_pair() -> tuple[str, str]:
    """Returns ``(code_verifier, code_challenge)`` -- S256, per RFC 7636.
    PrivacyFence's own authorization request to the IdP uses PKCE too (not
    just the one Claude makes to PrivacyFence's own AS): there is no reason
    the leg to the IdP should be weaker than the leg from Claude."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def build_authorization_url(
    idp: IdpConfig, *, redirect_uri: str, state: str, code_challenge: str, nonce: str,
    scope: str = DEFAULT_SCOPE, extra_params: dict[str, str] | None = None,
) -> str:
    """``nonce`` is OIDC Core's own replay defense for the ID token (distinct
    from ``state``, which is OAuth's CSRF defense for the *redirect*) --
    required here, not optional, since every call site already has a fresh
    one to hand (see PendingAuthorization/LoginAttempt in oauth_provider.py/
    org_session.py, both of which generate one alongside state/PKCE).

    ``extra_params`` (P9) is
    how web/routes_org_approvals.py's IdP step-up flow layers ``prompt``/
    ``max_age``/``acr_values`` onto the same authorization request this
    function already builds for an ordinary sign-in, rather than a second
    URL-building implementation: a step-up re-auth is not a different
    protocol, only a stricter request against the identical endpoint."""
    params = {
        "response_type": "code",
        "client_id": idp.client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "nonce": nonce,
    }
    if extra_params:
        params.update(extra_params)
    sep = "&" if "?" in idp.authorization_endpoint else "?"
    return f"{idp.authorization_endpoint}{sep}{urlencode(params)}"


def exchange_code_for_tokens(idp: IdpConfig, *, code: str, redirect_uri: str, code_verifier: str) -> dict[str, Any]:
    """POSTs the token request to the IdP and returns its JSON response
    (carries ``id_token``, and usually ``access_token``/``refresh_token``
    for the IdP itself -- only ``id_token`` is used by this module; the
    others are the IdP's own tokens, not PrivacyFence's, and are discarded
    by every caller here)."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": idp.client_id,
        "code_verifier": code_verifier,
    }
    if idp.client_secret:
        data["client_secret"] = idp.client_secret
    resp = requests.post(idp.token_endpoint, data=data, timeout=_HTTP_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


def verify_id_token(idp: IdpConfig, id_token: str, *, nonce: str) -> dict[str, Any]:
    """Verifies signature (via the IdP's own JWKS, fetched/cached by
    ``PyJWKClient``), audience, issuer, expiry and nonce, and returns the
    decoded claims. Raises ``jwt.PyJWTError`` (or a subclass) on any
    failure -- callers let that propagate; there is no partial-trust
    fallback for a token that doesn't fully verify."""
    jwk_client = PyJWKClient(idp.jwks_uri)
    signing_key = jwk_client.get_signing_key_from_jwt(id_token)
    claims = jwt.decode(
        id_token,
        signing_key.key,
        algorithms=ID_TOKEN_ALGORITHMS,
        audience=idp.client_id,
        issuer=idp.issuer,
        options={"require": ["exp", "iat", "sub"]},
    )
    if claims.get("nonce") != nonce:
        raise jwt.InvalidTokenError("ID token nonce does not match the one sent in the authorization request")
    return claims


def principal_from_claims(claims: dict[str, Any], idp: IdpConfig) -> Principal:
    """The one place OIDC claims become a ``Principal`` -- shared by
    web/oauth_provider.py's IdP-callback handler and web/routes_org_
    identity.py's browser ``/login/callback``, which is what makes §9.4's
    "the browser session and the MCP token are then provably the same
    identity" true by construction rather than by two implementations
    happening to agree.
    """
    subject = str(claims.get("sub") or "")
    if not subject:
        raise ValueError("ID token has no 'sub' claim")
    email = str(claims.get("email") or "")
    display_name = str(claims.get("name") or claims.get("preferred_username") or email or subject)
    is_admin = False
    if idp.admin_group_claim:
        groups = claims.get(idp.admin_group_claim) or []
        if isinstance(groups, str):
            groups = [groups]
        is_admin = any(g in idp.admin_group_values for g in groups)
    return Principal(
        id=safe_principal_id(subject), email=email, display_name=display_name, is_admin=is_admin,
    )


class AuthorizationDenied(PermissionError):
    """Raised by ``check_authz_policy`` when a principal the IdP itself
    already authenticated fails PrivacyFence's own app-level policy
    (SEC-22) -- unlike
    every other exception this module raises (a bad code, an unverifiable
    token, a discovery document that doesn't check out), this one means the
    IdP leg *succeeded*; PrivacyFence itself is the one declining.

    Both web/routes_org_identity.py's ``login_callback`` and web/oauth_
    provider.py's ``handle_idp_callback`` already wrap their whole claims-
    to-Principal sequence in a catch-all that logs and returns a generic
    "sign-in failed, try again" response -- raising this here fits that
    same handling with no route-level changes needed, while still logging
    *why* (this exception's own message) for whoever reads the daemon's
    log. Deliberately not surfaced to the browser/OAuth client itself: an
    unauthenticated caller should not learn from the response alone
    whether they failed identity verification or an org-specific allowlist
    (SEC-10's safe-error-taxonomy posture, applied here too)."""


def check_authz_policy(principal: Principal, claims: dict[str, Any], policy: AuthzPolicyConfig) -> None:
    """The one place a principal the IdP already authenticated can still be
    turned away by PrivacyFence itself. ``policy.enabled is False`` (the
    default -- no ``authz`` section in org_config.json, see AuthzPolicyConfig's
    own docstring) is a complete no-op, so an existing org-mode install
    keeps admitting every IdP-authenticated principal exactly as before
    this landed.

    Takes ``claims`` directly (not just ``principal``) because group
    membership never made it onto ``Principal`` -- only ``is_admin``, a
    single bit derived from IdpConfig.admin_group_claim, did (see that
    field's own docstring). Kept as a separate step from ``principal_from_
    claims`` above, rather than folded into it, because unlike ``is_admin``
    (carried forward on the Principal for other code to use later) an
    authz-policy failure has nowhere to be carried to -- there's no
    Principal to finish building, only a decision to raise on.
    """
    if not policy.enabled:
        return
    if policy.allowed_domains:
        domain = principal.email.rsplit("@", 1)[-1].lower() if "@" in principal.email else ""
        if domain not in policy.allowed_domains:
            raise AuthorizationDenied(
                f"principal {principal.id!r} (email {principal.email!r}) is not in an allowed domain"
            )
    if policy.required_groups:
        groups = claims.get(policy.groups_claim) or []
        if isinstance(groups, str):
            groups = [groups]
        if not any(group in policy.required_groups for group in groups):
            raise AuthorizationDenied(
                f"principal {principal.id!r} (email {principal.email!r}) is not in a required group"
            )


__all__ = [
    "DEFAULT_SCOPE",
    "AuthorizationDenied",
    "IdpConfig",
    "build_authorization_url",
    "check_authz_policy",
    "discover_idp",
    "exchange_code_for_tokens",
    "generate_pkce_pair",
    "principal_from_claims",
    "verify_id_token",
]
