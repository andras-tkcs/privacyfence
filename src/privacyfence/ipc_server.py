"""IPC server: asyncio Unix socket server that runs inside the daemon.

Handles the JSON-RPC-style methods described in ipc.py. Connector.call() may
block for an arbitrary duration (a gated call waiting on a native approval
popup), so each request is dispatched as a separate asyncio Task — multiple
in-flight requests from the same bridge connection are fully concurrent.
Popup display itself is serialized by gate.py's own lock so only one dialog
is ever on screen at a time.

A gated call sitting on a popup can easily take longer than the calling MCP
client's own tool-call timeout; when that fires, the client retries with an
identical request while the first one is still waiting on the user (or has
just finished) -- from here that's indistinguishable from the user
genuinely asking for the same write twice, so it would otherwise double up
the approval popup for one logical action. ``_call_connector`` dedupes
identical (connector, tool, args) calls: a retry that arrives while the
original is still in flight, or shortly after it completed, is served the
same result instead of re-running the gate.

A handful of tools break that assumption: a second identical call is
*supposed* to behave differently once the first has taken effect (e.g.
"create label X" should fail with "already exists" the second time, not
silently replay the first call's success). Those are listed in
``_DEDUPE_EXEMPT_TOOLS`` and only lose the completed-result reuse -- a
genuinely concurrent in-flight retry is still coalesced, since nothing has
taken effect yet there.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .audit_log import AuditEntry, current_week, get_audit_logger
from .auto_accept import TOOL_TO_GATE, TOOL_TO_OPERATION, get_auto_accept_evaluator
from .connector import Connector, ToolSpec
from .gate import unattended_scope
from .ipc import LINE_LIMIT, SOCKET_PATH, VERSION

logger = logging.getLogger(__name__)


class IPCServer:
    """Listens on SOCKET_PATH and dispatches connector calls."""

    # How long a completed call's result is kept around to serve an
    # identical retry without re-running it. Long enough to cover a client
    # timeout-and-retry (observed ~7s apart in practice), short enough that a
    # deliberate repeat of the same write minutes later isn't silently
    # short-circuited.
    _DEDUPE_TTL_SECONDS = 30

    # Tools exempt from completed-result reuse -- see module docstring.
    _DEDUPE_EXEMPT_TOOLS = frozenset({"gmail_create_label"})

    def __init__(self, connectors: list[Connector], *, unattended_sessions_enabled: bool = False) -> None:
        self._connectors: dict[str, Connector] = {c.name: c for c in connectors}
        self._server: asyncio.AbstractServer | None = None
        self._inflight: dict[str, tuple[asyncio.Future, float]] = {}
        # Opt-in gate for privacyfence_begin_unattended_session -- see
        # settings.yaml.example's unattended_sessions.enabled and
        # docs/cowork-scheduled-tasks-design.md. Off by default: a Claude
        # session gaining the ability to switch its own connection into
        # fail-fast mode is a deliberate per-organization choice.
        self._unattended_sessions_enabled = unattended_sessions_enabled
        # id(writer) -> currently in an unattended session. Connection-scoped
        # (not global) since the bridge is one process per Cowork task, so
        # "per connection" already means "per scheduled run"; cleaned up on
        # disconnect in _handle_connection's finally so a dropped connection
        # can never leave a stale entry behind.
        self._unattended_connections: set[int] = set()
        # Fired (on this asyncio thread -- the listener is responsible for
        # marshaling onto rumps' main thread) whenever membership of
        # _unattended_connections actually changes, so the menu bar's live
        # indicator can stay current without polling.
        self._unattended_changed_listener: Callable[[], None] | None = None

    def set_connectors(self, connectors: list[Connector]) -> None:
        """Swap in a freshly built connector set (e.g. after the menu bar
        authenticates a service or toggles one on/off). Called from the
        rumps main thread; the dict reassignment is a single atomic
        reference swap so no lock is needed against the IPC asyncio thread.
        """
        self._connectors = {c.name: c for c in connectors}

    def set_unattended_changed_listener(self, callback: Callable[[], None] | None) -> None:
        """Register a callback fired whenever unattended_session_count()
        changes. Called from the IPC server's own asyncio thread -- the
        menu bar uses this to marshal a menu rebuild onto its main thread
        via AppHelper.callAfter, the same pattern auto_accept.py's
        set_rules_changed_listener uses for rule changes.
        """
        self._unattended_changed_listener = callback

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        os.makedirs(os.path.dirname(SOCKET_PATH), exist_ok=True)
        # Remove stale socket file from a previous run.
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass
        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=SOCKET_PATH, limit=LINE_LIMIT
        )
        logger.info("IPC server listening on %s", SOCKET_PATH)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------ #
    # Connection handler
    # ------------------------------------------------------------------ #

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername") or "<unknown>"
        logger.debug("Bridge connected: %s", peer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                asyncio.create_task(self._dispatch(line, writer))
        except (ConnectionResetError, asyncio.IncompleteReadError, ValueError) as exc:
            logger.warning("Bridge connection %s terminated: %s", peer, exc)
        finally:
            logger.debug("Bridge disconnected: %s", peer)
            # Whatever unattended-session state this connection carried dies
            # with it -- there's no path where a dropped connection should
            # leave a stale "in an unattended session" entry behind.
            had_unattended = id(writer) in self._unattended_connections
            self._unattended_connections.discard(id(writer))
            if had_unattended:
                self._audit_unattended_session_event("unattended_session_ended")
                self._fire_unattended_changed()
            writer.close()

    async def _dispatch(self, raw: bytes, writer: asyncio.StreamWriter) -> None:
        req_id = None
        try:
            msg = json.loads(raw)
            req_id = msg.get("id")
            method = msg.get("method")
            params = msg.get("params", {})

            if method == "health":
                result = {"version": VERSION, "connectors": list(self._connectors)}
            elif method == "manifest":
                result = self._build_manifest()
            elif method == "call":
                with unattended_scope(id(writer) in self._unattended_connections):
                    result = await self._call_connector(params)
            elif method == "check_policy":
                result = self._check_policy(params)
            elif method == "begin_unattended_session":
                result = self._begin_unattended_session(writer)
            elif method == "end_unattended_session":
                result = self._end_unattended_session(writer)
            else:
                raise ValueError(f"Unknown method: {method!r}")

            await self._send(writer, {"id": req_id, "result": result})
        except Exception as exc:  # noqa: BLE001
            logger.error("IPC dispatch error for request %s: %s", req_id, exc, exc_info=True)
            await self._send(writer, {"id": req_id, "error": str(exc)})

    async def _call_connector(self, params: dict) -> Any:
        connector_name = params["connector"]
        tool = params["tool"]
        args = params.get("args", {})
        connector = self._connectors.get(connector_name)
        if connector is None:
            raise ValueError(f"Unknown connector: {connector_name!r}")

        now = time.time()
        self._prune_stale(now)
        key = self._dedupe_key(connector_name, tool, args)
        entry = self._inflight.get(key)
        if entry is not None:
            fut, recorded_at = entry
            still_fresh = (now - recorded_at) < self._DEDUPE_TTL_SECONDS
            reusable = not fut.done() or (still_fresh and tool not in self._DEDUPE_EXEMPT_TOOLS)
            if reusable:
                logger.info(
                    "Deduping repeat call to %s/%s: reusing in-flight/recent result",
                    connector_name, tool,
                )
                return await fut

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = (fut, now)
        try:
            result = await connector.call(tool, args)
        except Exception as exc:
            fut.set_exception(exc)
            fut.exception()  # mark retrieved so an unwaited future doesn't log "never retrieved"
            raise
        fut.set_result(result)
        return result

    def _check_policy(self, params: dict) -> dict:
        """Preflight for privacyfence_check_policy -- see ipc.py's module docstring.

        Deliberately bypasses Connector.call() entirely: no external API
        call, no popup, no mutation of anything. Records a lightweight
        "policy_check" audit entry (not a real decision) so a scheduled
        task repeatedly probing something it's never allowed to do shows up
        in the log, same as any other pattern worth noticing.
        """
        connector_name = params["connector"]
        tool = params["tool"]
        args = params.get("args", {})
        connector = self._connectors.get(connector_name)
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

        self._audit_policy_check(connector_name, tool, result)
        return result

    @staticmethod
    def _audit_policy_check(connector_name: str, tool: str, result: dict) -> None:
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
            ))
        except Exception as exc:
            logger.warning("Audit log write failed for policy check: %s", exc)

    @staticmethod
    def _audit_unattended_session_event(decision: str) -> None:
        """Session-level audit entry for begin/end_unattended_session --
        this connection's gate posture just changed, which is a governance
        decision in its own right, not just a bookkeeping detail."""
        try:
            get_audit_logger().record(AuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                week=current_week(),
                request_id=uuid.uuid4().hex[:12],
                connector="",
                tool="",
                tool_name="",
                summary="This connection's unattended-session state changed",
                sender="",
                decision=decision,
                auto_accept_rule="",
                latency_seconds=0.0,
                pii_detected=False,
            ))
        except Exception as exc:
            logger.warning("Audit log write failed for unattended-session event: %s", exc)

    def _begin_unattended_session(self, writer: asyncio.StreamWriter) -> dict:
        """privacyfence_begin_unattended_session -- see settings.yaml.example's
        unattended_sessions.enabled and docs/cowork-scheduled-tasks-design.md.
        """
        if not self._unattended_sessions_enabled:
            raise ValueError(
                "Unattended sessions are disabled. An administrator must set "
                "unattended_sessions.enabled: true in settings.yaml before this connection "
                "can be marked unattended."
            )
        self._unattended_connections.add(id(writer))
        logger.warning(
            "Unattended session started on connection %s -- unmatched review/popup calls on this "
            "connection will now be denied immediately instead of prompting",
            writer.get_extra_info("peername") or "<unknown>",
        )
        self._audit_unattended_session_event("unattended_session_started")
        self._fire_unattended_changed()
        return {"unattended": True}

    def _end_unattended_session(self, writer: asyncio.StreamWriter) -> dict:
        changed = id(writer) in self._unattended_connections
        self._unattended_connections.discard(id(writer))
        logger.info(
            "Unattended session ended on connection %s", writer.get_extra_info("peername") or "<unknown>"
        )
        if changed:
            self._audit_unattended_session_event("unattended_session_ended")
            self._fire_unattended_changed()
        return {"unattended": False}

    def unattended_session_count(self) -> int:
        """Number of connections currently in an unattended session -- read
        by the menu bar for its live indicator (see menu_bar.py)."""
        return len(self._unattended_connections)

    def _fire_unattended_changed(self) -> None:
        if self._unattended_changed_listener is not None:
            self._unattended_changed_listener()

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

    def _build_manifest(self) -> dict:
        return {
            "version": VERSION,
            "connectors": [
                {
                    "name": c.name,
                    "tools": [spec.to_dict() for spec in c.tool_specs()],
                }
                for c in self._connectors.values()
            ]
        }

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, msg: dict) -> None:
        try:
            data = json.dumps(msg, default=str) + "\n"
            writer.write(data.encode())
            await writer.drain()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to send IPC response: %s", exc)
