"""Google Apps Script API client.

Handles OAuth2 authorization and read/write access to a script project's
*source* (the `.gs`/`.html` files plus the `appsscript.json` manifest), and
best-effort read access to the result of a run the user triggered themselves.

Running a script is deliberately out of scope for this client and for
PrivacyFence entirely -- see ``connectors/apps_script.py``'s module docstring
for why. There is no ``run`` method here, and there never should be one added
without a fresh threat-model discussion (see issue #154's "Non-goals").

The Apps Script API itself has no "list my script projects" endpoint, so
``list_projects`` goes through the Drive API instead (standalone script
projects show up there with mime type
``application/vnd.google-apps.script``) -- hence the extra
``drive.metadata.readonly`` scope below, kept as narrow as it can be (just
enough to list/name script projects, not read Drive file content).
``get_execution_log`` uses the Processes API's ``listScriptProcesses``
(open question 1(b) in issue #154): status/duration/function name per
recent run, not a full ``console.log`` transcript -- the Apps Script
editor's "Executions" panel transcript would need the script bound to a
standard, Cloud-Logging-enabled GCP project plus a `logging.read` scope,
extra per-script user setup this client deliberately doesn't require.

Per project conventions we always use the documented Google client libraries
(`googleapiclient`, `google.auth`) and authenticate via the standard
google-auth-oauthlib installed-app flow.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/script.projects",      # read/write project source
    "https://www.googleapis.com/auth/script.processes",     # listScriptProcesses (execution status)
    "https://www.googleapis.com/auth/drive.metadata.readonly",  # list_projects only -- see module docstring
]

# Google Apps Script file types accepted by projects.updateContent -- every
# file in a write_content call must be one of these, same set the Apps
# Script editor itself works with.
VALID_FILE_TYPES = ("SERVER_JS", "HTML", "JSON")


class AppsScriptClientError(Exception):
    """Raised for unrecoverable Apps Script client problems (auth, config, API)."""


@dataclass
class ScriptProject:
    """A script project's identity/metadata -- no source, see ScriptContent."""

    id: str
    name: str
    created_time: str = ""
    modified_time: str = ""

    def short_summary(self) -> str:
        return self.name or self.id


@dataclass
class ScriptFile:
    name: str
    type: str  # one of VALID_FILE_TYPES
    source: str


@dataclass
class ScriptContent:
    script_id: str
    files: list[ScriptFile] = field(default_factory=list)


@dataclass
class ScriptExecution:
    """One row of ``processes.listScriptProcesses`` -- status/duration only,
    not a console.log transcript (see module docstring)."""

    function_name: str
    status: str  # e.g. COMPLETED / FAILED / RUNNING / CANCELED / TIMED_OUT
    start_time: str
    duration: str  # Google Duration string, e.g. "1.234s" -- already human-readable
    process_type: str = ""


