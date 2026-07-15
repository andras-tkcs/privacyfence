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
from privacyfence.audit_log import current_week, init_audit_logger
from privacyfence.auto_accept import init_auto_accept_evaluator
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
    def __init__(
        self, name: str, *, result=None, error: Exception | None = None, delay: float = 0.0,
        my_email: str = "",
    ):
        self._name = name
        self._result = result
        self._error = error
        self._delay = delay
        self.my_email = my_email
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


class TestDedupeRetries:
    """A gated call can block for a long time waiting on a native approval
    popup -- long enough that the calling MCP client's own tool-call timeout
    fires and it retries with an identical request. Without dedup, that
    retry runs the whole tool a second time and shows a second approval
    popup for what the user experiences as one action.
    """

    async def test_identical_concurrent_calls_share_one_connector_invocation(self, running_server):
        server, socket_path = running_server
        connector = FakeConnector("drive", result="written", delay=0.1)
        server.set_connectors([connector])
        client = await _RawClient.connect(socket_path)
        try:
            params = {"connector": "drive", "tool": "write_file_content", "args": {"file_id": "f1", "content": "hi"}}
            await client.send({"id": "1", "method": "call", "params": params})
            await client.send({"id": "2", "method": "call", "params": params})
            first = await client.recv()
            second = await client.recv()

            assert first["result"] == "written"
            assert second["result"] == "written"
            assert connector.calls == [("write_file_content", {"file_id": "f1", "content": "hi"})]
        finally:
            await client.close()

    async def test_identical_call_shortly_after_completion_reuses_cached_result(self, running_server):
        server, socket_path = running_server
        connector = FakeConnector("drive", result="written")
        server.set_connectors([connector])
        client = await _RawClient.connect(socket_path)
        try:
            params = {"connector": "drive", "tool": "write_file_content", "args": {"file_id": "f1"}}
            await client.send({"id": "1", "method": "call", "params": params})
            assert (await client.recv())["result"] == "written"

            # Simulates the client-timeout-then-retry scenario: a second,
            # identical request arrives after the first already finished.
            await client.send({"id": "2", "method": "call", "params": params})
            assert (await client.recv())["result"] == "written"

            assert len(connector.calls) == 1  # the retry did not re-run the write
        finally:
            await client.close()

    async def test_different_args_are_not_deduped(self, running_server):
        server, socket_path = running_server
        connector = FakeConnector("drive", result="ok")
        server.set_connectors([connector])
        client = await _RawClient.connect(socket_path)
        try:
            await client.send({
                "id": "1", "method": "call",
                "params": {"connector": "drive", "tool": "write_file_content", "args": {"file_id": "f1"}},
            })
            await client.recv()
            await client.send({
                "id": "2", "method": "call",
                "params": {"connector": "drive", "tool": "write_file_content", "args": {"file_id": "f2"}},
            })
            await client.recv()

            assert len(connector.calls) == 2
        finally:
            await client.close()

    async def test_error_from_original_call_propagates_to_deduped_retry(self, running_server):
        server, socket_path = running_server
        connector = FakeConnector("drive", error=ValueError("boom"), delay=0.1)
        server.set_connectors([connector])
        client = await _RawClient.connect(socket_path)
        try:
            params = {"connector": "drive", "tool": "write_file_content", "args": {"file_id": "f1"}}
            await client.send({"id": "1", "method": "call", "params": params})
            await client.send({"id": "2", "method": "call", "params": params})
            first = await client.recv()
            second = await client.recv()

            assert first["error"] == "boom"
            assert second["error"] == "boom"
            assert len(connector.calls) == 1
        finally:
            await client.close()

    async def test_dedupe_window_expires_after_ttl(self, running_server):
        server, socket_path = running_server
        server._DEDUPE_TTL_SECONDS = 0.05
        connector = FakeConnector("drive", result="ok")
        server.set_connectors([connector])
        client = await _RawClient.connect(socket_path)
        try:
            params = {"connector": "drive", "tool": "write_file_content", "args": {"file_id": "f1"}}
            await client.send({"id": "1", "method": "call", "params": params})
            await client.recv()

            await asyncio.sleep(0.1)

            await client.send({"id": "2", "method": "call", "params": params})
            await client.recv()

            assert len(connector.calls) == 2  # outside the dedupe window: a real retry
        finally:
            await client.close()

    async def test_exempt_tool_always_reruns_after_completion(self, running_server):
        """gmail_create_label: a second identical call is supposed to hit the
        "already exists" error the tool itself raises, not silently replay
        the first call's success -- so it must not be served from the
        completed-result cache, even well inside the dedupe window."""
        server, socket_path = running_server
        connector = FakeConnector("gmail", result="label-created")
        server.set_connectors([connector])
        client = await _RawClient.connect(socket_path)
        try:
            params = {"connector": "gmail", "tool": "gmail_create_label", "args": {"label_name": "QA"}}
            await client.send({"id": "1", "method": "call", "params": params})
            await client.recv()

            await client.send({"id": "2", "method": "call", "params": params})
            await client.recv()

            assert len(connector.calls) == 2  # not deduped, despite identical args
        finally:
            await client.close()

    async def test_exempt_tool_still_coalesces_concurrent_in_flight_calls(self, running_server):
        """The exemption only drops completed-result reuse -- two calls that
        are genuinely concurrent (nothing has taken effect yet for either)
        are still coalesced into one connector invocation."""
        server, socket_path = running_server
        connector = FakeConnector("gmail", result="label-created", delay=0.1)
        server.set_connectors([connector])
        client = await _RawClient.connect(socket_path)
        try:
            params = {"connector": "gmail", "tool": "gmail_create_label", "args": {"label_name": "QA"}}
            await client.send({"id": "1", "method": "call", "params": params})
            await client.send({"id": "2", "method": "call", "params": params})
            first = await client.recv()
            second = await client.recv()

            assert first["result"] == "label-created"
            assert second["result"] == "label-created"
            assert len(connector.calls) == 1
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


