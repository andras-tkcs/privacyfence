"""Embedded HTTP(S) server package -- see docs/https-connector-refactor-plan.md
§3 for the full target module layout. P1 landed ``server.py`` (lifecycle,
bind policy, security headers) and ``routes_approvals.py`` (the approval
surface WebApprovalUI blocks on). P2 adds ``routes_mcp.py`` (the Streamable
HTTP MCP endpoint), ``mcp_dispatch.py`` (its connector-call dispatch --
dedupe, gating, meta-tools), ``mcp_tools.py`` (ToolSpec -> MCP tool
translation) and ``mcp_auth.py`` (its bearer-token auth). ``auth.py``/
``routes_settings.py``/``routes_oauth.py`` are later phases (P4/P7-P9).

Nothing under this package imports AppKit/PyObjC -- it has to stay
importable and testable on any platform (see approval_ui.py's own docstring
for why this matters starting at P1).
"""
from __future__ import annotations
