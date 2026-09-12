"""``OrgOAuthProvider`` -- PrivacyFence's own minimal OAuth 2.1 authorization
server (P7), implementing the official MCP SDK's ``OAuthAuthorizationServerProvider``
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
what makes "the browser session and the MCP token are then provably
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

Resource controls (SEC-16): ``/register`` is unauthenticated by design -- that's what "dynamic"
means in DCR -- so this class, not the reverse proxy in front of it, is
the only thing standing between an anonymous POST loop and an unbounded
``oauth_clients.json``. ``register_client`` enforces a total-client cap
and a per-registration metadata-size cap (both raise ``RegistrationError``,
surfaced by the SDK's handler as RFC 7591's ``invalid_client_metadata``),
and opportunistically prunes clients nobody has authenticated as in
``_STALE_CLIENT_TTL_SECONDS`` before checking the cap. ``authorize()``
count-bounds ``_pending`` (not just TTL-expires it) so a burst of
authorize attempts can't grow that dict without limit inside one TTL
window. None of this replaces reverse-proxy rate-limiting in front of
``/register``/``/authorize``/``/token`` (see the org setup guide's own
section on it) -- it's the in-process backstop for whatever gets through.
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
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .. import org_identity
from ..org_identity import IdpConfig
from ..org_mode import AuthzPolicyConfig
from ..principal import Principal
from ..secure_files import atomic_write_json

logger = logging.getLogger(__name__)

CLIENTS_FILE_NAME = "oauth_clients.json"
IDP_CALLBACK_PATH = "/oauth/idp/callback"

_AUTHORIZATION_CODE_TTL_SECONDS = 5 * 60
_ACCESS_TOKEN_TTL_SECONDS = 60 * 60
_PENDING_AUTHORIZATION_TTL_SECONDS = 5 * 60

# SEC-16: DCR's
# ``/register`` endpoint is unauthenticated by design (RFC 7591 -- that's
# the whole point of *dynamic* registration) and, before this fix, had no
# resource controls at all: no cap on how many clients could pile up in
# ``oauth_clients.json``, no cap on how large one registration's metadata
# could be, and no way for a long-abandoned client to ever leave the store.
# These four constants are that hardening's knobs -- all in-process,
# same-process defenses, complementary to (not a replacement for) the
# reverse-proxy rate-limiting the org setup guide now recommends in front
# of ``/register``, ``/authorize`` and ``/token``.
#
# Total distinct clients this daemon will ever hold at once. Generous --
# real orgs register one Claude Desktop/Code client per employee machine,
# so a few hundred to a few thousand principals is the normal range this
# has to comfortably clear -- while still bounding the disk file DCR spam
# could otherwise grow without limit.
_MAX_REGISTERED_CLIENTS = 2000
# One registration's serialized metadata (client_name, redirect_uris,
# jwks, contacts, etc.) -- bounds the per-client storage a single
# unauthenticated POST to /register can claim. Real MCP clients' metadata
# is a few hundred bytes; this leaves headroom for legitimate variation
# (long client_name/logo_uri/jwks) without letting one registration write
# an arbitrarily large blob into oauth_clients.json.
_MAX_CLIENT_METADATA_BYTES = 8 * 1024
# A client nobody has authenticated as (no /authorize, /token or /revoke
# call that named it -- see get_client's last_used_at bump) for this long
# is pruned the next time a new client registers. Long enough that a
# legitimately-idle Claude connector (someone on leave, a machine turned
# off for weeks) is never at real risk of losing its registration; long
# enough that this is genuinely "stale", not "a client cap enforced via a
# side door". Losing a stale registration just means that client
# re-registers via DCR next time it's used, per this module's own
# docstring precedent for why none of DCR's state needs to be permanent.
_STALE_CLIENT_TTL_SECONDS = 180 * 24 * 60 * 60
# Global ceiling on concurrently in-flight (not-yet-completed) /authorize
# attempts, on top of the TTL-based _prune_pending above. The TTL alone
# bounds how long a stale entry survives, not how many can pile up
# *within* that window -- a client (or anyone hitting /authorize without
# even a registered client_id's worth of legitimacy) that fires
# /authorize in a tight loop can otherwise grow this dict without bound
# for up to _PENDING_AUTHORIZATION_TTL_SECONDS before the next prune.
_MAX_PENDING_AUTHORIZATIONS = 1000

# SEC-12: a hard cap
# on how long one continuous refresh-token *chain* may be used, regardless
# of how many times it's rotated. Rotation alone (see _mint_tokens'
# ``refresh_issued_at`` below) isn't an expiry -- a client that keeps
# refreshing forever never hits one -- so this is checked against the
# chain's original issuance time, carried forward unchanged across every
# rotation, not against each individual refresh token's own mint time.
# 30 days: long enough that an MCP client (Claude Desktop et al.) held open
# across normal use doesn't force a re-login through the IdP every session,
# short enough that a refresh token exfiltrated once doesn't stay a usable
# credential indefinitely the way it did before this fix.
_REFRESH_TOKEN_ABSOLUTE_LIFETIME_SECONDS = 30 * 24 * 60 * 60


class _OrgRefreshToken(RefreshToken):
    """Carries the principal's claims forward across a refresh so
    ``exchange_refresh_token`` can mint a fully-populated access token
    without a second, separate lookup keyed on ``subject`` (which
    wouldn't be enough on its own -- email/display_name/is_admin aren't
    derivable from a bare subject string).

    ``issued_at`` (SEC-12) is the refresh-token *chain's* original mint
    time -- set once, when the chain starts (``exchange_authorization_
    code``), and carried forward unchanged by every later rotation
    (``exchange_refresh_token``'s own ``refresh_issued_at`` argument to
    ``_mint_tokens``), so the absolute-lifetime check in
    ``load_refresh_token`` bounds the whole chain, not just however long
    the most recently rotated token has itself existed."""

    email: str = ""
    display_name: str = ""
    is_admin: bool = False
    issued_at: float = 0.0


@dataclass
class _StoredClient:
    """A DCR-registered client plus the bookkeeping SEC-16's stale-client
    pruning needs. ``last_used_at`` starts at registration time and is
    bumped by ``get_client`` -- called by the SDK's own handlers on every
    ``/authorize``, ``/token`` and ``/revoke`` request that names this
    client -- so it tracks actual use, not just how long ago DCR happened
    once."""

    info: OAuthClientInformationFull
    last_used_at: float


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

    def __init__(self, idp: IdpConfig, *, idp_callback_url: str, policy: AuthzPolicyConfig | None = None) -> None:
        self._idp = idp
        self._idp_callback_url = idp_callback_url
        # SEC-22: layered on top of the IdP dance below, same as web/
        # routes_org_identity.py's browser login -- see org_identity.
        # check_authz_policy's own docstring. Absent/disabled by default.
        self._policy = policy or AuthzPolicyConfig()
        self._clients_path = Path(_clients_file_path())
        self._lock = threading.Lock()
        self._clients: dict[str, _StoredClient] = self._load_clients()
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

    def _load_clients(self) -> dict[str, _StoredClient]:
        if not self._clients_path.exists():
            return {}
        try:
            raw = json.loads(self._clients_path.read_text(encoding="utf-8"))
            now = time.time()
            clients: dict[str, _StoredClient] = {}
            for client_id, data in raw.items():
                # New format (SEC-16): {"client": {...}, "last_used_at": ...}.
                # Old format (pre-SEC-16): the client's own fields directly,
                # at the top level -- still readable so an upgrade doesn't
                # drop every client an org already has registered. A client
                # loaded from the old format has no recorded last-use, so it
                # starts the clock now rather than being treated as already
                # stale (and immediately eligible for pruning) the moment
                # this daemon restarts on the new code.
                if isinstance(data, dict) and "client" in data and "last_used_at" in data:
                    info = OAuthClientInformationFull.model_validate(data["client"])
                    last_used_at = float(data["last_used_at"])
                else:
                    info = OAuthClientInformationFull.model_validate(data)
                    last_used_at = now
                clients[client_id] = _StoredClient(info=info, last_used_at=last_used_at)
            return clients
        except Exception as exc:
            logger.warning("Could not read %s: %s -- starting with no registered clients", self._clients_path, exc)
            return {}

    def _save_clients_locked(self) -> None:
        raw = {
            cid: {"client": json.loads(stored.info.model_dump_json()), "last_used_at": stored.last_used_at}
            for cid, stored in self._clients.items()
        }
        atomic_write_json(self._clients_path, raw, indent=2, sort_keys=True)

    def _prune_stale_clients_locked(self) -> bool:
        """SEC-16: a client nobody has authenticated as in
        ``_STALE_CLIENT_TTL_SECONDS`` is dropped -- called from
        ``register_client`` (mirroring ``authorize``'s own
        ``_prune_pending`` precedent: pruning piggybacks on the operation
        that would otherwise grow the store, rather than needing its own
        background scheduler). Returns whether anything was pruned, so the
        caller can persist the smaller store even on a path (e.g. the
        total-client cap still being hit right after pruning) that
        wouldn't otherwise write -- leaving a pruned client sitting in
        ``oauth_clients.json`` until some *other* registration happens to
        succeed would make the on-disk store outlive its own prune."""
        now = time.time()
        stale = [
            cid for cid, stored in self._clients.items()
            if (now - stored.last_used_at) > _STALE_CLIENT_TTL_SECONDS
        ]
        for cid in stale:
            del self._clients[cid]
        if stale:
            logger.info("Pruned %d stale DCR client(s) unused for over %d days", len(stale), _STALE_CLIENT_TTL_SECONDS // 86400)
        return bool(stale)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._lock:
            stored = self._clients.get(client_id)
            if stored is None:
                return None
            stored.last_used_at = time.time()
            self._save_clients_locked()
            return stored.info

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        metadata_bytes = len(client_info.model_dump_json().encode("utf-8"))
        if metadata_bytes > _MAX_CLIENT_METADATA_BYTES:
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description=f"registration metadata exceeds {_MAX_CLIENT_METADATA_BYTES} bytes",
            )
        with self._lock:
            pruned = self._prune_stale_clients_locked()
            if client_info.client_id not in self._clients and len(self._clients) >= _MAX_REGISTERED_CLIENTS:
                if pruned:
                    self._save_clients_locked()
                raise RegistrationError(
                    error="invalid_client_metadata",
                    error_description="this server has reached its registered-client limit; contact your administrator",
                )
            self._clients[client_info.client_id] = _StoredClient(info=client_info, last_used_at=time.time())
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
            # SEC-16: the TTL-based prune above bounds how long a pending
            # authorization survives, not how many can accumulate *within*
            # that window -- this bounds that too, so a burst of
            # /authorize calls can't grow this dict without limit before
            # the next natural prune.
            if len(self._pending) >= _MAX_PENDING_AUTHORIZATIONS:
                raise AuthorizeError(
                    error="temporarily_unavailable",
                    error_description="too many sign-ins in progress; please try again shortly",
                )
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
        # SEC-22: raises AuthorizationDenied (a plain exception, like every
        # other failure in this IdP leg) when the org's own allowlist
        # rejects an otherwise-legitimate IdP-authenticated principal --
        # web/routes_mcp.py's idp_callback route already wraps this whole
        # call in a catch-all that logs and returns a generic failure.
        org_identity.check_authz_policy(principal, claims, self._policy)

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
            # SEC-12: absolute lifetime, checked against the chain's
            # original issuance (see _OrgRefreshToken.issued_at), not this
            # particular token's own mint time -- same fail-closed-and-
            # revoke posture as load_access_token's expiry check below.
            if (time.time() - rt.issued_at) > _REFRESH_TOKEN_ABSOLUTE_LIFETIME_SECONDS:
                self._revoke_pair_locked(access_token=None, refresh_token_str=refresh_token)
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
            resource=None, principal=principal, refresh_issued_at=refresh_token.issued_at,
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
        refresh_issued_at: float | None = None,
    ) -> OAuthToken:
        access_token_str = secrets.token_urlsafe(32)
        refresh_token_str = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + _ACCESS_TOKEN_TTL_SECONDS
        # A fresh chain (exchange_authorization_code) starts its own clock
        # now; a rotation (exchange_refresh_token) passes the chain's
        # original issuance through unchanged -- see _OrgRefreshToken's
        # own docstring for why that's what SEC-12's absolute cap needs.
        issued_at = time.time() if refresh_issued_at is None else refresh_issued_at
        claims = {"email": principal.email, "display_name": principal.display_name, "is_admin": principal.is_admin}
        with self._lock:
            self._access_tokens[access_token_str] = AccessToken(
                token=access_token_str, client_id=client_id, scopes=scopes, expires_at=expires_at,
                resource=resource, subject=principal.id, claims=claims,
            )
            self._refresh_tokens[refresh_token_str] = _OrgRefreshToken(
                token=refresh_token_str, client_id=client_id, scopes=scopes, expires_at=None,
                subject=principal.id, email=principal.email, display_name=principal.display_name,
                is_admin=principal.is_admin, issued_at=issued_at,
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
