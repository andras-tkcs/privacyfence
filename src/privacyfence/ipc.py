"""IPC protocol constants shared by bridge and daemon.

Transport: Unix domain socket at SOCKET_PATH.
Format:    newline-delimited JSON.

Request:   {"id": "<str>", "method": "<str>", "params": {…}}
Response:  {"id": "<str>", "result": …}
           {"id": "<str>", "error": "<str>"}

Methods
-------
health        {} → {"version": "<str>", "connectors": ["gmail", …]}
manifest      {} → {"version": "<str>", "connectors": [{"name": "<str>", "tools": [{ToolSpec.to_dict()}]}]}
call          {"connector": "<str>", "tool": "<str>", "args": {…}} → <tool result>
check_policy  {"connector": "<str>", "tool": "<str>", "args": {…}} →
              {"gate": "auto"|"review"|"popup",
               "verdict": "auto_accept"|"requires_review"|"unknown",
               "matched_rule": <str|null>, "reason": "<str>",
               "pii_gate_may_apply": <bool>}
              Preflight only -- never reaches a connector, makes no external
              API call, opens no popup, and never blocks. Backs
              privacyfence_check_policy (see auto_accept.preflight_from_args
              and docs/cowork-scheduled-tasks-design.md).
begin_unattended_session  {} → {"unattended": true}
              Marks THIS connection as running an unattended/scheduled task:
              until end_unattended_session (or disconnect), any "call" on
              this connection that would otherwise open a native review/popup
              dialog and no auto-accept rule already covers is denied
              immediately (audited as "denied_unattended") instead of
              blocking. Errors if unattended_sessions.enabled is false in
              settings.yaml (off by default -- an administrator opts in).
              Never changes what auto-accepts, only what happens when
              nothing does. See docs/cowork-scheduled-tasks-design.md.
end_unattended_session    {} → {"unattended": false}
              Clears the flag set by begin_unattended_session on this
              connection. Also cleared automatically if the connection drops
              (the bridge is one process per Cowork task, so this normally
              happens anyway when the task ends).

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
