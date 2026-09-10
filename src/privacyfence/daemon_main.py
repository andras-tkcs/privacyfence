"""PrivacyFence daemon: persistent background process that owns credentials,
connectors, and the embedded web approval/config UI.

Started at login (LaunchAgent on macOS: com.privacyfence.app.plist), or
automatically by Claude Desktop's ``.mcpb`` shim on first use. Only one
instance is allowed (enforced via a lock file). Claude reaches this process
over the embedded ``/mcp`` Streamable HTTP endpoint (see
web/mcp_dispatch.py's module docstring) -- the original bridge/IPC-socket
transport was retired at P5 (docs/https-connector-refactor-plan.md §12);
``connector_host.py``'s ``ConnectorHost`` is what's left of
``ipc_server.py``'s own role once the socket and its dispatch logic are
gone. A human reaches it the same way: over the embedded web approval/
settings surfaces (``/approvals``, and ``/settings`` when
``web.settings.enabled``) -- through P9 there was also a native macOS
AppKit UI (a menu bar tray icon, native approval dialogs, a native webview
settings window); P10 deleted all of it (§12, decision D6 in §15: "two
approval surfaces means two places for a security fix to land"), so the
web surface is now the only one, on every platform this process runs on.

Threading model:
  - Main thread:   waits on ``_wait_for_shutdown()`` (below) until
    ``request_shutdown()`` is called (the web settings page's "Quit
    PrivacyFence" action) or the process receives SIGINT/Ctrl-C -- there is
    no run loop of its own to host anymore, since every Claude-facing and
    human-facing surface already runs on the web thread below.
  - Web thread:    uvicorn serving the embedded HTTP server (web/server.py)
    that hosts ``/mcp`` and the web approval/settings surfaces -- this is
    also the event loop every connector call now actually runs on.
  - Update timer:  a background thread pulsing ``SettingsController.
    on_update_check_timer()`` every few hours (see
    UPDATE_CHECK_TIMER_INTERVAL_SECONDS below) -- the direct successor of
    the old menu bar's own ``rumps.Timer``.
  - Cache warm:    short-lived background thread(s) started right after the
    web server's event loop is known, refreshing Slack/Telegram directory
    caches if they've gone stale -- see _warm_connector_caches(). Kept off
    the main thread so a large workspace/account doesn't delay startup.

Configuration is split into two files (see paths.py):
  - ``org/org_config.json``    — organization-level app registrations (Google
    OAuth client, Slack app, Salesforce Connected App, Atlassian OAuth app),
    installed via PrivacyFence Settings (``/settings``, or by hand-editing
    this file). Optional per service; a connector is offered only if its
    section is present. Telegram's api_id/api_hash are the one exception:
    they identify the PrivacyFence app itself (not an organization) and are
    baked into the release build — see app_credentials.py. Also carries
    ``unattended_sessions.enabled`` — a deliberate per-organization opt-in,
    not a per-user setting, so it lives here rather than settings.yaml.
    ``rooms`` (optional) is a static room/resource directory snapshot IT
    refreshes with ``scripts/sync_room_directory.py``, using a separate,
    admin-scoped Google Cloud project — see room_directory_client.py and
    docs/google-cloud-setup.md. It's plain data, not a credential, and is
    handed straight to CalendarConnector; the Calendar OAuth client itself
    never carries Workspace-admin directory scope. ``mode``/``server``/
    ``idp`` (P7, docs/https-connector-refactor-plan.md §4/§9.4/§10.2)
    switch this daemon into org mode — a real OAuth 2.1 authorization
    server on ``/mcp`` instead of the local shared-secret token, human
    identity resolved via the org's own OIDC IdP. Absent (every install
    before this phase, and every one that hasn't opted in) means local
    mode, byte-identical to before. See org_mode.py and org_identity.py
    for the schema, or build a bundle with ``scripts/build_org_bundle.py
    --mode org ...``.
  - ``config/settings.yaml``   — per-user settings: privacy policy,
    connectors{enabled}, auto_accept_rules,
    pii_detection{enabled, detect_ip_addresses, detect_financial_figures,
    audit_match_details}. No secrets live here.
Per-user credentials (OAuth tokens, Telegram session) live under
``credentials/``, one file per connector.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from . import audit_forwarding, org_bundle_signing, org_mode
from .paths import data_dir, org_dir, user_dir
from .principal import LOCAL_PRINCIPAL_ID, current_principal
from .app_credentials import telegram_app_credentials
from .approval_ui import init_approval_ui
from .audit_log import (
    AuditEntry,
    compute_security_config_hash,
    current_week,
    get_audit_logger,
    init_audit_logger,
)
from .auto_accept import (
    init_config_path,
    migrate_telegram_search_operation_key,
    reload_rules,
)
from .pii_detector import init_pii_detection
from .privacy_filter import check_consistency_warnings, init_privacy_filter
from .resource_grants import build_effective_rules, migrate_rules_to_grants
from .safe_errors import SecretRedactingFormatter
from .secure_files import (
    InsecurePermissionsError,
    atomic_write_bytes,
    atomic_write_text,
    audit_directory_permissions,
)
from .connectors.apps_script import AppsScriptConnector
from .connectors.calendar import CalendarConnector
from .connectors.confluence import ConfluenceConnector
from .connectors.contacts import ContactsConnector
from .connectors.drive import DriveConnector
from .connectors.gmail import GmailConnector
from .connectors.jira import JiraConnector
from .connectors.salesforce import SalesforceConnector
from .connectors.slack import SlackConnector
from .connectors.tasks import TasksConnector
from .connectors.telegram import TelegramConnector
from .apps_script_client import AppsScriptClient, AppsScriptClientError
from .atlassian_oauth import AtlassianOAuthError
from .atlassian_oauth import authorize_interactive as atlassian_authorize_interactive
from .atlassian_oauth import load_token_file as load_atlassian_token
from .calendar_client import CalendarClient, CalendarClientError
from .confluence_client import ConfluenceClient, ConfluenceClientError
from .connector_host import ConnectorHost
from .contacts_client import ContactsClient, ContactsClientError
from .drive_client import DriveClient, DriveClientError
from .gmail_client import GmailClient, GmailClientError
from .jira_client import JiraClient, JiraClientError
from .salesforce_client import SalesforceClient, SalesforceClientError
from .salesforce_client import authorize_interactive as salesforce_authorize_interactive
from .salesforce_client import load_token_file as load_salesforce_token
from .slack_client import SlackClient, SlackClientError
from .slack_client import authorize_interactive as slack_authorize_interactive
from .slack_client import load_token_file as load_slack_token
from .tasks_client import TasksClient, TasksClientError
from .telegram_client import TelegramClientError, TelegramPrivacyFenceClient

logger = logging.getLogger("privacyfence.daemon")

PROJECT_ROOT = str(data_dir())
LOCK_FILE = os.path.join(PROJECT_ROOT, "privacyfence.lock")

# Where each connector's per-user credential is cached. Purely internal — no
# longer user-configurable, since org app registration and per-user auth are
# now handled separately (see module docstring).
TOKEN_FILES: dict[str, str] = {
    "gmail": "credentials/token.json",
    "drive": "credentials/drive_token.json",
    "calendar": "credentials/calendar_token.json",
    "contacts": "credentials/contacts_token.json",
    "tasks": "credentials/tasks_token.json",
    "apps_script": "credentials/apps_script_token.json",
    "slack": "credentials/slack_token.json",
    "salesforce": "credentials/salesforce_token.json",
    "atlassian": "credentials/atlassian_token.json",
    "telegram": "credentials/telegram.session",
}

_lock_fd: int | None = None


# ---------------------------------------------------------------------------- #
# Instance lock
# ---------------------------------------------------------------------------- #

def _acquire_instance_lock() -> bool:
    global _lock_fd
    fd = os.open(LOCK_FILE, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    _lock_fd = fd
    return True


def _release_instance_lock() -> None:
    global _lock_fd
    if _lock_fd is not None:
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            os.close(_lock_fd)
        except OSError:
            pass
        _lock_fd = None


# ---------------------------------------------------------------------------- #
# Shutdown wait (P10, docs/https-connector-refactor-plan.md §12): through P9
# the main thread blocked inside menu_bar.run_menu_bar()'s own AppKit run
# loop until the tray icon's "Quit PrivacyFence" (or the web settings page's
# own quit action, wired to the same rumps.quit_application()) ended it. P10
# deleted that host, so run_app() below now blocks on this plain
# threading.Event instead -- request_shutdown() (called by SettingsController
# .quit_app(), the direct successor of that same "Quit PrivacyFence" action)
# sets it, same as SIGINT/Ctrl-C raising KeyboardInterrupt out of the wait.
# ---------------------------------------------------------------------------- #

_shutdown_event = threading.Event()


def request_shutdown() -> None:
    """Signal run_app()'s own wait loop to return. Called by
    SettingsController.quit_app() -- see that method's own docstring."""
    _shutdown_event.set()


