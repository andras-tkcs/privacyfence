"""Centralized path resolution for PrivacyFence.

In development (no PyInstaller bundle): data lives in the project root.
In a bundled .app: data lives in ~/.privacyfence/ so it survives app updates.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .principal import Principal

# Deliberately strict -- principal ids reach here from an OAuth 2.1/OIDC
# `sub` claim once P7 lands (today it's always "local"), and this is the one
# place that string becomes a filesystem path component. Anything outside
# this set (a "/", a leading "." that could hide a directory, ...) is
# rejected rather than sanitized, so a hostile or malformed id fails loudly
# instead of silently resolving somewhere unintended. The character class
# alone would still accept "." and ".." (both are made entirely of allowed
# characters) -- _is_safe_principal_id() below rejects those two literally,
# since they're path-traversal components in their own right, not just via
# an excluded character.
_SAFE_PRINCIPAL_ID = re.compile(r"^[A-Za-z0-9._@-]{1,200}$")


def _is_safe_principal_id(principal_id: str) -> bool:
    return principal_id not in (".", "..") and bool(_SAFE_PRINCIPAL_ID.match(principal_id))


def is_bundled() -> bool:
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def data_dir() -> Path:
    """Root directory for org-wide/install-wide data (org config, the local
    web/MCP tokens, the instance lock) -- see user_dir() for a specific
    principal's own storage root, which is what most callers actually want
    for anything that's per-user data.
    """
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


def user_dir(principal: "Principal | None" = None) -> Path:
    """Per-principal storage root (P6, docs/https-connector-refactor-plan.md
    §9.2's storage layout table): ``config/settings.yaml``,
    ``credentials/*``, ``logs/audit/*`` and the various per-connector cache
    files all live under here.

    The ``local`` principal's root *is* ``data_dir()`` itself -- not a
    ``users/local/`` subdirectory -- so an existing single-user install
    needs no migration and every path it already has on disk keeps working
    unchanged. Any other principal gets ``data_dir()/users/<id>/``, created
    on demand.

    ``principal`` defaults to ``current_principal()`` -- imported lazily to
    avoid a circular import (principal.py doesn't need paths.py, but nearly
    everything paths.py's callers do need principal.py transitively, so
    importing it at module load time here would risk one on some import
    orders).
    """
    from .principal import LOCAL_PRINCIPAL_ID, current_principal

    if principal is None:
        principal = current_principal()
    if principal.id == LOCAL_PRINCIPAL_ID:
        return data_dir()
    if not _is_safe_principal_id(principal.id):
        raise ValueError(f"Unsafe principal id for filesystem storage: {principal.id!r}")
    d = data_dir() / "users" / principal.id
    d.mkdir(parents=True, exist_ok=True)
    return d


def bundle_macos_dir() -> Path | None:
    """Path to Contents/MacOS inside the .app bundle, or None in dev."""
    if is_bundled():
        return Path(sys.executable).parent
    return None


def app_bundle_path() -> Path | None:
    """Path to PrivacyFenceApp.app itself, or None in dev."""
    if is_bundled():
        return Path(sys.executable).parent.parent.parent
    return None
