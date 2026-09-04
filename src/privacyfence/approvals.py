"""Deferred-approval registry: the domain object P3 adds on top of gate.py's
existing decision loop (docs/https-connector-refactor-plan.md §5-§6).

Principal dimension (P9, not P6/P7/P8): ``approval_ui.py``'s own module
docstring already promised this -- "``WebApprovalUI`` stays a true
singleton deliberately ... in org mode one instance still serves every
principal (its ``PendingApprovalRegistry`` gains the principal dimension
internally instead)" -- but nothing actually needed it until now, since
``/approvals`` was never mounted in org mode through P8 (web/server.py's
own module docstring). web/routes_org_approvals.py (P9) is what finally
reaches this registry from more than one principal at once, so every
``PendingApproval`` now stamps ``principal_id`` at registration time (from
``current_principal()`` -- the same contextvar pattern every other
per-principal registry in this codebase already uses, so gate.py's own
call sites into ``register_or_coalesce``/``register_confirm`` need no
signature change), and every read/write method below takes an optional
``principal_id`` to filter or authorize against. ``None`` (the default
everywhere) means "no filter" -- gate.py's own internal calls, and every
pre-P9 caller/test, keep seeing every approval regardless of principal,
which is also exactly correct for local mode's single implicit principal.
web/routes_org_approvals.py is the one real caller that ever passes a real
``principal_id``, and it does so on every method it calls (§10.5: "every
approval, card, preview, decision ... read is authorized against
current_principal()").

The coalescing/ledger key also gained a principal dimension for the same
reason web/mcp_dispatch.py's retry-dedupe cache did at P7: ``(connector,
tool, canonical(args))`` alone collides across two principals who happen to
call the same tool with the same arguments, which P7's own fix note (see
docs/https-connector-refactor-plan.md's P7 section) already named as
exactly this codebase's recurring failure mode once a second principal
becomes real. ``_by_key`` is keyed on ``(principal_id, dedupe_key)`` here
for the same reason.

Two kinds of caller reach this module:

- gate.py, for the *main* gate decision of a review/popup-gated call -- see
  ``register_or_coalesce``/``consume_ledger``/``finalize``/``wait_async``,
  which together implement the deferred protocol: register a pending
  approval, wait up to ``hold_window`` seconds, and if nobody decided in
  time, let the caller return a structured "approval_pending" result instead
  of blocking further. A later, identical call finds the decision via
  ``consume_ledger`` and releases without a second prompt.
- web_approval_ui.py, for *every* card and confirmation dialog it shows
  (whether or not it's the "main" decision for some gated call) -- see
  ``register_confirm``/``answer``/``get``/``list_pending``, which is the
  multi-item store P1's own module docstring already named as this module's
  future job ("``current()`` below is a single slot, not a registry... it
  becomes a real per-principal registry (approvals.py) in P3").

One registry instance backs both uses (``WebApprovalUI.deferred_registry``),
so a card gate.py is waiting on and a card a human is looking at in the
browser are always the same object.

Two decision layers, not one, because a single gated call can involve more
than one human interaction (the main popup, then possibly a PII
"are you sure?" confirmation, then possibly an "Always allow" rule
confirmation -- see gate.py's own module docstring):

- ``answer()`` resolves one *UI step* -- whatever card or confirmation is
  currently on screen. It never touches the ledger; it only wakes up
  whichever thread is blocked showing that one dialog (WebApprovalUI's
  ``show_popup``/``show_read_popup``/the two confirmation methods, exactly
  as blocking as they were pre-P3 -- see that module).
- ``finalize()`` resolves the *whole approval* -- the outcome gate.py's own
  interaction driver arrives at after however many UI steps it took. Only
  finalize() writes the decision ledger (keyed by
  ``(connector, tool, canonical(args))``, single-use for writes per D3) and
  wakes ``wait_async()`` -- the thing gate.py's hold window actually awaits.

Both events are ``threading.Event`` rather than ``asyncio.Event``: this
registry is touched from the asyncio event loop (gate.py, the web routes'
request handlers) *and* from plain OS threads (WebApprovalUI's blocking
calls run on gate.py's ``_popup_executor``; a rules-changed broadcast can
fire from any thread that called ``auto_accept.reload_rules()``, e.g. the
AppKit main thread). ``threading.Event`` is safe to set/wait from any of
them; ``wait_async()`` bridges back into the event loop via
``asyncio.to_thread`` for the one caller (gate.py) that needs a non-blocking
await.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .principal import current_principal

logger = logging.getLogger(__name__)

# Defaults per D3 (docs/https-connector-refactor-plan.md §15) -- "what P3's
# beta measures against", all overridable by daemon_main.py from
# settings.yaml's web.approvals.* keys.
DEFAULT_HOLD_WINDOW_SECONDS = 30.0
DEFAULT_PENDING_TTL_SECONDS = 15 * 60.0
DEFAULT_LEDGER_TTL_SECONDS = 5 * 60.0
DEFAULT_MAX_PENDING = 50

# Every UI-step decision a card/confirmation can resolve to -- the same
# vocabulary approval_popup.py's native bridge and approval_window_html.py's
# own JS already use. "auto_accepted" is never produced by a UI step (no
# button says that); it's finalize()'s own sentinel for "a rule appeared
# that already covers this, so no human ever needed to answer" -- gate.py's
# interaction driver returns it directly to finalize() without going through
# answer() at all. See gate.py's module docstring.
CARD_RESULTS = ("accept", "deny", "accept_all")
CONFIRM_RESULTS = ("confirm", "cancel")


class TooManyPendingApprovalsError(RuntimeError):
    """Raised by register_or_coalesce() when the per-registry cap
    (``max_pending``) is already reached -- fail-closed rather than queueing
    (docs/https-connector-refactor-plan.md §7.1): "reject further gated
    calls with a 'too many pending approvals' error... the natural backstop
    against a runaway agent"."""


