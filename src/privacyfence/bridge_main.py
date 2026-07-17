"""PrivacyFence bridge: ephemeral stdio MCP server spawned by Claude.

Startup sequence
----------------
1. Try to connect to the daemon socket.
2. If the daemon is not running, launch it (privacyfence-app) and wait up to 10 s.
3. Fetch the connector manifest from the daemon.
4. Register all connector tools with FastMCP dynamically.
5. Run FastMCP on the stdio transport — Claude can now call tools.

Each tool call is forwarded to the daemon over the persistent socket connection.
The bridge carries no state of its own; it is safe for Claude to kill and
restart it at any time.

Logs go to stderr only (stdout is the MCP protocol channel).
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from .connector import ToolSpec
from .ipc import SOCKET_PATH, VERSION
from .ipc_client import IPCClient, IPCError

logger = logging.getLogger("privacyfence.bridge")

_CONNECT_TIMEOUT = 10  # seconds to wait for daemon startup
_CONNECT_INTERVAL = 0.4

# Module-level client shared across all tool handler coroutines.
_ipc: IPCClient | None = None


# ---------------------------------------------------------------------------- #
# Logging (stderr only — stdout is the MCP wire protocol)
# ---------------------------------------------------------------------------- #

def _setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s [%(name)s] %(message)s")
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(h)


# ---------------------------------------------------------------------------- #
# Daemon auto-start + socket connection
# ---------------------------------------------------------------------------- #

def _find_daemon_cmd() -> list[str]:
    """Return the command to launch privacyfence-app.

    The bridge is built and distributed separately from the daemon (see
    PrivacyFenceBridge.spec) — it is never a sibling of privacyfence-app on
    disk — so this normally only matters as a fallback: the daemon should
    already be running via its LaunchAgent by the time Claude spawns us.
    """
    here = Path(sys.argv[0]).resolve().parent
    candidate = here / "privacyfence-app"
    if candidate.exists():
        return [str(candidate)]
    found = shutil.which("privacyfence-app")
    if found:
        return [found]
    default_app = Path("/Applications/PrivacyFenceApp.app/Contents/MacOS/privacyfence-app")
    if default_app.exists():
        return [str(default_app)]
    # Development fallback: run as a module.
    return [sys.executable, "-m", "privacyfence.daemon_main"]


def _socket_connectable() -> bool:
    """Return True if the daemon socket is accepting connections right now."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(SOCKET_PATH)
        s.close()
        return True
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        return False


