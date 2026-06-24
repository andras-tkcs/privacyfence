"""macOS menu bar app (rumps).

Runs on the main thread. Polls the ReviewQueue on a timer (rumps callbacks must
run on the main thread, so we cannot let the async server touch the UI directly)
and rebuilds the menu to reflect pending reviews. The user approves/rejects from
per-request submenus; a "Privacy Settings" submenu lets them flip each category
between allow and block at runtime.
"""

from __future__ import annotations

import logging
import os
from typing import Callable, Optional

import rumps

_RESOURCES = os.path.join(os.path.dirname(__file__), "resources")
_MENU_BAR_ICON = os.path.join(_RESOURCES, "icon_32.png")

from .privacy_filter import PrivacyFilter
from .review_queue import PendingReview, ReviewQueue, get_review_queue

logger = logging.getLogger(__name__)

APP_NAME = "Loopline"
IDLE_TITLE = "🛡️"

# Human-friendly labels for the privacy categories submenu (Gmail defaults).
CATEGORY_LABELS = {
    "body": "Body",
    "metadata": "Metadata",
    "attachments": "Attachments",
    "thread_history": "Thread History",
    # Drive
    "file_content": "File Content",
    "file_metadata": "File Metadata",
    "file_list": "File List",
    "folder_structure": "Folder Structure",
    # Slack
    "message_content": "Message Content",
    "user_identity": "User Identity",
    "channel_list": "Channel List",
    "thread_content": "Thread Content",
}


