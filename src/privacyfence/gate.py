"""Shared gating helper: auto-accept check -> popup -> audit log.

Every gated call resolves inside gated_call(): the data is fetched, an
auto-accept rule may skip the popup entirely, otherwise a popup shown
through the pluggable ApprovalUI seam (approval_ui.py) asks a human. Two
postures, chosen per call by whether the active ApprovalUI exposes a
``deferred_registry`` (approval_ui.py's own docstring):

- **No registry** (any ApprovalUI that has nowhere to send a human a
  reviewable link -- WebApprovalUI, the only implementation since P10
  retired NativeApprovalUI/approval_popup.py, always has one, so this path
  is dormant today but the seam still generalizes to it -- see
  approval_ui.py's own docstring): unchanged from before P3. gated_call()
  blocks until the popup returns; there is no pending-approval handshake,
  so Claude never holds a tool that can release gated data on its own.
- **A registry** (WebApprovalUI): gated_call() still resolves inline,
  identically, *if a human decides within ``registry.hold_window`` seconds*
  (default 30s -- D3). If not, it returns a structured
  ``{"status": "approval_pending", "approval_id", "url", ...}`` result
  instead of continuing to block, per
  docs/https-connector-refactor-plan.md §5. The human interaction keeps
  running in the background; when it concludes, the outcome lands in
  approvals.py's decision ledger, keyed by ``(connector, tool,
  canonical(args))``. Claude re-issuing the identical tool call finds that
  ledger entry (``_resolve_decision``'s consume_ledger() check, below,
  which every call -- deferred or not -- makes first) and releases the data
  without a second prompt. There is still no tool that takes an
  ``approval_id`` and returns content: the only path to data is the
  original gated call, replayed with identical arguments, exactly as before
  -- see approvals.py's own module docstring for the full protocol and why
  the security invariant survives it unchanged.

``_popup_lock`` (this module's own, pre-P3: one dialog serialized at a
time, and the "was this already covered by a rule created while queued?"
re-check that ran under it) is gone. Job 1 -- one screen, one dialog -- is
simply obsolete for the web surface, whose whole point is several
approvals pending at once (docs/https-connector-refactor-plan.md §6); the
native approval surface that used to keep its own, separate serialization
lock was retired at P10 (§12, D6), so there is no longer a second dialog
host to reconcile this module's own concurrency model against. Job 2
survives as an explicit re-check at the top of each
gate's interaction (see each branch's own ``_interact`` closure), for
whichever request happens to still be mid-interaction when a rule changes,
plus the rules-changed re-evaluation broadcast
(approvals.PendingApprovalRegistry.reevaluate_all(), subscribed below) for
anything that's already moved into the pending/registry state.

  gate="review"  (read tools)
    Popup offers Deny / Allow once / and — for every plausible auto-accept
    rule that can be derived from the item's attributes — an Always allow
    button, one per candidate (auto_accept.suggest_rule_choices()). Most
    operations only ever have one candidate, so this is a single button; the
    four operations in auto_accept.SUGGESTION_FAMILIES can match 2+ rules on
    the same item and render one button each. Clicking any of them proposes
    (with a second confirmation dialog) a standing rule for similar future
    reads.

  gate="popup"   (write tools)
    Popup offers Deny / Allow once by default. Auto-accepting writes
    silently is a materially bigger blast radius than auto-accepting reads,
    so Always allow is not offered on most write popups. Two narrow, opt-in
    exceptions to that rule, each scoped to avoid reopening it wholesale:

    - A small set of operations expected to be called repeatedly against
      the same file in quick succession (see
      auto_accept.TEMP_ACCEPT_ELIGIBLE_OPERATIONS) get a lighter-weight
      concession instead of a standing Always allow rule: clicking Allow
      once on one of these also auto-accepts further calls of the same
      operation against that same file for 5 minutes, in memory only
      (never written to settings.yaml, gone on daemon restart). Clicking
      Allow once on one of these operations arms this grace window
      automatically -- the popup discloses it with a plain caption
      (approval_window_html.py's temp_accept_eligible), not a separate control.
    - A separate, small set of operations that already have a
      resource-identity-scoped auto-accept rule (see
      auto_accept.WRITE_RULE_SUGGESTIONS -- one Gmail label, one calendar,
      one Jira project, one Confluence space, one Tasks list; never a bare
      "accept every future write of this type" toggle) get an actual
      Always allow button, proposing that rule scoped to the item just
      acted on -- the same second-confirmation-dialog flow the review
      branch already uses, reused here rather than reinvented.

    See the popup-gate branch below for where both actually get armed.

PII gate: read tools only (``gate="review"``). Before any auto-accept check,
the scan text (``pii_scan_text`` if the caller provided one, otherwise the
same ``details`` shown in the popup) is scanned by pii_detector.py for
likely Hungarian/English/German personal data. A match overrides a matching
auto-accept rule — the call is routed to the normal interactive popup
regardless — which is then tinted, and after the user clicks Allow once (or
Always allow), one more explicit "Are you sure?" dialog is required before the
decision is finalized. Declining it is treated the same as denying the
original request. Auto-accept rules are typically scoped to metadata (sender
domain, folder, "I am the organizer") rather than content, so a rule that
would silently pass through PII-bearing content still gets a human in the
loop for that specific item.

Write tools (``gate="popup"``) never run this real scan: the gate exists to
catch personal data flowing from an external source into Claude's context,
and a write is normally content Claude itself already generated going the
other way, to a tool Claude already described in chat -- there's no external
PII to intercept on that side. ``write_content_flags`` (below) is a
deliberately weaker, informational-only signal for the general write case.

PII-refinement trial capture, opt-in and off by default (pii_detection.
audit_match_details in settings.yaml -- see pii_detector.
is_pii_audit_match_details_enabled()): every audit entry always records
*which* category(ies) pii_detector.py flagged (``pii_categories`` --
category labels only, same thing the popup banner already shows, so this
part is always on). When the trial setting is also on, the entry's
``pii_match_details`` additionally carries the literal matched text (or a
redacted form for a value-bearing category -- see pii_detector.
describe_match_for_audit()) for a request that was actually approved, or a
fixed "details hidden" placeholder -- never the matched text -- for one
that wasn't. See _pii_match_details_for_audit() and AuditEntry's own
docstring in audit_log.py for the full contract.

One narrow, deliberate exception: ``upload_pii_scan_text``, only ever set by
drive_upload_file. Its payload (an arbitrary local file via ``local_path``,
or inline bytes via ``content_base64``) breaks the "Claude already generated
this" premise above -- it can be content Claude never read at all. When set,
this runs the same real scan (``upload_pii_categories``) and the same forced
second confirmation (``_confirm_pii_or_deny``) the read side gets, gated
strictly to this one operation rather than reopening "writes get the PII
gate" as a general rule.

A second, narrower exception on the *read* side: ``pii_already_reviewed``, set
by a caller that can prove the exact content behind this read is content
PrivacyFence itself wrote to this same file, still completely unchanged
since -- e.g. drive_get_file_content reading back a Google Doc whose Drive
``modifiedTime`` still matches the value recorded right after PrivacyFence's
own last write to it (see connectors/drive.py's ``own_write_revisions``).
``pii_categories`` still feeds the audit log's ``pii_detected`` field
faithfully either way; this only suppresses the forced second confirmation,
on the theory that a human already saw this exact content in the write's own
approval popup and re-confirming it on every subsequent re-read is pure
friction, not an extra safety check. The instant anything else modifies the
file -- a human collaborator, another app, a different Claude session --
``modifiedTime`` no longer matches and the very next read goes through the
ordinary PII gate again, no manual revocation needed.

Callers should pass ``pii_scan_text`` whenever a review-gate ``details_text``
mixes structural envelope metadata (an email's From/To headers, a chat
message's channel/sender, a page's author) with the actual content (body,
message text, description) -- that metadata is present on every item
regardless of what it says and will otherwise make the PII gate fire on
essentially every read. ``pii_scan_text`` should carry only the actual
content being read. Same reasoning applies to ``upload_pii_scan_text``.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from .approval_ui import get_approval_ui
from .approval_window_html import NARROW, WIDE
from .approvals import PendingApproval, PendingApprovalRegistry, canonical_key
from .audit_log import APPROVED_LIKE_DECISIONS, AuditEntry, current_week, get_audit_logger
from .auto_accept import (
    TOOL_TO_OPERATION,
    ReviewContext,
    add_auto_accept_rule,
    add_rules_changed_listener,
    describe_rule,
    describe_rule_change,
    describe_rule_short,
    get_auto_accept_evaluator,
    known_rule_names,
    mutate_grants,
    remove_auto_accept_rule,
    suggest_rule_choices,
    suggest_write_rule,
    temp_accept_key,
)
from .pii_detector import (
    PIIAuditMatch,
    describe_match_for_audit,
    detect_pii_categories,
    is_pii_audit_match_details_enabled,
    scan_pii_for_audit,
)
from .resource_grants import apply_grant_removal, apply_grant_upsert, describe_grant_change, resource_type

logger = logging.getLogger(__name__)

# Thin delegations to the pluggable ApprovalUI seam (approval_ui.py), kept as
# plain module-level functions -- rather than called as get_approval_ui().
# show_read_popup(...) inline at each call site below -- so this module
# still calls a bare name for each dialog, same as before this seam existed.
# That's what lets every gate.py test
# monkeypatch e.g. gate.show_read_popup directly without knowing anything
# about ApprovalUI. get_approval_ui() is re-resolved on every call rather
# than bound once at import time, so a later init_approval_ui() swap (a
# different ApprovalUI implementation, e.g. for #55's mobile remote
# approval) takes effect immediately, not just for gate.py calls that happen
# after this module was first imported.


def show_popup(*args, **kwargs):
    return get_approval_ui().show_popup(*args, **kwargs)


def show_read_popup(*args, **kwargs):
    return get_approval_ui().show_read_popup(*args, **kwargs)


def show_pii_confirmation_popup(*args, **kwargs):
    return get_approval_ui().show_pii_confirmation_popup(*args, **kwargs)


def show_rule_confirmation_popup(*args, **kwargs):
    return get_approval_ui().show_rule_confirmation_popup(*args, **kwargs)


# Per-tool NARROW/WIDE card-stack shape, ported verbatim from
# scripts/qa_popup_smoke.py's own _TOOL_LAYOUT (that script's own comment
# explains the handful of tools that render WIDE despite otherwise looking
# like a NARROW case, e.g. slack_send_message/telegram_send_message/
# jira_add_comment, since NARROW has no mechanism at all to show a real
# message/comment body) -- keep the two in sync if either ever changes;
# this is the one gate.py consults for real production calls,
# qa_popup_smoke.py's own copy is for local screenshot iteration only.
_TOOL_LAYOUT: dict[str, str] = {
    "gmail_get_message": WIDE, "gmail_get_thread": WIDE,
    "gmail_download_attachment": WIDE, "drive_download_file": WIDE,
    "confluence_download_attachment": WIDE,
    "salesforce_get_record": WIDE, "salesforce_search": WIDE, "salesforce_run_report": WIDE,
    "jira_get_issue": WIDE, "confluence_get_page": WIDE, "confluence_get_page_by_title": WIDE,
    "telegram_get_messages": WIDE, "telegram_search_messages": WIDE,
    "drive_sheets_get_values": WIDE, "slack_get_channel_history": WIDE,
    "slack_get_thread_replies": WIDE, "slack_search_messages": WIDE,
    "drive_get_file_content": WIDE,
    "gmail_create_draft": WIDE, "gmail_reply_draft": WIDE,
    "drive_sheets_write_range": WIDE, "drive_upload_file": WIDE,
    "jira_create_issue": WIDE, "confluence_create_page": WIDE,
    "calendar_get_event_details": NARROW, "calendar_create_event": NARROW,
    "slack_send_message": WIDE, "telegram_send_message": WIDE, "jira_add_comment": WIDE,
    "slack_create_group_chat": NARROW,
    "gmail_reply_all_draft": WIDE,
    # Parallel to gmail_create_draft/gmail_reply_draft/gmail_reply_all_draft above --
    # same WIDE right-pane body-text preview, just with an extra Attachments row in §1.
    "gmail_create_draft_with_attachments": WIDE, "gmail_reply_draft_with_attachments": WIDE,
    "gmail_reply_all_draft_with_attachments": WIDE,
    "gmail_add_label": NARROW, "gmail_remove_label": NARROW, "gmail_archive_message": NARROW,
    "gmail_create_filter": NARROW, "gmail_update_filter": NARROW, "gmail_create_label": NARROW,
    "drive_write_doc_content": WIDE, "drive_write_file_content": WIDE,
    "drive_docs_edit_content": WIDE,
    "drive_move_file": NARROW, "drive_sheets_add_sheet": NARROW,
    "drive_sheets_rename_sheet": NARROW, "drive_sheets_delete_dimensions": NARROW,
    "drive_sheets_format_range": NARROW, "drive_sheets_insert_dimensions": NARROW,
    "drive_add_comment": WIDE,
    "tasks_create_task": WIDE, "tasks_update_task": WIDE,
    "drive_docs_format_content": NARROW,
    "calendar_update_event": NARROW, "calendar_create_out_of_office": NARROW,
    "calendar_set_working_location": NARROW, "calendar_set_event_visibility": NARROW,
    "contacts_update": NARROW, "contacts_create": NARROW,
    "contacts_add_label": NARROW, "contacts_remove_label": NARROW,
    "jira_update_issue": NARROW, "jira_transition_issue": NARROW,
    "confluence_update_page": WIDE,
    "tasks_complete_task": NARROW,
    "tasks_uncomplete_task": NARROW, "tasks_move_task": NARROW,
    "apps_script_get_content": WIDE, "apps_script_write_content": WIDE,
    "apps_script_get_execution_log": WIDE,
}

# Every dialog this module shows (the approval popup itself, the PII
# confirmation, the "Always allow" rule confirmation) runs on this dedicated
# executor rather than asyncio.to_thread's default pool. That default pool
# (min(32, cpu_count + 4) workers) is shared with every connector's own
# blocking I/O (asyncio.to_thread wraps every *_client.py call the same way
# -- see connectors/slack.py's _fetch for one example). A handful of slow
# calls -- Slack's rate-limit retry sleeping out a Retry-After window is the
# one this was written for -- can occupy every worker in that shared pool,
# so giving popups their own dedicated lane connector I/O can never fill
# still matters regardless of worker count. See
# docs/slack-performance-review.md's R6.
#
# max_workers used to be 1, because gate.py's own _popup_lock (removed at
# P3 -- see module docstring) already serialized every dialog to one at a
# time, so a second worker would have sat idle. It's several now because
# that's no longer true for the web surface: several approvals showing at
# once is P3's whole point (docs/https-connector-refactor-plan.md §6, "New
# coalescing case" / "Job 1... obsolete").
_popup_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="pf-popup")


async def _run_in_popup_executor(func, *args, **kwargs) -> Any:
    """``await`` counterpart to ``asyncio.to_thread`` that runs on
    ``_popup_executor`` instead of the default pool -- see that executor's
    comment for why. ``asyncio.to_thread`` itself has no way to choose a
    different executor, hence this thin wrapper around
    ``loop.run_in_executor`` (which only accepts positional args, so a
    keyword-argument call is bound via ``functools.partial`` first).
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_popup_executor, functools.partial(func, *args, **kwargs))


