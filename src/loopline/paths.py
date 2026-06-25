"""Centralized path resolution for Loopline.

In development (no PyInstaller bundle): data lives in the project root.
In a bundled .app: data lives in ~/.loopline/ so it survives app updates.
"""
from __future__ import annotations

import sys
from pathlib import Path


def is_bundled() -> bool:
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def data_dir() -> Path:
    """Root directory for user data (config, credentials, logs)."""
    if is_bundled():
        d = Path.home() / ".loopline"
    else:
        d = Path(__file__).parent.parent.parent
    d.mkdir(parents=True, exist_ok=True)
    return d


def bundle_macos_dir() -> Path | None:
    """Path to Contents/MacOS inside the .app bundle, or None in dev."""
    if is_bundled():
        return Path(sys.executable).parent
    return None


def app_bundle_path() -> Path | None:
    """Path to Loopline.app itself, or None in dev."""
    if is_bundled():
        return Path(sys.executable).parent.parent.parent
    return None
