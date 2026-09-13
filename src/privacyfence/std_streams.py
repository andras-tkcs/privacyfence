"""Repair for the one thing a windowed Windows build starts without.

Kept in its own module, with no imports beyond the standard library, so
``src/_daemon_entry.py`` can run it *before* importing anything else --
including this package's own daemon and every third-party library it pulls
in. A library that probes ``sys.stdout`` at import time would otherwise hit
the same ``None`` this exists to get rid of, and no guard inside ``main()``
would ever get the chance to help.
"""
from __future__ import annotations

import os
import sys

_STREAM_NAMES = ("stdout", "stderr", "__stdout__", "__stderr__")


def ensure_std_streams() -> None:
    """Give this process real ``sys.stdout``/``sys.stderr`` objects when
    Windows didn't.

    The Windows build is a *windowed* PyInstaller executable
    (``PrivacyFenceApp.win.spec``'s ``console=False`` -- it is a background
    daemon, and a console window flashing up at every sign-in would be a bug
    of its own), and a windowed process started with no console at all has
    no standard handles. CPython then sets ``sys.stdout``/``sys.stderr`` to
    ``None`` rather than to a file object, and any library that reaches for
    them without checking raises ``AttributeError`` on ``None``.

    uvicorn is exactly such a library: ``uvicorn.Config(...)`` configures its
    own logging, whose default formatter calls ``sys.stdout.isatty()`` to
    decide about colour. So the daemon died there, every time, with

        AttributeError: 'NoneType' object has no attribute 'isatty'
        ValueError: Unable to configure formatter 'default'

    exiting 1 before it ever bound its port -- which is to say the Windows
    autostart task started the daemon correctly and the daemon then killed
    itself, and the same would have happened to the installer's own "launch
    PrivacyFence now" step or to double-clicking the executable. Nothing
    caught it because every automated start of this app until
    ``windows-graphical-session.yml`` had Task Scheduler do the launching --
    the packaged smoke tests included -- ran it from a shell with stdout
    redirected to a file, which is a perfectly valid stream.

    Repairing the frozen process's environment before anything else runs is
    the same move, for the same reason, as ``src/_daemon_entry.py``'s own
    ``SSL_CERT_FILE`` fix-up. ``os.devnull`` is the right target: this
    daemon's own logging goes to its log file regardless
    (``daemon_main.setup_logging``), and a background process has nowhere
    meaningful to write a console stream to anyway.

    A no-op anywhere the streams already exist, which is every other way this
    code ever runs: the test suite, a dev checkout, the macOS/Linux builds
    (a launchd- or systemd-started process inherits real descriptors).
    """
    devnull = None
    for name in _STREAM_NAMES:
        if getattr(sys, name, None) is None:
            if devnull is None:
                devnull = open(os.devnull, "w", encoding="utf-8", buffering=1)  # noqa: SIM115
            setattr(sys, name, devnull)
