# Design: Connector-scoped auto-accept grants & menu bar redesign

**Status:** Draft — design only, not implemented.
**Scope:** `src/privacyfence/auto_accept.py`, `src/privacyfence/menu_bar.py`,
`config/settings.yaml`, `docs/TECHNICAL_REFERENCE.md`.
**Non-scope:** `gate.py`, PII detection gate, `review`/`popup` gate assignment per tool, the
Accept-All / Accept-for-5-min mechanics. See [Security considerations](#7-security-considerations)
for why these are deliberately left alone.

---

## 1. Goals

This redesign targets three usability problems in the current auto-accept setup, reported
directly against the shipped v1.0 UI:

1. **Simplify setup.** A "sandbox folder" or "accepted task list" is a property of a *resource*
   (a folder, a task list, a channel), not of a *tool call*. Today the user must configure the
   same folder ID separately for every operation that touches it — there is no "apply to all"
   action. Setting up trust for one resource across read + write should be one action, not five.
2. **Improve the UI.** Drop the current pattern of pasting multiple IDs into one multi-line text
   box in a single window. Replace it with individual **+ Add** / **✕ Remove** menu items, one
   per resource.
3. **Show names, not IDs.** Editing by ID is fine as the underlying storage format, but the menu
   should display the resource's real name (folder name, task list name, channel name, etc.),
   not the opaque ID string.

---

## 2. Current state (what exists today)

### 2.1 Config: one rule list per operation key

`auto_accept_rules` in `settings.yaml` is keyed by **operation key** (`drive.write_file`,
`sheets.write_range`, `sheets.rename_sheet`, …), each holding a list of `{rule, value}` entries.
`AutoAcceptEvaluator.should_auto_accept()` (`auto_accept.py`) looks up the operation key for the
current tool call (via `TOOL_TO_OPERATION`) and evaluates only that key's rule list.

The problem this design addresses is stated plainly in the current docs
(`docs/TECHNICAL_REFERENCE.md`, Auto-accept rules § Google Drive):

> "each of these five operation keys needs the same folder ID (or other rule value) added to it
> separately... there's no 'apply to all' action, each is configured independently via its own
> menu entry"

This isn't a one-off wart — it's structural. The same shape repeats for:

| Resource | Rule name(s) | Operation keys that must each be configured separately |
|---|---|---|
| Drive folder (read) | `approved_folder` | `drive.read_file_contents`, `drive.download_file`, `sheets.read_values` |
| Drive folder (sandbox/write) | `approved_sandbox_folder` | `drive.write_file`, `drive.write_doc`, `sheets.write_range`, `sheets.add_sheet`, `sheets.rename_sheet`, `sheets.format_range` |
| Spreadsheet | `approved_spreadsheet` | `sheets.read_values`, `sheets.write_range`, `sheets.add_sheet`, `sheets.rename_sheet`, `sheets.format_range` |
| Task list | `approved_task_list` | `tasks.create_task`, `tasks.update_task`, `tasks.complete_task`, `tasks.uncomplete_task`, `tasks.move_task` |
| Slack channel | `approved_channel` / `approved_recipient` | `slack.read_messages`, `slack.send_message` |
| Telegram chat | `approved_chats` | `telegram.read_chat_messages`, `telegram.send_message` |
| Jira project | `approved_project_keys` | `jira.read_issue`, `jira.create_issue`, `jira.add_comment`, `jira.update_issue`, `jira.transition_issue` |
| Confluence space | `approved_space_keys` | `confluence.read_page`, `confluence.create_page`, `confluence.update_page` |
| Calendar | `personal_calendar` | `calendar.read_event_details`, `calendar.create_modify_event` |

For every row above, trusting one resource today means opening the menu N separate times and
re-pasting the same ID into N separate rule editors.

### 2.2 UI: nested rumps menus + one shared multi-line text box

`menu_bar.py::_build_operation_menu()` renders, per operation key: **+ Add rule…** → a native
list picker (`_osascript_pick`) to choose a rule name → for rules with a value
(`RULES_LIST_VALUE` / `RULES_PAIR_VALUE` / `RULES_INT_VALUE`), a single `rumps.Window` text box
where **all current values live together on separate lines** (`_edit_rule_value`). Adding one
folder ID means: open Edit…, see the existing IDs already in the box, add a new line, save the
whole box back as one `yaml.dump`. Removing one folder ID means the same, done by hand-editing
the text. There is a separate `✕ Remove` item, but it removes the **entire rule** (all values for
that operation), not one value.

### 2.3 IDs shown verbatim

`RULE_HINTS` (`menu_bar.py`) shows example IDs as placeholder text (e.g.
`1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms`) and `_format_value()` previews a rule's current
value by printing the raw IDs, comma-joined, truncated to 3 with a "+N more" suffix. There is no
name lookup anywhere in `menu_bar.py` today — the user has to keep track of which Drive folder ID
is which folder themselves, usually by keeping the "Get Info" URL open in another window.

---

## 3. Proposed design

Three additions, layered so the security-critical evaluation path in `auto_accept.py` does not
change:

1. **`auto_accept_grants`** — a new, higher-level config section, grouped by connector and
   resource type, with per-capability booleans instead of duplicated rule entries.
2. **A grant-expansion compiler** that turns `auto_accept_grants` into the exact same
   `{operation_key: [{rule, value}, ...]}` shape `AutoAcceptEvaluator` already consumes — so the
   evaluator itself needs zero changes.
3. **A name-resolution layer + rebuilt menu tree** that displays resolved names, adds resources
   one at a time via a native picker (or paste-and-confirm where no listing API exists), and
   removes them one at a time — no shared multi-line text box.

The existing `auto_accept_rules` key keeps working, unmodified, as the **advanced / escape-hatch**
layer for rules that aren't resource grants at all (see §3.1.2). Nothing written by hand into
`auto_accept_rules` today breaks.

### 3.1 Config model

#### 3.1.1 `auto_accept_grants` — resource-level trust

```yaml
auto_accept_grants:
  drive:
    folders:
      - id: "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
        # name is a cosmetic cache of the last resolved folder name — informational
        # only, never read by the evaluator, refreshed on menu rebuild.
        name: "Q3 Reports"
        read: true
        write: false
    sandbox_folders:
      - id: "1CdeFghIJKLmnoPQRstuVWxyz0123456789AbCdEfGh"
        name: "Claude scratch space"
        write: true
    spreadsheets:
      - id: "1FghIJKLmnoPQRstuVWxyz0123456789AbCdEfGhIjK"
        name: "Team Budget 2026"
        tab: "Q3"           # omit for "every tab"
        read: true
        write: true

  tasks:
    task_lists:
      - id: "MDAwMDAwMDAwMDAwMDAwMDAwMDA6MDow"
        name: "Personal"
        create: true
        edit: true
        complete: true
        move: true

  slack:
    channels:
      - id: "C0123456789"
        name: "#team-ops"
        read: true
        send: false

  telegram:
    chats:
      - id: "-100987654321"
        name: "Ops group"
        read: true
        send: true

  jira:
    projects:
      - key: "MYPROJ"       # Jira/Confluence resources are already named by key, no id/name split
        read: true
        create: true
        comment: true
        update: true
        transition: false

  confluence:
    spaces:
      - key: "TEAM"
        read: true
        create: false
        update: false

  calendar:
    calendars:
      - id: "primary"
        name: "Andras (primary)"
        read: true
        write: true
```

This is additive config, not a replacement for every existing rule — see §3.1.2 for what stays
out of it.

#### 3.1.2 What stays as plain `auto_accept_rules` (not a grant)

Grants model *"I trust this specific resource."* They deliberately do **not** cover rules that
describe an attribute of the request rather than a specific resource identity — those have no ID
to resolve a name for for, and folding them into the grants schema would just be renaming the same
problem:

- Gmail: `trusted_sender_domain`, `label_match`, `age_threshold_days`, `no_attachments`,
  `i_am_sender`, `i_am_sole_recipient`, `to_is_myself`, `approved_recipient_domain`,
  `label_name_allowlist`
- Drive: `i_am_owner`, `created_by_me`, `created_this_session`, `file_type_allowlist`,
  `shared_drive_exclusion`, `move_within_approved_folders`, `parent_folder_allowlist`
- Slack: `dm_with_myself`, `send_to_myself`, `public_channels_only`, `no_file_attachments`,
  `reply_in_existing_thread`
- Calendar: `i_am_organizer`, `no_external_attendees`, `past_event`, `time_window_days`,
  `no_conferencing_link`
- Contacts: `no_contact_info_change`
- Salesforce: `approved_object_types` (a small fixed vocabulary, not an ID), `approved_report_ids`
  (see §3.5 — this one *does* get name resolution, via `salesforce_list_reports`, even though it
  stays in the "filter list" UI category rather than becoming a full grant, since a report has no
  read/write duality to collapse)
- Telegram: `no_media_attachments`
- Jira/Confluence: none beyond what's already a grant above

These remain in `auto_accept_rules`, edited through the **Filters** menu (§3.6.2), which gets the
same add/remove-one-at-a-time UI treatment as grants (goal 2) even though it carries no name
resolution (goal 3 doesn't apply — there's no resource identity to resolve).

`parent_folder_allowlist` (`drive.upload_file`) and `move_within_approved_folders`
(`drive.move_file`) are folder-ID lists too, and *do* get name resolution — they're listed here
because they aren't folded into the `folders`/`sandbox_folders` grant (upload-destination and
move-both-ends are distinct enough semantics from "read this folder" / "write into this sandbox"
that collapsing them in would misrepresent what the checkbox grants). They're presented as their
own rows under **Drive → Filters** with resolved names, just not merged into the `folders` grant
UI.

