"""Org-mode download staging: an in-memory registry plus per-principal,
encrypted-at-rest disk storage for file bytes that are too large to return
inline in an MCP tool result. Mirrors approvals.py's ``PendingApprovalRegistry`` shape --
same TTL-sweep pattern, same "ephemeral state lost on daemon restart is
acceptable" posture (an interrupted download just means the human re-runs
the tool call) -- but this registry's payload is real file content, not a
decision, so it adds one more property approvals.py never needed:
plaintext must never touch this server's disk.

**Restart is not supposed to leak ciphertext.** The registry entry is
ephemeral by design, but the file it names on disk is not -- so a naive
restart would turn "lost the in-memory entry" into "orphaned the
ciphertext forever," since nothing else ever revisits a principal's
``downloads_dir()`` on its own. ``DownloadStagingStore.__init__`` closes
that gap: a fresh instance has no ``self._pending`` entries yet, so any
file already sitting in any principal's ``downloads_dir()`` at
construction time cannot correspond to one -- it is unclaimable dead
weight from a previous process life -- and gets deleted immediately,
before the first ``stage()`` call could possibly have written anything.

**Encryption at rest.** ``stage()`` generates a fresh 256-bit ``token``
(``secrets.token_bytes(32)``) and returns it to the caller -- that return
value is the *only* copy of the token that ever exists. The registry keys
itself and the on-disk ciphertext filename by ``lookup_id =
sha256(token)``, and derives the AES-256-GCM key via HKDF-SHA256 from the
token itself. Neither the token nor the derived key is ever written to
disk, logged, or kept anywhere in this process after ``stage()`` returns
except inside the caller's own return value (which flows into a tool-call
result and, from there, into the human's one-time download URL). A
snapshot of this server's disk, a backup, or a forensic recovery of a
"deleted" file therefore all yield AES-GCM ciphertext, never plaintext --
with one explicit caveat: a live compromise
of this daemon process itself, which necessarily holds plaintext briefly
around encrypt/decrypt, same as any encryption-at-rest scheme.

``claim()`` is single-use: a successful claim deletes both the ciphertext
file and the registry entry before returning, so nothing meant to be
downloaded once still exists on the server afterward. There is
deliberately no oracle distinguishing "wrong token" from "expired token"
from "already claimed" -- all three return ``None``.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import paths
from .secure_files import atomic_write_bytes

if TYPE_CHECKING:
    from .principal import Principal

logger = logging.getLogger(__name__)

# Default -- 5 minutes, shorter
# than an earlier draft's 15: staging means an encrypted-but-real copy of
# the file exists on the server's disk for this long, so the window is set
# by how fast a human can open the link, not by generous UX slack.
DEFAULT_TTL_SECONDS = 300.0

# AES-256-GCM: a 12-byte (96-bit) nonce is the standard size for this
# construction (a longer one is accepted but requires an extra internal
# hash step for no benefit here). Fresh and random per file -- not secret,
# so it's stored right alongside the ciphertext it belongs to rather than
# needing its own protection.
_NONCE_LEN = 12
_HKDF_INFO = b"privacyfence-download-staging-v1"
_KEY_LEN = 32  # AES-256


def _derive_key(token: bytes) -> bytes:
    """HKDF-SHA256 over the raw token -- the only key material this module
    ever produces, and only ever in memory. See module docstring."""
    return HKDF(algorithm=hashes.SHA256(), length=_KEY_LEN, salt=None, info=_HKDF_INFO).derive(token)


def _lookup_id(token: bytes) -> str:
    return hashlib.sha256(token).hexdigest()


@dataclass
class StagedDownload:
    """Deliberately carries no key material anywhere -- see module
    docstring. ``path`` points at the on-disk ciphertext; ``name``/
    ``mime_type`` are needed for the claim route's ``Content-Disposition``
    header and aren't sensitive on their own (Drive/Gmail/Confluence
    filenames are already visible in whatever preview the human approved
    before this file was ever staged)."""

    lookup_id: str
    principal_id: str
    path: Path
    name: str
    size_bytes: int
    mime_type: str
    created_at: float
    expires_at: float
    claimed_at: float | None = None


class DownloadStagingStore:
    """See module docstring. Every public method is safe to call from any
    thread; internal state is protected by one ``threading.Lock`` (cheap,
    dict-sized critical sections only -- the actual file I/O and AES-GCM
    work happen outside the lock)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, StagedDownload] = {}
        self._sweep_orphaned_from_previous_process()

    def _sweep_orphaned_from_previous_process(self) -> None:
        """Called once, from ``__init__``, before this instance's lock or
        ``self._pending`` are ever touched by a caller. See the module
        docstring's "Restart is not supposed to leak ciphertext" section:
        every file this finds is unclaimable by construction, so it is
        deleted outright rather than TTL-checked -- there is no persisted
        ``expires_at`` to check it against, and every legitimate write goes
        through ``stage()``, which cannot have run yet."""
        for directory in paths.all_downloads_dirs():
            try:
                entries = list(directory.iterdir())
            except OSError as exc:
                logger.warning("download_staging: could not scan %s for startup cleanup: %s", directory, exc)
                continue
            for entry in entries:
                if not entry.is_file():
                    continue
                try:
                    entry.unlink()
                except OSError as exc:
                    logger.warning("download_staging: could not remove orphaned ciphertext %s: %s", entry, exc)
                else:
                    logger.info("download_staging: removed ciphertext orphaned by a previous process: %s", entry)

    # ------------------------------------------------------------------ #
    # Stage
    # ------------------------------------------------------------------ #

    def stage(
        self,
        principal: "Principal",
        data: bytes,
        name: str,
        mime_type: str,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> bytes:
        """Encrypts ``data`` and writes it under this principal's own
        ``paths.downloads_dir()``. Returns the raw 256-bit token the caller
        must turn into a download URL (base64url-encode it) -- the
        registry itself never stores this value, only its hash (see
        module docstring).

        ``name`` is sanitized the same ``os.path.basename(...) or "file"``
        way as every other download/attachment path in this codebase
        (docs/coding-and-testing-guidelines.md §1.6) before being stored;
        it is never used to construct the on-disk filename (that's always
        ``lookup_id``, which carries no plaintext name).
        """
        token = secrets.token_bytes(32)
        lookup_id = _lookup_id(token)
        key = _derive_key(token)
        nonce = secrets.token_bytes(_NONCE_LEN)
        ciphertext = AESGCM(key).encrypt(nonce, data, None)

        safe_name = os.path.basename(name) or "file"
        target = paths.downloads_dir(principal) / lookup_id
        atomic_write_bytes(target, nonce + ciphertext)

        now = time.time()
        staged = StagedDownload(
            lookup_id=lookup_id,
            principal_id=principal.id,
            path=target,
            name=safe_name,
            size_bytes=len(data),
            mime_type=mime_type or "application/octet-stream",
            created_at=now,
            expires_at=now + ttl_seconds,
        )
        with self._lock:
            self._sweep_expired_locked()
            self._pending[lookup_id] = staged
        logger.info(
            "download_staging: staged %r (%d bytes) for principal=%s, expires in %.0fs",
            safe_name, len(data), principal.id, ttl_seconds,
        )
        return token

    # ------------------------------------------------------------------ #
    # Claim
    # ------------------------------------------------------------------ #

    def claim(self, token: bytes, principal_id: str) -> tuple[bytes, str, str] | None:
        """Returns ``(plaintext, name, mime_type)`` on a successful,
        single-use claim, or ``None`` for a missing, expired, wrong-
        principal, or already-claimed token -- deliberately the same
        return for all four (see module docstring: no oracle distinguishes
        them). On success, both the ciphertext file and the registry entry
        are deleted before this returns.
        """
        lookup_id = _lookup_id(token)
        with self._lock:
            self._sweep_expired_locked()
            staged = self._pending.get(lookup_id)
            if staged is None or staged.principal_id != principal_id:
                return None
            # Claimed exactly once -- pop now, under the lock, so a second
            # concurrent claim (a retried request, a guessed token racing
            # the real one) can never see this entry again even before the
            # decrypt below finishes.
            del self._pending[lookup_id]

        try:
            raw = staged.path.read_bytes()
        except OSError as exc:
            logger.warning("download_staging: ciphertext missing for a live entry (%s): %s", lookup_id, exc)
            return None
        finally:
            try:
                staged.path.unlink(missing_ok=True)
            except OSError:
                pass

        nonce, ciphertext = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
        key = _derive_key(token)
        try:
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
        except Exception:
            # Wrong key (shouldn't happen -- the token that unlocked the
            # lookup above is the same one the key is derived from) or
            # corrupted ciphertext. Fail closed, same as every other branch
            # of this method.
            logger.warning("download_staging: AES-GCM decrypt failed for %s", lookup_id)
            return None
        return plaintext, staged.name, staged.mime_type

    # ------------------------------------------------------------------ #
    # Expiry -- opportunistic, mirroring approvals.PendingApprovalRegistry's
    # own _expire_stale_locked pattern (called at the top of stage()/
    # claim(), not on a background timer).
    # ------------------------------------------------------------------ #

    def _sweep_expired_locked(self) -> None:
        """Must be called with self._lock held. Deletes both the registry
        entry and the orphaned ciphertext file for anything past
        ``expires_at`` that was never claimed."""
        now = time.time()
        expired = [sd for sd in self._pending.values() if now > sd.expires_at]
        for staged in expired:
            del self._pending[staged.lookup_id]
            try:
                staged.path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("download_staging: could not remove expired ciphertext %s: %s", staged.path, exc)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)


_INSTANCE: DownloadStagingStore | None = None


def get_download_staging_store() -> DownloadStagingStore:
    """Lazily-constructed process-wide singleton -- one registry instance
    serves every principal (their own ``principal_id`` is stamped on each
    ``StagedDownload``), same posture as approvals.PendingApprovalRegistry.
    tests/conftest.py's autouse fixture resets this to ``None`` between
    tests."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = DownloadStagingStore()
    return _INSTANCE


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "DownloadStagingStore",
    "StagedDownload",
    "get_download_staging_store",
]
