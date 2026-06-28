"""Shared gating helper: auto-accept check → approval popup → audit log."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from .audit_log import AuditEntry, current_week, get_audit_logger
from .auto_accept import TOOL_TO_OPERATION, AutoAcceptEvaluator, ReviewContext, get_auto_accept_evaluator

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
    gate: str = "review",         # "review" | "popup"
    preview: dict | None = None,  # fields shown in the short preview (review gate)
    details_text: str = "",       # full text for the details popup
    my_email: str = "",
    session_created_ids: set | None = None,
    args: dict | None = None,
    display_hint: dict | None = None,  # kept for compatibility, ignored
) -> Any:
    """Auto-accept check → approval popup → audit log.

    gate="review" — preview popup with Accept / Deny / Show Details.
                    'preview' dict sets the fields shown in the short view.
                    'details_text' is the full content shown on Show Details.
    gate="popup"  — full-details popup with Accept / Deny immediately.
                    'details_text' is shown in the popup body.

    Returns filtered_data on approval.  Raises RuntimeError if denied.
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
            created_at=created_at, connector=connector, tool=tool,
            tool_name=tool_name, summary=summary, sender=sender,
            decision="auto_accepted", auto_accept_rule=matched_rule,
        )
        logger.info("Auto-accepted: %s/%s rule=%r", connector, tool, matched_rule)
        return filtered_data

    from .approval_popup import show_popup, show_review_popup

    popup_title = f"Loopline — {tool_name}"
    details = details_text or _default_details(raw_data)

    if gate == "review":
        pf = preview or {}
        decision = await asyncio.to_thread(show_review_popup, popup_title, pf, details)
    else:
        decision = await asyncio.to_thread(show_popup, popup_title, details)

    if decision == "accept":
        _audit(
            created_at=created_at, connector=connector, tool=tool,
            tool_name=tool_name, summary=summary, sender=sender,
            decision="approved", auto_accept_rule="",
        )
        return filtered_data

    _audit(
        created_at=created_at, connector=connector, tool=tool,
        tool_name=tool_name, summary=summary, sender=sender,
        decision="rejected", auto_accept_rule="",
    )
    raise RuntimeError("Request denied by user")


def _default_details(raw_data: Any) -> str:
    try:
        if hasattr(raw_data, "__dict__"):
            return json.dumps(raw_data.__dict__, default=str, indent=2, ensure_ascii=False)
        return json.dumps(raw_data, default=str, indent=2, ensure_ascii=False)
    except Exception:
        return str(raw_data)


def _audit(
    *, created_at, connector, tool, tool_name, summary, sender, decision, auto_accept_rule
) -> None:
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
