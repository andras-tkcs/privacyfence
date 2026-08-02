"""Shared fixtures. Resets module-level singletons that auto_accept.py,
audit_log.py, and approval_ui.py use, so tests don't leak state into each
other via import-time globals.
"""
from __future__ import annotations

import pytest

from privacyfence import approval_ui, auto_accept, audit_log, pii_detector, privacy_filter, quicklook_preview


@pytest.fixture(autouse=True)
def _reset_singletons():
    auto_accept._INSTANCE = None
    auto_accept._config_path = None
    auto_accept._rules_changed_listener = None
    auto_accept._suggestion_priority = {}
    audit_log._INSTANCE = None
    approval_ui._INSTANCE = None
    pii_detector._enabled = True
    pii_detector._changed_listener = None
    pii_detector._disabled_categories.clear()
    privacy_filter._GROUPS = {}
    quicklook_preview._enabled = False
    quicklook_preview._max_wait_seconds = quicklook_preview.DEFAULT_MAX_WAIT_SECONDS
    quicklook_preview._changed_listener = None
    yield
    auto_accept._INSTANCE = None
    auto_accept._config_path = None
    auto_accept._rules_changed_listener = None
    auto_accept._suggestion_priority = {}
    audit_log._INSTANCE = None
    approval_ui._INSTANCE = None
    pii_detector._enabled = True
    pii_detector._changed_listener = None
    pii_detector._disabled_categories.clear()
    privacy_filter._GROUPS = {}
    quicklook_preview._enabled = False
    quicklook_preview._max_wait_seconds = quicklook_preview.DEFAULT_MAX_WAIT_SECONDS
    quicklook_preview._changed_listener = None