### 3.2 Resource manifest (compiler's source of truth)

A new module, `resource_grants.py`, declares one `GrantResourceType` per row of §2.1's table:

```python
@dataclass
class GrantResourceType:
    connector: str                # "drive", "tasks", "slack", ...
    key: str                      # "folders", "sandbox_folders", "task_lists", ...
    id_field: str                 # "id" or "key" (Jira/Confluence use "key")
    capabilities: dict[str, list[tuple[str, str]]]
    # capability name -> [(operation_key, rule_name), ...] to emit when that
    # capability is true. Most capabilities map to exactly one operation key;
    # spreadsheets' "write" maps to four (write_range/add_sheet/rename_sheet/format_range).
    resolver: Callable[[Connector, str], str | None]
    # (connector_instance, resource_id) -> display name, or None if not resolvable
    # right now (not authenticated, deleted, transient error).
    picker: PickerStrategy        # see §3.5
```

Example entries (abridged):

```python
GrantResourceType(
    connector="drive", key="folders", id_field="id",
    capabilities={
        "read": [("drive.read_file_contents", "approved_folder"),
                  ("drive.download_file", "approved_folder"),
                  ("sheets.read_values", "approved_folder")],
    },
    resolver=lambda drive, fid: drive.get_file_metadata(fid).name,
    picker=PasteIdOrUrl(url_pattern=DRIVE_FOLDER_URL_RE),
)

GrantResourceType(
    connector="tasks", key="task_lists", id_field="id",
    capabilities={
        "create":     [("tasks.create_task", "approved_task_list")],
        "edit":       [("tasks.update_task", "approved_task_list")],
        "complete":   [("tasks.complete_task", "approved_task_list"),
                        ("tasks.uncomplete_task", "approved_task_list")],
        "move":       [("tasks.move_task", "approved_task_list")],
    },
    resolver=lambda tasks, lid: next(
        (tl.title for tl in tasks.list_task_lists() if tl.id == lid), None
    ),
    picker=LiveList(lambda tasks: [(tl.id, tl.title) for tl in tasks.list_task_lists()]),
)
```

