"""macOS menu bar tray icon (rumps).

Issue #120 replaced the ~2000-line NSMenu tree this module used to build
(connector auth, PII/update-check toggles, the "Manage Auto-accept
Rules…"/"Privacy Filter…" native windows, org config, About) with a single
webview-based settings window (settings_window.py / settings_window_html.py)
covering the same ground. (QuickLook preview toggling, which briefly lived
here too, was dropped project-wide in favor of rendering a file's own
extracted content -- see settings_controller.py's git history -- rather than
carried over into the new window.) This module is now just the tray icon:
two items, "Settings…" (lazily creates and shows the settings
window) and "Quit PrivacyFence".

Because that menu is static and never mutated after construction, the
NSMenu-open/rebuild-crash-avoidance machinery this module used to carry
(``_MenuTrackingDelegate``, ``_menu_is_open``/``_rebuild_pending``) is gone
too -- it existed only to defer live edits to a menu that might be open on
screen, and a two-item menu that's built once has nothing to defer.

All the domain logic that used to live here (rule/grant/PII/privacy/
connector/audit/org-config mutation) now lives in settings_controller.py,
which this module's ``PrivacyFenceMenuBar`` holds one instance of for the
app's whole lifetime -- see that module's docstring for the load-config ->
mutate -> save-config -> hot-reload -> push-fresh-state shape every mutating
method there follows.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import rumps

from .settings_controller import SettingsController
from .settings_window import SettingsWindowController

if TYPE_CHECKING:
    from .ipc_server import IPCServer

logger = logging.getLogger(__name__)

# Periodic "is it time to check yet?" pulse for the update checker --
# deliberately shorter than update_checker.CHECK_INTERVAL_SECONDS (24h).
# SettingsController.on_update_check_timer() re-derives whether 24h have
# actually passed from its own on-disk timestamp, so this is robust to
# sleep/wake and doesn't need to match the real interval exactly.
UPDATE_CHECK_TIMER_INTERVAL_SECONDS = 6 * 60 * 60


def _find_icon() -> str | None:
    here = Path(__file__).parent / "resources"
    for name in ("icon_menubar.png", "icon_32.png", "icon_64.png", "icon_512.png"):
        p = here / name
        if p.exists():
            return str(p)
    return None


class PrivacyFenceMenuBar(rumps.App):
    def __init__(
        self,
        config_path: str,
        connectors: list[str],
        ipc_server: "IPCServer",
        connector_objs: list[Any] | None = None,
    ) -> None:
        self.controller = SettingsController(
            config_path=config_path,
            connectors=connectors,
            ipc_server=ipc_server,
            connector_objs=connector_objs,
        )
        # Lazily created on first "Settings…" click (see
        # _open_settings_window) -- one long-lived window reused for the
        # app's whole lifetime, same lazy-singleton pattern the pre-#120
        # _rules_manager/_privacy_manager used.
        self._settings_window: SettingsWindowController | None = None

        icon_path = _find_icon()
        super().__init__(name="PrivacyFence", icon=icon_path, quit_button=None, template=True)

        self.menu = [
            rumps.MenuItem("Settings…", callback=self._open_settings_window),
            rumps.separator,
            rumps.MenuItem("Quit PrivacyFence", callback=self._quit),
        ]

        self._update_check_timer = rumps.Timer(
            self._on_update_check_timer, UPDATE_CHECK_TIMER_INTERVAL_SECONDS
        )
        self._update_check_timer.start()
        self._on_update_check_timer()

    def _open_settings_window(self, _sender: Any = None) -> None:
        if self._settings_window is None:
            self._settings_window = SettingsWindowController.alloc().init()
            self._settings_window.configure(self.controller)
        self._settings_window.show_window()

    def _on_update_check_timer(self, _timer: Any = None) -> None:
        self.controller.on_update_check_timer()

    def _quit(self, _sender: Any = None) -> None:
        self.controller.quit_app()


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
