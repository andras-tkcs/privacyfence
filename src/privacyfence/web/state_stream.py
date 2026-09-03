"""SSE state-push channel (docs/https-connector-refactor-plan.md §16.3):
``GET /api/state/stream``, one connection per open tab, carrying two
independently-updating event types --

- ``event: settings`` -- ``SettingsController.snapshot()``, pushed the
  moment something changes it out from under an open page: a rule edited
  over MCP, an OAuth flow finishing on its own background thread, a grant's
  resource name resolving. Without this channel a browser tab showing
  ``/settings`` would need a manual refresh for every one of those, and the
  snapshot a mutating POST returns is a promise the page can't otherwise
  keep -- see settings_controller.py's own docstring on ``on_change``.
- ``event: approvals`` -- ``approvals.PendingApprovalRegistry.list_pending()``
  summaries, polled the same way ``web/routes_approvals.py``'s own
  ``/api/approvals/stream`` already does. P3 already built the registry
  that can hold several of these at once; this is the one new event this
  phase adds to the shared channel, so the approvals page's own SSE
  subscription (routes_approvals.py, unchanged) and this settings-shaped
  one agree on the same underlying data without either needing to know
  about the other.

Every open page re-renders from a *full* state dict, never a patch --
``window.__pfRender(state)``/``window.__pfRenderApprovals(state)`` are both
idempotent full re-renders (settings_window_html.py's/the approval list's
own convention) -- so there is no ordering or patch-application problem to
solve here, only "deliver the latest snapshot, promptly, to every open
connection", including on first connect/reconnect (§16.7: "a reconnect
delivers full state rather than a patch").

This module also owns the one piece of plumbing settings_controller.py's
``call_on_main`` dispatcher seam (§16.2.1) needs once a web server is
actually running: ``set_loop``/``call_soon_threadsafe`` marshal a
background-thread callback (an OAuth flow finishing, a rules-changed
broadcast) onto this stream's own asyncio event loop, the same "some run
loop is the safe place to touch shared state from" reasoning AppHelper.
callAfter has always given the native window.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# How often a connection checks the approvals registry for a change, absent
# any settings push arriving first -- same interval and reasoning as
# routes_approvals.py's own _STREAM_POLL_SECONDS: short enough that a human
# watching the page doesn't perceive a lag, cheap enough (an in-memory dict
# scan) that polling it beats adding a second pub/sub mechanism just for
# approvals.
_APPROVALS_POLL_SECONDS = 1.0

# The web server's own asyncio event loop, captured once at startup (see
# server.py's lifespan wiring) -- settings_controller.call_on_main's
# fallback dispatcher for a process with no AppKit run loop hosting
# (§16.2.1) marshals onto this loop rather than running inline, once one is
# known. Module-level, not stashed on StateStream, because
# set_main_dispatcher() needs a plain function reference it can register
# once and never touch again -- see call_soon_threadsafe below.
_loop: asyncio.AbstractEventLoop | None = None


def set_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    global _loop
    _loop = loop


def get_loop() -> asyncio.AbstractEventLoop | None:
    """The ASGI app's own running event loop, once captured by
    ``_state_stream_loop_lifespan`` -- ``None`` before startup or after
    shutdown. daemon_main.py's cache-warming uses this (via web/server.py's
    ``WebServer.wait_until_ready``) as the one loop every connector call
    now actually runs on, the direct successor of the old IPC thread's own
    loop for that same purpose."""
    return _loop


def call_soon_threadsafe(fn: Callable[..., None], *args: Any) -> None:
    """settings_controller.set_main_dispatcher()'s target once the web
    server is up -- see settings_controller.call_on_main's own docstring.
    Runs ``fn(*args)`` inline if no loop has been captured yet (e.g. called
    before the ASGI app's startup event has fired), the same safe fallback
    call_on_main itself takes when nothing is hosting yet."""
    if _loop is not None:
        _loop.call_soon_threadsafe(fn, *args)
    else:
        fn(*args)


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


class StateStream:
    """One instance per running web server (server.py owns it, alongside
    ``WebApprovalUI``), shared by every ``/api/state/stream`` connection.

    ``settings_snapshot`` is a zero-arg callable returning
    ``SettingsController.snapshot()``, or ``None`` when no controller is
    wired (``web.settings.enabled: false`` -- the approvals-only surface
    still gets its own event on this channel). ``list_pending`` is
    ``WebApprovalUI.deferred_registry.list_pending``.
    """

    def __init__(
        self,
        *,
        settings_snapshot: Callable[[], dict[str, Any] | None],
        list_pending: Callable[[], list[Any]],
    ) -> None:
        self._settings_snapshot = settings_snapshot
        self._list_pending = list_pending
        self._subscribers: set[asyncio.Queue] = set()

    # ------------------------------------------------------------------ #
    # Push side -- settings_controller.SettingsController.add_change_
    # listener's target (see routes_settings.py's wiring).
    # ------------------------------------------------------------------ #

    def push_settings(self, state: dict[str, Any]) -> None:
        """Called via call_on_main's dispatch, so always on this stream's
        own event loop by the time a real web server is running (see
        server.py's wiring of set_main_dispatcher(call_soon_threadsafe))
        -- never directly from a background thread."""
        self._broadcast("settings", state)

    def _broadcast(self, event: str, data: Any) -> None:
        for queue in list(self._subscribers):
            # maxsize=1, replace-not-append: each subscriber only ever
            # holds the *latest* pending message per stream, which is what
            # "pushes are debounced" (§16.7) means here -- a burst of rapid
            # changes (several grant names resolving back to back) collapses
            # to one flush of the newest snapshot instead of a growing
            # backlog the reader falls behind on.
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            queue.put_nowait((event, data))

    # ------------------------------------------------------------------ #
    # Read side -- web/routes_settings.py's /api/state/stream route.
    # ------------------------------------------------------------------ #

    async def subscribe(self, is_disconnected: Callable[[], Awaitable[bool]]):
        """Async generator of SSE-formatted strings for one connection."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        try:
            settings_state = self._settings_snapshot()
            if settings_state is not None:
                yield _sse("settings", settings_state)
            last_ids = self._approvals_ids()
            yield _sse("approvals", self._approvals_payload())

            while True:
                if await is_disconnected():
                    break
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=_APPROVALS_POLL_SECONDS)
                    yield _sse(event, data)
                except asyncio.TimeoutError:
                    pass
                ids = self._approvals_ids()
                if ids != last_ids:
                    last_ids = ids
                    yield _sse("approvals", self._approvals_payload())
        finally:
            self._subscribers.discard(queue)

    def _approvals_ids(self) -> tuple[str, ...]:
        return tuple(a.id for a in self._list_pending())

    def _approvals_payload(self) -> list[dict[str, Any]]:
        return [a.to_summary_dict() for a in self._list_pending()]
