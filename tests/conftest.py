"""Shared fixtures. Resets module-level singletons that auto_accept.py,
audit_log.py, approval_ui.py, resource_names.py, and web_approval_ui.py use,
so tests don't leak state into each other via import-time globals.

Five of these (auto_accept, audit_log, pii_detector, privacy_filter,
resource_names) are per-*principal* registries as of P6 (docs/
https-connector-refactor-plan.md §9.2), not bare singletons -- resetting
means clearing every principal's cached instance, not just the local one,
so a test that used principal_scope() directly doesn't leak into the next
test either. approval_ui and web_approval_ui stay true process-wide
singletons by design (see principal.py's own docstring on why) --
still reset the same way as before this phase.
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
    settings_controller,
    web_approval_ui,
)
from privacyfence.web import state_stream


def _reset() -> None:
    auto_accept._REGISTRY.reset()
    audit_log._REGISTRY.reset()
    approval_ui._INSTANCE = None
    pii_detector._REGISTRY.reset()
    privacy_filter._REGISTRY.reset()
    resource_names._REGISTRY.reset()
    web_approval_ui._INSTANCE = None
    settings_controller._main_dispatch = None
    state_stream._loop = None


@pytest.fixture(autouse=True)
def _reset_singletons():
    _reset()
    yield
    _reset()
