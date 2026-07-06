"""Tests for IPCClient: the bridge-side half of the bridge<->daemon Unix
socket protocol described in privacyfence.ipc.

Each test runs a small hand-rolled asyncio Unix socket "fake daemon" that we
script directly (send/expect specific wire messages), rather than mocking
asyncio streams — this exercises IPCClient's real framing, request-id
multiplexing, and disconnect handling exactly as it runs against the real
daemon.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

from privacyfence.ipc_client import IPCClient, IPCError


@pytest.fixture
def short_socket_path():
    """See tests/unit/test_ipc_server.py for why this isn't tmp_path:
    AF_UNIX sun_path is too short (~104 bytes) for pytest's nested tmp dirs."""
    directory = f"/tmp/pf-{uuid.uuid4().hex[:8]}"
    os.makedirs(directory, exist_ok=True)
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


class FakeDaemon:
    """A scriptable fake daemon: records every request it receives and lets
    the test decide what (and when) to write back, so we can test ordering,
    malformed lines, and disconnects precisely."""

    def __init__(self):
        self.received: list[dict] = []
        self._writer: asyncio.StreamWriter | None = None
        self._server: asyncio.AbstractServer | None = None
        self._new_conn: asyncio.Event = asyncio.Event()

    async def start(self, socket_path: str) -> None:
        self._server = await asyncio.start_unix_server(self._handle, path=socket_path)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        self._new_conn.set()
        while True:
            line = await reader.readline()
            if not line:
                break
            self.received.append(json.loads(line))

    async def wait_for_connection(self) -> None:
        await asyncio.wait_for(self._new_conn.wait(), timeout=2.0)

    async def wait_for_n_requests(self, n: int, timeout: float = 2.0) -> None:
        async def _poll():
            while len(self.received) < n:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(_poll(), timeout=timeout)

    async def send_raw(self, raw: bytes) -> None:
        assert self._writer is not None
        self._writer.write(raw)
        await self._writer.drain()

    async def send_response(self, req_id: str, *, result=None, error=None) -> None:
        msg = {"id": req_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        await self.send_raw((json.dumps(msg) + "\n").encode())

    async def disconnect(self) -> None:
        assert self._writer is not None
        self._writer.close()

    async def stop(self) -> None:
        # Server.wait_closed() (3.12+) waits for the listening socket *and*
        # every accepted connection to drop. Close our side of any accepted
        # connection explicitly rather than relying on the peer having
        # already done so, or wait_closed() can hang.
        if self._writer:
            self._writer.close()
        if self._server:
            self._server.close()
            await self._server.wait_closed()


@pytest.fixture
async def daemon_and_client(short_socket_path):
    daemon = FakeDaemon()
    await daemon.start(short_socket_path)
    client = IPCClient(short_socket_path)
    await client.connect()
    await daemon.wait_for_connection()
    try:
        yield daemon, client
    finally:
        await client.close()
        await daemon.stop()


class TestRequestFraming:
    async def test_call_sends_newline_delimited_json_with_method_and_params(self, daemon_and_client):
        daemon, client = daemon_and_client
        call_task = asyncio.ensure_future(client.call("gmail", "gmail_get_message", {"message_id": "abc"}))
        await daemon.wait_for_n_requests(1)

        assert daemon.received[0]["method"] == "call"
        assert daemon.received[0]["params"] == {
            "connector": "gmail", "tool": "gmail_get_message", "args": {"message_id": "abc"},
        }
        assert "id" in daemon.received[0]

        await daemon.send_response(daemon.received[0]["id"], result={"ok": True})
        assert await call_task == {"ok": True}

    async def test_manifest_sends_method_with_empty_params(self, daemon_and_client):
        daemon, client = daemon_and_client
        task = asyncio.ensure_future(client.manifest())
        await daemon.wait_for_n_requests(1)
        assert daemon.received[0]["method"] == "manifest"
        assert daemon.received[0]["params"] == {}
        await daemon.send_response(daemon.received[0]["id"], result={"version": "0.4.10", "connectors": []})
        assert (await task)["version"] == "0.4.10"

    async def test_request_ids_are_unique_and_increment(self, daemon_and_client):
        daemon, client = daemon_and_client
        t1 = asyncio.ensure_future(client.call("gmail", "x", {}))
        await daemon.wait_for_n_requests(1)
        t2 = asyncio.ensure_future(client.call("gmail", "x", {}))
        await daemon.wait_for_n_requests(2)

        ids = [r["id"] for r in daemon.received]
        assert len(set(ids)) == 2

        for r in daemon.received:
            await daemon.send_response(r["id"], result="done")
        await t1
        await t2


class TestResponseRouting:
    async def test_error_response_raises_ipc_error_with_message(self, daemon_and_client):
        daemon, client = daemon_and_client
        task = asyncio.ensure_future(client.call("gmail", "x", {}))
        await daemon.wait_for_n_requests(1)
        await daemon.send_response(daemon.received[0]["id"], error="Unknown connector: 'gmail'")

        with pytest.raises(IPCError, match="Unknown connector"):
            await task

    async def test_out_of_order_responses_route_to_correct_caller(self, daemon_and_client):
        daemon, client = daemon_and_client
        t1 = asyncio.ensure_future(client.call("a", "x", {}))
        t2 = asyncio.ensure_future(client.call("b", "x", {}))
        await daemon.wait_for_n_requests(2)

        id1, id2 = daemon.received[0]["id"], daemon.received[1]["id"]
        # Respond in reverse order to prove routing is by id, not arrival order.
        await daemon.send_response(id2, result="second-result")
        await daemon.send_response(id1, result="first-result")

        assert await t1 == "first-result"
        assert await t2 == "second-result"

    async def test_malformed_response_line_is_ignored_not_fatal(self, daemon_and_client):
        daemon, client = daemon_and_client
        task = asyncio.ensure_future(client.call("gmail", "x", {}))
        await daemon.wait_for_n_requests(1)

        await daemon.send_raw(b"{not valid json\n")
        # The read loop must survive the bad line and still deliver the real response.
        await daemon.send_response(daemon.received[0]["id"], result="ok")
        assert await task == "ok"

    async def test_response_with_unknown_id_is_ignored(self, daemon_and_client):
        daemon, client = daemon_and_client
        task = asyncio.ensure_future(client.call("gmail", "x", {}))
        await daemon.wait_for_n_requests(1)

        await daemon.send_response("some-other-id-nobody-is-waiting-on", result="orphan")
        await daemon.send_response(daemon.received[0]["id"], result="ok")
        assert await task == "ok"


class TestLineLimit:
    """Regression coverage for the v0.4.10 fix on the client side."""

    async def test_response_larger_than_64kib_is_read_intact(self, daemon_and_client):
        daemon, client = daemon_and_client
        big_payload = "x" * (200 * 1024)
        task = asyncio.ensure_future(client.call("drive", "x", {}))
        await daemon.wait_for_n_requests(1)
        await daemon.send_response(daemon.received[0]["id"], result={"content": big_payload})
        assert (await task)["content"] == big_payload


class TestDisconnectHandling:
    async def test_daemon_disconnect_fails_all_pending_calls(self, daemon_and_client):
        daemon, client = daemon_and_client
        t1 = asyncio.ensure_future(client.call("a", "x", {}))
        t2 = asyncio.ensure_future(client.call("b", "x", {}))
        await daemon.wait_for_n_requests(2)

        await daemon.disconnect()

        with pytest.raises(IPCError, match="IPC connection closed"):
            await asyncio.wait_for(t1, timeout=2.0)
        with pytest.raises(IPCError, match="IPC connection closed"):
            await asyncio.wait_for(t2, timeout=2.0)

    async def test_close_cancels_reader_task(self, short_socket_path):
        daemon = FakeDaemon()
        await daemon.start(short_socket_path)
        client = IPCClient(short_socket_path)
        await client.connect()
        await daemon.wait_for_connection()

        reader_task = client._reader_task
        await client.close()
        await asyncio.sleep(0.01)
        assert reader_task.cancelled() or reader_task.done()
        await daemon.stop()
        await daemon.stop()
