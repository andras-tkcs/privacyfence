"""MobileRelayApprovalUI (issue #55): an ApprovalUI backend that routes a
gate's popup through the mobile relay's mailbox instead of a native macOS
dialog.

Used standalone this would make mobile the *only* approval surface; in
practice it's always wrapped by CompositeApprovalUI (composite_approval_ui.py)
alongside NativeApprovalUI, so a phone answers *in addition to*, never
*instead of*, the existing desktop popup -- issue #55's requirement 5.

**Multi-device (Phase 2).** This backend doesn't hold one device's
connection -- it holds a PairingStore (mobile_relay_pairing.py) and reads
`list_active_devices()` fresh on *every* call, then races all of them
concurrently: the same first-response-wins mechanics CompositeApprovalUI
uses one level up, applied here across N phones instead of native-vs-mobile.
The first device to produce a trustworthy decision wins; the others are
told to stop (an internal abandon event, same idea as the outer one
CompositeApprovalUI sets when native wins) but their own eventual
request/response is otherwise irrelevant -- the exact same "loser left
running in the background, correctness doesn't depend on it stopping
promptly" reasoning as composite_approval_ui.py's own docstring. Racing
against zero devices (a store with no active pairings) is a well-defined
case too: an immediate deny, same as any other "nothing to answer this"
outcome, so the composite still just falls back to whatever native does.

Accept/Deny only -- no Always-allow / accept-all from mobile, per issue
#55's own reasoning: a phone can be handed off or glanced at half-asleep,
so it must never be the surface that grants a standing or time-limited
rule. Every method here that has a chosen_index in its return contract
therefore returns None for it unconditionally; a "decision" from this
backend is always exactly "approved" or "denied" (mapped to "accept"/"deny").

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
import queue
import threading
import uuid
from typing import Any

from .approval_ui import ApprovalUI
from .mobile_relay_client import MobileRelayClient, MobileRelayClientError
from .mobile_relay_pairing import PairingStore

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
        self,
        relay_url: str,
        pairing_store: PairingStore,
        *,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._relay_url = relay_url
        self._pairing_store = pairing_store
        self._request_timeout_seconds = request_timeout_seconds
        # Duck-typed, read by CompositeApprovalUI/gate.py's own future
        # "answered from mobile, by device X" audit wiring (see
        # audit_log.py's answered_via field) -- not itself wired up yet
        # (that's its own follow-up, not Phase 2 scope). "" means either
        # native answered or no mobile decision was reached at all.
        self.last_answered_device_name: str = ""

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

    def _request_decision(self, payload: dict[str, Any], outer_abandon_event: threading.Event | None) -> str:
        """Post `payload` to every currently-active paired device and
        return the first trustworthy decision any of them produces --
        "denied" if there are no active devices, every device fails to
        even post the request, or none answers within the timeout.

        Deliberately never raises: a phone (or every phone) being
        unreachable must never fail a call the native popup (raced
        alongside this by CompositeApprovalUI) can still answer, per issue
        #55's requirement 5.
        """
        self.last_answered_device_name = ""
        devices = self._pairing_store.list_active_devices()
        if not devices:
            logger.info("Mobile relay has no active paired devices -- treating this call as denied")
            return "denied"

        internal_abandon_event = threading.Event()

        def should_abandon() -> bool:
            return internal_abandon_event.is_set() or (
                outer_abandon_event is not None and outer_abandon_event.is_set()
            )

        results: queue.Queue[tuple[str, str | None]] = queue.Queue()

        def run(device_name: str, config, request_id: str) -> None:
            client = MobileRelayClient(config)
            try:
                client.post_request(request_id, payload, ttl_seconds=self._request_timeout_seconds)
            except MobileRelayClientError as exc:
                logger.warning("Could not post approval request to device %r: %s", device_name, exc)
                results.put((device_name, None))
                return
            decision = client.poll_decision(
                request_id, overall_timeout_seconds=self._request_timeout_seconds, should_abandon=should_abandon,
            )
            results.put((device_name, decision))

        for device in devices:
            threading.Thread(
                target=run,
                args=(device.device_name, device.to_config(self._relay_url), uuid.uuid4().hex),
                daemon=True,
            ).start()

        for _ in range(len(devices)):
            device_name, decision = results.get()
            if decision is not None:
                internal_abandon_event.set()
                self.last_answered_device_name = device_name
                return decision

        logger.info(
            "No trustworthy decision from any of %d paired device(s) within timeout -- "
            "treating as denied (fail closed)", len(devices),
        )
        return "denied"
