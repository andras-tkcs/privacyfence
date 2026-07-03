"""Shared Atlassian OAuth 2.0 (3LO) helper for Jira + Confluence.

One Atlassian OAuth app (organization-level config, installed via the
"Install/Update Organization Config…" menu bar action) covers both products —
a single browser consent grants access to whichever Atlassian site the user
picks, and the resulting token is shared by jira_client.py and
confluence_client.py (``credentials/atlassian_token.json``).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable
from urllib.parse import urlencode

import requests

from .oauth_loopback import OAuthLoopbackError, run_browser_oauth

logger = logging.getLogger(__name__)

ATLASSIAN_OAUTH_PORT = 53684
ATLASSIAN_REDIRECT_PATH = "/callback"

AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"
ACCESSIBLE_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"

# Jira stays on classic scopes — its endpoints work fine with them, and
# Atlassian's own guidance is to prefer classic scopes for Jira where
# available. Confluence must use granular scopes: its v2 API (used for space
# listing — see confluence_client.py) only accepts granular-scoped tokens and
# 401s ("scope does not match") on classic ones. These are independent scope
# namespaces per product, so mixing classic Jira + granular Confluence scopes
# in one authorize request is fine — Atlassian tracks them separately (see
# the per-resource "scopes" list in the accessible-resources response).
DEFAULT_SCOPES: list[str] = [
    "read:jira-work", "write:jira-work", "read:jira-user",
    "read:space:confluence", "read:page:confluence", "write:page:confluence",
    "read:content:confluence", "search:confluence",
    "offline_access",
]


class AtlassianOAuthError(Exception):
    """Raised for unrecoverable Atlassian OAuth problems."""


def authorize_interactive(
    client_id: str,
    client_secret: str,
    token_file: str,
    scopes: list[str] | None = None,
    port: int = ATLASSIAN_OAUTH_PORT,
    pick_resource: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run Atlassian's OAuth 2.0 (3LO) browser flow and persist the token.

    ``client_id``/``client_secret`` come from the organization config bundle.
    ``pick_resource`` is called with the list of Atlassian sites this account
    can access when there is more than one; it must return the chosen entry.
    Returns the saved token record; raises ``AtlassianOAuthError`` on failure.
    """
    scope_str = " ".join(scopes or DEFAULT_SCOPES)

    def build_authorize_url(redirect_uri: str, state: str, code_challenge: str) -> str:
        params = {
            "audience": "api.atlassian.com",
            "client_id": client_id,
            "scope": scope_str,
            "redirect_uri": redirect_uri,
            "state": state,
            "response_type": "code",
            "prompt": "consent",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTHORIZE_URL}?" + urlencode(params)

    def exchange(code: str, redirect_uri: str, code_verifier: str) -> dict[str, Any]:
        try:
            resp = requests.post(
                TOKEN_URL,
                json={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                },
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise AtlassianOAuthError(f"Atlassian OAuth exchange failed: {exc}") from exc
        return resp.json()

    try:
        response = run_browser_oauth(
            build_authorize_url, exchange, port=port, path=ATLASSIAN_REDIRECT_PATH
        )
    except OAuthLoopbackError as exc:
        raise AtlassianOAuthError(f"Atlassian sign-in failed: {exc}") from exc

    access_token = response.get("access_token", "")
    refresh_token = response.get("refresh_token", "")
    if not access_token:
        raise AtlassianOAuthError(f"Atlassian OAuth did not return an access token: {response}")

    resources = _fetch_accessible_resources(access_token)
    if not resources:
        raise AtlassianOAuthError("No accessible Atlassian sites were returned for this account.")
    if len(resources) == 1:
        resource = resources[0]
    elif pick_resource is not None:
        resource = pick_resource(resources)
    else:
        sites = ", ".join(r.get("url", "?") for r in resources)
        raise AtlassianOAuthError(
            f"Multiple Atlassian sites are accessible ({sites}); pass pick_resource to choose one."
        )

    cloud_id = resource.get("id", "")
    token_record = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "cloud_id": cloud_id,
        "site_url": resource.get("url", ""),
        "account_email": _fetch_account_email(access_token, cloud_id),
    }
    _save_token_file(token_file, token_record)
    logger.info("Atlassian OAuth complete for site %s", token_record["site_url"])
    return token_record


def refresh(client_id: str, client_secret: str, refresh_token: str) -> dict[str, Any]:
    """Exchange a refresh token for a new access token (Atlassian rotates refresh tokens)."""
    try:
        resp = requests.post(
            TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            },
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise AtlassianOAuthError(f"Atlassian token refresh failed: {exc}") from exc
    return resp.json()


def load_token_file(token_file: str) -> dict[str, Any]:
    """Load a previously saved Atlassian token record, or raise AtlassianOAuthError."""
    if not os.path.exists(token_file):
        raise AtlassianOAuthError(
            f"No Atlassian token found at '{token_file}'. Use Authenticate… in "
            "the PrivacyFence menu bar to sign in."
        )
    with open(token_file, encoding="utf-8") as fh:
        return json.load(fh)


def _fetch_account_email(access_token: str, cloud_id: str) -> str:
    """Best-effort lookup of the signed-in account's email via Jira's ``myself``.

    Used to populate connector.my_email for auto-accept rules (i_am_owner /
    i_am_organizer / …) shared by the Jira and Confluence connectors. Never
    raises — an empty string just means those rules won't match.
    """
    try:
        resp = requests.get(
            f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/myself",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("emailAddress", "")
    except requests.RequestException as exc:
        logger.debug("Could not resolve Atlassian account email (non-fatal): %s", exc)
        return ""


def _fetch_accessible_resources(access_token: str) -> list[dict[str, Any]]:
    try:
        resp = requests.get(
            ACCESSIBLE_RESOURCES_URL,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise AtlassianOAuthError(f"Could not list accessible Atlassian sites: {exc}") from exc
    return resp.json()


def _save_token_file(token_file: str, token_record: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(token_file)), exist_ok=True)
    with open(token_file, "w", encoding="utf-8") as fh:
        json.dump(token_record, fh)
    try:
        os.chmod(token_file, 0o600)
    except OSError:  # pragma: no cover - best effort on non-POSIX
        logger.debug("Could not chmod Atlassian token file (non-fatal)")
