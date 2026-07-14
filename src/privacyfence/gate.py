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
    is not offered here. A small set of operations expected to be called
    repeatedly against the same file in quick succession (see
    auto_accept.TEMP_ACCEPT_ELIGIBLE_OPERATIONS) get a narrower "Accept for
    5 min" button instead: it auto-accepts further calls of the same
    operation against that same file for 5 minutes, in memory only (never
    written to settings.yaml, gone on daemon restart) -- a much smaller
    commitment than a standing Accept All rule.

PII gate: read tools only (``gate="review"``). Before any auto-accept check,
the scan text (``pii_scan_text`` if the caller provided one, otherwise the
same ``details`` shown in the popup) is scanned by pii_detector.py for
likely Hungarian/English/German personal data. A match overrides a matching
auto-accept rule — the call is routed to the normal interactive popup
regardless — which is then tinted, and after the user clicks Accept (or
Accept All), one more explicit "Are you sure?" dialog is required before the
decision is finalized. Declining it is treated the same as denying the
original request. Auto-accept rules are typically scoped to metadata (sender
domain, folder, "I am the organizer") rather than content, so a rule that
would silently pass through PII-bearing content still gets a human in the
loop for that specific item.

Write tools (``gate="popup"``) never run this scan: the gate exists to catch
personal data flowing from an external source into Claude's context, and a
write is content Claude itself already generated going the other way, to a
tool Claude already described in chat -- there's no external PII to
intercept on that side.

Callers should pass ``pii_scan_text`` whenever a review-gate ``details_text``
mixes structural envelope metadata (an email's From/To headers, a chat
message's channel/sender, a page's author) with the actual content (body,
message text, description) -- that metadata is present on every item
regardless of what it says and will otherwise make the PII gate fire on
essentially every read. ``pii_scan_text`` should carry only the actual
content being read.
"""
from __future__ import annotations

import asyncio
import contextvars
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
    temp_accept_key,
)
from .pii_detector import detect_pii_categories

logger = logging.getLogger(__name__)

_popup_lock = asyncio.Lock()  # only one native dialog on screen at a time

# Set by ipc_server.py around a single dispatched request, for the duration
# of that request only, when the request came in on a connection that
# called privacyfence_begin_unattended_session() and hasn't since called
# privacyfence_end_unattended_session() -- see unattended_scope() below and
# docs/cowork-scheduled-tasks-design.md. Deliberately NOT a module-level
# bool: a plain bool would be shared across every concurrent request on
# every connection, but unattended mode is a per-connection state (the
# bridge is one process per Cowork task, so "per connection" already means
# "per scheduled run").
_unattended_ctx: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "privacyfence_unattended", default=False
)


def is_unattended() -> bool:
    return _unattended_ctx.get()


class unattended_scope:  # noqa: N801 (context-manager-style name, like `freeze_time`)
    """Run the wrapped code with the unattended-session flag set to `enabled`.

    ipc_server.py wraps each dispatched ``call`` request in this, based on
    whether the request's connection is currently in an unattended session.
    gated_call() below is the only reader (via is_unattended()) -- no
    connector code needs to know this exists.
    """

    def __init__(self, enabled: bool) -> None:
        self._enabled = enabled
        self._token: contextvars.Token | None = None

    def __enter__(self) -> "unattended_scope":
        self._token = _unattended_ctx.set(self._enabled)
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._token is not None:
            _unattended_ctx.reset(self._token)


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
    # Only the review (read) gate scans for PII -- see module docstring.
    pii_categories = (
        detect_pii_categories(details if pii_scan_text is None else pii_scan_text)
        if gate == "review" else []
    )

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

                if is_unattended():
                    # No rule matched, or one did but the PII gate still
                    # routed this to a human (see module docstring) -- either
                    # way, nobody's here to answer a popup. Fail this one
                    # step now instead of hanging on _popup_lock forever and
                    # blocking every other approval behind it.
                    _deny_unattended(audit, connector, tool, pii_categories=pii_categories)

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
            # No PII scan here -- see module docstring: this gate covers
            # content Claude itself generated for an outbound write, not
            # personal data flowing in from an external source.
            file_key = temp_accept_key(operation_key, ctx)
            async with _popup_lock:
                # Same race as above: a rule may have been created while queued.
                auto_ok, matched_rule = evaluator.should_auto_accept(operation_key, ctx)
                if auto_ok:
                    audit(decision="auto_accepted", auto_accept_rule=matched_rule, pii_detected=False)
                    logger.info("Auto-accepted while queued: %s/%s rule=%r", connector, tool, matched_rule)
                    return filtered_data

                if is_unattended():
                    _deny_unattended(audit, connector, tool, pii_categories=[])

                decision = await asyncio.to_thread(
                    show_popup, popup_title, preview or {}, details, file_key is not None
                )

            if decision == "accept_temp":
                if file_key is not None:
                    evaluator.register_temp_accept(operation_key, file_key)
                    audit(
                        decision="accepted_via_temp_session", auto_accept_rule="session_temp_accept",
                        pii_detected=False,
                    )
                    logger.info(
                        "Accept for 5 min: op=%s file=%s (%s, %s)", operation_key, file_key, connector, tool
                    )
                    return filtered_data
                # Button shouldn't have been offered without a file_key -- fall
                # back to a plain, once-only accept rather than denying a click
                # the user clearly meant as approval.
                decision = "accept"

            if decision == "accept":
                audit(decision="approved", auto_accept_rule="", pii_detected=False)
                return filtered_data

            audit(decision="rejected", auto_accept_rule="", pii_detected=False)
            raise RuntimeError("Request denied by user")
    finally:
        if not audited:
            logger.error(
                "gated_call for %s/%s (request %s) exited without recording a decision "
                "-- recording a fallback 'error' entry so the audit trail has no silent gap",
                connector, tool, request_id,
            )
            audit(decision="error", auto_accept_rule="", pii_detected=bool(pii_categories))


def _deny_unattended(audit, connector: str, tool: str, *, pii_categories: list[str]) -> None:
    """Fail-fast path for unattended sessions: same outcome as a human
    clicking Deny, minus the popup nobody's there to answer -- see
    unattended_scope() above and docs/cowork-scheduled-tasks-design.md.

    Always raises; the "-> None" return type documents that this never
    returns a decision to act on, only ever exits via exception.
    """
    audit(decision="denied_unattended", auto_accept_rule="", pii_detected=bool(pii_categories))
    logger.warning(
        "Unattended session: denying %s/%s without prompting -- no auto-accept rule matched%s",
        connector, tool, " (or the PII gate overrode one that did)" if pii_categories else "",
    )
    raise RuntimeError(
        "Request denied: this connection is in an unattended session and no auto-accept rule "
        "matches this call, so it can't be approved without a human present."
    )


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
