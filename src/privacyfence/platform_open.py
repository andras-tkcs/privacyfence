"""Cross-platform "open this in the default app" helper.

Three call sites (settings_window.py's "View on GitHub" bridge action,
settings_controller.py's update-checker "open release page" and "reveal
exported audit log" actions) used to shell out to macOS's ``open`` command
directly. That's the one piece of those three call sites that isn't already
portable -- everything else about them (building the URL/path, deciding
*when* to open it) stays exactly as it was; this module only replaces the
literal ``subprocess.run(["open", ...])`` call.

``os.startfile`` (Windows-only, not present in the ``os`` module on any other
platform -- referenced here only inside the ``sys.platform == "win32"``
branch, so accessing it doesn't fail module import elsewhere) and macOS's
``open`` both hand off to the OS's own file-type/URL-scheme association, so a
local directory opens in the file manager and a URL opens in the default
browser without this module needing to tell the two apart. ``webbrowser.open``
is the fallback for anything else (Linux dev/CI use) -- it shells out to
``xdg-open``/similar, which handles ``https://`` URLs fine but not arbitrary
local directories the way Finder/Explorer do; that gap doesn't matter today
since every call site here only ever passes a URL or an already-known-good
local path, never something whose openability needs to be checked first.
"""
from __future__ import annotations

import os
import subprocess
import sys
import webbrowser


def open_path_or_url(target: str) -> None:
    """Open ``target`` (a URL or a local file/directory path) in whatever the
    OS considers its default handler. Best-effort, same as the ``open``
    subprocess call this replaces -- a failure here (missing binary, no
    default app configured) is not raised, just silently a no-op, matching
    the old call sites' own ``check=False``/un-checked-return-code posture."""
    if sys.platform == "darwin":
        subprocess.run(["open", target], check=False)
    elif sys.platform == "win32":
        try:
            os.startfile(target)  # type: ignore[attr-defined] -- Windows-only, see module docstring
        except OSError:
            pass
    else:
        webbrowser.open(target)
