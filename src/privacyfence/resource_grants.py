"""Connector-scoped auto-accept grants.

Historically, trusting one resource (a Drive folder, a Google Tasks list, a
Slack channel, ...) for auto-accept meant adding the same ID separately to
every operation key that resource happens to touch — e.g. a "sandbox" Drive
folder needs the same folder ID added independently to
``drive.write_file``, ``drive.write_doc``, ``sheets.write_range``,
``sheets.add_sheet``, ``sheets.rename_sheet``, and ``sheets.format_range``,
with no "apply to all" action (see docs/TECHNICAL_REFERENCE.md's Auto-accept
rules section). That's a property of the *resource*, not of the *tool call*.

This module lets a resource be trusted once, under a new ``auto_accept_grants``
config section grouped by connector and resource type, with a small number of
capability booleans (read / write / create / ...) instead of duplicated rule
entries. ``expand_grants()`` compiles that into the exact same
``{operation_key: [{"rule": ..., "value": ...}, ...]}`` shape
``auto_accept.AutoAcceptEvaluator`` already consumes, so the evaluator itself
needs no changes and no new matching logic is introduced — this is purely a
friendlier way to author the same kind of rule entry a user could already
write by hand.

Deliberately NOT covered by this module (stay as plain ``auto_accept_rules``,
see docs/TECHNICAL_REFERENCE.md): attribute-style rules that describe a
property of the request rather than a specific resource's identity —
``trusted_sender_domain``, ``i_am_owner``, ``file_type_allowlist``,
``approved_object_types``, and similar. There's no resource identity there to
grant trust to once and reuse.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable

# ── Manifest ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GrantCapability:
    """One toggleable capability of a grant (e.g. "read auto-accept").

    ``targets`` is the list of (operation_key, rule_name) pairs that get a
    compiled rule entry when this capability is enabled on a grant entry —
    almost always one pair, but e.g. a spreadsheet's "write" capability
    spans four ``sheets.*`` operation keys, and this is exactly where the
    "apply to all" behavior comes from.
    """

    label: str
    targets: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class GrantResourceType:
    """One kind of resource that can be granted trust (a Drive folder, a
    Jira project, ...). Fully self-contained: the compiler (`expand_grants`)
    and the name resolver (`resource_names.py`) both work off this table —
    adding a new grantable resource type means adding one entry here, not
    touching the evaluator or the menu-building code.
    """

    connector: str  # top-level key in auto_accept_grants, e.g. "drive"
    config_key: str  # nested key, e.g. "folders", "task_lists"
    id_field: str  # "id" for most resources; "key" for Jira/Confluence
    label: str  # menu group label, e.g. "Trusted Folders"
    singular: str  # used in prompts, e.g. "Add folder…"
    capabilities: dict[str, GrantCapability]
    # (client, resource_id) -> display name, or None if not resolvable right
    # now. Pure duck-typing against a live connector client instance — this
    # module never imports a connector/client module itself, so it stays
    # importable without any of the optional connector dependencies
    # installed (google-api-python-client, slack_sdk, atlassian-python-api).
    resolver: Callable[[Any, str], str | None]
    # Build the rule value contributed by one grant entry. Defaults to just
    # the ID; spreadsheets override this to build the {"spreadsheet_id",
    # "tab"} pair dict `approved_spreadsheet` expects.
    value_of: Callable[[dict[str, Any]], Any] = field(
        default_factory=lambda: (lambda entry: entry)
    )
    # (client) -> [(resource_id, display_name), ...] when the connector has a
    # cheap, already-auto-gated listing call — lets the "+ Add …" menu item
    # offer a live picker by name instead of asking the user to paste an ID.
    # None for resources with no such call (Drive folders/spreadsheets: there
    # is no "list every folder I can see" API short of the heavier Google
    # Picker integration — see docs at the time this shipped) — those fall
    # back to paste-ID-or-URL entry in the menu bar.
    list_candidates: Callable[[Any], list[tuple[str, str]]] | None = None

    def id_of(self, entry: dict[str, Any]) -> str:
        return str(entry.get(self.id_field, ""))


def _plain_value_of(id_field: str) -> Callable[[dict[str, Any]], Any]:
    return lambda entry: entry.get(id_field, "")


def _spreadsheet_value_of(entry: dict[str, Any]) -> dict[str, Any]:
    value: dict[str, Any] = {"spreadsheet_id": entry.get("id", "")}
    if entry.get("tab"):
        value["tab"] = entry["tab"]
    return value


def _find_by(items: Any, attr: str, target: str) -> str | None:
    """First item's `attr` where str(item.attr) == target, else None."""
    for item in items or []:
        if str(getattr(item, attr, "")) == target:
            return getattr(item, "name", None) or getattr(item, "title", None) or getattr(item, "summary", None)
    return None


