"""Cross-process atomicity of secure_files.atomic_write_bytes() (SEC-09).

docs/automated-test-strategy-plan.md Phase 2.3 -- "file handling." The
existing coverage of secure_files.py (tests/unit/test_secure_files.py)
proves atomic_write_bytes() writes to a sibling temp file and os.replace()s
it into place, and that the destination's permissions/content are correct
once the call returns -- but every one of those tests is single-process:
nothing in this repo's suite has ever actually raced two independent
writers against the *same* destination path the way two PrivacyFence
components genuinely can (e.g. a connector's per-tool call refreshing a
cached OAuth token file while a concurrent call for the same connector does
the same, or two just-started daemon instances losing the instance-lock
race but still briefly touching the same config file before the loser
notices). A reader between those writers' truncate-and-write steps would
see a torn (partially-old, partially-new) file if this module's
"O_CREAT|O_EXCL sibling temp file + os.replace()" design didn't actually
deliver what its own docstring promises -- this is a real OS-level
guarantee (rename is atomic on a single filesystem, per every platform this
project supports), not just an artifact of Python's GIL serializing calls
within one process, so it can only be proven by two genuinely separate OS
processes.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.platform

# Distinct, uniform, repeated-byte payloads -- a torn read (part one
# writer's content, part the other's) is trivially detectable: a well-formed
# read is always entirely b"A"*_PAYLOAD_SIZE or entirely b"B"*_PAYLOAD_SIZE,
# never a mix, and never any other length.
_PAYLOAD_SIZE = 500_000
_WRITE_ROUNDS = 40

_WRITER_SCRIPT = """
import sys
from privacyfence.secure_files import atomic_write_bytes

path, marker, size, rounds = sys.argv[1], sys.argv[2].encode(), int(sys.argv[3]), int(sys.argv[4])
payload = marker * size
for _ in range(rounds):
    atomic_write_bytes(path, payload)
"""


def _spawn_writer(target: Path, marker: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", _WRITER_SCRIPT, str(target), marker, str(_PAYLOAD_SIZE), str(_WRITE_ROUNDS)],
    )


def test_concurrent_writers_from_separate_processes_never_produce_a_torn_read(tmp_path):
    target = tmp_path / "shared.bin"
    writers = [_spawn_writer(target, "A"), _spawn_writer(target, "B")]

    reads_seen: set[bytes] = set()
    stop = threading.Event()

    def _reader_loop() -> None:
        while not stop.is_set():
            try:
                content = target.read_bytes()
            except FileNotFoundError:
                continue  # not written yet -- fine, not a torn read
            # A well-formed read is uniformly one marker byte, repeated
            # exactly _PAYLOAD_SIZE times -- anything else means this read
            # landed mid-write, which atomic_write_bytes() must never allow.
            assert len(content) == _PAYLOAD_SIZE, f"torn read: wrong length {len(content)}"
            assert content == content[:1] * _PAYLOAD_SIZE, "torn read: mixed content within one read"
            reads_seen.add(content[:1])

    reader = threading.Thread(target=_reader_loop, name="atomic-write-reader")
    reader.start()
    try:
        for proc in writers:
            assert proc.wait(timeout=60) == 0
    finally:
        stop.set()
        reader.join(timeout=10)

    # Not a hard assertion (thread/process scheduling isn't guaranteed to
    # interleave the two writers within the reader's own poll rate) -- but
    # printed so a suspiciously-quiet run (the race never actually
    # happened, so this test proved nothing) is visible in -v output rather
    # than silently passing either way.
    print(f"atomic-write race: reader observed {len(reads_seen)} distinct writer(s)")
    final = target.read_bytes()
    assert final == final[:1] * _PAYLOAD_SIZE


def test_stray_temp_files_are_not_left_behind(tmp_path):
    target = tmp_path / "shared.bin"
    for proc in (_spawn_writer(target, "A"), _spawn_writer(target, "B")):
        assert proc.wait(timeout=60) == 0

    leftovers = [p for p in tmp_path.iterdir() if p.name != target.name]
    assert leftovers == [], f"atomic_write_bytes left stray temp file(s) behind: {leftovers}"
