"""The windowed-Windows-build std-streams guard (`daemon_main._ensure_std_streams`).

This is the regression test for the defect that made Windows autostart
never work: a windowed PyInstaller executable started with no console has
no standard handles, so CPython sets ``sys.stdout``/``sys.stderr`` to
``None``, and uvicorn's default log formatter calls ``sys.stdout.isatty()``
while ``uvicorn.Config(...)`` is being built. The daemon exited 1 before
binding its port — with Task Scheduler having started it exactly as
designed.

The test reproduces the real failing call rather than asserting on the
guard's own bookkeeping: construct uvicorn's own formatter with
``sys.stdout`` set to ``None``, which is what
``uvicorn/logging.py``'s ``ColourizedFormatter.__init__`` chokes on.
"""
from __future__ import annotations

import sys

import pytest
from uvicorn.logging import DefaultFormatter

from privacyfence.daemon_main import _ensure_std_streams


@pytest.fixture
def no_std_streams(monkeypatch):
    """Stand in for a windowed Windows process started with no console.

    ``monkeypatch`` restores the real streams afterwards, including over the
    guard's own assignment — the file object it opened is closed here so the
    test leaves nothing behind."""
    for name in ("stdout", "stderr", "__stdout__", "__stderr__"):
        monkeypatch.setattr(sys, name, None)
    opened: list = []
    yield opened
    for stream in opened:
        if stream is not None and not stream.closed:
            stream.close()


def test_uvicorn_formatter_cannot_be_built_without_the_guard(no_std_streams):
    """Documents the actual crash, so this test fails loudly if uvicorn ever
    stops touching ``sys.stdout`` and the guard stops being load-bearing for
    this particular caller (it would still be load-bearing for the next
    library that does)."""
    with pytest.raises(AttributeError):
        DefaultFormatter(use_colors=None)


def test_guard_restores_streams_and_uvicorn_configures(no_std_streams):
    _ensure_std_streams()
    no_std_streams.append(sys.stdout)

    assert sys.stdout is not None and sys.stderr is not None
    assert sys.__stdout__ is not None and sys.__stderr__ is not None
    # The real call that used to raise: uvicorn.Config's own logging setup.
    formatter = DefaultFormatter(use_colors=None)
    assert formatter.use_colors is False
    # Writing to them must be harmless, not an error -- anything in-process
    # that prints (a dependency's warning, say) has to keep working.
    print("this goes to os.devnull, and must not raise")


def test_guard_leaves_real_streams_alone():
    """Every non-frozen run -- the whole test suite, a dev `privacyfence-app`,
    the macOS/Linux builds -- must be untouched by this."""
    before_out, before_err = sys.stdout, sys.stderr
    _ensure_std_streams()
    assert sys.stdout is before_out
    assert sys.stderr is before_err
