"""Tests for bridge_main.py: the ephemeral stdio MCP server spawned by
Claude. Covers daemon auto-start/connection logic (real Unix sockets, same
short-path pattern as test_ipc_server.py -- pytest's tmp_path is too long
for AF_UNIX's sun_path), the version-mismatch guard, and dynamic tool
registration against a real FastMCP instance (verifying the deliberate
"always advertise read-only to the MCP client" override described in
_register_tools's docstring -- the real gate lives in the daemon, not here).
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import socket
import threading
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import FastMCP

from privacyfence import bridge_main as bridge_main_module
from privacyfence.connector import ToolParam, ToolSpec
from privacyfence.ipc_client import IPCError


@pytest.fixture
def short_socket_path():
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


# ---------------------------------------------------------------------------- #
# _find_daemon_cmd
# ---------------------------------------------------------------------------- #

class TestFindDaemonCmd:
    def test_prefers_sibling_binary_next_to_bridge(self, tmp_path, monkeypatch):
        sibling = tmp_path / "privacyfence-app"
        sibling.write_text("#!/bin/sh\n")
        monkeypatch.setattr(bridge_main_module.sys, "argv", [str(tmp_path / "privacyfence-bridge")])

        cmd = bridge_main_module._find_daemon_cmd()
        assert cmd == [str(sibling)]

    def test_falls_back_to_path_lookup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bridge_main_module.sys, "argv", [str(tmp_path / "privacyfence-bridge")])
        monkeypatch.setattr(bridge_main_module.shutil, "which", lambda name: "/usr/local/bin/privacyfence-app")
        cmd = bridge_main_module._find_daemon_cmd()
        assert cmd == ["/usr/local/bin/privacyfence-app"]

    def test_falls_back_to_module_invocation_when_nothing_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bridge_main_module.sys, "argv", [str(tmp_path / "privacyfence-bridge")])
        monkeypatch.setattr(bridge_main_module.shutil, "which", lambda name: None)

        # No sibling binary exists under tmp_path (real filesystem check, true
        # by construction). Force the one hardcoded /Applications path to
        # look absent too, without disturbing any other Path.exists() check.
        real_exists = Path.exists
        def fake_exists(self):
            if "PrivacyFenceApp.app" in str(self):
                return False
            return real_exists(self)
        monkeypatch.setattr(Path, "exists", fake_exists)

        cmd = bridge_main_module._find_daemon_cmd()
        assert cmd == [bridge_main_module.sys.executable, "-m", "privacyfence.daemon_main"]


# ---------------------------------------------------------------------------- #
# _socket_connectable
# ---------------------------------------------------------------------------- #

class TestSocketConnectable:
    def test_false_when_no_socket_file_exists(self, short_socket_path, monkeypatch):
        monkeypatch.setattr(bridge_main_module, "SOCKET_PATH", short_socket_path)
        assert bridge_main_module._socket_connectable() is False

    def test_false_when_nothing_listening(self, short_socket_path, monkeypatch):
        # Create the socket *file* without a listener behind it.
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(short_socket_path)
        monkeypatch.setattr(bridge_main_module, "SOCKET_PATH", short_socket_path)
        assert bridge_main_module._socket_connectable() is False
        s.close()

    def test_true_when_a_real_listener_is_present(self, short_socket_path, monkeypatch):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(short_socket_path)
        server.listen(1)
        monkeypatch.setattr(bridge_main_module, "SOCKET_PATH", short_socket_path)
        try:
            assert bridge_main_module._socket_connectable() is True
        finally:
            server.close()


# ---------------------------------------------------------------------------- #
# _ensure_daemon_running
# ---------------------------------------------------------------------------- #

class TestEnsureDaemonRunning:
    def test_returns_immediately_when_already_connectable(self, monkeypatch):
        monkeypatch.setattr(bridge_main_module, "_socket_connectable", lambda: True)
        popen_called = []
        monkeypatch.setattr(bridge_main_module.subprocess, "Popen", lambda *a, **kw: popen_called.append(1))

        bridge_main_module._ensure_daemon_running()

        assert popen_called == []

    def test_launches_daemon_and_waits_until_connectable(self, monkeypatch):
        states = [False, False, True]
        monkeypatch.setattr(bridge_main_module, "_socket_connectable", lambda: states.pop(0))
        monkeypatch.setattr(bridge_main_module, "_find_daemon_cmd", lambda: ["fake-daemon"])
        popen_called = []
        monkeypatch.setattr(bridge_main_module.subprocess, "Popen", lambda *a, **kw: popen_called.append(a))
        monkeypatch.setattr(bridge_main_module.time, "sleep", lambda s: None)

        bridge_main_module._ensure_daemon_running()

        assert popen_called == [(["fake-daemon"],)]

    def test_exits_with_error_after_timeout(self, monkeypatch, capsys):
        monkeypatch.setattr(bridge_main_module, "_socket_connectable", lambda: False)
        monkeypatch.setattr(bridge_main_module, "_find_daemon_cmd", lambda: ["fake-daemon"])
        monkeypatch.setattr(bridge_main_module.subprocess, "Popen", lambda *a, **kw: None)
        monkeypatch.setattr(bridge_main_module.time, "sleep", lambda s: None)
        # Make the deadline already elapsed so the polling loop doesn't spin.
        times = iter([0, 100, 100, 100])
        monkeypatch.setattr(bridge_main_module.time, "monotonic", lambda: next(times))

        with pytest.raises(SystemExit) as exc_info:
            bridge_main_module._ensure_daemon_running()
        assert exc_info.value.code == 1
        assert "did not start" in capsys.readouterr().err


# ---------------------------------------------------------------------------- #
# _fetch_manifest_sync: real Unix socket, blocking client
# ---------------------------------------------------------------------------- #

def _serve_one_manifest_request(socket_path: str, response: dict) -> tuple[threading.Thread, threading.Event]:
    ready = threading.Event()

    def serve():
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(socket_path)
        srv.listen(1)
        srv.settimeout(5)
        # listen() has already queued the backlog, so signaling readiness
        # here (rather than merely polling for the socket *file* to exist,
        # which can race bind() vs. listen()) means a connect() right after
        # this event fires is guaranteed to succeed rather than racing.
        ready.set()
        conn, _ = srv.accept()
        conn.recv(65536)
        conn.sendall((json.dumps({"id": "m0", "result": response}) + "\n").encode())
        conn.close()
        srv.close()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread, ready


class TestFetchManifestSync:
    def test_fetches_and_parses_result(self, short_socket_path, monkeypatch):
        monkeypatch.setattr(bridge_main_module, "SOCKET_PATH", short_socket_path)
        thread, ready = _serve_one_manifest_request(short_socket_path, {"version": "0.4.11", "connectors": []})

        assert ready.wait(timeout=5), "server never reached listen()"

        manifest = bridge_main_module._fetch_manifest_sync()
        assert manifest == {"version": "0.4.11", "connectors": []}
        thread.join(timeout=2)


# ---------------------------------------------------------------------------- #
# _check_version_match
# ---------------------------------------------------------------------------- #

class TestCheckVersionMatch:
    def test_matching_version_does_not_exit(self):
        bridge_main_module._check_version_match({"version": bridge_main_module.VERSION})

    def test_missing_version_key_does_not_exit(self):
        bridge_main_module._check_version_match({})

    def test_mismatched_version_exits_with_error(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            bridge_main_module._check_version_match({"version": "0.0.1-stale"})
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "version mismatch" in err
        assert bridge_main_module.VERSION in err
        assert "0.0.1-stale" in err


# ---------------------------------------------------------------------------- #
# _build_tool_fn
# ---------------------------------------------------------------------------- #

class TestBuildToolFn:
    def test_forwards_call_to_ipc_client(self, monkeypatch):
        fake_ipc = MagicMock()
        fake_ipc.call = AsyncMock(return_value={"ok": True})
        monkeypatch.setattr(bridge_main_module, "_ipc", fake_ipc)

        spec = ToolSpec(name="gmail_get_message", description="desc", params=[ToolParam("message_id", "str")])
        fn = bridge_main_module._build_tool_fn("gmail", spec)

        result = asyncio.run(fn(message_id="m1"))

        assert result == {"ok": True}
        fake_ipc.call.assert_awaited_once_with("gmail", "gmail_get_message", {"message_id": "m1"})

    def test_ipc_error_becomes_tool_error(self, monkeypatch):
        from fastmcp.exceptions import ToolError
        fake_ipc = MagicMock()
        fake_ipc.call = AsyncMock(side_effect=IPCError("daemon says no"))
        monkeypatch.setattr(bridge_main_module, "_ipc", fake_ipc)

        spec = ToolSpec(name="x", description="d", params=[])
        fn = bridge_main_module._build_tool_fn("gmail", spec)

        with pytest.raises(ToolError, match="daemon says no"):
            asyncio.run(fn())

    def test_uninitialized_ipc_client_raises_tool_error(self, monkeypatch):
        from fastmcp.exceptions import ToolError
        monkeypatch.setattr(bridge_main_module, "_ipc", None)

        spec = ToolSpec(name="x", description="d", params=[])
        fn = bridge_main_module._build_tool_fn("gmail", spec)

        with pytest.raises(ToolError, match="not initialized"):
            asyncio.run(fn())

    def test_required_param_has_no_default(self):
        spec = ToolSpec(name="x", description="d", params=[ToolParam("query", "str")])
        fn = bridge_main_module._build_tool_fn("gmail", spec)
        sig = fn.__signature__
        assert sig.parameters["query"].default is inspect.Parameter.empty

    def test_optional_param_carries_default(self):
        spec = ToolSpec(name="x", description="d", params=[
            ToolParam("max_results", "int", required=False, default=10),
        ])
        fn = bridge_main_module._build_tool_fn("gmail", spec)
        assert fn.__signature__.parameters["max_results"].default == 10

    def test_annotation_map_falls_back_to_str_for_unknown_types(self):
        spec = ToolSpec(name="x", description="d", params=[ToolParam("weird", "bytes")])
        fn = bridge_main_module._build_tool_fn("gmail", spec)
        assert fn.__annotations__["weird"] is str

    def test_name_and_docstring_set_from_spec(self):
        spec = ToolSpec(name="gmail_list_messages", description="Search Gmail", params=[])
        fn = bridge_main_module._build_tool_fn("gmail", spec)
        assert fn.__name__ == "gmail_list_messages"
        assert fn.__doc__ == "Search Gmail"


# ---------------------------------------------------------------------------- #
# _register_tools: real FastMCP instance
# ---------------------------------------------------------------------------- #

class TestRegisterTools:
    async def test_registers_every_tool_from_every_connector(self, monkeypatch):
        monkeypatch.setattr(bridge_main_module, "_ipc", MagicMock())
        mcp = FastMCP(name="test")
        manifest = {
            "connectors": [
                {"name": "gmail", "tools": [
                    ToolSpec(name="gmail_a", description="a", read_only=True).to_dict(),
                    ToolSpec(name="gmail_b", description="b", read_only=False).to_dict(),
                ]},
                {"name": "drive", "tools": [
                    ToolSpec(name="drive_a", description="c", read_only=False).to_dict(),
                ]},
            ]
        }

        bridge_main_module._register_tools(mcp, manifest)

        for name in ("gmail_a", "gmail_b", "drive_a"):
            assert await mcp.get_tool(name) is not None

    async def test_every_tool_advertised_read_only_regardless_of_true_gating(self, monkeypatch):
        # The real gate is enforced daemon-side; the client-facing annotation
        # is deliberately always read-only/non-destructive (see the long
        # comment in _register_tools) so Cowork doesn't double-prompt.
        monkeypatch.setattr(bridge_main_module, "_ipc", MagicMock())
        mcp = FastMCP(name="test")
        manifest = {
            "connectors": [{"name": "drive", "tools": [
                ToolSpec(name="drive_delete_everything", description="dangerous", read_only=False).to_dict(),
            ]}]
        }

        bridge_main_module._register_tools(mcp, manifest)

        tool = await mcp.get_tool("drive_delete_everything")
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.idempotentHint is True

    async def test_empty_manifest_registers_nothing(self):
        mcp = FastMCP(name="test")
        bridge_main_module._register_tools(mcp, {"connectors": []})
        # No assertion needed beyond "doesn't raise"; nothing to look up.


# ---------------------------------------------------------------------------- #
# _check_policy_handler + _register_meta_tools: privacyfence_check_policy
# ---------------------------------------------------------------------------- #

class TestCheckPolicyHandler:
    def test_forwards_to_ipc_client(self, monkeypatch):
        fake_ipc = MagicMock()
        fake_ipc.check_policy = AsyncMock(return_value={"verdict": "auto_accept"})
        monkeypatch.setattr(bridge_main_module, "_ipc", fake_ipc)

        result = asyncio.run(
            bridge_main_module._check_policy_handler("gmail", "gmail_get_message", {"message_id": "m1"})
        )

        assert result == {"verdict": "auto_accept"}
        fake_ipc.check_policy.assert_awaited_once_with("gmail", "gmail_get_message", {"message_id": "m1"})

    def test_missing_args_defaults_to_empty_dict(self, monkeypatch):
        fake_ipc = MagicMock()
        fake_ipc.check_policy = AsyncMock(return_value={"verdict": "auto_accept"})
        monkeypatch.setattr(bridge_main_module, "_ipc", fake_ipc)

        asyncio.run(bridge_main_module._check_policy_handler("gmail", "gmail_list_messages"))

        fake_ipc.check_policy.assert_awaited_once_with("gmail", "gmail_list_messages", {})

    def test_ipc_error_becomes_tool_error(self, monkeypatch):
        from fastmcp.exceptions import ToolError
        fake_ipc = MagicMock()
        fake_ipc.check_policy = AsyncMock(side_effect=IPCError("daemon says no"))
        monkeypatch.setattr(bridge_main_module, "_ipc", fake_ipc)

        with pytest.raises(ToolError, match="daemon says no"):
            asyncio.run(bridge_main_module._check_policy_handler("gmail", "gmail_get_message", {}))

    def test_uninitialized_ipc_client_raises_tool_error(self, monkeypatch):
        from fastmcp.exceptions import ToolError
        monkeypatch.setattr(bridge_main_module, "_ipc", None)

        with pytest.raises(ToolError, match="not initialized"):
            asyncio.run(bridge_main_module._check_policy_handler("gmail", "gmail_get_message", {}))


class TestBeginEndUnattendedSessionHandlers:
    def test_begin_forwards_to_ipc_client(self, monkeypatch):
        fake_ipc = MagicMock()
        fake_ipc.begin_unattended_session = AsyncMock(return_value={"unattended": True})
        monkeypatch.setattr(bridge_main_module, "_ipc", fake_ipc)

        result = asyncio.run(bridge_main_module._begin_unattended_session_handler())

        assert result == {"unattended": True}
        fake_ipc.begin_unattended_session.assert_awaited_once_with()

    def test_begin_ipc_error_becomes_tool_error(self, monkeypatch):
        from fastmcp.exceptions import ToolError
        fake_ipc = MagicMock()
        fake_ipc.begin_unattended_session = AsyncMock(side_effect=IPCError("unattended sessions disabled"))
        monkeypatch.setattr(bridge_main_module, "_ipc", fake_ipc)

        with pytest.raises(ToolError, match="disabled"):
            asyncio.run(bridge_main_module._begin_unattended_session_handler())

    def test_begin_uninitialized_ipc_client_raises_tool_error(self, monkeypatch):
        from fastmcp.exceptions import ToolError
        monkeypatch.setattr(bridge_main_module, "_ipc", None)

        with pytest.raises(ToolError, match="not initialized"):
            asyncio.run(bridge_main_module._begin_unattended_session_handler())

    def test_end_forwards_to_ipc_client(self, monkeypatch):
        fake_ipc = MagicMock()
        fake_ipc.end_unattended_session = AsyncMock(return_value={"unattended": False})
        monkeypatch.setattr(bridge_main_module, "_ipc", fake_ipc)

        result = asyncio.run(bridge_main_module._end_unattended_session_handler())

        assert result == {"unattended": False}
        fake_ipc.end_unattended_session.assert_awaited_once_with()

    def test_end_uninitialized_ipc_client_raises_tool_error(self, monkeypatch):
        from fastmcp.exceptions import ToolError
        monkeypatch.setattr(bridge_main_module, "_ipc", None)

        with pytest.raises(ToolError, match="not initialized"):
            asyncio.run(bridge_main_module._end_unattended_session_handler())


class TestRegisterMetaTools:
    async def test_registers_privacyfence_check_policy(self):
        mcp = FastMCP(name="test")
        bridge_main_module._register_meta_tools(mcp)
        tool = await mcp.get_tool("privacyfence_check_policy")
        assert tool is not None

    async def test_registers_begin_and_end_unattended_session(self):
        mcp = FastMCP(name="test")
        bridge_main_module._register_meta_tools(mcp)
        assert await mcp.get_tool("privacyfence_begin_unattended_session") is not None
        assert await mcp.get_tool("privacyfence_end_unattended_session") is not None

    async def test_advertised_read_only_with_no_side_effects(self):
        mcp = FastMCP(name="test")
        bridge_main_module._register_meta_tools(mcp)
        tool = await mcp.get_tool("privacyfence_check_policy")
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.idempotentHint is True

    async def test_does_not_touch_the_connector_manifest(self):
        # Meta-tools aren't sourced from a connector manifest -- registering
        # them must not require or consult one.
        mcp = FastMCP(name="test")
        bridge_main_module._register_meta_tools(mcp)  # no manifest argument at all
        assert await mcp.get_tool("privacyfence_check_policy") is not None


# ---------------------------------------------------------------------------- #
# _run_bridge: IPC lifespan around mcp.run_async
# ---------------------------------------------------------------------------- #

class TestRunBridge:
    async def test_connects_ipc_runs_mcp_then_closes_ipc_even_on_error(self, monkeypatch):
        fake_ipc = MagicMock()
        fake_ipc.connect = AsyncMock()
        fake_ipc.close = AsyncMock()
        monkeypatch.setattr(bridge_main_module, "IPCClient", lambda path: fake_ipc)

        mcp = MagicMock()
        mcp.run_async = AsyncMock(side_effect=RuntimeError("stdio closed"))

        with pytest.raises(RuntimeError, match="stdio closed"):
            await bridge_main_module._run_bridge(mcp)

        fake_ipc.connect.assert_awaited_once()
        fake_ipc.close.assert_awaited_once()

    async def test_happy_path_closes_ipc_after_run(self, monkeypatch):
        fake_ipc = MagicMock()
        fake_ipc.connect = AsyncMock()
        fake_ipc.close = AsyncMock()
        monkeypatch.setattr(bridge_main_module, "IPCClient", lambda path: fake_ipc)

        mcp = MagicMock()
        mcp.run_async = AsyncMock(return_value=None)

        await bridge_main_module._run_bridge(mcp)

        fake_ipc.close.assert_awaited_once()


# ---------------------------------------------------------------------------- #
# parse_args
# ---------------------------------------------------------------------------- #

class TestParseArgs:
    def test_default_config_path(self):
        args = bridge_main_module.parse_args([])
        assert args.config == "config/settings.yaml"

    def test_config_flag_overrides_default(self):
        args = bridge_main_module.parse_args(["--config", "/tmp/x.yaml"])
        assert args.config == "/tmp/x.yaml"


# ---------------------------------------------------------------------------- #
# main: full orchestration, everything else mocked
# ---------------------------------------------------------------------------- #

class TestMain:
    def test_orchestrates_startup_sequence_in_order(self, monkeypatch):
        calls = []
        monkeypatch.setattr(bridge_main_module, "_setup_logging", lambda: calls.append("setup_logging"))
        monkeypatch.setattr(bridge_main_module, "_ensure_daemon_running", lambda: calls.append("ensure_daemon"))
        monkeypatch.setattr(
            bridge_main_module, "_fetch_manifest_sync",
            lambda: calls.append("fetch_manifest") or {"version": bridge_main_module.VERSION, "connectors": []},
        )
        monkeypatch.setattr(bridge_main_module, "_check_version_match", lambda m: calls.append("check_version"))
        monkeypatch.setattr(bridge_main_module, "_register_tools", lambda mcp, m: calls.append("register_tools"))
        # _run_bridge(mcp) is evaluated eagerly as asyncio.run's argument, so
        # it must be replaced with a plain (non-async) stand-in too -- else
        # the mocked asyncio.run below never awaits/closes the real
        # coroutine object, which triggers a "never awaited" warning.
        monkeypatch.setattr(bridge_main_module, "_run_bridge", lambda mcp: "run-bridge-sentinel")
        monkeypatch.setattr(bridge_main_module.asyncio, "run", lambda coro: calls.append("run_bridge"))

        result = bridge_main_module.main([])

        assert result == 0
        assert calls == [
            "setup_logging", "ensure_daemon", "fetch_manifest", "check_version", "register_tools", "run_bridge",
        ]

    def test_keyboard_interrupt_during_run_is_swallowed(self, monkeypatch):
        monkeypatch.setattr(bridge_main_module, "_setup_logging", lambda: None)
        monkeypatch.setattr(bridge_main_module, "_ensure_daemon_running", lambda: None)
        monkeypatch.setattr(bridge_main_module, "_fetch_manifest_sync", lambda: {"connectors": []})
        monkeypatch.setattr(bridge_main_module, "_check_version_match", lambda m: None)
        monkeypatch.setattr(bridge_main_module, "_register_tools", lambda mcp, m: None)
        monkeypatch.setattr(bridge_main_module, "_run_bridge", lambda mcp: "run-bridge-sentinel")
        def raise_interrupt(coro):
            raise KeyboardInterrupt()
        monkeypatch.setattr(bridge_main_module.asyncio, "run", raise_interrupt)

        assert bridge_main_module.main([]) == 0