def _deferred_registry() -> PendingApprovalRegistry | None:
    """The active ApprovalUI's deferred registry, if it has one -- see
    approval_ui.py's ``deferred_registry`` docstring and this module's own.
    Re-resolved on every call, same reasoning as show_read_popup/show_popup
    above: a later init_approval_ui() swap takes effect immediately.

    Also (re-)registers _on_rules_changed (below) with auto_accept.py on
    every call, rather than once at import time: add_rules_changed_listener
    is idempotent (a no-op if already registered), and tests/conftest.py's
    per-test reset clears auto_accept._rules_changed_listeners between
    tests -- a one-time import-time registration would silently stop firing
    after the first test that resets it. Cheap enough (a membership check
    against a short list) to just always do.
    """
    add_rules_changed_listener(_on_rules_changed)
    return get_approval_ui().deferred_registry


# Sentinel returned by _resolve_decision() when a call couldn't be decided
# within the registry's hold window -- distinguishes "genuinely still
# pending" from every real decision string ("accept"/"deny"/"accept_all"/
# "auto_accepted"), none of which this object could ever equal.
_PENDING = object()


async def _resolve_decision(
    *,
    registry: PendingApprovalRegistry | None,
    dedupe_key: str,
    connector: str,
    tool: str,
    gate_kind: str,
    request_id: str,
    summary: str,
    tool_name: str,
    operation_key: str,
    ctx: ReviewContext,
    pii_forces_confirmation: list[str],
    pii_detected: bool,
    pii_categories: list[str],
    claude_reason: str,
    interact: Any,
) -> tuple[Any, str, float | None]:
    """Shared plumbing for the review/popup gate branches: get a decision
    for this call, either by running ``interact`` (see each branch's own
    definition of it) directly, or -- when ``registry`` is not None --
    checking the decision ledger first, then registering (or coalescing
    onto) a pending approval and waiting up to ``registry.hold_window``.

    Returns ``(decision, rule_name, decided_at)``. ``decision`` is one of
    "accept"/"deny"/"accept_all"/"auto_accepted", or the module-level
    ``_PENDING`` sentinel -- in which case ``rule_name`` is instead the
    ``PendingApproval`` the caller should build a pending result from (see
    each branch's own handling immediately after calling this).
    ``decided_at`` is None unless this decision came from the registry
    (either a ledger hit, or a live wait that resolved) -- the "no
    registry" / legacy path never had a separate decide-then-release split
    to time, so there is nothing new to report for it.
    """
    if registry is None:
        decision, rule_name = await interact(None)
        return decision, rule_name, None

    ledger_hit = registry.consume_ledger(dedupe_key)
    if ledger_hit is not None:
        decision, rule_name, decided_at = ledger_hit
        return decision, rule_name, decided_at

    approval, created = registry.register_or_coalesce(
        dedupe_key=dedupe_key, connector=connector, tool=tool, gate_kind=gate_kind,
        request_id=request_id, summary=summary, tool_name=tool_name,
        operation_key=operation_key, review_ctx=ctx, pii_forces_confirmation=bool(pii_forces_confirmation),
        pii_detected=pii_detected, pii_categories=pii_categories, claude_reason=claude_reason,
    )
    if created:
        asyncio.ensure_future(_drive_interaction(registry, approval, interact))

    decided = await registry.wait_async(approval, registry.hold_window)
    if not decided:
        return _PENDING, approval, None
    return approval.final_decision, approval.final_rule_name, approval.decided_at


