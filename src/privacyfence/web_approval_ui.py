"""Web approval UI: renders the same card-stack HTML gate.py has always
shown, into a page served locally over HTTP (web/routes_approvals.py)
instead of a native AppKit dialog.

Blocking contract identical to NativeApprovalUI (approval_ui.py):
show_popup/show_read_popup/show_pii_confirmation_popup/
show_rule_confirmation_popup still block the calling thread until a human
decides, the same synchronous handshake gate.py already assumes -- this
only changes *where* the human looks, not gated_call()'s calling
convention. See docs/https-connector-refactor-plan.md's P1: the deferred
(non-blocking) protocol described in that document's §5 is P3's work, not
this module's -- WebApprovalUI is a hosting change, exactly like
approval_window_html.py/settings_window_html.py already were (§11 of that
document).

At most one approval is ever pending at a time in P1: gate.py still
serializes every popup-gate/review-gate call through its own
``_popup_lock`` (see gate.py's module docstring), so show_popup/
show_read_popup/the two confirmation methods are themselves called
serially -- concurrency (several pending approvals at once, §6 of the
refactor plan) is P3's ``_popup_lock`` retirement, not this module's.
``current()`` below is a single slot, not a registry, for exactly that
reason; it becomes a real per-principal registry (approvals.py) in P3.
"""
from __future__ import annotations

import threading
import uuid

from . import dialog_window_html
from .approval_ui import ApprovalUI
from .card_builder import build_card_html

# Bridge protocol values gate.py's popup-gate/review-gate branches accept
# back from a resolved card -- same vocabulary as approval_popup.py's
# native _BRIDGE_RESULTS, and what approval_window_html.py's own _JS already
# posts (see that module's "Bridge protocol" docstring paragraph).
_CARD_RESULTS = ("accept", "deny", "accept_all")


class PendingCard:
    """One card (the full approval popup) or confirm (a smaller
    Cancel/Confirm dialog) waiting on a human decision -- returned by
    current() for the web route layer to render, and resolved by
    WebApprovalUI.resolve() once the decision endpoint receives a POST.

    ``id`` is 128 bits of ``uuid4`` entropy: unguessable, but (per
    docs/https-connector-refactor-plan.md §10.4) not itself treated as a
    bearer credential -- web/routes_approvals.py is what actually checks
    the caller is authorized before accepting a decision for it.
    """

    def __init__(self, kind: str, html: str) -> None:
        self.id = uuid.uuid4().hex
        self.kind = kind  # "card" | "confirm"
        self.html = html
        self._event = threading.Event()
        # (result, choice) once resolved -- result is one of _CARD_RESULTS
        # for kind=="card", or "confirm"/"cancel" for kind=="confirm";
        # choice is only ever set for a card resolved "accept_all".
        self.result: str | None = None
        self.choice: int | None = None
        self.resolved = False

    def wait(self) -> None:
        self._event.wait()

    def _resolve(self, result: str, choice: int | None) -> None:
        self.result = result
        self.choice = choice
        self.resolved = True
        self._event.set()