def _resolve_drive_file(client: Any, resource_id: str) -> str | None:
    try:
        return client.get_file_metadata(resource_id).name or None
    except Exception:
        return None


def _resolve_task_list(client: Any, resource_id: str) -> str | None:
    try:
        return _find_by(client.list_task_lists(), "id", resource_id)
    except Exception:
        return None


def _resolve_slack_channel(client: Any, resource_id: str) -> str | None:
    try:
        name = _find_by(client.list_channels(), "id", resource_id)
        return f"#{name}" if name else None
    except Exception:
        return None


def _resolve_telegram_chat(client: Any, resource_id: str) -> str | None:
    try:
        import asyncio

        chats = asyncio.run(client.list_chats())
        return _find_by(chats, "id", resource_id)
    except Exception:
        return None


def _resolve_jira_project(client: Any, resource_id: str) -> str | None:
    try:
        return _find_by(client.list_projects(), "key", resource_id)
    except Exception:
        return None


def _resolve_confluence_space(client: Any, resource_id: str) -> str | None:
    try:
        return _find_by(client.list_spaces(), "key", resource_id)
    except Exception:
        return None


def _resolve_calendar(client: Any, resource_id: str) -> str | None:
    try:
        return _find_by(client.list_calendars(), "id", resource_id)
    except Exception:
        return None


def _resolve_salesforce_report(client: Any, resource_id: str) -> str | None:
    try:
        return _find_by(client.list_reports(), "id", resource_id)
    except Exception:
        return None


def _list_task_lists(client: Any) -> list[tuple[str, str]]:
    return [(tl.id, tl.title) for tl in client.list_task_lists()]


def _list_slack_channels(client: Any) -> list[tuple[str, str]]:
    return [(c.id, f"#{c.name}") for c in client.list_channels()]


def _list_telegram_chats(client: Any) -> list[tuple[str, str]]:
    import asyncio

    return [(str(c.id), c.name) for c in asyncio.run(client.list_chats())]


def _list_jira_projects(client: Any) -> list[tuple[str, str]]:
    return [(p.key, f"{p.key} — {p.name}") for p in client.list_projects()]


def _list_confluence_spaces(client: Any) -> list[tuple[str, str]]:
    return [(s.key, f"{s.key} — {s.name}") for s in client.list_spaces()]


def _list_calendars(client: Any) -> list[tuple[str, str]]:
    return [(c.id, c.summary) for c in client.list_calendars()]


def _list_salesforce_reports(client: Any) -> list[tuple[str, str]]:
    return [(r.id, r.name) for r in client.list_reports()]