async def _drive_interaction(registry: PendingApprovalRegistry, approval: PendingApproval, interact: Any) -> None:
    """Runs ``interact`` to completion in the background and finalizes
    ``approval`` with the result -- started once, by whichever call to
    _resolve_decision() actually created ``approval`` (a coalesced caller
    just awaits the same approval; it never starts a second one of these).
    Keeps running -- and this function keeps its promise to eventually call
    finalize() -- even after the original gated_call() invocation has long
    since returned an "approval_pending" result to Claude; that's the whole
    point (docs/https-connector-refactor-plan.md §5.2 point 4-5)."""
    try:
        decision, rule_name = await interact(approval)
    except Exception:
        logger.exception("Approval interaction for %s failed -- resolving as denied", approval.id)
        decision, rule_name = "deny", ""
    registry.finalize(approval.id, decision, rule_name)


def _pending_result(registry: PendingApprovalRegistry, approval: PendingApproval) -> dict[str, Any]:
    """The structured result gated_call() returns to Claude instead of
    blocking further -- docs/https-connector-refactor-plan.md §5.2 point 4."""
    return {
        "status": "approval_pending",
        "approval_id": approval.id,
        "url": registry.approval_url(approval.id),
        "expires_at": datetime.fromtimestamp(approval.expires_at, tz=timezone.utc).isoformat(),
        "message": "This step needs your approval. Open the link above to review and decide.",
    }


def _pop_registry_expirations(registry: PendingApprovalRegistry | None) -> None:
    """Opportunistic TTL sweep -- called at the top of every gated_call()
    that has a registry, mirroring mcp_dispatch.McpDispatcher's own
    _prune_stale pattern rather than running on a background timer. Every
    approval this finds is audited as "expired": one still un-answered past
    its pending TTL (§10.5: "No decision = pending, then expired =
    denied"), or one whose human decision was never reclaimed by a
    re-issued call before the ledger TTL ran out (approvals.py's own
    docstring on why "expired" covers that case too)."""
    if registry is None:
        return
    for approval in registry.pop_expired_events():
        _audit(
            created_at=approval.created_at, request_id=approval.request_id, connector=approval.connector,
            tool=approval.tool, tool_name=approval.tool_name, summary=approval.summary, sender="",
            decision="expired", auto_accept_rule="", pii_detected=approval.pii_detected,
            pii_categories=approval.pii_categories, claude_reason=approval.claude_reason,
        )
    for approval in registry.pop_expired_ledger_events():
        _audit(
            created_at=approval.created_at, request_id=approval.request_id, connector=approval.connector,
            tool=approval.tool, tool_name=approval.tool_name, summary=approval.summary, sender="",
            decision="expired", auto_accept_rule=approval.final_rule_name, pii_detected=approval.pii_detected,
            pii_categories=approval.pii_categories, claude_reason=approval.claude_reason,
        )