class WebApprovalUI(ApprovalUI):
    """ApprovalUI implementation backing the web approval surface. See
    module docstring for the single-pending-slot design in P1."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: PendingCard | None = None

    # ------------------------------------------------------------------ #
    # Read side (web/routes_approvals.py): what's pending right now
    # ------------------------------------------------------------------ #

    def current(self) -> PendingCard | None:
        with self._lock:
            return self._current

    # ------------------------------------------------------------------ #
    # Write side (web/routes_approvals.py's decide endpoint)
    # ------------------------------------------------------------------ #

    def resolve(self, card_id: str, result: str, choice: int | None = None) -> bool:
        """Resolve the currently-pending card/confirmation, if ``card_id``
        matches it and it isn't already resolved. Returns whether the
        resolution was accepted -- False for an unknown, stale, or
        already-decided id, which is also what makes a decision POST
        idempotent (see docs/https-connector-refactor-plan.md §7.1: "the
        first accepted decision for an id wins; any later one is
        rejected")."""
        with self._lock:
            card = self._current
            if card is None or card.id != card_id or card.resolved:
                return False
            card._resolve(result, choice)
            # Cleared, not left in place -- current() is a single "what's
            # pending right now" slot (see module docstring), and a decided
            # card is no longer pending. web/routes_approvals.py's "not
            # found" messaging already covers a card_id that no longer
            # matches anything here, which is exactly what a stale
            # /approvals/{id} link should show once its card is decided.
            self._current = None
            return True

    # ------------------------------------------------------------------ #
    # ApprovalUI -- blocking calls from gate.py, same signatures as
    # approval_popup.py's (see approval_ui.py's ABC docstring)
    # ------------------------------------------------------------------ #

    def show_popup(
        self,
        title: str,
        preview: dict[str, str],
        details_text: str,
        temp_accept_eligible: bool = False,
        claude_reason: str = "",
        write_content_flags: list[str] | None = None,
        seen_count: int = 0,
        connector: str = "",
        accept_all_choices: list[tuple[str, str]] | None = None,
        preview_bytes: bytes = b"",
        preview_mime_type: str = "",
        preview_tables: list[dict] | None = None,
        preview_blocks: list[dict] | None = None,
        table_only: bool = False,
        upload_forced: bool = False,
        layout: str = "narrow",
    ) -> tuple[str, int | None]:
        html = build_card_html(
            title=title, preview=preview, details_text=details_text, is_read=False, layout=layout,
            accept_all_choices=accept_all_choices, claude_reason=claude_reason,
            write_content_flags=write_content_flags, seen_count=seen_count, connector=connector,
            preview_bytes=preview_bytes, preview_mime_type=preview_mime_type,
            preview_tables=preview_tables, preview_blocks=preview_blocks, table_only=table_only,
            upload_forced=upload_forced, temp_accept_eligible=temp_accept_eligible,
        )
        return self._run_card(html)

    def show_read_popup(
        self,
        title: str,
        preview: dict[str, str],
        details_text: str,
        accept_all_choices: list[tuple[str, str]] | None,
        pii_categories: list[str] | None = None,
        visibility: dict[str, str] | None = None,
        claude_reason: str = "",
        seen_count: int = 0,
        content_kind: str = "generic",
        pdf_bytes: bytes = b"",
        connector: str = "",
        preview_bytes: bytes = b"",
        preview_mime_type: str = "",
        new_info: dict[str, str] | None = None,
        preview_tables: list[dict] | None = None,
        preview_blocks: list[dict] | None = None,
        table_only: bool = False,
        layout: str = "narrow",
    ) -> tuple[str, int | None]:
        html = build_card_html(
            title=title, preview=preview, details_text=details_text, is_read=True, layout=layout,
            accept_all_choices=accept_all_choices, pii_categories=pii_categories, visibility=visibility,
            claude_reason=claude_reason, seen_count=seen_count, pdf_bytes=pdf_bytes, connector=connector,
            preview_bytes=preview_bytes, preview_mime_type=preview_mime_type, new_info=new_info,
            preview_tables=preview_tables, preview_blocks=preview_blocks, table_only=table_only,
        )
        return self._run_card(html)

    def show_pii_confirmation_popup(self, categories: list[str]) -> bool:
        cats = ", ".join(categories) if categories else "personal data"
        html = dialog_window_html.build_confirmation_html(
            title="PrivacyFence — Possible PII Detected",
            message_lines=[
                f"PrivacyFence detected possible personal data in this content: {cats}.",
                "Are you sure you want to proceed?",
            ],
            cancel_label="Cancel",
            confirm_label="Proceed",
        )
        return self._run_confirm(html)

    def show_rule_confirmation_popup(self, description: str) -> bool:
        html = dialog_window_html.build_confirmation_html(
            title="PrivacyFence — Confirm Auto-Accept Rule",
            message_lines=[
                "PrivacyFence will create an auto-accept rule:",
                description,
                "Future matching requests will be approved automatically, without a popup.",
            ],
            cancel_label="Cancel",
            confirm_label="Confirm",
        )
        return self._run_confirm(html)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _run_card(self, html: str) -> tuple[str, int | None]:
        card = self._start(PendingCard("card", html))
        card.wait()
        result = card.result if card.result in _CARD_RESULTS else "deny"
        return result, card.choice

    def _run_confirm(self, html: str) -> bool:
        card = self._start(PendingCard("confirm", html))
        card.wait()
        return card.result == "confirm"

    def _start(self, card: PendingCard) -> PendingCard:
        with self._lock:
            self._current = card
        return card


_INSTANCE: WebApprovalUI | None = None


def get_web_approval_ui() -> WebApprovalUI:
    """Lazily-constructed singleton -- the same instance gate.py's calls
    (via approval_ui.init_approval_ui(get_web_approval_ui())) and
    web/routes_approvals.py's routes (via this same accessor) must share,
    so a decision posted to the web route actually reaches the card
    gate.py is blocked on."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = WebApprovalUI()
    return _INSTANCE