GRANT_RESOURCE_TYPES: tuple[GrantResourceType, ...] = (
    GrantResourceType(
        connector="drive", config_key="folders", id_field="id",
        label="Trusted Folders", singular="folder",
        capabilities={
            "read": GrantCapability("Read auto-accept", (
                ("drive.read_file_contents", "approved_folder"),
                ("drive.download_file", "approved_folder"),
                ("sheets.read_values", "approved_folder"),
            )),
        },
        resolver=_resolve_drive_file,
        value_of=_plain_value_of("id"),
    ),
    GrantResourceType(
        connector="drive", config_key="sandbox_folders", id_field="id",
        label="Sandbox Folders", singular="folder",
        capabilities={
            "write": GrantCapability("Write auto-accept", (
                ("drive.write_file", "approved_sandbox_folder"),
                ("drive.write_doc", "approved_sandbox_folder"),
                ("sheets.write_range", "approved_sandbox_folder"),
                ("sheets.add_sheet", "approved_sandbox_folder"),
                ("sheets.rename_sheet", "approved_sandbox_folder"),
                ("sheets.format_range", "approved_sandbox_folder"),
                ("sheets.insert_dimensions", "approved_sandbox_folder"),
                ("sheets.delete_dimensions", "approved_sandbox_folder"),
                ("docs.edit_content", "approved_sandbox_folder"),
                ("docs.format_content", "approved_sandbox_folder"),
            )),
        },
        resolver=_resolve_drive_file,
        value_of=_plain_value_of("id"),
    ),
    GrantResourceType(
        connector="drive", config_key="spreadsheets", id_field="id",
        label="Trusted Spreadsheets", singular="spreadsheet",
        capabilities={
            "read": GrantCapability("Read auto-accept", (
                ("sheets.read_values", "approved_spreadsheet"),
            )),
            "write": GrantCapability("Write auto-accept", (
                ("sheets.write_range", "approved_spreadsheet"),
                ("sheets.add_sheet", "approved_spreadsheet"),
                ("sheets.rename_sheet", "approved_spreadsheet"),
                ("sheets.format_range", "approved_spreadsheet"),
                ("sheets.insert_dimensions", "approved_spreadsheet"),
                ("sheets.delete_dimensions", "approved_spreadsheet"),
            )),
        },
        resolver=_resolve_drive_file,
        value_of=_spreadsheet_value_of,
    ),
    GrantResourceType(
        connector="tasks", config_key="task_lists", id_field="id",
        label="Trusted Task Lists", singular="task list",
        capabilities={
            "create": GrantCapability("Auto-accept new tasks", (
                ("tasks.create_task", "approved_task_list"),
            )),
            "edit": GrantCapability("Auto-accept edits", (
                ("tasks.update_task", "approved_task_list"),
            )),
            "complete": GrantCapability("Auto-accept complete/uncomplete", (
                ("tasks.complete_task", "approved_task_list"),
                ("tasks.uncomplete_task", "approved_task_list"),
            )),
            "move": GrantCapability("Auto-accept moves", (
                ("tasks.move_task", "approved_task_list"),
            )),
        },
        resolver=_resolve_task_list,
        value_of=_plain_value_of("id"),
        list_candidates=_list_task_lists,
    ),
    GrantResourceType(
        connector="slack", config_key="channels", id_field="id",
        label="Trusted Channels", singular="channel",
        capabilities={
            "read": GrantCapability("Read auto-accept", (
                ("slack.read_messages", "approved_channel"),
            )),
            "send": GrantCapability("Send auto-accept", (
                ("slack.send_message", "approved_recipient"),
            )),
        },
        resolver=_resolve_slack_channel,
        value_of=_plain_value_of("id"),
        list_candidates=_list_slack_channels,
    ),
    GrantResourceType(
        connector="telegram", config_key="chats", id_field="id",
        label="Trusted Chats", singular="chat",
        capabilities={
            "read": GrantCapability("Read auto-accept", (
                ("telegram.read_chat_messages", "approved_chats"),
            )),
            "send": GrantCapability("Send auto-accept", (
                ("telegram.send_message", "approved_chats"),
            )),
        },
        resolver=_resolve_telegram_chat,
        value_of=_plain_value_of("id"),
        list_candidates=_list_telegram_chats,
    ),
    GrantResourceType(
        connector="jira", config_key="projects", id_field="key",
        label="Trusted Projects", singular="project",
        capabilities={
            "read": GrantCapability("Read auto-accept", (
                ("jira.read_issue", "approved_project_keys"),
            )),
            "create": GrantCapability("Auto-accept new issues", (
                ("jira.create_issue", "approved_project_keys"),
            )),
            "comment": GrantCapability("Auto-accept comments", (
                ("jira.add_comment", "approved_project_keys"),
            )),
            "update": GrantCapability("Auto-accept updates", (
                ("jira.update_issue", "approved_project_keys"),
            )),
            "transition": GrantCapability("Auto-accept transitions", (
                ("jira.transition_issue", "approved_project_keys"),
            )),
        },
        resolver=_resolve_jira_project,
        value_of=_plain_value_of("key"),
        list_candidates=_list_jira_projects,
    ),
    GrantResourceType(
        connector="confluence", config_key="spaces", id_field="key",
        label="Trusted Spaces", singular="space",
        capabilities={
            "read": GrantCapability("Read auto-accept", (
                ("confluence.read_page", "approved_space_keys"),
            )),
            "create": GrantCapability("Auto-accept new pages", (
                ("confluence.create_page", "approved_space_keys"),
            )),
            "update": GrantCapability("Auto-accept updates", (
                ("confluence.update_page", "approved_space_keys"),
            )),
        },
        resolver=_resolve_confluence_space,
        value_of=_plain_value_of("key"),
        list_candidates=_list_confluence_spaces,
    ),
    GrantResourceType(
        connector="calendar", config_key="calendars", id_field="id",
        label="Trusted Calendars", singular="calendar",
        capabilities={
            "read": GrantCapability("Read auto-accept", (
                ("calendar.read_event_details", "personal_calendar"),
            )),
            "write": GrantCapability("Create/modify auto-accept", (
                ("calendar.create_modify_event", "personal_calendar"),
                ("calendar.set_visibility", "personal_calendar"),
            )),
        },
        resolver=_resolve_calendar,
        value_of=_plain_value_of("id"),
        list_candidates=_list_calendars,
    ),
    GrantResourceType(
        connector="salesforce", config_key="reports", id_field="id",
        label="Trusted Reports", singular="report",
        capabilities={
            "run": GrantCapability("Read auto-accept", (
                ("salesforce.run_report", "approved_report_ids"),
            )),
        },
        resolver=_resolve_salesforce_report,
        value_of=_plain_value_of("id"),
        list_candidates=_list_salesforce_reports,
    ),
)

