"""IPC protocol constants shared by bridge and daemon.

Transport: Unix domain socket at SOCKET_PATH.
Format:    newline-delimited JSON.

Request:   {"id": "<str>", "method": "<str>", "params": {…}}
Response:  {"id": "<str>", "result": …}
           {"id": "<str>", "error": "<str>"}

Methods
-------
health    {} → {"version": "<str>", "connectors": ["gmail", …]}
manifest  {} → {"version": "<str>", "connectors": [{"name": "<str>", "tools": [{ToolSpec.to_dict()}]}]}
call      {"connector": "<str>", "tool": "<str>", "args": {…}} → <tool result>

The bridge and the daemon are built and distributed independently (the bridge
ships in PrivacyFence.mcpb, the daemon in PrivacyFenceApp.app) so they can end
up out of sync — e.g. the app auto-updates but the running daemon process
isn't restarted yet. Both sides report the same VERSION (the package version,
not a separate protocol number) in "manifest"/"health" so the bridge can
refuse to proceed and tell the user to restart something, rather than risk
silently talking a mismatched wire format.
"""

from __future__ import annotations

import os

from . import __version__ as VERSION

SOCKET_PATH = os.path.expanduser("~/.privacyfence/privacyfence.sock")

# Messages are newline-delimited JSON; asyncio's default StreamReader line
# limit is 64 KiB, well under drive's 100 KiB file-content cap (before JSON
# escaping overhead even). Raise it generously so a big tool result never
# breaks the line framing.
LINE_LIMIT = 8 * 1024 * 1024