def _wait_for_shutdown() -> None:
    """Blocks the calling (main) thread until request_shutdown() is called
    or the process receives SIGINT/Ctrl-C (KeyboardInterrupt). Everything
    Claude-facing and human-facing already runs on the web server's own
    thread (McpDispatcher.call() via /mcp, and every web/routes_*.py
    handler) -- this thread has nothing left to do but wait."""
    _shutdown_event.wait()


# Periodic "is it time to check yet?" pulse for the update checker --
# deliberately shorter than update_checker.CHECK_INTERVAL_SECONDS (24h).
# SettingsController.on_update_check_timer() re-derives whether 24h have
# actually passed from its own on-disk timestamp, so this is robust to
# sleep/wake and doesn't need to match the real interval exactly. Through
# P9 this pulse came from menu_bar.py's own rumps.Timer; P10 deleted that
# host, so _run_update_check_timer (started as its own daemon thread by
# run_app(), below) is now what fires it.
UPDATE_CHECK_TIMER_INTERVAL_SECONDS = 6 * 60 * 60


def _run_update_check_timer(controller: Any) -> None:
    """Fires ``controller.on_update_check_timer()`` once immediately, then
    every UPDATE_CHECK_TIMER_INTERVAL_SECONDS until shutdown is requested --
    reuses ``_shutdown_event`` as its own sleep/cancel mechanism so this
    thread wakes and exits promptly rather than sleeping out its last
    interval on the way down."""
    while True:
        controller.on_update_check_timer()
        if _shutdown_event.wait(UPDATE_CHECK_TIMER_INTERVAL_SECONDS):
            return


# ---------------------------------------------------------------------------- #
# Configuration & logging
# ---------------------------------------------------------------------------- #

def _resolve_path(path: str) -> str:
    """Relative to ``PROJECT_ROOT`` for the local principal -- exactly as
    before this phase, including for the tests that monkeypatch
    ``PROJECT_ROOT`` directly to sandbox where a test run reads/writes --
    or to that *other* principal's own storage root (P6, docs/
    https-connector-refactor-plan.md §9.2) when this runs inside a
    ``principal_scope()`` block for someone else (only
    connector_registry.py's ``ConnectorRegistry.get()`` does that today).
    """
    if os.path.isabs(path):
        return path
    principal = current_principal()
    if principal.id == LOCAL_PRINCIPAL_ID:
        return os.path.join(PROJECT_ROOT, path)
    return str(user_dir(principal) / path)


def _bootstrap_config(resolved: str) -> None:
    """Seed a default settings.yaml from the packaged example on first run.

    The example carries no secrets (org credentials and per-user auth are
    handled separately, via PrivacyFence Settings), so it's safe to install
    automatically now that there's no setup wizard to do it.
    """
    example = Path(__file__).parent / "resources" / "settings.yaml.example"
    atomic_write_bytes(resolved, example.read_bytes())


def load_config(config_path: str) -> dict[str, Any]:
    resolved = _resolve_path(config_path)
    if not os.path.exists(resolved):
        _bootstrap_config(resolved)
    with open(resolved, encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}
    if not isinstance(config, dict):
        raise ValueError(f"Config file {resolved} did not parse to a mapping")
    return config


