"""Tool manifest translation for the ``/mcp`` endpoint (routes_mcp.py).

Ports bridge/src/tools.ts's schema mapping (``paramSchema``/
``buildInputShape``/``UNIFORM_READ_ONLY_ANNOTATIONS``/``toCallToolResult``)
from zod/TypeScript into JSON Schema/Python -- a translation of an existing,
shipped mapping, not a redesign (docs/https-connector-refactor-plan.md §8.1).
``ToolSpec.to_dict()`` (connector.py) stays the single source of truth for
what a tool *is*; this module only decides how that shape is presented to an
MCP client.

Also carries PrivacyFence's own meta-tools (privacyfence_check_policy and
friends) -- not sourced from any connector's manifest, ported field-for-field
from bridge/src/tools.ts's ``registerMetaTools`` (same names, same
descriptions, same input shapes) since routes_mcp.py replaces the bridge as
the thing serving them, not what they are (§8.1: "the other three move into
web/routes_mcp.py against the connector registry directly").
"""
from __future__ import annotations

import json
from typing import Any

from mcp import types

from ..connector import ToolSpec

# Same rationale as bridge/src/tools.ts's UNIFORM_READ_ONLY_ANNOTATIONS: MCP
# tool annotations are UI hints, not a security boundary (the spec says so
# explicitly). The real authorization is gate.py's gate -- auto/review/popup,
# auto-accept rules, the audit log -- enforced here in the daemon itself, not
# in the calling client. Advertising every tool uniformly as read-only/
# non-destructive keeps the client from throwing its own redundant
# confirmation prompt in front of gate.py's real one (see
# TECHNICAL_REFERENCE.md's "Why every tool is advertised as read-only",
# referenced by §8.1 of the refactor plan).
_UNIFORM_READ_ONLY_ANNOTATIONS = types.ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True,
)

_JSON_SCHEMA_TYPE = {"int": "integer", "float": "number", "bool": "boolean"}


def _param_schema(annotation: str, description: str) -> dict[str, Any]:
    # Unknown annotation types fall back to string -- mirrors tools.ts's own
    # `case "str": default:` fallthrough.
    schema: dict[str, Any] = {"type": _JSON_SCHEMA_TYPE.get(annotation, "string")}
    if description:
        schema["description"] = description
    return schema


def tool_input_schema(spec: ToolSpec) -> dict[str, Any]:
    """JSON Schema for ``spec``'s params -- the Python-side equivalent of
    tools.ts's ``buildInputShape``. An optional param with a non-null
    default carries it forward as the schema's own ``default`` (informational
    for the client; PrivacyFence still applies the connector's own default
    when the arg is actually omitted)."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for p in spec.params:
        schema = _param_schema(p.annotation, p.description)
        if not p.required and p.default is not None:
            schema["default"] = p.default
        properties[p.name] = schema
        if p.required:
            required.append(p.name)
    result: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    return result


def to_mcp_tool(spec: ToolSpec) -> types.Tool:
    return types.Tool(
        name=spec.name,
        description=spec.description,
        inputSchema=tool_input_schema(spec),
        annotations=_UNIFORM_READ_ONLY_ANNOTATIONS,
    )


def to_call_tool_result(value: Any) -> types.CallToolResult:
    """Mirrors tools.ts's ``toCallToolResult``: a string result becomes plain
    text; any other JSON-serializable value is also rendered as text (so
    there's always something readable) and, when it's a plain dict, also
    attached as ``structuredContent`` -- the same "no explicit output schema"
    case fastmcp's own default ``convert_result`` handles that way."""
    if value is None:
        return types.CallToolResult(content=[])
    if isinstance(value, str):
        return types.CallToolResult(content=[types.TextContent(type="text", text=value)])
    text = json.dumps(value, default=str)
    content = [types.TextContent(type="text", text=text)]
    if isinstance(value, dict):
        return types.CallToolResult(content=content, structuredContent=value)
    return types.CallToolResult(content=content)


def error_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)], isError=True)


# --------------------------------------------------------------------------- #
# Meta-tools -- ported verbatim (name, description, schema) from
# bridge/src/tools.ts's registerMetaTools(). See routes_mcp.py for the
# handlers dispatching these to gate.py/auto_accept.py.
# --------------------------------------------------------------------------- #

CHECK_POLICY_TOOL = types.Tool(
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
    inputSchema={
        "type": "object",
        "properties": {
            "connector": {"type": "string"},
            "tool": {"type": "string"},
            "reason": {"type": "string"},
            "args": {"type": "object"},
        },
        "required": ["connector", "tool", "reason"],
    },
    annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
)