class TestCheckPolicyDispatch:
    """privacyfence_check_policy's daemon-side handler: must never reach a
    real connector call, never touch the network, and never open a popup --
    it only reports what would happen."""

    @pytest.fixture(autouse=True)
    def _audit_dir(self, tmp_path):
        init_audit_logger(str(tmp_path))
        self._audit_dir = tmp_path

    def _read_entries(self):
        week_file = self._audit_dir / f"{current_week()}.jsonl"
        if not week_file.exists():
            return []
        return [json.loads(line) for line in week_file.read_text(encoding="utf-8").splitlines()]

    async def test_auto_gated_tool_is_always_auto_accept(self, running_server):
        server, socket_path = running_server
        server.set_connectors([FakeConnector("gmail")])
        client = await _RawClient.connect(socket_path)
        try:
            await client.send({
                "id": "1", "method": "check_policy",
                "params": {"connector": "gmail", "tool": "gmail_list_messages", "args": {}},
            })
            resp = await client.recv()
            assert resp["result"] == {
                "gate": "auto", "verdict": "auto_accept", "matched_rule": None,
                "reason": "Unconditionally auto-accepted -- never reaches the review gate.",
                "pii_gate_may_apply": False,
            }
        finally:
            await client.close()

    async def test_never_calls_the_connector(self, running_server):
        server, socket_path = running_server
        connector = FakeConnector("gmail")
        server.set_connectors([connector])
        client = await _RawClient.connect(socket_path)
        try:
            await client.send({
                "id": "1", "method": "check_policy",
                "params": {"connector": "gmail", "tool": "gmail_get_message", "args": {}},
            })
            await client.recv()
            assert connector.calls == []
        finally:
            await client.close()

    async def test_popup_tool_matching_args_only_rule_is_auto_accept(self, running_server):
        init_auto_accept_evaluator({
            "gmail.create_draft": [{"rule": "to_is_myself"}],
        })
        server, socket_path = running_server
        server.set_connectors([FakeConnector("gmail", my_email="me@example.com")])
        client = await _RawClient.connect(socket_path)
        try:
            await client.send({
                "id": "1", "method": "check_policy",
                "params": {
                    "connector": "gmail", "tool": "gmail_create_draft",
                    "args": {"to": "me@example.com", "subject": "x", "body": "y"},
                },
            })
            resp = await client.recv()
            assert resp["result"]["gate"] == "popup"
            assert resp["result"]["verdict"] == "auto_accept"
            assert resp["result"]["matched_rule"] == "to_is_myself"
            assert resp["result"]["pii_gate_may_apply"] is False
        finally:
            await client.close()

    async def test_review_tool_with_data_dependent_rule_is_unknown_and_flags_pii_gate(self, running_server):
        init_auto_accept_evaluator({
            "gmail.read_message": [{"rule": "i_am_sender"}],
        })
        server, socket_path = running_server
        server.set_connectors([FakeConnector("gmail", my_email="me@example.com")])
        client = await _RawClient.connect(socket_path)
        try:
            await client.send({
                "id": "1", "method": "check_policy",
                "params": {"connector": "gmail", "tool": "gmail_get_message", "args": {"message_id": "m1"}},
            })
            resp = await client.recv()
            assert resp["result"]["gate"] == "review"
            assert resp["result"]["verdict"] == "unknown"
            assert resp["result"]["pii_gate_may_apply"] is True
        finally:
            await client.close()

    async def test_no_matching_rule_is_requires_review(self, running_server):
        init_auto_accept_evaluator({})
        server, socket_path = running_server
        server.set_connectors([FakeConnector("gmail")])
        client = await _RawClient.connect(socket_path)
        try:
            await client.send({
                "id": "1", "method": "check_policy",
                "params": {"connector": "gmail", "tool": "gmail_get_message", "args": {}},
            })
            resp = await client.recv()
            assert resp["result"]["verdict"] == "requires_review"
        finally:
            await client.close()

    async def test_unknown_tool_returns_error(self, running_server):
        server, socket_path = running_server
        server.set_connectors([FakeConnector("gmail")])
        client = await _RawClient.connect(socket_path)
        try:
            await client.send({
                "id": "1", "method": "check_policy",
                "params": {"connector": "gmail", "tool": "not_a_real_tool", "args": {}},
            })
            resp = await client.recv()
            assert "Unknown tool" in resp["error"]
        finally:
            await client.close()

    async def test_unknown_connector_returns_error(self, running_server):
        server, socket_path = running_server
        client = await _RawClient.connect(socket_path)
        try:
            await client.send({
                "id": "1", "method": "check_policy",
                "params": {"connector": "nope", "tool": "gmail_get_message", "args": {}},
            })
            resp = await client.recv()
            assert "Unknown connector" in resp["error"]
        finally:
            await client.close()

    async def test_records_a_policy_check_audit_entry_not_a_real_decision(self, running_server):
        server, socket_path = running_server
        server.set_connectors([FakeConnector("gmail")])
        client = await _RawClient.connect(socket_path)
        try:
            await client.send({
                "id": "1", "method": "check_policy",
                "params": {"connector": "gmail", "tool": "gmail_list_messages", "args": {}},
            })
            await client.recv()
        finally:
            await client.close()
        entries = self._read_entries()
        assert len(entries) == 1
        assert entries[0]["decision"] == "policy_check"
        assert entries[0]["connector"] == "gmail"
        assert entries[0]["tool"] == "gmail_list_messages"


