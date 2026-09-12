"""Centralized path resolution for PrivacyFence.

In a source checkout (editable dev install, or no install at all): data
lives in the project root. In a bundled .app, or a real (non-editable)
``pip``/``pipx install privacyfence``: data lives in ~/.privacyfence/ so it
survives app updates/reinstalls -- see is_bundled() and
_is_installed_package().
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .secure_files import secure_mkdir

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


def safe_principal_id(raw: str) -> str:
    """``raw`` unchanged if it's already filesystem-safe, otherwise a
    stable hash of it (P7: an OIDC ``sub`` claim is opaque per spec and may
    contain characters ``_is_safe_principal_id`` rejects -- hashing keeps
    org_identity.py's ``principal_from_claims`` always able to produce a
    ``Principal`` rather than letting a login fail on an oddly-formatted
    but legitimate subject). Deterministic, so the same IdP subject always
    maps to the same storage directory across logins."""
    if _is_safe_principal_id(raw):
        return raw
    return "idp-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def is_bundled() -> bool:
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def _is_installed_package() -> bool:
    """True for a normal (non-editable) ``pip``/``pipx install privacyfence``
    -- i.e. this file living under some ``site-packages``/``dist-packages``
    -- as opposed to a source checkout, editable dev install included: an
    editable install (``pip install -e .``, what every documented dev/source
    setup in this repo uses) keeps the real ``.py`` files at their original
    checkout location, so ``__file__`` still resolves under the repo root
    exactly as it does with no install step run at all. Checking the path
    rather than "is this package installed" is what makes that distinction
    -- ``importlib.metadata`` reports a distribution as installed either
    way. Without this, ``data_dir()`` would fall to its ``else`` branch for
    a real PyPI install too, landing config/credentials/logs somewhere
    inside site-packages instead of a real per-user data directory."""
    return "site-packages" in Path(__file__).resolve().parts or "dist-packages" in Path(__file__).resolve().parts


def data_dir() -> Path:
    """Root directory for org-wide/install-wide data (org config, the local
    web/MCP tokens, the instance lock) -- see user_dir() for a specific
    principal's own storage root, which is what most callers actually want
    for anything that's per-user data.

    Created (or re-tightened, on an upgrade from a pre-SEC-09 install) to
    ``0700`` via ``secure_mkdir`` -- see that function's own docstring and
    ``docs/security-and-compliance.md``'s "Storage format and permissions"
    section for what this closes.
    """
    if is_bundled() or _is_installed_package():
        d = Path.home() / ".privacyfence"
    else:
        d = Path(__file__).parent.parent.parent
    return secure_mkdir(d)


def org_dir() -> Path:
    """Directory holding the installed organization config bundle."""
    return secure_mkdir(data_dir() / "org")


def user_dir(principal: "Principal | None" = None) -> Path:
    """Per-principal storage root (P6): ``config/settings.yaml``,
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
    return secure_mkdir(data_dir() / "users" / principal.id)


def downloads_dir(principal: "Principal | None" = None) -> Path:
    """Per-principal staging area for org-mode download delivery:
    ``user_dir(principal) / "downloads"``, created on demand exactly like
    ``org_dir()``. Holds only
    AES-256-GCM-encrypted ciphertext (download_staging.py's own
    ``DownloadStagingStore`` never derives or stores the decryption key
    anywhere on disk -- see that module's docstring), so this directory's
    contents are worthless without the one-time token that produced them.
    Reuses ``user_dir()``'s own directory-safety logic (``_is_safe_
    principal_id``) rather than adding any new path-construction code
    here."""
    return secure_mkdir(user_dir(principal) / "downloads")


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
