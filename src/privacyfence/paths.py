"""Centralized path resolution for PrivacyFence.

In development (no PyInstaller bundle): data lives in the project root.
In a bundled .app: data lives in ~/.privacyfence/ so it survives app updates.
"""
from __future__ import annotations

import sys
from pathlib import Path


def is_bundled() -> bool:
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def data_dir() -> Path:
    """Root directory for user data (config, credentials, logs)."""
    if is_bundled():
        d = Path.home() / ".privacyfence"
    else:
        d = Path(__file__).parent.parent.parent
    d.mkdir(parents=True, exist_ok=True)
    return d


def org_dir() -> Path:
    """Directory holding the installed organization config bundle."""
    d = data_dir() / "org"
    d.mkdir(parents=True, exist_ok=True)
    return d


def bundle_macos_dir() -> Path | None:
    """Path to Contents/MacOS inside the .app bundle, or None in dev or on a
    non-macOS bundle. A PyInstaller Windows onedir build has no
    Contents/MacOS-style layout at all (the exe just sits in its own dist
    directory) -- see bundle_dir() for the cross-platform equivalent
    ("whatever directory the frozen executable itself lives in"), which is
    what a Windows caller wants instead."""
    if is_bundled() and sys.platform == "darwin":
        return Path(sys.executable).parent
    return None


def app_bundle_path() -> Path | None:
    """Path to PrivacyFenceApp.app itself, or None in dev or on a non-macOS
    bundle. Same darwin-only reasoning as bundle_macos_dir() -- the three
    levels this walks up (Contents/MacOS/exe -> Contents/MacOS ->
    Contents -> *.app) only mean anything inside an actual .app bundle."""
    if is_bundled() and sys.platform == "darwin":
        return Path(sys.executable).parent.parent.parent
    return None


def bundle_dir() -> Path | None:
    """Cross-platform "root of the frozen build" -- the directory the
    executable itself lives in, or None in dev. On macOS this is the same
    directory bundle_macos_dir() returns (Contents/MacOS); on Windows, a
    PyInstaller onedir build has no bundle layout to walk up out of at all,
    so this is simply where privacyfence-app.exe sits (alongside its own
    _internal/ support directory)."""
    if is_bundled():
        return Path(sys.executable).parent
    return None