def canonical_key(connector: str, tool: str, args: dict[str, Any] | None) -> str:
    """The decision-ledger / coalescing key: ``(connector, tool,
    canonical(args))``. Same shape ipc_server.py's and mcp_dispatch.py's own
    retry-dedupe keys already use -- already retry-stable, since every
    caller into gate.gated_call() has "reason" popped out of ``args`` before
    it gets here (see gate.py's ``reason_scope`` docstring), so re-issuing
    the identical tool call always reproduces the identical key.
    """
    return f"{connector}:{tool}:{json.dumps(args or {}, sort_keys=True, default=str)}"


def is_pending_result(result: Any) -> bool:
    """True for exactly the shape gate.py's ``_pending_result()`` returns
    (``{"status": "approval_pending", ...}``) -- the one gated_call() result
    shape that is not a real answer yet. Both ipc_server.py's and
    mcp_dispatch.py's own retry-dedupe caches (a pre-P3 mechanism, built
    for "reuse the answer to an identical in-flight or just-finished call")
    check this before caching a completed result: caching a *pending*
    result would mean the identical re-call Claude is supposed to make to
    actually collect the decision (§5.2 point 6) just gets handed the same
    stale "still pending" blob back for up to that cache's own TTL, instead
    of ever reaching gate.gated_call() again to check the decision ledger.
    """
    return isinstance(result, dict) and result.get("status") == "approval_pending"


def _iso(ts: float | None) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


