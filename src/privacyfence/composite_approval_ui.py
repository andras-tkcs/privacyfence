"""CompositeApprovalUI (issue #55, Phase 1): races the native macOS popup
against the mobile relay's mailbox, first response wins.

Not a generic N-way composite -- wraps exactly one native backend and one
mobile-relay backend, since that's the one pairing this issue's Phase 1
calls for (a phone answering *in addition to*, never *instead of*, the
existing desktop popup -- requirement 5). daemon_main.py only ever installs
this (via init_approval_ui()) when a MobileRelayConfig was actually found in
org_config.json; otherwise NativeApprovalUI is used directly, unwrapped, so
a deployment with no mobile relay configured sees zero behavior change.

Racing mechanics: each call starts one plain daemon thread per backend
(never a ThreadPoolExecutor -- see the correctness note in _race()'s
docstring for why that matters here) and returns as soon as either backend
returns a value, without waiting for or joining the other. The loser is
simply left running in the background:

- If native loses (the phone answered first), the desktop popup has no
  external-abort hook today (approval_window.py's runApproval_ only exits
  through its own JS bridge callback -- see that module's own docstring,
  and issue #55's comment thread for the gap this leaves). It stays open,
  showing a request that's already been decided, until a human at the Mac
  clicks something -- at which point gate.py's own request lifecycle
  applies the *first* decision only (see below) and the stale click is a
  no-op. Adding a real abort path is tracked as a Phase 1 follow-up; it
  needs approval_window.py's modal-loop plumbing changed and verified with
  scripts/qa_popup_smoke.py on real hardware, which is out of scope for the
  change that introduces this file.
- If mobile loses (native answered first), `abandon_event` (passed to every
  MobileRelayApprovalUI call) is set immediately, which stops that
  backend's poll loop within one long-poll cycle -- see
  MobileRelayApprovalUI._request_decision and MobileRelayClient.
  poll_decision's own `should_abandon` handling. The relay itself is never
  told the request was superseded (that would need a relay-side "cancel"
  endpoint Phase 0's spike doesn't have); a phone that answers after this
  point has its decision accepted by the relay but simply never read by
  the daemon, since MobileRelayApprovalUI has already returned.

Correctness does not depend on either of the above cleanups happening
promptly, or at all: this module never applies a second answer for the same
call. Once a race is decided, its result is returned and both backends'
eventual outcomes (including a very-late one from the loser) are irrelevant
-- gate.py's own gated_call() only ever awaits the one asyncio.to_thread
call this composite backs, so there is no code path that could apply a
second decision to the same gated_call invocation even if the loser
produced one.
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Any

from .approval_ui import ApprovalUI
from .mobile_relay_approval_ui import MobileRelayApprovalUI

logger = logging.getLogger(__name__)


class CompositeApprovalUI(ApprovalUI):
    def __init__(self, native: ApprovalUI, mobile: MobileRelayApprovalUI) -> None:
        self._native = native
        self._mobile = mobile

    def show_popup(self, *args: Any, **kwargs: Any) -> tuple[str, int | None]:
        return self._race("show_popup", args, kwargs)

    def show_read_popup(self, *args: Any, **kwargs: Any) -> tuple[str, int | None]:
        return self._race("show_read_popup", args, kwargs)

    def show_pii_confirmation_popup(self, *args: Any, **kwargs: Any) -> bool:
        return self._race("show_pii_confirmation_popup", args, kwargs)

    def show_rule_confirmation_popup(self, *args: Any, **kwargs: Any) -> bool:
        return self._race("show_rule_confirmation_popup", args, kwargs)

    def _race(self, method_name: str, args: tuple, kwargs: dict) -> Any:
        """First-response-wins race between the native and mobile backends.

        Uses plain `threading.Thread(daemon=True)`, not a
        `ThreadPoolExecutor` -- an executor's context manager (or a bare
        `shutdown()`) blocks on exit until every submitted task finishes,
        and even without one, Python's interpreter-exit machinery waits for
        every non-daemon thread pool worker to finish before the process can
        exit. Either would defeat the entire point of "return as soon as one
        backend answers, leave the loser running in the background" -- a
        daemon thread, by contrast, never blocks this method's return or the
        interpreter's shutdown.
        """
        abandon_event = threading.Event()
        outcomes: queue.Queue[tuple[str, Exception | None, Any]] = queue.Queue()

        def run(label: str, call) -> None:
            try:
                outcomes.put((label, None, call()))
            except Exception as exc:  # noqa: BLE001 -- one backend's failure must
                                       # never crash the other's race entry
                outcomes.put((label, exc, None))

        threading.Thread(
            target=run, args=("native", lambda: getattr(self._native, method_name)(*args, **kwargs)),
            daemon=True,
        ).start()
        threading.Thread(
            target=run,
            args=(
                "mobile",
                lambda: getattr(self._mobile, method_name)(*args, **kwargs, abandon_event=abandon_event),
            ),
            daemon=True,
        ).start()

        errors: dict[str, Exception] = {}
        for _ in range(2):
            label, error, value = outcomes.get()
            if error is None:
                abandon_event.set()  # no-op if the other side already finished
                return value
            logger.warning("%s approval backend raised while racing %s: %s", label, method_name, error)
            errors[label] = error

        # Both backends failed -- fail closed rather than hang forever:
        # surface native's exception preferentially, since it's the primary
        # path and its failure is the more actionable signal.
        raise errors.get("native") or errors["mobile"]
