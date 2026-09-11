"""daemon_main's single-instance lock, proven against a genuinely separate
OS process (docs/automated-test-strategy-plan.md Phase 2.3 -- "single-
instance locking").

tests/unit/test_daemon_main.py's own TestInstanceLock already proves the
lock is OS-level, not just in-process bookkeeping
(``test_second_acquire_is_rejected_at_the_os_level_not_just_in_process``),
but does so with a second file descriptor opened by *this same process* --
which is enough to prove portalocker.lock() takes a real kernel-level lock,
but not that the lock actually survives and enforces across a process
boundary the way a second, wholly independent ``privacyfence-app`` launch
genuinely would (a different PID, its own Python interpreter, no shared
memory with the first). That's what this module adds: a real
``subprocess.Popen`` holds the lock while this test process tries (and
must fail) to acquire it, then either releases explicitly or is killed
outright -- both of which must free the lock for the next acquirer, since a
real second daemon launch after a crash needs exactly that.
"""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

from privacyfence import daemon_main

pytestmark = pytest.mark.platform

# Blocks on stdin after acquiring, so the parent test controls exactly when
# it lets go (rather than racing a fixed sleep) -- printed feedback (flushed
# immediately) lets the parent wait for "the lock is really held" instead of
# just "the process has started."
_HOLDER_SCRIPT = """
import sys
from privacyfence import daemon_main

daemon_main.LOCK_FILE = sys.argv[1]
print("ACQUIRED" if daemon_main._acquire_instance_lock() else "DENIED", flush=True)
sys.stdin.readline()  # blocks until the parent test tells it to let go
daemon_main._release_instance_lock()
print("RELEASED", flush=True)
"""


@pytest.fixture(autouse=True)
def _reset_lock_state(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_main, "LOCK_FILE", str(tmp_path / "privacyfence.lock"))
    daemon_main._lock_fd = None
    yield
    daemon_main._release_instance_lock()


def _spawn_holder(lock_file: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SCRIPT, lock_file],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )


def _acquire_eventually(timeout: float = 5.0) -> bool:
    """``proc.wait()`` returning already guarantees the kernel has reclaimed
    the dead process's file descriptors (and with them, its lock) -- this
    retry loop exists only as a small safety margin against any platform-
    specific teardown latency, not because that guarantee is in doubt."""
    deadline = time.monotonic() + timeout
    while True:
        if daemon_main._acquire_instance_lock():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def test_lock_blocks_a_separate_process_and_frees_on_clean_release(tmp_path):
    lock_file = str(tmp_path / "privacyfence.lock")
    holder = _spawn_holder(lock_file)
    try:
        assert holder.stdout.readline().strip() == "ACQUIRED"

        # A real, wholly independent second process -- not a second fd this
        # same interpreter opened -- is denied while the holder is alive.
        assert daemon_main._acquire_instance_lock() is False

        holder.stdin.write("release\n")
        holder.stdin.flush()
        assert holder.stdout.readline().strip() == "RELEASED"
        assert holder.wait(timeout=10) == 0

        # Freed now that the holder released and exited cleanly.
        assert _acquire_eventually()
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)


def test_lock_frees_when_the_holding_process_is_killed_without_releasing(tmp_path):
    """A crashed/force-killed daemon (no chance to run its own ``finally:
    _release_instance_lock()``) must not permanently wedge the lock -- the
    OS itself releases an advisory/mandatory file lock when the holding
    process's file descriptors close on exit, regardless of how it exited.
    This is exactly what lets a fresh ``privacyfence-app`` launch recover
    after a previous instance was killed rather than shut down cleanly."""
    lock_file = str(tmp_path / "privacyfence.lock")
    holder = _spawn_holder(lock_file)
    try:
        assert holder.stdout.readline().strip() == "ACQUIRED"
        assert daemon_main._acquire_instance_lock() is False

        holder.kill()
        assert holder.wait(timeout=10) is not None

        assert _acquire_eventually()
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)
