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
from typing import Any

from .connector import Connector, ToolSpec
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

    def __init__(self, connectors: list[Connector]) -> None:
        self._connectors: dict[str, Connector] = {c.name: c for c in connectors}
        self._server: asyncio.AbstractServer | None = None
        self._inflight: dict[str, tuple[asyncio.Future, float]] = {}

    def set_connectors(self, connectors: list[Connector]) -> None:
        """Swap in a freshly built connector set (e.g. after the menu bar
        authenticates a service or toggles one on/off). Called from the
        rumps main thread; the dict reassignment is a single atomic
        reference swap so no lock is needed against the IPC asyncio thread.
        """
        self._connectors = {c.name: c for c in connectors}

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
                result = await self._call_connector(params)
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
