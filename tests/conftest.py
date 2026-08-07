"""Shared fixtures. Resets module-level singletons that auto_accept.py,
audit_log.py, approval_ui.py, and resource_names.py use, so tests don't
leak state into each other via import-time globals.
"""
from __future__ import annotations

import sys

import pytest

from privacyfence import approval_ui, auto_accept, audit_log, pii_detector, privacy_filter, resource_names

# These four test files import their macOS-only module under test (menu_bar/
# approval_window/dialog_window/settings_window -- all unconditionally
# `import rumps`/`objc`/`from AppKit import ...`/`from WebKit import ...` at
# module scope) directly, not through a platform-dispatched seam the way
# approval_popup.py's tests do (see approval_popup.py's own module
# docstring for that dispatch, issue #121). Each of these files' own
# in-file `pytestmark = pytest.mark.skipif(sys.platform != "darwin", ...)`
# only skips *running* their tests -- it can't prevent *collection* from
# failing first with a bare ModuleNotFoundError on a machine with no pyobjc
# installed at all (real Windows CI; this project's own Linux dev sandbox
# uses a set of pyobjc stub modules on PYTHONPATH specifically so collection
# still succeeds there too -- see docs/windows-port-status.md). Excluding
# them from collection entirely here, rather than only in-file, is what
# actually keeps a Windows (or plain Linux, without those stubs) pytest run
# from aborting outright before any test gets to run at all.
collect_ignore_glob: list[str] = []
if sys.platform != "darwin":
    collect_ignore_glob += [
        "unit/test_menu_bar.py",
        "unit/test_approval_window.py",
        "unit/test_dialog_window.py",
        "unit/test_settings_window.py",
    ]


@pytest.fixture(autouse=True)
def _reset_singletons():
    auto_accept._INSTANCE = None
    auto_accept._config_path = None
    auto_accept._rules_changed_listener = None
    audit_log._INSTANCE = None
    approval_ui._INSTANCE = None
    pii_detector._enabled = True
    pii_detector._changed_listener = None
    pii_detector._disabled_categories.clear()
    privacy_filter._GROUPS = {}
    resource_names._INSTANCE = None
    yield
    auto_accept._INSTANCE = None
    auto_accept._config_path = None
    auto_accept._rules_changed_listener = None
    audit_log._INSTANCE = None
    approval_ui._INSTANCE = None
    pii_detector._enabled = True
    pii_detector._changed_listener = None
    pii_detector._disabled_categories.clear()
    privacy_filter._GROUPS = {}
    resource_names._INSTANCE = None
