"""Approval UI seam: the interface gate.py depends on instead of importing
a concrete approval-surface implementation directly.

gate.py is the policy engine: auto-accept check -> block on a human decision
-> audit log. The policy loop itself has no reason to know how a human
decision actually gets shown -- it just needs something that can show the
write-gate popup, the review-gate popup, and the two smaller confirmation
dialogs, and return a decision. ApprovalUI is that something.

Through P9 this had two implementations: NativeApprovalUI (macOS AppKit/
WKWebView dialogs, via approval_popup.py) and WebApprovalUI (the same card
stack, served over HTTP). P10 (docs/https-connector-refactor-plan.md §12,
decision D6 in §15) deleted the native one -- "two approval surfaces means
two places for a security fix to land" -- leaving WebApprovalUI
(web_approval_ui.py) as the sole implementation. The ABC stays here, and
gate.py still reaches it through get_approval_ui() rather than importing
WebApprovalUI directly, on purpose: D6's own reasoning was "the ApprovalUI
seam lets it come back if that proves wrong", so a future implementation
(e.g. a Windows-native dialog for #121, once that's revisited per §14) only
needs to implement this interface and call init_approval_ui() with an
instance of it -- gate.py's own call sites never change.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class ApprovalUI(ABC):
    """One blocking human-approval surface. Every method mirrors one of
    WebApprovalUI's own (see web_approval_ui.py), which in turn mirrors
    approval_popup.py's pre-P10 free functions -- these signatures were kept
    identical across that transition so nothing calling through this ABC had
    to change shape.
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
        'accept_all', else None. See web_approval_ui.WebApprovalUI.show_popup's
        docstring."""

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
        'accept_all', else None. See web_approval_ui.WebApprovalUI.
        show_read_popup's docstring."""

    @abstractmethod
    def show_pii_confirmation_popup(self, categories: list[str]) -> bool:
        """Second-step confirmation for content the PII detector flagged.
        See web_approval_ui.WebApprovalUI.show_pii_confirmation_popup's
        docstring."""

    @abstractmethod
    def show_rule_confirmation_popup(self, description: str) -> bool:
        """Second-step confirmation after a specific "Always allow" button
        is clicked. See web_approval_ui.WebApprovalUI.
        show_rule_confirmation_popup's docstring."""

    @property
    def deferred_registry(self):  # -> approvals.PendingApprovalRegistry | None
        """A ``PendingApprovalRegistry`` (approvals.py) this backend is
        registered with, if it supports the deferred/hold-window protocol
        (docs/https-connector-refactor-plan.md §5) -- ``None`` (the
        default) means this backend only ever blocks until a human decides.
        WebApprovalUI (the only implementation since P10) always overrides
        this with a real registry; the default stays here for whatever
        future implementation the seam's own docstring anticipates, in case
        it has nowhere to send a human a reviewable link either.
        gate.py checks this property, not the concrete class, to decide
        whether to apply the deferred protocol -- see that module's
        docstring."""
        return None


class _UnconfiguredApprovalUI(ApprovalUI):
    """The bare fallback get_approval_ui() constructs when nothing has
    called init_approval_ui() yet. Deliberately not WebApprovalUI: this
    plays the same "inert, no-registry default" role NativeApprovalUI
    played before P10 -- gate.py's own test suite mostly monkeypatches its
    module-level show_popup/show_read_popup/etc. wrappers directly, relying
    on the default ApprovalUI having no deferred_registry (so gated_call()
    takes the plain blocking path, not the deferred/hold-window one) rather
    than installing a real ApprovalUI itself. Every method here raises if
    actually invoked without being monkeypatched or replaced first -- a
    misconfigured daemon should fail loudly, not silently deny (or block
    forever on) a real gated call. daemon_main.py always calls
    init_approval_ui() with a real, config-driven WebApprovalUI before any
    gated call could reach this."""

    def _unconfigured(self) -> None:
        raise RuntimeError(
            "No ApprovalUI configured -- call approval_ui.init_approval_ui() first "
            "(daemon_main.py always does this at startup)."
        )

    def show_popup(self, *args, **kwargs):
        self._unconfigured()

    def show_read_popup(self, *args, **kwargs):
        self._unconfigured()

    def show_pii_confirmation_popup(self, categories: list[str]) -> bool:
        self._unconfigured()

    def show_rule_confirmation_popup(self, description: str) -> bool:
        self._unconfigured()


_INSTANCE: ApprovalUI | None = None


def get_approval_ui() -> ApprovalUI:
    """Lazily-constructed singleton -- see _UnconfiguredApprovalUI's own
    docstring for what the default is and why."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = _UnconfiguredApprovalUI()
    return _INSTANCE


def init_approval_ui(ui: ApprovalUI) -> ApprovalUI:
    global _INSTANCE
    _INSTANCE = ui
    return _INSTANCE