This table is the single place that encodes "which operation keys does trusting this resource
actually affect" — adding a new grantable resource type in the future means adding one entry
here, not touching the evaluator or the menu code.

### 3.3 Grant → rule compiler

A pure function, `expand_grants(grants_cfg: dict) -> dict[str, list[dict]]`, walks
`auto_accept_grants` against the manifest and produces the legacy per-operation-key rule-list
shape. It **appends to**, rather than replaces, whatever the user still has under
`auto_accept_rules` — the two merge at load time:

```python
def build_effective_rules(cfg: dict) -> dict[str, list[dict]]:
    rules = deepcopy(cfg.get("auto_accept_rules", {}) or {})
    for op_key, rule_name, value in expand_grants(cfg.get("auto_accept_grants", {}) or {}):
        rules.setdefault(op_key, []).append({"rule": rule_name, "value": value, "_grant": True})
    return rules
```

`_reload_rules()` in `menu_bar.py` and the daemon's config loader call `build_effective_rules()`
instead of reading `auto_accept_rules` directly, then pass the result into
`AutoAcceptEvaluator`/`reload_rules()` exactly as today. **`auto_accept.py` itself does not
change** — same dataclass, same `should_auto_accept()`, same `_evaluate()` dispatch, same test
suite (`tests/unit/test_auto_accept.py`) continues to pass unmodified against the expanded output.
The `_grant: true` marker on compiled entries is informational only (lets the menu code know "this
came from a grant, don't offer to edit it as a raw rule"); the evaluator ignores unknown dict keys
already, so it's a no-op for evaluation.

For the multi-operation-key capabilities (spreadsheet `write` → four operation keys, task-list
`complete` → two), one grant produces multiple compiled rule entries — this is exactly the
"apply to all" behavior goal 1 asks for, achieved without adding a from-scratch parallel
evaluator.

### 3.4 Name resolution

New module `resource_names.py`:

- `resolve(connector_name: str, resource_type_key: str, resource_id: str, live_connectors: dict) -> str | None`
  looks up the `GrantResourceType.resolver` and calls it against the live connector instance if
  one is currently authenticated; returns `None` (not an exception) on any failure — not
  authenticated, network error, resource deleted, insufficient scope.
- An in-memory cache, `{(connector, key, id): (name, resolved_at)}`, with a short TTL (e.g. 15
  minutes) — cheap enough that `_rebuild()` can call it on every rebuild without hammering the
  connector APIs, since `_rebuild()` already runs synchronously on config/connector changes today.
- A small on-disk cache, `~/.privacyfence/resource_name_cache.json` (or the dev equivalent under
  `data_dir()`), written whenever a resolution succeeds. This is what makes a name available
  **immediately** on menu build even before any live refresh completes (e.g. right after daemon
  restart, before the user has re-authenticated a connector this session) — the on-disk `name`
  written into `auto_accept_grants` itself (§3.1.1) serves the same purpose for the config file
  specifically, so a user reading `settings.yaml` by hand also sees a name, not just an ID.
- Because network calls must never block `_rebuild()` (main thread, AppKit), resolution follows
  the existing `_run_async()` pattern (`menu_bar.py`): the menu renders immediately using
  cached/last-known names (or the raw ID, tagged "resolving…", if no cache entry exists yet), and
  a background fetch calls `AppHelper.callAfter(self._rebuild)` once new names arrive — the same
  mechanism `_on_rules_changed()` already uses.

Display states for a grant row's label:

| State | Label shown |
|---|---|
| Cached name available | `Q3 Reports` |
| No cache yet, resolving | `1BxiMVs0…OgVE2upms (resolving…)` |
| Resolution failed, connector not authenticated | `1BxiMVs0…OgVE2upms (name unavailable — connect Drive)` |
| Resolution failed, resource gone/no access | `1BxiMVs0…OgVE2upms (not found — may have been deleted or unshared)` |

The ID is never hidden — it's always available (truncated) as a fallback and in full via the
"Copy ID" action (§3.6.1), since ID is still the ground-truth value written to config and the
thing a user might need to cross-reference against a URL.

### 3.5 Adding a resource: picker strategies

`GrantResourceType.picker` is one of two strategies, chosen per resource type based on whether the
connector's API can cheaply enumerate the resource:

**`LiveList`** — connector already exposes a cheap, small, unconditionally-auto-accepted listing
call. Reuses the existing `_osascript_pick()` native list picker (already used for rule-name
selection and Atlassian site selection), fed `"{name} ({id})"` strings so the user picks by name
directly and never has to know the ID at all:

- Slack channels — `slack_list_channels` (`auto` gate already)
- Google Tasks lists — `tasks_list_task_lists` (`auto`)
- Jira projects — `jira_list_projects` (`auto`)
- Confluence spaces — `confluence_list_spaces` (`auto`)
- Telegram chats — `telegram_list_chats` (`auto`)
- Calendars — `calendar_list_calendars` (`auto`)
- Salesforce report IDs — `salesforce_list_reports` (`auto`)

**`PasteIdOrUrl`** — no cheap enumeration exists (Drive can have tens of thousands of files/folders
across shared drives; there's no "list all folders I can see" call, and building one would be its
own project — see §8 Google Picker API). The **+ Add…** prompt accepts either a bare ID or a full
Drive/Sheets URL and extracts the ID via regex (`.../folders/<id>`, `.../d/<id>/edit`) so a user
can literally copy-paste the browser address bar instead of hand-extracting the ID segment — a
small but real UX win over today's raw-ID-only text box. Immediately after paste, the daemon
resolves and shows the name back to the user for confirmation *before* the grant is saved
("Add 'Q3 Reports' as a trusted folder?") — this catches paste mistakes and satisfies goal 3 at
the moment of entry, not just in the ongoing menu display.

- Drive folders (`folders`, `sandbox_folders`)
- Spreadsheets (`spreadsheets`)

Salesforce object types keep a third, degenerate strategy — a **fixed list** (`Account`, `Contact`,
`Lead`, `Opportunity`, `Case`, …, plus "Custom…" free text) via the same `_osascript_pick`, since
object types are a small closed vocabulary rather than something to enumerate from an API or paste
a URL for.

### 3.6 Menu bar UI redesign

#### 3.6.1 Grant resource menus

Old tree (today):

```
Auto-accept Rules
└─ Drive
   └─ Write file
      ├─ + Add rule…
      ├─ ✓ approved_sandbox_folder
      │      ↳ 1Bxi…OgVE2, 1Cde…AbCd  Edit…      <- multi-line text box, all IDs together
      │      ✕ Remove                             <- removes the WHOLE rule, all IDs
      └─ ✓ i_am_owner
             ✕ Remove
```

New tree:

```
Auto-accept Rules
└─ Drive
   ├─ Trusted Folders
   │  ├─ Q3 Reports                     <- resolved name, own submenu
   │  │  ├─ ☑ Read auto-accept
   │  │  ├─ ☐ Write auto-accept (sandbox)
   │  │  ├─ Copy ID (1BxiMVs0…OgVE2upms)
   │  │  └─ ✕ Remove
   │  ├─ Claude scratch space
   │  │  ├─ ☐ Read auto-accept
   │  │  ├─ ☑ Write auto-accept (sandbox)
   │  │  ├─ Copy ID (1CdeFghIJK…AbCdEfGh)
   │  │  └─ ✕ Remove
   │  └─ + Add folder…                  <- one new grant at a time, name resolved before save
   ├─ Trusted Spreadsheets
   │  ├─ Team Budget 2026 — Q3
   │  │  ├─ ☑ Read auto-accept
   │  │  ├─ ☑ Write auto-accept
   │  │  ├─ Copy ID
   │  │  └─ ✕ Remove
   │  └─ + Add spreadsheet…
   └─ Filters
      ├─ File type allowlist
      │  ├─ Google Doc
      │  │  └─ ✕ Remove
      │  ├─ Plain text
      │  │  └─ ✕ Remove
      │  └─ + Add file type…
      ├─ ☑ I am the owner
      ├─ ☑ Created this session
      └─ ☐ Shared drive exclusion
```

Each **+ Add …** item triggers the resource type's picker (§3.5); each existing grant is its own
submenu keyed by resolved name, with capability checkboxes rendered as ordinary toggle
`rumps.MenuItem`s (same click-to-toggle pattern the code already uses for `pii_item.state`), a
"Copy ID" convenience item (writes the raw ID to the clipboard via `pbcopy`, since the name is
what's displayed but the ID is sometimes still needed — e.g. to paste into a Drive URL), and its
own `✕ Remove`. This directly satisfies goal 2: adding is a single **+ Add …** action per
resource, and removing is a single **✕ Remove** on that resource's own row — no shared window, no
multi-line text, no accidentally deleting a sibling's value while editing.

Toggling a capability checkbox writes straight back to `auto_accept_grants` (flip one boolean) and
calls the same `_save_and_reload()` path already used today — no new persistence mechanism.

#### 3.6.2 Filters menu

Attribute rules that aren't resource grants (§3.1.2) get the *same* one-item-per-value treatment
for list-valued rules (`trusted_sender_domain`, `label_match`, `approved_recipient_domain`,
`label_name_allowlist`, `file_type_allowlist`) — each value is its own submenu row with its own
✕ Remove, added via **+ Add …** (a single-value prompt, not a multi-line box). Boolean/no-value
rules (`i_am_owner`, `no_attachments`, `i_am_organizer`, …) keep today's single-click toggle
`MenuItem`, and int-valued rules (`age_threshold_days`, `time_window_days`) keep today's one-shot
edit prompt — those two categories were never the multi-line-textbox problem goal 2 describes, so
they're left as-is.

`file_type_allowlist` gets a friendly-name layer purely for display/picking (`Google Doc` →
`application/vnd.google-apps.document`, etc.) via a small static lookup table, in the same spirit
as name resolution even though there's no connector call involved — MIME strings are exactly the
kind of "ID-shaped" value goal 3 is about.

#### 3.6.3 Resolved names are cosmetic

To be explicit about the trust boundary: capability checkboxes and the stored `id` (or Jira/
Confluence `key`) are the only fields `expand_grants()` reads. The `name` field cached alongside
each grant entry in `settings.yaml`, and everything shown in the menu, is **display-only** — an
attacker who could rewrite `settings.yaml`'s `name` fields without touching `id`/capabilities gains
nothing, since the daemon never matches on `name`. This mirrors how `OPERATION_LABELS` today is
already cosmetic relative to the operation-key matching in `TOOL_TO_OPERATION`.

### 3.7 One-time migration

On first load of a version carrying this feature, if `auto_accept_grants` is absent but
`auto_accept_rules` contains a set of entries that exactly matches a manifest resource type's
compiled-output shape (same rule name, same value, present across *all* of that capability's
operation keys — e.g. `approved_folder` with an identical folder-ID list on all three of
`drive.read_file_contents` / `drive.download_file` / `sheets.read_values`), the migration folds
those matching entries into `auto_accept_grants` and removes them from `auto_accept_rules`,
resolving names opportunistically (best-effort, non-blocking) in the background afterward.

Anything that's a **partial** match — e.g. a folder ID present under `drive.read_file_contents`
but not `drive.download_file` — is left untouched under `auto_accept_rules`. Migrating it would
silently *broaden* auto-accept to operation keys the user never explicitly approved, which this
design must not do. A partial match instead surfaces as a one-time menu bar notification
("N existing rules look like they could be simplified — see Auto-accept Rules → Drive → Trusted
Folders") pointing at the manual add flow, rather than being auto-migrated.

The migration runs once, is idempotent (checks a `migrated_to_grants_vN: true` marker in the
config so it never re-runs and re-fights hand edits), and is logged to `logs/privacyfence.log` at
INFO level with a summary of what moved.

---

## 4. Evaluator impact

None. `AutoAcceptEvaluator.should_auto_accept()` / `_evaluate()` in `auto_accept.py` are called
with exactly the same `dict[str, list[dict]]` shape they receive today; `build_effective_rules()`
is the only new call site, and it lives in the config-loading layer (`menu_bar.py` /
`daemon_main.py`), not the gate. `tests/unit/test_auto_accept.py` needs no changes to keep
passing; new tests are additive, targeting `expand_grants()` / `build_effective_rules()` /
the migration function in isolation.

---

## 5. Config example (end state)

```yaml
auto_accept_grants:
  drive:
    folders:
      - id: "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
        name: "Q3 Reports"
        read: true
    sandbox_folders:
      - id: "1CdeFghIJKLmnoPQRstuVWxyz0123456789AbCdEfGh"
        name: "Claude scratch space"
        write: true
  tasks:
    task_lists:
      - id: "MDAwMDAwMDAwMDAwMDAwMDAwMDA6MDow"
        name: "Personal"
        create: true
        edit: true
        complete: true
        move: true

auto_accept_rules:
  contacts.edit:
    - rule: no_contact_info_change
  drive.read_file_contents:
    - rule: file_type_allowlist
      value: ["application/vnd.google-apps.document", "text/plain"]

migrated_to_grants_v1: true
```

Both sections coexist. `auto_accept_rules` remains fully documented in
`docs/TECHNICAL_REFERENCE.md` as the advanced/manual layer; `auto_accept_grants` becomes the
primary, menu-driven path documented alongside it.

---

## 6. Documentation impact (for the implementing PR, not this design)

- `docs/TECHNICAL_REFERENCE.md` — Auto-accept rules section needs a new subsection introducing
  grants, with the per-connector rule tables annotated to show which rows are now
  grant-manageable vs. advanced-only.
- `src/privacyfence/resources/settings.yaml.example` — add an annotated `auto_accept_grants`
  example alongside the existing `auto_accept_rules` example.
- `README.md` menu bar screenshot (`docs/images/screenshots/menubar.png`) will need retaking once
  the new menu tree ships.

---

## 7. Security considerations

- **No change to what gets auto-accepted for a given effective config.** `expand_grants()` is a
  pure, deterministic function from grants to the same rule-list shape a user could already
  produce by hand under `auto_accept_rules` — it doesn't add new matching logic, only a new way to
  author the existing kind of entry.
- **New grants default every capability to `false`.** Adding a folder via **+ Add folder…** creates
  a grant with `read: false, write: false` — the user must explicitly flip a checkbox for the
  grant to do anything, mirroring how adding a rule today requires an explicit value before it can
  match anything.
- **PII detection gate, `review`/`popup` gate assignment, Accept-All, and Accept-for-5-min are
  untouched.** Grants only ever populate the same rule-matching layer those mechanisms already sit
  on top of/around.
- **Name resolution makes no new network calls beyond what the connector is already authorized
  for.** It calls the same client methods (`get_file_metadata`, `list_task_lists`, …) already used
  elsewhere in the daemon, under the same OAuth/session credentials, and only when a live
  connector instance exists (i.e., already authenticated). A resolution failure never blocks or
  changes an auto-accept decision — it only affects a menu label.
- **Resolved `name` fields are cosmetic, not authoritative** (§3.6.3) — re-stated here because it's
  the one place a naive implementation could accidentally introduce a name-based matching path;
  the design explicitly rules that out.
- **Migration never broadens scope** (§3.7) — partial rule sets are left alone rather than folded
  in, so no existing installation gets a wider auto-accept surface than it explicitly configured,
  purely as a side effect of upgrading.

---

## 8. Explicitly out of scope / future work

- **Google Picker API integration** for true Drive folder/file browsing (search-as-you-type,
  breadcrumb navigation) instead of paste-ID-or-URL. This needs its own OAuth consent surface and
  a webview, which is a meaningfully bigger project than this redesign; §3.5's paste-and-confirm
  flow is the pragmatic v1 for Drive/Sheets specifically.
- **Persisting resolved names across daemon restarts beyond the on-disk cache** described in
  §3.4 — the cache is best-effort, not a guarantee; a cold start with no network can still show
  "(resolving…)" briefly.
- **Bulk import** (e.g. paste a list of 10 folder URLs at once) is intentionally not proposed —
  goal 2 asks for individual add/remove, and one-at-a-time with a name-confirmation step is safer
  against paste mistakes than bulk entry.
- Extending the grants model to Gmail label-based rules, since Gmail labels are also enumerable
  (`gmail_list_labels`, already `auto`-gated) and could reasonably become a `LiveList`-picked
  grant type (`gmail.labels`) in a later iteration — deferred here to keep the first version's
  manifest scoped to the resource types explicitly called out by goal 1's motivating examples
  (sandbox folder, task list).

---

## 9. Open questions for review before implementation

1. Task-list capabilities: is a single "Auto-approve edits to this list" toggle (covering
   create/edit/complete/move together) the right default granularity, with per-action expansion
   as an "Advanced" disclosure — or should all five always be shown as separate checkboxes from
   the start? §3.1.1's example shows them separate; a simpler default may serve goal 1 better.
   **Recommendation:** the design as-built above is fine either way; needs a2/3-choice call, not
   an architectural one — decide during implementation with a quick look at real settings.yaml
   files in use.
2. Should `Copy ID` also expose "Open in browser" for Drive folders/spreadsheets (constructing the
   `https://drive.google.com/drive/folders/<id>` URL)? Small addition, not required for the three
   stated goals.
3. Confirm the exact TTL for the in-memory name cache (§3.4 proposes 15 minutes) against how often
   `_rebuild()` actually fires in practice (config change, connector auth change, rules hot-reload)
   — want to avoid both stale names and needless API calls on every menu open.
