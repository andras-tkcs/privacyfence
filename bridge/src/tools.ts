/**
 * Dynamic tool registration + PrivacyFence's own meta-tools. Ported from
 * bridge_main.py's _build_tool_fn / _register_tools / _register_meta_tools.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import type { CallToolResult, ToolAnnotations } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";
import { IPCClientLike, IPCError } from "./ipcClient.js";
import type { ConnectorManifestEntry, Manifest, ToolParamDict, ToolSpecDict } from "./manifest.js";

// Deliberately advertise EVERY tool — reads and writes alike — as read-only /
// non-destructive to the MCP client (Claude Code / Cowork).
//
// Why: MCP tool annotations are UI hints, not security boundaries (the spec
// is explicit: "these are hints, not guarantees"). Claude uses them only to
// decide which permission prompts to show. Write tools default to
// destructiveHint=true, which makes Cowork prompt on every call and greys
// out "Allow all for this task" — with no org-level pre-approval available
// on the Team plan.
//
// The REAL authorization does not happen in the client. Every call is
// forwarded over IPC to the PrivacyFence daemon, which enforces the per-tool
// gate (auto / review / popup), the auto-accept rules, and the audit log
// before any external read or write occurs. That gate is the actual
// security boundary; the client-side prompt would only be a redundant
// second gate. So we suppress it by presenting a uniformly read-only
// surface to Claude and let PrivacyFence do the checking. Each connector's
// ToolSpec.read_only still records the tool's true nature for the daemon
// and the audit log — we only override what Claude is told.
const UNIFORM_READ_ONLY_ANNOTATIONS: ToolAnnotations = {
  readOnlyHint: true,
  destructiveHint: false,
  idempotentHint: true,
};

function paramSchema(p: ToolParamDict): z.ZodTypeAny {
  let base: z.ZodTypeAny;
  switch (p.annotation) {
    case "int":
    case "float":
      base = z.number();
      break;
    case "bool":
      base = z.boolean();
      break;
    case "str":
    default:
      // Unknown annotation types fall back to string, mirroring
      // bridge_main.py's _ANNOTATION_MAP.get(p.annotation, str).
      base = z.string();
      break;
  }
  if (p.description) base = base.describe(p.description);
  if (!p.required) {
    return p.default === null || p.default === undefined ? base.optional() : base.optional().default(p.default);
  }
  return base;
}

function buildInputShape(params: ToolParamDict[]): Record<string, z.ZodTypeAny> {
  const shape: Record<string, z.ZodTypeAny> = {};
  for (const p of params) {
    shape[p.name] = paramSchema(p);
  }
  return shape;
}

/**
 * Convert an arbitrary JSON value returned by the daemon into a
 * CallToolResult, mirroring fastmcp's default `convert_result`: strings
 * become plain text content; other JSON values are also serialized as text
 * (so Claude always has something readable), and plain objects additionally
 * get `structuredContent` — fastmcp only attaches structured content for
 * dict-shaped results when the tool has no explicit output schema, which is
 * the case for every dynamically-registered tool here.
 */
function toCallToolResult(value: unknown): CallToolResult {
  if (value === null || value === undefined) {
    return { content: [] };
  }
  if (typeof value === "string") {
    return { content: [{ type: "text", text: value }] };
  }
  const text = JSON.stringify(value);
  const isPlainObject =
    typeof value === "object" && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype;
  if (isPlainObject) {
    return {
      content: [{ type: "text", text }],
      structuredContent: value as Record<string, unknown>,
    };
  }
  return { content: [{ type: "text", text }] };
}

function registerConnectorTool(server: McpServer, ipc: IPCClientLike, connectorName: string, spec: ToolSpecDict): void {
  server.registerTool(
    spec.name,
    {
      description: spec.description,
      inputSchema: buildInputShape(spec.params),
      annotations: UNIFORM_READ_ONLY_ANNOTATIONS,
    },
    async (args: Record<string, unknown>): Promise<CallToolResult> => {
      try {
        const result = await ipc.call(connectorName, spec.name, args);
        return toCallToolResult(result);
      } catch (exc) {
        if (exc instanceof IPCError) throw new Error(exc.message);
        throw exc;
      }
    }
  );
  console.error(`Registered tool: ${spec.name} (connector=${connectorName})`);
}

export function registerTools(server: McpServer, ipc: IPCClientLike, manifest: Manifest): void {
  let total = 0;
  const connectors: ConnectorManifestEntry[] = manifest.connectors ?? [];
  for (const connectorInfo of connectors) {
    for (const toolDict of connectorInfo.tools ?? []) {
      registerConnectorTool(server, ipc, connectorInfo.name, toolDict);
      total++;
    }
  }
  console.error(`Bridge registered ${total} tool(s) from ${connectors.length} connector(s)`);
}

