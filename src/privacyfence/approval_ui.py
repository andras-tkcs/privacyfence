"""Approval UI seam: the interface gate.py depends on instead of importing
approval_popup.py (native macOS osascript/AppKit dialogs) directly.

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
instance of it -- gate.py's own call sites never change.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from . import approval_popup


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


class NativeApprovalUI(ApprovalUI):
    """Today's (and so far only) implementation: native macOS osascript/AppKit
    dialogs, via approval_popup.py. Pure delegation -- no logic of its own."""

    def show_popup(self, *args, **kwargs) -> str:
        return approval_popup.show_popup(*args, **kwargs)

    def show_read_popup(self, *args, **kwargs) -> str:
        return approval_popup.show_read_popup(*args, **kwargs)

    def show_pii_confirmation_popup(self, categories: list[str]) -> bool:
        return approval_popup.show_pii_confirmation_popup(categories)

    def show_rule_confirmation_popup(self, description: str) -> bool:
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