class GmailGuardMenuBar(rumps.App):
    """The menu bar application.

    Can be driven by any privacy filter (Gmail, Drive, Slack). The filter's
    ``policies()`` method is used to discover categories at runtime, so the
    Privacy Settings submenu always matches the active service.
    """

    def __init__(
        self,
        privacy_filter: PrivacyFilter,
        review_queue: Optional[ReviewQueue] = None,
        on_quit: Optional[Callable[[], None]] = None,
        poll_interval: float = 1.0,
        app_name: str = APP_NAME,
    ) -> None:
        icon_path = _MENU_BAR_ICON if os.path.exists(_MENU_BAR_ICON) else None
        super().__init__(app_name, title=IDLE_TITLE, icon=icon_path, quit_button=None)
        self._filter = privacy_filter
        self._queue = review_queue or get_review_queue()
        self._on_quit = on_quit
        self._app_name = app_name
        # Track which request_ids we have already notified about, so we only
        # raise one notification per request.
        self._notified: set[str] = set()
        self._last_rendered_ids: tuple[str, ...] = ()

        self._build_static_menu()
        self._timer = rumps.Timer(self._poll, poll_interval)
        self._timer.start()
        # Render once immediately so the menu is populated at launch.
        self._render()

    # ------------------------------------------------------------------ #
    # Static menu scaffolding
    # ------------------------------------------------------------------ #
    def _build_static_menu(self) -> None:
        """Build the parts of the menu that do not change per-poll."""
        self._status_item = rumps.MenuItem("No pending requests")
        self._privacy_menu = rumps.MenuItem("Privacy Settings")

        self.menu = [
            self._status_item,
            None,  # separator; pending items are inserted above this at render
            self._privacy_menu,
            None,
            rumps.MenuItem(f"Quit {self._app_name}", callback=self._quit),
        ]
        self._refresh_privacy_menu()

    def _refresh_privacy_menu(self) -> None:
        """Rebuild the Privacy Settings submenu to reflect current policies."""
        if self._privacy_menu._menu is not None:
            self._privacy_menu.clear()
        policies = self._filter.policies()
        # Derive categories from the filter's own policy dict so this works
        # for Gmail, Drive, and Slack filters without any hardcoding.
        for category in policies:
            label = CATEGORY_LABELS.get(category, category)
            policy = policies.get(category, "block")
            item = rumps.MenuItem(
                f"{label}: {policy}",
                callback=self._make_toggle_callback(category),
            )
            # A check mark indicates the category is currently allowed.
            item.state = 1 if policy == "allow" else 0
            self._privacy_menu.add(item)

    def _make_toggle_callback(self, category: str) -> Callable[[rumps.MenuItem], None]:
        def _callback(_item: rumps.MenuItem) -> None:
            current = self._filter.policy_for(category)
            new_policy = "block" if current == "allow" else "allow"
            self._filter.set_policy(category, new_policy)
            logger.info("User toggled %s -> %s", category, new_policy)
            self._refresh_privacy_menu()

        return _callback

    # ------------------------------------------------------------------ #
    # Polling / rendering
    # ------------------------------------------------------------------ #
    def _poll(self, _timer: rumps.Timer) -> None:
        """Timer callback: notify on new requests and re-render if changed."""
        pending = self._queue.list_pending()
        current_ids = tuple(r.request_id for r in pending)

        # Notify on any newly-arrived request.
        for review in pending:
            if review.request_id not in self._notified:
                self._notified.add(review.request_id)
                self._notify(review)

        # Drop ids that are no longer pending so memory does not grow forever.
        active = set(current_ids)
        self._notified = {rid for rid in self._notified if rid in active}

        if current_ids != self._last_rendered_ids:
            self._render(pending)
            self._last_rendered_ids = current_ids

    def _render(self, pending: Optional[list[PendingReview]] = None) -> None:
        """Rebuild the dynamic part of the menu and the title badge."""
        if pending is None:
            pending = self._queue.list_pending()
        count = len(pending)

        # Update the menu bar title badge.
        self.title = IDLE_TITLE if count == 0 else f"{IDLE_TITLE} ({count})"

        # Remove any previously-rendered request items (titles start with the
        # request marker) before re-adding the current set.
        for key in list(self.menu.keys()):
            if isinstance(key, str) and key.startswith("req::"):
                del self.menu[key]

        if count == 0:
            self._status_item.title = "No pending requests"
            return

        self._status_item.title = f"{count} pending request(s)"

        # Insert each pending request as a submenu just before the first
        # separator (which sits above Privacy Settings).
        for review in pending:
            item = self._build_request_item(review)
            self.menu.insert_before("Privacy Settings", item)

    def _build_request_item(self, review: PendingReview) -> rumps.MenuItem:
        """Create the submenu for one pending request."""
        title = f"req::{review.request_id}"
        parent = rumps.MenuItem(title)
        # Override the visible label (the dict key stays the stable req:: id).
        parent.title = self._truncate(f"{review.tool_name}: {review.summary}")

        context = rumps.MenuItem(self._truncate(f"From/Context: {review.sender}"))
        approve = rumps.MenuItem(
            "✅ Approve", callback=self._make_approve_callback(review.request_id)
        )
        reject = rumps.MenuItem(
            "❌ Reject", callback=self._make_reject_callback(review.request_id)
        )
        parent.add(context)
        parent.add(rumps.separator)
        parent.add(approve)
        parent.add(reject)
        return parent

    # ------------------------------------------------------------------ #
    # Decision callbacks
    # ------------------------------------------------------------------ #
    def _make_approve_callback(self, request_id: str) -> Callable[[rumps.MenuItem], None]:
        def _callback(_item: rumps.MenuItem) -> None:
            if self._queue.approve(request_id):
                logger.info("Approved request %s via menu", request_id)
            self._render()

        return _callback

    def _make_reject_callback(self, request_id: str) -> Callable[[rumps.MenuItem], None]:
        def _callback(_item: rumps.MenuItem) -> None:
            if self._queue.reject(request_id):
                logger.info("Rejected request %s via menu", request_id)
            self._render()

        return _callback

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _notify(self, review: PendingReview) -> None:
        try:
            rumps.notification(
                title=self._app_name,
                subtitle=f"Claude requests: {review.tool_name}",
                message=self._truncate(review.summary, 120),
            )
        except Exception as exc:  # noqa: BLE001 - notifications are best-effort
            logger.warning("Could not post notification: %s", exc)

    @staticmethod
    def _truncate(text: str, limit: int = 70) -> str:
        text = (text or "").replace("\n", " ").strip()
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "…"

    def _quit(self, _item: rumps.MenuItem) -> None:
        logger.info("Quit requested from menu bar")
        # Reject anything still outstanding so awaiting MCP calls do not hang.
        self._queue.reject_all("Application shutting down")
        if self._on_quit is not None:
            try:
                self._on_quit()
            except Exception as exc:  # noqa: BLE001
                logger.warning("on_quit handler raised: %s", exc)
        rumps.quit_application()