def _on_rules_changed() -> None:
    """Subscribed to auto_accept.add_rules_changed_listener() at import
    time (below) -- the live half of §6's "Job 2": any already-pending (not
    yet answered) approval that a rule/grant change now covers is resolved
    as auto_accepted immediately, without waiting for a human to open it,
    exactly like the description in approvals.PendingApprovalRegistry.
    reevaluate_all()'s own docstring. A no-op for a registry-less
    ApprovalUI (see approval_ui.py's deferred_registry docstring) -- there
    is never anything to reevaluate."""
    registry = _deferred_registry()
    if registry is None:
        return
    for approval in registry.reevaluate_all(get_auto_accept_evaluator().should_auto_accept):
        logger.info(
            "Pending approval %s auto-accepted after a rule changed: %s/%s rule=%r",
            approval.id, approval.connector, approval.tool, approval.final_rule_name,
        )


# Set by ipc_server.py around a single dispatched request, for the duration
# of that request only, when the request came in on a connection that
# called privacyfence_begin_unattended_session() and hasn't since called
# privacyfence_end_unattended_session() -- see unattended_scope() below and
# docs/TECHNICAL_REFERENCE.md's "Scheduled / unattended Cowork tasks"
# section. Deliberately NOT a module-level bool: a plain bool would be
# shared across every concurrent request on
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


# Every gated tool's ToolSpec declares a required "reason" param so Claude
# must state, in one sentence, why it's calling the tool -- enforced at the
# MCP schema
# level, not by convention. Carried the same way is_unattended() is: a
# contextvar set once, centrally, in ipc_server.py._call_connector() (which
# pops "reason" out of args before it reaches _dedupe_key -- see that
# module's docstring on why args must stay retry-stable, and its own
# comment at the pop site), not threaded through all ~95 tool call sites
# individually. No connector method signature needs to change for this to
# work; gated_call() and every connector's _auto_audit() read it directly.
_reason_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "privacyfence_reason", default=""
)


def current_reason() -> str:
    return _reason_ctx.get()


