"""IPC server: asyncio TCP loopback server that runs inside the daemon.

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

Read-only tools (``ToolSpec.read_only``) lose completed-result reuse only
when a write to the *same connector* has completed since this particular
result was produced (``_last_write_at``) -- not unconditionally. The
concern this guards is real: a read repeated with identical args shortly
after an unrelated write to the same resource (e.g. checking an event's
visibility right after setting it) must see the write's effect, not a
cached pre-write result. But refusing reuse for every completed read,
always, defeats the whole mechanism for the read side for no reason: a
gated read sitting on the review popup is exactly as likely to outlast the
client's timeout as a gated write is, and a retry landing just after the
human approved it used to re-run the entire fetch and show the popup a
second time for a decision already made (see
docs/slack-performance-review.md's item #8) -- reads are silent or
independently gated per call, so that popup, not staleness, is what
completed-result reuse actually needs to prevent for them too. A
genuinely concurrent in-flight duplicate read is still coalesced either
way, since no result exists yet to be stale. The write-tracking is
per-connector, not per-resource -- coarser than tracking exactly which
write could affect which read, but never wrong in the direction that
matters: at worst it re-runs a read that some unrelated write in the same
connector didn't actually touch, never serves a read stale relative to a
write that did.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .audit_log import AuditEntry, current_week, get_audit_logger
from .auto_accept import TOOL_TO_GATE, TOOL_TO_OPERATION, get_auto_accept_evaluator, get_current_config
from .connector import Connector
from .gate import propose_rule_change, reason_scope, unattended_scope
from .ipc import HOST, LINE_LIMIT, PORT_FILE, TOKEN_FILE, VERSION

logger = logging.getLogger(__name__)


class IPCServer:
    """Listens on HOST at an OS-assigned ephemeral port and dispatches
    connector calls, gated by a per-launch random auth token (see start())
    -- see ipc.py's module docstring for the connection handshake and for
    why the port is discovered (PORT_FILE) rather than fixed."""

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
        # Generated fresh in start() -- see that method's comment on the
        # listen-then-write-files ordering.
        self._token: str = ""
        self._inflight: dict[str, tuple[asyncio.Future, float]] = {}
        # (id(writer), request "id") -> the asyncio.Task dispatching that
        # request, for as long as it's in flight -- what the "cancel"
        # method (see ipc.py's module docstring) looks up to call .cancel()
        # on. Keyed by connection identity too, not just the request id
        # alone: each bridge connection's own id counter starts fresh, so
        # two different connections can otherwise reuse the same id.
        self._request_tasks: dict[tuple[int, Any], asyncio.Task] = {}
        # connector name -> time.time() of its most recently *completed*
        # write (a non-read_only tool call that didn't raise). See
        # _call_connector's read-reuse check just below _inflight's own
        # comment -- a cached read older than the connector's own last write
        # is the one case completed-result reuse must refuse, not every
        # read unconditionally.
        self._last_write_at: dict[str, float] = {}
        # Opt-in gate for privacyfence_begin_unattended_session -- see
        # org_config.json's unattended_sessions.enabled. Off by
        # default: a Claude session gaining the ability to switch its own
        # connection into fail-fast mode is a deliberate per-organization
        # choice.
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

    @property
    def connectors(self) -> dict[str, Connector]:
        """Read-only view of the live connector set -- web/mcp_dispatch.py's
        ``McpDispatcher`` polls this (rather than holding its own copy) so
        the single call to ``set_connectors`` below (SettingsController.
        refresh_connectors) keeps both the bridge and the ``/mcp`` endpoint
        in sync with no second push needed. See daemon_main.py's wiring."""
        return self._connectors

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
        # port=0 asks the OS for a free ephemeral port -- see ipc.py's module
        # docstring on why this is discovered (PORT_FILE) rather than a
        # fixed, hardcoded number both sides agree on. The actual port is
        # only known once the server is already bound and listening, so
        # PORT_FILE/TOKEN_FILE are written after start_server() returns, not
        # before -- unlike the old fixed-port design, a bridge that only
        # proceeds once it can read PORT_FILE can never observe one without
        # a real listener already behind it.
        self._token = secrets.token_hex(32)
        self._server = await asyncio.start_server(
            self._handle_connection, host=HOST, port=0, limit=LINE_LIMIT
        )
        port = self._server.sockets[0].getsockname()[1]
        self._write_permissioned_file(TOKEN_FILE, self._token)
        self._write_permissioned_file(PORT_FILE, str(port))
        logger.info("IPC server listening on %s:%s", HOST, port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for path in (TOKEN_FILE, PORT_FILE):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    @staticmethod
    def _write_permissioned_file(path: str, content: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)

    # ------------------------------------------------------------------ #
    # Connection handler
    # ------------------------------------------------------------------ #

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername") or "<unknown>"
        try:
            # Auth handshake: the first line on every new connection must be
            # the current launch's token as bare text (not JSON) -- see
            # ipc.py's module docstring. Anything else, including the
            # connection dropping before a full line arrives, is treated the
            # same as a failed auth: close without responding.
            auth_line = await reader.readline()
            if auth_line.decode("utf-8", errors="replace").strip() != self._token:
                logger.warning("Connection %s failed IPC auth; closing", peer)
                return
            logger.debug("Bridge connected: %s", peer)
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
        task_key: tuple[int, Any] | None = None
        try:
            msg = json.loads(raw)
            req_id = msg.get("id")
            method = msg.get("method")
            params = msg.get("params", {})
            # Registered before dispatching to any handler (including
            # "cancel" itself, though nothing ever targets a cancel request
            # -- it completes far too fast) so "cancel" can look this
            # request up by id the instant its own message is parsed.
            if req_id is not None:
                task_key = (id(writer), req_id)
                self._request_tasks[task_key] = asyncio.current_task()  # type: ignore[assignment]

            if method == "health":
                result = {"version": VERSION, "connectors": list(self._connectors)}
            elif method == "manifest":
                result = self._build_manifest()
            elif method == "call":
                with unattended_scope(id(writer) in self._unattended_connections):
                    result = await self._call_connector(params)
            elif method == "cancel":
                result = self._cancel_request(id(writer), params)
            elif method == "check_policy":
                result = self._check_policy(params)
            elif method == "list_rules":
                result = self._list_rules(params)
            elif method == "propose_rule_change":
                with unattended_scope(id(writer) in self._unattended_connections):
                    result = await self._propose_rule_change(params)
            elif method == "begin_unattended_session":
                result = self._begin_unattended_session(writer, params.get("reason", ""))
            elif method == "end_unattended_session":
                result = self._end_unattended_session(writer, params.get("reason", ""))
            else:
                raise ValueError(f"Unknown method: {method!r}")

            await self._send(writer, {"id": req_id, "result": result})
        except asyncio.CancelledError:
            # This request's own task was the target of a "cancel" -- still
            # owed exactly one response so the bridge's own pending-request
            # map doesn't leak a promise nothing will ever resolve. _send is
            # itself best-effort (catches and logs), so no extra guard
            # needed around it here.
            logger.info("Request %s cancelled", req_id)
            await self._send(writer, {"id": req_id, "error": "cancelled"})
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("IPC dispatch error for request %s: %s", req_id, exc, exc_info=True)
            await self._send(writer, {"id": req_id, "error": str(exc)})
        finally:
            if task_key is not None:
                self._request_tasks.pop(task_key, None)

    def _cancel_request(self, writer_id: int, params: dict) -> dict:
        """Handler for the "cancel" method -- see ipc.py's module docstring
        for the full contract. Synchronous and immediate: finding and
        signalling the target task never itself needs to await anything.
        """
        target_id = params.get("target_id")
        task = self._request_tasks.get((writer_id, target_id))
        if task is None or task.done():
            return {"cancelled": False}
        task.cancel()
        return {"cancelled": True}

    async def _call_connector(self, params: dict) -> Any:
        connector_name = params["connector"]
        tool = params["tool"]
        args = params.get("args", {})
        # Every gated/auto tool's ToolSpec declares a required "reason"
        # param, but it must never reach _dedupe_key or connector.call():
        # left in args, a
        # client-timeout retry with freshly-regenerated reason text would
        # get a different dedupe key every time and silently defeat the
        # coalescing this method's docstring describes -- exactly the
        # double-popup bug that mechanism exists to prevent. It's also not
        # a parameter any connector method actually accepts (no method
        # signature changed for this feature -- see gate.py's reason_scope
        # docstring), so leaving it in args would raise a TypeError on
        # every single gated call. Popped here, once, centrally; carried
        # from here on via reason_scope, the same pattern unattended_scope
        # already uses for connection-scoped state.
        reason = args.pop("reason", "")
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
            # A completed read is reusable unless some write to this same
            # connector has completed since -- see the module docstring's
            # "Read-only tools" paragraph for why this replaced an
            # unconditional refusal.
            read_is_stale = self._is_read_only(connector, tool) and (
                recorded_at <= self._last_write_at.get(connector_name, 0.0)
            )
            reusable = not fut.done() or (
                still_fresh
                and tool not in self._DEDUPE_EXEMPT_TOOLS
                and not read_is_stale
            )
            if reusable:
                logger.info(
                    "Deduping repeat call to %s/%s: reusing in-flight/recent result",
                    connector_name, tool,
                )
                return await fut

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = (fut, now)
        try:
            with reason_scope(reason):
                result = await connector.call(tool, args)
        except asyncio.CancelledError:
            # Not caught by "except Exception" below -- CancelledError is a
            # BaseException. Popped from _inflight immediately (rather than
            # left as a done-but-cancelled entry for the dedupe TTL) so a
            # later, genuinely new identical call starts fresh instead of
            # being handed -- or itself immediately cancelled by -- this
            # one's outcome. A concurrent duplicate already awaiting this
            # exact fut still correctly observes the cancellation; it holds
            # its own reference to fut independent of the dict.
            self._inflight.pop(key, None)
            if not fut.done():
                fut.cancel()
            raise
        except Exception as exc:
            fut.set_exception(exc)
            fut.exception()  # mark retrieved so an unwaited future doesn't log "never retrieved"
            raise
        fut.set_result(result)
        if not self._is_read_only(connector, tool):
            # A write that raised didn't take effect (or at least isn't
            # known to have), so only a successful one invalidates cached
            # reads for this connector -- see the module docstring.
            self._last_write_at[connector_name] = time.time()
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
        claude_reason = params.get("reason", "")
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
    def _list_rules(params: dict) -> dict:
        """list_rules -- see ipc.py's module docstring. Records a lightweight
        "rules_listed" audit entry (like _check_policy's "policy_check")
        since it discloses the full current rule/grant set."""
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
                claude_reason=params.get("reason", ""),
            ))
        except Exception as exc:
            logger.warning("Audit log write failed for list_rules: %s", exc)
        return result

    @staticmethod
    async def _propose_rule_change(params: dict) -> dict:
        """propose_rule_change -- see ipc.py's module docstring and
        gate.propose_rule_change()'s own docstring for the full field list
        and the "gate only" write guarantee. Wrapped in unattended_scope by
        _dispatch the same way "call" is, since gate.py's is_unattended()
        check applies here too."""
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

    @staticmethod
    def _audit_unattended_session_event(decision: str, claude_reason: str = "") -> None:
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
                claude_reason=claude_reason,
            ))
        except Exception as exc:
            logger.warning("Audit log write failed for unattended-session event: %s", exc)

    def _begin_unattended_session(self, writer: asyncio.StreamWriter, claude_reason: str = "") -> dict:
        """privacyfence_begin_unattended_session -- see org_config.json's
        unattended_sessions.enabled and docs/TECHNICAL_REFERENCE.md's
        "Scheduled / unattended Cowork tasks" section.
        """
        if not self._unattended_sessions_enabled:
            raise ValueError(
                "Unattended sessions are disabled. An administrator must set "
                "unattended_sessions.enabled: true in the organization config bundle "
                "(org_config.json) before this connection can be marked unattended."
            )
        self._unattended_connections.add(id(writer))
        logger.warning(
            "Unattended session started on connection %s -- unmatched review/popup calls on this "
            "connection will now be denied immediately instead of prompting",
            writer.get_extra_info("peername") or "<unknown>",
        )
        self._audit_unattended_session_event("unattended_session_started", claude_reason)
        self._fire_unattended_changed()
        return {"unattended": True}

    def _end_unattended_session(self, writer: asyncio.StreamWriter, claude_reason: str = "") -> dict:
        changed = id(writer) in self._unattended_connections
        self._unattended_connections.discard(id(writer))
        logger.info(
            "Unattended session ended on connection %s", writer.get_extra_info("peername") or "<unknown>"
        )
        if changed:
            self._audit_unattended_session_event("unattended_session_ended", claude_reason)
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

    @staticmethod
    def _is_read_only(connector: Connector, tool: str) -> bool:
        for spec in connector.tool_specs():
            if spec.name == tool:
                return spec.read_only
        return False

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