_BY_CONNECTOR_AND_KEY: dict[tuple[str, str], GrantResourceType] = {
    (rt.connector, rt.config_key): rt for rt in GRANT_RESOURCE_TYPES
}


def resource_type(connector: str, config_key: str) -> GrantResourceType | None:
    return _BY_CONNECTOR_AND_KEY.get((connector, config_key))


def resource_types_for_connector(connector: str) -> list[GrantResourceType]:
    return [rt for rt in GRANT_RESOURCE_TYPES if rt.connector == connector]


# ── Config access helpers ────────────────────────────────────────────────


def get_grant_entries(grants_cfg: dict[str, Any], rt: GrantResourceType) -> list[dict[str, Any]]:
    return list((grants_cfg.get(rt.connector) or {}).get(rt.config_key) or [])


def set_grant_entries(grants_cfg: dict[str, Any], rt: GrantResourceType, entries: list[dict[str, Any]]) -> None:
    connector_cfg = grants_cfg.setdefault(rt.connector, {})
    if entries:
        connector_cfg[rt.config_key] = entries
    else:
        connector_cfg.pop(rt.config_key, None)
        if not connector_cfg:
            grants_cfg.pop(rt.connector, None)


def _grant_matches(rt: GrantResourceType, entry: dict[str, Any], resource_id: str, tab: str | None) -> bool:
    if rt.id_of(entry) != resource_id:
        return False
    return rt.config_key != "spreadsheets" or entry.get("tab") == (tab or None)


def apply_grant_upsert(
    cfg: dict[str, Any], rt: GrantResourceType, resource_id: str, *,
    name: str | None = None, tab: str | None = None,
    capabilities: dict[str, bool] | None = None,
) -> bool:
    """Add a new grant entry, or update an existing one's cosmetic name
    and/or capability flags (matched by id, plus tab for spreadsheets).
    Mutates `cfg["auto_accept_grants"]` in place. Always returns True --
    see auto_accept.mutate_grants -- since even re-confirming the same
    capabilities is a proposal that already went through a human
    confirmation, not a no-op worth silently skipping the write for.

    Shared by menu_bar.py's "+ Add <resource>…" flow and gate.py's
    propose_rule_change() (the bridge-facing counterpart with a
    confirmation popup instead of a menu prompt) -- one place owns what a
    grant entry looks like on disk.
    """
    grants_cfg = cfg.setdefault("auto_accept_grants", {})
    entries = get_grant_entries(grants_cfg, rt)
    existing = next((e for e in entries if _grant_matches(rt, e, resource_id, tab)), None)
    if existing is None:
        existing = {rt.id_field: resource_id}
        if tab:
            existing["tab"] = tab
        entries.append(existing)
    if name:
        existing["name"] = name
    for key, enabled in (capabilities or {}).items():
        if key not in rt.capabilities:
            raise ValueError(f"Unknown capability {key!r} for {rt.connector}.{rt.config_key}")
        existing[key] = bool(enabled)
    set_grant_entries(grants_cfg, rt, entries)
    return True


