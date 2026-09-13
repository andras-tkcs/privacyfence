"""The windowed-Windows-build std-streams guard (``std_streams.ensure_std_streams``).

This is the regression test for the defect that kept Windows autostart from
ever working: a windowed PyInstaller executable started with no console has
no standard handles, so CPython sets ``sys.stdout``/``sys.stderr`` to
``None``, and uvicorn's default log formatter calls ``sys.stdout.isatty()``
while ``uvicorn.Config(...)`` is being built. The daemon exited 1 before
binding its port -- with Task Scheduler having started it exactly as
designed.

The interesting half runs in a **subprocess**, deliberately. A test process
cannot convincingly pretend to have no standard streams: pytest's own output
capture owns ``sys.stdout`` and reinstates it between the setup and call
phases, so a fixture that sets it to ``None`` is silently undone before the
test body runs (and code that then "restores" what it finds there closes
pytest's capture file instead, which fails the run in a confusing way).
A child interpreter has no such stake in its own streams, so the probe below
reproduces the real startup environment exactly, and exercises the real
failing call -- uvicorn's own formatter -- rather than the guard's
bookkeeping.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from privacyfence.std_streams import ensure_std_streams

# Runs with `python -c`, so sys.argv[1] is the marker path. Everything it
# learns goes into that file: with the streams gone, the child has nowhere
# else to say anything.
_PROBE = """
import sys

sys.stdout = None
sys.stderr = None
sys.__stdout__ = None
sys.__stderr__ = None

from uvicorn.logging import DefaultFormatter

marker = open(sys.argv[1], "w", encoding="utf-8")

try:
    DefaultFormatter(use_colors=None)
    marker.write("without-guard: built\\n")
except AttributeError as exc:
    marker.write("without-guard: %s\\n" % exc)

from privacyfence.std_streams import ensure_std_streams

ensure_std_streams()

assert sys.stdout is not None and sys.stderr is not None
assert sys.__stdout__ is not None and sys.__stderr__ is not None
DefaultFormatter(use_colors=None)
print("a library that prints must not raise either")
sys.stderr.write("nor one that writes to stderr\\n")
marker.write("with-guard: ok\\n")
marker.close()
"""


def test_daemon_starts_with_no_std_streams_at_all(tmp_path: Path):
    marker = tmp_path / "probe.txt"
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, str(marker)],
        capture_output=True, text=True, timeout=120,
    )
    report = marker.read_text(encoding="utf-8") if marker.exists() else "(no marker written)"
    assert result.returncode == 0, (
        f"a process with no std streams could not start (exit {result.returncode}):\n"
        f"{result.stdout}{result.stderr}\n---- probe ----\n{report}"
    )
    assert "with-guard: ok" in report, report
    # Not asserted, only recorded: whether uvicorn itself still crashes
    # without the guard. The guard exists for any library that probes the
    # streams, not for one particular version of one of them, so a future
    # uvicorn hardening its own formatter must not fail this test.
    print(report)


def test_guard_leaves_real_streams_alone():
    """Every non-frozen run -- this suite, a dev ``privacyfence-app``, the
    macOS/Linux builds (a launchd- or systemd-started process inherits real
    descriptors) -- must be completely untouched by this."""
    before_out, before_err = sys.stdout, sys.stderr
    ensure_std_streams()
    assert sys.stdout is before_out
    assert sys.stderr is before_err
