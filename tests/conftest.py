"""Shared fixtures. Resets module-level singletons that auto_accept.py,
audit_log.py, approval_ui.py, resource_names.py, and web_approval_ui.py use,
so tests don't leak state into each other via import-time globals.
"""
from __future__ import annotations

import pytest

from privacyfence import (
    approval_ui,
    auto_accept,
    audit_log,
    pii_detector,
    privacy_filter,
    resource_names,
    web_approval_ui,
)


@pytest.fixture(autouse=True)
def _reset_singletons():
    auto_accept._INSTANCE = None
    auto_accept._config_path = None
    auto_accept._rules_changed_listener = None
    auto_accept._rules_changed_listeners.clear()
    audit_log._INSTANCE = None
    approval_ui._INSTANCE = None
    pii_detector._enabled = True
    pii_detector._changed_listener = None
    pii_detector._disabled_categories.clear()
    pii_detector._audit_match_details_enabled = False
    privacy_filter._GROUPS = {}
    resource_names._INSTANCE = None
    web_approval_ui._INSTANCE = None
    yield
    auto_accept._INSTANCE = None
    auto_accept._config_path = None
    auto_accept._rules_changed_listener = None
    auto_accept._rules_changed_listeners.clear()
    audit_log._INSTANCE = None
    approval_ui._INSTANCE = None
    pii_detector._enabled = True
    pii_detector._changed_listener = None
    pii_detector._disabled_categories.clear()
    pii_detector._audit_match_details_enabled = False
    privacy_filter._GROUPS = {}
    resource_names._INSTANCE = None
    web_approval_ui._INSTANCE = None