class AppsScriptClient:
    """Google Apps Script client with OAuth2 token caching."""

    def __init__(self, client_config: dict, token_file: str) -> None:
        self._client_config = client_config
        self._token_file = token_file
        # googleapiclient service objects (and the httplib2 transport they
        # wrap) are not thread-safe -- see DriveClient's own comment on this
        # same pattern. Requests are dispatched to a thread per call
        # (connectors/apps_script.py._fetch), so each thread gets its own
        # service instance rather than sharing one.
        self._local = threading.local()
        self._creds_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Authentication
    # ------------------------------------------------------------------ #

    def authorize_interactive(self) -> None:
        """Run the interactive OAuth flow and persist the token.

        ``client_config`` comes from the organization config bundle (installed
        via the menu bar) -- the same Google OAuth client Drive/Gmail/etc.
        already use, just with this connector's own scopes and its own
        cached token file.
        """
        if not self._client_config:
            raise AppsScriptClientError(
                "No Google organization config installed. Install/Update "
                "Organization Config from the PrivacyFence menu bar first."
            )
        logger.info("Starting Apps Script interactive OAuth flow")
        flow = InstalledAppFlow.from_client_config(self._client_config, SCOPES)
        creds = flow.run_local_server(port=0)
        self._save_token(creds)
        logger.info("Apps Script OAuth token saved to '%s'", self._token_file)

    def _load_credentials(self) -> Credentials:
        # Guards concurrent refresh/save of the shared token file when
        # multiple threads hit an expired token at the same time.
        with self._creds_lock:
            if not os.path.exists(self._token_file):
                raise AppsScriptClientError(
                    f"No OAuth token found at '{self._token_file}'. "
                    "Run the application once with '--apps-script-oauth' to authorize."
                )
            creds = Credentials.from_authorized_user_file(self._token_file, SCOPES)
            if creds.valid:
                return creds
            if creds.expired and creds.refresh_token:
                logger.info("Refreshing expired Apps Script OAuth token")
                try:
                    creds.refresh(Request())
                except Exception as exc:  # noqa: BLE001 - surface a clear message
                    raise AppsScriptClientError(
                        f"Failed to refresh OAuth token: {exc}. "
                        "Re-run with '--apps-script-oauth' to re-authorize."
                    ) from exc
                self._save_token(creds)
                return creds
            raise AppsScriptClientError(
                "Cached OAuth token is invalid and cannot be refreshed. "
                "Re-run with '--apps-script-oauth' to re-authorize."
            )

    def _save_token(self, creds: Credentials) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self._token_file)), exist_ok=True)
        with open(self._token_file, "w", encoding="utf-8") as handle:
            handle.write(creds.to_json())
        try:
            os.chmod(self._token_file, 0o600)
        except OSError:  # pragma: no cover - best effort on non-POSIX
            logger.debug("Could not chmod token file (non-fatal)")

    def _get_service(self):
        """Build (or reuse) the Apps Script API service resource for this thread."""
        service = getattr(self._local, "service", None)
        if service is None:
            creds = self._load_credentials()
            service = build("script", "v1", credentials=creds, cache_discovery=False)
            self._local.service = service
            logger.debug("Apps Script API service initialized for thread %s", threading.current_thread().name)
        return service

    def _get_drive_service(self):
        """Build (or reuse) a Drive API service resource for this thread --
        used only by list_projects (no Apps Script API endpoint lists
        projects) and check_connection, both read-only and both within the
        drive.metadata.readonly scope this client requests."""
        service = getattr(self._local, "drive_service", None)
        if service is None:
            creds = self._load_credentials()
            service = build("drive", "v3", credentials=creds, cache_discovery=False)
            self._local.drive_service = service
            logger.debug("Drive API (metadata) service initialized for thread %s", threading.current_thread().name)
        return service

    def check_connection(self) -> str:
        """Verify the credentials work. Returns the authorized email address."""
        try:
            about = self._get_drive_service().about().get(fields="user").execute()
        except HttpError as exc:
            raise AppsScriptClientError(f"Apps Script connection check failed: {exc}") from exc
        email = about.get("user", {}).get("emailAddress", "unknown")
        logger.info("Connected to Apps Script as %s", email)
        return email

    # ------------------------------------------------------------------ #
    # Read operations
    # ------------------------------------------------------------------ #

    def list_projects(self, max_results: int = 50) -> list[ScriptProject]:
        """List standalone Apps Script projects visible to the user.

        Goes through the Drive API (mimeType filter), not the Apps Script
        API -- see module docstring. Container-bound scripts (attached to a
        Sheet/Doc/Form) don't show up here; only standalone script projects
        do.
        """
        max_results = max(1, min(max_results, 1000))
        service = self._get_drive_service()
        try:
            response = (
                service.files()
                .list(
                    q="mimeType='application/vnd.google-apps.script' and trashed=false",
                    pageSize=max_results,
                    fields="files(id,name,createdTime,modifiedTime)",
                )
                .execute()
            )
        except HttpError as exc:
            raise AppsScriptClientError(f"list_projects failed: {exc}") from exc
        projects = [
            ScriptProject(
                id=raw.get("id", ""),
                name=raw.get("name", ""),
                created_time=raw.get("createdTime", ""),
                modified_time=raw.get("modifiedTime", ""),
            )
            for raw in response.get("files", [])
        ]
        logger.info("list_projects returned %d project(s)", len(projects))
        return projects

    def get_project_metadata(self, script_id: str) -> ScriptProject:
        """Fetch one script project's title/create/update time via the Apps
        Script API's own projects.get -- the source of truth for a single
        project's name, used to build review/popup previews."""
        if not script_id:
            raise AppsScriptClientError("get_project_metadata requires a non-empty script_id")
        service = self._get_service()
        try:
            raw = service.projects().get(scriptId=script_id).execute()
        except HttpError as exc:
            raise AppsScriptClientError(f"get_project_metadata({script_id}) failed: {exc}") from exc
        return ScriptProject(
            id=raw.get("scriptId", script_id),
            name=raw.get("title", "") or script_id,
            created_time=raw.get("createTime", ""),
            modified_time=raw.get("updateTime", ""),
        )

    def get_content(self, script_id: str) -> ScriptContent:
        """Fetch a script project's full source: every file (`.gs`/`.html`)
        plus the `appsscript.json` manifest."""
        if not script_id:
            raise AppsScriptClientError("get_content requires a non-empty script_id")
        service = self._get_service()
        try:
            raw = service.projects().getContent(scriptId=script_id).execute()
        except HttpError as exc:
            raise AppsScriptClientError(f"get_content({script_id}) failed: {exc}") from exc
        files = [
            ScriptFile(name=f.get("name", ""), type=f.get("type", ""), source=f.get("source", ""))
            for f in raw.get("files", [])
        ]
        logger.info("get_content %s: %d file(s)", script_id, len(files))
        return ScriptContent(script_id=script_id, files=files)

    def get_execution_log(self, script_id: str, max_results: int = 10) -> list[ScriptExecution]:
        """Return the result of the most recent run(s) of this script that
        the user triggered themselves, outside PrivacyFence (status,
        duration, which function ran) -- not a live console.log transcript,
        see module docstring."""
        if not script_id:
            raise AppsScriptClientError("get_execution_log requires a non-empty script_id")
        max_results = max(1, min(max_results, 50))
        service = self._get_service()
        try:
            result = (
                service.processes()
                .listScriptProcesses(scriptId=script_id, pageSize=max_results)
                .execute()
            )
        except HttpError as exc:
            raise AppsScriptClientError(f"get_execution_log({script_id}) failed: {exc}") from exc
        executions = [
            ScriptExecution(
                function_name=p.get("functionName", "") or "(unknown)",
                status=p.get("processStatus", "") or "(unknown)",
                start_time=p.get("startTime", ""),
                duration=p.get("duration", "") or "(unknown)",
                process_type=p.get("processType", ""),
            )
            for p in result.get("processes", [])
        ]
        logger.info("get_execution_log %s: %d execution(s)", script_id, len(executions))
        return executions

    # ------------------------------------------------------------------ #
    # Write operations
    # ------------------------------------------------------------------ #

    def write_content(self, script_id: str, files: list[dict]) -> dict:
        """Replace a script project's entire file set -- there is no
        partial/single-file update in the underlying updateContent API, so,
        like drive_write_doc_content, this always replaces everything
        rather than patching one file."""
        if not script_id:
            raise AppsScriptClientError("write_content requires a non-empty script_id")
        if not files:
            raise AppsScriptClientError("write_content requires at least one file")
        normalized = []
        for f in files:
            name = str(f.get("name") or "").strip()
            file_type = str(f.get("type") or "").strip().upper()
            source = f.get("source", "")
            if not name or file_type not in VALID_FILE_TYPES:
                raise AppsScriptClientError(
                    "write_content: each file needs a non-empty 'name' and a "
                    f"'type' of {'/'.join(VALID_FILE_TYPES)}, got name={name!r} type={file_type!r}"
                )
            normalized.append({"name": name, "type": file_type, "source": source})
        service = self._get_service()
        try:
            service.projects().updateContent(
                scriptId=script_id, body={"files": normalized}
            ).execute()
        except HttpError as exc:
            raise AppsScriptClientError(f"write_content({script_id}) failed: {exc}") from exc
        logger.info("write_content %s: %d file(s)", script_id, len(normalized))
        return {"script_id": script_id, "file_count": len(normalized)}
