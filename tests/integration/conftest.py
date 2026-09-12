"""``pytest_runtest_makereport`` -- a hook implementation, which (unlike a
fixture) pytest only ever collects from a ``conftest.py``/plugin, never from
an ordinary test module -- which is why this one function lives here rather
than alongside the fixture that actually consumes its result
(``test_browser_smoke.py``'s own ``_capture_failure_artifacts``, closing
Phase 4 item 4.5's "systematic failure-artifact capture", docs/automated-
test-strategy-plan.md).

Stashes each phase's own outcome (setup/call/teardown) onto the test item as
``rep_<phase>`` -- the standard pytest pattern for "did the test body itself
fail?" from inside a fixture's teardown code, without pulling in
``pytest-playwright``'s own opinionated plugin (screenshot-on-failure baked
into its own fixtures, a different browser-launch story than this suite's
module-scoped ``browser`` fixture already settled on -- see that fixture's
own docstring) just for this one hook.
"""
from __future__ import annotations

import pytest


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    rep = yield
    setattr(item, f"rep_{rep.when}", rep)
    return rep
