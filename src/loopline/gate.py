"""Shared gating helper: auto-accept check → native popup → audit log.

Every gated call resolves synchronously inside gated_call(): the data is
fetched, an auto-accept rule may skip the popup entirely, otherwise a native
macOS dialog (approval_popup.py) blocks until the user decides. There is no
pending-approval handshake — gated_call() either returns the data or raises
in the same call that fetched it, so Claude never holds a tool that can
release gated data on its own.

  gate="review"  (read tools)
    Popup offers Deny / Accept / and — when a plausible auto-accept rule can
    be derived from the item's attributes — Accept All, which proposes (with
    a second confirmation dialog) a standing rule for similar future reads.

  gate="popup"   (write tools)
    Popup offers Deny / Accept only. Auto-accepting writes silently is a
    materially bigger blast radius than auto-accepting reads, so Accept All
    is not offered here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from .approval_popup import show_popup, show_read_popup, show_rule_confirmation_popup
from .audit_log import AuditEntry, current_week, get_audit_logger
from .auto_accept import (
    TOOL_TO_OPERATION,
    ReviewContext,
    add_auto_accept_rule,
    describe_rule,
    get_auto_accept_evaluator,
    suggest_rule,
)

logger = logging.getLogger(__name__)

_popup_lock = asyncio.Lock()  # only one native dialog on screen at a time


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
    preview: dict | None = None,  # fields shown in the review-gate dialog
    details_text: str = "",       # full text shown inline or via TextEdit
    my_email: str = "",
    session_created_ids: set | None = None,
    args: dict | None = None,
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

    details = details_text or _default_details(raw_data)
    popup_title = f"Loopline — {tool_name}"

    if gate == "review":
        suggestion = suggest_rule(operation_key, ctx)
        async with _popup_lock:
            decision = await asyncio.to_thread(
                show_read_popup, popup_title, preview or {}, details, suggestion is not None
            )

        if decision == "deny":
            _audit(
                created_at=created_at, connector=connector, tool=tool,
                tool_name=tool_name, summary=summary, sender=sender,
                decision="rejected", auto_accept_rule="",
            )
            raise RuntimeError("Request denied by user")

        if decision == "accept_all" and suggestion is not None:
            rule_name, value = suggestion
            description = describe_rule(rule_name, value)
            async with _popup_lock:
                confirmed = await asyncio.to_thread(show_rule_confirmation_popup, description)
            if confirmed:
                add_auto_accept_rule(operation_key, rule_name, value)
                _audit(
                    created_at=created_at, connector=connector, tool=tool,
                    tool_name=tool_name, summary=summary, sender=sender,
                    decision="accepted_via_accept_all", auto_accept_rule=rule_name,
                )
                logger.info("Accept All: created rule %r for %s", rule_name, operation_key)
                return filtered_data
            # Cancelled rule creation — this item is still accepted, just once.

        _audit(
            created_at=created_at, connector=connector, tool=tool,
            tool_name=tool_name, summary=summary, sender=sender,
            decision="approved", auto_accept_rule="",
        )
        return filtered_data

    else:
        # ── Popup gate: block and show native approval dialog for a write ───
        async with _popup_lock:
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
