# Design: Sheets row/column tools & Docs formatting + partial edits

Status: **design only, not implemented.** No code in this repo has changed as a result of this
document; it exists to be reviewed before any of it is built.

Both proposals extend the `drive_sheets_*` / `drive_write_doc_content` surface already living on
`DriveConnector` (`src/privacyfence/connectors/drive.py`) and `DriveClient`
(`src/privacyfence/drive_client.py`) — Sheets and Docs are not separate connectors, they ride the
Drive connector's OAuth `drive` scope, and these proposals keep that structure rather than
splitting them out.

---

## 1. Sheets: insert/delete rows and columns

### 1.1 Gap today

The Sheets surface can create a new tab (`drive_sheets_add_sheet`), write cell values/formulas
(`drive_sheets_write_range`), rename a tab (`drive_sheets_rename_sheet`), and format a range
(`drive_sheets_format_range`) — but nothing changes a sheet's *shape*. There's no way to insert or
remove rows/columns without dropping to the raw Sheets UI, even though it's one of the most common
edits when an agent is building out a spreadsheet (make room for a new header row, delete a
scratch column).

### 1.2 New tools

Two tools, each covering both dimensions via a `dimension` param, consistent with how
`drive_sheets_format_range` bundles many related concerns behind one call rather than one tool per
formatting attribute:

```python
ToolSpec(
    name="drive_sheets_insert_dimensions",
    description=(
        "Insert blank rows or columns into a sheet tab, shifting existing "
        "content after the insertion point. Values/formulas are untouched, "
        "only their position shifts; formulas referencing shifted cells are "
        "adjusted by the Sheets API automatically."
    ),
    params=[
        ToolParam("spreadsheet_id", "str"),
        ToolParam("sheet_id", "int", description="Numeric tab id, from drive_sheets_get_metadata (not the tab name)"),
        ToolParam("dimension", "str", description='"ROWS" or "COLUMNS"'),
        ToolParam("start_index", "int", description="0-based index to insert before"),
        ToolParam("count", "int", default=1),
        ToolParam("inherit_from_before", "bool", default=True,
                   description="Copy formatting from the row/column before the insertion point (Sheets UI default)"),
    ],
)

ToolSpec(
    name="drive_sheets_delete_dimensions",
    description=(
        "Delete rows or columns from a sheet tab, including any values, "
        "formulas, and formatting they contain. Remaining rows/columns shift "
        "to close the gap. This is destructive — deleted cell content is not "
        "recoverable through PrivacyFence."
    ),
    params=[
        ToolParam("spreadsheet_id", "str"),
        ToolParam("sheet_id", "int"),
        ToolParam("dimension", "str", description='"ROWS" or "COLUMNS"'),
        ToolParam("start_index", "int", description="0-based, inclusive"),
        ToolParam("count", "int", default=1),
    ],
)
```

`start_index`/`count` (rather than `start_index`/`end_index`) matches how `drive_sheets_add_sheet`
already takes `rows`/`cols` as counts, and avoids Claude having to compute an exclusive end index —
the connector handler converts to the Sheets API's `DimensionRange{startIndex, endIndex}` shape
internally. `sheet_id` (numeric), not tab name, matches the existing precedent in
`drive_sheets_rename_sheet` / `drive_sheets_format_range` — both already require the numeric id
because the Sheets API's `batchUpdate` addresses tabs that way.

### 1.3 Client implementation

New `DriveClient` methods mirroring the existing `format_sheet_range` / `add_sheet` /
`rename_sheet` pattern (each is a thin wrapper issuing one `spreadsheets().batchUpdate()` call):

```python
def insert_dimensions(self, spreadsheet_id, sheet_id, dimension, start_index, count, inherit_from_before=True) -> dict:
    # one InsertDimensionRequest, inheritFromBefore per param

def delete_dimensions(self, spreadsheet_id, sheet_id, dimension, start_index, count) -> dict:
    # one DeleteDimensionRequest
```

