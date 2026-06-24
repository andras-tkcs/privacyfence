"""Shared gating helper: auto-accept check → review queue → audit log."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from .audit_log import AuditEntry, current_week, get_audit_logger
from .auto_accept import TOOL_TO_OPERATION, AutoAcceptEvaluator, ReviewContext, get_auto_accept_evaluator
from .review_queue import ReviewRejected, get_review_queue

logger = logging.getLogger(__name__)


async def gated_call(
    *,
    connector: str,
    tool: str,
    tool_name: str,
    summary: str,
    sender: str,
    raw_data: Any,
    filtered_data: Any,
    display_hint: dict | None = None,
    my_email: str = "",
    session_created_ids: set | None = None,
    args: dict | None = None,
) -> Any:
    """Check auto-accept rules; if not matched, submit to the review queue.

    Returns filtered_data (either immediately via auto-accept, or after the
    user approves in the UI). Raises RuntimeError if rejected.
    """
    created_at = time.time()
    operation_key = TOOL_TO_OPERATION.get(tool, f"{connector}.{tool}")

    ctx = ReviewContext(
        connector=connector,
        tool=tool,
        args=args or {},
        raw_data=raw_data,
        my_email=my_email,
        session_created_ids=session_created_ids or set(),
    )
    evaluator = get_auto_accept_evaluator()
    auto_ok, matched_rule = evaluator.should_auto_accept(operation_key, ctx)

    if auto_ok:
        _audit(
            created_at=created_at,
            connector=connector, tool=tool, tool_name=tool_name,
            summary=summary, sender=sender,
            decision="auto_accepted", auto_accept_rule=matched_rule,
        )
        logger.info("Auto-accepted: %s/%s rule=%r", connector, tool, matched_rule)
        return filtered_data

    # Human gate
    queue = get_review_queue()
    future = queue.submit(
        tool_name=tool_name,
        summary=summary,
        sender=sender,
        raw_data=raw_data,
        filtered_data=filtered_data,
        display_hint=display_hint,
        connector=connector,
        tool=tool,
    )
    try:
        result = await future
    except ReviewRejected as exc:
        _audit(
            created_at=created_at,
            connector=connector, tool=tool, tool_name=tool_name,
            summary=summary, sender=sender,
            decision="rejected", auto_accept_rule="",
        )
        raise RuntimeError(f"Request denied by user: {exc}") from exc

    # approved — audit is already recorded by review_queue.approve()
    return result


def _audit(*, created_at, connector, tool, tool_name, summary, sender, decision, auto_accept_rule):
    try:
        get_audit_logger().record(AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            week=current_week(),
            request_id="",
            connector=connector,
            tool=tool,
            tool_name=tool_name,
            summary=summary,
            sender=sender,
            decision=decision,
            auto_accept_rule=auto_accept_rule,
            latency_seconds=time.time() - created_at,
        ))
    except Exception as exc:
        logger.warning("Audit log write failed: %s", exc)
