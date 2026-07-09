"""Shared gating helper: auto-accept check -> native popup -> audit log.

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

PII gate: before any auto-accept check, the scan text (``pii_scan_text`` if
the caller provided one, otherwise the same ``details`` shown in the popup)
is scanned by pii_detector.py for likely Hungarian/English/German personal
data. A match overrides a matching auto-accept rule — the call is routed to
the normal interactive popup regardless — which is then tinted, and after
the user clicks Accept (or Accept All), one more explicit "Are you sure?"
dialog is required before the decision is finalized. Declining it is treated
the same as denying the original request. Auto-accept rules are typically
scoped to metadata (sender domain, folder, "I am the organizer") rather than
content, so a rule that would silently pass through PII-bearing content
still gets a human in the loop for that specific item.

Callers should pass ``pii_scan_text`` whenever ``details_text`` mixes
structural envelope metadata (an email's From/To headers, a chat message's
channel/sender, a page's author) with the actual content (body, message
text, description) -- that metadata is present on every item regardless of
what it says and will otherwise make the PII gate fire on essentially every
read. ``pii_scan_text`` should carry only the actual content being read.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from .approval_popup import (
    show_pii_confirmation_popup,
    show_popup,
    show_read_popup,
    show_rule_confirmation_popup,
)
from .audit_log import AuditEntry, current_week, get_audit_logger
from .auto_accept import (
    TOOL_TO_OPERATION,
    ReviewContext,
    add_auto_accept_rule,
    describe_rule,
    get_auto_accept_evaluator,
    suggest_rule,
)
from .pii_detector import detect_pii_categories

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
    pii_scan_text: str | None = None,  # content-only text for the PII scan; defaults to details_text
    my_email: str = "",
    session_created_ids: set | None = None,
    args: dict | None = None,
) -> Any:
    created_at = time.time()
    request_id = uuid.uuid4().hex[:12]
    operation_key = TOOL_TO_OPERATION.get(tool, f"{connector}.{tool}")

    ctx = ReviewContext(
        connector=connector,
        tool=tool,
        args=args or {},
        raw_data=raw_data,
        my_email=my_email,
        session_created_ids=session_created_ids or set(),
    )
    details = details_text or _default_details(raw_data)
    popup_title = f"PrivacyFence — {tool_name}"
    pii_categories = detect_pii_categories(details if pii_scan_text is None else pii_scan_text)

    # Every exit from this function -- including one triggered by an
    # exception nobody anticipated below (a native popup call raising, a
    # rule-file write failing) -- must leave exactly one audit entry behind.
    # Without that guarantee, a call that visibly ran and got a real decision
    # from the user can still leave "no matching entry" in the log: a true
    # gap in the trust boundary this module exists to enforce. `audited`
    # tracks whether one of the normal decision branches below already wrote
    # one; the `finally` block below only steps in if none of them did.
    audited = False

    def audit(*, decision: str, auto_accept_rule: str, pii_detected: bool) -> None:
        nonlocal audited
        audited = True
        _audit(
            created_at=created_at, request_id=request_id, connector=connector, tool=tool,
            tool_name=tool_name, summary=summary, sender=sender,
            decision=decision, auto_accept_rule=auto_accept_rule, pii_detected=pii_detected,
        )

    try:
        evaluator = get_auto_accept_evaluator()
        auto_ok, matched_rule = evaluator.should_auto_accept(operation_key, ctx)

        if auto_ok and not pii_categories:
            audit(decision="auto_accepted", auto_accept_rule=matched_rule, pii_detected=False)
            logger.info("Auto-accepted: %s/%s rule=%r", connector, tool, matched_rule)
            return filtered_data

        if gate == "review":
            suggestion = suggest_rule(operation_key, ctx)
            # Everything interactive for this item — including the PII
            # confirmation, the "Accept All" confirmation, and persisting the
            # resulting rule — stays inside one continuous lock acquisition.
            # Releasing and re-acquiring the lock between popups would open a
            # window where a request queued behind this one slips through with
            # the pre-rule rule set and pops up its own dialog for something the
            # user just approved.
            async with _popup_lock:
                # Re-check: while this call was queued behind another popup, that
                # popup's "Accept All" may have just created a rule that now
                # covers this item too. A PII match still overrides it either way.
                auto_ok, matched_rule = evaluator.should_auto_accept(operation_key, ctx)
                if auto_ok and not pii_categories:
                    audit(decision="auto_accepted", auto_accept_rule=matched_rule, pii_detected=False)
                    logger.info("Auto-accepted while queued: %s/%s rule=%r", connector, tool, matched_rule)
                    return filtered_data

                decision = await asyncio.to_thread(
                    show_read_popup, popup_title, preview or {}, details, suggestion is not None, pii_categories
                )

                if decision in ("accept", "accept_all") and pii_categories:
                    decision = await _confirm_pii_or_deny(decision, pii_categories)

                if decision == "accept_all" and suggestion is not None:
                    rule_name, value = suggestion
                    description = describe_rule(rule_name, value)
                    confirmed = await asyncio.to_thread(show_rule_confirmation_popup, description)
                    if confirmed:
                        add_auto_accept_rule(operation_key, rule_name, value)
                        audit(
                            decision="accepted_via_accept_all", auto_accept_rule=rule_name,
                            pii_detected=bool(pii_categories),
                        )
                        logger.info("Accept All: created rule %r for %s", rule_name, operation_key)
                        return filtered_data
                    # Cancelled rule creation — this item is still accepted, just once.
                    decision = "accept"

            if decision == "deny":
                audit(decision="rejected", auto_accept_rule="", pii_detected=bool(pii_categories))
                raise RuntimeError("Request denied by user")

            audit(decision="approved", auto_accept_rule="", pii_detected=bool(pii_categories))
            return filtered_data

        else:
            # ── Popup gate: block and show native approval dialog for a write ───
            async with _popup_lock:
                # Same race as above: a rule may have been created while queued.
                # A PII match still overrides it either way.
                auto_ok, matched_rule = evaluator.should_auto_accept(operation_key, ctx)
                if auto_ok and not pii_categories:
                    audit(decision="auto_accepted", auto_accept_rule=matched_rule, pii_detected=False)
                    logger.info("Auto-accepted while queued: %s/%s rule=%r", connector, tool, matched_rule)
                    return filtered_data

                decision = await asyncio.to_thread(show_popup, popup_title, preview or {}, details, pii_categories)

                if decision == "accept" and pii_categories:
                    decision = await _confirm_pii_or_deny(decision, pii_categories)

            if decision == "accept":
                audit(decision="approved", auto_accept_rule="", pii_detected=bool(pii_categories))
                return filtered_data

            audit(decision="rejected", auto_accept_rule="", pii_detected=bool(pii_categories))
            raise RuntimeError("Request denied by user")
    finally:
        if not audited:
            logger.error(
                "gated_call for %s/%s (request %s) exited without recording a decision "
                "-- recording a fallback 'error' entry so the audit trail has no silent gap",
                connector, tool, request_id,
            )
            audit(decision="error", auto_accept_rule="", pii_detected=bool(pii_categories))


async def _confirm_pii_or_deny(decision: str, pii_categories: list[str]) -> str:
    """Extra gate for content the PII detector flagged: forces one more
    explicit confirmation on top of the popup's own Accept/Accept All,
    declining which is treated as a deny of the whole request."""
    confirmed = await asyncio.to_thread(show_pii_confirmation_popup, pii_categories)
    return decision if confirmed else "deny"


def _default_details(raw_data: Any) -> str:
    try:
        if hasattr(raw_data, "__dict__"):
            return json.dumps(raw_data.__dict__, default=str, indent=2, ensure_ascii=False)
        return json.dumps(raw_data, default=str, indent=2, ensure_ascii=False)
    except Exception:
        return str(raw_data)


def _audit(
    *, created_at, request_id, connector, tool, tool_name, summary, sender, decision, auto_accept_rule,
    pii_detected=False,
) -> None:
    try:
        get_audit_logger().record(AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            week=current_week(),
            request_id=request_id,
            connector=connector,
            tool=tool,
            tool_name=tool_name,
            summary=summary,
            sender=sender,
            decision=decision,
            auto_accept_rule=auto_accept_rule,
            latency_seconds=time.time() - created_at,
            pii_detected=pii_detected,
        ))
    except Exception as exc:
        logger.warning("Audit log write failed: %s", exc)