@dataclass
class PendingApproval:
    id: str
    kind: str                      # "card" | "confirm"
    # P9: whichever principal's request context was active at registration
    # time (see module docstring) -- "local" for every approval in local
    # mode, unchanged from before this field existed.
    principal_id: str = field(default_factory=lambda: current_principal().id)
    connector: str = ""
    tool: str = ""
    gate_kind: str = ""            # "review" | "popup" -- "" for a bare confirm dialog
    request_id: str = ""
    summary: str = ""
    tool_name: str = ""
    dedupe_key: str | None = None  # None for a confirm dialog: never coalesced, never ledgered
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0        # pending-TTL deadline
    html: str = ""
    # Re-evaluation context for the rules-changed broadcast (§6, Job 2).
    operation_key: str | None = None
    review_ctx: Any = None
    pii_forces_confirmation: bool = False
    pii_detected: bool = False
    pii_categories: list[str] = field(default_factory=list)
    claude_reason: str = ""

    # UI-step state -- see module docstring.
    event: threading.Event = field(default_factory=threading.Event, repr=False)
    result: str | None = None
    chosen_index_result: int | None = None

    # Approval-level (finalize) state -- see module docstring.
    finalize_event: threading.Event = field(default_factory=threading.Event, repr=False)
    final_decision: str | None = None       # "accept" | "deny" | "accept_all" | "auto_accepted" | "expired"
    # The one piece of extra context the final decision can carry: the
    # auto-accept rule name, for "accept_all" (a rule was just created) and
    # "auto_accepted" (an existing rule matched) alike -- "" otherwise. A
    # single string slot, not gate.py's UI-level button index: by the time
    # an interaction finalizes, any button-index bookkeeping it needed has
    # already been resolved into a rule name (or discarded), so there's
    # nothing else worth carrying here. See gate.py's own module docstring.
    final_rule_name: str = ""
    decided_at: float | None = None
    ledger_expires_at: float | None = None
    ledger_consumed: bool = False

    def answer(self, result: str, chosen_index: int | None = None) -> bool:
        """Resolve this UI step. Idempotent: the first answer wins (mirrors
        WebApprovalUI's pre-P3 ``resolve()``/§7.1's "first accepted decision
        wins")."""
        if self.event.is_set():
            return False
        self.result = result
        self.chosen_index_result = chosen_index
        self.event.set()
        return True

    def is_finalized(self) -> bool:
        return self.finalize_event.is_set()

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "connector": self.connector,
            "tool": self.tool,
            "tool_name": self.tool_name,
            # "review" (read) | "popup" (write) | "" for a bare confirm
            # dialog -- safe to expose unconditionally (it names a category,
            # never gated content) and is exactly the "direction" field
            # web_shell.py's notification-detail allowlist needs (P5,
            # docs/approval-list-ui-ux.md §4.3): never derived from
            # ``summary``, which is the one field that can carry real gated
            # content (see that field's own docstring below).
            "gate_kind": self.gate_kind,
            # The row's own title/content line -- can carry real gated data
            # (an event title, a contact name, a document title -- see
            # gate.py's call sites). Only ever shown by a consumer that's
            # allowed to at its own detail level; web_shell.py's notification
            # body never reads it below "detailed".
            "summary": self.summary,
            "created_at": _iso(self.created_at),
            "expires_at": _iso(self.expires_at),
            "decided": self.is_finalized(),
        }


