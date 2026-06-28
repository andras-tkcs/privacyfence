"""macOS menu bar app (rumps).

Runs on the main thread. Shows Loopline status, recent activity count,
and config submenus for auto-accept rules and connector toggles.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

import rumps
import yaml

from .auto_accept import get_auto_accept_evaluator
from .paths import data_dir

logger = logging.getLogger(__name__)

_CONFIG_PATH: str = ""  # set by LooplineMenuBar on init


class LooplineMenuBar(rumps.App):
    def __init__(self, config_path: str, connectors: list[str]) -> None:
        global _CONFIG_PATH
        _CONFIG_PATH = config_path
        self._connectors = connectors
        self._config_path = config_path

        icon_path = _find_icon()
        super().__init__(
            name="Loopline",
            icon=icon_path,
            quit_button=None,
            template=True,
        )
        self._build_menu()

    # ------------------------------------------------------------------ #
    # Menu construction
    # ------------------------------------------------------------------ #

    def _build_menu(self) -> None:
        cfg = self._load_config()

        rules_items = self._build_rules_menu(cfg)
        connector_items = self._build_connectors_menu(cfg)

        self.menu = [
            rumps.MenuItem("Loopline is running", callback=None),
            rumps.separator,
            rumps.MenuItem("Auto-accept Rules", callback=None),
            *[rumps.MenuItem("  " + item.title, callback=item.callback)
              for item in rules_items],
            rumps.separator,
            rumps.MenuItem("Connectors", callback=None),
            *[rumps.MenuItem("  " + item.title, callback=item.callback)
              for item in connector_items],
            rumps.separator,
            rumps.MenuItem("Open Audit Log", callback=self.open_audit_log),
            rumps.separator,
            rumps.MenuItem("Quit Loopline", callback=self.quit_app),
        ]

    def _build_rules_menu(self, cfg: dict) -> list[rumps.MenuItem]:
        rules_cfg: dict = cfg.get("auto_accept_rules", {})
        items = []
        for rule_key, rule_val in rules_cfg.items():
            enabled = bool(rule_val) if not isinstance(rule_val, dict) else True
            title = f"{'✓' if enabled else '○'} {rule_key}"
            item = rumps.MenuItem(title)
            item._rule_key = rule_key  # type: ignore[attr-defined]
            item.set_callback(self._toggle_rule)
            items.append(item)
        return items

    def _build_connectors_menu(self, cfg: dict) -> list[rumps.MenuItem]:
        connectors_cfg: dict = cfg.get("connectors", {})
        items = []
        for cname in self._connectors:
            conn_cfg = connectors_cfg.get(cname, {})
            enabled = conn_cfg.get("enabled", True)
            title = f"{'✓' if enabled else '○'} {cname}"
            item = rumps.MenuItem(title)
            item._connector = cname  # type: ignore[attr-defined]
            item.set_callback(self._toggle_connector)
            items.append(item)
        return items

    # ------------------------------------------------------------------ #
    # Callbacks
    # ------------------------------------------------------------------ #

    def _toggle_rule(self, sender: rumps.MenuItem) -> None:
        rule_key = getattr(sender, "_rule_key", None)
        if not rule_key:
            return
        cfg = self._load_config()
        rules = cfg.setdefault("auto_accept_rules", {})
        current = rules.get(rule_key, {})
        if isinstance(current, dict):
            current["enabled"] = not current.get("enabled", True)
            rules[rule_key] = current
        else:
            rules[rule_key] = not bool(current)
        self._save_config(cfg)
        try:
            get_auto_accept_evaluator().reload_rules()
        except Exception as exc:
            logger.warning("Rule reload failed: %s", exc)
        self._build_menu()

    def _toggle_connector(self, sender: rumps.MenuItem) -> None:
        cname = getattr(sender, "_connector", None)
        if not cname:
            return
        cfg = self._load_config()
        conn = cfg.setdefault("connectors", {}).setdefault(cname, {})
        conn["enabled"] = not conn.get("enabled", True)
        self._save_config(cfg)
        self._build_menu()

    @rumps.clicked("Open Audit Log")
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


def _find_icon() -> str | None:
    here = Path(__file__).parent / "resources"
    for name in ("icon_32.png", "icon_64.png", "icon_512.png"):
        p = here / name
        if p.exists():
            return str(p)
    return None


def run_menu_bar(config_path: str, connectors: list[str]) -> None:
    """Entry point called from the main thread."""
    app = LooplineMenuBar(config_path=config_path, connectors=connectors)
    app.run()
