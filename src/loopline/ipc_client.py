"""IPC client used by the bridge to talk to the daemon.

Maintains a single persistent async connection with request multiplexing:
multiple tool calls may be in flight simultaneously (Claude can call tools
concurrently). Each request gets a unique ID; the reader loop matches responses
back to their waiting futures.

Usage inside FastMCP's event loop:
    client = IPCClient(SOCKET_PATH)
    result = await client.call("gmail", "gmail_get_message", {"message_id": "…"})
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class IPCError(Exception):
    """Raised when the daemon returns an error response."""


class IPCClient:
    def __init__(self, socket_path: str) -> None:
        self._path = socket_path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def connect(self) -> None:
        """Open the connection. Must be called once inside an event loop."""
        self._reader, self._writer = await asyncio.open_unix_connection(self._path)
        self._reader_task = asyncio.create_task(self._read_loop())
        logger.debug("IPC client connected to %s", self._path)

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        if self._writer:
            self._writer.close()

    async def manifest(self) -> dict:
        return await self._request("manifest", {})

    async def call(self, connector: str, tool: str, args: dict[str, Any]) -> Any:
        return await self._request(
            "call", {"connector": connector, "tool": tool, "args": args}
        )

    async def confirm(self, request_id: str) -> Any:
        return await self._request("confirm", {"request_id": request_id})

    async def deny(self, request_id: str) -> Any:
        return await self._request("deny", {"request_id": request_id})

    async def show_details(self, request_id: str) -> Any:
        return await self._request("show_details", {"request_id": request_id})

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    async def _request(self, method: str, params: dict) -> Any:
        req_id = str(self._next_id)
        self._next_id += 1
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut

        msg = json.dumps({"id": req_id, "method": method, "params": params}) + "\n"
        async with self._write_lock:
            assert self._writer is not None
            self._writer.write(msg.encode())
            await self._writer.drain()

        return await fut

    async def _read_loop(self) -> None:
        try:
            while True:
                assert self._reader is not None
                line = await self._reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError as exc:
                    logger.warning("IPC: malformed response line: %s", exc)
                    continue
                req_id = msg.get("id")
                fut = self._pending.pop(req_id, None)
                if fut is None or fut.done():
                    continue
                if "error" in msg:
                    fut.set_exception(IPCError(msg["error"]))
                else:
                    fut.set_result(msg.get("result"))
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            # Fail all in-flight requests if the connection drops.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(IPCError("IPC connection closed"))
            self._pending.clear()
