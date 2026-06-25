"""IPC protocol constants shared by bridge and daemon.

Transport: Unix domain socket at SOCKET_PATH.
Format:    newline-delimited JSON.

Request:   {"id": "<str>", "method": "<str>", "params": {…}}
Response:  {"id": "<str>", "result": …}
           {"id": "<str>", "error": "<str>"}

Methods
-------
health    {} → {"version": "<str>", "connectors": ["gmail", …]}
manifest  {} → {"connectors": [{"name": "<str>", "tools": [{ToolSpec.to_dict()}]}]}
call      {"connector": "<str>", "tool": "<str>", "args": {…}} → <tool result>
"""

from __future__ import annotations

import os

SOCKET_PATH = os.path.expanduser("~/.loopline/loopline.sock")
VERSION = "0.2.1"
