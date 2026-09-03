"""Generalized "block a worker thread on a human's choice" helper
(docs/https-connector-refactor-plan.md §16.2.2), factored out of
web_approval_ui.py -- ``WebApprovalUI._run_card``/``_run_confirm`` are now
thin wrappers around ``block_on_card``/``block_on_confirm`` below, with no
behavior change (§16.5's W5 "Done when": no behavior change to approvals).

The insight §16.2.2 names: a worker thread blocked on a human's answer is
the same mechanism whether the answer is "accept/deny/accept_all" (a card),
"confirm/cancel" (a PII/rule confirmation), or "which of these N options"
(settings_controller.py's Atlassian multi-resource picker, today's
dialog_window.show_choice_dialog on the native side) -- register a
``PendingApproval`` in the one shared registry, render it as HTML, block the
calling thread on its ``threading.Event``, and read back whatever a human's
decision POST (web/routes_approvals.py's ``decide`` endpoint, unchanged)
wrote into ``result``/``chosen_index_result``. One mechanism, one set of
tests, and the picker gets the same "no longer pending" landing page an
approval link already has -- see approvals.PendingApprovalRegistry and
web/routes_approvals.py for the registration/serving/deciding side this
module sits on top of.
"""
from __future__ import annotations

from .approvals import CARD_RESULTS, CONFIRM_RESULTS, PendingApprovalRegistry


def block_on_card(registry: PendingApprovalRegistry, html: str, approval=None) -> tuple[str, int | None]:
    """Register (or reuse a pre-registered) card, set its HTML, block until
    a human answers, and return ``(decision, chosen_index)`` -- the same
    shape ``ApprovalUI.show_popup``/``show_read_popup`` return. ``approval``
    is gate.py's deferred-protocol pre-registration (see
    web_approval_ui.WebApprovalUI's own docstring); omitted, a fresh confirm-
    shaped registration is used instead -- unchanged from before this
    extraction."""
    card = approval if approval is not None else registry.register_confirm()
    card.kind = "card"
    registry.set_html(card.id, html)
    card.event.wait()
    result = card.result if card.result in CARD_RESULTS else "deny"
    return result, card.chosen_index_result


def block_on_confirm(registry: PendingApprovalRegistry, html: str) -> bool:
    """Register a confirmation dialog, block until answered, and return
    whether it was confirmed (vs. cancelled/denied)."""
    card = registry.register_confirm()
    registry.set_html(card.id, html)
    card.event.wait()
    return card.result == CONFIRM_RESULTS[0]  # "confirm"


def block_on_choice(registry: PendingApprovalRegistry, html: str) -> int | None:
    """Register a choice/picker dialog (dialog_window_html.build_choice_html
    -- its JS posts the clicked row's index as a bare number, or the string
    ``"cancel"``; web/routes_approvals.py's decide route normalizes either
    into ``PendingApproval.result`` as a string), block until answered, and
    return the chosen index, or ``None`` for "cancel"/no answer.

    Two behaviors settings_controller.py's Atlassian picker (§16.2.2) needs
    to carry over deliberately, not by accident: a cancelled picker is
    ``None`` here (the caller -- pick_resource -- is what falls back to
    ``resources[0]``, there never having been an abort path of its own);
    and this function only ever returns a plain int index, never the
    resource itself, so option *labels* stay the caller's concern (site
    URLs, not names -- see settings_controller.py's own comment on that).
    """
    card = registry.register_confirm()
    card.kind = "choice"
    registry.set_html(card.id, html)
    card.event.wait()
    result = card.result
    if result is None or result == "cancel":
        return None
    try:
        return int(result)
    except (TypeError, ValueError):
        return None