def _ensure_daemon_running() -> None:
    """Connect to daemon, launching it first if needed. Blocks until ready."""
    if _socket_connectable():
        logger.info("Daemon already running")
        return

    logger.info("Daemon not running — launching it now")
    cmd = _find_daemon_cmd()
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # detach from our process group
    )

    deadline = time.monotonic() + _CONNECT_TIMEOUT
    while time.monotonic() < deadline:
        if _socket_connectable():
            logger.info("Daemon is ready")
            return
        time.sleep(_CONNECT_INTERVAL)

    print(
        "ERROR: PrivacyFence daemon did not start within "
        f"{_CONNECT_TIMEOUT} seconds.\n"
        "Try running 'privacyfence-app' manually and check the logs.",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------- #
# Sync manifest fetch (before FastMCP event loop starts)
# ---------------------------------------------------------------------------- #

def _fetch_manifest_sync() -> dict:
    """Open a short-lived connection to get the connector manifest."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(SOCKET_PATH)
    req = json.dumps({"id": "m0", "method": "manifest", "params": {}}) + "\n"
    s.sendall(req.encode())
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf.split(b"\n")[0])["result"]


def _check_version_match(manifest: dict) -> None:
    """Refuse to proceed if the daemon is running a different PrivacyFence version.

    The bridge (PrivacyFence.mcpb) and the daemon (PrivacyFenceApp.app) are
    built and updated independently, so a stale daemon process (e.g. left
    running across an app update) can silently drift from the bridge's wire
    format expectations. Fail loudly instead of risking a confusing crash
    deeper inside a tool call.
    """
    daemon_version = manifest.get("version")
    if daemon_version is None or daemon_version == VERSION:
        return
    print(
        "ERROR: PrivacyFence version mismatch — refusing to start.\n"
        f"  Claude extension (privacyfence-bridge): {VERSION}\n"
        f"  Running daemon (PrivacyFenceApp.app):    {daemon_version}\n"
        "\n"
        "This usually happens when PrivacyFenceApp.app was updated (or "
        "reinstalled) but the previously running daemon process was never "
        "restarted, or when the Claude extension was updated separately "
        "from the app.\n"
        "\n"
        "To fix it:\n"
        "  1. Quit PrivacyFence from its menu bar icon (or run: "
        "pkill -f PrivacyFenceApp)\n"
        "  2. Relaunch PrivacyFenceApp.app so it starts on the same version "
        "as the extension\n"
        "  3. Restart this conversation in Claude so it reconnects\n",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------- #
# Dynamic tool registration
# ---------------------------------------------------------------------------- #

_ANNOTATION_MAP: dict[str, type] = {
    "str": str,
    "int": int,
    "bool": bool,
    "float": float,
}


def _build_tool_fn(
    connector_name: str,
    spec: ToolSpec,
) -> Any:
    """Return a coroutine function with the correct signature for FastMCP."""
    sig_params = []
    for p in spec.params:
        ann = _ANNOTATION_MAP.get(p.annotation, str)
        if p.required:
            sig_params.append(
                inspect.Parameter(p.name, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=ann)
            )
        else:
            sig_params.append(
                inspect.Parameter(
                    p.name,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=p.default,
                    annotation=ann,
                )
            )

    async def _handler(**kwargs: Any) -> Any:
        global _ipc
        if _ipc is None:
            raise ToolError("IPC client not initialized")
        try:
            return await _ipc.call(connector_name, spec.name, kwargs)
        except IPCError as exc:
            raise ToolError(str(exc)) from exc

    _handler.__name__ = spec.name
    _handler.__doc__ = spec.description
    _handler.__signature__ = inspect.Signature(sig_params)
    _handler.__annotations__ = {p.name: _ANNOTATION_MAP.get(p.annotation, str) for p in spec.params}
    return _handler


async def _check_policy_handler(
    connector: str, tool: str, reason: str, args: dict[str, Any] | None = None
) -> dict:
    global _ipc
    if _ipc is None:
        raise ToolError("IPC client not initialized")
    try:
        return await _ipc.check_policy(connector, tool, args or {}, reason)
    except IPCError as exc:
        raise ToolError(str(exc)) from exc


async def _begin_unattended_session_handler(reason: str) -> dict:
    global _ipc
    if _ipc is None:
        raise ToolError("IPC client not initialized")
    try:
        return await _ipc.begin_unattended_session(reason)
    except IPCError as exc:
        raise ToolError(str(exc)) from exc


async def _end_unattended_session_handler(reason: str) -> dict:
    global _ipc
    if _ipc is None:
        raise ToolError("IPC client not initialized")
    try:
        return await _ipc.end_unattended_session(reason)
    except IPCError as exc:
        raise ToolError(str(exc)) from exc


def _register_meta_tools(mcp: FastMCP) -> None:
    """Register PrivacyFence's own tools -- not sourced from a connector
    manifest, since they aren't backed by a real connector. See
    docs/TECHNICAL_REFERENCE.md's "Scheduled / unattended Cowork tasks"
    section.
    """
    from mcp.types import ToolAnnotations

    mcp.tool(
        name="privacyfence_check_policy",
        description=(
            "Ask PrivacyFence, before calling a gated tool, whether that specific call would "
            "auto-accept or need a human. Pass the same connector, tool, and args you're about "
            "to call, plus reason: one sentence on why you're checking this right now (logged, "
            "self-reported, unverified -- same as every gated tool's reason param). Returns "
            "{gate, verdict, matched_rule, reason, pii_gate_may_apply}, where "
            "verdict is one of: 'auto_accept' (the real call will pass through identically), "
            "'requires_review' (no configured rule can match these arguments, with or without "
            "fetching anything), or 'unknown' (whether it auto-accepts depends on the actual "
            "fetched content, which this can't see in advance). For 'review'-gated (read) tools, "
            "pii_gate_may_apply is always true: PrivacyFence's PII detection gate scans real "
            "content and can force a popup even when a rule matches, and that can never be "
            "predicted ahead of time. This makes no external API call, opens no popup, and has "
            "no side effects -- call it as often as you want while planning a task. Most useful "
            "before and during a scheduled/unattended Cowork run, to plan around steps that would "
            "otherwise need a human who isn't there."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    )(_check_policy_handler)

    mcp.tool(
        name="privacyfence_begin_unattended_session",
        description=(
            "Tell PrivacyFence this conversation is an unattended/scheduled Cowork run (e.g. a "
            "Routine firing on a schedule) with no human necessarily watching, for the rest of "
            "this connection. From then on, any gated tool call that isn't already covered by a "
            "configured auto-accept rule is denied immediately with a clear error, instead of "
            "PrivacyFence opening a native approval dialog that nobody will answer. Call this once "
            "at the start of a scheduled run, and pair it with privacyfence_check_policy to plan "
            "which steps are safe to attempt. Never changes what auto-accepts, only what happens "
            "when nothing does. Errors if an administrator hasn't enabled unattended sessions for "
            "this install. Do not call this during a normal interactive conversation -- it makes "
            "denials immediate instead of prompting. reason: one sentence on why this session is "
            "unattended (e.g. the Routine/schedule that triggered it) -- logged in the audit "
            "entry for this session change, since no popup is shown for it to appear in."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    )(_begin_unattended_session_handler)

    mcp.tool(
        name="privacyfence_end_unattended_session",
        description=(
            "Clear the unattended-session flag set by privacyfence_begin_unattended_session for "
            "this connection, restoring normal interactive approval behavior. Call this when a "
            "scheduled run finishes. Not strictly required -- the flag also clears automatically "
            "when the connection closes -- but call it if this connection might be reused "
            "afterward for something interactive. reason: one sentence on why the unattended "
            "session is ending now -- logged the same way as privacyfence_begin_unattended_session's."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
    )(_end_unattended_session_handler)

    logger.info(
        "Registered meta-tools: privacyfence_check_policy, privacyfence_begin_unattended_session, "
        "privacyfence_end_unattended_session"
    )


def _register_tools(mcp: FastMCP, manifest: dict) -> None:
    total = 0
    for connector_info in manifest.get("connectors", []):
        cname = connector_info["name"]
        for tool_dict in connector_info.get("tools", []):
            spec = ToolSpec.from_dict(tool_dict)
            fn = _build_tool_fn(cname, spec)
            from mcp.types import ToolAnnotations
            # Deliberately advertise EVERY tool — reads and writes alike — as
            # read-only / non-destructive to the MCP client (Claude Code / Cowork).
            #
            # Why: MCP tool annotations are UI hints, not security boundaries
            # (the spec is explicit: "these are hints, not guarantees"). Claude
            # uses them only to decide which permission prompts to show. Write
            # tools default to destructiveHint=true, which makes Cowork prompt on
            # every call and greys out "Allow all for this task" — with no
            # org-level pre-approval available on the Team plan.
            #
            # The REAL authorization does not happen in the client. Every call is
            # forwarded over IPC to the PrivacyFence daemon, which enforces the
            # per-tool gate (auto / review / popup), the auto-accept rules, and
            # the audit log before any external read or write occurs. That gate
            # is the actual security boundary; the client-side prompt would only
            # be a redundant second gate. So we suppress it by presenting a
            # uniformly read-only surface to Claude and let PrivacyFence do the
            # checking. `spec.read_only` still records each tool's true nature for
            # the daemon and the audit log — we only override what Claude is told.
            annotations = ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
            )
            mcp.tool(name=spec.name, description=spec.description, annotations=annotations)(fn)
            logger.info("Registered tool: %s (connector=%s)", spec.name, cname)
            total += 1
    logger.info("Bridge registered %d tool(s) from %d connector(s)", total, len(manifest.get("connectors", [])))


# ---------------------------------------------------------------------------- #
# FastMCP lifespan: open/close the persistent IPC connection
# ---------------------------------------------------------------------------- #

async def _run_bridge(mcp: FastMCP) -> None:
    global _ipc
    _ipc = IPCClient(SOCKET_PATH)
    await _ipc.connect()
    logger.info("IPC client connected; starting stdio MCP")
    try:
        await mcp.run_async(transport="stdio")
    finally:
        await _ipc.close()


# ---------------------------------------------------------------------------- #
# Entry point
# ---------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="privacyfence-bridge",
        description="PrivacyFence MCP bridge — connects Claude to the PrivacyFence daemon.",
    )
    parser.add_argument("--config", default="config/settings.yaml", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    parse_args(argv)  # validates flags; config is daemon-side only

    _ensure_daemon_running()

    manifest = _fetch_manifest_sync()
    _check_version_match(manifest)
    logger.info(
        "Got manifest: connectors=%s",
        [c["name"] for c in manifest.get("connectors", [])],
    )

    mcp = FastMCP(name="privacyfence", version=VERSION)
    _register_tools(mcp, manifest)
    _register_meta_tools(mcp)

    try:
        asyncio.run(_run_bridge(mcp))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