class reason_scope:  # noqa: N801 (context-manager-style name, like `freeze_time`)
    """Run the wrapped code with Claude's stated reason for the current tool
    call available via current_reason(). Self-reported and unverified --
    see gated_call()'s claude_reason handling for why it must never be
    rendered or logged as fact."""

    def __init__(self, reason: str) -> None:
        self._reason = reason
        self._token: contextvars.Token | None = None

    def __enter__(self) -> "reason_scope":
        self._token = _reason_ctx.set(self._reason)
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._token is not None:
            _reason_ctx.reset(self._token)


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
    new_info: dict[str, str] | None = None,  # §3 ("What will be provided to
        # Claude") -- real (label, value) pairs a connector builds directly, e.g.
        # calendar_get_event_details's Attendees/Location/Description. Read-only
        # (gate="review") calls only, same reasoning as visibility below. Only consulted by
        # approval_window_html.py's layout="narrow"/"wide" rendering (falls back to a
        # visibility-derived summary when empty).
    preview_tables: list[dict] | None = None,  # WIDE right-pane preview, as
        # structured table(s) instead of a plain-text dump -- each dict is
        # {"caption": str (optional), "headers": [...], "rows": [[...], ...],
        # "footer": str (optional)}. For record/list-shaped "new" content with no
        # fixed field count (a Salesforce record's fields, search results, a
        # message list) -- see approval_window_html.py's _table_html. Valid on both
        # gate="review" and gate="popup" calls (e.g. drive_sheets_write_range's own
        # values-being-written table).
    preview_blocks: list[dict] | None = None,  # WIDE right-pane preview, as
        # an ordered list of {"type": "text"|"field"|"table", ...} blocks -- lets text
        # and tables interleave (e.g. Jira's Reporter field, then its Description
        # paragraph, then its Comments table), which details_text/preview_tables alone
        # can't express. Takes full precedence over both when given -- see
        # approval_window_html.py's build_preview_body_html. Valid on both gate="review"
        # and gate="popup" calls (e.g. jira_create_issue's own Description heading).
    table_only: bool = False,  # When True and preview_tables is non-empty,
        # the WIDE right pane shows *only* the table(s), not details_text too -- for tools
        # whose details_text is a full duplicate of the table's own data (a Salesforce
        # record's plain-text field dump, a Telegram message list) rather than genuinely
        # distinct content. details_text itself is untouched -- the PII scan's
        # default fallback still sees it in full. No effect when preview_blocks is set
        # (blocks already control exactly what renders, no separate "hide text" concept
        # needed) or when preview_tables is empty. Valid on both gate="review" and
        # gate="popup" calls.
    details_text: str = "",       # full text shown inline or via TextEdit
    pii_scan_text: str | None = None,  # content-only text for the PII scan; defaults to details_text
    visibility: dict[str, str] | None = None,  # {label: "allow"|"redact"|"block"} -- the review
        # gate's "AI will receive" checklist, from privacy_filter.category_policy(). Read-only
        # (gate="review") calls only: a popup-gate write already shows exactly what's being sent,
        # since the human is looking at content Claude itself just drafted, not something read
        # from an external source and potentially filtered on the way in.
    content_kind: str = "generic",  # "generic" | "email" -- accepted but currently unused
        # by approval_window_html.py's rendering (see web_approval_ui.WebApprovalUI.show_read_popup's docstring);
        # threaded through from gmail.py. Read-only (gate="review") calls only, same
        # reasoning as visibility above -- a write is Claude's own drafted content, not
        # something this pane needs a per-surface reading affordance for.
    pdf_bytes: bytes = b"",  # Raw PDF bytes for an inline <embed> data URI (see
        # approval_window_html.py's build_preview_body_html), instead of the
        # "[binary content...]" placeholder text.
        # Read-only (gate="review") calls only. The caller (drive.py's _get_file_content) must
        # only ever pass this when category_policy(..., "file_content") == "allow" for the same
        # item -- exactly the one case where details_text/filtered_data's own content already
        # flows through unredacted -- so the human reviewer is never shown a rendered PDF that's
        # richer than what "AI will receive" already discloses Claude gets for this same call.
    preview_bytes: bytes = b"",  # Raw image bytes for an inline <img> data URI, instead of the
        # "[binary content...]" placeholder text or a plain metadata-only popup.
        # Unlike pdf_bytes, carries no AI-visibility parity constraint: only ever set by
        # download/upload-shaped tools (gmail_download_attachment, drive_download_file,
        # drive_upload_file) whose content never reaches Claude at all -- there's nothing an
        # "AI will receive" checklist could disclose for these calls to stay in parity with, so
        # the human reviewer can be shown the full image regardless of any category policy.
        # Valid on both gate="review" and gate="popup" calls (unlike pdf_bytes/content_kind/
        # visibility, which are read-only).
    preview_mime_type: str = "",  # MIME type for preview_bytes, e.g. "image/png" -- used to decide
        # whether approval_window_html.py can render it as an image at all before attempting to.
    upload_pii_scan_text: str | None = None,  # content-only text for a REAL PII scan on the popup
        # (write) direction -- unlike write_content_flags below, a match here forces the same
        # second "Are you sure?" confirmation the review-gate's pii_scan_text does. Only ever set
        # by drive_upload_file: see this function's module docstring for why that one write tool
        # (whose payload can be an arbitrary local file Claude never read) is a deliberate, narrow
        # exception to "writes don't get the real PII gate" -- every other popup-gate call must
        # leave this at its default.
    pii_already_reviewed: bool = False,  # Read-gate-only escape hatch from the PII confirmation
        # step (not from PII detection itself -- see module docstring's "second, narrower
        # exception" paragraph). Set only when the caller can prove nothing has touched this exact
        # content since PrivacyFence's own last write to it. Anything else -- no matching auto-
        # accept rule, a popup-gate write, upload_pii_scan_text -- is unaffected.
    my_email: str = "",
    session_created_ids: set | None = None,
    args: dict | None = None,
) -> Any:
    created_at = time.time()
    request_id = uuid.uuid4().hex[:12]
    operation_key = TOOL_TO_OPERATION.get(tool, f"{connector}.{tool}")
    # Set by ipc_server.py._call_connector() via reason_scope(), from the
    # mandatory "reason" param every gated ToolSpec now declares -- see
    # gate.py's reason_scope docstring. Self-reported, never verified;
    # rendered as such (see approval_window_html.py's "Claude says" block).
    claude_reason = current_reason()

    ctx = ReviewContext(
        connector=connector,
        tool=tool,
        args=args or {},
        raw_data=raw_data,
        my_email=my_email,
        session_created_ids=session_created_ids or set(),
    )
    details = details_text or _default_details(raw_data)
    # No "PrivacyFence — " prefix here -- the "PrivacyFence" kicker line
    # directly above this title in approval_window_html.py already says that;
    # repeating it in the title itself would be redundant.
    popup_title = tool_name
    # NARROW/WIDE card-stack shape, keyed by tool name -- see _TOOL_LAYOUT's
    # own comment. Falls back to NARROW for any tool not yet in that table
    # (shouldn't happen -- it's kept exhaustive against every gated tool --
    # but a missing entry should degrade to the smaller shape, not raise).
    layout = _TOOL_LAYOUT.get(tool, NARROW)
    # Only the review (read) gate scans for PII -- see module docstring.
    # Run via asyncio.to_thread (the default pool, same as every
    # connector's own blocking I/O -- there's no popup-style latency
    # sensitivity here that would justify gate.py's own dedicated
    # _popup_executor): detect_pii_categories/scan_pii_for_audit scan the
    # full details/pii_scan_text synchronously, which measured ~80ms per
    # 1000 messages -- fine for one call, but run inline this used to block
    # every OTHER concurrently-dispatched request on the IPC server's
    # single event loop for that whole duration (see
    # docs/slack-performance-review.md's R9).
    if gate == "review":
        pii_scan_source = details if pii_scan_text is None else pii_scan_text
        pii_categories = await asyncio.to_thread(detect_pii_categories, pii_scan_source)
    else:
        pii_categories = []
    # A separate, deliberately weaker signal for the popup (write) gate:
    # the same local detector, run over Claude's own drafted content, but
    # informational only -- unlike pii_categories above, this never routes
    # through _confirm_pii_or_deny (there is no "possible PII flowed in
    # from an external source" here, so no second confirmation is owed),
    # is never folded into the audit log's pii_detected field (that field's
    # established meaning is specifically about the read-gate scan -- see
    # its docstring in audit_log.py), and renders in the popup with a
    # neutral/informational style, not the red tint+banner that implies a
    # confirmation is coming. Exists as its own signal rather than reusing
    # pii_categories's machinery.
    if gate == "popup":
        write_content_flags = await asyncio.to_thread(detect_pii_categories, details)
    else:
        write_content_flags = []
    # The one deliberate exception to the comment above: drive_upload_file's
    # payload can be external content Claude never read (see module
    # docstring), so when it supplies upload_pii_scan_text this runs the same
    # real scan pii_categories does -- folded into pii_detected below and
    # routed through _confirm_pii_or_deny, unlike write_content_flags.
    if gate == "popup" and upload_pii_scan_text:
        upload_pii_categories = await asyncio.to_thread(detect_pii_categories, upload_pii_scan_text)
    else:
        upload_pii_categories = []
    # PII-refinement trial capture (opt-in, off by default -- see
    # pii_detector.is_pii_audit_match_details_enabled()). Mirrors
    # pii_categories/upload_pii_categories above exactly -- same source
    # text, same gate scoping -- just also carrying the matched text.
    # Computed separately, and only when the setting is on, so a default
    # install never runs this extra scan at all.
    pii_audit_matches: list[PIIAuditMatch]
    if gate == "review" and is_pii_audit_match_details_enabled():
        pii_audit_matches = await asyncio.to_thread(scan_pii_for_audit, pii_scan_source)
    else:
        pii_audit_matches = []
    upload_pii_audit_matches: list[PIIAuditMatch]
    if gate == "popup" and upload_pii_scan_text and is_pii_audit_match_details_enabled():
        upload_pii_audit_matches = await asyncio.to_thread(scan_pii_for_audit, upload_pii_scan_text)
    else:
        upload_pii_audit_matches = []
    # The value everything below actually branches on for "does PII force a
    # popup/confirmation". pii_categories itself (the raw detector result)
    # stays untouched so the audit log's pii_detected field below keeps
    # reporting what was genuinely found, regardless of whether the
    # confirmation step it would normally force got suppressed here.
    pii_forces_confirmation = [] if pii_already_reviewed else pii_categories
    # Single pair of values every audit() call below reads (via closure, not
    # as a parameter -- see audit()'s own body), mirroring pii_detected's
    # own "pii_categories or upload_pii_categories" combination above.
    # Mutually exclusive by construction: gate="review" only ever populates
    # the first of each pair, gate="popup" only the second.
    audit_pii_categories = pii_categories or upload_pii_categories
    audit_pii_matches = pii_audit_matches or upload_pii_audit_matches
    # Request fingerprint: "you've approved this exact (connector, tool,
    # summary) N times this week" -- read directly from the audit log.
    # recent_matches() is a full scan of the current week's JSONL file
    # (measured ~225ms at 50,000 rows), so -- unlike AuditLogger.record()'s
    # own small, established-precedent synchronous appends elsewhere in
    # this module -- it goes through asyncio.to_thread too, for the same
    # R9 reasoning as the PII scans above. get_audit_logger() itself is a
    # cheap singleton lookup, called synchronously first so only the actual
    # file scan runs on the thread.
    audit_logger = get_audit_logger()
    seen_count = await asyncio.to_thread(audit_logger.recent_matches, connector, tool, summary)

    # The deferred-protocol registry, if the active ApprovalUI has one (see
    # this module's own docstring) -- None for a registry-less ApprovalUI,
    # exactly like every gated_call() before P3. dedupe_key mirrors ipc_
    # server.py's/mcp_dispatch.py's own retry-dedupe key shape (already
    # retry-stable -- see approvals.canonical_key's docstring); computed
    # unconditionally, cheaply, since both branches below need it whether or
    # not a registry is active.
    registry = _deferred_registry()
    dedupe_key = canonical_key(connector, tool, args)
    _pop_registry_expirations(registry)

    # Every exit from this function -- including one triggered by an
    # exception nobody anticipated below (a popup call raising, a
    # rule-file write failing) -- must leave exactly one audit entry behind.
    # Without that guarantee, a call that visibly ran and got a real decision
    # from the user can still leave "no matching entry" in the log: a true
    # gap in the trust boundary this module exists to enforce. `audited`
    # tracks whether one of the normal decision branches below already wrote
    # one; the `finally` block below only steps in if none of them did.
    audited = False

    def audit(*, decision: str, auto_accept_rule: str, pii_detected: bool, decided_at: float | None = None) -> None:
        nonlocal audited
        audited = True
        _audit(
            created_at=created_at, request_id=request_id, connector=connector, tool=tool,
            tool_name=tool_name, summary=summary, sender=sender,
            decision=decision, auto_accept_rule=auto_accept_rule, pii_detected=pii_detected,
            pii_categories=audit_pii_categories,
            pii_match_details=_pii_match_details_for_audit(audit_pii_matches, decision),
            claude_reason=claude_reason, decided_at=decided_at,
        )

    try:
        evaluator = get_auto_accept_evaluator()
        auto_ok, matched_rule = evaluator.should_auto_accept(operation_key, ctx)

        if auto_ok and not pii_forces_confirmation and not upload_pii_categories:
            audit(
                decision="auto_accepted", auto_accept_rule=matched_rule,
                pii_detected=bool(pii_categories) or bool(upload_pii_categories),
            )
            logger.info("Auto-accepted: %s/%s rule=%r", connector, tool, matched_rule)
            return filtered_data

        if gate == "review":
            # Every auto-accept rule that plausibly matches this item, up
            # front -- not just a single top-priority hint with the "which
            # one?" question deferred to a second dialog after the click.
            # Each becomes its own "Always allow" button in the popup (see
            # approval_window_html.py's _button_row_html); only ever 2+
            # entries for the four families in auto_accept.SUGGESTION_
            # FAMILIES, at most 1 for every other operation.
            choices = suggest_rule_choices(operation_key, ctx)
            accept_all_choices = [(rule_name, describe_rule_short(rule_name)) for rule_name, _value in choices]

            # Re-check up front: by the time we actually get here, a rule may
            # already cover this item -- created by another concurrently-
            # running approval's own "Always allow" (no more _popup_lock to
            # serialize this against; see module docstring's "Job 2" note).
            # An unreviewed PII match still overrides it either way --
            # pii_forces_confirmation, not pii_categories itself, since
            # pii_already_reviewed's own carve-out (see module docstring) is
            # unaffected by anything decided in the meantime.
            auto_ok, matched_rule = evaluator.should_auto_accept(operation_key, ctx)
            if auto_ok and not pii_forces_confirmation:
                audit(decision="auto_accepted", auto_accept_rule=matched_rule, pii_detected=bool(pii_categories))
                logger.info("Auto-accepted: %s/%s rule=%r", connector, tool, matched_rule)
                return filtered_data

            if is_unattended():
                # No rule matched, or one did but the PII gate still routed
                # this to a human (see module docstring) -- either way,
                # nobody's here to answer a popup. Fail this one step now,
                # synchronously, before any deferred-protocol registration:
                # an unattended session must never receive a pending result
                # either (docs/https-connector-refactor-plan.md §5.4).
                _deny_unattended(audit, connector, tool, pii_categories=pii_forces_confirmation)

            async def _interact(approval: PendingApproval | None) -> tuple[str, str]:
                """The human interaction itself: show the card, force the PII
                confirmation if flagged, and -- if "Always allow" is clicked
                and confirmed -- create the rule right here (not deferred to
                whatever later observes the outcome, so a rule this creates
                covers other pending approvals immediately, per reevaluate_
                all()). Returns (decision, rule_name) -- rule_name is "" for
                a plain accept/deny, else the rule this call just created.
                Runs either awaited directly (registry is None, or the human
                decides within the hold window) or as a background task
                (approvals._drive_interaction) that outlives this specific
                gated_call() invocation -- see _resolve_decision.
                """
                extra = {"approval": approval} if approval is not None else {}
                d, ci = await _run_in_popup_executor(
                    show_read_popup, popup_title, preview or {}, details, accept_all_choices,
                    pii_forces_confirmation, visibility, claude_reason, seen_count, content_kind, pdf_bytes,
                    connector, preview_bytes, preview_mime_type, new_info=new_info,
                    preview_tables=preview_tables, preview_blocks=preview_blocks,
                    table_only=table_only, layout=layout, **extra,
                )
                if d in ("accept", "accept_all") and pii_forces_confirmation:
                    d = await _confirm_pii_or_deny(d, pii_forces_confirmation)
                if d == "accept_all":
                    # Which candidate was clicked -- see approval_window_html.py's
                    # bridge docstring. Bounds-checked rather than trusted:
                    # ci comes back from the popup's own JS bridge, so an
                    # out-of-range or missing index (shouldn't happen against
                    # the real button row, but not a contract this module
                    # needs to trust blindly) degrades to a plain accept
                    # rather than raising.
                    chosen = choices[ci] if ci is not None and 0 <= ci < len(choices) else None
                    if chosen is not None:
                        description = describe_rule(*chosen)
                        confirmed = await _run_in_popup_executor(show_rule_confirmation_popup, description)
                        rn, value = chosen
                        if confirmed:
                            add_auto_accept_rule(operation_key, rn, value)
                            return "accept_all", rn
                    # Cancelled rule creation — this item is still accepted, just once.
                    d = "accept"
                return d, ""

            decision, rule_name, decided_at = await _resolve_decision(
                registry=registry, dedupe_key=dedupe_key, connector=connector, tool=tool, gate_kind="review",
                request_id=request_id, summary=summary, tool_name=tool_name, operation_key=operation_key, ctx=ctx,
                pii_forces_confirmation=pii_forces_confirmation, pii_detected=bool(pii_categories),
                pii_categories=audit_pii_categories, claude_reason=claude_reason, interact=_interact,
            )
            if decision is _PENDING:
                pending_approval = rule_name  # see _resolve_decision's own docstring
                audit(decision="approval_pending", auto_accept_rule="", pii_detected=bool(pii_categories))
                return _pending_result(registry, pending_approval)

            if decision == "auto_accepted":
                audit(
                    decision="auto_accepted", auto_accept_rule=rule_name, pii_detected=bool(pii_categories),
                    decided_at=decided_at,
                )
                logger.info(
                    "Pending approval auto-accepted after a rule changed: %s/%s rule=%r",
                    connector, tool, rule_name,
                )
                return filtered_data

            if decision == "deny":
                audit(decision="rejected", auto_accept_rule="", pii_detected=bool(pii_categories), decided_at=decided_at)
                raise RuntimeError("Request denied by user")

            if decision == "accept_all":
                audit(
                    decision="accepted_via_accept_all", auto_accept_rule=rule_name,
                    pii_detected=bool(pii_categories), decided_at=decided_at,
                )
                logger.info("Always allow: created rule %r for %s", rule_name, operation_key)
                return filtered_data

            audit(decision="approved", auto_accept_rule="", pii_detected=bool(pii_categories), decided_at=decided_at)
            return filtered_data

        else:
            # ── Popup gate: block and show the approval dialog for a write ───
            # No real PII scan here in general -- see module docstring: this
            # gate covers content Claude itself generated for an outbound
            # write, not personal data flowing in from an external source.
            # upload_pii_categories (computed above) is the one narrow
            # exception, drive_upload_file only.
            file_key = temp_accept_key(operation_key, ctx)
            suggestion = suggest_write_rule(operation_key, ctx)
            # At most one entry -- no write operation is a
            # SUGGESTION_FAMILIES multi-candidate case (see gate.py's own
            # module docstring); kept as a list for the same shape as the
            # review branch's `choices` above, so the accept_all handling
            # below reads identically to it.
            choices = [suggestion] if suggestion is not None else []
            accept_all_choices = [(suggestion[0], describe_rule_short(suggestion[0]))] if suggestion else []

            # Same race as the review branch above: a rule may already cover
            # this by the time we get here.
            auto_ok, matched_rule = evaluator.should_auto_accept(operation_key, ctx)
            if auto_ok and not upload_pii_categories:
                audit(decision="auto_accepted", auto_accept_rule=matched_rule, pii_detected=False)
                logger.info("Auto-accepted: %s/%s rule=%r", connector, tool, matched_rule)
                return filtered_data

            if is_unattended():
                _deny_unattended(audit, connector, tool, pii_categories=upload_pii_categories)

            async def _interact(approval: PendingApproval | None) -> tuple[str, str]:
                """Write-gate counterpart to the review branch's own
                _interact -- see that one's docstring."""
                extra = {"approval": approval} if approval is not None else {}
                d, ci = await _run_in_popup_executor(
                    show_popup, popup_title, preview or {}, details, file_key is not None,
                    claude_reason, write_content_flags, seen_count, connector,
                    accept_all_choices, preview_bytes, preview_mime_type,
                    preview_tables=preview_tables, preview_blocks=preview_blocks,
                    table_only=table_only, layout=layout,
                    # upload_forced selects the distinct "write-forced" PII card (see
                    # show_popup's own docstring) -- upload_pii_categories is only
                    # ever non-empty for drive_upload_file's real PII match, the one write
                    # call that forces the same second confirmation the read side gets.
                    upload_forced=bool(upload_pii_categories), **extra,
                )
                if d in ("accept", "accept_all") and upload_pii_categories:
                    d = await _confirm_pii_or_deny(d, upload_pii_categories)
                if d == "accept_all":
                    # See the review branch's matching comment above --
                    # bounds-checked against the popup's own JS bridge
                    # rather than trusted; shouldn't happen against the real
                    # window (the Always-allow button only renders when
                    # accept_all_choices was non-empty), but degrades to a
                    # plain accept rather than falling through to "denied"
                    # below if it somehow does.
                    chosen = choices[ci] if ci is not None and 0 <= ci < len(choices) else None
                    if chosen is None:
                        d = "accept"
                    else:
                        rn, value = chosen
                        # describe_rule_change(), not describe_rule() -- these
                        # five rule names are shared with a read operation key
                        # too (e.g. jira.read_issue), and describe_rule()'s
                        # canned templates are read-direction-only English
                        # ("Jira issue reads in project(s): ..."), which would
                        # mislabel a write's own confirmation.
                        # describe_rule_change() names operation_key explicitly
                        # and reads correctly regardless of direction.
                        description = describe_rule_change("add", operation_key, rn, value)
                        confirmed = await _run_in_popup_executor(show_rule_confirmation_popup, description)
                        if confirmed:
                            add_auto_accept_rule(operation_key, rn, value)
                            return "accept_all", rn
                        # Cancelled rule creation — this item is still accepted, just once.
                        d = "accept"
                return d, ""

            decision, rule_name, decided_at = await _resolve_decision(
                registry=registry, dedupe_key=dedupe_key, connector=connector, tool=tool, gate_kind="popup",
                request_id=request_id, summary=summary, tool_name=tool_name, operation_key=operation_key, ctx=ctx,
                pii_forces_confirmation=upload_pii_categories, pii_detected=bool(upload_pii_categories),
                pii_categories=audit_pii_categories, claude_reason=claude_reason, interact=_interact,
            )
            if decision is _PENDING:
                pending_approval = rule_name  # see _resolve_decision's own docstring
                audit(decision="approval_pending", auto_accept_rule="", pii_detected=bool(upload_pii_categories))
                return _pending_result(registry, pending_approval)

            if decision == "auto_accepted":
                audit(
                    decision="auto_accepted", auto_accept_rule=rule_name, pii_detected=False, decided_at=decided_at,
                )
                return filtered_data

            if decision == "accept_all":
                audit(
                    decision="accepted_via_accept_all", auto_accept_rule=rule_name,
                    pii_detected=bool(upload_pii_categories), decided_at=decided_at,
                )
                logger.info("Always allow: created rule %r for %s", rule_name, operation_key)
                return filtered_data

            if decision == "accept":
                if file_key is not None:
                    # Eligible for the same-file grace window (see module
                    # docstring) -- a plain Allow once on one of these
                    # operations arms it, so Claude's follow-up calls
                    # against this same file don't reprompt for the next
                    # 5 minutes.
                    evaluator.register_temp_accept(operation_key, file_key)
                    audit(
                        decision="accepted_via_temp_session", auto_accept_rule="session_temp_accept",
                        pii_detected=bool(upload_pii_categories), decided_at=decided_at,
                    )
                    logger.info(
                        "Allow once (also armed 5 min grace window): op=%s file=%s (%s, %s)",
                        operation_key, file_key, connector, tool,
                    )
                else:
                    audit(
                        decision="approved", auto_accept_rule="", pii_detected=bool(upload_pii_categories),
                        decided_at=decided_at,
                    )
                return filtered_data

            audit(
                decision="rejected", auto_accept_rule="", pii_detected=bool(upload_pii_categories),
                decided_at=decided_at,
            )
            raise RuntimeError("Request denied by user")
    except asyncio.CancelledError:
        # The bridge asked the daemon to give up on this request (see
        # ipc.py's "cancel" method) -- an expected, named outcome, not a
        # bug, so it gets its own decision rather than falling through to
        # the generic "error" fallback below. If this fires while still
        # waiting on a popup, the dialog itself can't be closed this
        # way and stays open (see ipc.py's own docstring) -- this
        # entry is still the accurate record of what Claude received:
        # nothing, because nothing was waiting anymore by the time (if
        # ever) a human answered it.
        audit(
            decision="cancelled", auto_accept_rule="",
            pii_detected=bool(pii_categories or upload_pii_categories),
        )
        raise
    finally:
        if not audited:
            logger.error(
                "gated_call for %s/%s (request %s) exited without recording a decision "
                "-- recording a fallback 'error' entry so the audit trail has no silent gap",
                connector, tool, request_id,
            )
            audit(decision="error", auto_accept_rule="", pii_detected=bool(pii_categories or upload_pii_categories))


