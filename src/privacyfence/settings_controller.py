"""Domain/business logic behind the webview settings window (settings_window.py).

No AppKit/WebKit imports at module level -- this stays importable and
unit-testable without PyObjC (see docs/coding-and-testing-guidelines.md's
"stay dependency-light" pattern already used by resource_grants.py/
privacy_filter.py). One thing this module genuinely needs *is*
AppKit-tainted (rumps.Window is how the Telegram sign-in flow's native text
prompts still work -- see its own docstring below for why that one flow
keeps a native prompt) -- that's imported lazily, inside the function that
needs it, not at module scope, so a plain
``import privacyfence.settings_controller`` never touches AppKit even though
that one method does once actually called on a real macOS run.

``SettingsController`` holds the same instance state ``PrivacyFenceMenuBar``
used to hold directly, with one method per mutation the old NSMenu tree
performed (see menu_bar.py's git history pre-#120 for the shape this was
extracted from) -- every mutating method follows load config -> mutate ->
save config -> hot-reload -> return a fresh ``snapshot()`` for the caller
(settings_window.py) to push into the webview. Long-running work (OAuth
flows, org-config file picker's subprocess, grant name resolution) runs on a
background thread via ``_run_async``, with the result marshaled back onto
the main thread via ``PyObjCTools.AppHelper.callAfter`` before this module
touches ``self`` again -- AppKit/the webview are not thread-safe, and
``self.on_change`` (set by settings_window.py) is expected to touch the
webview directly.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from . import __version__
from .app_credentials import telegram_app_credentials
from .audit_log import AuditLogger, current_week
from .auto_accept import (
    SUGGESTION_FAMILIES,
    reload_rules,
    set_rules_changed_listener,
    set_suggestion_priority,
    suggestion_order,
)
from .calendar_client import CalendarClient
from .contacts_client import ContactsClient
from .drive_client import DriveClient
from .gmail_client import GmailClient
from .paths import data_dir, org_dir
from .pii_detector import set_pii_category_enabled, set_pii_detection_enabled
from .privacy_filter import _parse_group as _parse_privacy_group
from .privacy_filter import _VALID_POLICIES as PRIVACY_POLICIES
from .privacy_filter import init_privacy_filter
from .resource_grants import (
    GRANT_RESOURCE_TYPES,
    GrantResourceType,
    build_effective_rules,
    get_grant_entries,
    resource_type as grant_resource_type,
    resource_types_for_connector,
    set_grant_entries,
)
from .resource_names import get_resolver
from .tasks_client import TasksClient
from .update_checker import (
    REPO_RELEASES_URL_FALLBACK,
    UpdateCheckResult,
    check_for_update,
    mark_remind_later,
    mark_skipped,
    should_notify_now,
)
from .atlassian_oauth import authorize_interactive as atlassian_authorize_interactive
from .salesforce_client import authorize_interactive as salesforce_authorize_interactive
from .slack_client import authorize_interactive as slack_authorize_interactive

# AppHelper/rumps are pyobjc packages -- both ultimately backed by AppKit.
# Guarded so a plain `import privacyfence.settings_controller` succeeds on a
# machine with no pyobjc installed (this repo's own CI-less sandbox, or a
# future non-interactive test run); the names resolve to None there, and
# every real call site only runs them on an actual macOS/pyobjc process.
# Tests running on macOS CI monkeypatch these attributes directly, the same
# way test_menu_bar.py already patches ``menu_bar.AppHelper.callAfter``.
try:
    from PyObjCTools import AppHelper
except ImportError:  # pragma: no cover - exercised only where pyobjc is present
    AppHelper = None  # type: ignore[assignment]

try:
    import rumps
except ImportError:  # pragma: no cover - exercised only where pyobjc is present
    rumps = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

REPO_URL = "https://github.com/andras-tkcs/privacyfence"
LICENSE_NAME = "Apache-2.0"

# ---------------------------------------------------------------------------- #
# Rule metadata (moved verbatim from menu_bar.py -- see its pre-#120 history)
# ---------------------------------------------------------------------------- #

OPERATION_LABELS: dict[str, str] = {
    "gmail.read_message":          "Gmail – Read message",
    "gmail.read_thread":           "Gmail – Read thread",
    "gmail.download_attachment":   "Gmail – Download attachment",
    "gmail.create_draft":          "Gmail – Create draft",
    "gmail.add_label":             "Gmail – Add label",
    "gmail.remove_label":          "Gmail – Remove label",
    "gmail.archive_message":       "Gmail – Archive message",
    "gmail.create_label":          "Gmail – Create label",
    "drive.read_file_contents":    "Drive – Read file",
    "drive.download_file":         "Drive – Download file",
    "drive.write_file":            "Drive – Write file",
    "drive.write_doc":             "Drive – Write Google Doc",
    "drive.upload_file":           "Drive – Upload file",
    "drive.move_file":             "Drive – Move file",
    "drive.comment_file":          "Drive – Add comment",
    "sheets.read_values":          "Sheets – Read values",
    "sheets.write_range":          "Sheets – Write range",
    "sheets.add_sheet":            "Sheets – Add tab",
    "sheets.rename_sheet":         "Sheets – Rename tab",
    "sheets.format_range":         "Sheets – Format range",
    "sheets.insert_dimensions":    "Sheets – Insert rows/columns",
    "sheets.delete_dimensions":    "Sheets – Delete rows/columns",
    "docs.edit_content":           "Docs – Edit content",
    "docs.format_content":         "Docs – Format content",
    "slack.read_messages":         "Slack – Read messages",
    "slack.send_message":          "Slack – Send message",
    "calendar.read_event_details": "Calendar – Read event",
    "calendar.create_modify_event":"Calendar – Create/modify event",
    "calendar.set_visibility":     "Calendar – Set event visibility",
    "calendar.out_of_office":      "Calendar – Create out-of-office",
    "calendar.working_location":   "Calendar – Set working location",
    "salesforce.read_record":      "Salesforce – Read record",
    "salesforce.run_report":       "Salesforce – Run report",
    "salesforce.search":           "Salesforce – Search",
    "contacts.edit":               "Contacts – Update contact",
    "contacts.create":             "Contacts – Create contact",
    "contacts.add_label":          "Contacts – Add label",
    "contacts.remove_label":       "Contacts – Remove label",
    "jira.read_issue":             "Jira – Read issue",
    "jira.create_issue":           "Jira – Create issue",
    "jira.add_comment":            "Jira – Add comment",
    "jira.update_issue":           "Jira – Update issue",
    "jira.transition_issue":       "Jira – Transition issue",
    "confluence.read_page":        "Confluence – Read page",
    "confluence.download_attachment": "Confluence – Download attachment",
    "confluence.create_page":      "Confluence – Create page",
    "confluence.update_page":      "Confluence – Update page",
    # telegram_search_messages shares this key with telegram_get_messages
    # (see auto_accept.TOOL_TO_OPERATION) rather than its own
    # "telegram.search_messages" -- one label covers both tools' rules.
    "telegram.read_chat_messages": "Telegram – Read/search chat messages",
    "telegram.send_message":       "Telegram – Send message",
    "tasks.create_task":           "Tasks – Create task",
    "tasks.update_task":           "Tasks – Update task",
    "tasks.complete_task":         "Tasks – Complete task",
    "tasks.uncomplete_task":       "Tasks – Uncomplete task",
    "tasks.move_task":             "Tasks – Move task",
}

RULES_BY_OPERATION: dict[str, list[str]] = {
    "gmail.read_message":           ["i_am_sender", "i_am_sole_recipient", "trusted_sender_domain", "label_match", "age_threshold_days", "no_attachments"],
    "gmail.read_thread":            ["i_am_sender", "trusted_sender_domain", "age_threshold_days"],
    "gmail.download_attachment":    ["i_am_sender", "trusted_sender_domain", "label_match"],
    "gmail.create_draft":           ["to_is_myself", "approved_recipient_domain", "always_allow"],
    "gmail.add_label":              ["label_name_allowlist", "i_am_sender", "trusted_sender_domain"],
    "gmail.remove_label":           ["label_name_allowlist", "i_am_sender", "trusted_sender_domain"],
    "gmail.archive_message":        ["i_am_sender", "trusted_sender_domain", "label_match"],
    "gmail.create_label":           ["label_name_allowlist"],
    "drive.read_file_contents":     ["i_am_owner", "created_by_me", "approved_folder", "file_type_allowlist", "created_this_session", "shared_drive_exclusion"],
    "drive.download_file":          ["i_am_owner", "approved_folder", "file_type_allowlist", "created_this_session", "shared_drive_exclusion"],
    "drive.write_file":             ["i_am_owner", "approved_sandbox_folder", "file_type_allowlist", "created_this_session"],
    "drive.write_doc":              ["i_am_owner", "approved_sandbox_folder", "created_this_session"],
    "drive.upload_file":            ["parent_folder_allowlist"],
    "drive.move_file":              ["move_within_approved_folders"],
    "drive.comment_file":           ["i_am_owner", "approved_sandbox_folder", "created_this_session"],
    "sheets.read_values":           ["i_am_owner", "created_by_me", "approved_folder", "created_this_session", "shared_drive_exclusion"],
    "sheets.write_range":           ["i_am_owner", "approved_sandbox_folder", "created_this_session"],
    "sheets.add_sheet":             ["i_am_owner", "approved_sandbox_folder", "created_this_session"],
    "sheets.rename_sheet":          ["i_am_owner", "approved_sandbox_folder", "created_this_session"],
    "sheets.format_range":          ["i_am_owner", "approved_sandbox_folder", "created_this_session"],
    "sheets.insert_dimensions":     ["i_am_owner", "approved_sandbox_folder", "created_this_session"],
    "sheets.delete_dimensions":     ["i_am_owner", "approved_sandbox_folder", "created_this_session"],
    "docs.edit_content":            ["i_am_owner", "approved_sandbox_folder", "created_this_session"],
    "docs.format_content":          ["i_am_owner", "approved_sandbox_folder", "created_this_session"],
    "slack.read_messages":          ["dm_with_myself", "group_dm", "approved_channel", "approved_channel_all_results", "public_channels_only", "no_file_attachments"],
    "slack.send_message":           ["dm_with_myself", "send_to_myself", "approved_channel", "approved_recipient", "reply_in_existing_thread"],
    "calendar.read_event_details":  ["i_am_organizer", "no_external_attendees", "personal_calendar", "past_event", "time_window_days", "no_conferencing_link", "non_private_event"],
    "calendar.create_modify_event": ["i_am_organizer", "no_external_attendees", "personal_calendar"],
    "calendar.set_visibility":      ["i_am_organizer", "no_external_attendees", "personal_calendar"],
    "calendar.out_of_office":       ["always_allow"],
    "calendar.working_location":    ["always_allow"],
    "salesforce.read_record":       ["approved_object_types"],
    "salesforce.run_report":        ["approved_report_ids"],
    "salesforce.search":            ["approved_object_types"],
    "contacts.edit":                ["no_contact_info_change"],
    "contacts.create":              ["no_contact_info_change"],
    "contacts.add_label":           ["label_name_allowlist"],
    "contacts.remove_label":        ["label_name_allowlist"],
    "jira.read_issue":              ["i_am_reporter", "i_am_assignee", "approved_project_keys"],
    "jira.create_issue":            ["approved_project_keys"],
    "jira.add_comment":             ["approved_project_keys"],
    "jira.update_issue":            ["approved_project_keys"],
    "jira.transition_issue":        ["approved_project_keys"],
    "confluence.read_page":         ["i_am_author", "approved_space_keys"],
    "confluence.download_attachment": ["i_am_author", "approved_space_keys"],
    "confluence.create_page":       ["approved_space_keys"],
    "confluence.update_page":       ["approved_space_keys"],
    "telegram.read_chat_messages":  ["approved_chats", "approved_chats_all_results", "no_media_attachments"],
    "telegram.send_message":        ["approved_chats"],
    "tasks.create_task":            ["approved_task_list"],
    "tasks.update_task":            ["approved_task_list"],
    "tasks.complete_task":          ["approved_task_list"],
    "tasks.uncomplete_task":        ["approved_task_list"],
    "tasks.move_task":              ["approved_task_list"],
}

# Rules that take a list-of-strings value
RULES_LIST_VALUE: set[str] = {
    "trusted_sender_domain", "label_match", "send_to_myself",
    "approved_channel", "approved_channel_all_results", "approved_recipient", "personal_calendar",
    "approved_object_types", "approved_report_ids", "file_type_allowlist",
    "approved_folder", "approved_sandbox_folder",
    "approved_recipient_domain", "label_name_allowlist", "parent_folder_allowlist",
    "approved_project_keys", "approved_space_keys", "approved_chats",
    "approved_chats_all_results", "approved_task_list",
}
# Rules that take a single integer value
RULES_INT_VALUE: set[str] = {"age_threshold_days", "time_window_days"}

# All connectors PrivacyFence supports, in display order
ALL_CONNECTORS: list[str] = [
    "gmail", "drive", "contacts", "calendar", "tasks",
    "slack", "jira", "confluence", "salesforce", "telegram",
]

# Top-level groups shown in the Auto-accept Rules page specifically --
# distinct from ALL_CONNECTORS because "sheets" and "docs" aren't connectors
# (neither has a separate auth, org-config section, or entry in
# GOOGLE_CONNECTORS/_GOOGLE_CLIENTS/ORG_CONFIG_SERVICE -- both ride on
# Drive's OAuth grant), but their rules live under their own "sheets.*"/
# "docs.*" operation keys (see TOOL_TO_OPERATION in auto_accept.py) rather
# than nested under "drive.*", so the connector-prefix grouping below needs
# them listed here, or the whole bucket is silently dropped (never
# iterated, so never rendered).
RULES_MENU_GROUPS: list[str] = [
    "gmail", "drive", "sheets", "docs", "contacts", "calendar", "tasks",
    "slack", "jira", "confluence", "salesforce", "telegram",
]

# Sheets and Docs pages have no grant section of their own -- neither is a
# real connector (see RULES_MENU_GROUPS' own comment above), so
# resource_types_for_connector("sheets"/"docs") is always empty -- even
# though a folder granted Drive's Trusted Folders "read" or Sandbox Folders
# "write" capability silently covers rows on both of these pages too
# (sheets.read_values; every sheets.*/docs.* write -- see
# resource_grants.DRIVE_FOLDER_READ_TARGETS/DRIVE_SANDBOX_WRITE_TARGETS).
# _rules_state's drive_grant_summary_by_connector carries a read-only
# pointer back to Drive for exactly these two pages, so that's discoverable
# without already knowing to go check Drive's own page.
DRIVE_GRANT_SUMMARY_GROUPS: tuple[str, ...] = ("sheets", "docs")

# Which connector's Auto-accept Rules page gets an "Always-allow Suggestion
# Order" block -- one per auto_accept.SUGGESTION_FAMILIES entry. Drive's
# family covers drive.read_file_contents/drive.download_file, both under
# the "drive" connector, so this is a 1:1 connector->family map even though
# a family could in principle span connectors. Restored per user direction
# after being dropped in the first pass of issue #120 (the design mockup's
# Rules page has no UI for this) -- see _rules_state's suggestion_priority_
# by_connector and settings_window_html.py's rendering of it.
SUGGESTION_FAMILY_BY_CONNECTOR: dict[str, str] = {
    "drive": "drive_read",
    "calendar": "calendar_read_event",
    "jira": "jira_read_issue",
    "confluence": "confluence_read_page",
}

# Connectors authenticated via a shared Google OAuth client (org bundle's
# "google" section).
GOOGLE_CONNECTORS: set[str] = {"gmail", "drive", "contacts", "calendar", "tasks"}

# Which section of the organization config bundle each connector depends on.
# Jira and Confluence share one Atlassian OAuth grant. Telegram is not part
# of the org bundle -- its app credentials are baked into the build (see
# app_credentials.py) and checked separately.
ORG_CONFIG_SERVICE: dict[str, str] = {
    "gmail": "google", "drive": "google", "contacts": "google",
    "calendar": "google", "tasks": "google",
    "slack": "slack",
    "jira": "atlassian", "confluence": "atlassian",
    "salesforce": "salesforce",
}
ORG_BUNDLE_SERVICES: list[str] = ["google", "slack", "salesforce", "atlassian"]

# Categories individually toggleable under the PII Detection Gate, on top of
# its own master enabled switch. Keys match pii_detector._OPTIONAL_CATEGORIES
# and the settings.yaml field names.
PII_OPTIONAL_CATEGORIES: list[tuple[str, str]] = [
    ("detect_ip_addresses", "Detect IP Addresses"),
    ("detect_financial_figures", "Detect Financial Figures"),
]

_GOOGLE_CLIENTS: dict[str, type] = {
    "gmail": GmailClient,
    "drive": DriveClient,
    "calendar": CalendarClient,
    "contacts": ContactsClient,
    "tasks": TasksClient,
}

RULE_HINTS: dict[str, str] = {
    "trusted_sender_domain": "domain1.com, domain2.com",
    "label_match":           "INBOX, UNREAD",
    "age_threshold_days":    "30",
    "send_to_myself":        "U0123456789",
    "approved_channel":      "C0123456789, C9876543210",
    "approved_channel_all_results": "C0123456789, C9876543210",
    "approved_recipient":    "U0123456789",
    "personal_calendar":     "primary",
    "time_window_days":      "14",
    "approved_object_types": "Account, Contact, Opportunity",
    "approved_report_ids":   "00O000000000001",
    "file_type_allowlist":   "application/vnd.google-apps.document, text/plain",
    "approved_folder":       "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
    "approved_sandbox_folder": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
    "approved_recipient_domain": "domain1.com, domain2.com",
    "label_name_allowlist": "Newsletters, Receipts",
    "parent_folder_allowlist": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
    "approved_project_keys": "MYPROJ, OTHERPROJ",
    "approved_space_keys":   "TEAM, DOCS",
    "approved_chats":        "123456789, -100987654321",
    "approved_chats_all_results": "123456789, -100987654321",
    "approved_task_list":    "MDAwMDAwMDAwMDAwMDAwMDAwMDA6MDow",
}

# Display metadata for the Privacy Filter page -- mirrors the group/category
# schema documented in resources/settings.yaml.example and enforced by
# privacy_filter.py. Every group privacy_filter.py knows about needs an
# entry here (and a matching PRIVACY_CATEGORY_LABELS sub-dict) to actually
# show up in the window.
PRIVACY_GROUP_LABELS: dict[str, str] = {
    "privacy": "Gmail",
    "drive_privacy": "Drive & Sheets",
    "slack_privacy": "Slack",
    "contacts_privacy": "Contacts",
    "tasks_privacy": "Tasks",
    "confluence_privacy": "Confluence",
}
PRIVACY_CATEGORY_LABELS: dict[str, dict[str, str]] = {
    "privacy": {
        "body": "Message body",
        "metadata": "Metadata (sender / recipients / date / subject)",
        "attachments": "Attachment metadata",
        "thread_history": "Thread history",
    },
    "drive_privacy": {
        "file_content": "Document content",
        "file_metadata": "File metadata (name / owners / dates / sharing)",
        "file_list": "File list results",
        "folder_structure": "Folder structure",
    },
    "slack_privacy": {
        "message_content": "Message text",
        "user_identity": "User identity (names / emails)",
        "channel_list": "Channel list",
        "thread_content": "Thread replies",
        "dm_list": "DM list",
        "group_chat_list": "Group chat list",
    },
    "contacts_privacy": {
        "notes": "Contact notes (free-text biography)",
    },
    "tasks_privacy": {
        "notes": "Task notes (free-text)",
    },
    "confluence_privacy": {
        "search_excerpt": "Search result excerpt",
        "attachments": "Attachment metadata",
    },
}

# Rule names configured through a Trusted-resource grant (see
# resource_grants.py), not hand-authored -- a compiled entry under one of
# these names is a pointer back to the grant, not something the Rules page's
# text-input rows edit directly (see _rules_state's `_grant` skip).
GRANT_COVERED_RULE_NAMES: set[str] = {
    rule_name
    for rt in GRANT_RESOURCE_TYPES
    for capability in rt.capabilities.values()
    for _op_key, rule_name in capability.targets
}

# Rule names whose value is the same kind of opaque resource ID a grant entry
# stores (a Drive folder ID, a Jira project key, ...), mapped to the resource
# type that knows how to resolve one to a display name -- so a hand-authored
# rule entry under one of these names still shows a real name instead of a
# raw ID, the same way a grant entry does. Mostly the grant-covered rule
# names, plus a few that hold the same kind of ID but aren't tied to any
# grant capability -- parent_folder_allowlist has no "auto-accept uploads
# into this folder" toggle in the grants UI, it's a hand-authored-only
# allowlist, but its values are still plain Drive folder IDs worth resolving.
RULE_NAME_TO_RESOURCE_TYPE: dict[str, GrantResourceType] = {
    rule_name: rt
    for rt in GRANT_RESOURCE_TYPES
    for capability in rt.capabilities.values()
    for _op_key, rule_name in capability.targets
}
_drive_folder_rt = grant_resource_type("drive", "folders")
assert _drive_folder_rt is not None
RULE_NAME_TO_RESOURCE_TYPE["parent_folder_allowlist"] = _drive_folder_rt

# Drive/Sheets URLs paste-able into a grant's ID field, so the user can copy
# the browser address bar instead of hand-extracting the ID segment. Order
# matters: a file URL also contains "/d/" so the folder pattern is tried
# first.
_DRIVE_FOLDER_URL_RE = re.compile(r"/folders/([a-zA-Z0-9_-]+)")
_DRIVE_FILE_URL_RE = re.compile(r"/d/([a-zA-Z0-9_-]+)")


def _extract_drive_id(text: str) -> str:
    """Pull a Drive/Sheets file or folder ID out of a pasted URL, or accept
    a bare ID as-is. Returns "" if nothing usable was found."""
    text = text.strip()
    for pattern in (_DRIVE_FOLDER_URL_RE, _DRIVE_FILE_URL_RE):
        m = pattern.search(text)
        if m:
            return m.group(1)
    if text and "/" not in text and " " not in text:
        return text
    return ""


def _short_id(resource_id: str, head: int = 8, tail: int = 6) -> str:
    if len(resource_id) <= head + tail + 1:
        return resource_id
    return f"{resource_id[:head]}…{resource_id[-tail:]}"


def _google_client_config(org_config: dict[str, Any]) -> dict[str, Any]:
    google = org_config.get("google") or {}
    if not google.get("client_id") or not google.get("client_secret"):
        return {}
    return {"installed": google}


def _run_async(work: Callable[[], Any], on_done: Callable[[bool, Any], None]) -> None:
    """Run ``work()`` on a background thread.

    ``on_done(ok, result)`` is called on the main thread via
    AppHelper.callAfter -- ``result`` is the return value on success, or the
    raised exception on failure. Never touch AppKit/the webview from
    ``work``; do it in ``on_done``. Relocated from menu_bar.py's identical
    helper -- see its own pre-#120 history.
    """
    def _runner() -> None:
        try:
            result = work()
            AppHelper.callAfter(on_done, True, result)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user via on_done
            AppHelper.callAfter(on_done, False, exc)

    threading.Thread(target=_runner, daemon=True).start()


def _parse_rule_value(rule_name: str, raw_text: str) -> Any:
    """Text-input value -> stored config value, per the rule's known shape
    (RULES_LIST_VALUE/RULES_INT_VALUE), or the plain string as-typed for an
    unrecognized rule name. Empty text means "boolean rule, no value"."""
    raw_text = (raw_text or "").strip()
    if not raw_text:
        return None
    if rule_name in RULES_LIST_VALUE:
        return [v.strip() for v in raw_text.split(",") if v.strip()]
    if rule_name in RULES_INT_VALUE:
        try:
            return int(raw_text)
        except ValueError:
            # Left as typed rather than rejected outright -- the evaluator's
            # own rule matching simply won't match a non-numeric value for
            # an int-value rule, which is a softer failure than blocking the
            # keystroke; see this module's docstring on text-input commit
            # semantics (commit on blur/Enter, not per-keystroke).
            return raw_text
    return raw_text


def _format_rule_value(rule_name: str, value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def _relative_time(timestamp: str) -> str:
    try:
        dt = datetime.fromisoformat(timestamp)
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


# ---------------------------------------------------------------------------- #
# Controller
# ---------------------------------------------------------------------------- #

class SettingsController:
    """Domain logic for the settings window. One instance, owned by
    menu_bar.py's PrivacyFenceMenuBar for the app's whole lifetime (unlike
    SettingsWindowController, which is created lazily on first "Open
    PrivacyFence…" click) -- so ``set_rules_changed_listener``/
    ``ipc_server.set_unattended_changed_listener`` are registered from
    __init__ here exactly the way PrivacyFenceMenuBar.__init__ used to,
    regardless of whether the window has ever been opened yet.

    ``on_change``, set by settings_window.py once its window exists, is
    called with a fresh ``snapshot()`` whenever something changes the state
    out from under a currently-open window (a rule added via the approval
    popup's Always allow, a background auth flow finishing, an unattended
    session starting/ending). It is a no-op (never set) until the window has
    been opened at least once -- there is nothing to push a re-render into
    before that.
    """

    def __init__(
        self,
        config_path: str,
        connectors: list[str],
        ipc_server: Any,
        connector_objs: list[Any] | None = None,
    ) -> None:
        self._config_path = config_path
        self._connectors = connectors
        self.ipc_server = ipc_server
        # name -> live Connector wrapper (exposes .client for resolving
        # grant resource names -- see resource_names.py). Populated at
        # startup from daemon_main.py's already-built connectors, refreshed
        # whenever refresh_connectors() re-authenticates/toggles one.
        self._connector_objs: dict[str, Any] = {c.name: c for c in (connector_objs or [])}
        self._resolver = get_resolver()
        # Latest known update-check outcome -- None until the first check
        # completes (or forever, if update checking is disabled). The
        # design's General page has no "update available" banner yet (see
        # this module's own docstring/the PR report for that gap) -- this is
        # kept so a future pass can surface it without re-plumbing.
        self._latest_update: UpdateCheckResult | None = None
        # Connector keys with an authenticate/refresh flow currently
        # in flight -- surfaced in snapshot()'s connectors[].busy so the
        # window can disable/spin that row instead of double-firing.
        self._busy_connectors: set[str] = set()
        # Last-seen failure message, surfaced as a small dismissable banner
        # by the window (see settings_window_html.py) -- the design has no
        # toast/error UI of its own, and simply dropping every rumps.alert
        # this module's methods used to show would silently regress error
        # visibility (see the PR report's "error surfacing" scope note).
        self.error: str = ""
        # Telegram's in-progress phone/code/2FA sign-in, or None when no
        # sign-in is running -- see telegram_start_auth/telegram_submit_code/
        # telegram_submit_2fa/telegram_cancel_auth below and _telegram_auth_
        # state's own docstring for the shape.
        self._telegram_auth: dict[str, Any] | None = None
        self.on_change: Callable[[dict[str, Any]], None] | None = None

        set_rules_changed_listener(self._on_rules_changed)
        if self.ipc_server is not None:
            self.ipc_server.set_unattended_changed_listener(self._on_unattended_changed)

    # ------------------------------------------------------------------ #
    # Cross-thread change notifications
    # ------------------------------------------------------------------ #

    def _on_rules_changed(self) -> None:
        """Fired by auto_accept.reload_rules(), possibly from the IPC
        server's thread -- marshal the state push onto the main thread."""
        AppHelper.callAfter(self._push_snapshot)

    def _on_unattended_changed(self) -> None:
        """Fired by ipc_server.py, on its own asyncio thread. No page of
        the current design surfaces the unattended-session count (the old
        tray menu's top status line is gone) -- kept wired for a future
        pass, same "plumbing survives, UI doesn't exist yet" posture as
        _latest_update above."""
        AppHelper.callAfter(self._push_snapshot)

    def _push_snapshot(self) -> None:
        if self.on_change is not None:
            self.on_change(self.snapshot())

    # ------------------------------------------------------------------ #
    # Config helpers
    # ------------------------------------------------------------------ #

    def _load_config(self) -> dict:
        try:
            with open(self._config_path, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as exc:
            logger.warning("Could not load config: %s", exc)
            return {}

    def _save_config(self, cfg: dict) -> None:
        try:
            with open(self._config_path, "w", encoding="utf-8") as f:
                yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
        except Exception as exc:
            logger.warning("Could not save config: %s", exc)

    def _save_and_reload(self, cfg: dict) -> None:
        self._save_config(cfg)
        try:
            # Triggers _on_rules_changed() -> a snapshot push, so callers
            # don't need a separate explicit push after this.
            reload_rules(build_effective_rules(cfg))
        except Exception as exc:
            logger.warning("Rule hot-reload failed: %s", exc)

    def _save_and_reload_privacy(self, cfg: dict) -> None:
        self._save_config(cfg)
        try:
            init_privacy_filter(cfg)
        except Exception as exc:
            logger.warning("Privacy filter hot-reload failed: %s", exc)

    def _client_for(self, connector: str) -> Any | None:
        """Live client for a connected connector (for resolving/listing
        grant resources), or None if that connector isn't currently
        connected."""
        conn = self._connector_objs.get(connector)
        return getattr(conn, "client", None) if conn is not None else None

    # ------------------------------------------------------------------ #
    # PII detection gate
    # ------------------------------------------------------------------ #

    def toggle_pii_detection(self) -> dict[str, Any]:
        cfg = self._load_config()
        pii_cfg = cfg.setdefault("pii_detection", {})
        enabled = not pii_cfg.get("enabled", True)
        pii_cfg["enabled"] = enabled
        self._save_config(cfg)
        set_pii_detection_enabled(enabled)
        return self.snapshot()

    def toggle_pii_category(self, category_key: str) -> dict[str, Any]:
        cfg = self._load_config()
        pii_cfg = cfg.setdefault("pii_detection", {})
        if not pii_cfg.get("enabled", True):
            # Mirrors the old submenu's grayed-out-without-a-callback state
            # while the master switch is off -- these two categories are
            # meaningless without it.
            return self.snapshot()
        enabled = not pii_cfg.get(category_key, True)
        pii_cfg[category_key] = enabled
        self._save_config(cfg)
        set_pii_category_enabled(category_key, enabled)
        return self.snapshot()

    # ------------------------------------------------------------------ #
    # Update checker (see update_checker.py)
    # ------------------------------------------------------------------ #

    def toggle_update_check(self) -> dict[str, Any]:
        cfg = self._load_config()
        update_check_cfg = cfg.setdefault("update_check", {})
        update_check_cfg["enabled"] = not update_check_cfg.get("enabled", True)
        self._save_config(cfg)
        return self.snapshot()

    def toggle_update_check_beta(self) -> dict[str, Any]:
        cfg = self._load_config()
        update_check_cfg = cfg.setdefault("update_check", {})
        update_check_cfg["include_beta"] = not update_check_cfg.get("include_beta", False)
        self._save_config(cfg)
        # The toggle itself is a channel switch -- check right away instead
        # of waiting for the next timer pulse.
        self.check_for_updates_now()
        return self.snapshot()

    def on_update_check_timer(self) -> None:
        """Periodic "is it time to check yet?" pulse -- called by
        menu_bar.py's rumps.Timer (rumps/AppKit stays there; this class has
        none of it). check_for_update() re-derives whether 24h have actually
        passed from its own on-disk timestamp."""
        cfg = self._load_config()
        update_check_cfg = cfg.get("update_check", {}) or {}
        if not update_check_cfg.get("enabled", True):
            return
        self.check_for_updates_now()

    def check_for_updates_now(self) -> dict[str, Any]:
        """Manual "Check for Updates" (About page) or the beta-toggle/timer
        pulse above -- always actually checks, ignoring the 24h throttle
        that's only inside check_for_update() for the periodic pulse's
        implicit callers."""
        cfg = self._load_config()
        include_beta = (cfg.get("update_check", {}) or {}).get("include_beta", False)
        _run_async(lambda: check_for_update(include_beta=include_beta), self._on_update_check_done)
        return self.snapshot()

    def _on_update_check_done(self, ok: bool, result: Any) -> None:
        if not ok:
            logger.warning("Update check failed: %s", result)
            return
        self._latest_update = result
        self._push_snapshot()
        if result is not None and result.is_update_available and should_notify_now():
            # No in-webview "update available" dialog exists yet (see this
            # class's docstring) -- the native alert is kept as the one
            # surviving notification path for this specific event so an
            # available update isn't silently unreported.
            self._show_update_available_alert(result)

    def _show_update_available_alert(self, result: UpdateCheckResult) -> None:
        beta_note = " (beta)" if result.is_beta else ""
        resp = rumps.alert(
            title="Update Available",
            message=f"PrivacyFence {result.latest_version}{beta_note} is available "
                     f"(you have {__version__}).",
            ok="Download",
            cancel="Skip This Version",
            other="Remind Me Later",
        )
        if resp == 1:
            url = result.release_url
            if not url.startswith(("http://", "https://")):
                url = REPO_RELEASES_URL_FALLBACK
            subprocess.run(["open", url], check=False)
        elif resp == 0:
            mark_skipped(result.latest_version)
        else:
            mark_remind_later()

    # ------------------------------------------------------------------ #
    # Organization config bundle
    # ------------------------------------------------------------------ #

    def install_org_config(self) -> dict[str, Any]:
        """Native "choose file" picker + install, run synchronously on the
        calling (main) thread -- matches the pre-#120 behavior exactly. This
        is an incidental native file-open dialog, not part of what issue
        #120 targets for removal (unlike the NSMenu tree/rules_manager_
        window.py's own windows)."""
        script = (
            'set chosenFile to choose file with prompt '
            '"Select the organization config bundle your IT team sent you" '
            'of type {"json", "public.json"}\n'
            'return POSIX path of chosenFile'
        )
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        src = result.stdout.strip()
        if not src:
            return self.snapshot()

        try:
            with open(src, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            self.error = f"Could not read that file as JSON: {exc}"
            return self.snapshot()
        if not isinstance(data, dict) or "version" not in data:
            self.error = (
                "That file doesn't look like a PrivacyFence organization config bundle "
                '(expected a JSON object with a "version" field).'
            )
            return self.snapshot()

        dest = org_dir() / "org_config.json"
        try:
            with open(dest, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.chmod(dest, 0o600)
        except OSError as exc:
            self.error = f"Could not install organization config: {exc}"
            return self.snapshot()

        self.error = ""
        return self.snapshot()

    # ------------------------------------------------------------------ #
    # Connector actions
    # ------------------------------------------------------------------ #

    def toggle_connector(self, connector: str) -> dict[str, Any]:
        cfg = self._load_config()
        conn = cfg.setdefault("connectors", {}).setdefault(connector, {})
        conn["enabled"] = not conn.get("enabled", True)
        self._save_config(cfg)
        self.refresh_connectors()
        return self.snapshot()

    def refresh_connectors(self) -> dict[str, Any]:
        """Re-run connector construction (which re-checks auth/enabled state
        for every service) and push the result live into the running IPC
        server, so authenticating or toggling a connector takes effect
        immediately instead of requiring a restart."""

        def work() -> list:
            from .daemon_main import build_connectors, load_org_config
            cfg = self._load_config()
            org_config = load_org_config()
            return build_connectors(cfg, org_config)

        def done(ok: bool, result: Any) -> None:
            if ok:
                self._connectors = [c.name for c in result]
                self._connector_objs = {c.name: c for c in result}
                if self.ipc_server is not None:
                    self.ipc_server.set_connectors(result)
            self._push_snapshot()

        _run_async(work, done)
        return self.snapshot()

    def authenticate_connector(self, connector: str) -> dict[str, Any]:
        """OAuth-style single-click connectors only -- Telegram's
        multi-step phone/code/2FA flow is routed client-side into its own
        modal instead (see settings_window_html.py's connector row
        handling and telegram_start_auth/telegram_submit_code/
        telegram_submit_2fa/telegram_cancel_auth below), so this is never
        called with connector == "telegram" from production JS. A stray
        call is a harmless no-op rather than an error."""
        from .daemon_main import load_org_config
        org_config = load_org_config()
        if connector in GOOGLE_CONNECTORS:
            self._authenticate_google(connector, org_config)
        elif connector == "slack":
            self._authenticate_slack(org_config)
        elif connector == "salesforce":
            self._authenticate_salesforce(org_config)
        elif connector in ("jira", "confluence"):
            self._authenticate_atlassian(org_config)
        return self.snapshot()

    def _authenticate_google(self, cname: str, org_config: dict[str, Any]) -> None:
        client_config = _google_client_config(org_config)
        if not client_config:
            self.error = "Google organization config isn't installed yet."
            return
        from .daemon_main import TOKEN_FILES
        client_cls = _GOOGLE_CLIENTS[cname]
        token_file = str(data_dir() / TOKEN_FILES[cname])
        self._busy_connectors.add(cname)

        def work() -> str:
            client = client_cls(client_config=client_config, token_file=token_file)
            client.authorize_interactive()
            return client.check_connection()

        def done(ok: bool, result: Any) -> None:
            self._busy_connectors.discard(cname)
            if ok:
                self.error = ""
                self.refresh_connectors()
            else:
                self.error = f"{cname.capitalize()} authentication failed: {result}"
                self._push_snapshot()

        _run_async(work, done)

    def _authenticate_slack(self, org_config: dict[str, Any]) -> None:
        slack_org = org_config.get("slack") or {}
        if not slack_org.get("client_id"):
            self.error = "Slack organization config isn't installed yet."
            return
        from .daemon_main import TOKEN_FILES
        token_file = str(data_dir() / TOKEN_FILES["slack"])
        self._busy_connectors.add("slack")

        def work() -> dict[str, Any]:
            return slack_authorize_interactive(
                client_id=slack_org["client_id"],
                client_secret=slack_org.get("client_secret", ""),
                token_file=token_file,
                user_scopes=slack_org.get("user_scopes"),
            )

        def done(ok: bool, result: Any) -> None:
            self._busy_connectors.discard("slack")
            if ok:
                self.error = ""
                self.refresh_connectors()
            else:
                self.error = f"Slack authentication failed: {result}"
                self._push_snapshot()

        _run_async(work, done)

    def _authenticate_salesforce(self, org_config: dict[str, Any]) -> None:
        sf_org = org_config.get("salesforce") or {}
        if not sf_org.get("consumer_key"):
            self.error = "Salesforce organization config isn't installed yet."
            return
        from .daemon_main import TOKEN_FILES
        token_file = str(data_dir() / TOKEN_FILES["salesforce"])
        self._busy_connectors.add("salesforce")

        def work() -> dict[str, Any]:
            return salesforce_authorize_interactive(
                consumer_key=sf_org["consumer_key"],
                consumer_secret=sf_org.get("consumer_secret", ""),
                token_file=token_file,
                login_url=sf_org.get("login_url", "https://login.salesforce.com"),
            )

        def done(ok: bool, result: Any) -> None:
            self._busy_connectors.discard("salesforce")
            if ok:
                self.error = ""
                self.refresh_connectors()
            else:
                self.error = f"Salesforce authentication failed: {result}"
                self._push_snapshot()

        _run_async(work, done)

    def _authenticate_atlassian(self, org_config: dict[str, Any]) -> None:
        atlassian_org = org_config.get("atlassian") or {}
        if not atlassian_org.get("client_id"):
            self.error = "Atlassian organization config isn't installed yet."
            return
        from .daemon_main import TOKEN_FILES
        token_file = str(data_dir() / TOKEN_FILES["atlassian"])
        self._busy_connectors.add("jira")
        self._busy_connectors.add("confluence")

        def pick_resource(resources: list[dict[str, Any]]) -> dict[str, Any]:
            options = [r.get("url", r.get("id", "")) for r in resources]
            choice = _osascript_pick(
                title="PrivacyFence",
                prompt="Choose the Atlassian site to connect:",
                options=options,
            )
            return next((r for r in resources if r.get("url") == choice), resources[0])

        def work() -> dict[str, Any]:
            return atlassian_authorize_interactive(
                client_id=atlassian_org["client_id"],
                client_secret=atlassian_org.get("client_secret", ""),
                token_file=token_file,
                pick_resource=pick_resource,
            )

        def done(ok: bool, result: Any) -> None:
            self._busy_connectors.discard("jira")
            self._busy_connectors.discard("confluence")
            if ok:
                self.error = ""
                self.refresh_connectors()
            else:
                self.error = f"Atlassian authentication failed: {result}"
                self._push_snapshot()

        _run_async(work, done)

    # -- Telegram: bridge-driven multi-step sign-in (phone -> code -> optional
    # 2FA password), replacing the native rumps.Window-based flow the first
    # pass of issue #120 kept as a deliberate scope boundary. Each step opens
    # its own short-lived TelegramClient/connect/disconnect, exactly like the
    # pre-#120 flow did (see git history at 1f367ca, menu_bar.py's
    # _authenticate_telegram) -- no long-lived connection is held across
    # bridge calls, since a webview round trip can be arbitrarily far apart
    # from the next one. self._telegram_auth carries the phone number and
    # phone_code_hash send_code_request returned, needed by the code step;
    # it's None whenever no Telegram sign-in is in progress. The JS side
    # opens its modal locally (see settings_window_html.py) the moment the
    # Telegram connector row's "Authenticate…" is clicked, before any bridge
    # call -- telegram_start_auth() below is only reached once the user
    # actually submits a phone number.

    def _telegram_auth_state(self) -> dict[str, Any]:
        if self._telegram_auth is None:
            return {"step": None, "error": ""}
        return {"step": self._telegram_auth.get("step"), "error": self._telegram_auth.get("error", "")}

    def telegram_start_auth(self, phone: str) -> dict[str, Any]:
        creds = telegram_app_credentials()
        if not creds:
            self.error = "Telegram app credentials are missing from this build."
            return self.snapshot()
        api_id, api_hash = creds
        phone = (phone or "").strip()
        if not phone:
            self._telegram_auth = {"step": "phone", "error": "Enter a phone number."}
            return self.snapshot()
        from .daemon_main import TOKEN_FILES
        session_file = str(data_dir() / TOKEN_FILES["telegram"])
        self._busy_connectors.add("telegram")
        self._telegram_auth = {"step": "phone", "error": ""}

        def work() -> str:
            import asyncio

            from telethon import TelegramClient

            async def _send_code() -> str:
                client = TelegramClient(session_file, api_id, api_hash)
                await client.connect()
                try:
                    result = await client.send_code_request(phone)
                    return result.phone_code_hash
                finally:
                    await client.disconnect()

            return asyncio.run(_send_code())

        def done(ok: bool, result: Any) -> None:
            self._busy_connectors.discard("telegram")
            if ok:
                self._telegram_auth = {"step": "code", "phone": phone, "phone_code_hash": result, "error": ""}
            else:
                self._telegram_auth = {"step": "phone", "error": str(result)}
            self._push_snapshot()

        _run_async(work, done)
        return self.snapshot()

    def telegram_submit_code(self, code: str) -> dict[str, Any]:
        if self._telegram_auth is None or self._telegram_auth.get("step") != "code":
            return self.snapshot()
        code = (code or "").strip()
        if not code:
            self._telegram_auth["error"] = "Enter the verification code."
            return self.snapshot()
        creds = telegram_app_credentials()
        if not creds:
            self.error = "Telegram app credentials are missing from this build."
            return self.snapshot()
        api_id, api_hash = creds
        from .daemon_main import TOKEN_FILES
        session_file = str(data_dir() / TOKEN_FILES["telegram"])
        phone = self._telegram_auth["phone"]
        phone_code_hash = self._telegram_auth["phone_code_hash"]
        self._busy_connectors.add("telegram")

        def work() -> str:
            import asyncio

            from telethon import TelegramClient
            from telethon.errors import SessionPasswordNeededError

            async def _sign_in() -> str:
                client = TelegramClient(session_file, api_id, api_hash)
                await client.connect()
                try:
                    try:
                        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
                    except SessionPasswordNeededError:
                        return "__needs_2fa__"
                    me = await client.get_me()
                    return f"{me.first_name or ''} {me.last_name or ''}".strip()
                finally:
                    await client.disconnect()

            return asyncio.run(_sign_in())

        def done(ok: bool, result: Any) -> None:
            self._busy_connectors.discard("telegram")
            if not ok:
                if self._telegram_auth is not None:
                    self._telegram_auth["error"] = str(result)
                self._push_snapshot()
                return
            if result == "__needs_2fa__":
                if self._telegram_auth is not None:
                    self._telegram_auth["step"] = "password"
                    self._telegram_auth["error"] = ""
                self._push_snapshot()
                return
            self._telegram_auth = None
            self.error = ""
            self.refresh_connectors()

        _run_async(work, done)
        return self.snapshot()

    def telegram_submit_2fa(self, password: str) -> dict[str, Any]:
        if self._telegram_auth is None or self._telegram_auth.get("step") != "password":
            return self.snapshot()
        password = (password or "").strip()
        if not password:
            self._telegram_auth["error"] = "Enter your two-step verification password."
            return self.snapshot()
        creds = telegram_app_credentials()
        if not creds:
            self.error = "Telegram app credentials are missing from this build."
            return self.snapshot()
        api_id, api_hash = creds
        from .daemon_main import TOKEN_FILES
        session_file = str(data_dir() / TOKEN_FILES["telegram"])
        self._busy_connectors.add("telegram")

        def work() -> str:
            import asyncio

            from telethon import TelegramClient

            async def _sign_in_2fa() -> str:
                client = TelegramClient(session_file, api_id, api_hash)
                await client.connect()
                try:
                    await client.sign_in(password=password)
                    me = await client.get_me()
                    return f"{me.first_name or ''} {me.last_name or ''}".strip()
                finally:
                    await client.disconnect()

            return asyncio.run(_sign_in_2fa())

        def done(ok: bool, result: Any) -> None:
            self._busy_connectors.discard("telegram")
            if not ok:
                if self._telegram_auth is not None:
                    self._telegram_auth["error"] = str(result)
                self._push_snapshot()
                return
            self._telegram_auth = None
            self.error = ""
            self.refresh_connectors()

        _run_async(work, done)
        return self.snapshot()

    def telegram_cancel_auth(self) -> dict[str, Any]:
        self._telegram_auth = None
        return self.snapshot()

    # ------------------------------------------------------------------ #
    # Rule actions (Auto-accept Rules page -- plain rule_type/value text
    # rows, per the design; see this module's docstring/the PR report for
    # why this no longer goes through a native picker the way menu_bar.py's
    # pre-#120 _add_rule/_add_rule_value did)
    # ------------------------------------------------------------------ #

    def update_rule_row(self, op_key: str, idx: int, field: str, value: str) -> dict[str, Any]:
        cfg = self._load_config()
        rules = cfg.get("auto_accept_rules", {}).get(op_key, [])
        if idx >= len(rules):
            return self.snapshot()
        rule = dict(rules[idx])
        if field == "rule_type":
            rule["rule"] = (value or "").strip()
        elif field == "value":
            parsed = _parse_rule_value(rule.get("rule", ""), value)
            if parsed is None:
                rule.pop("value", None)
            else:
                rule["value"] = parsed
        rules[idx] = rule
        cfg.setdefault("auto_accept_rules", {})[op_key] = rules
        self._save_and_reload(cfg)
        return self.snapshot()

    def add_rule_row(self, op_key: str) -> dict[str, Any]:
        cfg = self._load_config()
        rules = cfg.setdefault("auto_accept_rules", {}).setdefault(op_key, [])
        rules.append({"rule": ""})
        self._save_and_reload(cfg)
        return self.snapshot()

    def remove_rule_row(self, op_key: str, idx: int) -> dict[str, Any]:
        cfg = self._load_config()
        rules = cfg.get("auto_accept_rules", {}).get(op_key, [])
        if idx >= len(rules):
            return self.snapshot()
        rules.pop(idx)
        if rules:
            cfg["auto_accept_rules"][op_key] = rules
        else:
            cfg.get("auto_accept_rules", {}).pop(op_key, None)
        self._save_and_reload(cfg)
        return self.snapshot()

    # ------------------------------------------------------------------ #
    # Grant actions (Trusted <Resource> rows -- see resource_grants.py)
    # ------------------------------------------------------------------ #

    def toggle_grant_capability(
        self, connector: str, config_key: str, idx: int, cap: str
    ) -> dict[str, Any]:
        rt = grant_resource_type(connector, config_key)
        if rt is None:
            return self.snapshot()
        cfg = self._load_config()
        grants_cfg = cfg.setdefault("auto_accept_grants", {})
        entries = get_grant_entries(grants_cfg, rt)
        if idx >= len(entries):
            return self.snapshot()
        entries[idx][cap] = not entries[idx].get(cap, False)
        set_grant_entries(grants_cfg, rt, entries)
        self._save_and_reload(cfg)
        return self.snapshot()

    def add_grant_row(self, connector: str, config_key: str) -> dict[str, Any]:
        rt = grant_resource_type(connector, config_key)
        if rt is None:
            return self.snapshot()
        cfg = self._load_config()
        grants_cfg = cfg.setdefault("auto_accept_grants", {})
        entries = get_grant_entries(grants_cfg, rt)
        entries.append({rt.id_field: ""})
        set_grant_entries(grants_cfg, rt, entries)
        self._save_and_reload(cfg)
        return self.snapshot()

    def update_grant_row(
        self, connector: str, config_key: str, idx: int, field: str, value: str
    ) -> dict[str, Any]:
        rt = grant_resource_type(connector, config_key)
        if rt is None:
            return self.snapshot()
        cfg = self._load_config()
        grants_cfg = cfg.setdefault("auto_accept_grants", {})
        entries = get_grant_entries(grants_cfg, rt)
        if idx >= len(entries):
            return self.snapshot()

        if field == "id":
            resource_id = _extract_drive_id(value) or (value or "").strip()
            if resource_id and any(
                i != idx and rt.id_of(e) == resource_id for i, e in enumerate(entries)
            ):
                self.error = f"That {rt.singular} ({_short_id(resource_id)}) is already trusted."
                return self.snapshot()
            entries[idx][rt.id_field] = resource_id
            self.error = ""
        elif field == "name":
            entries[idx]["name"] = (value or "").strip()

        set_grant_entries(grants_cfg, rt, entries)
        self._save_and_reload(cfg)

        if field == "id":
            resource_id = rt.id_of(entries[idx])
            client = self._client_for(connector)
            if resource_id and client is not None:
                self._resolve_names_async(rt, [resource_id], client)
        return self.snapshot()

    def remove_grant_row(self, connector: str, config_key: str, idx: int) -> dict[str, Any]:
        rt = grant_resource_type(connector, config_key)
        if rt is None:
            return self.snapshot()
        cfg = self._load_config()
        grants_cfg = cfg.setdefault("auto_accept_grants", {})
        entries = get_grant_entries(grants_cfg, rt)
        if idx >= len(entries):
            return self.snapshot()
        entries.pop(idx)
        set_grant_entries(grants_cfg, rt, entries)
        self._save_and_reload(cfg)
        return self.snapshot()

    def _resolve_names_async(
        self, rt: GrantResourceType, resource_ids: list[str], client: Any | None
    ) -> None:
        """Kick off a background name lookup for any of these IDs with no
        cached name yet, then push a fresh snapshot once done. No-ops (and
        doesn't loop) once every ID has a cached name -- see
        resource_names.py's TTL."""
        if client is None:
            return
        missing = [rid for rid in resource_ids if rid and self._resolver.cached_name(rt, rid) is None]
        if not missing:
            return

        def work() -> bool:
            return any(self._resolver.resolve(rt, resource_id, client) for resource_id in missing)

        def done(ok: bool, resolved_something: Any) -> None:
            if ok and resolved_something:
                self._push_snapshot()

        _run_async(work, done)

    # ------------------------------------------------------------------ #
    # Suggestion-priority actions (Always-allow Suggestion Order -- see
    # auto_accept.SUGGESTION_FAMILIES). Restored per user direction; ported
    # from menu_bar.py's pre-#120 _move_suggestion_priority/
    # _exclude_suggestion_rule/_include_suggestion_rule (see git history at
    # 1f367ca) with no behavior change, just addressed by connector name
    # instead of by family directly (SUGGESTION_FAMILY_BY_CONNECTOR maps
    # one to the other) since that's what the Rules page's UI is keyed on.
    # ------------------------------------------------------------------ #

    def _set_suggestion_priority_and_refresh(self, family: str, order: list[str]) -> None:
        cfg = self._load_config()
        cfg.setdefault("rule_suggestion_priority", {})[family] = order
        self._save_config(cfg)
        set_suggestion_priority(family, order)

    def move_suggestion_priority(self, connector: str, direction: int, rule_name: str) -> dict[str, Any]:
        family = SUGGESTION_FAMILY_BY_CONNECTOR.get(connector)
        if family is None:
            return self.snapshot()
        order = list(suggestion_order(family))
        if rule_name not in order:
            return self.snapshot()
        idx = order.index(rule_name)
        new_idx = idx + direction
        if not (0 <= new_idx < len(order)):
            return self.snapshot()
        order[idx], order[new_idx] = order[new_idx], order[idx]
        self._set_suggestion_priority_and_refresh(family, order)
        return self.snapshot()

    def exclude_suggestion_rule(self, connector: str, rule_name: str) -> dict[str, Any]:
        family = SUGGESTION_FAMILY_BY_CONNECTOR.get(connector)
        if family is None:
            return self.snapshot()
        order = [r for r in suggestion_order(family) if r != rule_name]
        self._set_suggestion_priority_and_refresh(family, order)
        return self.snapshot()

    def include_suggestion_rule(self, connector: str, rule_name: str) -> dict[str, Any]:
        family = SUGGESTION_FAMILY_BY_CONNECTOR.get(connector)
        if family is None:
            return self.snapshot()
        order = list(suggestion_order(family))
        if rule_name in order:
            return self.snapshot()
        order.append(rule_name)
        self._set_suggestion_priority_and_refresh(family, order)
        return self.snapshot()

    # ------------------------------------------------------------------ #
    # Privacy filter (privacy / drive_privacy / slack_privacy / ... groups,
    # plus Calendar's one standalone toggle)
    # ------------------------------------------------------------------ #

    def set_default_policy(self, group: str, policy: str) -> dict[str, Any]:
        if policy not in PRIVACY_POLICIES:
            return self.snapshot()
        cfg = self._load_config()
        cfg.setdefault(group, {})["default_policy"] = policy
        self._save_and_reload_privacy(cfg)
        return self.snapshot()

    def set_category_policy(self, group: str, category: str, policy: str) -> dict[str, Any]:
        if policy not in PRIVACY_POLICIES:
            return self.snapshot()
        cfg = self._load_config()
        categories = cfg.setdefault(group, {}).setdefault("categories", {})
        categories[category] = policy
        self._save_and_reload_privacy(cfg)
        return self.snapshot()

    def toggle_calendar_free_busy(self) -> dict[str, Any]:
        cfg = self._load_config()
        calendar_cfg = cfg.setdefault("calendar", {})
        calendar_cfg["free_busy_full_event_details"] = not calendar_cfg.get(
            "free_busy_full_event_details", True
        )
        self._save_config(cfg)
        self.refresh_connectors()
        return self.snapshot()

    # ------------------------------------------------------------------ #
    # Audit log
    # ------------------------------------------------------------------ #

    def set_log_level(self, level: str) -> dict[str, Any]:
        level = (level or "").upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            return self.snapshot()
        cfg = self._load_config()
        cfg.setdefault("logging", {})["level"] = level
        self._save_config(cfg)
        from .daemon_main import setup_logging
        setup_logging(cfg)
        return self.snapshot()

    def export_audit_log(self) -> dict[str, Any]:
        log_dir = Path(data_dir()) / "logs" / "audit"
        if not log_dir.exists():
            self.error = "No audit log found yet."
            return self.snapshot()

        week = current_week()
        xlsx_path = None
        if (log_dir / f"{week}.jsonl").exists():
            xlsx_path = AuditLogger(str(log_dir)).export_week_to_excel(week)

        subprocess.run(["open", xlsx_path or str(log_dir)], check=False)
        self.error = ""
        return self.snapshot()

    # ------------------------------------------------------------------ #
    # About
    # ------------------------------------------------------------------ #

    def quit_app(self) -> None:
        rumps.quit_application()

    # ------------------------------------------------------------------ #
    # Snapshot
    # ------------------------------------------------------------------ #

    def snapshot(self) -> dict[str, Any]:
        cfg = self._load_config()
        from .daemon_main import load_org_config
        org_config = load_org_config()
        return {
            "error": self.error,
            "general": self._general_state(cfg),
            "connectors": self._connectors_state(cfg, org_config),
            "telegram_auth": self._telegram_auth_state(),
            "rules": self._rules_state(cfg),
            "privacy": self._privacy_state(cfg),
            "audit": self._audit_state(cfg),
            "about": self._about_state(),
        }

    def _general_state(self, cfg: dict[str, Any]) -> dict[str, Any]:
        pii_cfg = cfg.get("pii_detection", {}) or {}
        update_cfg = cfg.get("update_check", {}) or {}

        org_path = org_dir() / "org_config.json"
        org_installed = org_path.exists()
        org_installed_date = ""
        if org_installed:
            try:
                org_installed_date = datetime.fromtimestamp(org_path.stat().st_mtime).strftime("%b %-d, %Y")
            except OSError:
                org_installed_date = ""

        return {
            "pii_enabled": pii_cfg.get("enabled", True),
            "pii_ip": pii_cfg.get("detect_ip_addresses", True),
            "pii_financial": pii_cfg.get("detect_financial_figures", True),
            "update_check_enabled": update_cfg.get("enabled", True),
            "update_check_beta": update_cfg.get("include_beta", False),
            "org_installed": org_installed,
            "org_installed_date": org_installed_date,
            "org_button_label": (
                "Install/Update Organization Config…" if org_installed else "Install Organization Config…"
            ),
            "version": __version__,
        }

    def _connectors_state(self, cfg: dict[str, Any], org_config: dict[str, Any]) -> list[dict[str, Any]]:
        connectors_cfg: dict[str, dict] = cfg.get("connectors", {}) or {}
        rows = []
        for cname in ALL_CONNECTORS:
            connected = cname in self._connectors
            conn_cfg = connectors_cfg.get(cname, {})
            enabled = conn_cfg.get("enabled", True)
            busy = cname in self._busy_connectors
            if cname == "telegram":
                has_org = telegram_app_credentials() is not None
            else:
                has_org = bool(org_config.get(ORG_CONFIG_SERVICE[cname]))

            rows.append({
                "key": cname,
                "label": cname.capitalize(),
                "icon": cname,
                "authed": connected,
                "enabled": enabled,
                "busy": busy,
                "has_org": has_org,
                "auth_label": "Reconnect…" if connected else "Authenticate…",
            })
        return rows

    def _rules_state(self, cfg: dict[str, Any]) -> dict[str, Any]:
        rules_cfg: dict[str, list[dict]] = cfg.get("auto_accept_rules", {}) or {}
        grants_cfg: dict[str, Any] = cfg.get("auto_accept_grants", {}) or {}
        ops_by_connector: dict[str, list[str]] = {}
        for op_key in OPERATION_LABELS:
            ops_by_connector.setdefault(op_key.split(".", 1)[0], []).append(op_key)

        connectors = []
        sections_by_connector: dict[str, list[dict[str, Any]]] = {}
        grants_by_connector: dict[str, list[dict[str, Any]]] = {}
        suggestion_priority_by_connector: dict[str, dict[str, Any] | None] = {}
        drive_grant_summary_by_connector: dict[str, dict[str, Any] | None] = {}

        for cname in RULES_MENU_GROUPS:
            resource_types = resource_types_for_connector(cname)
            op_keys = ops_by_connector.get(cname, [])
            client = self._client_for(cname)

            drive_grant_summary_by_connector[cname] = (
                self._drive_grant_summary(grants_cfg) if cname in DRIVE_GRANT_SUMMARY_GROUPS else None
            )

            grant_sections = []
            for rt in resource_types:
                entries = get_grant_entries(grants_cfg, rt)
                rows = []
                for entry in entries:
                    resource_id = rt.id_of(entry)
                    name = entry.get("name") or self._resolver.cached_name(rt, resource_id)
                    rows.append({
                        "name": name or "",
                        "id": resource_id,
                        "caps": {cap_key: bool(entry.get(cap_key)) for cap_key in rt.capabilities},
                    })
                grant_sections.append({
                    "config_key": rt.config_key,
                    "title": rt.label,
                    "add_label": f"Add {rt.singular}…",
                    "cap_keys": list(rt.capabilities.keys()),
                    "cap_labels": {k: v.label for k, v in rt.capabilities.items()},
                    "rows": rows,
                })
                self._resolve_names_async(rt, [rt.id_of(e) for e in entries], client)
            grants_by_connector[cname] = grant_sections

            rule_sections = []
            for op_key in op_keys:
                label = OPERATION_LABELS[op_key]
                short_label = label.split(" – ", 1)[1] if " – " in label else label
                op_rules = rules_cfg.get(op_key) or []
                rows = [
                    {"rule_type": r.get("rule", ""), "value": _format_rule_value(r.get("rule", ""), r.get("value"))}
                    for r in op_rules
                    if not r.get("_grant")
                ]
                rule_sections.append({"op_key": op_key, "title": short_label, "rows": rows})
            sections_by_connector[cname] = rule_sections

            family = SUGGESTION_FAMILY_BY_CONNECTOR.get(cname)
            if family is None:
                suggestion_priority_by_connector[cname] = None
            else:
                included = list(suggestion_order(family))
                excluded = [r for r in SUGGESTION_FAMILIES[family] if r not in included]
                suggestion_priority_by_connector[cname] = {
                    "family": family, "included": included, "excluded": excluded,
                }

            count = sum(len(get_grant_entries(grants_cfg, rt)) for rt in resource_types)
            count += sum(len(rules_cfg.get(op_key) or []) for op_key in op_keys)
            connectors.append({"key": cname, "label": cname.capitalize(), "count": count})

        return {
            "connectors": connectors,
            "sections_by_connector": sections_by_connector,
            "grants_by_connector": grants_by_connector,
            "suggestion_priority_by_connector": suggestion_priority_by_connector,
            "drive_grant_summary_by_connector": drive_grant_summary_by_connector,
        }

    def _drive_grant_summary(self, grants_cfg: dict[str, Any]) -> dict[str, Any]:
        """Read-only pointer to Drive's Trusted/Sandbox Folder grants, shown
        at the top of the Sheets and Docs pages (see DRIVE_GRANT_SUMMARY_GROUPS'
        own comment for why those two pages need it). No checkboxes and no
        Remove action here -- the one editable copy of these grants stays on
        the Drive page; this is purely so a reviewer auditing Sheets or Docs
        alone isn't left assuming nothing governs the writes/reads they're
        looking at.
        """
        client = self._client_for("drive")
        rows = []
        for config_key, cap_key, cap_label in (
            ("folders", "read", "Trusted Folders — read auto-accept"),
            ("sandbox_folders", "write", "Sandbox Folders — write auto-accept"),
        ):
            rt = grant_resource_type("drive", config_key)
            entries = [e for e in get_grant_entries(grants_cfg, rt) if e.get(cap_key)]
            names = (
                ", ".join(self._grant_entry_label(rt, e, client) for e in entries)
                if entries else "(none configured)"
            )
            rows.append({"label": cap_label, "value": names})
        return {"title": "Governed by Drive", "rows": rows, "link_label": "Manage in Drive →"}

    def _grant_entry_label(
        self, rt: GrantResourceType, entry: dict[str, Any], client: Any | None
    ) -> str:
        """Display label for one grant entry: a hand-set/cached name, else a
        shortened id, plus a "still resolving"/"connect X" hint while no name
        is available yet. Used by the read-only Drive grant summary shown on
        the Sheets/Docs pages (_drive_grant_summary above) -- the main grant
        rows rendered by the webview keep name/id as separate editable fields
        (see the rows.append() above), so they don't go through this."""
        resource_id = rt.id_of(entry)
        name = entry.get("name") or self._resolver.cached_name(rt, resource_id)
        label = name or _short_id(resource_id)
        if entry.get("tab"):
            label += f" — {entry['tab']}"
        if name is None:
            label += (
                "  (resolving…)" if client is not None
                else f"  (connect {rt.connector.capitalize()} to see its name)"
            )
        return label

    def _privacy_state(self, cfg: dict[str, Any]) -> dict[str, Any]:
        groups = [{"key": g, "label": label} for g, label in PRIVACY_GROUP_LABELS.items()]
        groups.append({"key": "calendar", "label": "Calendar"})

        default_policy: dict[str, str] = {}
        categories: dict[str, list[dict[str, Any]]] = {}
        for group in PRIVACY_GROUP_LABELS:
            parsed = _parse_privacy_group(cfg.get(group))
            default_policy[group] = parsed["default_policy"]
            cat_list = []
            for cat_key, cat_label in PRIVACY_CATEGORY_LABELS.get(group, {}).items():
                policy = parsed["categories"].get(cat_key, parsed["default_policy"])
                cat_list.append({"key": cat_key, "label": cat_label, "policy": policy})
            categories[group] = cat_list

        calendar_cfg = cfg.get("calendar", {}) or {}
        return {
            "groups": groups,
            "default_policy": default_policy,
            "categories": categories,
            "calendar_free_busy": bool(calendar_cfg.get("free_busy_full_event_details", True)),
        }

    def _audit_state(self, cfg: dict[str, Any]) -> dict[str, Any]:
        log_cfg = cfg.get("logging", {}) or {}
        level = str(log_cfg.get("level", "INFO")).upper()
        log_file = log_cfg.get("file", "logs/privacyfence.log")
        week = current_week()
        log_dir = Path(data_dir()) / "logs" / "audit"

        recent: list[dict[str, Any]] = []
        if log_dir.exists():
            for entry in AuditLogger(str(log_dir)).recent_entries(20):
                recent.append({
                    "connector": entry.connector.capitalize() if entry.connector else "",
                    "tool": entry.tool_name or entry.tool,
                    "decision": entry.decision,
                    "time": _relative_time(entry.timestamp),
                })

        return {
            "log_level": level,
            "log_file": log_file,
            "export_hint": f"logs/audit/{week}.jsonl → {week}.xlsx",
            "recent": recent,
        }

    def _about_state(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "license": LICENSE_NAME,
            "repo_url": REPO_URL,
        }


def _osascript_pick(title: str, prompt: str, options: list[str], default: str | None = None) -> str | None:
    """Show a native macOS list-picker and return the chosen item or None.
    Kept only for _authenticate_atlassian's multi-resource picker (a real
    OAuth response can list more than one accessible Atlassian site, with no
    webview UI for that ambiguity in this pass) -- see
    _authenticate_atlassian's pick_resource above."""
    opts_as = "{" + ", ".join(f'"{o}"' for o in options) + "}"
    default_clause = f' default items {{"{default}"}}' if default in options else ""
    script = (
        f'set opts to {opts_as}\n'
        f'set chosen to (choose from list opts '
        f'with title "{title}" '
        f'with prompt "{prompt}"'
        f'{default_clause})\n'
        f'if chosen is false then return ""\n'
        f'return item 1 of chosen'
    )
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    text = result.stdout.strip()
    return text if text else None