def apply_grant_removal(
    cfg: dict[str, Any], rt: GrantResourceType, resource_id: str, tab: str | None = None
) -> bool:
    """Remove a grant entry (matched by id, plus tab for spreadsheets).
    Mutates `cfg["auto_accept_grants"]` in place. Returns False (no write
    needed) if no entry matched."""
    grants_cfg = cfg.setdefault("auto_accept_grants", {})
    entries = get_grant_entries(grants_cfg, rt)
    remaining = [e for e in entries if not _grant_matches(rt, e, resource_id, tab)]
    if len(remaining) == len(entries):
        return False
    set_grant_entries(grants_cfg, rt, remaining)
    return True


def describe_grant_change(
    operation: str, rt: GrantResourceType, resource_id: str, *,
    name: str | None = None, tab: str | None = None, capabilities: dict[str, bool] | None = None,
) -> str:
    """Human-readable description of a bridge-proposed add/update/remove to
    an auto_accept_grants entry, shown in gate.propose_rule_change()'s
    confirmation popup."""
    label = name or resource_id
    target_desc = f"{rt.singular} {label!r}" + (f" (tab: {tab})" if tab else "")
    if operation == "remove":
        return f"Remove {target_desc} from {rt.label}"
    enabled_labels = [
        rt.capabilities[key].label for key, enabled in (capabilities or {}).items()
        if enabled and key in rt.capabilities
    ]
    cap_str = ", ".join(enabled_labels) if enabled_labels else "no capabilities enabled"
    verb = "Add" if operation == "add" else "Update"
    return f"{verb} {target_desc} in {rt.label} ({cap_str})"


# ── Compiler: grants -> legacy per-operation rule shape ─────────────────


def expand_grants(grants_cfg: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Compile ``auto_accept_grants`` into the ``{operation_key: [{"rule",
    "value"}, ...]}`` shape ``AutoAcceptEvaluator`` already understands.

    One compiled rule entry is produced per (operation_key, rule_name) pair
    that at least one enabled capability targets; its value is the list of
    every grant entry's contributed value for that capability, deduplicated
    and order-preserving. Marked ``"_grant": True`` so callers (the menu UI)
    can tell a compiled entry apart from one hand-written under
    ``auto_accept_rules`` — the evaluator itself ignores the extra key.
    """
    buckets: dict[tuple[str, str], list[Any]] = {}
    for rt in GRANT_RESOURCE_TYPES:
        entries = get_grant_entries(grants_cfg, rt)
        if not entries:
            continue
        for capability_key, capability in rt.capabilities.items():
            values = []
            for entry in entries:
                if not entry.get(capability_key):
                    continue
                value = rt.value_of(entry)
                if value not in values:
                    values.append(value)
            if not values:
                continue
            for op_key, rule_name in capability.targets:
                bucket = buckets.setdefault((op_key, rule_name), [])
                for value in values:
                    if value not in bucket:
                        bucket.append(value)

    compiled: dict[str, list[dict[str, Any]]] = {}
    for (op_key, rule_name), values in buckets.items():
        compiled.setdefault(op_key, []).append(
            {"rule": rule_name, "value": values, "_grant": True}
        )
    return compiled


def build_effective_rules(cfg: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """The rule set actually handed to ``AutoAcceptEvaluator``: everything
    under ``auto_accept_rules`` (the advanced/manual layer, untouched) plus
    everything ``auto_accept_grants`` compiles to. Grants are appended after
    manual rules; order doesn't affect matching (``should_auto_accept``
    checks every rule in the list regardless of position).
    """
    rules: dict[str, list[dict[str, Any]]] = {
        op_key: [dict(r) for r in op_rules]
        for op_key, op_rules in (cfg.get("auto_accept_rules") or {}).items()
    }
    for op_key, entries in expand_grants(cfg.get("auto_accept_grants") or {}).items():
        rules.setdefault(op_key, []).extend(entries)
    return rules


# ── One-time migration: fold matching auto_accept_rules into grants ─────

MIGRATION_MARKER = "migrated_to_grants_v1"


def _values_equal(a: Any, b: Any) -> bool:
    """Order-independent equality for rule values (plain strings or the
    spreadsheet pair-dict shape), matching how the evaluator itself treats
    a rule's value as an unordered set."""
    if isinstance(a, list) and isinstance(b, list):
        def _key(v: Any) -> Any:
            return tuple(sorted(v.items())) if isinstance(v, dict) else v
        return sorted(map(_key, a), key=repr) == sorted(map(_key, b), key=repr)
    return a == b


