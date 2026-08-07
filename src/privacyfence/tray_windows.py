"""Windows tray icon (pystray), issue #121.

Windows equivalent of menu_bar.py's rumps-based PrivacyFenceMenuBar: same
two items ("Settings…"/"Quit PrivacyFence"), same
``run_menu_bar(config_path, connectors, ipc_server, connector_objs=None)``
entry point daemon_main.py's platform-dispatched import picks between (see
that module's own run_app()), same construction -- one SettingsController
held for the app's whole lifetime, one lazily-created
SettingsWindowController.

One process, two GUI frameworks: pystray's tray icon and pywebview's
approval/settings windows (approval_window_windows.py, dialog_window_
windows.py, settings_window_windows.py) are two separate event loops that
can't both own the main thread at once. This module runs the tray on its
own thread via pystray's ``Icon.run_detached()`` (supported on pystray's
win32 backend) and gives pywebview's ``webview.start()`` the main thread --
the standard documented combination for a pystray+pywebview app, and
pywebview's own main-loop requirement (window creation/JS evaluation calls
made from any *other* thread, e.g. gate.py's approval popups or this
module's own tray callbacks, are what pywebview documents as safe to do
once ``start()`` is already running -- see approval_window_windows.py's own
module docstring for where that matters most). This specific combination is
flagged in docs/windows-port-status.md as the piece of this port most worth
a real look on an actual Windows machine before release -- it can't be
exercised end-to-end from this project's own (Linux) CI/dev sandbox.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .settings_controller import SettingsController
from .settings_window_windows import SettingsWindowController

try:
    import pystray
    from PIL import Image
except ImportError:  # pragma: no cover - exercised only where pystray/Pillow are present (Windows)
    pystray = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]

try:
    import webview  # pywebview
except ImportError:  # pragma: no cover - exercised only where pywebview is present (Windows)
    webview = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _find_icon() -> str | None:
    here = Path(__file__).parent / "resources"
    for name in ("icon_menubar.png", "icon_32.png", "icon_64.png", "icon_512.png"):
        p = here / name
        if p.exists():
            return str(p)
    return None


class PrivacyFenceTray:
    def __init__(
        self,
        config_path: str,
        connectors: list[str],
        ipc_server: Any,
        connector_objs: list[Any] | None = None,
    ) -> None:
        self.controller = SettingsController(
            config_path=config_path,
            connectors=connectors,
            ipc_server=ipc_server,
            connector_objs=connector_objs,
        )
        # Lazily created on first "Settings…" click, same reasoning as
        # menu_bar.py's own _settings_window -- one long-lived window reused
        # for the app's whole lifetime.
        self._settings_window: SettingsWindowController | None = None
        self._icon: Any = None

    def _open_settings_window(self, _icon: Any = None, _item: Any = None) -> None:
        if self._settings_window is None:
            self._settings_window = SettingsWindowController()
            self._settings_window.configure(self.controller)
        self._settings_window.show_window()

    def _quit(self, _icon: Any = None, _item: Any = None) -> None:
        self.controller.quit_app()
        if self._icon is not None:
            self._icon.stop()
        if webview is not None:
            webview.destroy_window()

    def _build_icon(self) -> Any:
        icon_path = _find_icon()
        image = Image.open(icon_path) if icon_path else Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        menu = pystray.Menu(
            pystray.MenuItem("Settings…", self._open_settings_window),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit PrivacyFence", self._quit),
        )
        return pystray.Icon("PrivacyFence", image, "PrivacyFence", menu)

    def run(self) -> None:
        if pystray is None or webview is None:
            logger.error(
                "pystray and pywebview are required on Windows but are not installed; "
                "the tray cannot start."
            )
            return
        self._icon = self._build_icon()
        # run_detached() -- the tray runs on its own thread rather than
        # blocking this one, so webview.start() below can own the main
        # thread instead (see module docstring for why that split, not the
        # reverse, is the one pystray's own docs/examples recommend when
        # combined with a second GUI framework).
        self._icon.run_detached()
        webview.start()


def run_menu_bar(
    config_path: str,
    connectors: list[str],
    ipc_server: Any,
    connector_objs: list[Any] | None = None,
) -> None:
    tray = PrivacyFenceTray(
        config_path=config_path,
        connectors=connectors,
        ipc_server=ipc_server,
        connector_objs=connector_objs,
    )
    tray.run()