class PendingApprovalRegistry:
    """See module docstring. Every public method is safe to call from any
    thread; internal state is protected by one ``threading.Lock`` (cheap,
    dict-sized critical sections only -- never held across a wait)."""

    def __init__(
        self,
        *,
        hold_window: float = DEFAULT_HOLD_WINDOW_SECONDS,
        pending_ttl: float = DEFAULT_PENDING_TTL_SECONDS,
        ledger_ttl: float = DEFAULT_LEDGER_TTL_SECONDS,
        max_pending: int = DEFAULT_MAX_PENDING,
        base_url: str | None = None,
    ) -> None:
        self.hold_window = hold_window
        self.pending_ttl = pending_ttl
        self.ledger_ttl = ledger_ttl
        self.max_pending = max_pending
        self.base_url = base_url
        self._lock = threading.Lock()
        self._pending: dict[str, PendingApproval] = {}
        # (principal_id, dedupe_key) -> approval id -- see module docstring's
        # own note on why principal_id is folded into this key, not just
        # dedupe_key alone.
        self._by_key: dict[tuple[str, str], str] = {}

    def set_base_url(self, base_url: str | None) -> None:
        self.base_url = base_url

    def approval_url(self, approval_id: str) -> str | None:
        if not self.base_url:
            return None
        return f"{self.base_url}/approvals/{approval_id}"

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #

    def register_or_coalesce(
        self,
        *,
        dedupe_key: str,
        connector: str,
        tool: str,
        gate_kind: str,
        request_id: str,
        summary: str = "",
        tool_name: str = "",
        operation_key: str | None = None,
        review_ctx: Any = None,
        pii_forces_confirmation: bool = False,
        pii_detected: bool = False,
        pii_categories: list[str] | None = None,
        claude_reason: str = "",
    ) -> tuple[PendingApproval, bool]:
        """Returns ``(approval, created)``. ``created=False`` means an
        identical, not-yet-finalized approval was already outstanding for
        this exact ``(connector, tool, args)`` and the caller is coalescing
        onto it (§6's "New coalescing case") -- the caller must not show a
        second card, only await the existing one.

        Raises TooManyPendingApprovalsError if the cap is reached and this
        is a genuinely new key (never raised for a coalescing hit -- that
        doesn't grow the pending set).
        """
        principal_id = current_principal().id
        key = (principal_id, dedupe_key)
        with self._lock:
            self._expire_stale_locked()
            existing_id = self._by_key.get(key)
            if existing_id is not None:
                existing = self._pending.get(existing_id)
                if existing is not None and not existing.is_finalized():
                    return existing, False
            live = sum(1 for a in self._pending.values() if not a.is_finalized())
            if live >= self.max_pending:
                raise TooManyPendingApprovalsError(
                    f"Too many approvals are already pending ({self.max_pending}) -- decide some "
                    "or wait for them to expire before issuing more."
                )
            now = time.time()
            approval = PendingApproval(
                id=uuid.uuid4().hex, kind="card", principal_id=principal_id,
                connector=connector, tool=tool, gate_kind=gate_kind,
                request_id=request_id, summary=summary, tool_name=tool_name, dedupe_key=dedupe_key,
                created_at=now, expires_at=now + self.pending_ttl,
                operation_key=operation_key, review_ctx=review_ctx,
                pii_forces_confirmation=pii_forces_confirmation, pii_detected=pii_detected,
                pii_categories=list(pii_categories or []), claude_reason=claude_reason,
            )
            self._pending[approval.id] = approval
            self._by_key[key] = approval.id
            return approval, True

    def register_confirm(self) -> PendingApproval:
        """A PII/"Always allow" confirmation dialog -- never coalesced
        (``dedupe_key=None``), never subject to the pending cap (it's a
        short-lived follow-up to a card someone is already looking at, not
        a new gated call), never ledgered."""
        with self._lock:
            now = time.time()
            approval = PendingApproval(
                id=uuid.uuid4().hex, kind="confirm", created_at=now, expires_at=now + self.pending_ttl,
            )
            self._pending[approval.id] = approval
            return approval

    def set_html(self, approval_id: str, html: str) -> None:
        approval = self._pending.get(approval_id)
        if approval is not None:
            approval.html = html

    # ------------------------------------------------------------------ #
    # Decisions
    # ------------------------------------------------------------------ #

    def answer(
        self, approval_id: str, result: str, chosen_index: int | None = None, *, principal_id: str | None = None,
    ) -> bool:
        """Resolve one UI step -- called by web/routes_approvals.py's/
        web/routes_org_approvals.py's decide endpoint when a human clicks a
        button. See PendingApproval.answer.

        ``principal_id``, when given (web/routes_org_approvals.py always
        passes one -- see module docstring), rejects a decision on an
        approval belonging to a *different* principal exactly as if it
        didn't exist -- §10.5's "every ... decision ... is authorized
        against current_principal()", defense in depth on top of the
        approval id's own 128 bits of entropy."""
        with self._lock:
            approval = self._pending.get(approval_id)
        if approval is None:
            return False
        if principal_id is not None and approval.principal_id != principal_id:
            return False
        return approval.answer(result, chosen_index)

    def finalize(self, approval_id: str, decision: str, rule_name: str = "") -> bool:
        """Resolve the whole approval -- called once, by gate.py's own
        interaction driver, when the full multi-step dance (main popup, any
        PII/rule confirmation) has concluded, and by reevaluate_all() below
        for a rule that appeared while nobody had answered yet. Writes the
        decision ledger (single-use for gate_kind="popup", per D3) and
        wakes wait_async(). Idempotent, same "first decision wins" contract
        as answer()."""
        with self._lock:
            approval = self._pending.get(approval_id)
            if approval is None or approval.is_finalized():
                return False
            approval.final_decision = decision
            approval.final_rule_name = rule_name
            approval.decided_at = time.time()
            if approval.dedupe_key is not None:
                approval.ledger_expires_at = approval.decided_at + self.ledger_ttl
            approval.finalize_event.set()
            return True

    def consume_ledger(self, dedupe_key: str) -> tuple[str, str, float] | None:
        """A re-issued (or coalesced-and-since-finalized) identical call's
        first stop: is there already a decision on file for this exact
        ``(connector, tool, args)``, *for the calling principal*? Returns
        ``(decision, rule_name, decided_at)`` or None. Single-use entries
        (writes) are removed on the read that consumes them; read-gate
        entries stay reusable until ``ledger_ttl`` (§5.4: "Read decisions...
        stay TTL-bounded and reusable... unchanged").

        Scoped to ``current_principal()`` implicitly (module docstring) --
        gate.py's own call site needs no change, and this is the one method
        where that scoping isn't optional: without it, two principals
        issuing the identical tool call with identical arguments would
        share one ledger entry, releasing one principal's approved decision
        to the other's re-issued call (P9's own fix for the P7-precedented
        cross-principal dedupe-key collision -- see module docstring)."""
        principal_id = current_principal().id
        key = (principal_id, dedupe_key)
        with self._lock:
            self._expire_stale_locked()
            approval_id = self._by_key.get(key)
            if approval_id is None:
                return None
            approval = self._pending.get(approval_id)
            if approval is None or not approval.is_finalized() or approval.ledger_consumed:
                return None
            if approval.ledger_expires_at is not None and time.time() > approval.ledger_expires_at:
                return None
            if approval.gate_kind == "popup":
                approval.ledger_consumed = True
                del self._by_key[key]
                self._pending.pop(approval_id, None)
            return approval.final_decision, approval.final_rule_name, approval.decided_at

    # ------------------------------------------------------------------ #
    # Waiting (gate.py's hold window)
    # ------------------------------------------------------------------ #

    async def wait_async(self, approval: PendingApproval, timeout: float) -> bool:
        """True if ``approval`` was finalized within ``timeout`` seconds,
        else False (still pending -- see gate.py's own use of this)."""
        import asyncio

        return await asyncio.to_thread(approval.finalize_event.wait, timeout)

    # ------------------------------------------------------------------ #
    # Read side -- web/routes_approvals.py, privacyfence_await_approval
    # ------------------------------------------------------------------ #

    def get(self, approval_id: str, *, principal_id: str | None = None) -> PendingApproval | None:
        """``principal_id``, when given, makes a mismatched approval
        indistinguishable from a nonexistent one -- the authorization check
        web/routes_org_approvals.py's card/preview routes and
        web/mcp_dispatch.py's ``privacyfence_await_approval`` (P9) rely on.
        ``None`` (every pre-P9 caller, and gate.py's own internal use) means
        "no filter", unchanged from before this parameter existed."""
        with self._lock:
            approval = self._pending.get(approval_id)
        if approval is None:
            return None
        if principal_id is not None and approval.principal_id != principal_id:
            return None
        return approval

    def list_pending(self, principal_id: str | None = None) -> list[PendingApproval]:
        """Every card/confirmation not yet answered at the UI-step level --
        newest first. Used by web/routes_approvals.py's/web/routes_org_
        approvals.py's list view (§7.1). ``principal_id`` restricts the
        result to that principal's own approvals only (P9) -- ``None``
        (local mode's own call, and every pre-P9 caller) returns every
        approval regardless of principal, correct for local mode's single
        implicit principal and unchanged from before this parameter
        existed."""
        with self._lock:
            items = [
                a for a in self._pending.values()
                if not a.event.is_set() and (principal_id is None or a.principal_id == principal_id)
            ]
        items.sort(key=lambda a: a.created_at, reverse=True)
        return items

    def await_status(self, approval_id: str, *, principal_id: str | None = None) -> str:
        """One of "approved"/"denied"/"pending"/"expired"/"unknown" -- the
        vocabulary privacyfence_await_approval reports back to Claude (§5.2
        point 7): status only, never content. ``principal_id`` is P9's own
        cross-principal check (§10.5) -- an id belonging to another
        principal reads as "unknown", never leaking that it exists at all."""
        approval = self.get(approval_id, principal_id=principal_id)
        if approval is None:
            return "unknown"
        if not approval.is_finalized():
            return "pending"
        if approval.final_decision == "expired":
            return "expired"
        if approval.final_decision == "deny":
            return "denied"
        return "approved"  # accept | accept_all | auto_accepted

    # ------------------------------------------------------------------ #
    # Rules-changed re-evaluation broadcast (§6, Job 2)
    # ------------------------------------------------------------------ #

    def reevaluate_all(self, should_auto_accept: Callable[[str, Any], tuple[bool, str]]) -> list[PendingApproval]:
        """Called whenever the live rule/grant set changes (see gate.py's
        subscription to auto_accept.add_rules_changed_listener). Any
        not-yet-answered card whose operation is now covered by a rule --
        and whose PII gate isn't independently forcing a human look, per
        pii_forces_confirmation -- is finalized as "auto_accepted" right
        here, without waiting for a human to open it. Returns the list of
        approvals this call resolved, so the caller (gate.py) can audit each
        one and wake anything still awaiting it.

        Scoped to ``current_principal()`` (P9): ``should_auto_accept`` is
        itself one principal's own evaluator (auto_accept.py's own
        ``_REGISTRY``, resolved via ``current_principal()`` inside gate.py's
        ``_on_rules_changed`` at the moment *that* principal's rules
        changed), so re-evaluating another principal's pending cards
        against it would apply the wrong rule set entirely -- not merely a
        privacy leak but a correctness bug, the same class this module's
        own dedupe-key fix (see module docstring) exists to close.
        """
        principal_id = current_principal().id
        resolved: list[PendingApproval] = []
        with self._lock:
            candidates = [
                a for a in self._pending.values()
                if a.kind == "card" and not a.is_finalized() and not a.event.is_set()
                and a.operation_key is not None and not a.pii_forces_confirmation
                and a.principal_id == principal_id
            ]
        for approval in candidates:
            try:
                auto_ok, matched_rule = should_auto_accept(approval.operation_key, approval.review_ctx)
            except Exception:
                logger.exception("should_auto_accept raised during rules-changed re-evaluation")
                continue
            if not auto_ok:
                continue
            if self.finalize(approval.id, "auto_accepted", matched_rule):
                resolved.append(approval)
        return resolved

    # ------------------------------------------------------------------ #
    # Expiry -- opportunistic, mirroring mcp_dispatch.McpDispatcher's own
    # _prune_stale pattern (called at the top of every registration/lookup,
    # not on a background timer).
    # ------------------------------------------------------------------ #

    def _expire_stale_locked(self) -> None:
        """Must be called with self._lock held. Frees dedupe keys whose
        approval expired (pending TTL) or whose ledger entry did (ledger
        TTL) -- so a fresh call for the same key gets a clean new approval
        instead of perpetually finding a dead one. Does NOT delete
        unconsumed, not-yet-expired PendingApproval rows -- pop_expired_
        events()/pop_expired_ledger_events() (below) are what actually
        drain those, since each needs to become exactly one "expired" audit
        entry and this method is called far too often (every registration)
        to be that list's only producer."""
        now = time.time()
        for key, approval_id in list(self._by_key.items()):
            approval = self._pending.get(approval_id)
            if approval is None:
                del self._by_key[key]
                continue
            if not approval.is_finalized() and now > approval.expires_at:
                del self._by_key[key]
            elif approval.is_finalized() and approval.ledger_expires_at is not None and now > approval.ledger_expires_at:
                del self._by_key[key]

    def pop_expired_events(self) -> list[PendingApproval]:
        """Un-finalized approvals whose pending TTL has lapsed -- gate.py
        calls this opportunistically and audits each as "expired" (§10.5:
        "No decision = pending, then expired = denied. Never
        auto-approved."). Each is reported at most once (finalized here, as
        "expired", so a later human click on the same stale link is
        rejected the same way any late decision is)."""
        now = time.time()
        expired: list[PendingApproval] = []
        with self._lock:
            for approval in self._pending.values():
                if approval.is_finalized() or now <= approval.expires_at:
                    continue
                approval.final_decision = "expired"
                approval.decided_at = now
                approval.finalize_event.set()
                if approval.dedupe_key is not None:
                    self._by_key.pop((approval.principal_id, approval.dedupe_key), None)
                expired.append(approval)
        return expired

    def pop_expired_ledger_events(self) -> list[PendingApproval]:
        """Finalized approvals whose decision was never reclaimed (no
        re-issued call ever consumed it) before the ledger TTL lapsed --
        the "decided but nobody came back for it" case. gate.py audits each
        of these as "expired" too (see this module's own docstring on why
        there is no separate vocabulary for it: the human's real decision
        was already made, but nothing was ever released on the strength of
        it, which is exactly what "expired" already means for a pending
        approval that ran out the clock)."""
        now = time.time()
        events: list[PendingApproval] = []
        with self._lock:
            for approval in list(self._pending.values()):
                if (
                    approval.is_finalized() and not approval.ledger_consumed
                    and approval.final_decision != "expired"
                    and approval.ledger_expires_at is not None and now > approval.ledger_expires_at
                ):
                    approval.ledger_consumed = True
                    if approval.dedupe_key is not None:
                        self._by_key.pop((approval.principal_id, approval.dedupe_key), None)
                    self._pending.pop(approval.id, None)
                    events.append(approval)
        return events
