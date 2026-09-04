"""Shared Google OAuth 2.0 helper for org mode's server-redirect flow (P8,
docs/https-connector-refactor-plan.md §9.3).

Local mode keeps using ``google-auth-oauthlib``'s own ``InstalledAppFlow``
loopback implementation directly -- each of gmail_client.py/drive_client.py/
calendar_client.py/contacts_client.py/tasks_client.py's own
``authorize_interactive()`` is unchanged by this module and calls
``flow.run_local_server(port=0)`` exactly as before. This module is
additive, used only by ``web/routes_connect.py``'s org-mode routes, where a
remote browser (a phone, say) can't have PrivacyFence open a local port and
a local browser window on its own behalf -- see ``oauth_loopback.py``'s own
module docstring for why that assumption breaks down.

``google_auth_oauthlib.flow.Flow`` is the lower-level counterpart of
``InstalledAppFlow`` that takes an explicit ``redirect_uri`` instead of
managing a loopback listener itself -- exactly the plan document's own
words for this phase ("Google's InstalledAppFlow becomes google_auth_
oauthlib.flow.Flow with an explicit redirect_uri"). ``Flow`` auto-generates
its own PKCE ``code_verifier``/``code_challenge`` pair (see its own
``authorization_url()``), so unlike the Slack/Salesforce/Atlassian helpers
this module has no ``code_challenge`` parameter of its own to plumb through
-- callers just need to persist ``Flow.code_verifier`` between the "start"
and "callback" requests (two separate HTTP requests, and therefore two
separate ``Flow`` instances) and pass it back in on the second one.

Google Cloud Console OAuth clients are typed at creation ("Desktop app" vs.
"Web application"); only a "Web application" client can have an arbitrary
HTTPS redirect URI registered against it. An org running ``mode: org``
needs its ``org_config.json`` "google" section to hold a *Web application*
OAuth client's credentials (registered with ``{issuer_url}/oauth/callback/
<service>`` for each Google connector), separate from whatever "Desktop
app" client an install might otherwise use for local mode's loopback flow
-- ``_web_client_config`` below wraps the same flat "google" section
daemon_main.py's own ``_google_client_config`` reads, just under the "web"
top-level key ``Flow.from_client_config`` needs instead of "installed".
"""
from __future__ import annotations

import logging
import os
from typing import Any

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

logger = logging.getLogger(__name__)


class GoogleOAuthError(Exception):
    """Raised for unrecoverable problems in the org-mode server-redirect flow."""


def web_client_config(google_org: dict[str, Any]) -> dict[str, Any]:
    """Wraps ``org_config.json``'s flat "google" section into the "web"
    client-config shape ``Flow.from_client_config`` requires (see module
    docstring). Returns ``{}`` if the required fields aren't present --
    same "connector skipped, not fatal" posture every other missing-config
    check in this codebase takes."""
    if not google_org.get("client_id") or not google_org.get("client_secret"):
        return {}
    if not google_org.get("auth_uri") or not google_org.get("token_uri"):
        return {}
    return {"web": google_org}


def build_flow(client_config: dict[str, Any], scopes: list[str], redirect_uri: str) -> Flow:
    return Flow.from_client_config(client_config, scopes=scopes, redirect_uri=redirect_uri)


def authorize_url(client_config: dict[str, Any], scopes: list[str], redirect_uri: str, state: str) -> tuple[str, str]:
    """Returns ``(authorize_url, code_verifier)`` -- the caller must persist
    ``code_verifier`` (keyed by ``state``) and hand it back to
    ``exchange_code`` below on the matching callback request."""
    flow = build_flow(client_config, scopes, redirect_uri)
    url, _ = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent", state=state)
    assert flow.code_verifier is not None  # Flow.authorization_url() always sets it (autogenerate_code_verifier=True)
    return url, flow.code_verifier


def exchange_code(
    client_config: dict[str, Any], scopes: list[str], redirect_uri: str, code: str, code_verifier: str,
) -> Credentials:
    """Exchanges an authorization code for Google credentials. Raises
    ``GoogleOAuthError`` on failure -- ``Flow.fetch_token`` itself raises
    whatever ``requests_oauthlib``/``oauthlib`` raise for a rejected
    exchange (an expired/reused code, a redirect_uri mismatch, ...), which
    isn't a stable, user-presentable type on its own."""
    flow = Flow.from_client_config(
        client_config, scopes=scopes, redirect_uri=redirect_uri,
        code_verifier=code_verifier, autogenerate_code_verifier=False,
    )
    try:
        flow.fetch_token(code=code)
    except Exception as exc:  # noqa: BLE001 -- any provider-side failure ends the same way
        raise GoogleOAuthError(f"Google OAuth exchange failed: {exc}") from exc
    return flow.credentials


def save_credentials(token_file: str, creds: Credentials) -> None:
    """Same file format ``GmailClient._save_token``/etc. write and
    ``Credentials.from_authorized_user_file`` reads back -- a token
    obtained through this module's server-redirect flow is indistinguishable
    on disk from one obtained through the local-mode loopback flow."""
    os.makedirs(os.path.dirname(os.path.abspath(token_file)), exist_ok=True)
    with open(token_file, "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())
    try:
        os.chmod(token_file, 0o600)
    except OSError:  # pragma: no cover - best effort on non-POSIX
        logger.debug("Could not chmod Google token file (non-fatal)")


__all__ = [
    "GoogleOAuthError",
    "authorize_url",
    "build_flow",
    "exchange_code",
    "save_credentials",
    "web_client_config",
]
