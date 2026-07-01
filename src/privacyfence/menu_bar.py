"""macOS menu bar app (rumps).

Main thread only. Provides:
  - Auto-accept rule management: add / toggle / edit values per operation
  - Connector management: enable/disable, configure, authenticate, help
  - About panel
  - Open Audit Log
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, NamedTuple

import rumps
import yaml

from . import __version__
from .auto_accept import reload_rules
from .paths import data_dir

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

# Connectors that have an interactive authentication command (OAuth or
# session login). Run in a Terminal window via the CLI flag.
AUTH_FLAGS: dict[str, str] = {
    "gmail":    "--gmail-oauth",
    "drive":    "--drive-oauth",
    "contacts": "--contacts-oauth",
    "calendar": "--calendar-oauth",
    "tasks":    "--tasks-oauth",
    "telegram": "--telegram-setup",
}

# All connectors PrivacyFence supports, in display order
ALL_CONNECTORS: list[str] = [
    "gmail", "drive", "contacts", "calendar", "tasks",
    "slack", "jira", "confluence", "salesforce", "telegram",
]

# Connectors authenticated via a shared Google OAuth client secret file,
# configured by uploading the JSON downloaded from Google Cloud Console.
GOOGLE_CONNECTORS: set[str] = {"gmail", "drive", "contacts", "calendar", "tasks"}


class ConfigField(NamedTuple):
    key: str
    label: str
    secret: bool = False
    kind: str = "text"  # "text" or "int"


# Text fields collected via native prompts for "Configure…", per connector.
# Google connectors are configured via client-secret upload instead (see
# GOOGLE_CONNECTORS / _upload_client_secret).
CONNECTOR_CONFIG_FIELDS: dict[str, list[ConfigField]] = {
    "slack": [
        ConfigField("bot_token", "Bot User OAuth Token (xoxb-...)", secret=True),
    ],
    "jira": [
        ConfigField("cloud_url", "Atlassian Cloud URL (e.g. https://yourcompany.atlassian.net)"),
        ConfigField("email", "Atlassian account email"),
        ConfigField("api_token", "API token", secret=True),
    ],
    "confluence": [
        ConfigField("cloud_url", "Atlassian Cloud URL (e.g. https://yourcompany.atlassian.net)"),
        ConfigField("email", "Atlassian account email"),
        ConfigField("api_token", "API token", secret=True),
    ],
    "salesforce": [
        ConfigField("instance_url", "Instance URL (e.g. https://yourorg.my.salesforce.com)"),
        ConfigField("username", "Username"),
        ConfigField("password", "Password", secret=True),
        ConfigField("security_token", "Security token", secret=True),
    ],
    "telegram": [
        ConfigField("api_id", "API ID (from https://my.telegram.org/apps)", kind="int"),
        ConfigField("api_hash", "API hash", secret=True),
    ],
}

# docs/ file (on GitHub) explaining how to set up each connector
CONNECTOR_HELP_DOCS: dict[str, str] = {
    "gmail":      "google-cloud-setup.md",
    "drive":      "google-cloud-setup.md",
    "contacts":   "google-cloud-setup.md",
    "calendar":   "google-cloud-setup.md",
    "tasks":      "google-cloud-setup.md",
    "slack":      "slack-setup.md",
    "jira":       "atlassian-setup.md",
    "confluence": "atlassian-setup.md",
    "salesforce": "salesforce-setup.md",
    "telegram":   "telegram-setup.md",
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


# ---------------------------------------------------------------------------- #
# App
# ---------------------------------------------------------------------------- #

class PrivacyFenceMenuBar(rumps.App):
    def __init__(self, config_path: str, connectors: list[str]) -> None:
        self._config_path = config_path
        self._connectors = connectors
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

        connectors_parent = rumps.MenuItem("Connectors")
        for cname in ALL_CONNECTORS:
            connected = cname in self._connectors
            conn_cfg = connectors_cfg.get(cname, {})
            enabled = conn_cfg.get("enabled", True)

            status = "●" if connected else ("○" if enabled else "✕")
            conn_item = rumps.MenuItem(f"{status} {cname.capitalize()}")

            toggle_label = "  Disable" if enabled else "  Enable"
            toggle = rumps.MenuItem(toggle_label)
            toggle.set_callback(_bind(self._toggle_connector, cname))
            conn_item.add(toggle)

            conn_item.add(rumps.separator)

            if cname in GOOGLE_CONNECTORS:
                configure = rumps.MenuItem("  Configure… (upload Google client secret)")
                configure.set_callback(_bind(self._upload_client_secret, cname))
                conn_item.add(configure)
            elif cname in CONNECTOR_CONFIG_FIELDS:
                configure = rumps.MenuItem("  Configure…")
                configure.set_callback(_bind(self._configure_connector, cname))
                conn_item.add(configure)

            if cname in AUTH_FLAGS:
                authenticate = rumps.MenuItem("  Authenticate…")
                authenticate.set_callback(_bind(self._run_auth, cname))
                conn_item.add(authenticate)

            help_item = rumps.MenuItem("  Help…")
            help_item.set_callback(_bind(self._open_help, cname))
            conn_item.add(help_item)

            connectors_parent.add(conn_item)

        self.menu.clear()
        self.menu = [
            rumps.MenuItem("PrivacyFence is running"),
            rumps.separator,
            rules_parent,
            connectors_parent,
            rumps.separator,
            rumps.MenuItem("Open Audit Log", callback=self.open_audit_log),
            rumps.MenuItem("About PrivacyFence", callback=self.show_about),
            rumps.separator,
            rumps.MenuItem("Quit PrivacyFence", callback=self.quit_app),
        ]

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
    # Connector actions
    # ------------------------------------------------------------------ #

    def _toggle_connector(self, cname: str, _sender: Any = None) -> None:
        cfg = self._load_config()
        conn = cfg.setdefault("connectors", {}).setdefault(cname, {})
        conn["enabled"] = not conn.get("enabled", True)
        self._save_config(cfg)
        self._rebuild()

    def _configure_connector(self, cname: str, _sender: Any = None) -> None:
        fields = CONNECTOR_CONFIG_FIELDS.get(cname)
        if not fields:
            return
        cfg = self._load_config()
        section = cfg.setdefault(cname, {})

        collected: dict[str, Any] = {}
        for field in fields:
            current = section.get(field.key)
            if field.secret:
                default_text = "" if current in (None, "") else "••••••••"
            else:
                default_text = "" if current is None else str(current)

            w = rumps.Window(
                title=f"Configure {cname.capitalize()}",
                message=f"{field.label}:" + (
                    "\n(leave unchanged to keep the current value)" if field.secret and current else ""
                ),
                default_text=default_text,
                ok="Next" if field is not fields[-1] else "Save",
                cancel="Cancel",
                dimensions=(320, 40),
            )
            resp = w.run()
            if not resp.clicked:
                return
            text = resp.text.strip()

            if field.secret and current and text == "••••••••":
                continue  # unchanged
            if not text:
                continue

            if field.kind == "int":
                try:
                    collected[field.key] = int(text)
                except ValueError:
                    rumps.alert("Invalid value", f"Expected an integer for {field.label}, got: {text!r}")
                    return
            else:
                collected[field.key] = text

        section.update(collected)
        cfg.setdefault("connectors", {}).setdefault(cname, {}).setdefault("enabled", True)
        self._save_config(cfg)
        self._rebuild()
        rumps.alert("PrivacyFence", f"{cname.capitalize()} configured. Quit and reopen PrivacyFence to apply.")

    def _upload_client_secret(self, cname: str, _sender: Any = None) -> None:
        script = (
            'set chosenFile to choose file with prompt '
            '"Select the Google OAuth client secret JSON file" '
            'of type {"json", "public.json"}\n'
            'return POSIX path of chosenFile'
        )
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        src = result.stdout.strip()
        if not src:
            return

        cfg = self._load_config()
        section = cfg.setdefault(cname, {})
        dest_rel = section.get("credentials_file", "credentials/client_secret.json")
        dest = Path(dest_rel)
        if not dest.is_absolute():
            dest = Path(data_dir()) / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            shutil.copyfile(src, dest)
        except OSError as exc:
            rumps.alert("PrivacyFence", f"Could not copy client secret: {exc}")
            return

        section.setdefault("credentials_file", dest_rel)
        cfg.setdefault("connectors", {}).setdefault(cname, {}).setdefault("enabled", True)
        self._save_config(cfg)
        self._rebuild()
        rumps.alert(
            "PrivacyFence",
            f"Client secret installed for {cname.capitalize()}.\n\nUse 'Authenticate…' to complete OAuth.",
        )

    def _run_auth(self, cname: str, _sender: Any = None) -> None:
        flag = AUTH_FLAGS.get(cname)
        if not flag:
            return
        cmd = shutil.which("privacyfence-app")
        if not cmd:
            # Try the same executable that started this process
            cmd = sys.argv[0] if sys.argv[0].endswith("privacyfence-app") else sys.executable
        full_cmd = f"{cmd} {flag}"
        script = f'tell application "Terminal" to do script "{full_cmd}"'
        subprocess.run(["osascript", "-e", script], check=False)

    def _open_help(self, cname: str, _sender: Any = None) -> None:
        doc = CONNECTOR_HELP_DOCS.get(cname)
        if not doc:
            return
        url = f"{REPO_URL}/blob/main/docs/{doc}"
        subprocess.run(["open", url], check=False)

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


def run_menu_bar(config_path: str, connectors: list[str]) -> None:
    app = PrivacyFenceMenuBar(config_path=config_path, connectors=connectors)
    app.run()
