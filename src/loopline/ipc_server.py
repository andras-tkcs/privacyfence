"""IPC server: asyncio Unix socket server that runs inside the daemon.

Handles three JSON-RPC-style methods (see ipc.py for protocol docs).
Connector.call() may block for an arbitrary duration (user approval), so each
request is dispatched as a separate asyncio Task — multiple in-flight requests
from the same bridge connection are fully concurrent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from .connector import Connector, ToolSpec
from .ipc import SOCKET_PATH, VERSION

logger = logging.getLogger(__name__)


class IPCServer:
    """Listens on SOCKET_PATH and dispatches connector calls."""

    def __init__(self, connectors: list[Connector]) -> None:
        self._connectors: dict[str, Connector] = {c.name: c for c in connectors}
        self._server: asyncio.AbstractServer | None = None

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
            self._handle_connection, path=SOCKET_PATH
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
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
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
            logger.error("IPC dispatch error for request %s: %s", req_id, exc)
            await self._send(writer, {"id": req_id, "error": str(exc)})

    async def _call_connector(self, params: dict) -> Any:
        connector_name = params["connector"]
        tool = params["tool"]
        args = params.get("args", {})
        connector = self._connectors.get(connector_name)
        if connector is None:
            raise ValueError(f"Unknown connector: {connector_name!r}")
        return await connector.call(tool, args)

    def _build_manifest(self) -> dict:
        return {
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