def load_org_config() -> dict[str, Any]:
    """Load the installed organization config bundle.

    Three states (SEC-04), not two: absent entirely → {} (local mode, same
    "missing config → connector skipped" philosophy used for every
    connector below); a valid JSON object → parsed and returned as
    configured; present but broken (unreadable, malformed JSON, or a
    non-object top level) → raise ``org_mode.ConfigurationError`` and
    refuse to start.

    Before this, "broken" collapsed into the same {} result as "absent" —
    so a corrupted or tampered org_config.json silently behaved exactly
    like no org config at all. For an org-mode install that's a silent
    downgrade to no IdP-backed auth at all (org_mode.resolve_mode({})
    resolves to "local"), triggerable by anything that can truncate or
    corrupt the file — not a state this daemon should ever paper over.
    Installed via PrivacyFence Settings' "Install/Update Organization
    Config…" (or by hand-editing this file).

    SEC-05 (full signing): a well-formed-but-hostile *replacement* of
    this file (as opposed to the malformed-file cases above) is a
    separate, more dangerous failure mode SEC-04 alone can't catch —
    parses fine, just carries someone else's IdP/app credentials. Every
    load here also runs the bundle through org_bundle_signing.verify_
    and_maybe_pin(): a bundle that fails to verify against a previously
    pinned signing key raises ConfigurationError the same as a malformed
    one, and ``mode: org`` additionally requires the bundle to actually
    be signed at all (see that module's own docstring for the trust-on-
    first-use model). A local-mode install that has never adopted
    signing is unaffected — this call is then a no-op past the "no
    signing key pinned yet and no signature present" pass-through case.
    """
    path = org_dir() / "org_config.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise org_mode.ConfigurationError(f"Could not read organization config at {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise org_mode.ConfigurationError(f"Organization config at {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise org_mode.ConfigurationError(f"Organization config at {path} is not a JSON object")

    trust = org_bundle_signing.verify_and_maybe_pin(data, org_dir())
    if not trust.ok:
        raise org_mode.ConfigurationError(
            f"Organization config at {path} failed signing-key verification ({trust.detail}) -- "
            f"refusing to start. If a legitimate signing-key rotation is expected, an "
            f"administrator must delete {org_bundle_signing.pinned_public_key_path(org_dir())} "
            f"to re-trust a new key."
        )
    if trust.newly_pinned:
        logger.warning(
            "Organization config bundle signing key trusted for the first time (TOFU) and "
            "pinned to %s -- every future bundle must verify against this key",
            org_bundle_signing.pinned_public_key_path(org_dir()),
        )
    if org_mode.resolve_mode(data) == "org" and not trust.signed:
        raise org_mode.ConfigurationError(
            f"Organization config at {path} has \"mode\": \"org\" but is not signed -- org mode "
            "requires a signed bundle (build one with scripts/build_org_bundle.py --sign-key ...; "
            "generate a signing key first with --generate-signing-key if you haven't yet)."
        )
    return data


def log_org_config_bundle_hash(org_config: dict[str, Any]) -> None:
    """SEC-05 (interim): record a hash of the installed org_config.json at
    every daemon startup, both to the regular log and to the audit trail,
    so tampering between one startup and the next is detectable by
    comparing hashes -- even for an install that hasn't adopted full
    signing (org_bundle_signing.py, SEC-05 full) at all, and as a
    belt-and-suspenders record alongside it for one that has. Called once
    by run_app() on the same org_config load_org_config() already returned
    earlier in that function (SEC-07 needs it sooner, to pick
    init_privacy_filter()'s fail-safe default) -- deferred until after
    init_audit_logger() so there's an audit logger to record to, and still
    not called from inside load_org_config() itself, which is also called
    from places that aren't "daemon startup" (e.g. settings_controller.py
    refreshing connector state) and shouldn't each add their own audit-log
    entry.
    """
    path = org_dir() / "org_config.json"
    if not path.exists():
        logger.info("No organization config bundle installed (org_config.json absent)")
        return
    try:
        raw = path.read_bytes()
    except OSError as exc:
        logger.warning("Could not read organization config bundle for startup hash logging: %s", exc)
        return
    digest = org_bundle_signing.sha256_hex(raw)
    signed = bool(org_config.get(org_bundle_signing.SIGNATURE_FIELD))
    logger.info(
        "Organization config bundle at startup: sha256=%s size=%d bytes signed=%s",
        digest, len(raw), signed,
    )
    try:
        get_audit_logger().record(AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            week=current_week(),
            request_id=uuid.uuid4().hex[:12],
            connector="",
            tool="",
            tool_name="",
            summary=f"Daemon startup: organization config bundle sha256={digest} ({len(raw)} bytes, signed={signed})",
            sender="",
            decision="org_config_startup",
            auto_accept_rule="",
            latency_seconds=0.0,
            pii_detected=False,
        ))
    except Exception as exc:
        logger.warning("Audit log write failed for organization config startup hash: %s", exc)


def get_or_create_deployment_id() -> str:
    """SEC-23 (docs/security-remediation-plan.md, Phase 3 item 3.6): a
    stable, opaque identifier for *this installation* -- not per-principal,
    and not re-generated across restarts -- stamped onto every audit entry
    (audit_log.AuditEntry.deployment_id, filled in by AuditLogger.record())
    so a centralized collection of entries -- forwarded (audit_forwarding.py)
    or manually aggregated -- can tell which employee machine or org-mode
    server produced a given decision. Persisted once at
    ``data_dir()/deployment_id`` (install-wide, like ``org_dir()``'s own
    bundle -- not per-principal like ``user_dir()``, since one daemon
    process is one deployment regardless of how many principals it serves
    in org mode) and read back on every later startup. A random hex id,
    not a hostname/MAC address -- it identifies "which install", not
    anything about the machine itself.
    """
    path = data_dir() / "deployment_id"
    try:
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
    except OSError as exc:
        logger.warning("Could not read deployment id at %s: %s", path, exc)
    new_id = uuid.uuid4().hex
    try:
        atomic_write_text(str(path), new_id)
    except OSError as exc:
        logger.warning(
            "Could not persist deployment id at %s: %s -- a new one will be generated next "
            "startup", path, exc,
        )
    return new_id


def check_storage_permissions(org_mode_active: bool) -> None:
    """SEC-09's startup check: warn (or, in org mode, refuse to start) if
    any of this install's own data directories grant group/other access --
    e.g. an install that predates ``paths.py``'s ``secure_mkdir`` adoption,
    upgraded in place, or a directory hand-created (or restored from a
    backup) under a looser umask than PrivacyFence itself would ever use.
    ``data_dir()``/``org_dir()``/``user_dir()`` already self-heal on every
    call (``secure_mkdir`` re-``chmod``s an existing directory to ``0700``
    every time it's resolved) -- this check exists for the cases that
    self-healing doesn't cover: a filesystem that silently refuses
    ``chmod`` (already logged at ``warning`` from ``secure_mkdir`` itself,
    but easy to miss in a large log), or a directory whose looser
    permissions were restored *after* the ``chmod`` that would have fixed
    them ran.

    Local mode logs every finding at ``warning`` and keeps starting -- the
    same "detectable, not necessarily preventable" posture SEC-05's interim
    hash-logging takes. Org mode -- centrally managed, and the posture this
    whole phase is meant to bring up to enterprise-production-ready -- fails
    closed: ``InsecurePermissionsError`` propagates out of ``run_app()``
    through the same top-level "print and refuse to start" path SEC-04's
    ``ConfigurationError`` already uses (main()'s own ``except Exception``).
    """
    # dict.fromkeys dedupes without disturbing order -- user_dir() with no
    # principal in scope resolves to data_dir() itself (the local
    # principal's storage root *is* data_dir(), see paths.py's own
    # docstring), so at daemon startup this list often names the same
    # directory twice; no need to warn about it twice too.
    dirs = list(dict.fromkeys([data_dir(), org_dir(), user_dir()]))
    problems = audit_directory_permissions(dirs)
    for problem in problems:
        logger.warning("SEC-09: %s", problem)
    if problems and org_mode_active:
        raise InsecurePermissionsError(
            "Refusing to start in organization mode: " + " ".join(problems)
        )


def setup_logging(config: dict[str, Any]) -> None:
    log_cfg = config.get("logging", {}) or {}
    level_name = str(log_cfg.get("level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    log_file = _resolve_path(log_cfg.get("file", "logs/privacyfence.log"))
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    # SEC-10 (docs/security-remediation-plan.md Phase 1.7): every logger in
    # the process inherits the root logger's handlers, so this is the one
    # place that needs to redact token-shaped substrings for the whole
    # daemon rather than at each individual `except Exception` -- see
    # safe_errors.py's module docstring for what this catches and why the
    # MCP-boundary public-message allowlist (routes_mcp.py) is a separate,
    # stricter layer rather than relying on this alone.
    fmt = SecretRedactingFormatter("%(asctime)s %(levelname)-8s [%(name)s] %(message)s")
    handlers: list[logging.Handler] = [
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ]
    for h in handlers:
        h.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    for h in handlers:
        root.addHandler(h)

    logger.info("Logging initialized → %s", log_file)


# ---------------------------------------------------------------------------- #
# Web approval UI + MCP-over-HTTP (see docs/https-connector-refactor-plan.md's
# P1/P2). Through P9, config/settings.yaml's web.approval_ui selected native
# (AppKit, the default) or web; P10 deleted the native implementation (§12,
# D6), so the web approval UI is now unconditionally installed in local mode
# -- there is nothing left to select. web.mcp.enabled independently turns
# the /mcp endpoint on (§8 of that document is a transport change, separate
# from the approval surface), and web.settings.enabled independently turns
# /settings on -- either can be on or off without affecting the other two;
# all three share the one embedded server/one port, per §3's target
# architecture, which local mode now always starts.
# ---------------------------------------------------------------------------- #

def _maybe_start_web_server(
    config: dict[str, Any],
    connector_host: ConnectorHost,
    *,
    unattended_sessions_enabled: bool,
    controller: Any = None,
    org_config: dict[str, Any] | None = None,
) -> Any:
    """Returns the started WebServer -- always, in local mode, since P10
    made the web approval UI the only one there is (see this section's own
    comment above); org mode still returns None if ``mcp.enabled`` is off,
    unchanged from before this phase (there is no approval surface to fall
    back to there either, but org mode has never had a way to reach one
    without ``/mcp`` in the first place -- see ``_start_org_web_server``'s
    own docstring). ``web.mcp.enabled``/``web.settings.enabled`` remain
    independent rollback levers for those two surfaces specifically
    (docs/https-connector-refactor-plan.md §12's "P2: the HTTP listener is
    off unless configured" / §16.6's ``web.settings.enabled``) -- turning
    both off still leaves the server running for ``/approvals`` alone,
    since P10 left that with no off switch of its own (§12: "P10 is the one
    phase with no rollback -- it deletes the fallback"). Since P5 retired
    the bridge, turning ``mcp.enabled`` off also leaves this install with
    no way for Claude to reach it at all -- ``web.mcp.enabled: true``
    (settings.yaml.example's default since D11/P4b) is no longer "additive
    alongside the bridge", it is the only transport there is; the key
    survives as a deliberate full-stop kill switch, not as a rollback to
    some other still-working path. Imports the web/starlette/uvicorn/mcp
    stack lazily so constructing it is deferred to the one place that
    actually needs it, same posture this module has taken since P1.

    ``connector_host`` is already built and holds the real connector set by
    the time this is called (see run_app's ordering) -- the MCP dispatcher
    polls ``connector_host.connectors`` live rather than taking its own
    snapshot, so a connector rebuild pushed by SettingsController.
    refresh_connectors (-> ConnectorHost.set_connectors) reaches the
    ``/mcp`` endpoint too, with nothing here needing a second push. Note
    that this connector set is the *local* principal's own (org mode's
    real per-user connectors are P8's job, per docs/https-connector-
    refactor-plan.md's own phase dependency chart -- P7 delivers identity,
    P8 the per-user service authorization that makes a second principal's
    connectors buildable at all); every principal authenticated via org
    mode's OAuth 2.1 AS dispatches against this same shared connector set
    until then.

    ``controller``, when given, is the SettingsController instance
    run_app() built for this daemon's whole lifetime -- passed through here
    so the web settings page (when ``web.settings.enabled``) can drive it;
    always the same instance regardless, so a rule changed via ``/settings``
    and one changed via a gated call's own "Always allow" button stay in
    sync with no separate plumbing.

    ``org_config`` (P7, §4) is where ``"mode"`` lives -- ``org`` switches
    this into web/server.py's own org-mode wiring (a real OAuth 2.1
    authorization server on ``/mcp``, no local-token approval/settings
    surface at all -- see that module's own docstring for why). Defaults
    to ``{}`` (local mode, byte-identical to before this phase) when
    omitted, which no real caller does -- run_app() always passes the
    result of ``load_org_config()``.
    """
    web_config = config.get("web", {}) or {}
    mcp_config = web_config.get("mcp", {}) or {}
    settings_config = web_config.get("settings", {}) or {}
    notifications_config = web_config.get("notifications", {}) or {}
    org_config = org_config or {}
    mode = org_mode.resolve_mode(org_config)
    mcp_enabled = bool(mcp_config.get("enabled", False))

    if mode == "org":
        if not mcp_enabled:
            return None
        return _start_org_web_server(
            web_config, org_config, connector_host, unattended_sessions_enabled=unattended_sessions_enabled,
        )

    use_web_settings = bool(settings_config.get("enabled", False)) and controller is not None

    from .approvals import PendingApprovalRegistry
    from .web.mcp_dispatch import McpDispatcher
    from .web.server import DEFAULT_PORT, WebServer
    from .web_approval_ui import init_web_approval_ui

    # web.approvals.* overrides D3's defaults (docs/https-connector-refactor-
    # plan.md §15: "hold 30s, pending TTL 15 min, ledger TTL 5 min" --
    # "these defaults are what P3's beta measures against"). One registry
    # backs both the web approval surface and privacyfence_await_approval
    # (below), whether or not mcp_enabled is actually on -- constructing it
    # unconditionally here costs nothing (it's just an empty dict-backed
    # object until something registers into it) and means turning mcp.
    # enabled on later, without restarting, would find it ready.
    approvals_config = web_config.get("approvals", {}) or {}
    registry = PendingApprovalRegistry(
        hold_window=float(approvals_config.get("hold_window_seconds", 30.0)),
        pending_ttl=float(approvals_config.get("pending_ttl_seconds", 15 * 60.0)),
        ledger_ttl=float(approvals_config.get("ledger_ttl_seconds", 5 * 60.0)),
        max_pending=int(approvals_config.get("max_pending", 50)),
        # SEC-15 (docs/security-remediation-plan.md, Phase 1 item 1.8): see
        # approvals.DEFAULT_MAX_PENDING_PER_PRINCIPAL's own comment for why
        # this exists alongside max_pending above.
        max_pending_per_principal=int(approvals_config.get("max_pending_per_principal", 20)),
    )
    web_ui = init_web_approval_ui(registry=registry)
    init_approval_ui(web_ui)

    mcp_dispatcher = None
    if mcp_enabled:
        mcp_dispatcher = McpDispatcher(
            lambda: connector_host.connectors, unattended_sessions_enabled=unattended_sessions_enabled,
            registry=registry,
        )
        if controller is not None:
            # The direct successor of ipc_server.py's own constructor-time
            # ``ipc_server.set_unattended_changed_listener(self._on_
            # unattended_changed)`` wiring -- moved out here because, unlike
            # the old always-on IPCServer, whether a dispatcher exists at
            # all now depends on mcp_enabled, which SettingsController's own
            # constructor has no visibility into.
            controller.wire_unattended_listener(mcp_dispatcher)

    server = WebServer(
        web_ui,
        port=int(web_config.get("port", DEFAULT_PORT)),
        mcp_dispatcher=mcp_dispatcher,
        controller=controller if use_web_settings else None,
        allow_quit=bool(settings_config.get("allow_quit", True)),
        notifications_enabled=bool(notifications_config.get("enabled", True)),
        notifications_detail=str(notifications_config.get("detail", "minimal")),
    )
    server.start()
    # The pending-result URL gate.py hands back to Claude (§5.2 point 4) is
    # only meaningful once the server is actually listening -- set here,
    # not at registry construction.
    registry.set_base_url(server.base_url)
    # SEC-06 (docs/security-remediation-plan.md, Phase 1 item 1.2): each of
    # these is a fresh, single-use bootstrap link, not the persistent
    # secret itself -- see WebServer.mint_bootstrap_url()'s own docstring.
    # Once it's expired or already used, a fresh one needs either a daemon
    # restart or POST /api/bootstrap with the raw token as a Bearer header.
    logger.info(
        "Web approval UI active -- approvals open at %s",
        server.mint_bootstrap_url("/approvals"),
    )
    if use_web_settings:
        logger.info(
            "Web settings active -- open at %s",
            server.mint_bootstrap_url("/settings"),
        )
    if server.mcp_url:
        from .web.mcp_auth import MCP_TOKEN_FILE_NAME

        logger.info(
            "MCP-over-HTTP active -- %s (Authorization: Bearer <token in %s>)",
            server.mcp_url, data_dir() / MCP_TOKEN_FILE_NAME,
        )
    return server


def _start_org_web_server(
    web_config: dict[str, Any], org_config: dict[str, Any], connector_host: ConnectorHost,
    *, unattended_sessions_enabled: bool,
) -> Any:
    """org mode's own boot path (P7, docs/https-connector-refactor-plan.md
    §9.4, §10.2) -- a real OAuth 2.1 authorization server on ``/mcp``
    instead of the local shared-secret ``StaticTokenVerifier``, no local-
    token approval/settings surface mounted at all (see web/server.py's
    own module docstring for why). Raises ``org_mode.ConfigurationError``
    (SEC-04; surfaced as a startup failure, the same posture a missing/
    invalid settings.yaml already has) if org_config.json is missing the
    ``idp``/``server`` sections org mode requires -- there is no silent
    partial-org-mode fallback.

    ``connector_host`` (the local principal's own connector set, built once
    at startup by run_app()) is accepted for signature parity with local
    mode's own call but is otherwise unused here as of P8: every org
    principal's connectors now come from ``ConnectorRegistry`` below,
    built lazily per principal instead of shared off the local principal's
    set (docs/https-connector-refactor-plan.md §9.3 -- see connector_
    registry.py's own docstring for why this was left unwired until now).
    """
    from .approvals import PendingApprovalRegistry
    from .connector_registry import ConnectorRegistry
    from .org_identity import IdpConfig
    from .principal import Principal, current_principal
    from .web.mcp_dispatch import McpDispatcher
    from .web.oauth_provider import OrgOAuthProvider
    from .web.org_session import OrgSessionStore
    from .web.server import OrgAuth, WebServer
    from .web_approval_ui import init_web_approval_ui

    server_config = org_mode.ServerConfig.from_org_config(org_config)
    idp = IdpConfig.from_org_config(org_config)
    if idp is None:
        raise org_mode.ConfigurationError(
            "org mode (org_config.json \"mode\": \"org\") requires an \"idp\" section "
            "(issuer, client_id, client_secret)"
        )

    approvals_config = web_config.get("approvals", {}) or {}
    approval_registry = PendingApprovalRegistry(
        hold_window=float(approvals_config.get("hold_window_seconds", 30.0)),
        pending_ttl=float(approvals_config.get("pending_ttl_seconds", 15 * 60.0)),
        ledger_ttl=float(approvals_config.get("ledger_ttl_seconds", 5 * 60.0)),
        max_pending=int(approvals_config.get("max_pending", 50)),
        # SEC-15 (docs/security-remediation-plan.md, Phase 1 item 1.8): see
        # approvals.DEFAULT_MAX_PENDING_PER_PRINCIPAL's own comment for why
        # this exists alongside max_pending above.
        max_pending_per_principal=int(approvals_config.get("max_pending_per_principal", 20)),
    )
    web_ui = init_web_approval_ui(registry=approval_registry)
    # WebApprovalUI is unconditionally the ApprovalUI here, same as local
    # mode's own _maybe_start_web_server above since P10 -- org mode never
    # had a native option to begin with (there is no GUI on a server).
    init_approval_ui(web_ui)

    def _connectors_for_principal(_principal: Principal) -> list:
        # Loaded fresh per principal, under the principal_scope
        # ConnectorRegistry.get() already enters before calling this --
        # the *relative* path is what makes _resolve_path() (and
        # load_config's own bootstrap-a-default-on-first-use behavior)
        # resolve against that principal's own users/<id>/config/
        # settings.yaml rather than the local principal's, per §9.2's
        # storage layout. Re-reading load_org_config() here (rather than
        # closing over the org_config this function was called with)
        # would also be defensible, but it's already loaded once by
        # run_app() and passed down consistently everywhere else in this
        # module, so this stays consistent with that.
        cfg = load_config("config/settings.yaml")
        return build_connectors(cfg, org_config)

    connector_registry = ConnectorRegistry(factory=_connectors_for_principal)

    # SEC-22 (docs/security-remediation-plan.md, Phase 3 item 3.7): layered
    # on top of the IdP's own authentication above -- see org_identity.
    # check_authz_policy's own docstring for what this does and doesn't
    # change. Absent "authz" section in org_config.json -> disabled,
    # every IdP-authenticated principal is admitted, unchanged from before.
    authz_policy = org_mode.AuthzPolicyConfig.from_org_config(org_config)
    provider = OrgOAuthProvider(
        idp, idp_callback_url=f"{server_config.issuer_url.rstrip('/')}/oauth/idp/callback", policy=authz_policy,
    )
    sessions = OrgSessionStore()
    mcp_dispatcher = McpDispatcher(
        lambda: connector_registry.get(current_principal()).connectors,
        unattended_sessions_enabled=unattended_sessions_enabled,
        registry=approval_registry,
    )

    server = WebServer(
        web_ui,
        host=server_config.bind_host, port=server_config.port,
        mcp_dispatcher=mcp_dispatcher,
        org=OrgAuth(
            provider=provider, sessions=sessions, idp=idp, issuer_url=server_config.issuer_url,
            connector_registry=connector_registry, org_config=org_config,
        ),
        ssl_certfile=server_config.cert_file or None,
        ssl_keyfile=server_config.key_file or None,
        trusted_proxies=server_config.trusted_proxies,
    )
    server.start()
    approval_registry.set_base_url(server.base_url)
    step_up = org_mode.StepUpConfig.from_org_config(org_config)
    logger.info(
        "Org mode active -- MCP-over-HTTP at %s (OAuth 2.1, DCR at %s/register), IdP %s, "
        "WebAuthn step-up %s, app-level authz policy %s",
        server.mcp_url, server.base_url, idp.issuer,
        f"enabled (scope={step_up.scope})" if step_up.enabled else "disabled",
        f"enabled ({len(authz_policy.allowed_domains)} allowed domain(s), "
        f"{len(authz_policy.required_groups)} required group(s))" if authz_policy.enabled else "disabled",
    )
    return server


def _google_client_config(org_config: dict[str, Any]) -> dict[str, Any]:
    """Wrap the bundle's flat Google app fields back into the "installed" shape
    that ``InstalledAppFlow.from_client_config`` expects."""
    google = org_config.get("google") or {}
    if not google.get("client_id") or not google.get("client_secret"):
        return {}
    return {"installed": google}


# ---------------------------------------------------------------------------- #
# Connector construction (graceful: missing org config or auth → connector skipped)
# ---------------------------------------------------------------------------- #

def build_connectors(config: dict[str, Any], org_config: dict[str, Any]) -> list:
    """Builds every enabled, currently-authenticated connector for the
    *current principal* (P6, docs/https-connector-refactor-plan.md §9.2):
    every credential/cache path below resolves through ``_resolve_path()``/
    ``user_dir()``, which is the local principal's own storage root (i.e.
    unchanged from before this phase) unless this is called from inside a
    ``principal_scope()`` block for someone else -- see
    connector_registry.py's ``ConnectorRegistry``, which is what actually
    does that once a second principal's connectors are buildable at all
    (P8). ``run_app()`` below still calls this directly, once, for the local
    principal only -- that's what keeps local mode byte-identical."""
    connectors: list[Any] = []
    connectors_cfg: dict[str, dict] = config.get("connectors", {}) or {}

    def enabled(name: str) -> bool:
        return (connectors_cfg.get(name) or {}).get("enabled", True)

    google_client_config = _google_client_config(org_config)

    # docs/org-mode-download-delivery-plan.md, Phase 2: computed once, not
    # per connector -- Drive/Gmail/Confluence's own _download_file/
    # _download_attachment methods branch on connector.download_mode, and
    # (in org mode only) build a fully-qualified download_url from
    # connector.download_base_url. Local mode's own connector.download_mode
    # stays "local" and connector.download_config/download_base_url stay
    # unset, exactly the pre-Phase-2 shape.
    download_mode = org_mode.resolve_mode(org_config)
    download_config = None
    download_base_url = ""
    if download_mode == "org":
        download_config = org_mode.DownloadDeliveryConfig.from_org_config(org_config)
        # ServerConfig.from_org_config already ran (and would have raised
        # org_mode.ConfigurationError) before build_connectors was ever
        # reached in a real org-mode boot -- see _start_org_web_server,
        # which computes its own server_config first. Re-deriving it here
        # keeps build_connectors self-contained (safe to call from a test
        # or from ConnectorRegistry's own per-principal factory with no
        # other org-mode wiring already in scope) at the cost of one cheap
        # re-parse of org_config.json's own "server" section.
        download_base_url = org_mode.ServerConfig.from_org_config(org_config).issuer_url.rstrip("/")

    # Gmail
    if enabled("gmail"):
        try:
            if not google_client_config:
                raise GmailClientError("Google organization config not installed")
            client = GmailClient(
                client_config=google_client_config,
                token_file=_resolve_path(TOKEN_FILES["gmail"]),
            )
            email = client.check_connection()
            logger.info("Gmail connector ready for %s", email)
            connector = GmailConnector(client)
            connector.my_email = email
            connector.download_mode = download_mode
            connector.download_config = download_config
            connector.download_base_url = download_base_url
            connectors.append(connector)
        except (GmailClientError, FileNotFoundError) as exc:
            logger.warning("Gmail connector disabled: %s", exc)

    # Drive
    if enabled("drive"):
        try:
            if not google_client_config:
                raise DriveClientError("Google organization config not installed")
            client = DriveClient(
                client_config=google_client_config,
                token_file=_resolve_path(TOKEN_FILES["drive"]),
            )
            email = client.check_connection()
            logger.info("Drive connector ready for %s", email)
            connector = DriveConnector(client)
            connector.my_email = email
            connector.download_mode = download_mode
            connector.download_config = download_config
            connector.download_base_url = download_base_url
            connectors.append(connector)
        except (DriveClientError, FileNotFoundError) as exc:
            logger.warning("Drive connector disabled: %s", exc)

    # Calendar
    if enabled("calendar"):
        try:
            if not google_client_config:
                raise CalendarClientError("Google organization config not installed")
            client = CalendarClient(
                client_config=google_client_config,
                token_file=_resolve_path(TOKEN_FILES["calendar"]),
            )
            email = client.check_connection()
            logger.info("Calendar connector ready for %s", email)
            connector = CalendarConnector(client, rooms=org_config.get("rooms", []))
            connector.my_email = email
            connector.free_busy_full_details = bool(
                (config.get("calendar", {}) or {}).get("free_busy_full_event_details", True)
            )
            connectors.append(connector)
        except (CalendarClientError, FileNotFoundError) as exc:
            logger.warning("Calendar connector disabled: %s", exc)

    # Contacts
    if enabled("contacts"):
        try:
            if not google_client_config:
                raise ContactsClientError("Google organization config not installed")
            client = ContactsClient(
                client_config=google_client_config,
                token_file=_resolve_path(TOKEN_FILES["contacts"]),
            )
            email = client.check_connection()
            logger.info("Contacts connector ready for %s", email)
            connector = ContactsConnector(client)
            connector.my_email = email
            connectors.append(connector)
        except (ContactsClientError, FileNotFoundError) as exc:
            logger.warning("Contacts connector disabled: %s", exc)

    # Tasks
    if enabled("tasks"):
        try:
            if not google_client_config:
                raise TasksClientError("Google organization config not installed")
            client = TasksClient(
                client_config=google_client_config,
                token_file=_resolve_path(TOKEN_FILES["tasks"]),
            )
            email = client.check_connection()
            logger.info("Tasks connector ready for %s", email)
            connectors.append(TasksConnector(client))
        except (TasksClientError, FileNotFoundError) as exc:
            logger.warning("Tasks connector disabled: %s", exc)

    # Apps Script
    if enabled("apps_script"):
        try:
            if not google_client_config:
                raise AppsScriptClientError("Google organization config not installed")
            client = AppsScriptClient(
                client_config=google_client_config,
                token_file=_resolve_path(TOKEN_FILES["apps_script"]),
            )
            email = client.check_connection()
            logger.info("Apps Script connector ready for %s", email)
            connectors.append(AppsScriptConnector(client))
        except (AppsScriptClientError, FileNotFoundError) as exc:
            logger.warning("Apps Script connector disabled: %s", exc)

    # Slack
    if enabled("slack"):
        try:
            slack_org = org_config.get("slack") or {}
            if not slack_org.get("client_id"):
                raise SlackClientError("Slack organization config not installed")
            token = load_slack_token(_resolve_path(TOKEN_FILES["slack"]))
            client = SlackClient(
                user_token=token.get("access_token", ""),
                user_cache_file=str(user_dir() / "slack_user_cache.json"),
                channel_cache_file=str(user_dir() / "slack_channel_cache.json"),
            )
            workspace = client.check_connection()
            # Directory-cache warming (if stale) happens after the whole
            # connector list is built, in the background -- see
            # _warm_connector_caches() in run_app(). Doing it here,
            # synchronously, used to delay startup completing
            # until a full users.list/conversations.list re-sync finished,
            # which read as "the app isn't running yet."
            logger.info("Slack connector ready for workspace %r", workspace)
            connector = SlackConnector(client)
            connector.my_email = token.get("email", "")
            connectors.append(connector)
        except (SlackClientError, FileNotFoundError) as exc:
            logger.warning("Slack connector disabled: %s", exc)

    # Salesforce
    if enabled("salesforce"):
        try:
            sf_org = org_config.get("salesforce") or {}
            if not sf_org.get("consumer_key"):
                raise SalesforceClientError("Salesforce organization config not installed")
            token = load_salesforce_token(_resolve_path(TOKEN_FILES["salesforce"]))
            merged = {**sf_org, **token}
            client = SalesforceClient(config=merged, token_file=_resolve_path(TOKEN_FILES["salesforce"]))
            client.check_connection()
            logger.info("Salesforce connector ready for %s", merged.get("instance_url"))
            connectors.append(SalesforceConnector(client))
        except (SalesforceClientError, FileNotFoundError) as exc:
            logger.warning("Salesforce connector disabled: %s", exc)

    # Jira / Confluence — share one Atlassian OAuth grant.
    atlassian_org = org_config.get("atlassian") or {}
    atlassian_token: dict[str, Any] | None = None
    if atlassian_org.get("client_id"):
        try:
            atlassian_token = load_atlassian_token(_resolve_path(TOKEN_FILES["atlassian"]))
        except AtlassianOAuthError:
            atlassian_token = None
    # Merge in client_id/client_secret so JiraClient/ConfluenceClient can
    # refresh an expired access token instead of forcing re-authentication
    # on every restart (the token file only ever holds the per-user fields).
    atlassian_config = {**atlassian_org, **(atlassian_token or {})}

    if enabled("jira"):
        try:
            if not atlassian_org.get("client_id"):
                raise JiraClientError("Atlassian organization config not installed")
            if not atlassian_token:
                raise JiraClientError("Jira is not authenticated. Use Authenticate… in PrivacyFence Settings.")
            client = JiraClient(config=atlassian_config, token_file=_resolve_path(TOKEN_FILES["atlassian"]))
            info = client.check_connection()
            logger.info("Jira connector ready: %s", info)
            connector = JiraConnector(client)
            connector.my_email = atlassian_token.get("account_email", "")
            connectors.append(connector)
        except (JiraClientError, FileNotFoundError) as exc:
            logger.warning("Jira connector disabled: %s", exc)

    if enabled("confluence"):
        try:
            if not atlassian_org.get("client_id"):
                raise ConfluenceClientError("Atlassian organization config not installed")
            if not atlassian_token:
                raise ConfluenceClientError("Confluence is not authenticated. Use Authenticate… in PrivacyFence Settings.")
            client = ConfluenceClient(config=atlassian_config, token_file=_resolve_path(TOKEN_FILES["atlassian"]))
            url = client.check_connection()
            logger.info("Confluence connector ready: %s", url)
            connector = ConfluenceConnector(client)
            connector.my_email = atlassian_token.get("account_email", "")
            connector.download_mode = download_mode
            connector.download_config = download_config
            connector.download_base_url = download_base_url
            connectors.append(connector)
        except (ConfluenceClientError, FileNotFoundError) as exc:
            logger.warning("Confluence connector disabled: %s", exc)

    # Telegram — the sole exception to browser OAuth (MTProto has no
    # equivalent for full user-session access). api_id/api_hash identify the
    # PrivacyFence app itself and are baked into the build (app_credentials.py),
    # not part of the organization config bundle; phone+code(+2FA) auth is
    # still per-user.
    if enabled("telegram"):
        try:
            creds = telegram_app_credentials()
            if not creds:
                raise TelegramClientError("Telegram app credentials not available in this build")
            api_id, api_hash = creds
            session_file = _resolve_path(TOKEN_FILES["telegram"])
            if not os.path.exists(session_file) and not os.path.exists(session_file + ".session"):
                raise TelegramClientError(
                    "Telegram is not authenticated. Use Authenticate… in PrivacyFence Settings."
                )
            tg_client = TelegramPrivacyFenceClient(
                api_id=api_id,
                api_hash=api_hash,
                session_file=session_file,
                chat_cache_file=str(user_dir() / "telegram_chat_cache.json"),
            )
            # Directory-cache warming happens the same way as Slack's now --
            # see _warm_connector_caches() in run_app() -- just scheduled on
            # the web server's own event loop rather than awaited here,
            # since that's the loop every Telegram tool call now actually
            # runs on (McpDispatcher.call(), on the ASGI app's own loop) and
            # therefore the loop Telethon's client ends up bound to on its
            # first connection (see telegram_client.py).
            logger.info("Telegram connector registered (chat cache will warm in the background)")
            connectors.append(TelegramConnector(tg_client))
        except (TelegramClientError, FileNotFoundError, Exception) as exc:
            logger.warning("Telegram connector disabled: %s", exc)

    return connectors


def _warm_connector_caches(connectors: list, web_loop: asyncio.AbstractEventLoop) -> None:
    """Kick off each connector's directory-cache freshness check (Slack's
    user/channel snapshots, Telegram's chat snapshot) in the background,
    right after the web server's event loop is known (see run_app()). Both
    ensure_directories_fresh() and ensure_chat_directory_fresh() are
    best-effort and never raise -- a failure here just means the cache
    stays stale until the next lazy lookup or the explicit
    slack_refresh_*/telegram_refresh_chat_cache tool, same as if this
    warming never ran.

    Deliberately not run inline in build_connectors(), and not awaited
    here either: a full weekly re-sync (users.list/conversations.list/
    get_dialogs) can take a while on a large workspace/account, and running
    it synchronously on the main thread used to delay startup completing
    until it finished -- which read as "the app isn't running
    yet."

    Slack's client is synchronous (blocking HTTP via slack_sdk), so it gets
    its own plain background thread. Telegram's is asyncio-native
    (Telethon) and its client binds to whichever event loop first connects
    it -- that has to be ``web_loop`` (the ASGI app's own loop, captured by
    web/server.py's WebServer -- see run_app()'s wait_until_ready() call),
    the same loop every Telegram tool call now actually runs on
    (McpDispatcher.call()), not a throwaway loop of some other thread -- so
    it's scheduled there via run_coroutine_threadsafe instead.
    """
    for connector in connectors:
        if isinstance(connector, SlackConnector):
            threading.Thread(
                target=connector.client.ensure_directories_fresh,
                name="slack-cache-warm",
                daemon=True,
            ).start()
        elif isinstance(connector, TelegramConnector):
            future = asyncio.run_coroutine_threadsafe(
                connector.client.ensure_chat_directory_fresh(), web_loop
            )
            future.add_done_callback(_log_cache_warm_failure)


def _log_cache_warm_failure(future: "asyncio.Future[None]") -> None:
    # Defensive only -- ensure_chat_directory_fresh() is documented never to
    # raise, same as ensure_directories_fresh(). If it somehow does, this is
    # a background warm with nothing waiting on its result, so log instead
    # of letting the exception vanish into the event loop's default handler.
    exc = future.exception()
    if exc is not None:
        logger.warning("Background Telegram cache warm failed: %s", exc)


# ---------------------------------------------------------------------------- #
# OAuth / interactive-auth setup commands (headless/dev use — the primary UX
# path is now "Authenticate…" in PrivacyFence Settings)
# ---------------------------------------------------------------------------- #

def run_gmail_oauth(org_config: dict[str, Any]) -> int:
    client_config = _google_client_config(org_config)
    client = GmailClient(client_config=client_config, token_file=_resolve_path(TOKEN_FILES["gmail"]))
    try:
        client.authorize_interactive()
        email = client.check_connection()
    except GmailClientError as exc:
        print(f"Gmail OAuth setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Gmail OAuth complete. Authorized as: {email}")
    return 0


def run_drive_oauth(org_config: dict[str, Any]) -> int:
    client_config = _google_client_config(org_config)
    client = DriveClient(client_config=client_config, token_file=_resolve_path(TOKEN_FILES["drive"]))
    try:
        client.authorize_interactive()
        email = client.check_connection()
    except DriveClientError as exc:
        print(f"Drive OAuth setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Drive OAuth complete. Authorized as: {email}")
    return 0


def run_contacts_oauth(org_config: dict[str, Any]) -> int:
    client_config = _google_client_config(org_config)
    client = ContactsClient(client_config=client_config, token_file=_resolve_path(TOKEN_FILES["contacts"]))
    try:
        client.authorize_interactive()
        result = client.check_connection()
    except ContactsClientError as exc:
        print(f"Contacts OAuth setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Contacts OAuth complete. Authorized as: {result}")
    return 0


def run_calendar_oauth(org_config: dict[str, Any]) -> int:
    client_config = _google_client_config(org_config)
    client = CalendarClient(client_config=client_config, token_file=_resolve_path(TOKEN_FILES["calendar"]))
    try:
        client.authorize_interactive()
        email = client.check_connection()
    except CalendarClientError as exc:
        print(f"Calendar OAuth setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Calendar OAuth complete. Authorized as: {email}")
    return 0


def run_tasks_oauth(org_config: dict[str, Any]) -> int:
    client_config = _google_client_config(org_config)
    client = TasksClient(client_config=client_config, token_file=_resolve_path(TOKEN_FILES["tasks"]))
    try:
        client.authorize_interactive()
        email = client.check_connection()
    except TasksClientError as exc:
        print(f"Tasks OAuth setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Tasks OAuth complete. Authorized as: {email}")
    return 0


def run_apps_script_oauth(org_config: dict[str, Any]) -> int:
    client_config = _google_client_config(org_config)
    client = AppsScriptClient(client_config=client_config, token_file=_resolve_path(TOKEN_FILES["apps_script"]))
    try:
        client.authorize_interactive()
        email = client.check_connection()
    except AppsScriptClientError as exc:
        print(f"Apps Script OAuth setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Apps Script OAuth complete. Authorized as: {email}")
    return 0


def run_slack_oauth(org_config: dict[str, Any]) -> int:
    slack_org = org_config.get("slack") or {}
    if not slack_org.get("client_id") or not slack_org.get("client_secret"):
        print("No Slack organization config installed.", file=sys.stderr)
        return 1
    try:
        token = slack_authorize_interactive(
            client_id=slack_org["client_id"],
            client_secret=slack_org["client_secret"],
            token_file=_resolve_path(TOKEN_FILES["slack"]),
            user_scopes=slack_org.get("user_scopes"),
        )
    except SlackClientError as exc:
        print(f"Slack OAuth setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Slack OAuth complete. Authorized for workspace: {token.get('team_name')}")
    return 0


def run_salesforce_oauth(org_config: dict[str, Any]) -> int:
    sf_org = org_config.get("salesforce") or {}
    if not sf_org.get("consumer_key") or not sf_org.get("consumer_secret"):
        print("No Salesforce organization config installed.", file=sys.stderr)
        return 1
    try:
        token = salesforce_authorize_interactive(
            consumer_key=sf_org["consumer_key"],
            consumer_secret=sf_org["consumer_secret"],
            token_file=_resolve_path(TOKEN_FILES["salesforce"]),
            login_url=sf_org.get("login_url", "https://login.salesforce.com"),
        )
    except SalesforceClientError as exc:
        print(f"Salesforce OAuth setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Salesforce OAuth complete. Authorized for instance: {token.get('instance_url')}")
    return 0


def run_atlassian_oauth(org_config: dict[str, Any]) -> int:
    atlassian_org = org_config.get("atlassian") or {}
    if not atlassian_org.get("client_id") or not atlassian_org.get("client_secret"):
        print("No Atlassian organization config installed.", file=sys.stderr)
        return 1
    try:
        token = atlassian_authorize_interactive(
            client_id=atlassian_org["client_id"],
            client_secret=atlassian_org["client_secret"],
            token_file=_resolve_path(TOKEN_FILES["atlassian"]),
        )
    except AtlassianOAuthError as exc:
        print(f"Atlassian OAuth setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"Atlassian OAuth complete. Authorized for site: {token.get('site_url')}")
    return 0


def run_telegram_setup() -> int:
    creds = telegram_app_credentials()
    if not creds:
        print(
            "No Telegram app credentials in this build. For local dev, set "
            "PRIVACYFENCE_TELEGRAM_API_ID and PRIVACYFENCE_TELEGRAM_API_HASH.",
            file=sys.stderr,
        )
        return 1
    api_id, api_hash = creds
    session_file = _resolve_path(TOKEN_FILES["telegram"])
    client = TelegramPrivacyFenceClient(api_id=api_id, api_hash=api_hash, session_file=session_file)
    asyncio.run(client.authorize_interactive())
    print(f"Telegram session saved to {session_file}")
    return 0


# ---------------------------------------------------------------------------- #
# Main app
# ---------------------------------------------------------------------------- #

def run_app(config: dict[str, Any], config_path: str) -> int:
    if not _acquire_instance_lock():
        logger.error("Another instance is already running; exiting.")
        print("PrivacyFence daemon is already running.", file=sys.stderr)
        return 1

    init_config_path(_resolve_path(config_path))

    config, migration_summary = migrate_rules_to_grants(config)
    config, telegram_search_migrated = migrate_telegram_search_operation_key(config)
    if migration_summary or telegram_search_migrated:
        try:
            atomic_write_text(
                _resolve_path(config_path), yaml.safe_dump(config, default_flow_style=False, allow_unicode=True),
            )
            if migration_summary:
                logger.info(
                    "Auto-accept config migrated to connector-scoped grants:\n  %s",
                    "\n  ".join(migration_summary),
                )
            if telegram_search_migrated:
                logger.info(
                    "Auto-accept config migrated: telegram.search_messages rules "
                    "moved onto telegram.read_chat_messages"
                )
        except OSError as exc:
            logger.warning("Could not persist auto-accept config migration: %s", exc)

    reload_rules(build_effective_rules(config))
    if "rule_suggestion_priority" in config:
        # Issue #151: every matching auto-accept rule now gets its own
        # "Always allow" button, so there's nothing left to prioritize or
        # exclude -- same forward-compatible "unknown key is inert" posture
        # used elsewhere, not a dedicated migration (nothing to migrate
        # *to*). A pre-existing settings.yaml with this key still loads
        # without error; it's just never consulted again.
        logger.info(
            "rule_suggestion_priority is no longer used -- every matching rule now gets its own "
            "\"Always allow\" button. Ignoring this settings.yaml key."
        )
    pii_config = config.get("pii_detection", {}) or {}
    init_pii_detection(
        pii_config.get("enabled", True),
        detect_ip_addresses=pii_config.get("detect_ip_addresses", True),
        detect_financial_figures=pii_config.get("detect_financial_figures", True),
        audit_match_details=pii_config.get("audit_match_details", False),
    )
    # Loaded here, ahead of its previous spot just before build_connectors(),
    # so init_privacy_filter (SEC-07) knows whether this install is org-
    # managed before it picks a fail-safe default for a genuinely absent
    # privacy group -- still the one load_org_config() call for this whole
    # function, its ConfigurationError (SEC-04) still surfacing through the
    # same top-level "print and refuse to start" path in main().
    org_config = load_org_config()
    # SEC-09: same "org mode fails closed, local mode warns" posture as the
    # rest of this function's fail-safe defaults now that mode is known.
    check_storage_permissions(org_mode.resolve_mode(org_config) == "org")
    init_privacy_filter(config, org_managed=org_mode.resolve_mode(org_config) == "org")
    for warning in check_consistency_warnings():
        logger.warning(warning)

    # SEC-23: centralized forwarding is org-mode-only (see
    # org_mode.AuditForwardingConfig's own docstring) -- a local-mode
    # install's org_config.json could theoretically carry an
    # "audit_forwarding" section, but there is no "centralize" to speak of
    # for a single employee's own machine, so it's ignored outside org
    # mode regardless of what the section says.
    audit_forwarder = None
    audit_forwarding_config = org_mode.AuditForwardingConfig.from_org_config(org_config)
    if audit_forwarding_config.enabled and org_mode.resolve_mode(org_config) == "org":
        try:
            sender = audit_forwarding.build_sender(audit_forwarding_config)
            audit_forwarder = audit_forwarding.AuditForwarder(sender)
            logger.info("Audit-log forwarding enabled (%s)", audit_forwarding_config.kind)
        except Exception as exc:
            logger.warning("Could not start audit-log forwarding -- continuing without it: %s", exc)

    audit_logger = init_audit_logger(
        str(Path(data_dir()) / "logs" / "audit"),
        deployment_id=get_or_create_deployment_id(),
        security_config_hash=compute_security_config_hash(config),
        forwarder=audit_forwarder,
    )
    audit_logger.export_all_pending()

    # log_org_config_bundle_hash() needs the audit logger initialized above
    # (it records to it, see its own docstring) -- org_config itself was
    # already loaded earlier, ahead of init_privacy_filter(), so SEC-07's
    # org_managed fail-safe default is known before that call.
    log_org_config_bundle_hash(org_config)
    connectors = build_connectors(config, org_config)
    if not connectors:
        logger.warning("No connectors could be initialized; daemon still starting.")

    unattended_enabled = bool((org_config.get("unattended_sessions", {}) or {}).get("enabled", False))
    connector_host = ConnectorHost(connectors)

    # Built once, here, and handed to *both* the web settings surface
    # (_maybe_start_web_server, below) -- through P9 also handed to the
    # native menu bar/settings window, which P10 deleted. One
    # SettingsController instance for this daemon's whole lifetime, rather
    # than each surface building its own and drifting out of sync with the
    # other's in-memory state (_busy_connectors, _telegram_auth, the
    # update-check cache).
    from .settings_controller import SettingsController

    connector_names = [c.name for c in connectors]
    settings_controller = SettingsController(
        config_path=config_path, connectors=connector_names, connector_host=connector_host,
        connector_objs=connectors,
    )

    # Built after connector_host so the MCP dispatcher (if web.mcp.enabled)
    # can poll connector_host.connectors for the live connector set -- see
    # _maybe_start_web_server's own docstring.
    server = _maybe_start_web_server(
        config, connector_host, unattended_sessions_enabled=unattended_enabled, controller=settings_controller,
        org_config=org_config,
    )

    # Every connector call now runs on the embedded web server's own ASGI
    # event loop (McpDispatcher.call(), via /mcp) -- there is no separate
    # IPC loop to wait on any more, so Telegram's cache warm (the one piece
    # that needs a live loop, not just a thread -- see
    # _warm_connector_caches' own docstring) waits for that loop to be
    # captured instead. None when this daemon never starts the web server
    # at all -- since P10, that's only org mode with mcp.enabled off (see
    # _maybe_start_web_server's own docstring); local mode always starts
    # one now, so there's always a loop to wait for there.
    web_loop = server.wait_until_ready(timeout=5) if server is not None else None
    logger.info("Startup complete")

    if web_loop is not None:
        _warm_connector_caches(connectors, web_loop)
    elif server is not None:
        logger.warning("Web server event loop not ready in time; skipping background cache warm")

    threading.Thread(
        target=_run_update_check_timer, args=(settings_controller,),
        name="update-check-timer", daemon=True,
    ).start()

    try:
        _wait_for_shutdown()
    except KeyboardInterrupt:
        logger.info("Interrupted; shutting down")
    finally:
        audit_logger.close()
        _release_instance_lock()
    return 0


# ---------------------------------------------------------------------------- #
# Argument parsing
# ---------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="privacyfence-app",
        description="PrivacyFence daemon — governance UI and connector host.",
    )
    default_config = os.path.join(PROJECT_ROOT, "config", "settings.yaml")
    parser.add_argument("--config", default=default_config)
    parser.add_argument("--gmail-oauth", action="store_true")
    parser.add_argument("--drive-oauth", action="store_true")
    parser.add_argument("--contacts-oauth", action="store_true")
    parser.add_argument("--calendar-oauth", action="store_true")
    parser.add_argument("--tasks-oauth", action="store_true")
    parser.add_argument("--apps-script-oauth", action="store_true")
    parser.add_argument("--slack-oauth", action="store_true")
    parser.add_argument("--salesforce-oauth", action="store_true")
    parser.add_argument("--atlassian-oauth", action="store_true")
    parser.add_argument("--telegram-setup", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    oauth_flag = (
        args.gmail_oauth or args.drive_oauth or args.contacts_oauth
        or args.calendar_oauth or args.tasks_oauth or args.apps_script_oauth
        or args.slack_oauth
        or args.salesforce_oauth or args.atlassian_oauth or args.telegram_setup
    )

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    setup_logging(config)

    try:
        if oauth_flag:
            org_config = load_org_config()
            if args.gmail_oauth:
                return run_gmail_oauth(org_config)
            if args.drive_oauth:
                return run_drive_oauth(org_config)
            if args.contacts_oauth:
                return run_contacts_oauth(org_config)
            if args.calendar_oauth:
                return run_calendar_oauth(org_config)
            if args.tasks_oauth:
                return run_tasks_oauth(org_config)
            if args.apps_script_oauth:
                return run_apps_script_oauth(org_config)
            if args.slack_oauth:
                return run_slack_oauth(org_config)
            if args.salesforce_oauth:
                return run_salesforce_oauth(org_config)
            if args.atlassian_oauth:
                return run_atlassian_oauth(org_config)
            if args.telegram_setup:
                return run_telegram_setup()
        return run_app(config, args.config)
    except Exception as exc:
        logger.error("Fatal error: %s", exc, exc_info=True)
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
