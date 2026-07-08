"""Shared fixtures. Resets module-level singletons that auto_accept.py and
audit_log.py use, so tests don't leak state into each other via import-time
globals.
"""
from __future__ import annotations

import pytest

from privacyfence import auto_accept, audit_log, pii_detector


@pytest.fixture(autouse=True)
def _reset_singletons():
    auto_accept._INSTANCE = None
    auto_accept._config_path = None
    auto_accept._rules_changed_listener = None
    audit_log._INSTANCE = None
    pii_detector._enabled = True
    pii_detector._changed_listener = None
    yield
    auto_accept._INSTANCE = None
    auto_accept._config_path = None
    auto_accept._rules_changed_listener = None
    audit_log._INSTANCE = None
    pii_detector._enabled = True
    pii_detector._changed_listener = None
