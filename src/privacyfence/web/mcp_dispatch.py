"""Connector-call dispatch for the ``/mcp`` endpoint -- the Streamable HTTP
counterpart of ipc_server.py's ``IPCServer``.

Deliberately a separate, self-contained implementation rather than a shared
refactor of ``IPCServer`` (which has its own large, already-green test suite
this phase has no reason to put at risk): §8.1 of
docs/https-connector-refactor-plan.md describes this as "a translation of
bridge/src/tools.ts's schema mapping into Python, not a redesign", and the
same applies to the dispatch logic this module ports from
``ipc_server.IPCServer``'s ``_call_connector``/``_check_policy``/
``_list_rules``/``_propose_rule_change``/``_build_manifest``/
begin-end-unattended-session -- same dedupe/staleness/gating rules, same
audit entries, ported onto a session key that isn't ``id(writer)`` (there is
no writer here) but plays the identical role: one value, stable for the
life of one logical connection, that dedupe/unattended-session state is
scoped to. routes_mcp.py supplies a fresh UUID per Streamable HTTP session
(via the low-level Server's own per-session lifespan, see that module) as
the session key.

The duplication with ipc_server.py is temporary by construction: P5 deletes
``ipc_server.py`` entirely once the bridge is retired
(docs/https-connector-refactor-plan.md §12), at which point this module (or
its P3 successor, ``approvals.py``) is the only one left.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Hashable

from ..approvals import PendingApprovalRegistry
from ..audit_log import AuditEntry, current_week, get_audit_logger
from ..auto_accept import TOOL_TO_GATE, TOOL_TO_OPERATION, get_auto_accept_evaluator, get_current_config
from ..connector import Connector
from ..gate import propose_rule_change, reason_scope, unattended_scope

logger = logging.getLogger(__name__)


class McpDispatcher:
    """Owns dedupe/unattended-session state for the ``/mcp`` endpoint and
    dispatches every connector call and meta-tool through it -- see module
    docstring for why this mirrors ``IPCServer`` rather than sharing code
    with it.

    ``connectors_provider`` is called fresh on every dispatch rather than
    captured once, so a connector rebuild pushed live elsewhere (e.g.
    ``SettingsController.refresh_connectors``, which today only calls
    ``IPCServer.set_connectors``) is picked up here too as soon as both call
    sites are updated to also push into this dispatcher -- see
    daemon_main.py's wiring.
    """

    _DEDUPE_TTL_SECONDS = 30
    _DEDUPE_EXEMPT_TOOLS = frozenset({"gmail_create_label"})
    # privacyfence_await_approval's own timeout_seconds is clamped into this
    # range regardless of what the caller asked for -- fail-safe against a
    # client-supplied value of 0 (busy-poll) or something absurdly large
    # (holding the connection open indefinitely). Polled, not evented: the
    # registry has no pub/sub of its own (see approvals.py), and polling a
    # few in-memory dict lookups is cheap enough not to need one here either.
    _AWAIT_APPROVAL_MIN_TIMEOUT = 1
    _AWAIT_APPROVAL_MAX_TIMEOUT = 120
    _AWAIT_APPROVAL_POLL_SECONDS = 0.5

    def __init__(
        self,
        connectors_provider: Callable[[], dict[str, Connector]],
        *,
        unattended_sessions_enabled: bool = False,
        registry: PendingApprovalRegistry | None = None,
    ) -> None:
        self._connectors_provider = connectors_provider
        self._inflight: dict[str, tuple[Any, float]] = {}
        self._last_write_at: dict[str, float] = {}
        self._unattended_sessions_enabled = unattended_sessions_enabled
        self._unattended_sessions: set[Hashable] = set()
        self._unattended_changed_listener: Callable[[], None] | None = None
        # The deferred-approval registry privacyfence_await_approval polls
        # (P3, docs/https-connector-refactor-plan.md §5.2 point 7) -- None
        # when nothing in this install can ever produce a pending approval
        # (native-only local mode with /mcp still enabled), in which case
        # every id this tool is asked about is simply "unknown".
        self._registry = registry

    @property
    def connectors(self) -> dict[str, Connector]:
        return self._connectors_provider()

    def set_unattended_changed_listener(self, callback: Callable[[], None] | None) -> None:
        self._unattended_changed_listener = callback

    # ------------------------------------------------------------------ #
    # Manifest
    # ------------------------------------------------------------------ #

    def build_manifest(self) -> dict:
        """Same shape as ``IPCServer._build_manifest`` -- kept for parity/
        debugging even though routes_mcp.py's ``list_tools`` handler builds
        MCP ``Tool`` objects (mcp_tools.to_mcp_tool) rather than consuming
        this dict directly."""
        return {
            "connectors": [
                {"name": c.name, "tools": [spec.to_dict() for spec in c.tool_specs()]}
                for c in self.connectors.values()
            ]
        }

    # ------------------------------------------------------------------ #
    # Connector calls -- dedupe/staleness logic ported from
    # IPCServer._call_connector; see that method's own docstring (module
    # docstring of ipc_server.py) for the full rationale.
    # ------------------------------------------------------------------ #

    async def call(self, session_key: Hashable, connector_name: str, tool: str, args: dict) -> Any:
        # "reason" must never reach the dedupe key or connector.call() --
        # see ipc_server.py's _call_connector docstring for why (a freshly
        # regenerated reason string on every retry would defeat dedupe).
        args = dict(args)
        reason = args.pop("reason", "")
        connector = self.connectors.get(connector_name)
        if connector is None:
            raise ValueError(f"Unknown connector: {connector_name!r}")

        now = time.time()
        self._prune_stale(now)
        key = self._dedupe_key(connector_name, tool, args)
        entry = self._inflight.get(key)
        if entry is not None:
            fut, recorded_at = entry
            still_fresh = (now - recorded_at) < self._DEDUPE_TTL_SECONDS
            read_is_stale = self._is_read_only(connector, tool) and (
                recorded_at <= self._last_write_at.get(connector_name, 0.0)
            )
            reusable = not fut.done() or (
                still_fresh and tool not in self._DEDUPE_EXEMPT_TOOLS and not read_is_stale
            )
            if reusable:
                logger.info(
                    "Deduping repeat call to %s/%s: reusing in-flight/recent result", connector_name, tool,
                )
                return await fut

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = (fut, now)
        try:
            with unattended_scope(session_key in self._unattended_sessions), reason_scope(reason):
                result = await connector.call(tool, args)
        except asyncio.CancelledError:
            self._inflight.pop(key, None)
            if not fut.done():
                fut.cancel()
            raise
        except Exception as exc:
            fut.set_exception(exc)
            fut.exception()  # mark retrieved -- see ipc_server.py's identical comment
            raise
        fut.set_result(result)
        if not self._is_read_only(connector, tool):
            self._last_write_at[connector_name] = time.time()
        return result

    def _prune_stale(self, now: float) -> None:
        stale = [
            key for key, (fut, recorded_at) in self._inflight.items()
            if fut.done() and (now - recorded_at) >= self._DEDUPE_TTL_SECONDS
        ]
        for key in stale:
            del self._inflight[key]

    @staticmethod
    def _dedupe_key(connector_name: str, tool: str, args: dict) -> str:
        return f"{connector_name}:{tool}:{json.dumps(args, sort_keys=True, default=str)}"

    @staticmethod
    def _is_read_only(connector: Connector, tool: str) -> bool:
        for spec in connector.tool_specs():
            if spec.name == tool:
                return spec.read_only
        return False

    # ------------------------------------------------------------------ #
    # Meta-tools -- ported from IPCServer._check_policy/_list_rules/
    # _propose_rule_change (see ipc_server.py for the full rationale on
    # each; identical behavior, same audit entries).
    # ------------------------------------------------------------------ #

    def check_policy(self, connector_name: str, tool: str, args: dict, claude_reason: str = "") -> dict:
        connector = self.connectors.get(connector_name)
        if connector is None:
            raise ValueError(f"Unknown connector: {connector_name!r}")

        gate = TOOL_TO_GATE.get(tool)
        if gate is None:
            raise ValueError(f"Unknown tool: {tool!r}")

        if gate == "auto":
            result = {
                "gate": "auto", "verdict": "auto_accept", "matched_rule": None,
                "reason": "Unconditionally auto-accepted -- never reaches the review gate.",
                "pii_gate_may_apply": False,
            }
        else:
            operation_key = TOOL_TO_OPERATION.get(tool, f"{connector_name}.{tool}")
            my_email = getattr(connector, "my_email", "")
            verdict, matched_rule, reason = get_auto_accept_evaluator().preflight_from_args(
                operation_key, args, my_email
            )
            if gate == "review":
                reason += (
                    " Read calls also pass through the PII detection gate, which scans actual "
                    "content and can force a popup even when a rule matches -- this can't be "
                    "predicted before the read happens."
                )
            result = {
                "gate": gate, "verdict": verdict, "matched_rule": matched_rule or None,
                "reason": reason, "pii_gate_may_apply": gate == "review",
            }

        self._audit_policy_check(connector_name, tool, result, claude_reason)
        return result

    @staticmethod
    def _audit_policy_check(connector_name: str, tool: str, result: dict, claude_reason: str = "") -> None:
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id=uuid.uuid4().hex[:12],
                connector=connector_name,
                tool=tool,
                tool_name="",
                summary=f"Preflight check: verdict={result['verdict']!r}",
                sender="",
                decision="policy_check",
                auto_accept_rule=result.get("matched_rule") or "",
                latency_seconds=0.0,
                pii_detected=False,
                claude_reason=claude_reason,
            ))
        except Exception as exc:
            logger.warning("Audit log write failed for policy check: %s", exc)

    @staticmethod
    def list_rules(claude_reason: str = "") -> dict:
        result = get_current_config()
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id=uuid.uuid4().hex[:12],
                connector="",
                tool="",
                tool_name="",
                summary="Listed current auto-accept rules/grants",
                sender="",
                decision="rules_listed",
                auto_accept_rule="",
                latency_seconds=0.0,
                pii_detected=False,
                claude_reason=claude_reason,
            ))
        except Exception as exc:
            logger.warning("Audit log write failed for list_rules: %s", exc)
        return result

    async def await_approval(self, approval_ids: list[str], timeout_seconds: int = 30) -> dict[str, str]:
        """privacyfence_await_approval's handler: long-poll ``approval_ids``
        against the registry and return their current status once anything
        changes, or once the (clamped) timeout elapses -- whichever comes
        first. Status only, never content (§5.2 point 7 -- see
        approvals.PendingApprovalRegistry.await_status's own docstring for
        the exact vocabulary)."""
        ids = [str(i) for i in (approval_ids or [])]
        if not ids:
            return {}
        if self._registry is None:
            return {approval_id: "unknown" for approval_id in ids}
        timeout = max(
            self._AWAIT_APPROVAL_MIN_TIMEOUT,
            min(int(timeout_seconds or 0), self._AWAIT_APPROVAL_MAX_TIMEOUT),
        )
        deadline = time.time() + timeout
        while True:
            statuses = {approval_id: self._registry.await_status(approval_id) for approval_id in ids}
            if time.time() >= deadline or any(s != "pending" for s in statuses.values()):
                return statuses
            await asyncio.sleep(self._AWAIT_APPROVAL_POLL_SECONDS)

    @staticmethod
    async def propose_rule_change(params: dict) -> dict:
        return await propose_rule_change(
            target=params["target"],
            operation=params["operation"],
            reason=params.get("reason", ""),
            operation_key=params.get("operation_key", ""),
            rule_name=params.get("rule_name", ""),
            value=params.get("value"),
            old_value=params.get("old_value"),
            connector=params.get("connector", ""),
            config_key=params.get("config_key", ""),
            resource_id=params.get("resource_id", ""),
            name=params.get("name"),
            tab=params.get("tab"),
            capabilities=params.get("capabilities"),
        )

    # ------------------------------------------------------------------ #
    # Unattended sessions -- ported from IPCServer.begin/end_unattended_
    # session/unattended_session_count/_audit_unattended_session_event.
    # Cleared explicitly by end_unattended_session, or by end_session()
    # (routes_mcp.py calls this from the per-MCP-session lifespan's own
    # finally block -- the direct counterpart of ipc_server.py's
    # _handle_connection finally block clearing id(writer)).
    # ------------------------------------------------------------------ #

    def begin_unattended_session(self, session_key: Hashable, claude_reason: str = "") -> dict:
        if not self._unattended_sessions_enabled:
            raise ValueError(
                "Unattended sessions are disabled. An administrator must set "
                "unattended_sessions.enabled: true in the organization config bundle "
                "(org_config.json) before this connection can be marked unattended."
            )
        self._unattended_sessions.add(session_key)
        logger.warning(
            "Unattended session started on MCP session %r -- unmatched review/popup calls on "
            "this session will now be denied immediately instead of prompting",
            session_key,
        )
        self._audit_unattended_session_event("unattended_session_started", claude_reason)
        self._fire_unattended_changed()
        return {"unattended": True}

    def end_unattended_session(self, session_key: Hashable, claude_reason: str = "") -> dict:
        changed = session_key in self._unattended_sessions
        self._unattended_sessions.discard(session_key)
        logger.info("Unattended session ended on MCP session %r", session_key)
        if changed:
            self._audit_unattended_session_event("unattended_session_ended", claude_reason)
            self._fire_unattended_changed()
        return {"unattended": False}

    def end_session(self, session_key: Hashable) -> None:
        """Called once, when the MCP session this key identifies ends (see
        module docstring) -- whatever unattended-session state it carried
        dies with it, exactly like a dropped bridge connection today."""
        had_unattended = session_key in self._unattended_sessions
        self._unattended_sessions.discard(session_key)
        if had_unattended:
            self._audit_unattended_session_event("unattended_session_ended")
            self._fire_unattended_changed()

    def unattended_session_count(self) -> int:
        return len(self._unattended_sessions)

    def _fire_unattended_changed(self) -> None:
        if self._unattended_changed_listener is not None:
            self._unattended_changed_listener()

    @staticmethod
    def _audit_unattended_session_event(decision: str, claude_reason: str = "") -> None:
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id=uuid.uuid4().hex[:12],
                connector="",
                tool="",
                tool_name="",
                summary="This MCP session's unattended-session state changed",
                sender="",
                decision=decision,
                auto_accept_rule="",
                latency_seconds=0.0,
                pii_detected=False,
                claude_reason=claude_reason,
            ))
        except Exception as exc:
            logger.warning("Audit log write failed for unattended-session event: %s", exc)