class UnattendedAwareConnector(Connector):
    """Records gate.is_unattended() at the moment call() runs, so tests can
    confirm the daemon actually flips the flag for the right connection and
    the right duration -- not just that the IPC methods return the right
    JSON."""

    def __init__(self, name: str):
        self._name = name
        self.observed_unattended: list[bool] = []

    @property
    def name(self) -> str:
        return self._name

    def tool_specs(self) -> list[ToolSpec]:
        return [ToolSpec(name=f"{self._name}_tool", description="t", read_only=True)]

    async def call(self, tool: str, args: dict) -> object:
        from privacyfence.gate import is_unattended
        self.observed_unattended.append(is_unattended())
        return "ok"


class TestUnattendedSessionDispatch:
    """privacyfence_begin/end_unattended_session -- see
    docs/TECHNICAL_REFERENCE.md's "Scheduled / unattended Cowork tasks"
    section. Opt-in (unattended_sessions_enabled), connection-scoped, and
    cleared on disconnect.
    """

    @pytest.fixture(autouse=True)
    def _audit_dir(self, tmp_path):
        init_audit_logger(str(tmp_path))
        self._audit_dir = tmp_path

    def _read_entries(self):
        week_file = self._audit_dir / f"{current_week()}.jsonl"
        if not week_file.exists():
            return []
        return [json.loads(line) for line in week_file.read_text(encoding="utf-8").splitlines()]

    async def _server(self, socket_path, monkeypatch, *, enabled: bool):
        monkeypatch.setattr(ipc_server_module, "SOCKET_PATH", socket_path)
        server = IPCServer([], unattended_sessions_enabled=enabled)
        await server.start()
        return server

    async def test_begin_fails_when_not_enabled_by_config(self, short_socket_path, monkeypatch):
        server = await self._server(short_socket_path, monkeypatch, enabled=False)
        client = await _RawClient.connect(short_socket_path)
        try:
            await client.send({"id": "1", "method": "begin_unattended_session", "params": {}})
            resp = await client.recv()
            assert "disabled" in resp["error"]
            assert server.unattended_session_count() == 0
            assert self._read_entries() == []  # rejected before anything worth auditing happened
        finally:
            await client.close()
            await server.stop()

    async def test_begin_succeeds_when_enabled(self, short_socket_path, monkeypatch):
        server = await self._server(short_socket_path, monkeypatch, enabled=True)
        client = await _RawClient.connect(short_socket_path)
        try:
            await client.send({"id": "1", "method": "begin_unattended_session", "params": {}})
            resp = await client.recv()
            assert resp["result"] == {"unattended": True}
            assert server.unattended_session_count() == 1
        finally:
            await client.close()
            await server.stop()

    async def test_begin_and_end_leave_an_audit_trail(self, short_socket_path, monkeypatch):
        server = await self._server(short_socket_path, monkeypatch, enabled=True)
        client = await _RawClient.connect(short_socket_path)
        try:
            await client.send({"id": "1", "method": "begin_unattended_session", "params": {}})
            await client.recv()
            await client.send({"id": "2", "method": "end_unattended_session", "params": {}})
            await client.recv()
        finally:
            await client.close()
            await server.stop()

        entries = self._read_entries()
        assert [e["decision"] for e in entries] == [
            "unattended_session_started", "unattended_session_ended",
        ]

    async def test_disconnect_while_unattended_is_audited_as_ended(self, short_socket_path, monkeypatch):
        server = await self._server(short_socket_path, monkeypatch, enabled=True)
        client = await _RawClient.connect(short_socket_path)
        await client.send({"id": "1", "method": "begin_unattended_session", "params": {}})
        await client.recv()

        client.writer.close()
        await asyncio.sleep(0.05)

        try:
            entries = self._read_entries()
            assert [e["decision"] for e in entries] == [
                "unattended_session_started", "unattended_session_ended",
            ]
        finally:
            await server.stop()

    async def test_end_without_begin_does_not_audit_a_phantom_end(self, short_socket_path, monkeypatch):
        server = await self._server(short_socket_path, monkeypatch, enabled=True)
        client = await _RawClient.connect(short_socket_path)
        try:
            await client.send({"id": "1", "method": "end_unattended_session", "params": {}})
            await client.recv()
        finally:
            await client.close()
            await server.stop()

        assert self._read_entries() == []

    async def test_call_after_begin_runs_with_unattended_flag_set(self, short_socket_path, monkeypatch):
        server = await self._server(short_socket_path, monkeypatch, enabled=True)
        connector = UnattendedAwareConnector("gmail")
        server.set_connectors([connector])
        client = await _RawClient.connect(short_socket_path)
        try:
            await client.send({"id": "1", "method": "begin_unattended_session", "params": {}})
            await client.recv()

            await client.send({
                "id": "2", "method": "call",
                "params": {"connector": "gmail", "tool": "x", "args": {}},
            })
            await client.recv()

            assert connector.observed_unattended == [True]
        finally:
            await client.close()
            await server.stop()

    async def test_call_without_begin_is_not_unattended(self, short_socket_path, monkeypatch):
        server = await self._server(short_socket_path, monkeypatch, enabled=True)
        connector = UnattendedAwareConnector("gmail")
        server.set_connectors([connector])
        client = await _RawClient.connect(short_socket_path)
        try:
            await client.send({
                "id": "1", "method": "call",
                "params": {"connector": "gmail", "tool": "x", "args": {}},
            })
            await client.recv()

            assert connector.observed_unattended == [False]
        finally:
            await client.close()
            await server.stop()

    async def test_end_unattended_session_restores_normal_mode(self, short_socket_path, monkeypatch):
        server = await self._server(short_socket_path, monkeypatch, enabled=True)
        connector = UnattendedAwareConnector("gmail")
        server.set_connectors([connector])
        client = await _RawClient.connect(short_socket_path)
        try:
            await client.send({"id": "1", "method": "begin_unattended_session", "params": {}})
            await client.recv()
            await client.send({"id": "2", "method": "end_unattended_session", "params": {}})
            resp = await client.recv()
            assert resp["result"] == {"unattended": False}
            assert server.unattended_session_count() == 0

            await client.send({
                "id": "3", "method": "call",
                "params": {"connector": "gmail", "tool": "x", "args": {}},
            })
            await client.recv()
            assert connector.observed_unattended == [False]
        finally:
            await client.close()
            await server.stop()

    async def test_unattended_state_is_scoped_to_one_connection(self, short_socket_path, monkeypatch):
        server = await self._server(short_socket_path, monkeypatch, enabled=True)
        connector = UnattendedAwareConnector("gmail")
        server.set_connectors([connector])
        marked_client = await _RawClient.connect(short_socket_path)
        plain_client = await _RawClient.connect(short_socket_path)
        try:
            await marked_client.send({"id": "1", "method": "begin_unattended_session", "params": {}})
            await marked_client.recv()

            await plain_client.send({
                "id": "1", "method": "call",
                "params": {"connector": "gmail", "tool": "x", "args": {}},
            })
            await plain_client.recv()

            assert connector.observed_unattended == [False]
            assert server.unattended_session_count() == 1
        finally:
            await marked_client.close()
            await plain_client.close()
            await server.stop()

    async def test_disconnect_clears_unattended_state(self, short_socket_path, monkeypatch):
        server = await self._server(short_socket_path, monkeypatch, enabled=True)
        client = await _RawClient.connect(short_socket_path)
        await client.send({"id": "1", "method": "begin_unattended_session", "params": {}})
        await client.recv()
        assert server.unattended_session_count() == 1

        client.writer.close()
        await asyncio.sleep(0.05)  # give the server a beat to observe the disconnect

        try:
            assert server.unattended_session_count() == 0
        finally:
            await server.stop()

    async def test_end_unattended_session_is_a_no_op_when_never_begun(self, short_socket_path, monkeypatch):
        server = await self._server(short_socket_path, monkeypatch, enabled=True)
        client = await _RawClient.connect(short_socket_path)
        try:
            await client.send({"id": "1", "method": "end_unattended_session", "params": {}})
            resp = await client.recv()
            assert resp["result"] == {"unattended": False}
        finally:
            await client.close()
            await server.stop()


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
