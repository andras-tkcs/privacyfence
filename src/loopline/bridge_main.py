"""Loopline bridge: ephemeral stdio MCP server spawned by Claude.

Startup sequence
----------------
1. Try to connect to the daemon socket.
2. If the daemon is not running, launch it (loopline-app) and wait up to 10 s.
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

logger = logging.getLogger("loopline.bridge")

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
    """Return the command to launch loopline-app."""
    # When running as a PyInstaller bundle, the daemon is a sibling binary.
    # The main exe is named "Loopline" (the .app name); "loopline-app" is a
    # symlink to it created by build_dmg.sh for convenience.
    if getattr(sys, "frozen", False):
        bundle_macos = Path(sys.executable).parent
        for name in ("loopline-app", "Loopline"):
            candidate = bundle_macos / name
            if candidate.exists():
                return [str(candidate)]

    here = Path(sys.argv[0]).resolve().parent
    candidate = here / "loopline-app"
    if candidate.exists():
        return [str(candidate)]
    found = shutil.which("loopline-app")
    if found:
        return [found]
    # Development fallback: run as a module.
    return [sys.executable, "-m", "loopline.daemon_main"]


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
        "ERROR: Loopline daemon did not start within "
        f"{_CONNECT_TIMEOUT} seconds.\n"
        "Try running 'loopline-app' manually and check the logs.",
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


def _register_tools(mcp: FastMCP, manifest: dict) -> None:
    total = 0
    for connector_info in manifest.get("connectors", []):
        cname = connector_info["name"]
        for tool_dict in connector_info.get("tools", []):
            spec = ToolSpec.from_dict(tool_dict)
            fn = _build_tool_fn(cname, spec)
            from mcp.types import ToolAnnotations
            annotations = ToolAnnotations(
                readOnlyHint=spec.read_only,
                destructiveHint=not spec.read_only,
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
        prog="loopline-bridge",
        description="Loopline MCP bridge — connects Claude to the Loopline daemon.",
    )
    parser.add_argument("--config", default="config/settings.yaml", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    parse_args(argv)  # validates flags; config is daemon-side only

    _ensure_daemon_running()

    manifest = _fetch_manifest_sync()
    logger.info(
        "Got manifest: connectors=%s",
        [c["name"] for c in manifest.get("connectors", [])],
    )

    mcp = FastMCP(name="loopline", version=VERSION)
    _register_tools(mcp, manifest)

    try:
        asyncio.run(_run_bridge(mcp))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
