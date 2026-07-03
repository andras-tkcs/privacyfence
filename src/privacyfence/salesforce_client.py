"""Salesforce REST API client.

Uses the `simple-salesforce` library if available; otherwise raises a clear
error. Authentication is OAuth 2.0 (Web Server Flow with PKCE), driven from the
PrivacyFence menu bar via ``authorize_interactive`` below — no username/
password/security-token entry. The Connected App (consumer key/secret) is
organization-level config installed via "Install/Update Organization Config…";
the resulting access/refresh token is per-user, stored in a token file.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, TypeVar
from urllib.parse import urlencode

import requests

from .oauth_loopback import OAuthLoopbackError, run_browser_oauth

logger = logging.getLogger(__name__)

SALESFORCE_OAUTH_PORT = 53683
SALESFORCE_REDIRECT_PATH = "/callback"
DEFAULT_LOGIN_URL = "https://login.salesforce.com"
DEFAULT_SCOPES = "api refresh_token"

T = TypeVar("T")


class SalesforceClientError(Exception):
    """Raised for unrecoverable Salesforce client problems (config, API)."""


@dataclass
class SalesforceReport:
    id: str
    name: str
    report_type: str
    folder_name: str
    description: str


@dataclass
class SalesforceRecord:
    object_type: str
    id: str
    fields: dict


def authorize_interactive(
    consumer_key: str,
    consumer_secret: str,
    token_file: str,
    login_url: str = DEFAULT_LOGIN_URL,
    port: int = SALESFORCE_OAUTH_PORT,
) -> dict[str, Any]:
    """Run Salesforce's OAuth 2.0 Web Server flow and persist the token.

    ``consumer_key``/``consumer_secret``/``login_url`` come from the
    organization config bundle (the Connected App IT registered). Returns the
    saved token record; raises ``SalesforceClientError`` on failure.
    """
    login_url = (login_url or DEFAULT_LOGIN_URL).rstrip("/")

    def build_authorize_url(redirect_uri: str, state: str, code_challenge: str) -> str:
        params = {
            "response_type": "code",
            "client_id": consumer_key,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": DEFAULT_SCOPES,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{login_url}/services/oauth2/authorize?" + urlencode(params)

    def exchange(code: str, redirect_uri: str, code_verifier: str) -> dict[str, Any]:
        try:
            resp = requests.post(
                f"{login_url}/services/oauth2/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": consumer_key,
                    "client_secret": consumer_secret,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                },
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise SalesforceClientError(f"Salesforce OAuth exchange failed: {exc}") from exc
        return resp.json()

    try:
        response = run_browser_oauth(
            build_authorize_url,
            exchange,
            port=port,
            path=SALESFORCE_REDIRECT_PATH,
            redirect_host="localhost",
        )
    except OAuthLoopbackError as exc:
        raise SalesforceClientError(f"Salesforce sign-in failed: {exc}") from exc

    access_token = response.get("access_token", "")
    instance_url = response.get("instance_url", "")
    if not access_token or not instance_url:
        raise SalesforceClientError(f"Salesforce OAuth did not return a usable token: {response}")

    token_record = {
        "access_token": access_token,
        "refresh_token": response.get("refresh_token", ""),
        "instance_url": instance_url,
    }
    _save_token_file(token_file, token_record)
    logger.info("Salesforce OAuth complete for instance %s", instance_url)
    return token_record


def load_token_file(token_file: str) -> dict[str, Any]:
    """Load a previously saved Salesforce token record, or raise SalesforceClientError."""
    if not os.path.exists(token_file):
        raise SalesforceClientError(
            f"No Salesforce token found at '{token_file}'. Use Authenticate… in "
            "the PrivacyFence menu bar to sign in."
        )
    with open(token_file, encoding="utf-8") as fh:
        return json.load(fh)


def _save_token_file(token_file: str, token_record: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(token_file)), exist_ok=True)
    with open(token_file, "w", encoding="utf-8") as fh:
        json.dump(token_record, fh)
    try:
        os.chmod(token_file, 0o600)
    except OSError:  # pragma: no cover - best effort on non-POSIX
        logger.debug("Could not chmod Salesforce token file (non-fatal)")


def _is_expired_session_error(exc: Exception) -> bool:
    text = str(exc)
    return "INVALID_SESSION_ID" in text or "Session expired" in text


class SalesforceClient:
    """Salesforce client backed by simple-salesforce, authenticated via OAuth.

    ``config`` merges organization-level Connected App credentials
    (``consumer_key``, ``consumer_secret``, ``login_url``) with the per-user
    token (``access_token``, ``refresh_token``, ``instance_url``). When the
    access token expires mid-session, the client refreshes it once and
    retries automatically; if ``token_file`` is given, the refreshed token is
    persisted back to disk.
    """

    def __init__(self, config: dict[str, Any], token_file: str | None = None) -> None:
        self._config = dict(config)
        self._token_file = token_file
        self._sf = None  # lazily initialized

    def _build_sf(self):
        try:
            from simple_salesforce import Salesforce
        except ImportError as exc:
            raise SalesforceClientError(
                "The 'simple-salesforce' package is not installed. "
                "Run: pip install simple-salesforce"
            ) from exc

        access_token = self._config.get("access_token", "")
        instance_url = self._config.get("instance_url", "")
        if not access_token or not instance_url:
            raise SalesforceClientError(
                "Salesforce is not authenticated. Use Authenticate… in the "
                "PrivacyFence menu bar to sign in."
            )
        instance = instance_url.replace("https://", "").replace("http://", "").rstrip("/")
        try:
            return Salesforce(instance=instance, session_id=access_token)
        except Exception as exc:
            raise SalesforceClientError(f"Salesforce authentication failed: {exc}") from exc

    def _get_sf(self):
        if self._sf is None:
            self._sf = self._build_sf()
        return self._sf

    def _try_refresh(self) -> bool:
        """Attempt to refresh the access token in place. Returns True on success."""
        refresh_token = self._config.get("refresh_token", "")
        consumer_key = self._config.get("consumer_key", "")
        consumer_secret = self._config.get("consumer_secret", "")
        login_url = (self._config.get("login_url") or DEFAULT_LOGIN_URL).rstrip("/")
        if not refresh_token or not consumer_key or not consumer_secret:
            return False
        try:
            resp = requests.post(
                f"{login_url}/services/oauth2/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": consumer_key,
                    "client_secret": consumer_secret,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            logger.warning("Salesforce token refresh failed: %s", exc)
            return False

        self._config["access_token"] = data.get("access_token", self._config.get("access_token"))
        self._config["instance_url"] = data.get("instance_url", self._config.get("instance_url"))
        self._sf = None
        if self._token_file:
            _save_token_file(self._token_file, {
                "access_token": self._config["access_token"],
                "refresh_token": refresh_token,
                "instance_url": self._config["instance_url"],
            })
        logger.info("Salesforce access token refreshed")
        return True

    def _call(self, fn: Callable[[Any], T]) -> T:
        """Run ``fn(sf)`` with one automatic refresh-and-retry on an expired session."""
        try:
            return fn(self._get_sf())
        except SalesforceClientError:
            raise
        except Exception as exc:
            if _is_expired_session_error(exc) and self._try_refresh():
                try:
                    return fn(self._get_sf())
                except Exception as retry_exc:
                    raise SalesforceClientError(str(retry_exc)) from retry_exc
            raise SalesforceClientError(str(exc)) from exc

    def check_connection(self) -> str:
        """Verify credentials. Returns the org name."""
        def _run(sf):
            result = sf.query("SELECT Id, Name FROM Organization LIMIT 1")
            records = result.get("records", [])
            return records[0].get("Name", "unknown") if records else "unknown"

        org_name = self._call(_run)
        logger.info("Connected to Salesforce org: %s", org_name)
        return org_name

    def list_reports(self) -> list[SalesforceReport]:
        """List reports accessible to the authenticated user."""
        def _run(sf):
            return sf.query(
                "SELECT Id, Name, Description, FolderName, DeveloperName "
                "FROM Report ORDER BY Name LIMIT 200"
            )

        result = self._call(_run)
        reports = [
            SalesforceReport(
                id=raw.get("Id", ""),
                name=raw.get("Name", ""),
                report_type=raw.get("DeveloperName", ""),
                folder_name=raw.get("FolderName", ""),
                description=raw.get("Description", ""),
            )
            for raw in result.get("records", [])
        ]
        logger.info("list_reports returned %d report(s)", len(reports))
        return reports

    def get_record(self, object_type: str, record_id: str) -> SalesforceRecord:
        """Fetch a single record by object type and id."""
        if not object_type or not record_id:
            raise SalesforceClientError("get_record requires object_type and record_id")

        def _run(sf):
            try:
                obj = getattr(sf, object_type)
            except AttributeError as exc:
                raise SalesforceClientError(f"Unknown Salesforce object type: {object_type!r}") from exc
            return obj.get(record_id)

        raw = self._call(_run)
        fields = {k: v for k, v in raw.items() if not k.startswith("attributes")}
        return SalesforceRecord(object_type=object_type, id=record_id, fields=fields)

    def run_report(self, report_id: str) -> dict:
        """Run a Salesforce report and return its result as a dict."""
        if not report_id:
            raise SalesforceClientError("run_report requires a report_id")

        def _run(sf):
            return sf.restful(
                f"analytics/reports/{report_id}",
                method="POST",
                json={"reportMetadata": {}},
            )

        result = self._call(_run)
        logger.info("run_report %s completed", report_id)
        return result
