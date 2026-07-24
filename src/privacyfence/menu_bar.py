"""macOS menu bar app (rumps).

Main thread only, except where noted. Provides:
  - Auto-accept rule management: "Trusted <Resource>" grants (folders, task
    lists, channels, ...) — see resource_grants.py for the connector/
    resource-scoped model this compiles from — plus the lower-level
    per-operation "Filters" menus for attribute rules that aren't grants
  - Organization config bundle install/update (the IT-admin-facing side of
    connector setup — see the module docstring in daemon_main.py)
  - Per-connector Authenticate…: runs each service's browser OAuth flow (or,
    for Telegram, the phone+code(+2FA) flow) directly, no Terminal window
  - PII Detection Gate: on/off toggle for the extra confirmation gate in
    pii_detector.py, persisted to settings.yaml and hot-reloaded live
  - Export Audit Log / About panel

Long-running auth flows (anything that waits on a browser) run on a
background thread; results are marshaled back to the main thread via
``PyObjCTools.AppHelper.callAfter`` before touching any rumps/AppKit object
(NSAlert, NSWindow, the menu itself), since AppKit is not thread-safe.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import threading
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import objc
import rumps
import yaml
from AppKit import (
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSMutableAttributedString,
)
from Foundation import NSObject
from PyObjCTools import AppHelper

from . import __version__
from .audit_log import AuditLogger, current_week
from .auto_accept import reload_rules, set_rules_changed_listener
from .paths import data_dir, org_dir
from .pii_detector import set_pii_category_enabled, set_pii_detection_enabled
from .privacy_filter import _parse_group as _parse_privacy_group
from .privacy_filter import _VALID_POLICIES as PRIVACY_POLICIES
from .privacy_filter import init_privacy_filter
from .app_credentials import telegram_app_credentials
from .daemon_main import TOKEN_FILES, build_connectors, load_org_config
from .atlassian_oauth import authorize_interactive as atlassian_authorize_interactive
from .calendar_client import CalendarClient
from .contacts_client import ContactsClient
from .drive_client import DriveClient
from .gmail_client import GmailClient
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
from .rules_manager_window import RulesManagerWindowController, Row, Section
from .salesforce_client import authorize_interactive as salesforce_authorize_interactive
from .slack_client import authorize_interactive as slack_authorize_interactive
from .tasks_client import TasksClient

if TYPE_CHECKING:
    from .ipc_server import IPCServer

logger = logging.getLogger(__name__)

REPO_URL = "https://github.com/andras-tkcs/privacyfence"
LICENSE_NAME = "Apache-2.0"

# ---------------------------------------------------------------------------- #
# Rule metadata
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
    "gmail.create_draft":           ["to_is_myself", "approved_recipient_domain"],
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
    "drive.comment_file":           ["i_am_owner", "created_this_session"],
    "sheets.read_values":           ["approved_spreadsheet", "i_am_owner", "created_by_me", "approved_folder", "created_this_session", "shared_drive_exclusion"],
    "sheets.write_range":           ["approved_spreadsheet", "i_am_owner", "approved_sandbox_folder", "created_this_session"],
    "sheets.add_sheet":             ["approved_spreadsheet", "i_am_owner", "approved_sandbox_folder", "created_this_session"],
    "sheets.rename_sheet":          ["approved_spreadsheet", "i_am_owner", "approved_sandbox_folder", "created_this_session"],
    "sheets.format_range":          ["approved_spreadsheet", "i_am_owner", "approved_sandbox_folder", "created_this_session"],
    "sheets.insert_dimensions":     ["approved_spreadsheet", "i_am_owner", "approved_sandbox_folder", "created_this_session"],
    "sheets.delete_dimensions":     ["approved_spreadsheet", "i_am_owner", "approved_sandbox_folder", "created_this_session"],
    "docs.edit_content":            ["i_am_owner", "approved_sandbox_folder", "created_this_session"],
    "docs.format_content":          ["i_am_owner", "approved_sandbox_folder", "created_this_session"],
    "slack.read_messages":          ["dm_with_myself", "approved_channel", "approved_channel_all_results", "public_channels_only", "no_file_attachments"],
    "slack.send_message":           ["dm_with_myself", "send_to_myself", "approved_channel", "approved_recipient", "reply_in_existing_thread"],
    "calendar.read_event_details":  ["i_am_organizer", "no_external_attendees", "personal_calendar", "past_event", "time_window_days", "no_conferencing_link", "non_private_event"],
    "calendar.create_modify_event": ["i_am_organizer", "no_external_attendees", "personal_calendar"],
    "calendar.set_visibility":      ["i_am_organizer", "no_external_attendees", "personal_calendar"],
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
# Rules that take a list of "spreadsheet_id" or "spreadsheet_id:tab" pairs
RULES_PAIR_VALUE: set[str] = {"approved_spreadsheet"}

# All connectors PrivacyFence supports, in display order
ALL_CONNECTORS: list[str] = [
    "gmail", "drive", "contacts", "calendar", "tasks",
    "slack", "jira", "confluence", "salesforce", "telegram",
]

# Top-level groups shown in the "Auto-accept Rules" menu specifically --
# distinct from ALL_CONNECTORS because "sheets" and "docs" aren't connectors
# (neither has a separate auth, org-config section, or entry in
# GOOGLE_CONNECTORS/_GOOGLE_CLIENTS/ORG_CONFIG_SERVICE -- both ride on
# Drive's OAuth grant), but their rules live under their own "sheets.*"/
# "docs.*" operation keys (see TOOL_TO_OPERATION in auto_accept.py) rather
# than nested under "drive.*", so _build_rules_menu's connector-prefix
# grouping needs them listed here or the whole bucket is silently dropped
# (never iterated, so never rendered) -- exactly what happened before this
# constant existed.
RULES_MENU_GROUPS: list[str] = [
    "gmail", "drive", "sheets", "docs", "contacts", "calendar", "tasks",
    "slack", "jira", "confluence", "salesforce", "telegram",
]

# Connectors authenticated via a shared Google OAuth client (org bundle's
# "google" section).
GOOGLE_CONNECTORS: set[str] = {"gmail", "drive", "contacts", "calendar", "tasks"}

# Which section of the organization config bundle each connector depends on.
# Jira and Confluence share one Atlassian OAuth grant. Telegram is not part
# of the org bundle — its app credentials are baked into the build (see
# app_credentials.py) and checked separately in _build_connectors_menu.
ORG_CONFIG_SERVICE: dict[str, str] = {
    "gmail": "google", "drive": "google", "contacts": "google",
    "calendar": "google", "tasks": "google",
    "slack": "slack",
    "jira": "atlassian", "confluence": "atlassian",
    "salesforce": "salesforce",
}
ORG_BUNDLE_SERVICES: list[str] = ["google", "slack", "salesforce", "atlassian"]

# Categories individually toggleable from the "PII Detection Gate" submenu,
# on top of that submenu's own master "Enabled" switch. Keys match
# pii_detector._OPTIONAL_CATEGORIES and the settings.yaml field names.
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
    "trusted_sender_domain": "domain1.com\ndomain2.com",
    "label_match":           "INBOX\nUNREAD",
    "age_threshold_days":    "30",
    "send_to_myself":        "U0123456789",
    "approved_channel":      "C0123456789\nC9876543210",
    "approved_channel_all_results": "C0123456789\nC9876543210",
    "approved_recipient":    "U0123456789",
    "personal_calendar":     "primary",
    "time_window_days":      "14",
    "approved_object_types": "Account\nContact\nOpportunity",
    "approved_report_ids":   "00O000000000001",
    "file_type_allowlist":   "application/vnd.google-apps.document\ntext/plain",
    "approved_folder":       "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
    "approved_sandbox_folder": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
    "approved_recipient_domain": "domain1.com\ndomain2.com",
    "label_name_allowlist": "Newsletters\nReceipts",
    "parent_folder_allowlist": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
    "approved_project_keys": "MYPROJ\nOTHERPROJ",
    "approved_space_keys":   "TEAM\nDOCS",
    "approved_chats":        "123456789\n-100987654321",
    "approved_chats_all_results": "123456789\n-100987654321",
    "approved_spreadsheet":  "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms\n1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms:Sheet1",
    "approved_task_list":    "MDAwMDAwMDAwMDAwMDAwMDAwMDA6MDow\nMTExMTExMTExMTExMTExMTExMTE6MDow",
}

# Display metadata for the "Privacy Filter" window -- mirrors the group/
# category schema documented in resources/settings.yaml.example and enforced
# by privacy_filter.py. Deliberately only the 3 connectors that module knows
# about; adding a 4th group means adding it there first.
PRIVACY_GROUP_LABELS: dict[str, str] = {
    "privacy": "Gmail",
    "drive_privacy": "Drive & Sheets",
    "slack_privacy": "Slack",
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
    },
}

# Rule names now configured through a Trusted-resource grant (see
# resource_grants.py) instead of by hand. Hidden from "+ Add rule…" so there
# isn't a second, more tedious way to do the same thing — existing entries
# under these names (hand-authored, or left behind by a partial migration —
# see resource_grants.migrate_rules_to_grants) still display and can still be
# removed, just not created fresh from here.
GRANT_COVERED_RULE_NAMES: set[str] = {
    rule_name
    for rt in GRANT_RESOURCE_TYPES
    for capability in rt.capabilities.values()
    for _op_key, rule_name in capability.targets
}

# Rule names whose value is the same kind of opaque resource ID a grant entry
# stores (a Drive folder ID, a Jira project key, ...), mapped to the resource
# type that knows how to resolve one to a display name -- so a hand-authored/
# partially-migrated rule entry (see GRANT_COVERED_RULE_NAMES above) still
# shows a real name instead of the raw ID, the same way a grant entry does.
# Mostly the grant-covered rule names, plus a few that hold the same kind of
# ID but aren't tied to any grant capability -- parent_folder_allowlist has
# no "auto-accept uploads into this folder" toggle in the grants UI, it's a
# hand-authored-only allowlist, but its values are still plain Drive folder
# IDs worth resolving.
RULE_NAME_TO_RESOURCE_TYPE: dict[str, GrantResourceType] = {
    rule_name: rt
    for rt in GRANT_RESOURCE_TYPES
    for capability in rt.capabilities.values()
    for _op_key, rule_name in capability.targets
}
_drive_folder_rt = grant_resource_type("drive", "folders")
assert _drive_folder_rt is not None
RULE_NAME_TO_RESOURCE_TYPE["parent_folder_allowlist"] = _drive_folder_rt

# Drive/Sheets URLs paste-able into "+ Add folder…" / "+ Add spreadsheet…",
# so the user can copy the browser address bar instead of hand-extracting
# the ID segment. Order matters: a spreadsheet URL also contains "/d/" so
# the folder pattern is tried first.
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


class _AuthFlowCancelled(Exception):
    """Raised internally when the user cancels a native prompt mid-flow."""


def _google_client_config(org_config: dict[str, Any]) -> dict[str, Any]:
    google = org_config.get("google") or {}
    if not google.get("client_id") or not google.get("client_secret"):
        return {}
    return {"installed": google}


class _MenuTrackingDelegate(NSObject):
    """NSMenuDelegate that tracks whether the status-bar dropdown is
    currently open on screen.

    Mutating an NSMenu's items (rumps' self.menu.clear()/self.menu = [...])
    while AppKit is tracking that same menu (i.e. the dropdown is visibly
    open) crashes the process with EXC_BAD_ACCESS deep inside AppKit's
    tracking loop -- it's left holding pointers into NSMenuItem objects
    that just got released. PrivacyFenceMenuBar._rebuild() consults
    ``app._menu_is_open`` (kept up to date by this delegate) to defer such
    rebuilds until the dropdown closes instead of mutating it live.
    """

    def init(self):
        self = objc.super(_MenuTrackingDelegate, self).init()
        if self is None:
            return None
        self.app = None
        return self

    def menuWillOpen_(self, _menu: Any) -> None:
        self.app._menu_is_open = True

    def menuDidClose_(self, _menu: Any) -> None:
        self.app._menu_is_open = False
        if self.app._rebuild_pending:
            self.app._rebuild_pending = False
            self.app._rebuild()


# ---------------------------------------------------------------------------- #
# App
# ---------------------------------------------------------------------------- #

class PrivacyFenceMenuBar(rumps.App):
    def __init__(
        self,
        config_path: str,
        connectors: list[str],
        ipc_server: "IPCServer",
        connector_objs: list[Any] | None = None,
    ) -> None:
        self._config_path = config_path
        self._connectors = connectors
        self._ipc_server = ipc_server
        # name -> live Connector wrapper (exposes .client for resolving grant
        # resource names — see resource_names.py). Populated at startup from
        # daemon_main.py's already-built connectors, and refreshed whenever
        # _refresh_connectors() re-authenticates/toggles one.
        self._connector_objs: dict[str, Any] = {c.name: c for c in (connector_objs or [])}
        self._resolver = get_resolver()
        # Lazily created on first "Manage Auto-accept Rules…" click (see
        # _open_rules_manager) -- one long-lived window reused for the app's
        # whole lifetime, unlike the modal one-shot approval windows.
        self._rules_manager: RulesManagerWindowController | None = None
        # Lazily created on first "Privacy Filter…" click (see
        # _open_privacy_filter_manager) -- same lazy/long-lived pattern as
        # _rules_manager above, a separate instance of the same generic
        # window class (see rules_manager_window.py's window_title param).
        self._privacy_manager: RulesManagerWindowController | None = None
        icon_path = _find_icon()
        super().__init__(
            name="PrivacyFence",
            icon=icon_path,
            quit_button=None,
            template=True,
        )
        self._menu_is_open = False
        self._rebuild_pending = False
        self._menu_tracking_delegate = _MenuTrackingDelegate.alloc().init()
        self._menu_tracking_delegate.app = self
        self.menu._menu.setDelegate_(self._menu_tracking_delegate)
        self._rebuild()
        set_rules_changed_listener(self._on_rules_changed)
        self._ipc_server.set_unattended_changed_listener(self._on_unattended_changed)

    def _on_rules_changed(self) -> None:
        """Fired by auto_accept.reload_rules(), possibly from the IPC
        server's thread — marshal the menu rebuild onto the main thread."""
        AppHelper.callAfter(self._rebuild)

    def _client_for(self, connector: str) -> Any | None:
        """Live client for a connected connector (for resolving/listing grant
        resources), or None if that connector isn't currently connected."""
        conn = self._connector_objs.get(connector)
        return getattr(conn, "client", None) if conn is not None else None

    def _on_unattended_changed(self) -> None:
        """Fired by ipc_server.py's IPCServer, on its own asyncio thread,
        whenever a connection starts or stops an unattended session --
        marshal the live-indicator refresh onto the main thread, same
        pattern as _on_rules_changed."""
        AppHelper.callAfter(self._rebuild)

    def _status_label(self) -> str:
        count = self._ipc_server.unattended_session_count()
        if not count:
            return "PrivacyFence is running"
        plural = "s" if count != 1 else ""
        return f"PrivacyFence is running — {count} unattended session{plural} active"

    # ------------------------------------------------------------------ #
    # Menu building
    # ------------------------------------------------------------------ #

    def _rebuild(self) -> None:
        # Rebuilding mutates the live NSMenu the status-bar item shares with
        # AppKit's tracking loop -- if the dropdown is currently open,
        # mutating it now crashes the process (see _MenuTrackingDelegate).
        # Defer instead; menuDidClose_ replays the rebuild once it's safe.
        if self._menu_is_open:
            self._rebuild_pending = True
            return
        cfg = self._load_config()
        org_config = load_org_config()
        connectors_cfg: dict[str, dict] = cfg.get("connectors", {}) or {}
        pii_cfg: dict[str, Any] = cfg.get("pii_detection", {}) or {}
        pii_enabled: bool = pii_cfg.get("enabled", True)

        org_item = self._build_org_menu(org_config)
        connectors_parent = self._build_connectors_menu(org_config, connectors_cfg)
        rules_item = rumps.MenuItem("Manage Auto-accept Rules…", callback=self._open_rules_manager)
        privacy_item = rumps.MenuItem("Privacy Filter…", callback=self._open_privacy_filter_manager)

        pii_item = self._build_pii_menu(pii_cfg, pii_enabled)

        self.menu.clear()
        self.menu = [
            rumps.MenuItem(self._status_label()),
            rumps.separator,
            pii_item,
            rumps.separator,
            connectors_parent,
            rules_item,
            privacy_item,
            org_item,
            rumps.separator,
            rumps.MenuItem("Export Audit Log…", callback=self.export_audit_log),
            rumps.MenuItem("About PrivacyFence", callback=self.show_about),
            rumps.separator,
            rumps.MenuItem("Quit PrivacyFence", callback=self.quit_app),
        ]
        # The rules-manager window (if open) shows the same connector/rule
        # state as the menu -- refresh it on every path that gets here,
        # rather than duplicating this call at each of _rebuild()'s many
        # callers (see _save_and_reload, _refresh_connectors, auth-flow
        # done() callbacks, ...).
        if self._rules_manager is not None:
            self._rules_manager._refresh_window()
        if self._privacy_manager is not None:
            self._privacy_manager._refresh_window()

    def _open_rules_manager(self, _sender: Any = None) -> None:
        if self._rules_manager is None:
            self._rules_manager = RulesManagerWindowController.alloc().init()
            self._rules_manager._configure_window(self._list_rule_connectors, self._gather_connector_sections)
        self._rules_manager._show_window()

    def _open_privacy_filter_manager(self, _sender: Any = None) -> None:
        if self._privacy_manager is None:
            self._privacy_manager = RulesManagerWindowController.alloc().init()
            self._privacy_manager._configure_window(
                self._list_privacy_groups, self._gather_privacy_sections, window_title="Privacy Filter"
            )
        self._privacy_manager._show_window()

    def _list_privacy_groups(self) -> list[tuple[str, str, int]]:
        cfg = self._load_config()
        result: list[tuple[str, str, int]] = []
        for group, label in PRIVACY_GROUP_LABELS.items():
            parsed = _parse_privacy_group(cfg.get(group))
            result.append((group, label, len(parsed["categories"])))
        return result

    def _gather_privacy_sections(self, group: str) -> list[Section]:
        cfg = self._load_config()
        parsed = _parse_privacy_group(cfg.get(group))
        default_policy = parsed["default_policy"]
        categories = parsed["categories"]

        rows = [Row(
            f"Default: {default_policy}", False,
            [("Change…", partial(self._change_privacy_default, group))],
        )]
        for category, label in PRIVACY_CATEGORY_LABELS.get(group, {}).items():
            policy = categories.get(category, default_policy)
            suffix = "" if category in categories else "  (default)"
            rows.append(Row(
                f"{label}: {policy}{suffix}", True,
                [("Change…", partial(self._change_privacy_category, group, category))],
            ))
        return [Section("", rows)]

    def _list_rule_connectors(self) -> list[tuple[str, str, int]]:
        cfg = self._load_config()
        rules_cfg: dict[str, list[dict]] = cfg.get("auto_accept_rules", {}) or {}
        grants_cfg: dict[str, Any] = cfg.get("auto_accept_grants", {}) or {}
        ops_by_connector: dict[str, list[str]] = {}
        for op_key in OPERATION_LABELS:
            ops_by_connector.setdefault(op_key.split(".", 1)[0], []).append(op_key)

        result: list[tuple[str, str, int]] = []
        for cname in RULES_MENU_GROUPS:
            count = sum(len(get_grant_entries(grants_cfg, rt)) for rt in resource_types_for_connector(cname))
            count += sum(len(rules_cfg.get(op_key) or []) for op_key in ops_by_connector.get(cname, []))
            result.append((cname, cname.capitalize(), count))
        return result

    def _gather_connector_sections(self, cname: str) -> list[Section]:
        """Data for the rules-manager window's main pane -- the same
        connector/grant/rule iteration the old cascading "Auto-accept Rules"
        NSMenu used to do, just producing Section/Row data instead of
        rumps.MenuItem objects. Every
        row action below is one of this class's own existing mutation
        methods (see "Rule actions"/"Grant actions"), unchanged -- only how
        they're triggered (a window row's link button, not a menu click)
        and how the result gets back on screen (see _rebuild's
        _refresh_window() call) is new."""
        cfg = self._load_config()
        rules_cfg: dict[str, list[dict]] = cfg.get("auto_accept_rules", {}) or {}
        grants_cfg: dict[str, Any] = cfg.get("auto_accept_grants", {}) or {}
        ops_by_connector: dict[str, list[str]] = {}
        for op_key in OPERATION_LABELS:
            ops_by_connector.setdefault(op_key.split(".", 1)[0], []).append(op_key)

        resource_types = resource_types_for_connector(cname)
        op_keys = ops_by_connector.get(cname, [])
        client = self._client_for(cname)
        sections: list[Section] = []

        for rt in resource_types:
            entries = get_grant_entries(grants_cfg, rt)
            rows: list[Row] = []
            for idx, entry in enumerate(entries):
                resource_id = rt.id_of(entry)
                name = entry.get("name") or self._resolver.cached_name(rt, resource_id)
                label = name or _short_id(resource_id)
                if entry.get("tab"):
                    label += f" — {entry['tab']}"
                if name is None:
                    label += (
                        "  (resolving…)" if client is not None
                        else f"  (connect {cname.capitalize()} to see its name)"
                    )
                actions: list[tuple[str, Any]] = [
                    (
                        f"{'☑' if entry.get(cap_key) else '☐'} {capability.label}",
                        partial(self._toggle_grant_capability, cname, rt.config_key, idx),
                    )
                    for cap_key, capability in rt.capabilities.items()
                ]
                actions.append(("Copy ID", partial(self._copy_to_clipboard, resource_id)))
                actions.append(("✕ Remove", partial(self._remove_grant, cname, rt.config_key, idx)))
                rows.append(Row(label, False, actions))
            sections.append(Section(rt.label, rows, f"+ Add {rt.singular}…", partial(self._add_grant, cname, rt.config_key)))
            self._resolve_names_async(rt, [rt.id_of(e) for e in entries], client)

        for op_key in op_keys:
            label = OPERATION_LABELS[op_key]
            short_label = label.split(" – ", 1)[1] if " – " in label else label
            op_rules = rules_cfg.get(op_key) or []
            rows = []
            for idx, rule_cfg in enumerate(op_rules):
                rows.extend(self._rule_rows_for(op_key, idx, rule_cfg, client))
            sections.append(Section(short_label, rows, "+ Add rule…", partial(self._add_rule, op_key)))

        if not resource_types and not op_keys:
            sections.append(Section("", [Row("All operations always auto-approved — no rules needed", False, [])]))

        return sections

    def _rule_rows_for(self, op_key: str, idx: int, rule_cfg: dict[str, Any], client: Any | None) -> list[Row]:
        rule_name = rule_cfg.get("rule", "")
        value = rule_cfg.get("value")

        if rule_cfg.get("_grant"):
            # Compiled from a Trusted-resource grant, not a hand-authored
            # rule — nothing to edit here, only where it actually lives.
            return [Row(f"{rule_name}  (via grant above)", False, [])]

        if rule_name in RULES_LIST_VALUE or rule_name in RULES_PAIR_VALUE:
            is_pair = rule_name in RULES_PAIR_VALUE
            # A hand-authored/partially-migrated rule under one of the
            # resource-scoped rule names (see GRANT_COVERED_RULE_NAMES) holds
            # the same kind of opaque resource ID a grant entry does -- show
            # its resolved name instead of the raw ID, the same way the grant
            # section above does (see resource_names.py).
            rt = RULE_NAME_TO_RESOURCE_TYPE.get(rule_name)
            rows = [Row(rule_name, False, [("+ Add value…", partial(self._add_rule_value, op_key, idx))])]
            values = value if isinstance(value, list) else ([value] if value else [])
            if rt is not None:
                ids = [v.get("spreadsheet_id", "") if is_pair else str(v) for v in values]
                self._resolve_names_async(rt, ids, client)
            for v_idx, v in enumerate(values):
                if rt is not None:
                    resource_id = v.get("spreadsheet_id", "") if is_pair else str(v)
                    name = self._resolver.cached_name(rt, resource_id)
                    v_label = name or _short_id(resource_id)
                    if is_pair and v.get("tab"):
                        v_label += f" — {v['tab']}"
                    if name is None:
                        v_label += (
                            "  (resolving…)" if client is not None
                            else f"  (connect {rt.connector.capitalize()} to see its name)"
                        )
                else:
                    v_label = _format_pair_line(v) if is_pair else str(v)
                rows.append(Row(v_label, True, [("✕ Remove", partial(self._remove_rule_value, op_key, idx, v_idx))]))
            return rows

        if rule_name in RULES_INT_VALUE:
            return [Row(f"{rule_name}: {value}", False, [
                ("Edit…", partial(self._edit_rule_value, op_key, idx)),
                ("✕ Remove", partial(self._remove_rule, op_key, idx)),
            ])]

        # Boolean rule (no value) — one action removes it; there's nothing
        # else to configure, so there's no separate toggle-vs-remove split.
        return [Row(f"✓ {rule_name}", False, [("✕ Remove", partial(self._remove_rule, op_key, idx))])]

    def _resolve_names_async(
        self, rt: GrantResourceType, resource_ids: list[str], client: Any | None
    ) -> None:
        """Kick off a background name lookup for any of these IDs with no
        cached name yet (a grant entry's ID, or a hand-authored rule value's
        -- see _rule_rows_for), then rebuild the menu once done. No-ops (and
        doesn't loop) once every ID has a cached name — see
        resource_names.py's TTL."""
        if client is None:
            return
        missing = [rid for rid in resource_ids if rid and self._resolver.cached_name(rt, rid) is None]
        if not missing:
            return

        def work() -> bool:
            # Only report back "something changed" if resolution actually
            # found a name for at least one entry — an entry that keeps
            # failing to resolve (deleted resource, transient error, ...)
            # would otherwise still be "missing" on the next rebuild, and
            # unconditionally rebuilding here would re-trigger this same
            # lookup forever.
            return any(self._resolver.resolve(rt, resource_id, client) for resource_id in missing)

        def done(ok: bool, resolved_something: Any) -> None:
            if ok and resolved_something:
                self._rebuild()

        self._run_async(work, done)

    def _build_org_menu(self, org_config: dict[str, Any]) -> rumps.MenuItem:
        """Single top-level item, not a submenu -- the old version held only
        two static status lines plus one action, exactly the kind of shallow
        "menu wearing a data-browser's clothes" the menu bar redesign review
        flagged (see that review's item 1, generalized). Clicking shows
        status first (if any config is installed) before handing off to the
        unchanged install/update flow."""
        label = "Organization Config…" if org_config else "Install Organization Config…"
        item = rumps.MenuItem(label)
        item.set_callback(self._open_org_config)
        return item

    def _open_org_config(self, _sender: Any = None) -> None:
        org_config = load_org_config()
        if org_config:
            org_name = org_config.get("org_name", "")
            installed = [s for s in ORG_BUNDLE_SERVICES if org_config.get(s)]
            header = f"Installed: {org_name}" if org_name else "Installed"
            resp = rumps.alert(
                title="Organization Config",
                message=f"{header}\nServices: {', '.join(installed) or 'none'}",
                ok="Update…",
                cancel="Close",
            )
            if resp != 1:
                return
        self._install_org_config()

    def _build_connectors_menu(
        self, org_config: dict[str, Any], connectors_cfg: dict[str, dict]
    ) -> rumps.MenuItem:
        connectors_parent = rumps.MenuItem("Connectors")
        for cname in ALL_CONNECTORS:
            connected = cname in self._connectors
            conn_cfg = connectors_cfg.get(cname, {})
            enabled = conn_cfg.get("enabled", True)
            if cname == "telegram":
                has_org = telegram_app_credentials() is not None
            else:
                has_org = bool(org_config.get(ORG_CONFIG_SERVICE[cname]))

            if connected:
                status, status_color = "●", NSColor.systemGreenColor()  # connected
            elif not enabled:
                status, status_color = "✕", NSColor.secondaryLabelColor()  # disabled
            elif not has_org:
                status, status_color = "○", NSColor.systemRedColor()  # org config / app credentials missing
            else:
                status, status_color = "◐", NSColor.systemOrangeColor()  # org config present, needs auth

            conn_item = rumps.MenuItem(f"{status} {cname.capitalize()}")
            _colorize_status_glyph(conn_item, status, status_color)

            toggle_label = "  Disable" if enabled else "  Enable"
            toggle = rumps.MenuItem(toggle_label)
            toggle.set_callback(_bind(self._toggle_connector, cname))
            conn_item.add(toggle)

            conn_item.add(rumps.separator)

            if not has_org:
                msg = (
                    "  App credentials missing from this build"
                    if cname == "telegram"
                    else "  Organization config missing — install it above"
                )
                conn_item.add(rumps.MenuItem(msg))
            else:
                auth_label = "  Reconnect…" if connected else "  Authenticate…"
                authenticate = rumps.MenuItem(auth_label)
                authenticate.set_callback(_bind(self._authenticate, cname))
                conn_item.add(authenticate)

            connectors_parent.add(conn_item)
        return connectors_parent

    # ------------------------------------------------------------------ #
    # Rule actions
    # ------------------------------------------------------------------ #

    def _add_rule(self, op_key: str, _sender: Any = None) -> None:
        available = [r for r in RULES_BY_OPERATION.get(op_key, []) if r not in GRANT_COVERED_RULE_NAMES]
        if not available:
            rumps.alert(
                "Add Rule",
                f"No configurable filter rules for\n{OPERATION_LABELS.get(op_key, op_key)}.\n\n"
                "Resource-scoped trust (folders, task lists, channels, spaces, …) is configured "
                "under this connector's Trusted-resource menus above, not here.",
            )
            return

        # _osascript_pick blocks on a subprocess without pumping the run
        # loop -- calling it straight from this button's action handler
        # segfaulted AppKit (an in-flight window-activation animation lost
        # its object while the main thread sat blocked in subprocess.run,
        # see git history). Off the main thread, like every other
        # subprocess-backed picker in this file (e.g. _authenticate_atlassian's
        # pick_resource).
        def work() -> str | None:
            return _osascript_pick(
                title="Add Auto-accept Rule",
                prompt=f"Select a rule to add to:\n{OPERATION_LABELS.get(op_key, op_key)}",
                options=available,
            )

        def done(ok: bool, result: Any) -> None:
            if ok and result:
                self._finish_add_rule(op_key, result)

        self._run_async(work, done)

    def _finish_add_rule(self, op_key: str, rule_name: str) -> None:
        new_rule: dict[str, Any] = {"rule": rule_name}

        if rule_name in RULES_LIST_VALUE or rule_name in RULES_PAIR_VALUE:
            # Start empty — populated one value at a time via "+ Add value…"
            # on the new row, not a shared multi-line box.
            new_rule["value"] = []
        elif rule_name in RULES_INT_VALUE:
            hint = RULE_HINTS.get(rule_name, "")
            message = "Enter an integer value:"
            if hint:
                message += f"\nExample: {hint}"
            w = rumps.Window(
                title=f"Configure: {rule_name}",
                message=message,
                default_text="",
                ok="Add", cancel="Cancel",
                dimensions=(320, 80),
            )
            resp = w.run()
            if not resp.clicked or not resp.text.strip():
                return
            try:
                new_rule["value"] = int(resp.text.strip())
            except ValueError:
                rumps.alert("Invalid value", f"Expected an integer, got: {resp.text.strip()!r}")
                return

        cfg = self._load_config()
        op_rules = cfg.setdefault("auto_accept_rules", {}).setdefault(op_key, [])
        op_rules.append(new_rule)
        self._save_and_reload(cfg)

    def _add_rule_value(self, op_key: str, idx: int, _sender: Any = None) -> None:
        cfg = self._load_config()
        rules = cfg.get("auto_accept_rules", {}).get(op_key, [])
        if idx >= len(rules):
            return
        rule = rules[idx]
        rule_name = rule.get("rule", "")
        is_pair = rule_name in RULES_PAIR_VALUE
        hint = (RULE_HINTS.get(rule_name, "") or "").splitlines()[0] if RULE_HINTS.get(rule_name) else ""

        # Hint goes in the message, not pre-filled into the editable field --
        # a pre-filled example reads as garbage data the user has to delete
        # before typing their real value (see TestAddRule's regression test
        # for the same fix on _add_rule's int-value prompt).
        message = "Enter one 'spreadsheet_id' or 'spreadsheet_id:tab':" if is_pair else "Enter a value:"
        if hint:
            message += f"\nExample: {hint}"
        w = rumps.Window(
            title=f"Add value: {rule_name}",
            message=message,
            default_text="",
            ok="Add", cancel="Cancel",
            dimensions=(320, 80),
        )
        resp = w.run()
        if not resp.clicked or not resp.text.strip():
            return
        text = resp.text.strip()

        values = rule.get("value")
        values = values if isinstance(values, list) else ([values] if values else [])
        if is_pair:
            for entry in _parse_pair_lines(text):
                if entry not in values:
                    values.append(entry)
        elif text not in values:
            values.append(text)
        rule["value"] = values
        cfg["auto_accept_rules"][op_key][idx] = rule
        self._save_and_reload(cfg)

    def _remove_rule_value(self, op_key: str, idx: int, value_idx: int, _sender: Any = None) -> None:
        cfg = self._load_config()
        rules = cfg.get("auto_accept_rules", {}).get(op_key, [])
        if idx >= len(rules):
            return
        rule = rules[idx]
        values = rule.get("value")
        if not isinstance(values, list) or value_idx >= len(values):
            return
        values.pop(value_idx)
        if values:
            rule["value"] = values
            cfg["auto_accept_rules"][op_key][idx] = rule
        else:
            rules.pop(idx)
            if rules:
                cfg["auto_accept_rules"][op_key] = rules
            else:
                cfg.get("auto_accept_rules", {}).pop(op_key, None)
        self._save_and_reload(cfg)

    def _edit_rule_value(self, op_key: str, idx: int, _sender: Any = None) -> None:
        """Only reachable for RULES_INT_VALUE rows — list/pair values are
        edited one at a time via _add_rule_value/_remove_rule_value."""
        cfg = self._load_config()
        rules = cfg.get("auto_accept_rules", {}).get(op_key, [])
        if idx >= len(rules):
            return
        rule = rules[idx]
        rule_name = rule.get("rule", "")
        current = rule.get("value")

        w = rumps.Window(
            title=f"Edit: {rule_name}",
            message="Enter an integer value:",
            default_text=str(current) if current is not None else "",
            ok="Save", cancel="Cancel",
            dimensions=(280, 24),
        )
        resp = w.run()
        if not resp.clicked or not resp.text.strip():
            return
        try:
            rule["value"] = int(resp.text.strip())
        except ValueError:
            rumps.alert("Invalid value", f"Expected an integer, got: {resp.text.strip()!r}")
            return

        cfg["auto_accept_rules"][op_key][idx] = rule
        self._save_and_reload(cfg)

    def _remove_rule(self, op_key: str, idx: int, _sender: Any = None) -> None:
        cfg = self._load_config()
        rules = cfg.get("auto_accept_rules", {}).get(op_key, [])
        if idx >= len(rules):
            return
        rule_name = rules[idx].get("rule", "")
        resp = rumps.alert(
            title="Remove Rule",
            message=f"Remove rule '{rule_name}' from\n{OPERATION_LABELS.get(op_key, op_key)}?",
            ok="Remove",
            cancel="Cancel",
        )
        if resp != 1:
            return
        rules.pop(idx)
        if not rules:
            cfg.get("auto_accept_rules", {}).pop(op_key, None)
        else:
            cfg["auto_accept_rules"][op_key] = rules
        self._save_and_reload(cfg)

    # ------------------------------------------------------------------ #
    # Grant actions (Trusted <Resource> menus — see resource_grants.py)
    # ------------------------------------------------------------------ #

    def _toggle_grant_capability(
        self, connector: str, config_key: str, idx: int, capability_key: str, _sender: Any = None
    ) -> None:
        rt = grant_resource_type(connector, config_key)
        if rt is None:
            return
        cfg = self._load_config()
        grants_cfg = cfg.setdefault("auto_accept_grants", {})
        entries = get_grant_entries(grants_cfg, rt)
        if idx >= len(entries):
            return
        entries[idx][capability_key] = not entries[idx].get(capability_key, False)
        set_grant_entries(grants_cfg, rt, entries)
        self._save_and_reload(cfg)

    def _remove_grant(self, connector: str, config_key: str, idx: int, _sender: Any = None) -> None:
        rt = grant_resource_type(connector, config_key)
        if rt is None:
            return
        cfg = self._load_config()
        grants_cfg = cfg.setdefault("auto_accept_grants", {})
        entries = get_grant_entries(grants_cfg, rt)
        if idx >= len(entries):
            return
        entry = entries[idx]
        label = entry.get("name") or rt.id_of(entry)
        resp = rumps.alert(
            title=f"Remove {rt.singular}",
            message=f"Remove '{label}' from {rt.label}?",
            ok="Remove",
            cancel="Cancel",
        )
        if resp != 1:
            return
        entries.pop(idx)
        set_grant_entries(grants_cfg, rt, entries)
        self._save_and_reload(cfg)

    def _copy_to_clipboard(self, text: str, _sender: Any = None) -> None:
        try:
            subprocess.run(["pbcopy"], input=text, text=True, check=False)
        except OSError:
            pass

    def _add_grant(self, connector: str, config_key: str, _sender: Any = None) -> None:
        rt = grant_resource_type(connector, config_key)
        if rt is None:
            return
        client = self._client_for(connector)

        if rt.list_candidates is not None:
            if client is None:
                rumps.alert(
                    "PrivacyFence",
                    f"{connector.capitalize()} isn't connected — authenticate it from Connectors first.",
                )
                return

            def work() -> list[tuple[str, str]]:
                return rt.list_candidates(client)  # type: ignore[misc]

            def done(ok: bool, result: Any) -> None:
                if not ok:
                    rumps.alert("PrivacyFence", f"Could not list {rt.label.lower()}:\n{result}")
                    return
                self._on_candidates_listed(rt, result)

            self._run_async(work, done)
            return

        # No cheap enumeration for this resource type (e.g. Drive folders/
        # spreadsheets) — accept a pasted ID or full Drive/Sheets URL instead.
        w = rumps.Window(
            title=f"Add {rt.singular}",
            message=f"Paste the {rt.singular} ID, or its full Drive/Sheets URL:",
            ok="Next", cancel="Cancel",
            dimensions=(320, 24),
        )
        resp = w.run()
        if not resp.clicked or not resp.text.strip():
            return
        resource_id = _extract_drive_id(resp.text.strip())
        if not resource_id:
            rumps.alert("PrivacyFence", "Could not find an ID in that text.")
            return

        tab = ""
        if rt.config_key == "spreadsheets":
            tab_resp = rumps.Window(
                title="Add spreadsheet",
                message="Tab name to restrict to (optional — leave blank for the whole spreadsheet):",
                ok="Continue", cancel="Continue",
                dimensions=(320, 24),
            ).run()
            tab = tab_resp.text.strip()

        if client is None:
            self._confirm_and_save_grant(rt, resource_id, None, tab)
            return

        def work() -> str | None:
            return rt.resolver(client, resource_id)

        def done(ok: bool, result: Any) -> None:
            self._confirm_and_save_grant(rt, resource_id, result if ok else None, tab)

        self._run_async(work, done)

    def _on_candidates_listed(self, rt: GrantResourceType, candidates: list[tuple[str, str]]) -> None:
        if not candidates:
            rumps.alert("PrivacyFence", f"No {rt.label.lower()} found.")
            return
        options = [f"{name} ({_short_id(resource_id)})" for resource_id, name in candidates]

        # See _add_rule's comment -- _osascript_pick must not block the main
        # thread directly from an AppKit callback.
        def work() -> str | None:
            return _osascript_pick(
                title=f"Add {rt.singular}", prompt=f"Select a {rt.singular} to trust:", options=options
            )

        def done(ok: bool, choice: Any) -> None:
            if not ok or not choice:
                return
            chosen_idx = options.index(choice)
            resource_id, name = candidates[chosen_idx]
            self._confirm_and_save_grant(rt, resource_id, name, "")

        self._run_async(work, done)

    def _confirm_and_save_grant(
        self, rt: GrantResourceType, resource_id: str, name: str | None, tab: str
    ) -> None:
        label = name or resource_id
        message = (
            f"Add '{label}' as a trusted {rt.singular}?"
            if name
            else f"Could not resolve a name for {resource_id} — add it by ID anyway?"
        )
        resp = rumps.alert(title=f"Add {rt.singular}", message=message, ok="Add", cancel="Cancel")
        if resp != 1:
            return

        cfg = self._load_config()
        grants_cfg = cfg.setdefault("auto_accept_grants", {})
        entries = get_grant_entries(grants_cfg, rt)
        if any(rt.id_of(e) == resource_id and e.get("tab") == (tab or None) for e in entries):
            rumps.alert("PrivacyFence", f"That {rt.singular} is already trusted.")
            return

        entry: dict[str, Any] = {rt.id_field: resource_id}
        if name:
            entry["name"] = name
        if tab:
            entry["tab"] = tab
        entries.append(entry)
        set_grant_entries(grants_cfg, rt, entries)
        self._save_and_reload(cfg)

    # ------------------------------------------------------------------ #
    # Organization config bundle
    # ------------------------------------------------------------------ #

    def _install_org_config(self, _sender: Any = None) -> None:
        script = (
            'set chosenFile to choose file with prompt '
            '"Select the organization config bundle your IT team sent you" '
            'of type {"json", "public.json"}\n'
            'return POSIX path of chosenFile'
        )
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        src = result.stdout.strip()
        if not src:
            return

        try:
            with open(src, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            rumps.alert("PrivacyFence", f"Could not read that file as JSON:\n{exc}")
            return
        if not isinstance(data, dict) or "version" not in data:
            rumps.alert(
                "PrivacyFence",
                "That file doesn't look like a PrivacyFence organization config bundle "
                "(expected a JSON object with a \"version\" field).",
            )
            return

        dest = org_dir() / "org_config.json"
        try:
            with open(dest, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.chmod(dest, 0o600)
        except OSError as exc:
            rumps.alert("PrivacyFence", f"Could not install organization config:\n{exc}")
            return

        self._rebuild()
        org_name = data.get("org_name", "")
        installed = ", ".join(s for s in ORG_BUNDLE_SERVICES if data.get(s)) or "none"
        rumps.alert(
            "PrivacyFence",
            f"Organization config installed{f' for {org_name}' if org_name else ''}.\n\n"
            f"Services available: {installed}\n\n"
            "Use Authenticate… on each connector you want to use.",
        )

    # ------------------------------------------------------------------ #
    # PII detection gate
    # ------------------------------------------------------------------ #

    def _build_pii_menu(self, pii_cfg: dict[str, Any], pii_enabled: bool) -> rumps.MenuItem:
        pii_item = rumps.MenuItem("PII Detection Gate")

        enabled_item = rumps.MenuItem("Enabled", callback=self._toggle_pii_detection)
        enabled_item.state = pii_enabled
        pii_item.add(enabled_item)
        pii_item.add(rumps.separator)

        for key, label in PII_OPTIONAL_CATEGORIES:
            item = rumps.MenuItem(f"  {label}")
            item.state = pii_cfg.get(key, True)
            # Grayed out (no callback) while the gate itself is off -- these
            # two categories are meaningless without the master switch on.
            item.set_callback(_bind(self._toggle_pii_category, key) if pii_enabled else None)
            pii_item.add(item)

        return pii_item

    def _toggle_pii_detection(self, _sender: Any = None) -> None:
        cfg = self._load_config()
        pii_cfg = cfg.setdefault("pii_detection", {})
        enabled = not pii_cfg.get("enabled", True)
        pii_cfg["enabled"] = enabled
        self._save_config(cfg)
        set_pii_detection_enabled(enabled)
        self._rebuild()

    def _toggle_pii_category(self, category_key: str, _sender: Any = None) -> None:
        cfg = self._load_config()
        pii_cfg = cfg.setdefault("pii_detection", {})
        enabled = not pii_cfg.get(category_key, True)
        pii_cfg[category_key] = enabled
        self._save_config(cfg)
        set_pii_category_enabled(category_key, enabled)
        self._rebuild()

    # ------------------------------------------------------------------ #
    # Privacy filter (privacy / drive_privacy / slack_privacy)
    # ------------------------------------------------------------------ #

    def _change_privacy_default(self, group: str, _sender: Any = None) -> None:
        cfg = self._load_config()
        current = _parse_privacy_group(cfg.get(group))["default_policy"]

        # Off the main thread, like every other osascript-backed picker in
        # this file (see _add_rule's comment on why calling _osascript_pick
        # straight from a button handler segfaults AppKit).
        def work() -> str | None:
            return _osascript_pick(
                title="Privacy Filter",
                prompt=f"Default policy for {PRIVACY_GROUP_LABELS.get(group, group)}:",
                options=list(PRIVACY_POLICIES),
                default=current,
            )

        def done(ok: bool, result: Any) -> None:
            if ok and result:
                self._finish_change_privacy_default(group, result)

        self._run_async(work, done)

    def _finish_change_privacy_default(self, group: str, policy: str) -> None:
        cfg = self._load_config()
        cfg.setdefault(group, {})["default_policy"] = policy
        self._save_and_reload_privacy(cfg)

    def _change_privacy_category(self, group: str, category: str, _sender: Any = None) -> None:
        cfg = self._load_config()
        parsed = _parse_privacy_group(cfg.get(group))
        current = parsed["categories"].get(category, parsed["default_policy"])
        options = [*PRIVACY_POLICIES, "(use group default)"]

        def work() -> str | None:
            return _osascript_pick(
                title="Privacy Filter",
                prompt=f"Policy for {PRIVACY_CATEGORY_LABELS.get(group, {}).get(category, category)}:",
                options=options,
                default=current,
            )

        def done(ok: bool, result: Any) -> None:
            if ok and result:
                self._finish_change_privacy_category(group, category, result)

        self._run_async(work, done)

    def _finish_change_privacy_category(self, group: str, category: str, choice: str) -> None:
        cfg = self._load_config()
        categories = cfg.setdefault(group, {}).setdefault("categories", {})
        if choice == "(use group default)":
            categories.pop(category, None)
        else:
            categories[category] = choice
        self._save_and_reload_privacy(cfg)

    def _save_and_reload_privacy(self, cfg: dict) -> None:
        self._save_config(cfg)
        try:
            # No changed-listener to piggyback on the way reload_rules has
            # _on_rules_changed -- rebuild explicitly.
            init_privacy_filter(cfg)
        except Exception as exc:
            logger.warning("Privacy filter hot-reload failed: %s", exc)
        self._rebuild()

    # ------------------------------------------------------------------ #
    # Connector actions
    # ------------------------------------------------------------------ #

    def _toggle_connector(self, cname: str, _sender: Any = None) -> None:
        cfg = self._load_config()
        conn = cfg.setdefault("connectors", {}).setdefault(cname, {})
        conn["enabled"] = not conn.get("enabled", True)
        self._save_config(cfg)
        self._rebuild()
        self._refresh_connectors()

    def _refresh_connectors(self) -> None:
        """Re-run connector construction (which re-checks auth/enabled state
        for every service) and push the result live into the running IPC
        server, so authenticating or toggling a connector takes effect
        immediately instead of requiring a restart."""

        def work() -> list:
            cfg = self._load_config()
            org_config = load_org_config()
            return build_connectors(cfg, org_config)

        def done(ok: bool, result: Any) -> None:
            if ok:
                self._connectors = [c.name for c in result]
                self._connector_objs = {c.name: c for c in result}
                self._ipc_server.set_connectors(result)
            self._rebuild()

        self._run_async(work, done)

    def _authenticate(self, cname: str, _sender: Any = None) -> None:
        org_config = load_org_config()
        if cname in GOOGLE_CONNECTORS:
            self._authenticate_google(cname, org_config)
        elif cname == "slack":
            self._authenticate_slack(org_config)
        elif cname == "salesforce":
            self._authenticate_salesforce(org_config)
        elif cname in ("jira", "confluence"):
            self._authenticate_atlassian(org_config)
        elif cname == "telegram":
            self._authenticate_telegram()

    def _run_async(self, work, on_done) -> None:
        """Run ``work()`` on a background thread.

        ``on_done(ok: bool, result)`` is called on the main thread via
        AppHelper.callAfter — ``result`` is the return value on success, or
        the raised exception on failure. Never call rumps/AppKit APIs
        (alert, Window, menu mutation) from ``work``; do it in ``on_done``.
        """
        def _runner() -> None:
            try:
                result = work()
                AppHelper.callAfter(on_done, True, result)
            except Exception as exc:  # noqa: BLE001 - surfaced to the user via on_done
                AppHelper.callAfter(on_done, False, exc)

        threading.Thread(target=_runner, daemon=True).start()

    def _prompt(self, **window_kwargs: Any) -> tuple[bool, str]:
        """Show a rumps.Window from any thread; blocks the caller until answered."""
        result: dict[str, Any] = {}
        done = threading.Event()

        def _show() -> None:
            resp = rumps.Window(**window_kwargs).run()
            result["clicked"] = resp.clicked
            result["text"] = resp.text
            done.set()

        AppHelper.callAfter(_show)
        done.wait()
        return bool(result.get("clicked")), (result.get("text") or "")

    def _authenticate_google(self, cname: str, org_config: dict[str, Any]) -> None:
        client_config = _google_client_config(org_config)
        if not client_config:
            rumps.alert("PrivacyFence", "Google organization config isn't installed yet.")
            return
        client_cls = _GOOGLE_CLIENTS[cname]
        token_file = str(data_dir() / TOKEN_FILES[cname])

        def work() -> str:
            client = client_cls(client_config=client_config, token_file=token_file)
            client.authorize_interactive()
            return client.check_connection()

        def done(ok: bool, result: Any) -> None:
            if ok:
                rumps.alert("PrivacyFence", f"{cname.capitalize()} connected as {result}.")
                self._refresh_connectors()
            else:
                rumps.alert("PrivacyFence", f"{cname.capitalize()} authentication failed:\n{result}")
                self._rebuild()

        self._run_async(work, done)

    def _authenticate_slack(self, org_config: dict[str, Any]) -> None:
        slack_org = org_config.get("slack") or {}
        if not slack_org.get("client_id"):
            rumps.alert("PrivacyFence", "Slack organization config isn't installed yet.")
            return
        token_file = str(data_dir() / TOKEN_FILES["slack"])

        def work() -> dict[str, Any]:
            return slack_authorize_interactive(
                client_id=slack_org["client_id"],
                client_secret=slack_org.get("client_secret", ""),
                token_file=token_file,
                user_scopes=slack_org.get("user_scopes"),
            )

        def done(ok: bool, result: Any) -> None:
            if ok:
                rumps.alert("PrivacyFence", f"Slack connected: {result.get('team_name', '')}.")
                self._refresh_connectors()
            else:
                rumps.alert("PrivacyFence", f"Slack authentication failed:\n{result}")
                self._rebuild()

        self._run_async(work, done)

    def _authenticate_salesforce(self, org_config: dict[str, Any]) -> None:
        sf_org = org_config.get("salesforce") or {}
        if not sf_org.get("consumer_key"):
            rumps.alert("PrivacyFence", "Salesforce organization config isn't installed yet.")
            return
        token_file = str(data_dir() / TOKEN_FILES["salesforce"])

        def work() -> dict[str, Any]:
            return salesforce_authorize_interactive(
                consumer_key=sf_org["consumer_key"],
                consumer_secret=sf_org.get("consumer_secret", ""),
                token_file=token_file,
                login_url=sf_org.get("login_url", "https://login.salesforce.com"),
            )

        def done(ok: bool, result: Any) -> None:
            if ok:
                rumps.alert("PrivacyFence", f"Salesforce connected: {result.get('instance_url', '')}.")
                self._refresh_connectors()
            else:
                rumps.alert("PrivacyFence", f"Salesforce authentication failed:\n{result}")
                self._rebuild()

        self._run_async(work, done)

    def _authenticate_atlassian(self, org_config: dict[str, Any]) -> None:
        atlassian_org = org_config.get("atlassian") or {}
        if not atlassian_org.get("client_id"):
            rumps.alert("PrivacyFence", "Atlassian organization config isn't installed yet.")
            return
        token_file = str(data_dir() / TOKEN_FILES["atlassian"])

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
            if ok:
                rumps.alert(
                    "PrivacyFence",
                    f"Atlassian connected: {result.get('site_url', '')}.\n\n"
                    "This covers both Jira and Confluence.",
                )
                self._refresh_connectors()
            else:
                rumps.alert("PrivacyFence", f"Atlassian authentication failed:\n{result}")
                self._rebuild()

        self._run_async(work, done)

    def _authenticate_telegram(self) -> None:
        creds = telegram_app_credentials()
        if not creds:
            rumps.alert("PrivacyFence", "Telegram app credentials are missing from this build.")
            return
        api_id, api_hash = creds
        session_file = str(data_dir() / TOKEN_FILES["telegram"])

        def flow() -> str:
            from telethon import TelegramClient
            from telethon.errors import SessionPasswordNeededError

            clicked, phone = self._prompt(
                title="Telegram Sign-in",
                message="Phone number (with country code, e.g. +1234567890):",
                ok="Send Code", cancel="Cancel",
            )
            if not clicked or not phone.strip():
                raise _AuthFlowCancelled()
            phone = phone.strip()

            async def _send_code() -> str:
                client = TelegramClient(session_file, api_id, api_hash)
                await client.connect()
                try:
                    result = await client.send_code_request(phone)
                    return result.phone_code_hash
                finally:
                    await client.disconnect()

            phone_code_hash = asyncio.run(_send_code())

            clicked, code = self._prompt(
                title="Telegram Sign-in",
                message="Enter the verification code Telegram sent you:",
                ok="Authorize", cancel="Cancel",
            )
            if not clicked or not code.strip():
                raise _AuthFlowCancelled()
            code = code.strip()

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

            name = asyncio.run(_sign_in())
            if name != "__needs_2fa__":
                return name

            clicked, password = self._prompt(
                title="Telegram Sign-in",
                message="Two-step verification password:",
                ok="Submit", cancel="Cancel", secure=True,
            )
            if not clicked or not password.strip():
                raise _AuthFlowCancelled()
            password = password.strip()

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
            if not ok and isinstance(result, _AuthFlowCancelled):
                return
            if ok:
                rumps.alert("PrivacyFence", f"Telegram connected as {result}.")
                self._refresh_connectors()
            else:
                rumps.alert("PrivacyFence", f"Telegram sign-in failed:\n{result}")
                self._rebuild()

        self._run_async(flow, done)

    # ------------------------------------------------------------------ #
    # Misc actions
    # ------------------------------------------------------------------ #

    def export_audit_log(self, _: Any = None) -> None:
        log_dir = Path(data_dir()) / "logs" / "audit"
        if not log_dir.exists():
            rumps.alert("PrivacyFence", "No audit log found yet.")
            return

        week = current_week()
        xlsx_path = None
        if (log_dir / f"{week}.jsonl").exists():
            xlsx_path = AuditLogger(str(log_dir)).export_week_to_excel(week)

        # Refreshed this week's Excel export so it includes everything logged
        # since the last daemon restart; fall back to the folder (e.g. if
        # openpyxl is missing or there's nothing logged yet this week).
        subprocess.run(["open", xlsx_path or str(log_dir)], check=False)

    def show_about(self, _: Any = None) -> None:
        resp = rumps.alert(
            title="About PrivacyFence",
            message=f"PrivacyFence {__version__}\nLicense: {LICENSE_NAME}\n\n{REPO_URL}",
            ok="Open GitHub",
            cancel="Close",
        )
        if resp == 1:
            subprocess.run(["open", REPO_URL], check=False)

    def quit_app(self, _: Any = None) -> None:
        rumps.quit_application()

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
            # Triggers _on_rules_changed() -> _rebuild(), so the menu picks
            # up the change without a separate explicit rebuild here.
            reload_rules(build_effective_rules(cfg))
        except Exception as exc:
            logger.warning("Rule hot-reload failed: %s", exc)
            self._rebuild()


# ---------------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------------- #

def _colorize_status_glyph(item: rumps.MenuItem, glyph: str, color: NSColor) -> None:
    """Color just the leading status glyph (● ✕ ○ ◐) in an otherwise
    plain-text menu item title -- item.title stays untouched (tests and
    VoiceOver still see the same plain "glyph name" string; see
    _build_connectors_menu), this only changes what's drawn. One color per
    state (green/gray/red/amber) instead of four uncolored glyphs sharing no
    visual language with the native checkmark PII Detection Gate already
    uses for on/off."""
    title = item.title
    font = NSFont.menuFontOfSize_(0)
    attributed = NSMutableAttributedString.alloc().initWithString_attributes_(
        title, {NSFontAttributeName: font, NSForegroundColorAttributeName: NSColor.labelColor()}
    )
    attributed.addAttribute_value_range_(NSForegroundColorAttributeName, color, (0, len(glyph)))
    item._menuitem.setAttributedTitle_(attributed)


def _bind(fn, *bound_args):
    """Return a rumps-compatible callback with pre-bound positional args."""
    def _cb(sender):
        fn(*bound_args, sender)
    return _cb


def _format_pair_line(entry: Any) -> str:
    """Render an approved_spreadsheet entry as "id" or "id:tab"."""
    if not isinstance(entry, dict):
        return str(entry)
    spreadsheet_id = entry.get("spreadsheet_id", "")
    tab = entry.get("tab")
    return f"{spreadsheet_id}:{tab}" if tab else spreadsheet_id


def _parse_pair_lines(text: str) -> list[dict[str, str]]:
    """Parse "spreadsheet_id" / "spreadsheet_id:tab" lines into rule entries."""
    entries: list[dict[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        spreadsheet_id, sep, tab = line.partition(":")
        entry: dict[str, str] = {"spreadsheet_id": spreadsheet_id.strip()}
        if sep and tab.strip():
            entry["tab"] = tab.strip()
        entries.append(entry)
    return entries


def _osascript_pick(title: str, prompt: str, options: list[str], default: str | None = None) -> str | None:
    """Show a native macOS list-picker and return the chosen item or None.

    ``default``, if given and present in ``options``, pre-highlights that
    item (AppleScript's "default items") -- purely cosmetic, existing
    callers that don't pass it see no change in behavior."""
    opts_as = "{" + ", ".join(f'"{o}"' for o in options) + "}"
    default_clause = f' with default items {{"{default}"}}' if default in options else ""
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


def _find_icon() -> str | None:
    here = Path(__file__).parent / "resources"
    for name in ("icon_menubar.png", "icon_32.png", "icon_64.png", "icon_512.png"):
        p = here / name
        if p.exists():
            return str(p)
    return None


def run_menu_bar(
    config_path: str,
    connectors: list[str],
    ipc_server: "IPCServer",
    connector_objs: list[Any] | None = None,
) -> None:
    app = PrivacyFenceMenuBar(
        config_path=config_path,
        connectors=connectors,
        ipc_server=ipc_server,
        connector_objs=connector_objs,
    )
    app.run()