/**
 * Register PrivacyFence's own tools -- not sourced from a connector
 * manifest, since they aren't backed by a real connector. See
 * docs/TECHNICAL_REFERENCE.md's "Scheduled / unattended Cowork tasks"
 * section.
 */
export function registerMetaTools(server: McpServer, ipc: IPCClientLike): void {
  server.registerTool(
    "privacyfence_check_policy",
    {
      description:
        "Ask PrivacyFence, before calling a gated tool, whether that specific call would " +
        "auto-accept or need a human. Pass the same connector, tool, and args you're about " +
        "to call, plus reason: one sentence on why you're checking this right now (logged, " +
        "self-reported, unverified -- same as every gated tool's reason param). Returns " +
        "{gate, verdict, matched_rule, reason, pii_gate_may_apply}, where " +
        "verdict is one of: 'auto_accept' (the real call will pass through identically), " +
        "'requires_review' (no configured rule can match these arguments, with or without " +
        "fetching anything), or 'unknown' (whether it auto-accepts depends on the actual " +
        "fetched content, which this can't see in advance). For 'review'-gated (read) tools, " +
        "pii_gate_may_apply is always true: PrivacyFence's PII detection gate scans real " +
        "content and can force a popup even when a rule matches, and that can never be " +
        "predicted ahead of time. This makes no external API call, opens no popup, and has " +
        "no side effects -- call it as often as you want while planning a task. Most useful " +
        "before and during a scheduled/unattended Cowork run, to plan around steps that would " +
        "otherwise need a human who isn't there.",
      inputSchema: {
        connector: z.string(),
        tool: z.string(),
        reason: z.string(),
        args: z.record(z.string(), z.unknown()).optional(),
      },
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true },
    },
    async ({ connector, tool, reason, args }): Promise<CallToolResult> => {
      try {
        const result = await ipc.checkPolicy(connector, tool, args ?? {}, reason);
        return toCallToolResult(result);
      } catch (exc) {
        if (exc instanceof IPCError) throw new Error(exc.message);
        throw exc;
      }
    }
  );

  server.registerTool(
    "privacyfence_list_auto_accept_rules",
    {
      description:
        "List the auto-accept rules and grants currently configured in PrivacyFence's " +
        "settings.yaml -- both the auto_accept_rules section (per-operation rule entries) and " +
        "the auto_accept_grants section (resource-scoped grants, e.g. a trusted Drive sandbox " +
        "folder that covers several sheets.*/drive.* operations at once). Call this before " +
        "privacyfence_propose_auto_accept_rule_change: update/remove target an existing entry " +
        "by its exact identifying fields (operation_key/rule_name/value for a rule; " +
        "connector/config_key/resource_id for a grant), and those fields only match something " +
        "if you listed it first rather than guessed. Read-only, no popup -- reason: one " +
        "sentence on why you're listing the current rules right now (logged, self-reported, " +
        "same as every other gated/meta tool's reason param, since this discloses the full " +
        "current rule set).",
      inputSchema: { reason: z.string() },
      annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true },
    },
    async ({ reason }): Promise<CallToolResult> => {
      try {
        const result = await ipc.listRules(reason);
        return toCallToolResult(result);
      } catch (exc) {
        if (exc instanceof IPCError) throw new Error(exc.message);
        throw exc;
      }
    }
  );

  server.registerTool(
    "privacyfence_propose_auto_accept_rule_change",
    {
      description:
        "Propose adding, updating, or removing an auto-accept rule or grant in PrivacyFence's " +
        "settings.yaml. This ALWAYS blocks on a native confirmation dialog a human must " +
        "approve -- there is no way to change this config without one, even if an identical " +
        "entry already exists. If declined, or if this connection is in an unattended " +
        "session, the call throws -- never assume success without checking the result. Call " +
        "privacyfence_list_auto_accept_rules first so update/remove target an entry that " +
        "actually exists rather than guessing identifiers.\n\n" +
        "target='rule' edits the auto_accept_rules section (one list of {rule, value} entries " +
        "per operation_key): operation_key (e.g. 'sheets.format_range'), rule_name (e.g. " +
        "'trusted_sender_domain' -- must be one of the real rule names PrivacyFence's rule engine " +
        "knows, see privacyfence_list_auto_accept_rules' output or the Auto-accept rules tables in " +
        "the docs; an unrecognized name is rejected before any popup is shown, not silently " +
        "persisted as a dead rule), value (required for add/update -- often a list), old_value " +
        "(update only -- the prior value being replaced; omit to add alongside the existing " +
        "value instead of replacing it).\n\n" +
        "target='grant' edits the auto_accept_grants section (one resource trusted once, " +
        "covering several operations at a time -- e.g. a Drive sandbox folder): connector " +
        "(e.g. 'drive'), config_key (e.g. 'sandbox_folders'), resource_id (required), name " +
        "(optional cosmetic label), tab (no current resource type uses this), capabilities (add/update only -- " +
        "a map of capability key, e.g. 'write', to true/false; see " +
        "privacyfence_list_auto_accept_rules' auto_accept_grants output for which capability " +
        "keys apply to which resource type).\n\n" +
        "reason: one sentence on why you're proposing this change -- logged, self-reported, " +
        "unverified, same as every other gated tool's reason param.",
      inputSchema: {
        target: z.enum(["rule", "grant"]),
        operation: z.enum(["add", "update", "remove"]),
        reason: z.string(),
        operation_key: z.string().optional(),
        rule_name: z.string().optional(),
        value: z.unknown().optional(),
        old_value: z.unknown().optional(),
        connector: z.string().optional(),
        config_key: z.string().optional(),
        resource_id: z.string().optional(),
        name: z.string().optional(),
        tab: z.string().optional(),
        capabilities: z.record(z.string(), z.boolean()).optional(),
      },
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true },
    },
    async ({
      target, operation, reason, operation_key, rule_name, value, old_value,
      connector, config_key, resource_id, name, tab, capabilities,
    }): Promise<CallToolResult> => {
      try {
        const result = await ipc.proposeRuleChange({
          target, operation, reason,
          operationKey: operation_key,
          ruleName: rule_name,
          value,
          oldValue: old_value,
          connector,
          configKey: config_key,
          resourceId: resource_id,
          name,
          tab,
          capabilities,
        });
        return toCallToolResult(result);
      } catch (exc) {
        if (exc instanceof IPCError) throw new Error(exc.message);
        throw exc;
      }
    }
  );

  server.registerTool(
    "privacyfence_begin_unattended_session",
    {
      description:
        "Tell PrivacyFence this conversation is an unattended/scheduled Cowork run (e.g. a " +
        "Routine firing on a schedule) with no human necessarily watching, for the rest of " +
        "this connection. From then on, any gated tool call that isn't already covered by a " +
        "configured auto-accept rule is denied immediately with a clear error, instead of " +
        "PrivacyFence opening a native approval dialog that nobody will answer. Call this once " +
        "at the start of a scheduled run, and pair it with privacyfence_check_policy to plan " +
        "which steps are safe to attempt. Never changes what auto-accepts, only what happens " +
        "when nothing does. Errors if an administrator hasn't enabled unattended sessions for " +
        "this install. Do not call this during a normal interactive conversation -- it makes " +
        "denials immediate instead of prompting. reason: one sentence on why this session is " +
        "unattended (e.g. the Routine/schedule that triggered it) -- logged in the audit " +
        "entry for this session change, since no popup is shown for it to appear in.",
      inputSchema: { reason: z.string() },
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true },
    },
    async ({ reason }): Promise<CallToolResult> => {
      try {
        const result = await ipc.beginUnattendedSession(reason);
        return toCallToolResult(result);
      } catch (exc) {
        if (exc instanceof IPCError) throw new Error(exc.message);
        throw exc;
      }
    }
  );

  server.registerTool(
    "privacyfence_end_unattended_session",
    {
      description:
        "Clear the unattended-session flag set by privacyfence_begin_unattended_session for " +
        "this connection, restoring normal interactive approval behavior. Call this when a " +
        "scheduled run finishes. Not strictly required -- the flag also clears automatically " +
        "when the connection closes -- but call it if this connection might be reused " +
        "afterward for something interactive. reason: one sentence on why the unattended " +
        "session is ending now -- logged the same way as privacyfence_begin_unattended_session's.",
      inputSchema: { reason: z.string() },
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true },
    },
    async ({ reason }): Promise<CallToolResult> => {
      try {
        const result = await ipc.endUnattendedSession(reason);
        return toCallToolResult(result);
      } catch (exc) {
        if (exc instanceof IPCError) throw new Error(exc.message);
        throw exc;
      }
    }
  );

  console.error(
    "Registered meta-tools: privacyfence_check_policy, privacyfence_list_auto_accept_rules, " +
      "privacyfence_propose_auto_accept_rule_change, privacyfence_begin_unattended_session, " +
      "privacyfence_end_unattended_session"
  );
}
