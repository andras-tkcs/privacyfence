"""Approval UI seam: the interface gate.py depends on instead of importing
approval_popup.py (native macOS AppKit/WKWebView dialogs) directly.

gate.py is the policy engine: auto-accept check -> block on a human decision
-> audit log. Today the only way to get that human decision is a native
macOS dialog, but the policy loop itself has no reason to know that -- it
just needs something that can show the write-gate popup, the review-gate
popup, and the two smaller confirmation dialogs, and return a decision.
ApprovalUI is that something. NativeApprovalUI (today's only implementation)
delegates straight through to approval_popup.py, so this adds a seam with no
behavior change on macOS.

A future implementation (e.g. routing approval requests to a phone for
issue #55's mobile remote approval, or a Windows-native dialog for #121)
only needs to implement this interface and call init_approval_ui() with an
instance of it -- gate.py's own call sites never change. web_approval_ui.py
is the first one: this module (and therefore gate.py, which imports
get_approval_ui from here) must stay importable without PyObjC so the web
approval surface can run -- and be unit-tested -- on a platform with no
AppKit at all. See docs/https-connector-refactor-plan.md's P1.

approval_popup (AppKit/WKWebView, transitively) is therefore imported
lazily, guarded the same way settings_controller.py's own rumps/
dialog_window/AppHelper imports are (see that module's docstring) --
NativeApprovalUI is still the default (get_approval_ui()'s fallback below),
so nothing changes on macOS; a platform with no PyObjC just can't construct
one, which only matters if nothing ever calls init_approval_ui() with a
different implementation first.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class ApprovalUI(ABC):
    """One blocking human-approval surface. Every method mirrors one of
    approval_popup.py's free functions -- see that module's docstrings for
    the full parameter-by-parameter reference; these signatures are kept
    identical so NativeApprovalUI below is a pure delegation.
    """

    @abstractmethod
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
        """Approval popup for write tools. Returns (decision, chosen_index)
        -- decision is 'accept', 'deny', or 'accept_all'; chosen_index is
        the clicked button's index into accept_all_choices when decision is
        'accept_all', else None. See approval_popup.show_popup's docstring."""

    @abstractmethod
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
        """Approval popup for read tools. Returns (decision, chosen_index)
        -- decision is 'accept', 'deny', or 'accept_all'; chosen_index is
        the clicked button's index into accept_all_choices when decision is
        'accept_all', else None. See approval_popup.show_read_popup's
        docstring."""

    @abstractmethod
    def show_pii_confirmation_popup(self, categories: list[str]) -> bool:
        """Second-step confirmation for content the PII detector flagged.
        See approval_popup.show_pii_confirmation_popup's docstring."""

    @abstractmethod
    def show_rule_confirmation_popup(self, description: str) -> bool:
        """Second-step confirmation after a specific "Always allow" button
        is clicked. See approval_popup.show_rule_confirmation_popup's
        docstring."""

    @property
    def deferred_registry(self):  # -> approvals.PendingApprovalRegistry | None
        """A ``PendingApprovalRegistry`` (approvals.py) this backend is
        registered with, if it supports the deferred/hold-window protocol
        (docs/https-connector-refactor-plan.md §5) -- ``None`` (the
        default) means this backend only ever blocks until a human decides,
        exactly like every ApprovalUI did before P3. That's
        NativeApprovalUI's posture below: there is nowhere to send a human
        a reviewable link for a dialog already on their own screen, so
        deferring would only turn a wait into an error, not a convenience.
        gate.py checks this property, not the concrete class, to decide
        whether to apply the deferred protocol -- see that module's
        docstring."""
        return None


class NativeApprovalUI(ApprovalUI):
    """Today's (and so far only) macOS implementation: native AppKit/
    WKWebView dialogs, via approval_popup.py. Pure delegation -- no logic of
    its own. approval_popup is imported lazily inside each method (not at
    module scope -- see this module's own docstring) so constructing this
    class is the first point that actually needs PyObjC to be installed,
    not merely importing this module."""

    def show_popup(self, *args, **kwargs) -> str:
        from . import approval_popup
        return approval_popup.show_popup(*args, **kwargs)

    def show_read_popup(self, *args, **kwargs) -> str:
        from . import approval_popup
        return approval_popup.show_read_popup(*args, **kwargs)

    def show_pii_confirmation_popup(self, categories: list[str]) -> bool:
        from . import approval_popup
        return approval_popup.show_pii_confirmation_popup(categories)

    def show_rule_confirmation_popup(self, description: str) -> bool:
        from . import approval_popup
        return approval_popup.show_rule_confirmation_popup(description)


_INSTANCE: ApprovalUI | None = None


def get_approval_ui() -> ApprovalUI:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = NativeApprovalUI()
    return _INSTANCE


def init_approval_ui(ui: ApprovalUI) -> ApprovalUI:
    global _INSTANCE
    _INSTANCE = ui
    return _INSTANCE
