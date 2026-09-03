"""Web approval UI: renders the same card-stack HTML gate.py has always
shown, into a page served locally over HTTP (web/routes_approvals.py)
instead of a native AppKit dialog.

P1 gave this module its own single-slot ``current()``/``PendingCard`` store,
with a note that it "becomes a real per-principal registry (approvals.py) in
P3" -- this is that move. Card/confirmation storage now lives entirely in
``approvals.PendingApprovalRegistry`` (``self._registry``, exposed to gate.py
via the ``deferred_registry`` property so it can apply the deferred
protocol -- see approvals.py's and gate.py's own module docstrings); this
class is left owning only the *rendering* (building the HTML for each kind
of dialog, via card_builder.py/dialog_window_html.py) and the *blocking
contract* ApprovalUI's ABC specifies: show_popup/show_read_popup/the two
confirmation methods still block the calling thread until a human decides,
same as every ApprovalUI implementation always has -- what changed is that
several can now be pending, decided in any order, at once.
"""
from __future__ import annotations

from . import dialog_window_html
from .approval_ui import ApprovalUI
from .approvals import CARD_RESULTS, PendingApproval, PendingApprovalRegistry
from .card_builder import build_card_html


class WebApprovalUI(ApprovalUI):
    """ApprovalUI implementation backing the web approval surface. See
    module docstring."""

    def __init__(self, *, registry: PendingApprovalRegistry | None = None) -> None:
        self._registry = registry if registry is not None else PendingApprovalRegistry()

    @property
    def deferred_registry(self) -> PendingApprovalRegistry:
        return self._registry

    # ------------------------------------------------------------------ #
    # Read side (web/routes_approvals.py): what's pending right now
    # ------------------------------------------------------------------ #

    def current(self) -> PendingApproval | None:
        """Back-compat convenience for callers that only ever want "the one
        thing pending right now" -- the most-recently-created not-yet-
        answered card/confirmation, or None. web/routes_approvals.py's list
        view uses ``self._registry.list_pending()`` directly instead, now
        that more than one can be pending at once."""
        pending = self._registry.list_pending()
        return pending[0] if pending else None

    # ------------------------------------------------------------------ #
    # Write side (web/routes_approvals.py's decide endpoint)
    # ------------------------------------------------------------------ #

    def resolve(self, card_id: str, result: str, choice: int | None = None) -> bool:
        """Resolve one UI-step card/confirmation, if ``card_id`` matches a
        currently-unanswered one. Returns whether the resolution was
        accepted -- False for an unknown, stale, or already-decided id,
        which is also what makes a decision POST idempotent (see
        docs/https-connector-refactor-plan.md §7.1: "the first accepted
        decision for an id wins; any later one is rejected")."""
        return self._registry.answer(card_id, result, choice)

    # ------------------------------------------------------------------ #
    # ApprovalUI -- blocking calls from gate.py, same signatures as
    # approval_popup.py's (see approval_ui.py's ABC docstring). Each takes
    # an optional ``approval`` kwarg: gate.py's deferred-protocol callers
    # (see gate.py's module docstring) pre-register the *main* card via
    # ``self._registry`` themselves (so they have a stable id/URL to report
    # back to Claude even before this method returns) and pass it in here;
    # this method then only has to render into it and block. Called with no
    # ``approval`` (e.g. directly, as every pre-P3 test in this repo already
    # does), it registers its own -- unchanged behavior for any caller that
    # doesn't know about the registry.
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
        approval: PendingApproval | None = None,
    ) -> tuple[str, int | None]:
        html = build_card_html(
            title=title, preview=preview, details_text=details_text, is_read=False, layout=layout,
            accept_all_choices=accept_all_choices, claude_reason=claude_reason,
            write_content_flags=write_content_flags, seen_count=seen_count, connector=connector,
            preview_bytes=preview_bytes, preview_mime_type=preview_mime_type,
            preview_tables=preview_tables, preview_blocks=preview_blocks, table_only=table_only,
            upload_forced=upload_forced, temp_accept_eligible=temp_accept_eligible,
        )
        return self._run_card(html, approval)

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
        approval: PendingApproval | None = None,
    ) -> tuple[str, int | None]:
        html = build_card_html(
            title=title, preview=preview, details_text=details_text, is_read=True, layout=layout,
            accept_all_choices=accept_all_choices, pii_categories=pii_categories, visibility=visibility,
            claude_reason=claude_reason, seen_count=seen_count, pdf_bytes=pdf_bytes, connector=connector,
            preview_bytes=preview_bytes, preview_mime_type=preview_mime_type, new_info=new_info,
            preview_tables=preview_tables, preview_blocks=preview_blocks, table_only=table_only,
        )
        return self._run_card(html, approval)

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

    def _run_card(self, html: str, approval: PendingApproval | None) -> tuple[str, int | None]:
        # A caller with no pre-registered approval (any direct call that
        # bypasses gate.py's own deferred-protocol registration, including
        # every pre-P3 test in this repo) gets a confirm-shaped registration
        # instead: it has no dedupe_key to coalesce or ledger on, which is
        # the correct degraded behavior here -- there's no gated-call
        # context to attach one to.
        card = approval if approval is not None else self._registry.register_confirm()
        card.kind = "card"
        self._registry.set_html(card.id, html)
        card.event.wait()
        result = card.result if card.result in CARD_RESULTS else "deny"
        return result, card.chosen_index_result

    def _run_confirm(self, html: str) -> bool:
        card = self._registry.register_confirm()
        self._registry.set_html(card.id, html)
        card.event.wait()
        return card.result == "confirm"


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


def init_web_approval_ui(registry: PendingApprovalRegistry | None = None) -> WebApprovalUI:
    """Construct (or replace) the singleton with an explicit registry --
    daemon_main.py uses this to apply settings.yaml's web.approvals.* hold
    window/TTL/cap overrides instead of approvals.py's bare defaults."""
    global _INSTANCE
    _INSTANCE = WebApprovalUI(registry=registry)
    return _INSTANCE
