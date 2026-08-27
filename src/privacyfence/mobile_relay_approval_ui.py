"""MobileRelayApprovalUI (issue #55, Phase 1): an ApprovalUI backend that
routes a gate's popup through the mobile relay's mailbox instead of a native
macOS dialog.

Used standalone this would make mobile the *only* approval surface; in
practice it's always wrapped by CompositeApprovalUI (composite_approval_ui.py)
alongside NativeApprovalUI, so a phone answers *in addition to*, never
*instead of*, the existing desktop popup -- issue #55's requirement 5 ("the
existing desktop popup keeps working unmodified").

Accept/Deny only -- no Always-allow / accept-all from mobile, per issue
#55's own reasoning: a phone can be handed off or glanced at half-asleep, so
it must never be the surface that grants a standing or time-limited rule.
Every method here that has a chosen_index in its return contract therefore
returns None for it unconditionally; a "decision" from this backend is
always exactly "approved" or "denied" (mapped to "accept"/"deny").

Text-only parity for now: title, preview, full details_text, and the PII/
content-flag categories (issue #55's requirement 4 -- the red-tinted PII
warning and full content preview must render on mobile too) all cross the
relay. Native-only extras -- preview_bytes/pdf_bytes (image/PDF embeds),
preview_tables/preview_blocks (structured WIDE-layout previews) -- are NOT
yet sent to the phone; a request that relies on one of those to convey its
full content is, today, mobile-visible only as its plain-text
details_text/preview fields. Closing this gap by shipping the same HTML/CSS
payload approval_window_html.py already generates (rather than a hand-built
second renderer) is Phase 3's job -- see issue #55's phasing and the
2026-07-31 comment on how #120's webview-rendering pattern sets that up.
"""
from __future__ import annotations

import logging
import threading
import uuid
from typing import Any

from .approval_ui import ApprovalUI
from .mobile_relay_client import MobileRelayClient, MobileRelayClientError

logger = logging.getLogger(__name__)

# Long enough that a notification sitting unread on a locked phone for a
# few minutes doesn't automatically fail the request; short enough that a
# gate call doesn't hang indefinitely if nobody's there to answer at all
# (the native popup, shown at the same time via CompositeApprovalUI, is
# what actually keeps the request alive past this if a human is at the Mac
# instead).
DEFAULT_REQUEST_TIMEOUT_SECONDS = 300.0


class MobileRelayApprovalUI(ApprovalUI):
    def __init__(
        self, client: MobileRelayClient, *, request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._client = client
        self._request_timeout_seconds = request_timeout_seconds

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
        *,
        abandon_event: threading.Event | None = None,
    ) -> tuple[str, int | None]:
        decision = self._request_decision(
            {
                "kind": "popup",
                "title": title,
                "preview": preview,
                "details_text": details_text,
                "pii_flagged": bool(write_content_flags),
                "pii_categories": write_content_flags or [],
                "claude_reason": claude_reason,
                "connector": connector,
                "seen_count": seen_count,
            },
            abandon_event,
        )
        return ("accept" if decision == "approved" else "deny", None)

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
        *,
        abandon_event: threading.Event | None = None,
    ) -> tuple[str, int | None]:
        decision = self._request_decision(
            {
                "kind": "read_popup",
                "title": title,
                "preview": preview,
                "details_text": details_text,
                "pii_flagged": bool(pii_categories),
                "pii_categories": pii_categories or [],
                "claude_reason": claude_reason,
                "connector": connector,
                "seen_count": seen_count,
            },
            abandon_event,
        )
        return ("accept" if decision == "approved" else "deny", None)

    def show_pii_confirmation_popup(
        self, categories: list[str], *, abandon_event: threading.Event | None = None,
    ) -> bool:
        decision = self._request_decision(
            {"kind": "pii_confirmation", "pii_categories": categories, "pii_flagged": True},
            abandon_event,
        )
        return decision == "approved"

    def show_rule_confirmation_popup(
        self, description: str, *, abandon_event: threading.Event | None = None,
    ) -> bool:
        decision = self._request_decision(
            {"kind": "rule_confirmation", "description": description},
            abandon_event,
        )
        return decision == "approved"

    def _request_decision(self, payload: dict[str, Any], abandon_event: threading.Event | None) -> str:
        """Post `payload` as a new request and block for its decision.

        Any failure to even post the request (relay unreachable, bad
        config) is treated the same as "no mobile decision" -- denied, not
        raised -- since a phone that can't be reached must never block or
        fail a call the native popup can still answer (issue #55's
        requirement 5). CompositeApprovalUI's own race is what actually
        lets the native side win in that case; this method just needs to
        resolve to *something* rather than propagate the exception.
        """
        request_id = uuid.uuid4().hex
        should_abandon = abandon_event.is_set if abandon_event is not None else (lambda: False)

        try:
            self._client.post_request(request_id, payload, ttl_seconds=self._request_timeout_seconds)
        except MobileRelayClientError as exc:
            logger.warning("Could not post approval request %s to mobile relay: %s", request_id, exc)
            return "denied"

        decision = self._client.poll_decision(
            request_id, overall_timeout_seconds=self._request_timeout_seconds, should_abandon=should_abandon,
        )
        if decision is None:
            logger.info(
                "No trustworthy mobile decision for request %s within timeout -- "
                "treating as denied (fail closed)", request_id,
            )
            return "denied"
        return decision
