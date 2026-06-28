"""macOS menu bar app (rumps).

Main thread only. Provides:
  - Auto-accept rule management: add / toggle / edit values per operation
  - Connector management: enable/disable + OAuth setup shortcuts
  - Edit Config File shortcut
  - Open Audit Log
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import rumps
import yaml

from .auto_accept import get_auto_accept_evaluator, reload_rules
from .paths import data_dir

logger = logging.getLogger(__name__)

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

# Connectors that have an OAuth setup command
OAUTH_FLAGS: dict[str, str] = {
    "gmail":    "--gmail-oauth",
    "drive":    "--drive-oauth",
    "contacts": "--contacts-oauth",
    "calendar": "--calendar-oauth",
    "tasks":    "--tasks-oauth",
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

class LooplineMenuBar(rumps.App):
    def __init__(self, config_path: str, connectors: list[str]) -> None:
        self._config_path = config_path
        self._connectors = connectors
        icon_path = _find_icon()
        super().__init__(
            name="Loopline",
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
            op_rules = rules_cfg.get(op_key, [])

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
        for cname in self._connectors:
            conn_cfg = connectors_cfg.get(cname, {})
            enabled = conn_cfg.get("enabled", True)
            check = "✓" if enabled else "○"
            conn_item = rumps.MenuItem(f"{check} {cname}")

            toggle = rumps.MenuItem("  Toggle enabled")
            toggle.set_callback(_bind(self._toggle_connector, cname))
            conn_item.add(toggle)

            if cname in OAUTH_FLAGS:
                oauth = rumps.MenuItem("  Run OAuth setup…")
                oauth.set_callback(_bind(self._run_oauth, cname))
                conn_item.add(oauth)

            connectors_parent.add(conn_item)

        self.menu.clear()
        self.menu = [
            rumps.MenuItem("Loopline is running"),
            rumps.separator,
            rules_parent,
            connectors_parent,
            rumps.separator,
            rumps.MenuItem("Edit Config File…", callback=self.edit_config),
            rumps.MenuItem("Open Audit Log", callback=self.open_audit_log),
            rumps.separator,
            rumps.MenuItem("Quit Loopline", callback=self.quit_app),
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

    def _run_oauth(self, cname: str, _sender: Any = None) -> None:
        flag = OAUTH_FLAGS.get(cname)
        if not flag:
            return
        cmd = shutil.which("loopline-app")
        if not cmd:
            # Try the same executable that started this process
            cmd = sys.argv[0] if sys.argv[0].endswith("loopline-app") else sys.executable
        full_cmd = f"{cmd} {flag}"
        script = f'tell application "Terminal" to do script "{full_cmd}"'
        subprocess.run(["osascript", "-e", script], check=False)

    # ------------------------------------------------------------------ #
    # Misc actions
    # ------------------------------------------------------------------ #

    def edit_config(self, _: Any = None) -> None:
        subprocess.run(["open", "-t", self._config_path], check=False)

    def open_audit_log(self, _: Any = None) -> None:
        log_dir = Path(data_dir()) / "logs" / "audit"
        if log_dir.exists():
            subprocess.run(["open", str(log_dir)], check=False)
        else:
            rumps.alert("Loopline", "No audit log found yet.")

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
    for name in ("icon_32.png", "icon_64.png", "icon_512.png"):
        p = here / name
        if p.exists():
            return str(p)
    return None


def run_menu_bar(config_path: str, connectors: list[str]) -> None:
    app = LooplineMenuBar(config_path=config_path, connectors=connectors)
    app.run()
