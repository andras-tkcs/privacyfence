"""Thread-safe review queue bridging the async MCP server and the sync UI.

The MCP server runs inside an asyncio event loop on a background thread. The
menu bar (rumps) runs on the main thread and calls back synchronously when the
user clicks Approve/Reject. This module bridges the two worlds safely:

  - MCP coroutine calls ``submit(...)`` and awaits the returned ``asyncio.Future``.
    The future is created on the MCP event loop, so awaiting it is safe.
  - The menu bar thread calls ``approve(request_id)`` / ``reject(request_id)``.
    These resolve the future via ``loop.call_soon_threadsafe(...)`` so the
    resolution happens on the owning event loop - never touching the future
    from the wrong thread.

A module-level singleton is used because both halves of the app need the same
instance and there is exactly one queue per process.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ReviewRejected(Exception):
    """Raised inside the MCP coroutine when the user rejects a request."""


@dataclass
class PendingReview:
    """A single request awaiting the user's decision."""

    request_id: str
    tool_name: str
    summary: str  # short, human-readable description for the UI
    sender: str  # best-effort sender/subject context for the UI
    raw_data: Any  # the unfiltered data (kept for audit/logging only)
    filtered_data: Any  # what will be returned to Claude if approved
    future: asyncio.Future
    loop: asyncio.AbstractEventLoop
    created_at: float = field(default_factory=time.time)
    # Optional rich display metadata for the UI (e.g. email body preview).
    # Keys depend on the tool; see floating_window.py for rendering logic.
    display_hint: dict = field(default_factory=dict)
    connector: str = ""
    tool: str = ""


class ReviewQueue:
    """Thread-safe store of pending reviews."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, PendingReview] = {}

    def submit(
        self,
        tool_name: str,
        summary: str,
        sender: str,
        raw_data: Any,
        filtered_data: Any,
        display_hint: dict | None = None,
        connector: str = "",
        tool: str = "",
    ) -> asyncio.Future:
        """Register a pending review and return a Future to await.

        Must be called from within the MCP event loop (it captures the running
        loop to create the Future on it).
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        request_id = uuid.uuid4().hex

        review = PendingReview(
            request_id=request_id,
            tool_name=tool_name,
            summary=summary,
            sender=sender,
            raw_data=raw_data,
            filtered_data=filtered_data,
            future=future,
            loop=loop,
            display_hint=display_hint or {},
            connector=connector,
            tool=tool,
        )
        with self._lock:
            self._pending[request_id] = review
        logger.info(
            "Review submitted id=%s tool=%s summary=%r",
            request_id,
            tool_name,
            summary,
        )
        return future

    def approve(self, request_id: str) -> bool:
        """Approve a pending review. Resolves its future with filtered_data.

        Returns True if the request existed and was resolved. Safe to call from
        the menu bar (main) thread.
        """
        review = self._pop(request_id)
        if review is None:
            logger.warning("approve: unknown request_id %s", request_id)
            return False

        def _resolve() -> None:
            if not review.future.done():
                review.future.set_result(review.filtered_data)

        review.loop.call_soon_threadsafe(_resolve)
        logger.info("Review approved id=%s tool=%s", request_id, review.tool_name)
        try:
            from .audit_log import AuditEntry, current_week, get_audit_logger
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id=review.request_id,
                connector=review.connector,
                tool=review.tool,
                tool_name=review.tool_name,
                summary=review.summary,
                sender=review.sender,
                decision="approved",
                auto_accept_rule="",
                latency_seconds=time.time() - review.created_at,
            ))
        except Exception as exc:
            logger.warning("Audit log failed: %s", exc)
        return True

    def reject(self, request_id: str, reason: str = "Rejected by user") -> bool:
        """Reject a pending review. Resolves its future with a ReviewRejected.

        Returns True if the request existed and was resolved. Safe to call from
        the menu bar (main) thread.
        """
        review = self._pop(request_id)
        if review is None:
            logger.warning("reject: unknown request_id %s", request_id)
            return False

        def _resolve() -> None:
            if not review.future.done():
                review.future.set_exception(ReviewRejected(reason))

        review.loop.call_soon_threadsafe(_resolve)
        logger.info(
            "Review rejected id=%s tool=%s reason=%r",
            request_id,
            review.tool_name,
            reason,
        )
        try:
            from .audit_log import AuditEntry, current_week, get_audit_logger
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id=review.request_id,
                connector=review.connector,
                tool=review.tool,
                tool_name=review.tool_name,
                summary=review.summary,
                sender=review.sender,
                decision="rejected",
                auto_accept_rule="",
                latency_seconds=time.time() - review.created_at,
            ))
        except Exception as exc:
            logger.warning("Audit log failed: %s", exc)
        return True

    def fail(self, request_id: str, exc: Exception) -> bool:
        """Resolve a pending review with an arbitrary exception (e.g. shutdown)."""
        review = self._pop(request_id)
        if review is None:
            return False

        def _resolve() -> None:
            if not review.future.done():
                review.future.set_exception(exc)

        review.loop.call_soon_threadsafe(_resolve)
        return True

    def list_pending(self) -> list[PendingReview]:
        """Snapshot of pending reviews, oldest first (for the UI)."""
        with self._lock:
            return sorted(self._pending.values(), key=lambda r: r.created_at)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def reject_all(self, reason: str = "Application shutting down") -> None:
        """Reject every outstanding review (used on graceful shutdown)."""
        for review in self.list_pending():
            self.reject(review.request_id, reason)

    def _pop(self, request_id: str) -> Optional[PendingReview]:
        with self._lock:
            return self._pending.pop(request_id, None)


_INSTANCE: Optional[ReviewQueue] = None
_INSTANCE_LOCK = threading.Lock()


def get_review_queue() -> ReviewQueue:
    """Return the process-wide ReviewQueue singleton."""
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = ReviewQueue()
    return _INSTANCE