async def propose_rule_change(
    *,
    target: str,          # "rule" | "grant"
    operation: str,        # "add" | "update" | "remove"
    reason: str,
    operation_key: str = "",
    rule_name: str = "",
    value: Any = None,
    old_value: Any = None,
    connector: str = "",
    config_key: str = "",
    resource_id: str = "",
    name: str | None = None,
    tab: str | None = None,
    capabilities: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Bridge-facing counterpart to the popup's own "Always allow" flow:
    propose an add/update/remove to auto_accept_rules or auto_accept_grants,
    but never apply it without a human confirming via the same
    show_rule_confirmation_popup() dialog gated_call() uses for "Always
    allow" -- this is a "gate only" write path: config changes go through
    the approval gate without a real tool call behind them. Unlike
    gated_call(), there's no underlying tool call or auto-accept
    short-circuit here: every proposal reaches a human (or is denied
    outright in an unattended session, same as gated_call), even if an
    identical rule/grant already exists -- confirming again is cheap,
    silently no-op'ing a request Claude explicitly made is more surprising.

    Raises RuntimeError if the user declines, or if called on an unattended
    connection (see is_unattended()) -- mirroring gated_call's own "deny ==
    exception" contract so a declined proposal surfaces to Claude as a clear
    tool error rather than a result it has to remember to check.
    """
    if target == "rule":
        if rule_name not in known_rule_names():
            raise ValueError(
                f"Unknown auto-accept rule: {rule_name!r}. See privacyfence_list_auto_accept_rules "
                "or docs/TECHNICAL_REFERENCE.md's Auto-accept rules tables for valid rule names."
            )
        description = describe_rule_change(operation, operation_key, rule_name, value, old_value)
    elif target == "grant":
        rt = resource_type(connector, config_key)
        if rt is None:
            raise ValueError(f"Unknown grant resource type: {connector}.{config_key}")
        description = describe_grant_change(
            operation, rt, resource_id, name=name, tab=tab, capabilities=capabilities
        )
    else:
        raise ValueError(f"Unknown target: {target!r}")
    if operation not in ("add", "update", "remove"):
        raise ValueError(f"Unknown operation: {operation!r}")

    created_at = time.time()
    request_id = uuid.uuid4().hex[:12]
    summary = f"Proposed {operation} ({target}): {description}"

    if is_unattended():
        _audit(
            created_at=created_at, request_id=request_id, connector=connector or target, tool="",
            tool_name="", summary=summary, sender="", decision="denied_unattended",
            auto_accept_rule="", pii_detected=False, claude_reason=reason,
        )
        raise RuntimeError(
            "Request denied: this connection is in an unattended session, so a config change "
            "can't be confirmed without a human present."
        )

    confirmed = await _run_in_popup_executor(show_rule_confirmation_popup, description)

    if not confirmed:
        _audit(
            created_at=created_at, request_id=request_id, connector=connector or target, tool="",
            tool_name="", summary=summary, sender="", decision="rejected",
            auto_accept_rule="", pii_detected=False, claude_reason=reason,
        )
        raise RuntimeError("Request denied by user")

    if target == "rule":
        if operation == "remove":
            changed = remove_auto_accept_rule(operation_key, rule_name, value)
        else:
            if operation == "update" and old_value is not None:
                remove_auto_accept_rule(operation_key, rule_name, old_value)
            add_auto_accept_rule(operation_key, rule_name, value)
            changed = True
        applied_decision = "rule_removed_via_bridge_proposal" if operation == "remove" else "rule_changed_via_bridge_proposal"
        applied_rule_name = rule_name
    else:
        if operation == "remove":
            changed = mutate_grants(lambda cfg: apply_grant_removal(cfg, rt, resource_id, tab))
        else:
            changed = mutate_grants(
                lambda cfg: apply_grant_upsert(
                    cfg, rt, resource_id, name=name, tab=tab, capabilities=capabilities
                )
            )
        applied_decision = "grant_removed_via_bridge_proposal" if operation == "remove" else "grant_changed_via_bridge_proposal"
        applied_rule_name = resource_id

    # A confirmed "remove" can still be a no-op (the rule/grant named didn't
    # actually match anything, e.g. Claude proposed removing a value that
    # was already gone) -- `changed` already tells the two branches above
    # apart correctly, but the decision string didn't consult it at all
    # before this, so the audit log claimed a removal/change happened even
    # when config verifiably didn't change. "confirmed" (this function's
    # own return value) still means "the human said yes", so this stays
    # distinct from "rejected".
    decision = applied_decision if changed else "bridge_proposal_no_op"

    _audit(
        created_at=created_at, request_id=request_id, connector=connector or target, tool="",
        tool_name="", summary=summary, sender="", decision=decision,
        auto_accept_rule=applied_rule_name, pii_detected=False, claude_reason=reason,
    )
    logger.info(
        "Bridge-proposed %s %s confirmed%s: %s",
        operation, target, " and applied" if changed else " but was a no-op", description,
    )
    return {"confirmed": True, "changed": changed, "description": description}


def _deny_unattended(audit, connector: str, tool: str, *, pii_categories: list[str]) -> None:
    """Fail-fast path for unattended sessions: same outcome as a human
    clicking Deny, minus the popup nobody's there to answer -- see
    unattended_scope() above and docs/TECHNICAL_REFERENCE.md's
    "Scheduled / unattended Cowork tasks" section.

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


def _pii_match_details_for_audit(matches: list[PIIAuditMatch], decision: str) -> str:
    """Build the audit log's pii_match_details field (see AuditEntry's own
    docstring in audit_log.py for the full contract). ``matches`` is already
    [] whenever pii_detection.audit_match_details is off or nothing was
    detected -- see gated_call's pii_audit_matches/upload_pii_audit_matches
    -- so this function doesn't need to check that setting itself.

    A request that never actually released its content (``decision`` isn't
    one of APPROVED_LIKE_DECISIONS -- rejected, denied_unattended, error)
    gets a fixed placeholder, never the matched text: nothing was gained by
    showing the content to Claude/the destination in that case, so there's
    nothing to gain from recording it here either, only cost. Otherwise:
    one "<category>: <text>" entry per distinct category, text taken from
    the first match of that category (further occurrences of the same
    category in one item aren't materially more informative) and redacted
    per pii_detector.describe_match_for_audit() for value-bearing
    categories.
    """
    if not matches:
        return ""
    if decision not in APPROVED_LIKE_DECISIONS:
        return "User confirmed: details hidden"
    seen: set[str] = set()
    parts: list[str] = []
    for m in matches:
        if m.category in seen:
            continue
        seen.add(m.category)
        parts.append(f"{m.category}: {describe_match_for_audit(m.category, m.text)}")
    return "; ".join(parts)


async def _confirm_pii_or_deny(decision: str, pii_categories: list[str]) -> str:
    """Extra gate for content the PII detector flagged: forces one more
    explicit confirmation on top of the popup's own Allow once/Always allow,
    declining which is treated as a deny of the whole request."""
    confirmed = await _run_in_popup_executor(show_pii_confirmation_popup, pii_categories)
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
    pii_detected=False, pii_categories=None, pii_match_details="", claude_reason="", decided_at=None,
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
            pii_categories=pii_categories or [],
            pii_match_details=pii_match_details,
            claude_reason=claude_reason,
            # Set only when this decision came from the deferred-approval
            # ledger (a real human click that happened separately from --
            # and possibly long after -- the invocation now releasing on the
            # strength of it) or a live rules-changed auto-resolution while
            # genuinely pending. Empty for the ordinary decided-inline case,
            # where "when the human decided" and "when this entry was
            # written" are the same instant and a second timestamp would say
            # nothing new. See docs/https-connector-refactor-plan.md §5.4.
            decided_at=(
                datetime.fromtimestamp(decided_at, tz=timezone.utc).isoformat() if decided_at else ""
            ),
        ))
    except Exception as exc:
        logger.warning("Audit log write failed: %s", exc)
