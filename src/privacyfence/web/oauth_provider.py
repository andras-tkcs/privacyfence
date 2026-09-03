"""``OrgOAuthProvider`` -- PrivacyFence's own minimal OAuth 2.1 authorization
server (P7, docs/https-connector-refactor-plan.md §9.4, §15's decision B),
implementing the official MCP SDK's ``OAuthAuthorizationServerProvider``
protocol. Wired into web/routes_mcp.py's ``/mcp`` (via ``verify_token``,
satisfying the SDK's separate ``TokenVerifier`` protocol too) and into
``mcp.server.auth.routes.create_auth_routes`` (which builds ``/authorize``,
``/token``, ``/register``, ``/revoke`` and the AS metadata document against
whatever provider it's given -- see that module's own docstring for why
none of that protocol machinery needed hand-rolling, same reasoning as D2).

The diagram in ``OAuthAuthorizationServerProvider.authorize``'s own
docstring is exactly this class's shape:

    Client (Claude) --> PrivacyFence (this AS) --> the org's IdP (OIDC)

``authorize()`` doesn't decide anything itself -- it redirects the browser
to the org IdP (org_identity.py), stashing the *original* client's request
under a fresh state value of PrivacyFence's own. ``handle_idp_callback()``
(called by web/routes_mcp.py's own IdP-facing route, not part of the SDK's
protocol) is where the IdP's answer comes back: it verifies the ID token,
resolves a ``Principal`` (org_identity.principal_from_claims -- the exact
same function web/routes_org_identity.py's browser login uses, which is
what makes §9.4's "the browser session and the MCP token are then provably
the same identity" true by construction), and only then mints
PrivacyFence's *own* authorization code, bound to that principal, and
redirects the browser on to the original client's own redirect_uri.

Storage: registered OAuth clients (DCR) are persisted to disk
(``org_dir()/oauth_clients.json``) -- losing that on restart would mean
every installed Claude connector has to re-register, which is real user
friction DCR is supposed to spare people. Pending authorizations,
authorization codes, access tokens and refresh tokens are in-memory only --
short-lived by design (§5.4's decision-ledger precedent: state that's
supposed to expire soon anyway doesn't need to survive a restart), so
losing them on restart just means signing in again, not a security gap.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .. import org_identity
from ..org_identity import IdpConfig
from ..principal import Principal

logger = logging.getLogger(__name__)

CLIENTS_FILE_NAME = "oauth_clients.json"
IDP_CALLBACK_PATH = "/oauth/idp/callback"

_AUTHORIZATION_CODE_TTL_SECONDS = 5 * 60
_ACCESS_TOKEN_TTL_SECONDS = 60 * 60
_PENDING_AUTHORIZATION_TTL_SECONDS = 5 * 60


class _OrgRefreshToken(RefreshToken):
    """Carries the principal's claims forward across a refresh so
    ``exchange_refresh_token`` can mint a fully-populated access token
    without a second, separate lookup keyed on ``subject`` (which
    wouldn't be enough on its own -- email/display_name/is_admin aren't
    derivable from a bare subject string)."""

    email: str = ""
    display_name: str = ""
    is_admin: bool = False


@dataclass
class _PendingAuthorization:
    """One Claude/MCP client's ``/authorize`` request, parked while the
    human completes the IdP leg -- keyed by a fresh state value of
    PrivacyFence's own (never the original client's own ``state``, which
    stays opaque to the IdP and travels back untouched at the end)."""

    client_id: str
    params: AuthorizationParams
    idp_nonce: str
    idp_code_verifier: str
    created_at: float


@dataclass
class _IssuedCode:
    """PrivacyFence's own authorization code, bound to the principal the
    IdP leg resolved -- exchanged exactly once (``exchange_authorization_
    code`` pops it), per §5.4's single-consumption precedent for anything
    that releases on the strength of a one-time decision."""

    client_id: str
    principal: Principal
    params: AuthorizationParams
    expires_at: float


class OrgOAuthProvider:
    """Implements ``OAuthAuthorizationServerProvider`` (satisfied
    structurally -- this class is never registered against the Protocol at
    import time, matching every other duck-typed provider the SDK itself
    ships) and doubles as a ``TokenVerifier`` for web/routes_mcp.py's
    bearer-auth middleware via ``verify_token``.
    """

    def __init__(self, idp: IdpConfig, *, idp_callback_url: str) -> None:
        self._idp = idp
        self._idp_callback_url = idp_callback_url
        self._clients_path = Path(_clients_file_path())
        self._lock = threading.Lock()
        self._clients: dict[str, OAuthClientInformationFull] = self._load_clients()
        self._pending: dict[str, _PendingAuthorization] = {}
        self._codes: dict[str, _IssuedCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, _OrgRefreshToken] = {}
        # Access<->refresh pairing, so revoke_token() can cascade per the
        # SDK's own guidance ("SHOULD revoke both ... regardless of which
        # ... is provided") without a second index to keep in sync by hand.
        self._refresh_for_access: dict[str, str] = {}
        self._access_for_refresh: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # DCR client store
    # ------------------------------------------------------------------ #

    def _load_clients(self) -> dict[str, OAuthClientInformationFull]:
        if not self._clients_path.exists():
            return {}
        try:
            raw = json.loads(self._clients_path.read_text(encoding="utf-8"))
            return {
                client_id: OAuthClientInformationFull.model_validate(data)
                for client_id, data in raw.items()
            }
        except Exception as exc:
            logger.warning("Could not read %s: %s -- starting with no registered clients", self._clients_path, exc)
            return {}

    def _save_clients_locked(self) -> None:
        raw = {cid: json.loads(info.model_dump_json()) for cid, info in self._clients.items()}
        self._clients_path.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
        self._clients_path.chmod(0o600)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._lock:
            return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        with self._lock:
            self._clients[client_info.client_id] = client_info
            self._save_clients_locked()
        logger.info("Registered OAuth client %r via DCR", client_info.client_id)

    # ------------------------------------------------------------------ #
    # authorize() -- delegates human authentication to the org IdP
    # ------------------------------------------------------------------ #

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        self._prune_pending()
        own_state = secrets.token_urlsafe(32)
        idp_nonce = secrets.token_urlsafe(16)
        idp_code_verifier, idp_code_challenge = org_identity.generate_pkce_pair()
        with self._lock:
            self._pending[own_state] = _PendingAuthorization(
                client_id=client.client_id, params=params, idp_nonce=idp_nonce,
                idp_code_verifier=idp_code_verifier, created_at=time.time(),
            )
        return org_identity.build_authorization_url(
            self._idp, redirect_uri=self._idp_callback_url, state=own_state,
            code_challenge=idp_code_challenge, nonce=idp_nonce,
        )

    def _prune_pending(self) -> None:
        now = time.time()
        with self._lock:
            stale = [
                s for s, p in self._pending.items()
                if (now - p.created_at) > _PENDING_AUTHORIZATION_TTL_SECONDS
            ]
            for s in stale:
                del self._pending[s]

    async def handle_idp_callback(self, *, state: str, code: str) -> str:
        """Called by web/routes_mcp.py's own IdP-facing route (``GET
        /oauth/idp/callback``) once the human has finished at the IdP --
        not part of the SDK's ``OAuthAuthorizationServerProvider`` Protocol,
        since nothing in the spec has an opinion on how an AS talks to
        *its own* upstream IdP. Returns the URL to redirect the browser to
        next: back to the original client's own ``redirect_uri``, carrying
        PrivacyFence's own freshly-minted code and the original client's
        own ``state`` -- exactly the return value ``authorize()`` would
        have produced directly, had this AS trusted the human's identity
        on its own instead of asking the IdP.
        """
        with self._lock:
            pending = self._pending.pop(state, None)
        if pending is None:
            raise ValueError("invalid or expired authorization attempt")
        tokens = await asyncio.to_thread(
            org_identity.exchange_code_for_tokens,
            self._idp, code=code, redirect_uri=self._idp_callback_url, code_verifier=pending.idp_code_verifier,
        )
        id_token = tokens.get("id_token")
        if not id_token:
            raise ValueError("IdP token response carried no id_token")
        claims = await asyncio.to_thread(org_identity.verify_id_token, self._idp, id_token, nonce=pending.idp_nonce)
        principal = org_identity.principal_from_claims(claims, self._idp)

        own_code = secrets.token_urlsafe(32)
        with self._lock:
            self._codes[own_code] = _IssuedCode(
                client_id=pending.client_id, principal=principal, params=pending.params,
                expires_at=time.time() + _AUTHORIZATION_CODE_TTL_SECONDS,
            )
        return construct_redirect_uri(str(pending.params.redirect_uri), code=own_code, state=pending.params.state)

    # ------------------------------------------------------------------ #
    # Authorization code -> tokens
    # ------------------------------------------------------------------ #

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str,
    ) -> AuthorizationCode | None:
        with self._lock:
            issued = self._codes.get(authorization_code)
        if issued is None or issued.client_id != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=issued.params.scopes or [],
            expires_at=issued.expires_at,
            client_id=issued.client_id,
            code_challenge=issued.params.code_challenge,
            redirect_uri=issued.params.redirect_uri,
            redirect_uri_provided_explicitly=issued.params.redirect_uri_provided_explicitly,
            resource=issued.params.resource,
            subject=issued.principal.id,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        with self._lock:
            issued = self._codes.pop(authorization_code.code, None)  # single-use (§5.4 precedent)
        if issued is None:
            raise TokenError(error="invalid_grant", error_description="authorization code already used or unknown")
        return self._mint_tokens(
            client_id=client.client_id, scopes=authorization_code.scopes,
            resource=authorization_code.resource, principal=issued.principal,
        )

    # ------------------------------------------------------------------ #
    # Refresh token
    # ------------------------------------------------------------------ #

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str,
    ) -> _OrgRefreshToken | None:
        with self._lock:
            rt = self._refresh_tokens.get(refresh_token)
        if rt is None or rt.client_id != client.client_id:
            return None
        return rt

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: _OrgRefreshToken, scopes: list[str],
    ) -> OAuthToken:
        principal = Principal(
            id=refresh_token.subject or "", email=refresh_token.email,
            display_name=refresh_token.display_name, is_admin=refresh_token.is_admin,
        )
        with self._lock:
            self._revoke_pair_locked(access_token=None, refresh_token_str=refresh_token.token)
        return self._mint_tokens(
            client_id=client.client_id, scopes=scopes or refresh_token.scopes,
            resource=None, principal=principal,
        )

    # ------------------------------------------------------------------ #
    # Access token verification (TokenVerifier + load_access_token)
    # ------------------------------------------------------------------ #

    async def load_access_token(self, token: str) -> AccessToken | None:
        with self._lock:
            at = self._access_tokens.get(token)
            if at is None:
                return None
            if at.expires_at is not None and at.expires_at < time.time():
                self._revoke_pair_locked(access_token=token, refresh_token_str=None)
                return None
            return at

    async def verify_token(self, token: str) -> AccessToken | None:
        """Satisfies ``mcp.server.auth.provider.TokenVerifier`` -- web/
        routes_mcp.py's bearer-auth middleware calls this directly; it's a
        pure delegation so there is exactly one definition of "is this
        access token valid" in this class, not two that could drift."""
        return await self.load_access_token(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with self._lock:
            if isinstance(token, RefreshToken):
                self._revoke_pair_locked(access_token=None, refresh_token_str=token.token)
            else:
                self._revoke_pair_locked(access_token=token.token, refresh_token_str=None)

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _mint_tokens(
        self, *, client_id: str, scopes: list[str], resource: str | None, principal: Principal,
    ) -> OAuthToken:
        access_token_str = secrets.token_urlsafe(32)
        refresh_token_str = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + _ACCESS_TOKEN_TTL_SECONDS
        claims = {"email": principal.email, "display_name": principal.display_name, "is_admin": principal.is_admin}
        with self._lock:
            self._access_tokens[access_token_str] = AccessToken(
                token=access_token_str, client_id=client_id, scopes=scopes, expires_at=expires_at,
                resource=resource, subject=principal.id, claims=claims,
            )
            self._refresh_tokens[refresh_token_str] = _OrgRefreshToken(
                token=refresh_token_str, client_id=client_id, scopes=scopes, expires_at=None,
                subject=principal.id, email=principal.email, display_name=principal.display_name,
                is_admin=principal.is_admin,
            )
            self._refresh_for_access[access_token_str] = refresh_token_str
            self._access_for_refresh[refresh_token_str] = access_token_str
        return OAuthToken(
            access_token=access_token_str, token_type="Bearer", expires_in=_ACCESS_TOKEN_TTL_SECONDS,
            scope=" ".join(scopes) if scopes else None, refresh_token=refresh_token_str,
        )

    def _revoke_pair_locked(self, *, access_token: str | None, refresh_token_str: str | None) -> None:
        """Caller already holds ``self._lock``. Cascades to whichever half
        of an access/refresh pair wasn't given directly."""
        if refresh_token_str is None and access_token is not None:
            refresh_token_str = self._refresh_for_access.get(access_token)
        if access_token is None and refresh_token_str is not None:
            access_token = self._access_for_refresh.get(refresh_token_str)
        if access_token is not None:
            self._access_tokens.pop(access_token, None)
            self._refresh_for_access.pop(access_token, None)
        if refresh_token_str is not None:
            self._refresh_tokens.pop(refresh_token_str, None)
            self._access_for_refresh.pop(refresh_token_str, None)


def _clients_file_path() -> str:
    from ..paths import org_dir

    return str(org_dir() / CLIENTS_FILE_NAME)


__all__ = ["IDP_CALLBACK_PATH", "OrgOAuthProvider"]
