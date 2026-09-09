"""Shared atomic-write and permission helpers for credential/config storage
(SEC-09, docs/security-remediation-plan.md Phase 1 item 1.4).

Before this module existed, every credential/token/config writer in this
codebase followed the same pattern: truncate-and-write the destination file
directly, then ``chmod`` it to ``0600`` *after* the write completed -- a
reader (or a crash, or a concurrent daemon instance) between those two steps
could observe a partially-written file at whatever permissions the process
umask left it with, and a failed ``chmod`` was silently swallowed at
``debug`` level rather than surfaced. The directories holding these files
(``~/.privacyfence`` and its subdirectories) were created with the process's
default umask rather than deliberately restricted, too -- see
``docs/security-and-compliance.md``'s "Storage format and permissions"
section for the threat this closes.

``atomic_write_text``/``atomic_write_json`` fix the file-write half: the
real content is written to a freshly ``O_CREAT|O_EXCL``-created sibling temp
file (in the same directory, so the final ``os.replace`` is atomic -- a
rename across filesystems isn't) with the destination's final permissions
from the instant the file exists, then swapped into place. A reader can only
ever see the old complete file or the new complete file, never a partial
one. ``secure_mkdir`` fixes the directory half: it creates -- or re-tightens
-- a directory to ``0700`` rather than trusting the umask, including for a
directory that already existed (e.g. one created by a pre-SEC-09 install).

A permissions failure that used to be logged at ``debug`` (effectively
invisible) is now a ``warning`` in both helpers below, per SEC-09.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import stat
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

DEFAULT_DIR_MODE = 0o700
DEFAULT_FILE_MODE = 0o600


def secure_mkdir(path: Path | str, mode: int = DEFAULT_DIR_MODE) -> Path:
    """Create ``path`` (and any missing parents) if needed, then force its
    own permissions to ``mode``.

    Unlike ``Path.mkdir(mode=...)``, this is applied even when ``path``
    already existed -- e.g. a directory created by a pre-SEC-09 install
    under the process's default umask -- and isn't itself subject to
    umask. Parent directories created along the way (``parents=True``)
    keep whatever the umask leaves them with; only the leaf directory this
    call names is treated as a security boundary, since that's the one
    callers actually store sensitive files directly under.

    A ``chmod`` failure (e.g. a filesystem that doesn't support POSIX
    permissions) is logged at ``warning`` and otherwise non-fatal -- the
    directory is still created and usable, just not provably restricted.
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(mode)
    except OSError as exc:  # pragma: no cover -- best effort on non-POSIX
        logger.warning("Could not set permissions %04o on directory %s: %s", mode, path, exc)
    return path


def atomic_write_bytes(path: Path | str, data: bytes, *, mode: int = DEFAULT_FILE_MODE) -> None:
    """Write ``data`` to ``path`` atomically and with ``mode`` permissions
    from the moment the file exists -- see module docstring.

    The containing directory is created via ``secure_mkdir`` if it doesn't
    exist yet, so callers no longer need their own
    ``os.makedirs(..., exist_ok=True)`` before calling this.
    """
    path = Path(path)
    secure_mkdir(path.parent)
    tmp_path = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        # Belt-and-suspenders: os.open's own mode argument already applies
        # `mode` (minus whatever the umask clears, which for a 0600/0700-
        # style request is nothing -- umask only ever clears group/other
        # bits that such a request doesn't set in the first place). This
        # explicit chmod exists so a permissions problem is surfaced at
        # warning rather than silently trusted, per SEC-09.
        try:
            os.chmod(tmp_path, mode)
        except OSError as exc:  # pragma: no cover -- best effort on non-POSIX
            logger.warning("Could not set permissions %04o on %s: %s", mode, path, exc)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_text(
    path: Path | str, text: str, *, mode: int = DEFAULT_FILE_MODE, encoding: str = "utf-8",
) -> None:
    atomic_write_bytes(path, text.encode(encoding), mode=mode)


def atomic_write_json(path: Path | str, data: Any, *, mode: int = DEFAULT_FILE_MODE, **json_kwargs: Any) -> None:
    """``json_kwargs`` forwards to ``json.dumps`` (e.g. ``indent=2,
    sort_keys=True``) so callers that want pretty-printed/deterministic
    output keep that formatting."""
    atomic_write_text(path, json.dumps(data, **json_kwargs), mode=mode)


def _grants_group_or_other_access(st_mode: int) -> bool:
    return bool(st_mode & (stat.S_IRWXG | stat.S_IRWXO))


def audit_directory_permissions(directories: Iterable[Path | str], mode: int = DEFAULT_DIR_MODE) -> list[str]:
    """Return one human-readable warning per directory in ``directories``
    that exists on disk and grants group- or other-access of any kind
    (read, write, or execute) -- i.e. isn't at least as restrictive as
    ``mode`` (``0700`` by default). A directory that doesn't exist yet
    (nothing to warn about -- ``secure_mkdir`` will create it correctly)
    or can't be ``stat``'d (best effort, same posture as the ``chmod``
    calls above) is silently skipped.

    A pure function over paths -- callers decide what to do with the
    result (log-only, or fail closed in org mode -- see daemon_main.py's
    ``check_storage_permissions``), and it's usable directly from a unit
    test without touching real global state.
    """
    problems: list[str] = []
    for directory in directories:
        directory = Path(directory)
        try:
            st = directory.stat()
        except OSError:
            continue
        if not stat.S_ISDIR(st.st_mode):
            continue
        if _grants_group_or_other_access(st.st_mode):
            problems.append(
                f"{directory} is readable, writable, or executable by group or other "
                f"(mode {stat.S_IMODE(st.st_mode):04o}, expected {mode:04o} or stricter) -- "
                "another local account on this machine may be able to read credentials "
                "stored under it."
            )
    return problems


class InsecurePermissionsError(RuntimeError):
    """Raised by daemon startup (org mode only) when a data directory's
    on-disk permissions are broader than SEC-09 requires -- see
    daemon_main.py's ``check_storage_permissions``. Local mode logs the
    same finding as a warning instead of refusing to start."""