Both raise `DriveClientError` on failure, consistent with §1.4 of the coding guidelines; the
connector catches and re-raises as `RuntimeError`.

### 1.4 Gating classification — "behaves like formatting"

This is the specific behavior requested, made concrete against the existing mechanism
(`docs/TECHNICAL_REFERENCE.md#auto-accept-rules`, `src/privacyfence/auto_accept.py`):

- **Gate:** `popup` (write direction), same as every other `drive_sheets_*` write.
- **Operation keys:** `sheets.insert_dimensions`, `sheets.delete_dimensions` — new entries in
  `TOOL_TO_OPERATION` (`auto_accept.py:30-87`), one per tool, kept separate rather than shared so
  they can be independently allow-listed (mirrors `sheets.add_sheet` vs `sheets.rename_sheet`
  being distinct despite both being one-shot tab operations).
- **Standing auto-accept rules:** both operation keys get the same rule set already available to
  every other `sheets.*` write op — `i_am_owner`, `approved_sandbox_folder`,
  `created_this_session`, and `approved_spreadsheet` (optionally scoped to one tab via the numeric
  `sheet_id`, same convention as `sheets.rename_sheet`/`sheets.format_range` in
  `TECHNICAL_REFERENCE.md:401-408`). Reuses `_sheet_tab_of`-style scoping already in
  `auto_accept.py`, no new rule types needed. Both operation keys get this regardless of the
  "Accept for 5 min" split below.
- **"Accept for 5 min" — split by destructiveness, decided:**
  - `sheets.insert_dimensions` is added to `TEMP_ACCEPT_ELIGIBLE_OPERATIONS`
    (`auto_accept.py:21-25`), keyed by `spreadsheet_id` — identical treatment to
    `sheets.format_range` today. Inserting blank rows/columns doesn't touch existing content, so it
    carries the same non-destructive risk profile as formatting, and an agent building out a
    sheet's shape (insert a header row, insert a column, insert again) gets one popup per
    spreadsheet per 5 minutes instead of one per call.
  - `sheets.delete_dimensions` is **not** added to `TEMP_ACCEPT_ELIGIBLE_OPERATIONS`. Unlike
    `format_range`, it's genuinely destructive — it removes cell content, not just its appearance,
    and there's no undo path through PrivacyFence (only whatever Sheets' own native version history
    offers). It gets the standing-rule treatment only, same as `sheets.add_sheet` /
    `sheets.rename_sheet`: every call prompts unless a standing `approved_spreadsheet`/ownership
    rule is configured for it. So the two new tools intentionally diverge here —
    `sheets.insert_dimensions` behaves like formatting end-to-end; `sheets.delete_dimensions`
    behaves like formatting only for the standing-rule dimension, not the temp-accept one.

### 1.5 Preview / popup content

Following the `format_range` precedent (metadata summary in `preview`, no cell values) rather than
`write_range`'s (which shows the actual values being written) — dimension changes don't carry cell
content in their own parameters, so there's nothing to minimize:

```
preview = {
    "Spreadsheet": name, "Owner": owner,
    "Tab": sheet title (looked up from sheet_id via get_metadata),
    "Action": "Insert 2 ROWS before index 5" / "Delete 3 COLUMNS starting at index 1",
}
details_text = "<action> in the range shown; other rows/columns shift accordingly but are otherwise unchanged."
```

For delete, `details_text` additionally carries the data-loss sentence from §1.4.

### 1.6 Test/doc coverage checklist (per `docs/coding-and-testing-guidelines.md` §2.6)

- `TestDispatch` case for both new tool names.
- Preview-dict test asserting metadata-only content (no cell values leak into `preview`).
- `assert_all_tools_leave_an_audit_trail` coverage.
- New rows in `docs/TECHNICAL_REFERENCE.md`'s Google Drive tool table (§"Connectors & privacy
  matrix") and in the auto-accept rule catalogue (§"Auto-accept rules", extending the `sheets.*`
  five-operation-key list to seven).