LIST_RULES_TOOL = types.Tool(
    name="privacyfence_list_auto_accept_rules",
    description=(
        "List the auto-accept rules and grants currently configured in PrivacyFence's "
        "settings.yaml -- both the auto_accept_rules section (per-operation rule entries) and "
        "the auto_accept_grants section (resource-scoped grants, e.g. a trusted Drive sandbox "
        "folder that covers several sheets.*/drive.* operations at once). Call this before "
        "privacyfence_propose_auto_accept_rule_change: update/remove target an existing entry "
        "by its exact identifying fields (operation_key/rule_name/value for a rule; "
        "connector/config_key/resource_id for a grant), and those fields only match something "
        "if you listed it first rather than guessed. Read-only, no popup -- reason: one "
        "sentence on why you're listing the current rules right now (logged, self-reported, "
        "same as every other gated/meta tool's reason param, since this discloses the full "
        "current rule set)."
    ),
    inputSchema={"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
)

PROPOSE_RULE_CHANGE_TOOL = types.Tool(
    name="privacyfence_propose_auto_accept_rule_change",
    description=(
        "Propose adding, updating, or removing an auto-accept rule or grant in PrivacyFence's "
        "settings.yaml. This ALWAYS blocks on a native confirmation dialog a human must "
        "approve -- there is no way to change this config without one, even if an identical "
        "entry already exists. If declined, or if this connection is in an unattended "
        "session, the call throws -- never assume success without checking the result. Call "
        "privacyfence_list_auto_accept_rules first so update/remove target an entry that "
        "actually exists rather than guessing identifiers.\n\n"
        "target='rule' edits the auto_accept_rules section (one list of {rule, value} entries "
        "per operation_key): operation_key (e.g. 'sheets.format_range'), rule_name (e.g. "
        "'trusted_sender_domain' -- must be one of the real rule names PrivacyFence's rule engine "
        "knows, see privacyfence_list_auto_accept_rules' output or the Auto-accept rules tables in "
        "the docs; an unrecognized name is rejected before any popup is shown, not silently "
        "persisted as a dead rule), value (required for add/update -- often a list), old_value "
        "(update only -- the prior value being replaced; omit to add alongside the existing "
        "value instead of replacing it).\n\n"
        "target='grant' edits the auto_accept_grants section (one resource trusted once, "
        "covering several operations at a time -- e.g. a Drive sandbox folder): connector "
        "(e.g. 'drive'), config_key (e.g. 'sandbox_folders'), resource_id (required), name "
        "(optional cosmetic label), tab (no current resource type uses this), capabilities (add/update only -- "
        "a map of capability key, e.g. 'write', to true/false; see "
        "privacyfence_list_auto_accept_rules' auto_accept_grants output for which capability "
        "keys apply to which resource type).\n\n"
        "reason: one sentence on why you're proposing this change -- logged, self-reported, "
        "unverified, same as every other gated tool's reason param."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "target": {"type": "string", "enum": ["rule", "grant"]},
            "operation": {"type": "string", "enum": ["add", "update", "remove"]},
            "reason": {"type": "string"},
            "operation_key": {"type": "string"},
            "rule_name": {"type": "string"},
            "value": {},
            "old_value": {},
            "connector": {"type": "string"},
            "config_key": {"type": "string"},
            "resource_id": {"type": "string"},
            "name": {"type": "string"},
            "tab": {"type": "string"},
            "capabilities": {"type": "object", "additionalProperties": {"type": "boolean"}},
        },
        "required": ["target", "operation", "reason"],
    },
    annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
)

BEGIN_UNATTENDED_SESSION_TOOL = types.Tool(
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
    inputSchema={"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
)

END_UNATTENDED_SESSION_TOOL = types.Tool(
    name="privacyfence_end_unattended_session",
    description=(
        "Clear the unattended-session flag set by privacyfence_begin_unattended_session for "
        "this connection, restoring normal interactive approval behavior. Call this when a "
        "scheduled run finishes. Not strictly required -- the flag also clears automatically "
        "when the connection closes -- but call it if this connection might be reused "
        "afterward for something interactive. reason: one sentence on why the unattended "
        "session is ending now -- logged the same way as privacyfence_begin_unattended_session's."
    ),
    inputSchema={"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True),
)

META_TOOLS: tuple[types.Tool, ...] = (
    CHECK_POLICY_TOOL,
    LIST_RULES_TOOL,
    PROPOSE_RULE_CHANGE_TOOL,
    BEGIN_UNATTENDED_SESSION_TOOL,
    END_UNATTENDED_SESSION_TOOL,
)
META_TOOL_NAMES: frozenset[str] = frozenset(t.name for t in META_TOOLS)
