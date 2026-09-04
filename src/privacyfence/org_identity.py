"""Org identity: OIDC against the organization's IdP (P7, docs/
https-connector-refactor-plan.md §9.4 and §15's decision B).

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
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import jwt
import requests
from jwt import PyJWKClient

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
        key -- see org_mode.py)."""
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
    sync when the IdP rotates an endpoint URL."""
    url = issuer.rstrip("/") + DISCOVERY_PATH
    resp = requests.get(url, timeout=_HTTP_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


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

    ``extra_params`` (P9, docs/https-connector-refactor-plan.md §10.6) is
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


__all__ = [
    "DEFAULT_SCOPE",
    "IdpConfig",
    "build_authorization_url",
    "discover_idp",
    "exchange_code_for_tokens",
    "generate_pkce_pair",
    "principal_from_claims",
    "verify_id_token",
]
