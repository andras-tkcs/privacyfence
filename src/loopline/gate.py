"""Shared gating helper: auto-accept check → approval flow → audit log.

Two gate modes:

  gate="review"  (read tools)
    Data is fetched, stored in pending_reads, and a preview dict is returned
    to Claude immediately.  Claude presents the preview to the user in Cowork
    (Accept / Deny / Show Details) and then calls loopline_confirm,
    loopline_deny, or loopline_show_details with the request_id.

  gate="popup"   (write tools)
    Blocks the tool call and shows a native macOS popup (osascript) with the
    full action details.  Claude already described the action in chat so no
    Cowork step is needed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from .audit_log import AuditEntry, current_week, get_audit_logger
from .auto_accept import TOOL_TO_OPERATION, ReviewContext, get_auto_accept_evaluator

logger = logging.getLogger(__name__)

_ACTION_REQUIRED = (
    "A read request is pending user approval. "
    "Show the user the preview fields above and ask them to choose:\n"
    "  • Accept — call loopline_confirm(request_id) to release the data\n"
    "  • Deny   — call loopline_deny(request_id) to block the request\n"
    "  • Show Details — call loopline_show_details(request_id) to open a "
    "full-content popup; it returns the data if the user accepts there"
)


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
    preview: dict | None = None,  # fields shown as Cowork preview (review gate)
    details_text: str = "",       # full text for the details popup
    my_email: str = "",
    session_created_ids: set | None = None,
    args: dict | None = None,
    display_hint: dict | None = None,  # ignored, kept for compatibility
) -> Any:
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

    if gate == "review":
        # ── Phase 1: store data, return preview to Claude immediately ──────
        from . import pending_reads
        details = details_text or _default_details(raw_data)
        request_id = pending_reads.store(filtered_data, details)
        logger.info("Pending review: %s/%s request_id=%s", connector, tool, request_id)
        # Audit as "pending" so we know it was intercepted
        _audit(
            created_at=created_at, connector=connector, tool=tool,
            tool_name=tool_name, summary=summary, sender=sender,
            decision="pending", auto_accept_rule="",
        )
        return {
            "status": "pending_approval",
            "request_id": request_id,
            "tool": tool_name,
            "preview": preview or {},
            "action_required": _ACTION_REQUIRED,
        }

    else:
        # ── Popup gate: block and show native approval dialog ──────────────
        from .approval_popup import show_popup
        details = details_text or _default_details(raw_data)
        popup_title = f"Loopline — {tool_name}"
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