- `config/settings.yaml.example` gets `sheets.insert_dimensions` / `sheets.delete_dimensions`
  example entries alongside the existing `sheets.*` block.
- README's Sheets connector bullet point ("add and rename tabs") gets "insert/delete rows and
  columns" added.

---

## 2. Docs: highlight formatting + partial edits

### 2.1 Gap today

`drive_write_doc_content` (`connectors/drive.py:539-559`, client `drive_client.py:733-785`) is the
only way to put rich content into a Google Doc, and it is a **full-document replace on every
call**: it fetches the doc, issues one `deleteContentRange` spanning the entire existing body, then
re-inserts freshly rendered Markdown. Two consequences:

- **No highlight.** `_markdown_to_docs_requests` (`drive_client.py:102-205`) only recognizes
  headings, bold, italic, links, and lists — there's no syntax for background/highlight color, even
  though the Sheets side already has this concept (`background_color` in
  `drive_sheets_format_range`).
- **No partial edit.** Any change — fixing a typo, highlighting one sentence, adding a paragraph —
  requires Claude to hold and resend the *entire* document's Markdown. That's wasteful, and it's
  risky in a way that's specific to an LLM doing the resending: a doc that's long enough to be
  awkward to regenerate exactly is also long enough for a regeneration to silently drop or garble a
  section the model wasn't focused on. There's also no read-back of a Doc's existing
  formatting (`drive_get_file_content` exports Docs as flat `text/plain`,
  `drive_client.py:469-521`), so even a careful full-rewrite can't reliably preserve formatting it
  didn't itself just apply.

`drive_write_doc_content` stays as-is for what it's good at — generating a new document, or
deliberately replacing one wholesale. The proposals below are additive.

### 2.2 Highlight support

Extend the Markdown dialect `_markdown_to_docs_requests` already parses with `==highlighted
text==` (the de facto "extended Markdown" convention for highlight, used by e.g. Obsidian and
several GFM-adjacent renderers — chosen over inventing new syntax so it's guessable/writable by
Claude without a special prompt). Maps to a Docs API `updateTextStyle` request with
`backgroundColor` set, `fields: "backgroundColor"`, same mechanism `_markdown_to_docs_requests`
already uses for bold/italic/link runs (`drive_client.py:174-190`) — this is a same-shaped
addition to the existing inline-run parser, not a new code path.

- Default highlight color: a fixed yellow (e.g. `#FFF59D`, the closest standard Docs highlight
  swatch) — matches "highlight" as a single concept the way it reads in a word processor's
  toolbar, no color argument needed for the common case.
- Stretch goal, not required for v1: `==text|#RRGGBB==` to pick a color inline, or a
  `default_highlight_color` param on `drive_write_doc_content` or the new partial-edit tools below
  — deferred rather than designed in detail here, since the plain default covers the stated
  request ("highlight") and a color param can be added without breaking the syntax later.
- Update the tool's `description` string (`connectors/drive.py:161-175`) to document the new
  syntax, same as it already documents headings/bold/italic/links/lists inline.

### 2.3 Partial edits

New tool, separate from the full-rewrite `drive_write_doc_content`:

```python
ToolSpec(
    name="drive_docs_edit_content",
    description=(
        "Replace one occurrence of existing text in a Google Doc with new "
        "Markdown, without touching the rest of the document. find_text must "
        "match exactly one location in the document's plain text (as read by "
        "drive_get_file_content) — include enough surrounding context to make "
        "it unique, the same way a unique-match text editor requires. Set "
        "replace_all=true to replace every occurrence instead of requiring "
        "uniqueness."
    ),
    params=[
        ToolParam("file_id", "str"),
        ToolParam("find_text", "str", description="Exact plain-text substring to locate"),
        ToolParam("replace_markdown", "str", description="Markdown to insert in its place (supports the same inline syntax as drive_write_doc_content, including ==highlight==)"),
        ToolParam("replace_all", "bool", default=False),
    ],
)
```