def migrate_rules_to_grants(cfg: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Fold `auto_accept_rules` entries into `auto_accept_grants` wherever a
    resource type's capability is configured identically across every one of
    its target operation keys — i.e. exactly what a user would already get
    by hand-duplicating the same rule value everywhere it applies. Idempotent
    (checks/sets MIGRATION_MARKER) and never runs twice.

    A *partial* match (the value present on some but not all target
    operation keys) is deliberately left alone rather than migrated — folding
    it in would silently extend auto-accept to operation keys the user never
    configured, which this function must never do (see design rationale in
    docs/designs at the time this was written, since removed post-ship).
    Returns the updated config and a human-readable summary of what moved,
    for a one-time log line.
    """
    if cfg.get(MIGRATION_MARKER):
        return cfg, []

    cfg = deepcopy(cfg)
    rules_cfg: dict[str, list[dict[str, Any]]] = cfg.get("auto_accept_rules") or {}
    grants_cfg: dict[str, Any] = cfg.setdefault("auto_accept_grants", {})
    summary: list[str] = []

    for rt in GRANT_RESOURCE_TYPES:
        for capability_key, capability in rt.capabilities.items():
            op_keys = [op_key for op_key, _rule_name in capability.targets]
            if not op_keys:
                continue

            def _entry_for(op_key: str, rule_name: str) -> dict[str, Any] | None:
                for r in rules_cfg.get(op_key, []) or []:
                    if r.get("rule") == rule_name:
                        return r
                return None

            first_op_key, first_rule_name = capability.targets[0]
            first = _entry_for(first_op_key, first_rule_name)
            if not first or not first.get("value"):
                continue
            value = first["value"]

            all_match = all(
                (m := _entry_for(op_key, rule_name)) is not None and _values_equal(m.get("value"), value)
                for op_key, rule_name in capability.targets
            )
            if not all_match:
                continue

            values = value if isinstance(value, list) else [value]
            entries = get_grant_entries(grants_cfg, rt)
            by_id = {rt.id_of(e): e for e in entries}
            for raw in values:
                if isinstance(raw, dict):
                    resource_id = raw.get("spreadsheet_id", "") if rt.config_key == "spreadsheets" else ""
                    extra = {"tab": raw["tab"]} if raw.get("tab") else {}
                else:
                    resource_id = str(raw)
                    extra = {}
                if not resource_id:
                    continue
                entry = by_id.get(resource_id)
                if entry is None:
                    entry = {rt.id_field: resource_id, **extra}
                    entries.append(entry)
                    by_id[resource_id] = entry
                entry[capability_key] = True
            set_grant_entries(grants_cfg, rt, entries)

            for op_key, rule_name in capability.targets:
                rules_cfg[op_key] = [r for r in rules_cfg.get(op_key, []) if r.get("rule") != rule_name]
                if not rules_cfg[op_key]:
                    rules_cfg.pop(op_key, None)

            summary.append(
                f"{rt.connector}.{rt.config_key} [{capability_key}]: "
                f"migrated {len(values)} value(s) from {', '.join(op_keys)}"
            )

    if rules_cfg:
        cfg["auto_accept_rules"] = rules_cfg
    else:
        cfg.pop("auto_accept_rules", None)
    if not grants_cfg:
        cfg.pop("auto_accept_grants", None)
    cfg[MIGRATION_MARKER] = True
    return cfg, summary
