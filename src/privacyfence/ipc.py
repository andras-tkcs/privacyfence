"""IPC protocol constants shared by bridge and daemon.

Transport: 127.0.0.1 TCP loopback socket on an OS-assigned ephemeral port,
           authenticated by a per-launch random token. The daemon binds
           port 0 (asking the OS to pick a free port), then -- once it's
           actually listening -- writes that port number to PORT_FILE and a
           random token to TOKEN_FILE, both with 0o600 permissions so only
           this user's processes can read them, and both under
           ~/.privacyfence regardless of whether the daemon is otherwise
           running from source or bundled (see paths.py). A client discovers
           where to connect the same way it discovers the token: read
           PORT_FILE. Immediately after connecting -- before any JSON-RPC
           request -- it must send the token as a single bare-text line (not
           JSON). The server reads that one line, and if it doesn't match,
           closes the connection without responding and without processing
           anything further on it; only once it matches does the connection
           move on to normal newline-delimited JSON request/response
           traffic.

           This replaces what used to be a Unix domain socket at a fixed
           path. asyncio has no Unix-domain-socket support under Windows'
           event loop at all -- not a missing feature, an API that doesn't
           exist there -- so a portable transport needs to not be a Unix
           socket in the first place, on every OS, rather than adding a
           Windows-specific named-pipe branch alongside it. TOKEN_FILE
           replaces the Unix socket's own filesystem-permissions boundary
           (only this user's processes could open that path); an ephemeral,
           discovered port (rather than one fixed, hardcoded number both
           sides agree on) replaces the Unix socket path's own implicit
           per-user namespacing -- a fixed TCP port is a single namespace
           shared by every local user's loopback interface, not one scoped
           per home directory the way a filesystem path already was, so a
           hardcoded port would break the documented two-account dev/live
           setup (docs/dev-vs-live-setup.md) the moment both accounts'
           daemons tried to listen at the same time.
Format:    newline-delimited JSON.

Request:   {"id": "<str>", "method": "<str>", "params": {…}}
Response:  {"id": "<str>", "result": …}
           {"id": "<str>", "error": "<str>"}

Methods
-------
health        {} → {"version": "<str>", "connectors": ["gmail", …]}
manifest      {} → {"version": "<str>", "connectors": [{"name": "<str>", "tools": [{ToolSpec.to_dict()}]}]}
call          {"connector": "<str>", "tool": "<str>", "args": {…}} → <tool result>
check_policy  {"connector": "<str>", "tool": "<str>", "args": {…}, "reason": "<str>"} →
              {"gate": "auto"|"review"|"popup",
               "verdict": "auto_accept"|"requires_review"|"unknown",
               "matched_rule": <str|null>, "reason": "<str>",
               "pii_gate_may_apply": <bool>}
              Preflight only -- never reaches a connector, makes no external
              API call, opens no popup, and never blocks. Backs
              privacyfence_check_policy (see auto_accept.preflight_from_args).
              "reason" (like "call"'s -- see _call_connector's docstring) is
              Claude's self-reported reason for the check, recorded on the
              resulting "policy_check" audit entry; optional at this layer
              (defaults to "") even though the bridge's tool schema requires
              it, so an old bridge talking to a new daemon doesn't break.
list_rules    {"reason": "<str>"} → {"auto_accept_rules": {...}, "auto_accept_grants": {...}}
              Read-only snapshot of the persisted auto_accept_rules/auto_accept_grants
              config sections (auto_accept.get_current_config()) -- the raw,
              addressable shape a caller needs to identify an existing entry
              before proposing an update/removal via propose_rule_change, not
              the compiled/merged view build_effective_rules() produces for the
              evaluator itself. No popup, no mutation, no external API call.
              Records a lightweight "rules_listed" audit entry (like
              check_policy's "policy_check") since it discloses the full
              current rule set; "reason" is Claude's self-reported reason for
              asking, same handling as check_policy's -- optional at this
              layer even though the bridge's tool schema requires it.
propose_rule_change  {"target": "rule"|"grant", "operation": "add"|"update"|"remove",
              "reason": "<str>", ...target-specific fields} →
              {"confirmed": true, "changed": <bool>, "description": "<str>"}
              Propose an add/update/remove to auto_accept_rules (target="rule")
              or auto_accept_grants (target="grant"), gated behind the same
              native confirmation dialog the "Always allow" popup button
              uses -- see gate.propose_rule_change()'s docstring for the full
              field list per target and for why declining (or an unattended
              connection) raises rather than returning a false-y result, same
              as any other gated write. This is the one write path a bridge
              connection has into settings.yaml; there is no way to skip the
              popup from here.
              target="rule" fields: operation_key, rule_name, value (required
              for add/update), old_value (optional, update only -- the prior
              value to remove before adding the new one).
              target="grant" fields: connector, config_key, resource_id
              (required), name/tab (optional, cosmetic/spreadsheet-tab),
              capabilities (dict of capability_key -> bool, add/update only).
begin_unattended_session  {"reason": "<str>"} → {"unattended": true}
              Marks THIS connection as running an unattended/scheduled task:
              until end_unattended_session (or disconnect), any "call" on
              this connection that would otherwise open a native review/popup
              dialog and no auto-accept rule already covers is denied
              immediately (audited as "denied_unattended") instead of
              blocking. Errors if unattended_sessions.enabled is false in
              the organization config bundle (org_config.json; off by
              default -- an administrator opts in).
              Never changes what auto-accepts, only what happens when
              nothing does. See docs/TECHNICAL_REFERENCE.md's "Scheduled /
              unattended Cowork tasks" section. "reason" is recorded on the
              "unattended_session_started" audit entry -- the only record of
              why for calls this session denies without ever showing a
              popup; optional at this layer, same reasoning as check_policy's.
end_unattended_session    {"reason": "<str>"} → {"unattended": false}
              Clears the flag set by begin_unattended_session on this
              connection. Also cleared automatically if the connection drops
              (the bridge is one process per Cowork task, so this normally
              happens anyway when the task ends) -- that automatic path has
              no "reason" to record. "reason" is recorded on the
              "unattended_session_ended" audit entry when called explicitly;
              optional at this layer, same reasoning as check_policy's.

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

HOST = "127.0.0.1"
# Never bound to 0.0.0.0 or any other interface -- HOST above is what
# actually keeps this off the network, not the port number, which is why
# an OS-assigned ephemeral port (rather than a fixed one) costs nothing on
# the security side while fixing the multi-account collision problem
# described above.
#
# Per-launch, per-user files under ~/.privacyfence, written by ipc_server.py's
# start() with 0o600 permissions so only this user's processes can read
# them -- together these replace the access boundary + addressability a
# fixed Unix socket path used to provide in one name.
PORT_FILE = os.path.expanduser("~/.privacyfence/ipc_port")
TOKEN_FILE = os.path.expanduser("~/.privacyfence/ipc_token")

# Messages are newline-delimited JSON; asyncio's default StreamReader line
# limit is 64 KiB, well under drive's 100 KiB file-content cap (before JSON
# escaping overhead even). Raise it generously so a big tool result never
# breaks the line framing.
LINE_LIMIT = 8 * 1024 * 1024