Design choices, and why:

- **Anchor by exact text, not character index.** `drive_get_file_content` gives Claude the doc as
  plain text with no index mapping, so an index-based API (`start_index`/`end_index`) would force
  Claude to guess offsets it has no reliable way to compute — a recipe for off-by-one corruption of
  a document it can't fully see the internal representation of. Matching on `find_text` instead
  mirrors this harness's own file-editing convention (`Edit` tool: `old_string`/`new_string`,
  unique-match-or-fail) precisely because that convention already solves the same problem for an
  LLM caller with imperfect position tracking.
- **Uniqueness requirement, `replace_all` escape hatch.** Same reasoning as the `Edit` tool: silently
  picking "the first match" when `find_text` is ambiguous is how a caller's mental model of what it
  just changed silently diverges from what actually changed. Fail with a clear error ("`find_text`
  matches N locations; add more context or set replace_all=true") rather than guessing.
- **Implementation sketch:** `documents.get` the Doc, walk `body.content` concatenating each
  `textRun.content` while recording the cumulative plain-text offset → Docs API index for each run
  (the same traversal `write_doc_rich_content` already does to find `end_index`, extended to track
  per-run offsets instead of just the final one). Find `find_text` in the concatenated plain text,
  map the match span back to a Docs index range via that offset table, then issue a
  `deleteContentRange` scoped to just that range followed by an `insertText` +
  `_markdown_to_docs_requests`-derived style requests for `replace_markdown` — never a
  `deleteContentRange` over the whole body. This is the core difference from
  `write_doc_rich_content`: the blast radius of one call becomes "the matched span," not "the
  entire document."
- **Formatting-only variant:** for the common case of "highlight this sentence" or "bold this
  heading" without changing the text itself, requiring `replace_markdown` to be a styled copy of
  the same text works but is clunky. A companion tool covers it directly, parameter shape modeled
  on `drive_sheets_format_range`'s "each aspect is opt-in" style:

  ```python
  ToolSpec(
      name="drive_docs_format_content",
      description="Apply formatting to existing text in a Google Doc, locating it the same way as drive_docs_edit_content, without changing the text itself.",
      params=[
          ToolParam("file_id", "str"),
          ToolParam("find_text", "str"),
          ToolParam("bold", "str", default="", description='"true"/"false", omit to leave unchanged'),
          ToolParam("italic", "str", default=""),
          ToolParam("highlight_color", "str", default="", description="hex color, e.g. #FFF59D; empty clears any highlight"),
          ToolParam("text_color", "str", default=""),
          ToolParam("replace_all", "bool", default=False),
      ],
  )
  ```

### 2.4 Gating classification

- **Gate:** `popup` for both new tools, matching every other Docs/Drive write.
- **Operation keys:** `docs.edit_content` (text-changing) and `docs.format_content`
  (styling-only) — new entries in `TOOL_TO_OPERATION`.
- **Standing auto-accept rules:** both get the rule set `drive_write_doc_content` already implies
  via `drive.write_doc` — `i_am_owner`, `approved_sandbox_folder`, `created_this_session` — no new
  rule types.
- **"Accept for 5 min" — decided:** both `docs.edit_content` and `docs.format_content` are added to
  `TEMP_ACCEPT_ELIGIBLE_OPERATIONS`, keyed by `file_id`. `docs.format_content` is the
  non-destructive, direct parallel to `sheets.format_range`. `docs.edit_content` follows
  `sheets.write_range`'s precedent instead: that operation already gets temp-accept treatment today
  despite changing cell content, because it's the kind of write expected to be called repeatedly
  against the same file in a burst (an agent filling in a sheet cell-by-cell) — incremental Doc
  editing (fixing one paragraph, then the next) is the same shape of workload, and a replace is
  scoped to a matched span rather than the whole document the way `sheets.write_range` is scoped to
  one range rather than the whole sheet. This is a different case from `sheets.delete_dimensions`
  in §1.4: that one is irreversible data loss with no PrivacyFence undo path, while a text
  replacement, even a wrong one, leaves recoverable content behind (it can be found and replaced
  back), so the write_range parallel applies here without the same reservation.

### 2.5 Preview / popup content

Unlike today's `drive_write_doc_content` popup (`details_text` = the entire new document's
Markdown, `connectors/drive.py:554`), the partial-edit tools naturally produce a small,
reviewable diff:

```
preview = {"File": name, "Owner": owner, "Match": f'{count} occurrence(s) of "<find_text, truncated>"'}
details_text = "Replacing:\n<matched text>\n\nWith:\n<replace_markdown>"   # or, for format_content:
details_text = "Applying <bold=true, highlight=#FFF59D> to:\n<matched text>"
```

This is a meaningful ergonomic improvement over the status quo independent of the highlight/partial
-edit features themselves: today, editing one sentence in a 2,000-word doc means reviewing a
2,000-word popup to find what actually changed. A found/replaced-snippet-sized `details_text` is
also a better fit for `coding-and-testing-guidelines.md` §1.5's "preview is metadata, full content
in details_text" split, since "full content" is now proportional to the edit instead of the whole
document.

### 2.6 Test/doc coverage checklist

- `TestDispatch` cases for `drive_docs_edit_content` / `drive_docs_format_content`.
- Ambiguous-match and no-match error-path tests (mirrors how `Edit`-style tools are usually tested)
  — including a `replace_all=true` case actually replacing every occurrence.
- Preview-dict test asserting only the matched snippet appears, not full document content beyond
  it, and that `find_text` guides selection without ever landing raw document content in `preview`
  (it stays metadata: file name, owner, match count).
- New rows in `docs/TECHNICAL_REFERENCE.md`'s Drive tool table and auto-accept rule catalogue.
- README's Drive & Docs connector bullet ("write Google Docs") gets "highlight, partial edits"
  noted.
- Explicit test that `drive_write_doc_content` is unchanged (still full-rewrite) — this proposal is
  additive, not a replacement, and a regression that silently made the existing tool partial (or
  vice versa) should fail loudly.

---

## 3. Summary of new operation keys

| Tool | Operation key | Gate | Standing rules | Accept for 5 min |
|---|---|---|---|---|
| `drive_sheets_insert_dimensions` | `sheets.insert_dimensions` | popup | `i_am_owner`, `approved_sandbox_folder`, `created_this_session`, `approved_spreadsheet` | yes (by `spreadsheet_id`) |
| `drive_sheets_delete_dimensions` | `sheets.delete_dimensions` | popup | same as above | **no** — destructive, standing rules only (§1.4) |
| `drive_docs_edit_content` | `docs.edit_content` | popup | `i_am_owner`, `approved_sandbox_folder`, `created_this_session` | yes (by `file_id`) |
| `drive_docs_format_content` | `docs.format_content` | popup | same as above | yes (by `file_id`) |

## 4. Explicitly out of scope for this design

- A dedicated Docs connector split out of Drive — both proposals keep riding the Drive connector,
  consistent with how Sheets already does.
- A structural (non-plain-text) Docs read tool. `find_text`-based anchoring works against the
  existing `drive_get_file_content` plain-text export without needing one; a richer read tool that
  round-trips existing formatting is a plausible future improvement but isn't required to ship
  either feature here.
- Deleting an entire sheet tab. Unrelated gap, already deliberately excluded per
  `TECHNICAL_REFERENCE.md:184-189` (rename-to-mark-for-deletion is the sanctioned path) — row/column
  deletion within a tab is a different, much more routine operation and doesn't reopen that
  decision.
- Customizable highlight color syntax beyond the fixed default (§2.2) — deferred as a stretch goal.
