"""macOS menu bar app (rumps).

Main thread only, except where noted. Provides:
  - Auto-accept rule management: add / toggle / edit values per operation
  - Organization config bundle install/update (the IT-admin-facing side of
    connector setup — see the module docstring in daemon_main.py)
  - Per-connector Authenticate…: runs each service's browser OAuth flow (or,
    for Telegram, the phone+code(+2FA) flow) directly, no Terminal window
  - Open Audit Log / About panel

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
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import rumps
import yaml
from PyObjCTools import AppHelper

from . import __version__
from .auto_accept import reload_rules
from .paths import data_dir, org_dir
from .app_credentials import telegram_app_credentials
from .daemon_main import TOKEN_FILES, build_connectors, load_org_config
from .atlassian_oauth import authorize_interactive as atlassian_authorize_interactive
from .calendar_client import CalendarClient
from .contacts_client import ContactsClient
from .drive_client import DriveClient
from .gmail_client import GmailClient
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
    "drive.read_file_contents":    "Drive – Read file",
    "drive.write_file":            "Drive – Write file",
    "drive.move_file":             "Drive – Move file",
    "drive.comment_file":          "Drive – Add comment",
    "slack.read_messages":         "Slack – Read messages",
    "slack.send_message":          "Slack – Send message",
    "calendar.read_event_details": "Calendar – Read event",
    "calendar.create_modify_event":"Calendar – Create/modify event",
    "salesforce.read_record":      "Salesforce – Read record",
    "salesforce.run_report":       "Salesforce – Run report",
    "contacts.edit":               "Contacts – Update contact",
}

RULES_BY_OPERATION: dict[str, list[str]] = {
    "gmail.read_message":           ["i_am_sender", "i_am_sole_recipient", "trusted_sender_domain", "label_match", "age_threshold_days", "no_attachments"],
    "gmail.read_thread":            ["i_am_sender", "trusted_sender_domain", "age_threshold_days"],
    "drive.read_file_contents":     ["i_am_owner", "created_by_me", "approved_folder", "file_type_allowlist", "created_this_session", "shared_drive_exclusion"],
    "drive.write_file":             ["i_am_owner", "approved_sandbox_folder", "file_type_allowlist", "created_this_session"],
    "drive.move_file":              ["move_within_approved_folders"],
    "drive.comment_file":           ["i_am_owner", "created_this_session"],
    "slack.read_messages":          ["dm_with_myself", "approved_channel", "public_channels_only", "no_file_attachments"],
    "slack.send_message":           ["dm_with_myself", "send_to_myself", "approved_channel", "approved_recipient", "reply_in_existing_thread"],
    "calendar.read_event_details":  ["i_am_organizer", "no_external_attendees", "personal_calendar", "past_event", "time_window_days", "no_conferencing_link"],
    "calendar.create_modify_event": ["i_am_organizer", "no_external_attendees", "personal_calendar"],
    "salesforce.read_record":       ["approved_object_types"],
    "salesforce.run_report":        ["approved_report_ids"],
    "contacts.edit":                [],
}

# Rules that take a list-of-strings value
RULES_LIST_VALUE: set[str] = {
    "trusted_sender_domain", "label_match", "send_to_myself",
    "approved_channel", "approved_recipient", "personal_calendar",
    "approved_object_types", "approved_report_ids", "file_type_allowlist",
    "approved_folder", "approved_sandbox_folder",
}
# Rules that take a single integer value
RULES_INT_VALUE: set[str] = {"age_threshold_days", "time_window_days"}

# All connectors PrivacyFence supports, in display order
ALL_CONNECTORS: list[str] = [
    "gmail", "drive", "contacts", "calendar", "tasks",
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
    "approved_recipient":    "U0123456789",
    "personal_calendar":     "primary",
    "time_window_days":      "14",
    "approved_object_types": "Account\nContact\nOpportunity",
    "approved_report_ids":   "00O000000000001",
    "file_type_allowlist":   "application/vnd.google-apps.document\ntext/plain",
    "approved_folder":       "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
    "approved_sandbox_folder": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
}


class _AuthFlowCancelled(Exception):
    """Raised internally when the user cancels a native prompt mid-flow."""


def _google_client_config(org_config: dict[str, Any]) -> dict[str, Any]:
    google = org_config.get("google") or {}
    if not google.get("client_id") or not google.get("client_secret"):
        return {}
    return {"installed": google}


# ---------------------------------------------------------------------------- #
# App
# ---------------------------------------------------------------------------- #

class PrivacyFenceMenuBar(rumps.App):
    def __init__(self, config_path: str, connectors: list[str], ipc_server: "IPCServer") -> None:
        self._config_path = config_path
        self._connectors = connectors
        self._ipc_server = ipc_server
        icon_path = _find_icon()
        super().__init__(
            name="PrivacyFence",
            icon=icon_path,
            quit_button=None,
            template=True,
        )
        self._rebuild()

    # ------------------------------------------------------------------ #
    # Menu building
    # ------------------------------------------------------------------ #

    def _rebuild(self) -> None:
        cfg = self._load_config()
        org_config = load_org_config()
        rules_cfg: dict[str, list[dict]] = cfg.get("auto_accept_rules", {}) or {}
        connectors_cfg: dict[str, dict] = cfg.get("connectors", {}) or {}

        rules_parent = rumps.MenuItem("Auto-accept Rules")
        for op_key, label in OPERATION_LABELS.items():
            op_item = rumps.MenuItem(label)
            op_rules = rules_cfg.get(op_key) or []

            # "Add rule…" at the top
            add_item = rumps.MenuItem("  + Add rule…")
            add_item.set_callback(_bind(self._add_rule, op_key))
            op_item.add(add_item)

            if op_rules:
                op_item.add(rumps.MenuItem("  ─────────────────"))

            for idx, rule_cfg in enumerate(op_rules):
                rule_name = rule_cfg.get("rule", "")
                value = rule_cfg.get("value")
                has_value = rule_name in RULES_LIST_VALUE or rule_name in RULES_INT_VALUE

                # Toggle item
                toggle = rumps.MenuItem(f"  ✓ {rule_name}")
                toggle.set_callback(_bind(self._toggle_rule, op_key, idx))
                op_item.add(toggle)

                # Edit value item (only if rule takes a value)
                if has_value:
                    value_preview = _format_value(value)
                    edit = rumps.MenuItem(f"      ↳ {value_preview}  Edit…")
                    edit.set_callback(_bind(self._edit_rule_value, op_key, idx))
                    op_item.add(edit)

                # Remove item
                remove = rumps.MenuItem(f"      ✕ Remove")
                remove.set_callback(_bind(self._remove_rule, op_key, idx))
                op_item.add(remove)

            rules_parent.add(op_item)

        org_parent = self._build_org_menu(org_config)
        connectors_parent = self._build_connectors_menu(org_config, connectors_cfg)

        self.menu.clear()
        self.menu = [
            rumps.MenuItem("PrivacyFence is running"),
            rumps.separator,
            org_parent,
            rules_parent,
            connectors_parent,
            rumps.separator,
            rumps.MenuItem("Open Audit Log", callback=self.open_audit_log),
            rumps.MenuItem("About PrivacyFence", callback=self.show_about),
            rumps.separator,
            rumps.MenuItem("Quit PrivacyFence", callback=self.quit_app),
        ]

    def _build_org_menu(self, org_config: dict[str, Any]) -> rumps.MenuItem:
        org_parent = rumps.MenuItem("Organization Config")
        if org_config:
            org_name = org_config.get("org_name", "")
            installed = [s for s in ORG_BUNDLE_SERVICES if org_config.get(s)]
            header = f"Installed: {org_name}" if org_name else "Installed"
            org_parent.add(rumps.MenuItem(header))
            org_parent.add(rumps.MenuItem("  Services: " + (", ".join(installed) or "none")))
        else:
            org_parent.add(rumps.MenuItem("No organization config installed"))
        org_parent.add(rumps.separator)
        install_item = rumps.MenuItem(
            "Install/Update Organization Config…" if org_config else "Install Organization Config…"
        )
        install_item.set_callback(self._install_org_config)
        org_parent.add(install_item)
        return org_parent

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
                status = "●"  # connected
            elif not enabled:
                status = "✕"  # disabled
            elif not has_org:
                status = "○"  # org config / app credentials missing
            else:
                status = "◐"  # org config present, needs authentication

            conn_item = rumps.MenuItem(f"{status} {cname.capitalize()}")

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
        available = RULES_BY_OPERATION.get(op_key, [])
        if not available:
            rumps.alert("Add Rule", f"No configurable rules available for\n{op_key}.")
            return

        rule_name = _osascript_pick(
            title="Add Auto-accept Rule",
            prompt=f"Select a rule to add to:\n{OPERATION_LABELS.get(op_key, op_key)}",
            options=available,
        )
        if not rule_name:
            return

        new_rule: dict[str, Any] = {"rule": rule_name}

        if rule_name in RULES_LIST_VALUE or rule_name in RULES_INT_VALUE:
            hint = RULE_HINTS.get(rule_name, "")
            kind = "integer" if rule_name in RULES_INT_VALUE else "one value per line"
            w = rumps.Window(
                title=f"Configure: {rule_name}",
                message=f"Enter value ({kind}):",
                default_text=hint,
                ok="Add",
                cancel="Cancel",
                dimensions=(320, 80),
            )
            resp = w.run()
            if not resp.clicked:
                return
            text = resp.text.strip()
            if not text:
                return
            if rule_name in RULES_INT_VALUE:
                try:
                    new_rule["value"] = int(text)
                except ValueError:
                    rumps.alert("Invalid value", f"Expected an integer, got: {text!r}")
                    return
            else:
                new_rule["value"] = [v.strip() for v in text.splitlines() if v.strip()]

        cfg = self._load_config()
        op_rules = cfg.setdefault("auto_accept_rules", {}).setdefault(op_key, [])
        op_rules.append(new_rule)
        self._save_and_reload(cfg)

    def _toggle_rule(self, op_key: str, idx: int, _sender: Any = None) -> None:
        cfg = self._load_config()
        rules = cfg.get("auto_accept_rules", {}).get(op_key, [])
        if idx >= len(rules):
            return
        rule = rules[idx]
        # Toggle by removing/re-adding a disabled marker — simplest: just remove
        # the rule entirely (re-add from scratch to re-enable). For now just
        # remove it so the user can re-add if needed. A cleaner model would be
        # an `enabled` flag but the evaluator doesn't support that.
        rules.pop(idx)
        if not rules:
            cfg.get("auto_accept_rules", {}).pop(op_key, None)
        else:
            cfg["auto_accept_rules"][op_key] = rules
        self._save_and_reload(cfg)

    def _edit_rule_value(self, op_key: str, idx: int, _sender: Any = None) -> None:
        cfg = self._load_config()
        rules = cfg.get("auto_accept_rules", {}).get(op_key, [])
        if idx >= len(rules):
            return
        rule = rules[idx]
        rule_name = rule.get("rule", "")
        current = rule.get("value")

        if rule_name in RULES_INT_VALUE:
            default_text = str(current) if current is not None else ""
            kind = "integer"
        else:
            vals = current if isinstance(current, list) else ([current] if current else [])
            default_text = "\n".join(str(v) for v in vals)
            kind = "one value per line"

        w = rumps.Window(
            title=f"Edit: {rule_name}",
            message=f"Edit value ({kind}):",
            default_text=default_text,
            ok="Save",
            cancel="Cancel",
            dimensions=(320, 80),
        )
        resp = w.run()
        if not resp.clicked:
            return
        text = resp.text.strip()
        if not text:
            return

        if rule_name in RULES_INT_VALUE:
            try:
                rule["value"] = int(text)
            except ValueError:
                rumps.alert("Invalid value", f"Expected an integer, got: {text!r}")
                return
        else:
            rule["value"] = [v.strip() for v in text.splitlines() if v.strip()]

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

    def open_audit_log(self, _: Any = None) -> None:
        log_dir = Path(data_dir()) / "logs" / "audit"
        if log_dir.exists():
            subprocess.run(["open", str(log_dir)], check=False)
        else:
            rumps.alert("PrivacyFence", "No audit log found yet.")

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
            reload_rules(cfg.get("auto_accept_rules", {}))
        except Exception as exc:
            logger.warning("Rule hot-reload failed: %s", exc)
        self._rebuild()


# ---------------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------------- #

def _bind(fn, *bound_args):
    """Return a rumps-compatible callback with pre-bound positional args."""
    def _cb(sender):
        fn(*bound_args, sender)
    return _cb


def _format_value(value: Any) -> str:
    if value is None:
        return "(none)"
    if isinstance(value, list):
        if not value:
            return "(empty)"
        preview = ", ".join(str(v) for v in value[:3])
        return preview + (f" +{len(value) - 3} more" if len(value) > 3 else "")
    return str(value)


def _osascript_pick(title: str, prompt: str, options: list[str]) -> str | None:
    """Show a native macOS list-picker and return the chosen item or None."""
    opts_as = "{" + ", ".join(f'"{o}"' for o in options) + "}"
    script = (
        f'set opts to {opts_as}\n'
        f'set chosen to (choose from list opts '
        f'with title "{title}" '
        f'with prompt "{prompt}")\n'
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


def run_menu_bar(config_path: str, connectors: list[str], ipc_server: "IPCServer") -> None:
    app = PrivacyFenceMenuBar(config_path=config_path, connectors=connectors, ipc_server=ipc_server)
    app.run()
