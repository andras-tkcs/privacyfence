"""Test-only helpers shared across the unit test suite."""
from __future__ import annotations

from privacyfence.auto_accept import ReviewContext


def make_ctx(**overrides) -> ReviewContext:
    """Build a ReviewContext with sane defaults, override via kwargs."""
    defaults = dict(
        connector="gmail",
        tool="gmail_get_message",
        args={},
        raw_data=None,
        my_email="",
        session_created_ids=set(),
    )
    defaults.update(overrides)
    return ReviewContext(**defaults)
