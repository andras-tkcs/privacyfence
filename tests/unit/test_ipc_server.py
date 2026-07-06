"""Tests for IPCServer: the daemon-side half of the bridge<->daemon Unix
socket protocol described in privacyfence.ipc.

These use a real asyncio Unix domain socket (via a tmp_path SOCKET_PATH,
monkeypatched into the ipc_server module) with a small raw client helper that
speaks the wire protocol directly, rather than mocking asyncio streams — the
framing (newline-delimited JSON, the 8 MiB line limit) and the connection
lifecycle are exactly what's under test.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

from privacyfence import ipc_server as ipc_server_module
from privacyfence.connector import Connector, ToolSpec
from privacyfence.ipc import LINE_LIMIT
from privacyfence.ipc_server import IPCServer


@pytest.fixture
def short_socket_path():
    """A Unix domain socket path short enough to fit sun_path (~104 bytes on
    macOS) — pytest's tmp_path is nested too deep for that, so we use /tmp
    directly with a short unique subdir and clean up manually. The subdir
    doesn't exist yet, matching production (start() must create it)."""
    directory = f"/tmp/pf-{uuid.uuid4().hex[:8]}"
    path = f"{directory}/s.sock"
    yield path
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    try:
        os.rmdir(directory)
    except OSError:
        pass


class FakeConnector(Connector):
    def __init__(self, name: str, *, result=None, error: Exception | None = None, delay: float = 0.0):
        self._name = name
        self._result = result
        self._error = error
        self._delay = delay
        self.calls: list[tuple[str, dict]] = []

    @property
    def name(self) -> str:
        return self._name

    def tool_specs(self) -> list[ToolSpec]:
        return [ToolSpec(name=f"{self._name}_tool", description="test tool", read_only=True)]

    async def call(self, tool: str, args: dict) -> object:
        self.calls.append((tool, args))
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        return self._result


@pytest.fixture
async def running_server(short_socket_path, monkeypatch):
    """Starts a real IPCServer on a tmp socket path and yields (server, socket_path)."""
    socket_path = short_socket_path
    monkeypatch.setattr(ipc_server_module, "SOCKET_PATH", socket_path)
    server = IPCServer([])
    await server.start()
    try:
        yield server, socket_path
    finally:
        await server.stop()


class _RawClient:
    """Minimal hand-rolled client that speaks the wire protocol directly,
    independent of IPCClient, so server tests don't depend on client code."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(cls, socket_path: str) -> "_RawClient":
        # Match the daemon/bridge's own raised limit — a bare
        # open_unix_connection() defaults to 64 KiB, which is exactly the
        # limit the v0.4.10 fix raised on both real ends.
        reader, writer = await asyncio.open_unix_connection(socket_path, limit=LINE_LIMIT)
        return cls(reader, writer)

    async def send(self, msg: dict) -> None:
        self.writer.write((json.dumps(msg) + "\n").encode())
        await self.writer.drain()

    async def send_raw(self, raw: bytes) -> None:
        self.writer.write(raw)
        await self.writer.drain()

    async def recv(self) -> dict:
        line = await self.reader.readline()
        return json.loads(line)

    async def close(self) -> None:
        self.writer.close()


class TestLifecycle:
    async def test_start_creates_socket_file(self, short_socket_path, monkeypatch):
        monkeypatch.setattr(ipc_server_module, "SOCKET_PATH", short_socket_path)
        server = IPCServer([])
        await server.start()
        try:
            assert os.path.exists(short_socket_path)
        finally:
            await server.stop()

    async def test_stop_removes_socket_file(self, short_socket_path, monkeypatch):
        monkeypatch.setattr(ipc_server_module, "SOCKET_PATH", short_socket_path)
        server = IPCServer([])
        await server.start()
        await server.stop()
        assert not os.path.exists(short_socket_path)

    async def test_start_removes_stale_socket_file_from_prior_run(self, short_socket_path, monkeypatch):
        os.makedirs(os.path.dirname(short_socket_path), exist_ok=True)
        with open(short_socket_path, "w") as f:
            f.write("stale")
        monkeypatch.setattr(ipc_server_module, "SOCKET_PATH", short_socket_path)
        server = IPCServer([])
        await server.start()
        await server.stop()


class TestHealthAndManifest:
    async def test_health_reports_version_and_connector_names(self, running_server):
        server, socket_path = running_server
        server.set_connectors([FakeConnector("gmail"), FakeConnector("drive")])
        client = await _RawClient.connect(socket_path)
        try:
            await client.send({"id": "1", "method": "health", "params": {}})
            resp = await client.recv()
            assert resp["id"] == "1"
            assert set(resp["result"]["connectors"]) == {"gmail", "drive"}
            assert "version" in resp["result"]
        finally:
            await client.close()

    async def test_manifest_reports_tool_specs_per_connector(self, running_server):
        server, socket_path = running_server
        server.set_connectors([FakeConnector("slack")])
        client = await _RawClient.connect(socket_path)
        try:
            await client.send({"id": "1", "method": "manifest", "params": {}})
            resp = await client.recv()
            connectors = resp["result"]["connectors"]
            assert len(connectors) == 1
            assert connectors[0]["name"] == "slack"
            assert connectors[0]["tools"][0]["name"] == "slack_tool"
        finally:
            await client.close()

    async def test_set_connectors_swap_is_reflected_immediately(self, running_server):
        server, socket_path = running_server
        server.set_connectors([FakeConnector("gmail")])
        server.set_connectors([FakeConnector("jira")])
        client = await _RawClient.connect(socket_path)
        try:
            await client.send({"id": "1", "method": "health", "params": {}})
            resp = await client.recv()
            assert resp["result"]["connectors"] == ["jira"]
        finally:
            await client.close()


class TestCallDispatch:
    async def test_call_routes_to_matching_connector_and_returns_result(self, running_server):
        server, socket_path = running_server
        connector = FakeConnector("gmail", result={"ok": True})
        server.set_connectors([connector])
        client = await _RawClient.connect(socket_path)
        try:
            await client.send({
                "id": "1", "method": "call",
                "params": {"connector": "gmail", "tool": "gmail_get_message", "args": {"message_id": "abc"}},
            })
            resp = await client.recv()
            assert resp["result"] == {"ok": True}
            assert connector.calls == [("gmail_get_message", {"message_id": "abc"})]
        finally:
            await client.close()

    async def test_call_unknown_connector_returns_error_with_matching_id(self, running_server):
        server, socket_path = running_server
        server.set_connectors([FakeConnector("gmail")])
        client = await _RawClient.connect(socket_path)
        try:
            await client.send({
                "id": "42", "method": "call",
                "params": {"connector": "nope", "tool": "x", "args": {}},
            })
            resp = await client.recv()
            assert resp["id"] == "42"
            assert "Unknown connector" in resp["error"]
        finally:
            await client.close()

    async def test_call_connector_raises_becomes_error_response(self, running_server):
        server, socket_path = running_server
        server.set_connectors([FakeConnector("gmail", error=ValueError("boom"))])
        client = await _RawClient.connect(socket_path)
        try:
            await client.send({
                "id": "1", "method": "call",
                "params": {"connector": "gmail", "tool": "x", "args": {}},
            })
            resp = await client.recv()
            assert resp["error"] == "boom"
        finally:
            await client.close()

    async def test_unknown_method_returns_error(self, running_server):
        server, socket_path = running_server
        client = await _RawClient.connect(socket_path)
        try:
            await client.send({"id": "1", "method": "bogus", "params": {}})
            resp = await client.recv()
            assert "Unknown method" in resp["error"]
        finally:
            await client.close()

    async def test_malformed_json_line_returns_error_without_crashing_connection(self, running_server):
        server, socket_path = running_server
        server.set_connectors([FakeConnector("gmail", result="fine")])
        client = await _RawClient.connect(socket_path)
        try:
            await client.send_raw(b"{not valid json\n")
            resp = await client.recv()
            assert resp["id"] is None
            assert "error" in resp

            # Connection must still be usable for subsequent well-formed requests.
            await client.send({
                "id": "2", "method": "call",
                "params": {"connector": "gmail", "tool": "x", "args": {}},
            })
            resp2 = await client.recv()
            assert resp2["result"] == "fine"
        finally:
            await client.close()


class TestConcurrency:
    async def test_slow_call_does_not_block_a_later_fast_call(self, running_server):
        server, socket_path = running_server
        slow = FakeConnector("slow", result="slow-done", delay=0.2)
        fast = FakeConnector("fast", result="fast-done")
        server.set_connectors([slow, fast])
        client = await _RawClient.connect(socket_path)
        try:
            await client.send({
                "id": "1", "method": "call",
                "params": {"connector": "slow", "tool": "x", "args": {}},
            })
            await client.send({
                "id": "2", "method": "call",
                "params": {"connector": "fast", "tool": "x", "args": {}},
            })
            first = await client.recv()
            second = await client.recv()
            # The fast call was dispatched second but must complete first.
            assert first["id"] == "2"
            assert first["result"] == "fast-done"
            assert second["id"] == "1"
            assert second["result"] == "slow-done"
        finally:
            await client.close()


class TestLineLimit:
    """Regression coverage for the v0.4.10 fix: asyncio's default
    StreamReader.readline() limit is 64 KiB; a Drive file response near/over
    that used to raise an uncaught ValueError and silently kill the reader
    task. The server must accept lines well past 64 KiB now."""

    async def test_response_larger_than_64kib_is_delivered_intact(self, running_server):
        server, socket_path = running_server
        big_payload = "x" * (200 * 1024)  # 200 KiB, well over the old 64 KiB default limit
        server.set_connectors([FakeConnector("drive", result={"content": big_payload})])
        client = await _RawClient.connect(socket_path)
        try:
            await client.send({
                "id": "1", "method": "call",
                "params": {"connector": "drive", "tool": "x", "args": {}},
            })
            resp = await client.recv()
            assert resp["result"]["content"] == big_payload
        finally:
            await client.close()

    async def test_request_larger_than_64kib_is_accepted(self, running_server):
        server, socket_path = running_server
        big_arg = "y" * (200 * 1024)
        connector = FakeConnector("drive", result="ok")
        server.set_connectors([connector])
        client = await _RawClient.connect(socket_path)
        try:
            await client.send({
                "id": "1", "method": "call",
                "params": {"connector": "drive", "tool": "x", "args": {"content": big_arg}},
            })
            resp = await client.recv()
            assert resp["result"] == "ok"
            assert connector.calls[0][1]["content"] == big_arg
        finally:
            await client.close()


class TestConnectionHandling:
    async def test_abrupt_disconnect_does_not_crash_server_for_other_clients(self, running_server):
        server, socket_path = running_server
        server.set_connectors([FakeConnector("gmail", result="ok")])

        dying_client = await _RawClient.connect(socket_path)
        await dying_client.send({"id": "1", "method": "health", "params": {}})
        await dying_client.recv()
        dying_client.writer.close()
        # No wait_closed(); simulate an abrupt drop and give the server a
        # beat to observe it before asserting it's still healthy.
        await asyncio.sleep(0.05)

        healthy_client = await _RawClient.connect(socket_path)
        try:
            await healthy_client.send({
                "id": "1", "method": "call",
                "params": {"connector": "gmail", "tool": "x", "args": {}},
            })
            resp = await healthy_client.recv()
            assert resp["result"] == "ok"
        finally:
            await healthy_client.close()
